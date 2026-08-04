"""Authenticated capture and drafting, against the fake application.

Skipped when no Chromium is available.
"""

from __future__ import annotations

import pytest

from fixture_app import CSRF_HEADER, CSRF_VALUE, FixtureApp

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from eag_migrator.web.capture import capture, find_chromium  # noqa: E402
from eag_migrator.web.draft import draft_from_capture  # noqa: E402
from eag_migrator.web.harvest import harvest  # noqa: E402
from eag_migrator.web.login import LoginSpec, form_login  # noqa: E402
from eag_migrator.web.session import Session  # noqa: E402
from eag_migrator.web.staging import Staging  # noqa: E402

pytestmark = pytest.mark.skipif(
    find_chromium() is None, reason="no Chromium binary available"
)


@pytest.fixture(scope="module")
def app():
    with FixtureApp() as fixture:
        yield fixture


@pytest.fixture
def session(app) -> Session:
    s = Session.from_cookie_header(app.cookie_header, app.url)
    return s


# --- logging in -------------------------------------------------------------


def test_form_login_gets_a_session_and_the_csrf_header(app, monkeypatch):
    """The SPA sends a CSRF header its cookies do not carry; login must catch it."""
    monkeypatch.setenv("EAGM_USERNAME", "owner@clearview.example")
    monkeypatch.setenv("EAGM_PASSWORD", "correct-horse")

    session = form_login(
        app.url, LoginSpec(login_url="/login", success_selector=".dashboard")
    )

    assert any(c["name"] == "cv_session" for c in session.cookies)
    assert session.headers.get(CSRF_HEADER) == CSRF_VALUE


def test_form_login_rejects_bad_credentials(app, monkeypatch):
    from eag_migrator.web.login import LoginFailed

    monkeypatch.setenv("EAGM_USERNAME", "owner@clearview.example")
    monkeypatch.setenv("EAGM_PASSWORD", "wrong")

    with pytest.raises(LoginFailed):
        form_login(app.url, LoginSpec(login_url="/login", success_selector=".dashboard"))


# --- capture ----------------------------------------------------------------


def test_anonymous_capture_finds_nothing_useful(app):
    """Without a session the app shell never loads, so there is nothing to record."""
    report = capture(app.url, ["/app/"], har_path=None, wait_ms=1500, scroll=False)
    assert not report.api_calls


def test_authenticated_capture_finds_the_internal_api(app, session):
    report = capture(
        app.url, ["/app/"], har_path=None, wait_ms=3500, scroll=False, session=session
    )

    assert report.authenticated
    patterns = {c.pattern for c in report.api_calls}
    assert any("/api/v2/customers" in p for p in patterns)
    assert any("/api/v2/quotes" in p for p in patterns)
    assert any("/api/v2/payments" in p for p in patterns)

    customers = next(c for c in report.api_calls if "/api/v2/customers" in c.pattern)
    assert customers.item_count == 2          # the first page
    assert customers.record_path == "data"    # the list is wrapped
    assert {"id", "name", "email"} <= set(customers.response_keys)


def test_capture_picks_up_the_auth_header_for_replay(app, session):
    report = capture(
        app.url, ["/app/"], har_path=None, wait_ms=3500, scroll=False, session=session
    )
    assert report.auth_headers.get(CSRF_HEADER) == CSRF_VALUE

    # ...and it never reaches a report file.
    assert report.to_dict()["auth_headers"] == [CSRF_HEADER]


def test_capture_infers_each_pagination_style(app, session):
    report = capture(
        app.url, ["/app/"], har_path=None, wait_ms=3500, scroll=False, session=session
    )
    by_path = {c.pattern.split("/api/v2/")[-1].split("?")[0]: c for c in report.api_calls}

    assert by_path["customers"].pagination["style"] == "page"
    assert by_path["customers"].pagination["per_page_param"] == "per_page"
    assert by_path["quotes"].pagination["style"] == "offset"
    assert by_path["quotes"].pagination["per_page_param"] == "limit"

    # A POST endpoint keeps its paging in the body, not the query string.
    search = next(c for c in report.api_calls if c.method == "POST")
    assert search.pagination["style"] == "page"
    assert search.pagination["per_page_param"] == "per_page"


def test_draft_moves_post_paging_out_of_the_replayed_body(app, session):
    """The harvester sets page/per_page itself; leaving them pinned in the body
    would replay page 1 forever."""
    report = capture(
        app.url, ["/app/"], har_path=None, wait_ms=3500, scroll=False, session=session
    )
    config, _ = draft_from_capture(report, app.url)

    search = next(c for c in config.collections if c.name == "search")
    assert search.discover.api.method == "POST"
    assert search.discover.api.style == "page"
    assert set(search.discover.api.body or {}) == {"q", "type"}


