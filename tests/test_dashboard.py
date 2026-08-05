"""The dashboard.

Weighted towards the things that would actually hurt: leaking a session
credential into a page, starting a migration by accident, or letting a crafted
URL read files outside the reports directory.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard extras not installed")

from fastapi.testclient import TestClient  # noqa: E402

from eag_migrator.dashboard import app as dash  # noqa: E402
from eag_migrator.dashboard.jobs import JobBusy, JobManager  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    """Point every dashboard path at a scratch directory."""
    for name in ("CONFIG_DIR", "PROFILES_DIR", "REPORTS_DIR", "STATE_DIR"):
        folder = tmp_path / name.split("_")[0].lower()
        folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dash, name, folder)

    state = tmp_path / "state"
    monkeypatch.setattr(dash, "STATE_DB", state / "migration.sqlite")
    monkeypatch.setattr(dash, "STAGING_DB", state / "staging.sqlite")
    monkeypatch.setattr(dash, "SESSION_FILE", state / "session.json")
    monkeypatch.setattr(dash, "WEB_CACHE", state / "webcache")
    monkeypatch.setattr(dash, "V3_SESSION_FILE", state / "v3_session.json")
    for name in ("V3_API_BASE_URL", "V3_API_TOKEN", "V3_API_COOKIE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dash, "MAPPING_FILE", tmp_path / "config" / "mapping.yaml")
    monkeypatch.setattr(dash, "MAPPING_DRAFT", tmp_path / "config" / "mapping.draft.yaml")
    monkeypatch.setattr(dash, "HARVEST_FILE", tmp_path / "config" / "harvest.yaml")
    monkeypatch.setattr(dash, "HARVEST_DRAFT", tmp_path / "config" / "harvest.draft.yaml")
    monkeypatch.setattr(dash, "jobs", JobManager())

    monkeypatch.delenv("V2_DATABASE_URL", raising=False)
    monkeypatch.delenv("V3_DATABASE_URL", raising=False)
    monkeypatch.delenv("EAGM_DASHBOARD_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def client(workspace):
    return TestClient(dash.create_app())


# --- it loads ---------------------------------------------------------------


def test_overview_renders_with_nothing_configured(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "EAG migrator" in res.text
    assert "not configured" in res.text


def test_status_endpoint_describes_the_whole_pipeline(client):
    body = client.get("/api/status").json()
    assert {"databases", "session", "harvest", "mapping", "staging", "runs"} <= set(body)
    assert body["mapping"]["present"] is False


# --- credentials never reach the browser ------------------------------------


def test_session_values_are_never_rendered(client, workspace):
    """The page shows that a session exists, never what it is."""
    from eag_migrator.web.session import Session

    session = Session.from_cookie_header("sid=SUPER-SECRET-VALUE", "https://v2.example")
    session.headers["Authorization"] = "Bearer TOP-SECRET-TOKEN"
    session.save(dash.SESSION_FILE)

    page = client.get("/").text
    assert "SUPER-SECRET-VALUE" not in page
    assert "TOP-SECRET-TOKEN" not in page
    assert "sid" in page                       # the name is fine
    assert "withheld" in page

    body = client.get("/api/status").json()
    assert "SUPER-SECRET-VALUE" not in str(body)
    assert "TOP-SECRET-TOKEN" not in str(body)


def test_database_passwords_are_stripped(client, monkeypatch):
    monkeypatch.setenv(
        "V2_DATABASE_URL", "postgresql+psycopg://eag:hunter2@db.internal:5432/eag"
    )
    page = client.get("/").text
    assert "hunter2" not in page
    assert "db.internal" in page               # host is useful, password is not


# --- destructive actions need confirming ------------------------------------


def test_migration_will_not_start_without_the_confirmation_word(client):
    res = client.post("/actions/run", data={"confirm": ""}, follow_redirects=False)
    assert res.status_code == 303
    assert "MIGRATE" in res.headers["location"]
    assert dash.jobs.current is None           # nothing was started


def test_rollback_will_not_start_without_the_confirmation_word(client):
    res = client.post(
        "/actions/rollback",
        data={"run_id": "whatever", "confirm": "yes"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "ROLLBACK" in res.headers["location"]
    assert dash.jobs.current is None


# --- path traversal ---------------------------------------------------------


def test_reports_cannot_be_walked_out_of(client, workspace):
    (workspace / "secret.md").write_text("private")
    (dash.REPORTS_DIR / "fine.md").write_text("# fine")

    assert client.get("/reports/reports/fine.md").status_code == 200
    for attempt in ("../secret.md", "..%2Fsecret.md", "....//secret.md"):
        assert client.get(f"/reports/reports/{attempt}").status_code == 404
    assert client.get("/reports/etc/passwd").status_code == 404


def test_staging_rejects_an_unknown_table(client, workspace):
    dash.STAGING_DB.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(dash.STAGING_DB).executescript(
        "CREATE TABLE customers (_id INTEGER PRIMARY KEY, _key TEXT, name TEXT);"
        "INSERT INTO customers VALUES (1,'k','Dana');"
    )
    assert client.get("/staging?table=customers").status_code == 200
    assert "Dana" in client.get("/staging?table=customers").text
    assert client.get("/staging?table=sqlite_master").status_code == 404
    assert client.get("/staging?table=nope").status_code == 404


# --- token gate -------------------------------------------------------------


def test_token_gate_blocks_and_admits(workspace, monkeypatch):
    monkeypatch.setenv("EAGM_DASHBOARD_TOKEN", "s3cret")
    guarded = TestClient(dash.create_app())

    assert guarded.get("/").status_code == 401
    assert guarded.get("/api/status").status_code == 401
    assert guarded.post("/actions/discover", data={"side": "v2"}).status_code == 401

    assert guarded.get("/?token=s3cret").status_code == 200
    assert guarded.get("/api/status", headers={"x-eagm-token": "s3cret"}).status_code == 200
    assert guarded.get("/?token=wrong").status_code == 401


# --- jobs -------------------------------------------------------------------


def _wait(job, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while job.active and time.time() < deadline:
        time.sleep(0.02)


def test_jobs_run_one_at_a_time():
    """Two concurrent migrations would interleave writes and checkpoints."""
    manager = JobManager()
    gate = {"open": False}

    first = manager.start("slow", "first", lambda job: [
        time.sleep(0.01) for _ in range(200) if not gate["open"]
    ] and None)

    with pytest.raises(JobBusy, match="still running"):
        manager.start("other", "second", lambda job: None)

    gate["open"] = True
    _wait(first)
    manager.start("other", "second", lambda job: None)   # fine once the first ends


def test_job_failure_is_captured_not_crashed():
    manager = JobManager()

    def explode(job):
        raise RuntimeError("the database went away")

    job = manager.start("boom", "explodes", explode)
    _wait(job)

    assert job.status == "failed"
    assert "the database went away" in job.error
    assert any("FAILED" in line for line in job.log)


def test_stop_is_cooperative_and_reported():
    manager = JobManager()

    def loop(job):
        for _ in range(500):
            if job.should_stop():
                return {"stopped_early": True}
            time.sleep(0.005)
        return {"stopped_early": False}

    job = manager.start("loop", "long thing", loop)
    time.sleep(0.05)
    job.stop()
    _wait(job)

    assert job.status == "stopped"
    assert job.result == {"stopped_early": True}
    assert any("stop requested" in line for line in job.log)


def test_job_log_is_bounded():
    """A chatty job must not grow without limit."""
    manager = JobManager()
    job = manager.start("chatty", "lots of output",
                        lambda j: [j.say(f"line {i}") for i in range(3000)] and None)
    _wait(job, timeout=10)

    assert len(job.log) <= 2000
    assert "line 2999" in job.log[-2]          # the tail is what was kept


# --- the runner honours the stop flag ---------------------------------------


def test_runner_stops_between_batches_and_stays_resumable(tmp_path):
    """Stopping mid-run must leave a checkpoint, not a half-written mess."""
    from eag_migrator.adapters import SqlSink, SqlSource
    from eag_migrator.config import Settings
    from eag_migrator.db import build_engine
    from eag_migrator.mapping import Mapping
    from eag_migrator.runner import Runner
    from eag_migrator.state import RunState

    v2, v3 = tmp_path / "v2.sqlite", tmp_path / "v3.sqlite"
    src = sqlite3.connect(v2)
    src.executescript("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);")
    src.executemany(
        "INSERT INTO people VALUES (?,?)", [(i, f"P{i}") for i in range(1, 21)]
    )
    src.commit()
    sqlite3.connect(v3).executescript(
        "CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " legacy_id INTEGER, name TEXT);"
    )

    mapping = Mapping.model_validate({
        "entities": [{
            "name": "people",
            "source": {"table": "people", "key": "id"},
            "target": {"table": "people", "key": "id"},
            "fields": [{"to": "legacy_id", "from": "id"}, {"to": "name", "from": "name"}],
        }]
    })

    # Trip the stop flag once, after the first batch — not on every batch,
    # or the resume would stop again immediately.
    stop = {"now": False, "armed": True}

    def trip(*_args):
        if stop["armed"]:
            stop["now"] = True
            stop["armed"] = False
    settings = Settings(v2_url=f"sqlite:///{v2}", v3_url=f"sqlite:///{v3}", batch_size=5)
    with RunState(tmp_path / "state.sqlite") as state:
        runner = Runner(
            settings, mapping,
            SqlSource(build_engine(f"sqlite:///{v2}")),
            SqlSink(build_engine(f"sqlite:///{v3}")),
            state,
            progress=trip,
            should_stop=lambda: stop["now"],
        )
        first = runner.apply()

        written = sqlite3.connect(v3).execute("SELECT COUNT(*) FROM people").fetchone()[0]
        assert 0 < written < 20
        assert any("stopped on request" in n for e in first.entities for n in e.notes)

        # Resuming finishes the job without duplicating what already landed.
        stop["now"] = False
        runner.apply(resume_run_id=first.run_id)

    rows = sqlite3.connect(v3).execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert rows == 20


# --- port selection ---------------------------------------------------------


def test_default_port_avoids_the_usual_suspects():
    """8080 and friends are contended; the project's own ports are taken."""
    from eag_migrator.dashboard.net import DEFAULT_PORT

    contended = {80, 443, 3000, 3001, 4200, 5000, 5173, 8000, 8008, 8080, 8081,
                 8443, 8888, 9000, 9090, 5432, 3306, 6379, 27017}
    project = {13306, 13307, 15432, 15433, 18080}   # already published by compose
    assert DEFAULT_PORT not in contended
    assert DEFAULT_PORT not in project
    # Clear of the Linux ephemeral range, which the OS hands out to clients.
    assert 1024 < DEFAULT_PORT < 32768


