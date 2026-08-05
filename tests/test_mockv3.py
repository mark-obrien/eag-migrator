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
    assert "not the real system" in page.lower()


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


# --- snapshotting v3 into local files ---------------------------------------
#
# The snapshot is the "give me the real v3 data to analyze" path. It reads
# read-only and, because it uses the same client/envelope handling, it can be
# tested by pointing it at the mock — which speaks v3's dialect.


def test_snapshot_pulls_reference_data_and_records(mock, tmp_path):
    from eag_migrator import v3_snapshot

    # Put a couple of customers in the mock so the paged pull has something.
    sink = ApiSink(mock)
    sink.write_batch(_entity(), [(1, {"customerName": "Dana", "customerType": 9}),
                                 (2, {"customerName": "Sam", "customerType": 9})])
    sink.close()

    client = httpx.Client(base_url=mock)
    try:
        result = v3_snapshot.snapshot(client)
    finally:
        client.close()

    assert result["pricing_profiles"]["ok"]
    assert len(result["payment_terms"]["data"]) == 8
    # customers are paged: seed + the two posted.
    assert result["customers"]["ok"]
    names = {c["customerName"] for c in result["customers"]["data"] if "customerName" in c}
    assert {"Dana", "Sam"} <= names

    counts = v3_snapshot.save(result, tmp_path / "reports", tmp_path / "snap.json")
    assert counts["payment_terms"] == 8
    assert (tmp_path / "reports" / "v3-snapshot" / "pricing_profiles.json").exists()
    # The reference file the mock will serve.
    ref = v3_snapshot.load_reference(tmp_path / "snap.json")
    assert "pricing_profiles" in ref and "payment_terms" in ref
    # Records are NOT in the reference file — that is reference data only.
    assert "customers" not in ref


def test_field_keys_reports_the_shape_for_analysis(mock):
    from eag_migrator import v3_snapshot

    client = httpx.Client(base_url=mock)
    try:
        result = v3_snapshot.snapshot(client)
    finally:
        client.close()
    keys = v3_snapshot.field_keys(result)
    assert "profileName" in keys["pricing_profiles"]
    assert "termsCode" in keys["payment_terms"]


def test_the_mock_serves_real_reference_ids_from_a_snapshot(tmp_path):
    """The point of snapshotting: a rehearsal uses v3's true ids, not seed ones."""
    import json

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({
        "pricing_profiles": [{"key": "REAL-PROD-UUID", "profileName": "Shop Default"}],
        "payment_terms": [{"key": "REAL-TERMS-UUID", "termsCode": "NET30", "code": 1}],
    }))

    server = serve("127.0.0.1", 0, MockConfig(db_path=tmp_path / "m.sqlite", snapshot_path=snap))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    try:
        pp = httpx.get(base + "/api/V1/pricing-profiles").json()["data"]
        # Endpoints with no snapshot key fall back to the synthetic seed.
        loc = httpx.get(base + "/api/V1/locations/names/").json()["data"]
    finally:
        server.shutdown()
        server.server_close()

    assert pp == [{"key": "REAL-PROD-UUID", "profileName": "Shop Default"}]
    assert loc and loc[0]["name"] == "Default"        # seed fallback


def test_without_a_snapshot_the_mock_serves_the_synthetic_seed(mock):
    from eag_migrator.mockv3 import seed

    pp = httpx.get(mock + "/api/V1/pricing-profiles").json()["data"]
    assert pp == seed.PRICING_PROFILES


# --- browsable views: checking the migrated data ----------------------------


def test_the_views_show_migrated_records_and_stay_marked_a_rehearsal(mock):
    sink = ApiSink(mock)
    [dana, _sam] = sink.write_batch(_entity(), [
        (1, {"customerName": "Dana Reyes", "customerType": 9, "email": "dana@example.com"}),
        (2, {"customerName": "Sam Oyelaran", "customerType": 9}),
    ])
    sink.close()

    listing = httpx.get(mock + "/view/customers").text
    assert "Dana Reyes" in listing and "Sam Oyelaran" in listing
    # Every view carries the banner, so it can never be mistaken for real v3.
    assert "not the real system" in listing

    detail = httpx.get(mock + f"/view/customers/{dana.target_id}").text
    assert "dana@example.com" in detail
    assert "not the real system" in detail

    # The overview links to whatever entities exist.
    index = httpx.get(mock + "/").text
    assert "/view/customers" in index


def test_the_reference_view_shows_the_ids_the_mapping_points_at(mock):
    page = httpx.get(mock + "/view/reference").text
    assert "Default" in page                       # the pricing profile / location
    assert "NET30" in page                         # a payment term
    assert "synthetic seed" in page                # honest about the source


def test_an_empty_entity_view_says_so_rather_than_erroring(mock):
    page = httpx.get(mock + "/view/jobs")
    assert page.status_code == 200
    assert "No jobs yet" in page.text
