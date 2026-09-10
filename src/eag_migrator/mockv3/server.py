"""The mock v3 API, served over real HTTP.

Stdlib only, the same shape as the test fixtures, so it runs standalone with
`eagm mock-v3` and can be pointed at by the real ApiSink on a live socket.
Records land in a SQLite file you can inspect or throw away.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import seed

DEFAULT_PORT = 19090

# Fields we are confident a customer needs, from the survey. Enforced only in
# strict mode, because the survey gave the *labels*, not v3's JSON field names
# — so a real create request (`eagm api-post`) is what confirms these.
# Real v3 field names, from docs/v3-api.md — read off the live tenant. The
# earlier list (customerName, customerType, pricingProfile, phoneNumber) came
# from a survey and named fields the API does not have, so strict mode was
# checking a rehearsal against a vocabulary v3 never uses: a mapping could pass
# here and be refused in production, or fail here and have been fine.
#
# Which fields the live API actually insists on is not published. These are the
# mock's own judgment about what a record is meaningless without, not a
# contract — hence only the two that are unarguable.
DEFAULT_REQUIRED = {
    "customers": ["customerFullName", "pricingProfileKey"],
}

# Endpoints that write, and the entity each stores under.
WRITE_ENTITIES = {
    "/api/v1/customers": "customers",
    "/api/v1/jobs": "jobs",
}


@dataclass
class MockConfig:
    db_path: Path
    strict: bool = False
    required: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_REQUIRED))
    snapshot_path: Path | None = None
    """A `state/v3_snapshot.json` from `eagm v3-snapshot`. When present, the
    reference endpoints serve v3's real ids instead of the synthetic seed, so a
    rehearsal validates the exact ids a production run will use."""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS records ("
        " pk INTEGER PRIMARY KEY AUTOINCREMENT,"
        " entity TEXT NOT NULL,"
        " key TEXT NOT NULL UNIQUE,"
        " display_id TEXT,"
        " payload TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    return conn


def reset_store(path: Path) -> None:
    """Forget every posted record. The seed reference data is code, not stored,
    so it always comes back."""
    conn = _connect(path)
    with conn:
        conn.execute("DELETE FROM records")
    conn.close()


def is_mock_url(url: str | None) -> bool:
    """Whether a base URL points at a local mock rather than a real tenant."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    port = urlparse(url).port
    return host in ("mock-v3", "mockv3") or (
        host in ("localhost", "127.0.0.1") and port == DEFAULT_PORT
    )


