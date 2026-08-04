"""The authenticated-application path: login, capture, paginated API, scrubbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from eag_migrator.web.fetcher import Fetcher
from eag_migrator.web.harvest import AuthExpired, HarvestConfig, harvest
from eag_migrator.web.login import import_session
from eag_migrator.web.recon import recon
from eag_migrator.web.safety import looks_like_pan, luhn_ok, scrub
from eag_migrator.web.session import Session
from eag_migrator.web.staging import Staging

from fixture_app import CSRF_HEADER, CSRF_VALUE, FixtureApp


@pytest.fixture(scope="module")
def app():
    with FixtureApp() as fixture:
        yield fixture


@pytest.fixture
def session(app) -> Session:
    """What `eagm login --cookies` produces, plus the CSRF header the SPA sends."""
    s = import_session(app.cookie_header, app.url)
    s.headers[CSRF_HEADER] = CSRF_VALUE
    return s


@pytest.fixture
def auth_fetcher(app, session, tmp_path):
    with Fetcher(app.url, cache_dir=tmp_path / "cache", rate_limit_rps=0,
                 respect_robots=False, session=session) as f:
        yield f


# --- the login wall ---------------------------------------------------------


def test_recon_reports_that_the_app_is_behind_a_login(app, tmp_path):
    with Fetcher(app.url, cache_dir=tmp_path / "c", rate_limit_rps=0,
                 respect_robots=False) as f:
        profile = recon(f)

    assert profile.requires_auth
    assert any("password input" in e for e in profile.auth_evidence)
    assert any("behind a login" in n for n in profile.notes)


def test_anonymous_requests_get_no_data(app, tmp_path):
    with Fetcher(app.url, cache_dir=tmp_path / "c", rate_limit_rps=0,
                 respect_robots=False) as f:
        page = f.get(app.url)
        api = f.get(f"{app.url}/api/v2/customers")

    assert "Sign in" in page.text
    assert "Dana Reyes" not in page.text
    assert api.status == 401


def test_authenticated_recon_does_not_cry_login_wall(app, session, tmp_path):
    with Fetcher(app.url, cache_dir=tmp_path / "c", rate_limit_rps=0,
                 respect_robots=False, session=session) as f:
        profile = recon(f, probe_apis=False)

    assert profile.authenticated
    assert not profile.requires_auth


# --- sessions ---------------------------------------------------------------


def test_session_round_trips_through_disk(app, session, tmp_path):
    path = tmp_path / "session.json"
    session.save(path)

    assert oct(path.stat().st_mode)[-3:] == "600"  # credentials, not config
    restored = Session.load(path)
    assert restored.cookie_header() == session.cookie_header()
    assert restored.headers[CSRF_HEADER] == CSRF_VALUE


def test_session_description_never_leaks_secrets(app):
    s = Session.from_cookie_header("sid=super-secret-value", app.url)
    s.headers["Authorization"] = "Bearer topsecret"

    described = s.describe()
    assert "super-secret-value" not in described
    assert "topsecret" not in described
    assert "sid" in described and "withheld" in described


def test_session_imports_a_storage_state_export(app, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(
        '{"cookies": [{"name": "cv_session", "value": "abc", "domain": "x", "path": "/"}],'
        ' "origins": []}'
    )
    s = import_session(str(state), app.url)
    assert s.cookie_header() == "cv_session=abc"


def test_adopt_headers_takes_auth_and_ignores_noise(app):
    s = Session.from_cookie_header("sid=1", app.url)
    added = s.adopt_headers(
        {
            "X-CSRF-Token": "tok",
            "Authorization": "Bearer abc",
            "Content-Length": "42",
            "User-Agent": "Chrome",
        }
    )
    assert added == 2
    assert set(s.headers) == {"X-CSRF-Token", "Authorization"}


def test_expired_session_is_reported_not_silently_empty(app, tmp_path):
    """A stale cookie must fail loudly, not quietly harvest zero rows."""
    stale = Session.from_cookie_header("cv_session=expired", app.url)
    config = _api_config(app, "customers", "/api/v2/customers")

    with Staging(tmp_path / "s.sqlite") as staging:
        with pytest.raises(AuthExpired, match="401"):
            harvest(config, staging, cache_dir=tmp_path / "c", session=stale)


def test_harvest_refuses_to_run_unauthenticated_on_an_auth_app(app, tmp_path):
    config = _api_config(app, "customers", "/api/v2/customers")
    config.site.requires_auth = True

    with Staging(tmp_path / "s.sqlite") as staging:
        with pytest.raises(AuthExpired, match="requires a login"):
            harvest(config, staging, cache_dir=tmp_path / "c", session=None)


# --- the authenticated API --------------------------------------------------


def _api_config(app, name: str, path: str, **api) -> HarvestConfig:
    spec = {"url": f"{app.url}{path}", "record_path": "data", "per_page": 2}
    spec.update(api)
    return HarvestConfig.model_validate(
        {
            "site": {"base_url": app.url, "rate_limit_rps": 0, "respect_robots": False},
            "collections": [
                {
                    "name": name,
                    "key": "id",
                    "discover": {"api": spec},
                    "extract": {
                        "type": "json",
                        "fields": [
                            {"to": "id", "path": "id"},
                            {"to": "name", "path": "name"},
                            {"to": "email", "path": "email"},
                            {"to": "phone", "path": "phone"},
                        ],
                    },
                }
            ],
        }
    )


def test_page_style_pagination(app, session, tmp_path):
    config = _api_config(app, "customers", "/api/v2/customers", style="page")
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c", session=session)
        rows = staging.sample("customers", 10)

    assert report.total_stored == 5           # 5 records over 3 pages of 2
    assert report.authenticated
    assert {r["name"] for r in rows} >= {"Dana Reyes", "Ash Whitfield"}


def test_offset_style_pagination(app, session, tmp_path):
    config = _api_config(
        app, "customers", "/api/v2/customers",
        style="offset", per_page_param="limit", offset_param="offset",
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c", session=session)
    assert report.total_stored == 5


def test_cursor_style_pagination(app, session, tmp_path):
    config = _api_config(
        app, "customers", "/api/v2/customers",
        style="cursor", per_page_param="limit", cursor_param="cursor",
        cursor_path="meta.next_cursor",
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c", session=session)
    assert report.total_stored == 5


def test_post_endpoint_pagination(app, session, tmp_path):
    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": app.url, "rate_limit_rps": 0, "respect_robots": False},
            "collections": [
                {
                    "name": "search",
                    "key": "id",
                    "discover": {
                        "api": {
                            "url": f"{app.url}/api/v2/search",
                            "method": "POST",
                            "body": {"q": "", "type": "customer"},
                            "style": "page",
                            "per_page": 2,
                            "record_path": "results",
                        }
                    },
                    "extract": {
                        "type": "json",
                        "fields": [{"to": "id", "path": "id"}, {"to": "name", "path": "name"}],
                    },
                }
            ],
        }
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c", session=session)
    assert report.total_stored == 5


def test_nested_objects_are_stored_as_json(app, session, tmp_path):
    """A quote's customer is an object; it must survive rather than stringify oddly."""
    import json as _json

    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": app.url, "rate_limit_rps": 0, "respect_robots": False},
            "collections": [
                {
                    "name": "quotes",
                    "key": "id",
                    "discover": {
                        "api": {"url": f"{app.url}/api/v2/quotes", "record_path": "data",
                                "per_page": 2}
                    },
                    "extract": {
                        "type": "json",
                        "fields": [
                            {"to": "id", "path": "id"},
                            {"to": "customer", "path": "customer"},
                            {"to": "customer_id", "path": "customer.id"},
                            {"to": "vin", "path": "vin"},
                            {"to": "total", "path": "total"},
                        ],
                    },
                }
            ],
        }
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        harvest(config, staging, cache_dir=tmp_path / "c", session=session)
        rows = {r["id"]: r for r in staging.sample("quotes", 10)}

    assert _json.loads(rows[9001]["customer"])["name"] == "Dana Reyes"
    assert rows[9001]["customer_id"] == 5001   # the dotted path pulls it out flat