def test_port_comes_from_the_environment(monkeypatch):
    from eag_migrator.dashboard.net import DEFAULT_PORT, configured_port

    monkeypatch.delenv("EAGM_DASHBOARD_PORT", raising=False)
    assert configured_port() == DEFAULT_PORT

    monkeypatch.setenv("EAGM_DASHBOARD_PORT", "12345")
    assert configured_port() == 12345

    # Garbage falls back rather than crashing at startup.
    for junk in ("", "not-a-port", "0", "70000", "-1"):
        monkeypatch.setenv("EAGM_DASHBOARD_PORT", junk)
        assert configured_port() == DEFAULT_PORT


def test_a_port_in_use_is_detected():
    import socket

    from eag_migrator.dashboard.net import find_free, is_free

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]

        assert not is_free(busy)
        alternative = find_free(near=busy)
        assert alternative != busy
        assert is_free(alternative)

    # Released once the listener closes.
    assert is_free(busy)


def test_suggestion_stays_near_the_requested_port():
    """A nearby port keeps the URL recognisable instead of jumping to random."""
    import socket

    from eag_migrator.dashboard.net import find_free

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        suggestion = find_free(near=busy)

    assert busy < suggestion <= busy + 12 or suggestion > 1024


def test_dashboard_refuses_to_start_on_a_busy_port(monkeypatch):
    """It should say so, not surface a traceback from inside the server."""
    import socket

    from typer.testing import CliRunner

    from eag_migrator.cli import app as cli

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]

        result = CliRunner().invoke(cli, ["dashboard", "--port", str(busy)])

    assert result.exit_code == 1
    assert "already in use" in result.output
    assert "--auto-port" in result.output
    assert "EAGM_DASHBOARD_PORT" in result.output


