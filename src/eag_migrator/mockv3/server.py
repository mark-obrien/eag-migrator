"""The mock v3 API, served over real HTTP.

Stdlib only, the same shape as the test fixtures, so it runs standalone with
`eagm mock-v3` and can be pointed at by the real ApiSink on a live socket.
Records land in a SQLite file you can inspect or throw away.
"""

from __future__ import annotations

import datetime as dt
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
DEFAULT_REQUIRED = {
    "customers": ["customerName", "customerType", "pricingProfile", "phoneNumber"],
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
            self._send(_LANDING, ctype="text/html; charset=utf-8")
            return

        references = {
            "/api/v1/pricing-profiles": seed.PRICING_PROFILES,
            "/api/v1/locations/names": seed.LOCATIONS,
            "/api/v1/customers/paymentterms": seed.PAYMENT_TERMS,
            "/api/v1/identity/users": seed.USERS,
            "/api/v1/identity/users/installers": seed.INSTALLERS,
            "/api/v1/identity/profile": {"name": "Owner", "tenant": "rehearsal"},
        }
        if path in references:
            self._envelope(references[path])
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


_LANDING = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Mock v3 — rehearsal target</title>
<style>body{font:16px/1.6 system-ui;margin:8vh auto;max-width:640px;padding:0 20px;
color:#16191f}code{background:#eef;padding:1px 5px;border-radius:4px}
.b{background:#fffbe6;border:1px solid #f0c000;border-radius:8px;padding:14px 18px}</style>
</head><body>
<h1>Mock v3 &mdash; rehearsal target</h1>
<div class="b"><strong>This is not the real v3.</strong> It is a local
stand-in built from the survey, for rehearsing a migration without writing to
a production tenant. Records here are disposable.</div>
<p>Point the migrator at it with
<code>V3_API_BASE_URL=http://mock-v3:19090</code> (in compose) and run as
normal. Inspect what landed at <code>/__mock/records</code>, the enum codes at
<code>/__mock/enums</code>, and wipe it with <code>eagm mock-v3 --reset</code>.</p>
</body></html>"""


def serve(host: str, port: int, cfg: MockConfig) -> ThreadingHTTPServer:
    """A configured, not-yet-serving server. Call serve_forever() on it."""
    server = ThreadingHTTPServer((host, port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    return server
