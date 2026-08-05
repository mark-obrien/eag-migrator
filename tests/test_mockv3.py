"""The mock v3 API, and a real migration run against it.

The point of the mock is that the write path can be rehearsed end to end
without a production tenant. So the load-bearing test is not "does the mock
store a row" — it is "does the ApiSink, driven the way a migration drives it,
insert records, get ids back, and roll them all away again."
"""

from __future__ import annotations

import threading

import httpx
import pytest

from eag_migrator.adapters.api_sink import ApiSink
from eag_migrator.mapping import EntityMap
from eag_migrator.mockv3 import MockConfig, is_mock_url, serve


@pytest.fixture
def mock(tmp_path):
    server = serve("127.0.0.1", 0, MockConfig(db_path=tmp_path / "mock.sqlite"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def strict_mock(tmp_path):
    server = serve("127.0.0.1", 0, MockConfig(db_path=tmp_path / "s.sqlite", strict=True))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _entity(name="customers", endpoint="/api/V1/customers", conflict="error") -> EntityMap:
    return EntityMap.model_validate(
        {
            "name": name,
            "source": {"table": name, "key": "id"},
            "target": {"table": name, "key": "id", "endpoint": endpoint, "conflict": conflict},
            "fields": [{"to": "customerName", "from": "name"}],
        }
    )


# --- the reference data the mapping points at -------------------------------


def test_the_reference_endpoints_answer_like_v3(mock):
    for path in ("/api/V1/pricing-profiles", "/api/V1/locations/names/",
                 "/api/V1/customers/paymentterms/", "/api/V1/identity/users"):
        body = httpx.get(mock + path).json()
        assert body["succeeded"] is True
        assert isinstance(body["data"], list) and body["data"]
        assert all("key" in row for row in body["data"])


def test_the_landing_page_says_it_is_not_production(mock):
    page = httpx.get(mock + "/").text
    assert "not the real v3" in page.lower() or "not production" in page.lower()


# --- a migration writes and rolls back -------------------------------------


def test_a_run_inserts_captures_ids_and_rolls_back(mock):
    sink = ApiSink(mock)
    entity = _entity()
    rows = [(1, {"customerName": "Dana Reyes", "customerType": 9}),
            (2, {"customerName": "Sam Oyelaran", "customerType": 9})]

    results = sink.write_batch(entity, rows)
    assert [r.action for r in results] == ["inserted", "inserted"]
    # v3 assigns the key, and the sink has to read it back — that is what a
    # dependent entity's foreign key resolves against.
    keys = [r.target_id for r in results]
    assert all(keys) and len(set(keys)) == 2

    # It really landed.
    stored = httpx.get(mock + "/__mock/records").json()
    assert stored["count"] == 2
    names = {r["payload"]["customerName"] for r in stored["records"]}
    assert names == {"Dana Reyes", "Sam Oyelaran"}
    # And each carries a CUST-000N display id.
    assert all(r["payload"]["customerId"].startswith("CUST-") for r in stored["records"])

    # Rollback removes exactly what the run wrote.
    entries = [
        {"action": "inserted", "target_id": k, "endpoint": "/api/V1/customers"}
        for k in keys
    ]
    undone = sink.undo("customers", "customers", "id", entries)
    sink.close()

    assert undone == 2
    assert httpx.get(mock + "/__mock/records").json()["count"] == 0


def test_a_dependent_record_can_reference_a_captured_id(mock):
    """The reason ids matter: a job points at the customer v3 just assigned."""
    sink = ApiSink(mock)
    [cust] = sink.write_batch(_entity(), [(1, {"customerName": "Dana", "customerType": 9})])

    job = sink.write_batch(
        _entity("jobs", "/api/V1/jobs"),
        [(50, {"customerName": "Dana", "customerKey": cust.target_id, "status": 1})],
    )
    sink.close()

    assert job[0].action == "inserted"
    stored = httpx.get(mock + "/__mock/records").json()
    posted_job = next(r for r in stored["records"] if r["entity"] == "jobs")
    assert posted_job["payload"]["customerKey"] == cust.target_id


# --- validation faithful to what we know -----------------------------------


def test_an_unknown_customer_type_is_rejected_even_when_lenient(mock):
    """A code outside the known set is a mapping bug either way, so it is a
    rejection — arriving as HTTP 200 succeeded:false, the trap the sink reads."""
    sink = ApiSink(mock)
    [result] = sink.write_batch(_entity(), [(1, {"customerName": "X", "customerType": 99})])
    sink.close()

    assert result.action == "failed"
    assert "customerType" in result.error
    assert httpx.get(mock + "/__mock/records").json()["count"] == 0


def test_strict_mode_enforces_the_known_required_fields(strict_mock):
    sink = ApiSink(strict_mock)
    # Missing pricingProfile and phoneNumber.
    [result] = sink.write_batch(_entity(), [(1, {"customerName": "X", "customerType": 9})])
    sink.close()

    assert result.action == "failed"
    assert "required" in result.error


def test_lenient_mode_stores_a_thin_record_so_the_pipeline_can_run(mock):
    sink = ApiSink(mock)
    [result] = sink.write_batch(_entity(), [(1, {"customerName": "X", "customerType": 9})])
    sink.close()
    assert result.action == "inserted"


# --- housekeeping -----------------------------------------------------------


def test_reset_clears_records_but_keeps_the_seed(mock):
    sink = ApiSink(mock)
    sink.write_batch(_entity(), [(1, {"customerName": "X", "customerType": 9})])
    sink.close()

    # v3 wraps a list as data.data[], so the mock does too.
    listed = httpx.get(mock + "/api/V1/customers").json()["data"]["data"]
    assert any(r.get("isSeed") for r in listed)
    assert len(listed) == 2

    httpx.post(mock + "/__mock/reset")
    listed = httpx.get(mock + "/api/V1/customers").json()["data"]["data"]
    assert len(listed) == 1 and listed[0]["isSeed"]      # seed survives


def test_is_mock_url_recognises_the_local_stand_in():
    assert is_mock_url("http://mock-v3:19090")
    assert is_mock_url("http://localhost:19090")
    assert is_mock_url("http://127.0.0.1:19090/api/V1")
    assert not is_mock_url("https://zephyr-glass.eagsoftware.com")
    assert not is_mock_url("http://localhost:8000")   # some other local server
    assert not is_mock_url(None)