# --- signing in with credentials -------------------------------------------


def test_the_password_is_never_stored_or_logged(workspace, monkeypatch):
    """The credentials fill a form; only cookies and headers may be kept."""
    import eag_migrator.dashboard.app as dashmod
    from eag_migrator.web.session import Session

    seen = {}

    def fake_login(base_url, spec, *, username=None, password=None, headless=True):
        seen["username"] = username
        seen["password"] = password
        session = Session.from_cookie_header("sid=granted", base_url)
        session.headers["X-CSRF-Token"] = "tok"
        return session

    monkeypatch.setattr("eag_migrator.web.login.form_login", fake_login)
    client = TestClient(dashmod.create_app())

    res = client.post(
        "/actions/login-form",
        data={
            "url": "https://v2.example",
            "username": "owner@shop.example",
            "password": "hunter2-super-secret",
            "login_url": "/login",
            "success_selector": ".dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303

    job = dashmod.jobs.recent(1)[0]
    _wait(job)
    assert job.status == "done", job.error

    # It reached the login helper...
    assert seen["password"] == "hunter2-super-secret"
    # ...and went no further than that.
    log = "\n".join(job.log)
    assert "hunter2-super-secret" not in log
    assert "owner@shop.example" not in log        # masked in the log
    assert "ow" in log                            # but identifiable

    stored = dash.SESSION_FILE.read_text()
    assert "hunter2-super-secret" not in stored
    assert "owner@shop.example" not in stored
    assert "granted" in stored                    # the cookie is what we keep

    page = client.get("/").text
    assert "hunter2-super-secret" not in page


def test_a_failed_sign_in_is_reported_without_the_password(workspace, monkeypatch):
    import eag_migrator.dashboard.app as dashmod
    from eag_migrator.web.login import LoginFailed

    def fail(base_url, spec, *, username=None, password=None, headless=True):
        raise LoginFailed("still on the login page — credentials rejected")

    monkeypatch.setattr("eag_migrator.web.login.form_login", fail)
    client = TestClient(dashmod.create_app())

    client.post(
        "/actions/login-form",
        data={"url": "https://v2.example", "username": "u@e.com",
              "password": "wrong-password", "login_url": "/login",
              "success_selector": ""},
        follow_redirects=False,
    )
    job = dashmod.jobs.recent(1)[0]
    _wait(job)

    assert job.status == "failed"
    assert "credentials rejected" in job.error
    assert "wrong-password" not in "\n".join(job.log)
    assert not dash.SESSION_FILE.exists()


DISCOVERY_EXAMPLES = {
    "api": {"api": {"url": "https://v2.example/api/items"}},
    "sitemap": {"sitemap": {"match": "/x/"}},
    "crawl": {"crawl": {"start": "/x"}},
    "static": {"static": {"urls": ["/x"]}},
    "sequence": {"sequence": {"url": "/x/{n}"}},
    "from_collection": {"from_collection": {"name": "other", "url": "/x/{value}"}},
}


def test_every_discovery_kind_is_covered_below():
    """Parametrising over the examples cannot catch a kind nobody listed."""
    from eag_migrator.web.harvest import DISCOVERY_KINDS

    assert set(DISCOVERY_KINDS) == set(DISCOVERY_EXAMPLES)


@pytest.mark.parametrize("kind", sorted(DISCOVERY_EXAMPLES))
def test_the_status_page_survives_every_discovery_kind(workspace, client, kind):
    """A new discovery type must not be able to take the dashboard down.

    `sequence:` did exactly that: the status reader picked the kind with a
    next() over a hardcoded list, so a config it did not know about raised
    StopIteration and every page 500'd.
    """
    import yaml

    from eag_migrator.web.harvest import DISCOVERY_KINDS

    assert kind in DISCOVERY_KINDS, "an example is missing for a discovery kind"

    dash.HARVEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    dash.HARVEST_FILE.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "site": {"base_url": "https://v2.example"},
                "collections": [
                    {
                        "name": "things",
                        "discover": DISCOVERY_EXAMPLES[kind],
                        "extract": {"fields": [{"to": "title", "selector": "h1"}]},
                    }
                ],
            }
        )
    )

    body = client.get("/api/status").json()
    assert body["harvest"]["valid"] is True, body["harvest"]
    assert body["harvest"]["collections"][0]["kind"] == kind
    assert client.get("/").status_code == 200


