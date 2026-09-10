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

    def _reply(self) -> None:
        body = json.dumps(REPLY.get("body", {})).encode()
        self.send_response(REPLY.get("status", 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        SEEN.append(
            {
                "method": "POST",
                "path": self.path,
                "body": json.loads(self.rfile.read(length) or b"{}"),
                "cookie": self.headers.get("Cookie"),
                "auth": self.headers.get("Authorization"),
            }
        )
        self._reply()

    def do_GET(self) -> None:  # noqa: N802
        SEEN.append({"method": "GET", "path": self.path,
                     "cookie": self.headers.get("Cookie")})
        self._reply()


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


def test_an_envelope_whose_data_is_just_the_new_key_still_yields_an_id(api):
    """v3's POST /api/V1/jobs answers {"data": "<uuid>"} — the key alone.

    Reading no id from that leaves the id map empty, so rollback cannot delete
    what the run wrote and a re-run cannot tell the row was already migrated.
    """
    REPLY["body"] = {"data": "01a089eb-8b33-76c8-b97e-90fac9aca4a5",
                     "messages": [], "succeeded": True}
    results = _write(api)

    assert results[0].action == "inserted"
    assert results[0].target_id == "01a089eb-8b33-76c8-b97e-90fac9aca4a5"


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


def test_skip_does_not_swallow_a_rejection_that_is_not_a_conflict(api):
    """`conflict: skip` means "already there", not "ignore any refusal".

    Swallowing a validation error makes a run that wrote nothing report as
    clean: a rehearsal of 961 customers came back ok / 961 skipped while the
    API had refused every one of them for a missing required field.
    """
    REPLY["body"] = {"succeeded": False, "messages": ["customerName is required"]}
    sink = ApiSink(api)
    try:
        results = sink.write_batch(_entity("skip"), [(1, {"name": "Dana"})])
    finally:
        sink.close()

    assert results[0].action == "failed"
    assert "customerName is required" in results[0].error


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


def test_a_token_pasted_with_its_prefix_is_not_doubled(api):
    """Devtools shows "Bearer eyJ…", so that whole string is what gets pasted.

    Sending it unchanged would mean "Bearer Bearer eyJ…" and a 401 that says
    nothing about why — an afternoon lost to a credential that was correct.
    """
    REPLY["body"] = {"data": {"key": "abc"}, "succeeded": True}
    _write(api, token="Bearer tok123")

    assert SEEN[0]["auth"] == "Bearer tok123"


def test_the_row_is_posted_to_the_configured_endpoint(api):
    REPLY["body"] = {"succeeded": True, "data": {"key": "abc"}}
    _write(api, rows=[(7, {"name": "Dana", "customerType": 9})])

    assert SEEN[0]["path"] == "/api/V1/customers"
    assert SEEN[0]["body"] == {"name": "Dana", "customerType": 9}


# --- probing the target from the CLI ----------------------------------------
#
# `eagm api-get` / `api-post` exist so the API can be exercised before a
# migration runs, using the same client and the same response handling. If the
# probe says a write succeeded and the real run disagrees, the probe is
# worthless — so these check they share the verdict.


@pytest.fixture
def cli(api, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from eag_migrator import cli as cli_mod

    monkeypatch.setenv("V3_API_BASE_URL", api)
    monkeypatch.setenv("V3_API_COOKIE", "eag_session=probe")
    monkeypatch.setattr(cli_mod, "REPORTS_DIR", tmp_path / "reports")
    (tmp_path / "reports").mkdir()
    return CliRunner().invoke, cli_mod.app


def test_api_get_reads_an_endpoint_with_the_configured_credentials(cli):
    invoke, app = cli
    REPLY["body"] = {
        "data": [{"key": "019f-aaaa", "profileName": "Default", "isDefault": True}],
        "succeeded": True,
    }
    result = invoke(app, ["api-get", "/api/V1/pricing-profiles"])

    assert result.exit_code == 0, result.output
    assert "HTTP 200" in result.output
    assert "1 item(s)" in result.output
    assert "profileName" in result.output          # the keys the mapping needs
    assert SEEN[0]["cookie"] == "eag_session=probe"


def test_api_post_refuses_to_write_without_yes(cli):
    invoke, app = cli
    result = invoke(app, ["api-post", "/api/V1/customers", "--data", '{"name": "ZZ Test"}'])

    assert result.exit_code == 1
    assert "--yes" in result.output
    assert SEEN == []                              # nothing was sent


def test_api_post_reports_a_rejection_that_arrived_as_http_200(cli):
    """The whole point: the probe must not call this a success either."""
    invoke, app = cli
    REPLY["body"] = {
        "data": None,
        "messages": ["Phone Number is required"],
        "succeeded": False,
    }
    result = invoke(
        app, ["api-post", "/api/V1/customers", "--data", '{"name": "ZZ Test"}', "--yes"]
    )

    assert result.exit_code == 0
    assert "rejected" in result.output
    assert "Phone Number is required" in result.output


def test_api_post_reports_the_id_the_target_assigned(cli):
    invoke, app = cli
    REPLY["body"] = {"data": {"key": "019fca0c-2222", "customerId": "CUST-0002"},
                     "succeeded": True}
    result = invoke(
        app, ["api-post", "/api/V1/customers", "--data", '{"name": "ZZ Test"}', "--yes"]
    )

    assert result.exit_code == 0
    assert "accepted" in result.output
    assert "019fca0c-2222" in result.output
    assert SEEN[0]["body"] == {"name": "ZZ Test"}


def test_bad_json_is_caught_before_anything_is_sent(cli):
    invoke, app = cli
    result = invoke(app, ["api-post", "/api/V1/customers", "--data", "{not json", "--yes"])

    assert result.exit_code == 2
    assert "not valid JSON" in result.output
    assert SEEN == []


def test_a_missing_base_url_says_what_to_set(cli, monkeypatch):
    invoke, app = cli
    monkeypatch.delenv("V3_API_BASE_URL")
    result = invoke(app, ["api-get", "/api/V1/customers"])

    assert result.exit_code == 2
    assert "V3_API_BASE_URL" in result.output
