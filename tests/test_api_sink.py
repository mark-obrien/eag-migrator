"""Writing to v3 through its HTTP API rather than its database.

The case that matters: an API that answers HTTP 200 with an envelope saying
the write was rejected. Taking the status code at face value there records an
insert that never happened — the run reports success, the record is missing,
and rollback has nothing to undo. That is the worst failure this tool can
have, so it gets the most tests.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from eag_migrator.adapters.api_sink import ApiSink
from eag_migrator.mapping import EntityMap

# What the next POST should answer with, set per test.
REPLY: dict = {}
SEEN: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        SEEN.append(
            {
                "path": self.path,
                "body": json.loads(self.rfile.read(length) or b"{}"),
                "cookie": self.headers.get("Cookie"),
                "auth": self.headers.get("Authorization"),
            }
        )
        body = json.dumps(REPLY.get("body", {})).encode()
        self.send_response(REPLY.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    REPLY.clear()
    SEEN.clear()
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _entity(conflict: str = "error") -> EntityMap:
    return EntityMap.model_validate(
        {
            "name": "customer",
            "source": {"table": "customers", "key": "id"},
            "target": {"table": "customers", "key": "id",
                       "endpoint": "/api/V1/customers", "conflict": conflict},
            "fields": [{"to": "name", "from": "name"}],
        }
    )


def _write(base, rows=None, **kwargs):
    sink = ApiSink(base, **kwargs)
    try:
        return sink.write_batch(_entity(kwargs.pop("conflict", "error")),
                                rows or [(1, {"name": "Dana"})])
    finally:
        sink.close()


# --- the envelope -----------------------------------------------------------


def test_a_200_that_says_it_failed_is_a_failure(api):
    """{"succeeded": false} with HTTP 200 must not be recorded as an insert."""
    REPLY["body"] = {
        "data": None,
        "messages": ["Phone Number is required"],
        "succeeded": False,
    }
    results = _write(api)

    assert results[0].action == "failed"
    assert results[0].target_id is None
    assert "Phone Number is required" in results[0].error


def test_the_envelopes_data_is_where_the_id_comes_from(api):
    REPLY["body"] = {
        "data": {"key": "019fca0c-1111-7000-8000-000000000001", "customerId": "CUST-0002"},
        "messages": [],
        "succeeded": True,
    }
    results = _write(api)

    assert results[0].action == "inserted"
    assert results[0].target_id == "019fca0c-1111-7000-8000-000000000001"


def test_an_unenveloped_body_is_taken_at_face_value(api):
    REPLY["body"] = {"id": 4242}
    results = _write(api)

    assert results[0].action == "inserted"
    assert results[0].target_id == 4242


def test_a_named_id_field_wins_over_the_defaults(api):
    REPLY["body"] = {"data": {"id": "ignore-me", "customerId": "CUST-0007"}, "succeeded": True}
    results = _write(api, id_field="customerId")

    assert results[0].target_id == "CUST-0007"


def test_a_rejection_can_be_a_skip_when_that_is_the_policy(api):
    REPLY["body"] = {"succeeded": False, "messages": ["Customer already exists"]}
    sink = ApiSink(api)
    try:
        results = sink.write_batch(_entity("skip"), [(1, {"name": "Dana"})])
    finally:
        sink.close()

    assert results[0].action == "skipped"
    assert results[0].error is None


def test_a_2xx_with_no_json_body_is_still_an_insert(api):
    REPLY["body"] = None          # serialises to "null", which is valid JSON
    results = _write(api)
    assert results[0].action == "inserted"
    assert results[0].target_id is None


def test_an_http_error_is_still_an_error(api):
    REPLY["status"] = 400
    REPLY["body"] = {"succeeded": False, "messages": ["bad"]}
    results = _write(api)

    assert results[0].action == "failed"
    assert "400" in results[0].error


# --- credentials ------------------------------------------------------------


def test_a_session_cookie_can_authenticate_the_write(api):
    """Some targets issue no token at all — a session cookie is the credential."""
    REPLY["body"] = {"data": {"key": "abc"}, "succeeded": True}
    _write(api, cookie="eag_session=s3cr3t")

    assert SEEN[0]["cookie"] == "eag_session=s3cr3t"
    assert SEEN[0]["auth"] is None


def test_a_token_still_becomes_a_bearer_header(api):
    REPLY["body"] = {"data": {"key": "abc"}, "succeeded": True}
    _write(api, token="tok123")

    assert SEEN[0]["auth"] == "Bearer tok123"
    assert SEEN[0]["cookie"] is None


def test_the_row_is_posted_to_the_configured_endpoint(api):
    REPLY["body"] = {"succeeded": True, "data": {"key": "abc"}}
    _write(api, rows=[(7, {"name": "Dana", "customerType": 9})])

    assert SEEN[0]["path"] == "/api/V1/customers"
    assert SEEN[0]["body"] == {"name": "Dana", "customerType": 9}
