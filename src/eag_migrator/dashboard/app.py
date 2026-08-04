"""The dashboard: one place to drive and watch the whole migration.

Everything here is a thin wrapper over the same code the CLI calls — there is
no second implementation of anything, so the UI cannot drift from what `eagm`
does.

Safety choices worth knowing about:

  * It binds to localhost and compose publishes it on 127.0.0.1 only. This UI
    holds a session for a live customer system and has buttons that write to
    v3; it is not something to expose on a network.
  * Writing to v3 and rolling back both need an explicit confirmation field, so
    a stray click or a re-POSTed form cannot start a migration.
  * Cookie and token *values* are never rendered, and database URLs are shown
    with the password stripped.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..adapters import ApiSink, SqlSink, SqlSource
from ..config import CONFIG_DIR, PROFILES_DIR, REPORTS_DIR, STATE_DIR, ensure_dirs, load_settings
from ..db import build_engine, probe
from ..discovery import profile_database, render_markdown as render_schema_md, save_profile
from ..mapping import Mapping, dump_mapping, load_mapping
from ..report import render_report_markdown, render_verify_markdown, save_json, save_text
from ..runner import Runner
from ..scaffold import build_draft
from ..state import RunState
from ..verify import verify as run_verify
from .jobs import JobBusy, JobManager

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MAPPING_FILE = CONFIG_DIR / "mapping.yaml"
MAPPING_DRAFT = CONFIG_DIR / "mapping.draft.yaml"
HARVEST_FILE = CONFIG_DIR / "harvest.yaml"
HARVEST_DRAFT = CONFIG_DIR / "harvest.draft.yaml"
STATE_DB = STATE_DIR / "migration.sqlite"
STAGING_DB = STATE_DIR / "staging.sqlite"
SESSION_FILE = STATE_DIR / "session.json"
WEB_CACHE = STATE_DIR / "webcache"

JOB_LOGS = REPORTS_DIR / "jobs"
jobs = JobManager(log_dir=JOB_LOGS)


# --- auth -------------------------------------------------------------------


def _expected_token() -> str | None:
    return (os.getenv("EAGM_DASHBOARD_TOKEN") or "").strip() or None


async def require_token(request: Request) -> None:
    """Optional shared-secret gate.

    The dashboard is localhost-only by default, so this is belt-and-braces for
    anyone who publishes the port anyway. Set EAGM_DASHBOARD_TOKEN to enable.
    """
    expected = _expected_token()
    if not expected:
        return
    supplied = (
        request.query_params.get("token")
        or request.headers.get("x-eagm-token")
        or request.cookies.get("eagm_token")
        or ""
    )
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="dashboard token required")


# --- helpers ----------------------------------------------------------------


def _safe_url(url: str) -> str:
    """A connection string with the password removed."""
    if not url:
        return ""
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return url.split("@")[-1] if "@" in url else url


def _mask_user(value: str) -> str:
    """Enough of the username to confirm the right account, not the whole thing."""
    name, _, domain = value.partition("@")
    head = name[:2] if len(name) > 3 else name[:1]
    masked = f"{head}{'•' * max(len(name) - len(head), 1)}"
    return f"{masked}@{domain}" if domain else masked


def _format_step(step: Any) -> str:
    """Render a transform step readably.

    The YAML form is a dict, and Python's repr of it ({'lookup': {'entity':
    'customer'}}) is implementation detail nobody reviewing a mapping should
    have to read.
    """
    if isinstance(step, str):
        return step
    if isinstance(step, dict) and len(step) == 1:
        name, params = next(iter(step.items()))
        if not isinstance(params, dict) or not params:
            return f"{name}()"
        bits = []
        for key, value in params.items():
            if isinstance(value, bool):
                bits.append(f"{key}={'true' if value else 'false'}")
            elif isinstance(value, dict):
                bits.append(f"{key}={{{len(value)} entries}}")
            elif isinstance(value, (list, tuple)):
                bits.append(f"{key}=[{len(value)} items]")
            else:
                bits.append(f"{key}={value}")
        return f"{name}({', '.join(bits)})"
    return str(step)


def _mapping_status() -> dict[str, Any]:
    if not MAPPING_FILE.exists():
        return {
            "present": False,
            "draft": MAPPING_DRAFT.exists(),
            "message": "no mapping yet — run Discover then Scaffold",
        }
    try:
        mapping = load_mapping(MAPPING_FILE)
        order = mapping.topo_order()
    except Exception as exc:  # noqa: BLE001
        return {"present": True, "valid": False, "message": f"{type(exc).__name__}: {exc}"}

    todos = sum(1 for e in mapping.entities for f in e.fields if f.note)
    todos += sum(1 for e in mapping.entities if e.note)
    open_todos = sum(
        1
        for e in mapping.entities
        for f in e.fields
        if f.note and f.note.startswith("TODO")
    )
    return {
        "present": True,
        "valid": True,
        "fingerprint": mapping.fingerprint(),
        "entities": len(mapping.entities),
        "enabled": len(mapping.active()),
        "order": [e.name for e in order],
        "notes": todos,
        "open_todos": open_todos,
    }


def _staging_status() -> dict[str, Any]:
    if not STAGING_DB.exists():
        return {"present": False, "tables": []}
    from ..web.staging import Staging

    with Staging(STAGING_DB) as staging:
        tables = [{"name": t, "rows": staging.count(t)} for t in staging.tables()]
    return {"present": True, "tables": tables, "path": str(STAGING_DB)}


def _session_status() -> dict[str, Any]:
    if not SESSION_FILE.exists():
        return {"present": False}
    from ..web.session import Session

    session = Session.load(SESSION_FILE)
    return {
        "present": True,
        "origin": session.origin,
        # describe() prints names only; values never reach the browser.
        "describe": session.describe(),
        "age_hours": round(session.age_hours, 1),
        "stale": session.age_hours > 12,
    }


def _harvest_status() -> dict[str, Any]:
    if not HARVEST_FILE.exists():
        return {"present": False, "draft": HARVEST_DRAFT.exists()}
    try:
        from ..web.harvest import load_config

        config = load_config(HARVEST_FILE)
    except Exception as exc:  # noqa: BLE001
        return {"present": True, "valid": False, "message": str(exc)}
    return {
        "present": True,
        "valid": True,
        "base_url": config.site.base_url,
        "requires_auth": config.site.requires_auth,
        "collections": [
            {
                "name": c.name,
                "kind": next(
                    k for k in ("api", "sitemap", "crawl", "static")
                    if getattr(c.discover, k)
                ),
                "fields": len(c.extract.fields),
            }
            for c in config.active()
        ],
    }


_BROWSER_CACHE: dict[str, Any] = {}


def browser_state() -> dict[str, Any]:
    """Cached: launching a browser to test costs a second, and this is polled."""
    if not _BROWSER_CACHE:
        from ..web.capture import browser_status

        _BROWSER_CACHE.update(browser_status())
    return _BROWSER_CACHE


def build_status() -> dict[str, Any]:
    ensure_dirs()
    settings = load_settings()

    sides = []
    for side in ("v2", "v3"):
        url = settings.v2_url if side == "v2" else settings.v3_url
        entry: dict[str, Any] = {"side": side, "url": _safe_url(url), "configured": bool(url)}
        if url:
            ok, message = probe(build_engine(url))
            entry["ok"] = ok
            entry["message"] = message if not ok else "connected"
        sides.append(entry)

    runs: list[dict[str, Any]] = []
    if STATE_DB.exists():
        with RunState(STATE_DB) as state:
            for row in state.list_runs(10):
                cps = state.checkpoints(row["run_id"])
                runs.append(
                    {
                        "run_id": row["run_id"],
                        "mode": row["mode"],
                        "status": row["status"],
                        "started_at": row["started_at"][:19].replace("T", " "),
                        "written": sum(c.written for c in cps),
                        "failed": sum(c.failed for c in cps),
                        "entities_done": sum(1 for c in cps if c.done),
                        "entities": len(cps),
                    }
                )

    current = jobs.current
    recent = jobs.recent(8)
    # The last job whether or not it is still running: without this the log
    # vanishes the moment a job ends, which is exactly when you want to read it.
    last = current or (recent[0] if recent else None)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "databases": sides,
        "session": _session_status(),
        "harvest": _harvest_status(),
        "mapping": _mapping_status(),
        "staging": _staging_status(),
        "runs": runs,
        "job": current.to_dict() if current else None,
        "last_job": last.to_dict() if last else None,
        "recent_jobs": [
            {"id": j.id, "label": j.label, "status": j.status, "kind": j.kind}
            for j in recent
        ],
        "api_sink": bool(settings.v3_api_base_url),
        "browser": browser_state(),
    }


def _runner(state: RunState, mapping: Mapping, job: Any = None) -> Runner:
    settings = load_settings()
    sink = (
        ApiSink(settings.v3_api_base_url, settings.v3_api_token, settings.v3_api_timeout)
        if settings.v3_api_base_url
        else SqlSink(build_engine(settings.url_for("v3")), settings.v3_schema)
    )

    def progress(entity: str, done: int, total: int) -> None:
        if job:
            pct = f"{done / total:.0%}" if total else "?"
            job.say(f"{entity}: {done:,}/{total:,} ({pct})")

    return Runner(
        settings,
        mapping,
        SqlSource(build_engine(settings.url_for("v2")), settings.v2_schema),
        sink,
        state,
        progress=progress,
        should_stop=(job.should_stop if job else None),
    )


# --- app --------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="EAG migrator", docs_url=None, redoc_url=None)

    def page(request: Request, template: str, **context: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            context={
                "token": _expected_token(),
                # Appended to every internal link so the token survives navigation.
                "q": f"?token={_expected_token()}" if _expected_token() else "",
                **context,
            },
        )

    # --- pages --------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_token)])
    def index(request: Request) -> HTMLResponse:
        return page(request, "index.html", status=build_status())

    @app.get("/mapping", response_class=HTMLResponse, dependencies=[Depends(require_token)])
    def mapping_view(request: Request) -> HTMLResponse:
        if not MAPPING_FILE.exists():
            return page(request, "mapping.html", mapping=None, raw=None, error=None)
        try:
            mapping = load_mapping(MAPPING_FILE)
            entities = [
                {
                    "name": e.name,
                    "enabled": e.enabled,
                    "source": e.source.table,
                    "target": e.target.table,
                    "key": e.source.key,
                    "map_key": e.map_key,
                    "depends_on": e.depends_on,
                    "note": e.note,
                    "fields": [
                        {
                            "to": f.to,
                            "from": f.from_,
                            "transform": [_format_step(t) for t in f.transform],
                            "required": f.required,
                            "note": f.note,
                        }
                        for f in e.fields
                    ],
                }
                for e in mapping.topo_order()
            ]
            return page(
                request,
                "mapping.html",
                mapping=entities,
                raw=MAPPING_FILE.read_text(encoding="utf-8"),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            return page(
                request,
                "mapping.html",
                mapping=None,
                raw=MAPPING_FILE.read_text(encoding="utf-8"),
                error=f"{type(exc).__name__}: {exc}",
            )

    @app.get("/staging", response_class=HTMLResponse, dependencies=[Depends(require_token)])
    def staging_view(
        request: Request,
        table: str | None = Query(None),
        limit: int = Query(50, le=500),
    ) -> HTMLResponse:
        status = _staging_status()
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        if table and status["present"]:
            from ..web.staging import Staging

            known = {t["name"] for t in status["tables"]}
            if table not in known:
                raise HTTPException(404, "no such collection")
            with Staging(STAGING_DB) as staging:
                rows = staging.sample(table, limit)
            columns = list(rows[0].keys()) if rows else []
        return page(
            request, "staging.html", staging=status, table=table, rows=rows, columns=columns
        )

    @app.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(require_token)])
    def jobs_index(request: Request, job: str | None = Query(None)) -> HTMLResponse:
        recent = jobs.recent(25)
        chosen = jobs.get(job) if job else (recent[0] if recent else None)
        return page(
            request,
            "jobs.html",
            recent=[j.to_dict() for j in recent],
            job=chosen.to_dict() if chosen else None,
        )

    @app.get("/reports", response_class=HTMLResponse, dependencies=[Depends(require_token)])
    def reports_index(request: Request) -> HTMLResponse:
        files = []
        for folder, label in ((REPORTS_DIR, "reports"), (PROFILES_DIR, "profiles")):
            for path in sorted(folder.glob("*.md"), key=lambda p: -p.stat().st_mtime):
                files.append(
                    {
                        "name": path.name,
                        "folder": label,
                        "size": path.stat().st_size,
                        "modified": dt.datetime.fromtimestamp(
                            path.stat().st_mtime
                        ).strftime("%Y-%m-%d %H:%M"),
                    }
                )
        return page(request, "reports.html", files=files, body=None, title=None)

    @app.get("/reports/{folder}/{name}", response_class=HTMLResponse,
             dependencies=[Depends(require_token)])
    def report_view(request: Request, folder: str, name: str) -> HTMLResponse:
        if folder not in ("reports", "profiles"):
            raise HTTPException(404, "unknown folder")
        base = REPORTS_DIR if folder == "reports" else PROFILES_DIR
        target = (base / name).resolve()
        # Never let a crafted name walk out of the reports directory.
        if base.resolve() not in target.parents or not target.is_file():
            raise HTTPException(404, "no such report")

        import markdown as md

        body = md.markdown(
            target.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
        )
        return page(request, "reports.html", files=None, body=body, title=name)

    # --- status / jobs ------------------------------------------------------

    @app.get("/api/status", dependencies=[Depends(require_token)])
    def api_status() -> JSONResponse:
        return JSONResponse(build_status())

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
    def api_job(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return JSONResponse(job.to_dict())

    @app.get("/api/jobs/{job_id}/stream", dependencies=[Depends(require_token)])
    async def api_job_stream(job_id: str, since: int = Query(0)) -> Any:
        """Server-sent events: push each log line as it happens.

        Polling worked but lagged a second behind and re-sent the whole log
        each time. This pushes only what is new, and keeps streaming until the
        job ends so the browser sees the final status without another request.
        """
        import asyncio

        from fastapi.responses import StreamingResponse

        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")

        async def events() -> Any:
            offset = since
            while True:
                lines, offset = job.tail(offset)
                if lines:
                    payload = json.dumps({"lines": lines, "offset": offset})
                    yield f"event: log\ndata: {payload}\n\n"
                if not job.active:
                    done = json.dumps(
                        {
                            "status": job.status,
                            "error": job.error,
                            "result": job.result,
                            "finished_at": job.finished_at,
                        }
                    )
                    yield f"event: end\ndata: {done}\n\n"
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/jobs/{job_id}/stop", dependencies=[Depends(require_token)])
    def api_stop(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        job.stop()
        return JSONResponse(job.to_dict())

    # --- actions ------------------------------------------------------------

    def _back(request: Request, message: str | None = None, error: str | None = None):
        # Messages carry exception text, which can contain & or # — encode it
        # rather than let it split the query string.
        from urllib.parse import urlencode

        params = {}
        if _expected_token():
            params["token"] = _expected_token()
        if message:
            params["msg"] = message[:300]
        if error:
            params["err"] = error[:300]
        url = "/" + (f"?{urlencode(params)}" if params else "")
        return RedirectResponse(url, status_code=303)

    def _launch(request: Request, kind: str, label: str, work):
        try:
            jobs.start(kind, label, work)
        except JobBusy as exc:
            return _back(request, error=str(exc))
        return _back(request, message=f"{label} started")

    @app.post("/actions/discover", dependencies=[Depends(require_token)])
    def action_discover(request: Request, side: str = Form("both")) -> Any:
        def work(job: Any) -> dict[str, Any]:
            settings = load_settings()
            done = []
            for s in (["v2", "v3"] if side == "both" else [side]):
                url = settings.v2_url if s == "v2" else settings.v3_url
                if not url:
                    job.say(f"{s}: no URL configured, skipping")
                    continue
                job.say(f"introspecting {s}...")
                profile = profile_database(
                    build_engine(url),
                    s,
                    schema=settings.schema_for(s),
                    redactor=settings.redactor(),
                )
                save_profile(profile, PROFILES_DIR / f"{s}.json")
                save_text(render_schema_md(profile), PROFILES_DIR / f"{s}.md")
                inferred = sum(len(t.inferred_foreign_keys) for t in profile.tables)
                job.say(
                    f"{s}: {len(profile.tables)} tables, "
                    f"{sum(t.row_count or 0 for t in profile.tables):,} rows, "
                    f"{inferred} inferred relationship(s)"
                )
                done.append(s)
            return {"sides": done}

        return _launch(request, "discover", f"Discover {side}", work)

    @app.post("/actions/scaffold", dependencies=[Depends(require_token)])
    def action_scaffold(request: Request) -> Any:
        def work(job: Any) -> dict[str, Any]:
            from ..discovery import load_profile

            v2_path = PROFILES_DIR / "v2.json"
            if not v2_path.exists():
                raise RuntimeError("no v2 profile yet — run Discover first")
            v3_path = PROFILES_DIR / "v3.json"
            mapping, warnings = build_draft(
                load_profile(v2_path),
                load_profile(v3_path) if v3_path.exists() else None,
            )
            dump_mapping(mapping, MAPPING_DRAFT)
            job.say(f"drafted {len(mapping.entities)} entities -> {MAPPING_DRAFT}")
            for warning in warnings[:40]:
                job.say(f"  ! {warning}")
            return {"entities": len(mapping.entities), "warnings": len(warnings)}

        return _launch(request, "scaffold", "Scaffold mapping", work)

    @app.post("/actions/promote", dependencies=[Depends(require_token)])
    def action_promote(request: Request, which: str = Form(...)) -> Any:
        pairs = {"mapping": (MAPPING_DRAFT, MAPPING_FILE), "harvest": (HARVEST_DRAFT, HARVEST_FILE)}
        if which not in pairs:
            return _back(request, error="unknown draft")
        draft, live = pairs[which]
        if not draft.exists():
            return _back(request, error=f"no {which} draft to promote")
        live.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
        draft.unlink()
        return _back(request, message=f"{which} draft promoted")

    @app.post("/actions/plan", dependencies=[Depends(require_token)])
    def action_plan(request: Request, limit: int = Form(0)) -> Any:
        def work(job: Any) -> dict[str, Any]:
            mapping = load_mapping(MAPPING_FILE)
            with RunState(STATE_DB) as state:
                report = _runner(state, mapping, job).plan(limit=limit)
            save_json(report.to_dict(), REPORTS_DIR / f"plan-{report.run_id}.json")
            save_text(render_report_markdown(report), REPORTS_DIR / f"plan-{report.run_id}.md")
            job.say(
                f"{report.total_failed:,} row(s) would fail; nothing was written"
            )
            return {"run_id": report.run_id, "failed": report.total_failed}

        return _launch(request, "plan", "Dry run", work)

    @app.post("/actions/run", dependencies=[Depends(require_token)])
    def action_run(
        request: Request,
        confirm: str = Form(""),
        resume: str = Form(""),
        limit: int = Form(0),
    ) -> Any:
        # Writing to v3 is the one irreversible-ish action here. An explicit
        # confirmation means a stray click or a re-POSTed form cannot start it.
        if confirm != "MIGRATE":
            return _back(request, error="type MIGRATE to confirm writing to v3")

        def work(job: Any) -> dict[str, Any]:
            mapping = load_mapping(MAPPING_FILE)
            with RunState(STATE_DB) as state:
                report = _runner(state, mapping, job).apply(
                    resume_run_id=resume or None, limit=limit
                )
            save_json(report.to_dict(), REPORTS_DIR / f"run-{report.run_id}.json")
            save_text(render_report_markdown(report), REPORTS_DIR / f"run-{report.run_id}.md")
            job.say(
                f"wrote {report.total_written:,} row(s), {report.total_failed:,} failed"
            )
            return {
                "run_id": report.run_id,
                "written": report.total_written,
                "failed": report.total_failed,
            }

        return _launch(request, "run", "Migrate to v3", work)

    @app.post("/actions/verify", dependencies=[Depends(require_token)])
    def action_verify(request: Request, run_id: str = Form("")) -> Any:
        def work(job: Any) -> dict[str, Any]:
            mapping = load_mapping(MAPPING_FILE)
            with RunState(STATE_DB) as state:
                target = run_id
                if not target:
                    latest = state.latest_run("apply")
                    if not latest:
                        raise RuntimeError("no apply run to verify")
                    target = latest["run_id"]
                report = run_verify(_runner(state, mapping, job), mapping, state, target)
            save_json(report.to_dict(), REPORTS_DIR / f"verify-{target}.json")
            save_text(render_verify_markdown(report), REPORTS_DIR / f"verify-{target}.md")
            job.say("verification passed" if report.ok else "verification found problems")
            return {"run_id": target, "ok": report.ok}

        return _launch(request, "verify", "Verify", work)

    @app.post("/actions/rollback", dependencies=[Depends(require_token)])
    def action_rollback(
        request: Request, run_id: str = Form(...), confirm: str = Form("")
    ) -> Any:
        if confirm != "ROLLBACK":
            return _back(request, error="type ROLLBACK to confirm undoing that run")

        def work(job: Any) -> dict[str, Any]:
            mapping = load_mapping(MAPPING_FILE)
            with RunState(STATE_DB) as state:
                result = _runner(state, mapping, job).rollback(run_id)
            job.say(f"undid {result['rows_undone']:,} row(s)")
            return result

        return _launch(request, "rollback", f"Roll back {run_id}", work)

    # --- web source actions -------------------------------------------------

    @app.post("/actions/login", dependencies=[Depends(require_token)])
    def action_login(request: Request, url: str = Form(...), cookies: str = Form("")) -> Any:
        from ..web.fetcher import Fetcher
        from ..web.login import import_session
        from ..web.session import Session

        try:
            session = (
                import_session(cookies, url)
                if cookies.strip()
                else Session.from_env(url)
            )
            if session is None:
                return _back(request, error="paste a Cookie header, or set EAGM_COOKIE")
            with Fetcher(url, cache_dir=None, use_cache=False, rate_limit_rps=0,
                         respect_robots=False, session=session) as fetcher:
                resp = fetcher.get(url)
            if resp.status in (401, 403):
                return _back(request, error=f"session rejected (HTTP {resp.status})")
            session.save(SESSION_FILE)
        except Exception as exc:  # noqa: BLE001
            return _back(request, error=f"{type(exc).__name__}: {exc}")
        return _back(request, message="session saved")

    @app.post("/actions/login-form", dependencies=[Depends(require_token)])
    def action_login_form(
        request: Request,
        url: str = Form(...),
        username: str = Form(...),
        password: str = Form(...),
        login_url: str = Form("/login"),
        success_selector: str = Form(""),
    ) -> Any:
        """Sign in by driving the app's own login page in a real browser.

        The credentials fill the form and are then dropped — only the resulting
        cookies and auth headers are stored. They are deliberately not written
        to the job log, the session file or anywhere else.
        """
        from ..web.login import LoginSpec

        spec = LoginSpec(
            login_url=login_url or "/login",
            success_selector=success_selector.strip() or None,
        )

        def work(job: Any) -> dict[str, Any]:
            from ..web.login import form_login

            job.say(f"opening {url.rstrip('/')}/{spec.login_url.lstrip('/')}")
            job.say(f"signing in as {_mask_user(username)}")
            session = form_login(url, spec, username=username, password=password)
            session.save(SESSION_FILE)
            job.say(f"signed in — {session.describe()}")
            if not spec.success_selector:
                job.say(
                    "no success selector was given, so this only checked that the "
                    "page moved off the login URL. Set one for a real check."
                )
            return {"cookies": len(session.cookies), "headers": len(session.headers)}

        return _launch(request, "login", "Sign in to v2", work)

    @app.post("/actions/logout", dependencies=[Depends(require_token)])
    def action_logout(request: Request) -> Any:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        return _back(request, message="session deleted")

    @app.post("/actions/recon", dependencies=[Depends(require_token)])
    def action_recon(request: Request, url: str = Form(...)) -> Any:
        def work(job: Any) -> dict[str, Any]:
            from ..web.fetcher import Fetcher
            from ..web.recon import recon, render_markdown as render_recon
            from ..web.session import Session

            session = Session.load(SESSION_FILE) if SESSION_FILE.exists() else None
            with Fetcher(url, cache_dir=WEB_CACHE, rate_limit_rps=1.0,
                         session=session) as fetcher:
                profile = recon(fetcher)
            save_json(profile.to_dict(), PROFILES_DIR / "site.json")
            save_text(render_recon(profile), PROFILES_DIR / "site.md")

            if profile.requires_auth:
                job.say("this is an application behind a login:")
                for item in profile.auth_evidence:
                    job.say(f"  - {item}")
                job.say("sign in above, then run Capture")
            else:
                job.say(f"{len(profile.urls)} URL(s), "
                        f"{len(profile.content_types)} JSON content type(s)")
            return {"requires_auth": profile.requires_auth, "urls": len(profile.urls)}

        return _launch(request, "recon", "Recon site", work)

    @app.post("/actions/capture", dependencies=[Depends(require_token)])
    def action_capture(
        request: Request,
        url: str = Form(...),
        paths: str = Form("/"),
        explore: str = Form(""),
        max_pages: int = Form(25),
    ) -> Any:
        def work(job: Any) -> dict[str, Any]:
            from ..web.capture import capture, render_markdown as render_capture
            from ..web.draft import draft_from_capture
            from ..web.harvest import dump_config
            from ..web.session import Session

            session = Session.load(SESSION_FILE) if SESSION_FILE.exists() else None
            wanted = [p.strip() for p in paths.replace(",", "\n").splitlines() if p.strip()]
            job.say(
                f"driving {len(wanted)} page(s)"
                f"{' and following the app navigation' if explore else ''}"
                f"{' (authenticated)' if session else ''}"
            )

            report = capture(
                url,
                wanted or ["/"],
                har_path=REPORTS_DIR / "capture.har",
                session=session,
                explore=bool(explore),
                max_pages=max_pages,
            )
            if report.skipped_links:
                job.say(
                    f"left {len(report.skipped_links)} link(s) alone "
                    f"(state changes, downloads, logout)"
                )
                for link in report.skipped_links[:6]:
                    job.say(f"  · {link.url} — {link.reason}")
            save_json(report.to_dict(), REPORTS_DIR / "capture.json")
            save_text(render_capture(report), REPORTS_DIR / "capture.md")
            job.say(f"recorded {len(report.api_calls)} JSON endpoint(s)")

            if session is not None and report.auth_headers:
                added = session.adopt_headers(report.auth_headers)
                if added:
                    session.save(SESSION_FILE)
                    job.say(f"adopted {added} auth header(s) the app's JS sent")

            if report.api_calls:
                config, warnings = draft_from_capture(report, url)
                dump_config(config, HARVEST_DRAFT)
                job.say(f"drafted {len(config.collections)} collection(s) -> {HARVEST_DRAFT}")
                for warning in warnings[:20]:
                    job.say(f"  ! {warning}")
            elif report.pages_visited:
                # Nothing to capture because the app renders on the server —
                # so the records are in the markup, not behind an endpoint.
                job.say("no JSON endpoints; reading the HTML of those pages instead")
                _draft_html(job, url, report.pages_visited, session)
            return {"endpoints": len(report.api_calls)}

        return _launch(request, "capture", "Capture network calls", work)

    def _draft_html(job: Any, base_url: str, urls: list[str], session: Any) -> int:
        """Turn the repeated markup on each list screen into a harvest config."""
        from ..web.draft import draft_from_pages
        from ..web.fetcher import Fetcher
        from ..web.harvest import dump_config

        pages: list[tuple[str, str]] = []
        with Fetcher(base_url, cache_dir=WEB_CACHE, respect_robots=False,
                     session=session) as fetcher:
            for one in urls:
                resp = fetcher.get(one)
                if resp.ok and "html" in (resp.content_type or "html"):
                    pages.append((resp.url, resp.text))
                else:
                    job.say(f"  · {_safe_url(one)}: HTTP {resp.status}, skipped")

        config, warnings = draft_from_pages(pages, base_url)
        dump_config(config, HARVEST_DRAFT)
        for collection in config.collections:
            job.say(
                f"  {collection.name}: {collection.extract.rows} "
                f"({len(collection.extract.fields)} field(s), "
                f"key={collection.key or 'none'})"
            )
        job.say(f"drafted {len(config.collections)} collection(s) -> {HARVEST_DRAFT}")
        for warning in warnings[:20]:
            job.say(f"  ! {warning}")
        return len(config.collections)

    @app.post("/actions/draft-html", dependencies=[Depends(require_token)])
    def action_draft_html(
        request: Request, url: str = Form(...), paths: str = Form("")
    ) -> Any:
        def work(job: Any) -> dict[str, Any]:
            from ..web.session import Session

            session = Session.load(SESSION_FILE) if SESSION_FILE.exists() else None
            extra = [p.strip() for p in paths.replace(",", "\n").splitlines() if p.strip()]
            job.say(
                f"reading {1 + len(extra)} screen(s)"
                f"{' (authenticated)' if session else ' anonymously'}"
            )
            drafted = _draft_html(job, url, [url, *extra], session)
            if not drafted:
                job.say("nothing repeated found — are those list screens?")
            return {"collections": drafted}

        return _launch(request, "draft-html", "Draft from HTML", work)

    @app.post("/actions/harvest", dependencies=[Depends(require_token)])
    def action_harvest(request: Request, limit: int = Form(0)) -> Any:
        def work(job: Any) -> dict[str, Any]:
            from ..web.harvest import harvest, load_config
            from ..web.harvest import render_markdown as render_harvest
            from ..web.session import Session
            from ..web.staging import Staging

            config = load_config(HARVEST_FILE)
            session = Session.load(SESSION_FILE) if SESSION_FILE.exists() else None

            def say(name: str, done: int, total: int, note: str = "") -> None:
                where = f"{done:,}/{total:,}" if total else f"{done:,}"
                job.say(f"{name}: {where}{'  ' + note if note else ''}")

            with Staging(STAGING_DB) as staging:
                report = harvest(
                    config, staging, cache_dir=WEB_CACHE, limit=limit,
                    progress=say, session=session,
                )
            save_text(render_harvest(report), REPORTS_DIR / "harvest.md")

            for collection in report.collections:
                for item in collection.scrubbed.summary():
                    job.say(f"  {collection.name}: {item}")
            job.say(f"stored {report.total_stored:,} record(s)")
            return {"stored": report.total_stored, "failed": report.total_failed}

        return _launch(request, "harvest", "Harvest v2", work)

    return app