def _next_customer_number(conn: sqlite3.Connection) -> int:
    highest = 1  # CUST-0001 is the seed
    for row in conn.execute(
        "SELECT display_id FROM records WHERE entity = 'customers'"
    ):
        match = re.search(r"(\d+)$", row["display_id"] or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- plumbing -----------------------------------------------------------

    def log_message(self, *_args: Any) -> None:
        pass

    @property
    def cfg(self) -> MockConfig:
        return self.server.cfg  # type: ignore[attr-defined]

    def _send(self, body: str, status: int = 200, ctype: str = "application/json") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _envelope(self, data: Any, *, ok: bool = True, messages: list[str] | None = None,
                  status: int = 200) -> None:
        # v3 answers a rejected write with HTTP 200 and succeeded:false — the
        # exact case the sink has to notice, so the mock reproduces it.
        self._send(
            json.dumps({"data": data, "messages": messages or [], "succeeded": ok}),
            status=status,
        )

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def _paged(self, rows: list[dict], query: dict) -> dict:
        page = int((query.get("page") or ["1"])[0])
        size = int((query.get("pageSize") or query.get("per_page") or ["50"])[0])
        start = (page - 1) * size
        chunk = rows[start:start + size]
        total = len(rows)
        return {
            "data": chunk,
            "currentPage": page,
            "pageSize": size,
            "totalCount": total,
            "totalPages": (total + size - 1) // size if size else 1,
            "hasNextPage": start + size < total,
            "hasPreviousPage": page > 1,
        }

    # --- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/").lower() or "/"
        query = parse_qs(parsed.query)

        if path in ("", "/"):
            self._send(self._render_index(), ctype="text/html; charset=utf-8")
            return

        # The browsable data views: check what a rehearsal migration wrote,
        # laid out like v3's screens but marked a rehearsal on every page.
        if path == "/view" or path.startswith("/view/"):
            self._send(self._render_view(parsed.path), ctype="text/html; charset=utf-8")
            return

        # (snapshot key, synthetic fallback) per reference route. When a
        # snapshot exists, its real data wins.
        references = {
            "/api/v1/pricing-profiles": ("pricing_profiles", seed.PRICING_PROFILES),
            "/api/v1/locations/names": ("locations", seed.LOCATIONS),
            "/api/v1/customers/paymentterms": ("payment_terms", seed.PAYMENT_TERMS),
            "/api/v1/identity/users": ("users", seed.USERS),
            "/api/v1/identity/users/installers": ("installers", seed.INSTALLERS),
        }
        if path in references:
            key, fallback = references[path]
            snap = getattr(self.server, "snapshot", None) or {}
            self._envelope(snap.get(key) if key in snap else fallback)
            return
        if path == "/api/v1/identity/profile":
            self._envelope({"name": "Owner", "tenant": "rehearsal"})
            return

        # A rehearsal aid, honestly namespaced so it cannot be mistaken for a
        # real v3 route: the enum codes the mapping has to produce.
        if path == "/__mock/enums":
            self._send(json.dumps({
                "customerType": seed.CUSTOMER_TYPES,
                "jobStatus": seed.JOB_STATUS,
                "jobType": seed.JOB_TYPES,
                "causeOfLoss": seed.CAUSE_OF_LOSS,
                "paymentTerms": seed.PAYMENT_TERMS,
            }, indent=2))
            return

        if path == "/__mock/records":
            conn = _connect(self.cfg.db_path)
            rows = [
                {"entity": r["entity"], "key": r["key"], "display_id": r["display_id"],
                 "payload": json.loads(r["payload"])}
                for r in conn.execute("SELECT * FROM records ORDER BY pk")
            ]
            conn.close()
            self._send(json.dumps({"count": len(rows), "records": rows}, indent=2))
            return

        # A record list, e.g. GET /api/V1/customers.
        entity = WRITE_ENTITIES.get(path)
        if entity:
            self._envelope(self._paged(self._stored(entity), query))
            return

        # A single record by key: GET /api/V1/customers/<key>.
        got = self._record_by_key(path)
        if got is not None:
            self._envelope(got)
            return

        self._envelope(None, ok=False, messages=["not found"], status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/").lower()

        if path == "/__mock/reset":
            reset_store(self.cfg.db_path)
            self._envelope({"reset": True})
            return

        # Search endpoints mirror the real ones: POST returns a paged list.
        if path in ("/api/v1/customers/search", "/api/v1/jobs/search"):
            entity = path.split("/")[3]
            self._envelope(self._paged(self._stored(entity), {}))
            return

        entity = WRITE_ENTITIES.get(path)
        if entity is None:
            self._envelope(None, ok=False, messages=["not found"], status=404)
            return

        body = self._body()
        if not isinstance(body, dict):
            self._envelope(None, ok=False, messages=["request body was not a JSON object"])
            return

        problem = self._validate(entity, body)
        if problem:
            # HTTP 200, succeeded:false — the trap the sink must not read as an
            # insert.
            self._envelope(None, ok=False, messages=[problem])
            return

        self._envelope(self._store(entity, body), status=201)

    def do_DELETE(self) -> None:  # noqa: N802
        # Rollback deletes what a run wrote: DELETE /api/V1/customers/<key>.
        path = urlparse(self.path).path.rstrip("/").lower()
        key = path.rsplit("/", 1)[-1]
        conn = _connect(self.cfg.db_path)
        with conn:
            cur = conn.execute("DELETE FROM records WHERE key = ?", (key,))
        removed = cur.rowcount
        conn.close()
        if removed:
            self._envelope({"deleted": key})
        else:
            self._envelope(None, ok=False, messages=["not found"], status=404)

    # --- store --------------------------------------------------------------

    def _validate(self, entity: str, body: dict) -> str | None:
        if self.cfg.strict:
            for name in self.cfg.required.get(entity, []):
                if body.get(name) in (None, "", [], {}):
                    return f"{name} is required"
        # Enum membership is cheap and worth checking even when lenient: a
        # customerType outside the known set is a mapping bug either way.
        if entity == "customers" and "customerType" in body:
            value = body["customerType"]
            if value is not None and value not in seed.CUSTOMER_TYPE_CODES:
                return (
                    f"customerType {value!r} is not one of "
                    f"{sorted(seed.CUSTOMER_TYPE_CODES)}"
                )
        return None

    def _store(self, entity: str, body: dict) -> dict:
        conn = _connect(self.cfg.db_path)
        key = str(uuid.uuid4())
        display_id = None
        record = dict(body)
        record["key"] = key
        if entity == "customers":
            display_id = f"CUST-{_next_customer_number(conn):04d}"
            record["customerId"] = display_id
        with conn:
            conn.execute(
                "INSERT INTO records (entity, key, display_id, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (entity, key, display_id, json.dumps(record),
                 dt.datetime.now(dt.timezone.utc).isoformat()),
            )
        conn.close()
        return record

    def _stored(self, entity: str) -> list[dict]:
        conn = _connect(self.cfg.db_path)
        rows = [json.loads(r["payload"])
                for r in conn.execute(
                    "SELECT payload FROM records WHERE entity = ? ORDER BY pk", (entity,))]
        conn.close()
        if entity == "customers":
            rows = [dict(seed.SEED_CUSTOMER), *rows]
        return rows

    def _record_by_key(self, path: str) -> dict | None:
        key = path.rsplit("/", 1)[-1]
        conn = _connect(self.cfg.db_path)
        row = conn.execute("SELECT payload FROM records WHERE key = ?", (key,)).fetchone()
        conn.close()
        return json.loads(row["payload"]) if row else None

    # --- browsable views ----------------------------------------------------

    def _entities(self) -> list[str]:
        conn = _connect(self.cfg.db_path)
        found = {r["entity"] for r in conn.execute("SELECT DISTINCT entity FROM records")}
        conn.close()
        found.add("customers")  # always offer it, even before anything is written
        return sorted(found)

    def _reference(self) -> dict[str, list]:
        snap = getattr(self.server, "snapshot", None) or {}
        return {
            "pricing_profiles": snap.get("pricing_profiles", seed.PRICING_PROFILES),
            "locations": snap.get("locations", seed.LOCATIONS),
            "payment_terms": snap.get("payment_terms", seed.PAYMENT_TERMS),
            "users": snap.get("users", seed.USERS),
        }

    def _render_index(self) -> str:
        entities = self._entities()
        conn = _connect(self.cfg.db_path)
        counts = {
            e: conn.execute("SELECT COUNT(*) c FROM records WHERE entity = ?", (e,)).fetchone()["c"]
            for e in entities
        }
        conn.close()
        counts["customers"] = counts.get("customers", 0) + 1  # the seed
        rows = "".join(
            f'<li><a href="/view/{html.escape(e)}">{html.escape(e)}</a>'
            f' <span class="dim">{counts[e]}</span></li>'
            for e in entities
        )
        body = (
            "<p>A local stand-in for v3, for rehearsing a migration. Records here "
            "were written by a rehearsal run and are disposable.</p>"
            f"<h2>What has landed</h2><ul class='big'>{rows}"
            "<li><a href='/view/reference'>reference data</a> "
            "<span class='dim'>config</span></li></ul>"
            "<p class='dim'>Point the migrator at <code>http://mock-v3:19090</code> "
            "(compose), run a migration, then refresh this to check what it wrote.</p>"
        )
        return _page("Overview", body, active="")

    def _render_view(self, raw_path: str) -> str:
        parts = [p for p in raw_path[len("/view"):].split("/") if p]
        if not parts:
            return self._render_index()
        entity = parts[0]

        if entity == "reference":
            return self._render_reference()
        if len(parts) >= 2:
            return self._render_detail(entity, parts[1])
        return self._render_list(entity)

    def _render_list(self, entity: str) -> str:
        records = self._stored(entity)
        if not records:
            return _page(
                entity,
                f"<p class='dim'>No {html.escape(entity)} yet. Run a migration "
                f"pointed at this mock and they will appear here.</p>",
                active=entity, entities=self._entities(),
            )

        columns = _list_columns(records)
        head = "".join(f"<th>{html.escape(c)}</th>" for c in columns) + "<th></th>"
        body_rows = []
        for rec in records:
            cells = "".join(f"<td>{_cell(rec.get(c))}</td>" for c in columns)
            key = rec.get("key")
            link = (f'<a href="/view/{html.escape(entity)}/{html.escape(str(key))}">open</a>'
                    if key else '<span class="dim">seed</span>')
            body_rows.append(f"<tr>{cells}<td>{link}</td></tr>")
        table = (
            f"<div class='scroll'><table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table></div>"
            f"<p class='dim'>{len(records)} record(s).</p>"
        )
        return _page(entity, table, active=entity, entities=self._entities())

    def _render_detail(self, entity: str, key: str) -> str:
        record = None
        for rec in self._stored(entity):
            if str(rec.get("key")) == key:
                record = rec
                break
        if record is None:
            return _page(entity, "<p class='dim'>No such record.</p>",
                         active=entity, entities=self._entities())
        rows = "".join(
            f"<tr><td class='k'>{html.escape(str(k))}</td><td>{_cell(v)}</td></tr>"
            for k, v in record.items()
        )
        body = (
            f"<p><a href='/view/{html.escape(entity)}'>&larr; {html.escape(entity)}</a></p>"
            f"<table class='detail'><tbody>{rows}</tbody></table>"
        )
        return _page(f"{entity} record", body, active=entity, entities=self._entities())

    def _render_reference(self) -> str:
        snap = getattr(self.server, "snapshot", None)
        source = ("v3's real ids, from a snapshot" if snap
                  else "synthetic seed — run <code>eagm v3-snapshot</code> for real ids")
        blocks = [f"<p class='dim'>Reference data: {source}.</p>"]
        for name, rows in self._reference().items():
            if not rows:
                continue
            columns = _list_columns(rows)
            head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
            body_rows = "".join(
                "<tr>" + "".join(f"<td>{_cell(r.get(c))}</td>" for c in columns) + "</tr>"
                for r in rows
            )
            blocks.append(
                f"<h2>{html.escape(name)}</h2><div class='scroll'><table><thead><tr>"
                f"{head}</tr></thead><tbody>{body_rows}</tbody></table></div>"
            )
        return _page("reference", "".join(blocks), active="reference",
                     entities=self._entities())


# Which columns to show in a list view, when a record has many. Everything is
# always on the detail page; this just keeps the table readable.
_LIST_PREFERRED = (
    "customerId", "customerName", "name", "full_name", "first_name", "last_name",
    "email", "phone", "status", "customerType", "paymentTerms", "profileName",
    "termsCode", "code", "job_id", "vehicle_vin", "order_num",
)
_LIST_MAX_COLUMNS = 6


def _list_columns(records: list[dict]) -> list[str]:
    present: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for k in rec:
            if k not in seen:
                seen.add(k)
                present.append(k)
    ordered = [c for c in _LIST_PREFERRED if c in seen]
    ordered += [c for c in present if c not in _LIST_PREFERRED and c != "key"]
    return ordered[:_LIST_MAX_COLUMNS] or ["key"]


def _cell(value: Any) -> str:
    if value is None:
        return "<span class='dim'>—</span>"
    if isinstance(value, (dict, list)):
        return f"<span class='mono'>{html.escape(json.dumps(value)[:120])}</span>"
    return html.escape(str(value))


def _page(title: str, body: str, *, active: str, entities: list[str] | None = None) -> str:
    tabs = ""
    for name in entities or []:
        cls = " class='on'" if name == active else ""
        tabs += f"<a href='/view/{html.escape(name)}'{cls}>{html.escape(name)}</a>"
    if entities is not None:
        cls = " class='on'" if active == "reference" else ""
        tabs += f"<a href='/view/reference'{cls}>reference</a>"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Mock v3</title>
<style>
 :root {{ color-scheme: light dark; }}
 * {{ box-sizing: border-box; }}
 body {{ margin:0; font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
   background:#f6f7f9; color:#16191f; }}
 @media (prefers-color-scheme: dark) {{ body {{ background:#0f1115; color:#e6e8ec; }}
   .card {{ background:#171a21 !important; border-color:#2a2f3a !important; }}
   th {{ color:#9aa3b2 !important; }} td,th {{ border-color:#2a2f3a !important; }}
   a {{ color:#6ea8fe; }} code,.mono {{ background:#1d212a !important; }} }}
 .banner {{ background:#fffbe6; color:#7a5b00; border-bottom:1px solid #f0c000;
   padding:8px 20px; font-size:13.5px; font-weight:600; text-align:center; }}
 header {{ padding:14px 20px 0; }}
 header h1 {{ font-size:16px; margin:0 0 10px; }}
 nav {{ display:flex; gap:4px; flex-wrap:wrap; border-bottom:1px solid #d9dee7; }}
 nav a {{ padding:7px 13px; text-decoration:none; color:#5b6472; font-size:14px;
   border-bottom:2px solid transparent; text-transform:capitalize; }}
 nav a.on {{ color:inherit; border-bottom-color:#1f5fd0; font-weight:600; }}
 main {{ max-width:1100px; margin:0 auto; padding:20px; }}
 h2 {{ font-size:14px; text-transform:uppercase; letter-spacing:.5px; color:#5b6472;
   margin:22px 0 8px; }}
 table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
 th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid #e3e7ee;
   vertical-align:top; }}
 th {{ color:#5b6472; font-size:12px; text-transform:uppercase; letter-spacing:.4px; }}
 td.k {{ color:#5b6472; white-space:nowrap; }}
 table.detail td {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; }}
 .scroll {{ overflow-x:auto; }}
 .dim {{ color:#9aa3b2; }}
 code,.mono {{ background:#eef1f6; padding:1px 5px; border-radius:4px;
   font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; }}
 ul.big {{ list-style:none; padding:0; }} ul.big li {{ padding:6px 0; font-size:16px; }}
</style></head><body>
<div class="banner">Mock v3 — rehearsal data, not the real system</div>
<header><h1>Mock v3</h1><nav>{tabs}</nav></header>
<main>{body}</main>
</body></html>"""


def _load_snapshot(path: Path | None) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def serve(host: str, port: int, cfg: MockConfig) -> ThreadingHTTPServer:
    """A configured, not-yet-serving server. Call serve_forever() on it."""
    server = ThreadingHTTPServer((host, port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    # Read the snapshot once at startup, so reference GETs serve real ids.
    server.snapshot = _load_snapshot(cfg.snapshot_path)  # type: ignore[attr-defined]
    return server