def test_a_broken_harvest_config_is_reported_not_a_500(workspace, client):
    dash.HARVEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    dash.HARVEST_FILE.write_text("collections: [{name: oops}]\n")

    body = client.get("/api/status").json()
    assert body["harvest"]["present"] is True
    assert body["harvest"]["valid"] is False
    assert body["harvest"]["message"]
    assert client.get("/").status_code == 200


def test_draft_html_reads_a_list_screen_and_writes_a_config(workspace, monkeypatch):
    """The no-API path has to be reachable from the dashboard, not just the CLI."""
    import eag_migrator.dashboard.app as dashmod
    from eag_migrator.web.session import Session

    from fixture_app import FixtureApp

    with FixtureApp() as app:
        Session.from_cookie_header(app.cookie_header, app.url).save(dash.SESSION_FILE)
        client = TestClient(dashmod.create_app())

        res = client.post(
            "/actions/draft-html",
            data={"url": f"{app.url}/legacy/customers", "paths": ""},
            follow_redirects=False,
        )
        assert res.status_code == 303
        job = dashmod.jobs.recent(1)[0]
        _wait(job)

    assert job.status == "done", job.error
    assert job.result["collections"] == 1

    drafted = dash.HARVEST_DRAFT.read_text()
    assert "rows: table.listing tr.customer" in drafted
    assert "data-id" in drafted
    # The session cookie is not a thing to write into a config file or a log.
    assert "s3ss10n" not in drafted
    assert "s3ss10n" not in "\n".join(job.log)


