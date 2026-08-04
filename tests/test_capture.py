"""Network capture against a client-rendered page.

Skipped when there is no Chromium — capture is the one optional feature, since
the browser adds several hundred MB to the image.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixture_site import FixtureSite

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from eag_migrator.web.capture import capture, find_chromium, render_markdown  # noqa: E402

pytestmark = pytest.mark.skipif(
    find_chromium() is None, reason="no Chromium binary available"
)


@pytest.fixture(scope="module")
def site():
    with FixtureSite() as fixture:
        yield fixture


def test_capture_finds_the_xhr_behind_a_client_rendered_page(site, tmp_path):
    """/app/ renders nothing server-side; its content arrives over fetch()."""
    report = capture(site.url, ["/app/"], har_path=tmp_path / "c.har", wait_ms=3000)

    assert report.pages_visited == [f"{site.url}/app/"]
    assert report.api_calls, "the fetch() call should have been recorded"

    call = report.api_calls[0]
    assert call.method == "GET"
    assert "/wp-json/wp/v2/services" in call.pattern
    assert call.status == 200
    assert call.item_count == 3
    assert {"id", "slug", "link", "base_price"} <= set(call.response_keys)


def test_capture_normalises_urls_into_patterns(site, tmp_path):
    report = capture(site.url, ["/app/"], har_path=None, wait_ms=3000)
    call = report.api_calls[0]
    # Query values are dropped, names kept — so the same endpoint called with
    # different pages collapses to one row instead of hundreds.
    assert "per_page=…" in call.pattern
    assert "per_page=100" not in call.pattern


def test_capture_writes_a_har_for_devtools(site, tmp_path):
    har = tmp_path / "capture.har"
    report = capture(site.url, ["/app/"], har_path=har, wait_ms=2000)

    assert report.har_path == str(har)
    assert har.exists() and har.stat().st_size > 0


def test_capture_report_renders(site, tmp_path):
    report = capture(site.url, ["/app/"], har_path=None, wait_ms=2500)
    md = render_markdown(report)

    assert "JSON endpoints the site calls itself" in md
    assert "/wp-json/wp/v2/services" in md


def test_static_page_reports_that_there_is_no_api_to_find(site, tmp_path):
    """A server-rendered page yields no XHR — say so, don't leave it ambiguous."""
    report = capture(site.url, ["/services/rock-chip-repair/"], har_path=None, wait_ms=1500)

    assert not report.api_calls
    assert any("server-rendered" in note for note in report.notes)


def test_find_chromium_honours_an_explicit_override(monkeypatch, tmp_path):
    fake = tmp_path / "my-chrome"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("EAGM_CHROMIUM_PATH", str(fake))
    assert find_chromium() == fake
