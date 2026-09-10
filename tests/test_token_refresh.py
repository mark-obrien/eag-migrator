"""Renewing v3's access token in the middle of a run.

The token lasts about an hour; a migration of a few thousand records outlives
it. When it lapses the target answers 401 to every remaining write, so without
this a long run stops part way and has to be restarted by hand with a freshly
pasted credential.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from eag_migrator.adapters.api_sink import ApiSink, BearerWithRefresh, build_client
from eag_migrator.mapping import EntityMap

STALE, FRESH = "stale-token", "fresh-token"
STATE: dict = {}
SEEN: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: dict, cookie: str | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _token(self) -> str:
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()

    def do_GET(self) -> None:  # noqa: N802
        SEEN.append({"method": "GET", "path": self.path, "token": self._token()})
        if "tokens/refresh" in self.path:
            if STATE.get("refresh_window_closed"):
                self._send(401, {"errors": {"Authorization": ["session_invalid"]}})
                return
            # The real thing answers "ok" and puts the token in a cookie.
            self._send(
                200,
                {"data": {"accessToken": "ok"}, "succeeded": True},
                cookie=f"AccessToken={FRESH}; path=/; secure; httponly",
            )
            return
        self._send(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        token = self._token()
        SEEN.append({"method": "POST", "path": self.path, "token": token})
        if token != FRESH and STATE.get("expired"):
            self._send(401, {"errors": {"Authorization": ["session_invalid"]}})
            return
        self._send(200, {"data": {"key": "abc"}, "succeeded": True})


@pytest.fixture
def api():
    STATE.clear()
    SEEN.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _entity() -> EntityMap:
    return EntityMap.model_validate(
        {
            "name": "customers",
            "source": {"table": "c", "key": "id"},
            "target": {"table": "customers", "endpoint": "/api/V1/customers",
                       "conflict": "error"},
            "fields": [{"to": "name", "from": "name"}],
        }
    )


def _write(base: str, **kw):
    sink = ApiSink(base, token=STALE, refresh_path="/api/V1/tokens/refresh/", **kw)
    try:
        return sink.write_batch(_entity(), [(1, {"name": "Dana"})])
    finally:
        sink.close()


def test_a_lapsed_token_is_renewed_and_the_write_retried(api):
    STATE["expired"] = True
    results = _write(api)

    assert results[0].action == "inserted", results[0].error
    # first POST on the stale token, the refresh, then the same POST again
    assert [s["method"] for s in SEEN] == ["POST", "GET", "POST"]
    assert SEEN[0]["token"] == STALE
    assert "tokens/refresh" in SEEN[1]["path"]
    assert SEEN[2]["token"] == FRESH


def test_the_renewed_token_is_kept_for_later_writes(api):
    """Renewing per row would mean a refresh round trip on every record."""
    STATE["expired"] = True
    sink = ApiSink(api, token=STALE, refresh_path="/api/V1/tokens/refresh/")
    try:
        sink.write_batch(_entity(), [(1, {"name": "A"})])
        SEEN.clear()
        sink.write_batch(_entity(), [(2, {"name": "B"})])
    finally:
        sink.close()

    assert [s["method"] for s in SEEN] == ["POST"]
    assert SEEN[0]["token"] == FRESH


def test_a_closed_refresh_window_fails_loudly_rather_than_writing_nothing(api):
    """Refreshing is bounded by the login session. Once that is gone the 401
    has to reach the report, not be swallowed into a quiet no-op."""
    STATE["expired"] = True
    STATE["refresh_window_closed"] = True
    results = _write(api)

    assert results[0].action == "failed"
    assert "401" in results[0].error


def test_without_a_refresh_path_nothing_changes(api):
    """The old behaviour has to stay put for targets that have no such endpoint."""
    STATE["expired"] = True
    sink = ApiSink(api, token=STALE)
    try:
        results = sink.write_batch(_entity(), [(1, {"name": "Dana"})])
    finally:
        sink.close()

    assert results[0].action == "failed"
    assert [s["method"] for s in SEEN] == ["POST"]


def test_a_healthy_token_never_calls_the_refresh_endpoint(api):
    results = _write(api)

    assert results[0].action == "inserted"
    assert [s["method"] for s in SEEN] == ["POST"]


def test_the_flow_does_not_double_the_bearer_prefix(api):
    auth = BearerWithRefresh("Bearer already-prefixed", None)
    client = build_client(api, token="Bearer already-prefixed",
                          refresh_path="/api/V1/tokens/refresh/")
    try:
        client.get("/api/V1/tokens/refresh/")
    finally:
        client.close()
    assert auth.token == "Bearer already-prefixed"
    assert SEEN[-1]["token"] == "already-prefixed"