def test_form_login_accepts_credentials_directly_or_from_env(monkeypatch):
    """The dashboard passes them in; the CLI can still use the environment."""
    import inspect

    from eag_migrator.web.login import form_login

    params = inspect.signature(form_login).parameters
    assert "username" in params and "password" in params
    assert params["username"].default is None


# --- the live log -----------------------------------------------------------


def test_the_log_survives_the_job_ending(client, workspace):
    """The moment a job finishes is exactly when you want to read its log."""
    import eag_migrator.dashboard.app as dashmod

    job = dashmod.jobs.start("demo", "a thing", lambda j: j.say("did the thing"))
    _wait(job)

    status = client.get("/api/status").json()
    assert status["job"] is None                      # nothing running
    assert status["last_job"]["label"] == "a thing"   # but still reportable
    assert any("did the thing" in line for line in status["last_job"]["log"])

    page = client.get("/").text
    assert "did the thing" in page                    # rendered, not hidden


def test_logs_are_written_to_disk_as_they_happen(workspace, monkeypatch):
    """A crash mid-run must leave the log behind, not lose it with the process."""
    import eag_migrator.dashboard.app as dashmod
    from eag_migrator.dashboard.jobs import JobManager

    manager = JobManager(log_dir=workspace / "joblogs")
    monkeypatch.setattr(dashmod, "jobs", manager)

    started = threading_event()

    def slow(job):
        job.say("first line")
        started.set()
        while not job.should_stop():
            time.sleep(0.01)
        return {}

    job = manager.start("slow", "slow thing", slow)
    started.wait(5)

    # Readable while it is still running.
    assert job.log_path.exists()
    assert "first line" in job.log_path.read_text()

    job.stop()
    _wait(job)
    assert "finished" in job.log_path.read_text()


def threading_event():
    import threading

    return threading.Event()