# --- drafting from the capture ---------------------------------------------


def test_draft_from_capture_builds_a_runnable_config(app, session):
    report = capture(
        app.url, ["/app/"], har_path=None, wait_ms=3500, scroll=False, session=session
    )
    config, warnings = draft_from_capture(report, app.url)

    names = {c.name for c in config.collections}
    assert {"customers", "quotes", "payments"} <= names
    # /api/v2/session is plumbing, not a business record.
    assert "session" not in names

    assert config.site.requires_auth is True

    customers = next(c for c in config.collections if c.name == "customers")
    assert customers.discover.api.style == "page"
    assert customers.discover.api.record_path == "data"
    assert customers.key == "id"
    # Pagination params are stripped from the stored URL so they can be re-added.
    assert "page=" not in customers.discover.api.url
    assert {f.path for f in customers.extract.fields} >= {"id", "name", "email"}

    quotes = next(c for c in config.collections if c.name == "quotes")
    assert quotes.discover.api.style == "offset"
    assert any("nested" in w for w in warnings)  # quotes.customer is an object


def test_the_drafted_config_actually_harvests(app, session, tmp_path):
    """End to end: what capture drafts must run without hand-editing."""
    report = capture(
        app.url, ["/app/"], har_path=None, wait_ms=3500, scroll=False, session=session
    )
    config, _ = draft_from_capture(report, app.url)
    config.site.rate_limit_rps = 0
    session.headers[CSRF_HEADER] = CSRF_VALUE

    with Staging(tmp_path / "s.sqlite") as staging:
        result = harvest(config, staging, cache_dir=tmp_path / "c", session=session)
        counts = {t: staging.count(t) for t in staging.tables()}

    assert result.total_failed == 0
    assert counts["customers"] == 5     # paged past the first 2
    assert counts["quotes"] == 4
    assert counts["payments"] == 2

    # And the guard still holds on data pulled in by a generated config.
    payments = next(c for c in result.collections if c.name == "payments")
    assert "cvv" in payments.scrubbed.blocked
    raw = (tmp_path / "s.sqlite").read_bytes()
    assert b"4111111111111111" not in raw


# --- signing in from the dashboard -----------------------------------------


def test_dashboard_signs_in_with_supplied_credentials(app, tmp_path, monkeypatch):
    """End to end: type a username and password into the UI, get a session.

    No mocking — this drives the fixture app's real login form in real
    Chromium, exactly as it would against v2.
    """
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import eag_migrator.dashboard.app as dash

    for name in ("CONFIG_DIR", "PROFILES_DIR", "REPORTS_DIR", "STATE_DIR"):
        folder = tmp_path / name.lower()
        folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dash, name, folder)
    monkeypatch.setattr(dash, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(dash, "STATE_DB", tmp_path / "migration.sqlite")
    monkeypatch.setattr(dash, "STAGING_DB", tmp_path / "staging.sqlite")
    monkeypatch.setattr(dash, "jobs", dash.JobManager())
    monkeypatch.delenv("EAGM_DASHBOARD_TOKEN", raising=False)

    client = TestClient(dash.create_app())
    res = client.post(
        "/actions/login-form",
        data={
            "url": app.url,
            "username": "owner@clearview.example",
            "password": "correct-horse",
            "login_url": "/login",
            "success_selector": ".dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303

    import time

    job = dash.jobs.recent(1)[0]
    deadline = time.time() + 60
    while job.active and time.time() < deadline:
        time.sleep(0.1)
    assert job.status == "done", job.error

    # A working session landed, including the header the SPA attaches.
    session = Session.load(dash.SESSION_FILE)
    assert any(c["name"] == "cv_session" for c in session.cookies)
    assert session.headers.get(CSRF_HEADER) == CSRF_VALUE

    # And it is genuinely authenticated against the app.
    from eag_migrator.web.fetcher import Fetcher

    with Fetcher(app.url, cache_dir=None, use_cache=False, rate_limit_rps=0,
                 respect_robots=False, session=session) as fetcher:
        resp = fetcher.get(f"{app.url}/api/v2/customers?per_page=5")
    assert resp.status == 200
    assert "Dana Reyes" in resp.text

    # The password is nowhere.
    assert "correct-horse" not in "\n".join(job.log)
    assert "correct-horse" not in dash.SESSION_FILE.read_text()