# --- cardholder data --------------------------------------------------------


def test_luhn_and_pan_detection():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert looks_like_pan("4111 1111 1111 1111") == "4111111111111111"
    assert looks_like_pan("5555-5555-5555-4444") == "5555555555554444"
    # A job number of the same length must not be mistaken for a card.
    assert looks_like_pan("9900112233445566") is None
    assert looks_like_pan("not a card") is None


def test_scrub_blocks_cvv_and_masks_pan():
    row, report = scrub(
        {"id": 1, "card_number": "4111111111111111", "cvv": "123", "card_brand": "visa"}
    )
    assert row["cvv"] is None
    assert row["card_number"] == "•••• 1111"
    assert row["card_brand"] == "visa"
    assert "cvv" in report.blocked and "card_number" in report.masked


def test_scrub_catches_a_pan_hiding_in_free_text():
    row, report = scrub({"notes": "4111 1111 1111 1111"})
    assert "4111111111111111" not in str(row["notes"])
    assert "notes" in report.masked


def test_harvesting_payments_never_writes_card_data(app, session, tmp_path):
    """The whole point: a careless config must not put PANs on disk."""
    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": app.url, "rate_limit_rps": 0, "respect_robots": False},
            "collections": [
                {
                    "name": "payments",
                    "key": "id",
                    "discover": {
                        "api": {"url": f"{app.url}/api/v2/payments", "record_path": "data",
                                "per_page": 50}
                    },
                    "extract": {
                        "type": "json",
                        "fields": [
                            {"to": "id", "path": "id"},
                            {"to": "amount", "path": "amount"},
                            {"to": "processor_ref", "path": "processor_ref"},
                            # Deliberately careless — this is what the guard is for.
                            {"to": "card_number", "path": "card_number"},
                            {"to": "cvv", "path": "cvv"},
                            {"to": "card_brand", "path": "card_brand"},
                        ],
                    },
                }
            ],
        }
    )
    staging_path = tmp_path / "s.sqlite"
    with Staging(staging_path) as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c", session=session)
        rows = staging.sample("payments", 10)

    assert report.total_stored == 2
    assert all(r["cvv"] is None for r in rows)
    assert {r["card_number"] for r in rows} == {"•••• 1111", "•••• 4444"}
    # The safe parts survive — the migration still works.
    assert {r["processor_ref"] for r in rows} == {"ch_3PabcdEFGH", "ch_3PijklMNOP"}
    assert {r["card_brand"] for r in rows} == {"visa", "mastercard"}

    # And nothing sensitive is anywhere in the file, not even in a stray column.
    raw = staging_path.read_bytes()
    assert b"4111111111111111" not in raw
    assert b"5555555555554444" not in raw
    assert b"5555 5555 5555 4444" not in raw

    result = report.collections[0]
    assert "cvv" in result.scrubbed.blocked
    assert any("must never be stored" in s for s in result.scrubbed.summary())