def test_tail_returns_only_what_is_new():
    from eag_migrator.dashboard.jobs import Job

    job = Job(id="x", kind="k", label="l")
    for i in range(5):
        job.say(f"line {i}")

    lines, offset = job.tail(0)
    assert len(lines) == 5 and offset == 5

    fresh, offset = job.tail(offset)
    assert fresh == [] and offset == 5

    job.say("line 5")
    fresh, offset = job.tail(5)
    assert len(fresh) == 1 and "line 5" in fresh[0] and offset == 6


def test_tail_copes_with_a_trimmed_log():
    """The in-memory tail is bounded; offsets must still line up."""
    from eag_migrator.dashboard.jobs import Job

    job = Job(id="x", kind="k", label="l")
    for i in range(2500):
        job.say(f"line {i}")

    assert job.dropped == 500
    assert len(job.log) == 2000

    lines, offset = job.tail(0)          # asking for everything
    assert offset == 2500
    assert "line 2499" in lines[-1]

    lines, offset = job.tail(2499)       # asking for just the last one
    assert len(lines) == 1


def test_log_stream_pushes_lines_and_closes_on_completion(client, workspace):
    import eag_migrator.dashboard.app as dashmod

    job = dashmod.jobs.start(
        "demo", "streamed", lambda j: [j.say(f"step {i}") for i in range(3)] and {}
    )
    _wait(job)

    with client.stream("GET", f"/api/jobs/{job.id}/stream?since=0") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        body = "".join(res.iter_text())

    assert "event: log" in body
    assert "step 0" in body and "step 2" in body
    assert "event: end" in body          # the stream ends itself
    assert '"status": "done"' in body


def test_activity_page_lists_past_jobs_with_their_logs(client, workspace):
    import eag_migrator.dashboard.app as dashmod

    first = dashmod.jobs.start("demo", "earlier job", lambda j: j.say("older output"))
    _wait(first)
    second = dashmod.jobs.start("demo", "later job", lambda j: j.say("newer output"))
    _wait(second)

    page = client.get("/jobs").text
    assert "earlier job" in page and "later job" in page
    assert "newer output" in page                    # newest selected by default

    older = client.get(f"/jobs?job={first.id}").text
    assert "older output" in older


# --- knowing about the browser before you need it ---------------------------


def test_status_reports_whether_a_browser_is_available(client):
    body = client.get("/api/status").json()
    assert "browser" in body
    assert isinstance(body["browser"]["available"], bool)


def test_the_ui_warns_and_disables_when_there_is_no_browser(workspace, monkeypatch):
    """Finding out mid-job is too late; the buttons that need it are disabled."""
    import eag_migrator.dashboard.app as dashmod

    dashmod._BROWSER_CACHE.clear()
    monkeypatch.setattr(
        dashmod,
        "browser_state",
        lambda: {
            "available": False,
            "reason": "the image was built without Chromium",
            "hint": "make build-browser",
        },
    )
    page = TestClient(dashmod.create_app()).get("/").text

    assert "No browser in this image" in page
    assert "make build-browser" in page
    assert "disabled" in page
    # The cookie route needs no browser, so it stays offered.
    assert "Cookie header" in page
    dashmod._BROWSER_CACHE.clear()


def test_the_hint_matches_where_it_is_running(monkeypatch):
    from eag_migrator.web import capture as cap

    monkeypatch.setattr(cap, "in_container", lambda: True)
    assert "make build-browser" in cap.install_hint()
    assert "docker compose" in cap.install_hint()

    monkeypatch.setattr(cap, "in_container", lambda: False)
    assert "playwright install chromium" in cap.install_hint()
    assert "docker compose" not in cap.install_hint()


def test_browser_status_explains_itself_when_missing(monkeypatch):
    from eag_migrator.web import capture as cap

    monkeypatch.setattr(cap, "find_chromium", lambda: None)

    def explode(*_a, **_kw):
        raise RuntimeError("Executable doesn't exist at /root/.cache/ms-playwright/…")

    monkeypatch.setattr("playwright.sync_api.sync_playwright", explode)
    status = cap.browser_status()

    assert status["available"] is False
    assert "Executable doesn't exist" in status["reason"]
    assert status["hint"]


# --- v3's API, driven from the dashboard ------------------------------------


def _v3_api(monkeypatch):
    """A stand-in v3 that answers with the real envelope shape."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"reply": {"data": {"key": "019f-abc"}, "messages": [], "succeeded": True},
             "seen": []}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _go(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            state["seen"].append(
                {"method": method, "path": self.path,
                 "cookie": self.headers.get("Cookie"),
                 "body": self.rfile.read(length).decode() if length else ""}
            )
            body = _json.dumps(state["reply"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._go("GET")

        def do_POST(self):  # noqa: N802
            self._go("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    state["url"] = f"http://{host}:{port}"
    return state, server


def test_a_v3_session_can_be_saved_and_never_comes_back_out(workspace, client):
    state, server = _v3_api(None)
    try:
        res = client.post(
            "/actions/v3-connect",
            data={"base_url": state["url"], "cookie": "eag_v3=s3cr3t-value; other=x"},
            follow_redirects=False,
        )
        assert res.status_code == 303

        page = client.get("/").text
        body = client.get("/api/status").json()
    finally:
        server.shutdown()

    assert body["v3api"]["ready"] is True
    assert body["v3api"]["base_url"] == state["url"]
    # The whole point of storing it here: the value is never rendered again.
    assert "s3cr3t-value" not in page
    assert "s3cr3t-value" not in str(body)
    # It is stored, deliberately — but only on disk, and only readable by us.
    assert "s3cr3t-value" in dash.V3_SESSION_FILE.read_text()
    assert oct(dash.V3_SESSION_FILE.stat().st_mode)[-3:] == "600"


def test_api_get_runs_as_a_job_and_reports_the_keys(workspace, client):
    state, server = _v3_api(None)
    import eag_migrator.dashboard.app as dashmod

    try:
        state["reply"] = {
            "data": [{"key": "019f-aaa", "profileName": "Default", "isDefault": True}],
            "succeeded": True,
        }
        client.post("/actions/v3-connect",
                    data={"base_url": state["url"], "cookie": "eag_v3=tok"},
                    follow_redirects=False)
        res = client.post("/actions/api-get", data={"path": "/api/V1/pricing-profiles"},
                          follow_redirects=False)
        assert res.status_code == 303
        job = dashmod.jobs.recent(1)[0]
        _wait(job)
    finally:
        server.shutdown()

    assert job.status == "done", job.error
    log = "\n".join(job.log)
    assert "HTTP 200" in log
    assert "profileName" in log
    assert "eag_v3=tok" not in log            # the credential never reaches the log
    assert state["seen"][0]["cookie"] == "eag_v3=tok"


def test_api_post_needs_the_confirmation_word(workspace, client):
    state, server = _v3_api(None)
    try:
        client.post("/actions/v3-connect",
                    data={"base_url": state["url"], "cookie": "eag_v3=tok"},
                    follow_redirects=False)
        res = client.post(
            "/actions/api-post",
            data={"path": "/api/V1/customers", "body": '{"a": 1}', "confirm": ""},
            follow_redirects=False,
        )
    finally:
        server.shutdown()

    assert res.status_code == 303
    assert "type+write" in res.headers["location"] or "confirm" in res.headers["location"]
    assert state["seen"] == []                 # nothing was sent


def test_api_post_reports_a_rejection_that_arrived_as_200(workspace, client):
    """The dashboard must reach the same verdict the migration would."""
    state, server = _v3_api(None)
    import eag_migrator.dashboard.app as dashmod

    try:
        state["reply"] = {"data": None, "messages": ["Phone Number is required"],
                          "succeeded": False}
        client.post("/actions/v3-connect",
                    data={"base_url": state["url"], "cookie": "eag_v3=tok"},
                    follow_redirects=False)
        client.post(
            "/actions/api-post",
            data={"path": "/api/V1/customers", "body": '{"customerName": "ZZ Test"}',
                  "confirm": "write"},
            follow_redirects=False,
        )
        job = dashmod.jobs.recent(1)[0]
        _wait(job)
    finally:
        server.shutdown()

    assert job.status == "done", job.error
    log = "\n".join(job.log)
    assert "REJECTED" in log
    assert "Phone Number is required" in log
    assert job.result["accepted"] is False


def test_bad_json_never_reaches_the_network(workspace, client):
    state, server = _v3_api(None)
    try:
        client.post("/actions/v3-connect",
                    data={"base_url": state["url"], "cookie": "eag_v3=tok"},
                    follow_redirects=False)
        res = client.post(
            "/actions/api-post",
            data={"path": "/api/V1/customers", "body": "{not json", "confirm": "write"},
            follow_redirects=False,
        )
    finally:
        server.shutdown()

    assert res.status_code == 303
    assert "valid+JSON" in res.headers["location"] or "JSON" in res.headers["location"]
    assert state["seen"] == []


# --- making a config live ---------------------------------------------------


def test_a_config_from_the_directory_can_be_made_live(workspace, client):
    (dash.CONFIG_DIR / "harvest.eag-v2.yaml").write_text(
        "version: 1\nsite: {base_url: 'https://v2.example'}\ncollections: []\n"
    )
    body = client.get("/api/status").json()
    assert "harvest.eag-v2.yaml" in [c["name"] for c in body["configs"]]

    res = client.post("/actions/use-config", data={"name": "harvest.eag-v2.yaml"},
                      follow_redirects=False)
    assert res.status_code == 303
    assert dash.HARVEST_FILE.exists()
    assert "v2.example" in dash.HARVEST_FILE.read_text()


def test_a_config_name_cannot_escape_the_config_directory(workspace, client):
    res = client.post("/actions/use-config", data={"name": "../../etc/passwd"},
                      follow_redirects=False)
    assert res.status_code == 303
    assert "no+such+config" in res.headers["location"]
    assert not dash.HARVEST_FILE.exists()


def test_promoting_over_an_existing_config_keeps_the_old_one(workspace, client):
    """The second round of drafting had no button at all before this."""
    dash.HARVEST_FILE.write_text("version: 1\nsite: {base_url: 'https://old.example'}\n"
                                 "collections: []\n")
    dash.HARVEST_DRAFT.write_text("version: 1\nsite: {base_url: 'https://new.example'}\n"
                                  "collections: []\n")

    page = client.get("/").text
    assert "Promote harvest draft" in page      # the button is offered at all

    client.post("/actions/promote", data={"which": "harvest"}, follow_redirects=False)

    assert "new.example" in dash.HARVEST_FILE.read_text()
    assert "old.example" in (dash.CONFIG_DIR / "harvest.yaml.bak").read_text()
    assert not dash.HARVEST_DRAFT.exists()


# --- the reorganised overview -----------------------------------------------


def test_overview_reads_as_a_numbered_pipeline(client):
    """The cards are one flow — connect v2, extract, connect v3, map, migrate."""
    page = client.get("/").text
    for phase in ("Connect to v2", "Extract into staging", "Connect to v3",
                  "Map v2", "Migrate"):
        assert phase in page, phase
    # A single status strip up top, not a count buried in each card.
    assert "nothing staged" in page
    assert "not connected" in page


def test_the_status_strip_tracks_what_is_ready(client, workspace):
    from eag_migrator.web.session import Session

    Session.from_cookie_header("sid=x", "https://v2.example").save(dash.SESSION_FILE)
    import sqlite3
    dash.STAGING_DB.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(dash.STAGING_DB).executescript(
        "CREATE TABLE customers(_id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO customers VALUES (1, 'Dana'), (2, 'Sam');"
    )
    page = client.get("/").text
    assert "v2 ready" in page          # a session counts as reachable
    assert "2 rows staged" in page


def test_harvest_is_gated_on_a_live_config(client, workspace):
    """The harvest button stays disabled until a config is made live."""
    page = client.get("/").text
    assert "make a harvest config live first" in page   # the disabled reason


def test_migrate_is_gated_on_a_valid_mapping(client):
    page = client.get("/").text
    assert "Needs a valid mapping first" in page