def test_scaffold_does_not_let_scrape_metadata_hijack_real_columns(app, session, tmp_path):
    """`_fetched_at` fuzzy-matches a target `created_at`.

    If harvester bookkeeping is considered before the real columns, every
    migrated record silently gets the scrape time as its creation date.
    """
    import sqlite3

    from eag_migrator.db import build_engine
    from eag_migrator.discovery import profile_database
    from eag_migrator.scaffold import build_draft

    config = _api_config(app, "customers", "/api/v2/customers")
    config.collections[0].extract.fields.append(
        __import__("eag_migrator.web.harvest", fromlist=["FieldSpec"]).FieldSpec(
            to="created_at", path="created_at"
        )
    )
    staging_path = tmp_path / "staging.sqlite"
    with Staging(staging_path) as staging:
        harvest(config, staging, cache_dir=tmp_path / "c", session=session)

    v3_path = tmp_path / "v3.sqlite"
    sqlite3.connect(v3_path).executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " legacy_id INTEGER, name TEXT, email TEXT, created_at DATETIME);"
    )

    v2 = profile_database(build_engine(f"sqlite:///{staging_path}"), "v2", sample_rows=0)
    v3 = profile_database(build_engine(f"sqlite:///{v3_path}"), "v3", sample_rows=0)
    mapping, _ = build_draft(v2, v3)

    created = next(f for f in mapping.get("customer").fields if f.to == "created_at")
    assert created.from_ == "created_at"
    assert created.from_ != "_fetched_at"


def test_pii_detection_does_not_cry_wolf():
    """On a scheduling record `state` means scheduled/completed, not a US state.

    A processing record full of false positives is one nobody reads.
    """
    _, report = scrub({"id": 1, "state": "completed", "status": "done", "code": "AB12"})
    assert not report.pii_fields

    _, qualified = scrub({"billing_state": "CA", "address_state": "NY"})
    assert qualified.pii_fields == {"billing_state", "address_state"}


def test_pii_is_recorded_for_the_processing_log(app, session, tmp_path):
    config = _api_config(app, "customers", "/api/v2/customers")
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c", session=session)

    pii = report.collections[0].scrubbed.pii_fields
    assert {"name", "email", "phone"} <= pii
    assert any("personal data present" in s for s in report.collections[0].scrubbed.summary())
