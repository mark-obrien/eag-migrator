"""Recon, harvest and staging against a real HTTP server."""

from __future__ import annotations

from pathlib import Path

import pytest

from eag_migrator.web.draft import draft_config
from eag_migrator.web.extract import extract_html, extract_json, links_from
from eag_migrator.web.fetcher import Fetcher
from eag_migrator.web.harvest import HarvestConfig, harvest
from eag_migrator.web.recon import recon
from eag_migrator.web.staging import Staging

from fixture_site import FixtureSite


@pytest.fixture(scope="module")
def site():
    with FixtureSite() as fixture:
        yield fixture


@pytest.fixture
def fetcher(site, tmp_path: Path):
    with Fetcher(site.url, cache_dir=tmp_path / "cache", rate_limit_rps=0) as f:
        yield f


# --- politeness -------------------------------------------------------------


def test_robots_disallow_is_obeyed(fetcher, site):
    assert fetcher.allowed(f"{site.url}/services/rock-chip-repair/")
    assert not fetcher.allowed(f"{site.url}/private/secret/")

    resp = fetcher.get(f"{site.url}/private/secret/")
    assert resp.status == 999
    assert "secret" not in resp.text
    assert fetcher.stats.blocked_by_robots


def test_robots_disallow_can_not_be_bypassed_by_the_crawler(site, tmp_path):
    """A disallowed URL in the sitemap must not be fetched."""
    with Fetcher(site.url, cache_dir=tmp_path / "c", rate_limit_rps=0) as f:
        profile = recon(f, probe_apis=False)
    assert any("/private/" in u for u in profile.urls)  # it *is* in the sitemap

    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": site.url, "rate_limit_rps": 0},
            "collections": [
                {
                    "name": "everything",
                    "discover": {"sitemap": {"match": "."}},
                    "extract": {"fields": [{"to": "url", "source": "url"}]},
                }
            ],
        }
    )
    with Staging(tmp_path / "staging.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c2")
        rows = staging.sample("everything", 100)

    assert report.robots_blocked >= 1
    assert not any("/private/" in (r["_url"] or "") for r in rows)


def test_responses_are_cached_so_reruns_do_not_re_hit_the_site(fetcher, site):
    first = fetcher.get(f"{site.url}/about/")
    assert not first.from_cache
    made = fetcher.stats.requests

    second = fetcher.get(f"{site.url}/about/")
    assert second.from_cache
    assert second.text == first.text
    assert fetcher.stats.requests == made  # no new traffic


def test_offsite_urls_are_refused(fetcher):
    assert fetcher.get("https://example.org/whatever").status == 0


# --- recon ------------------------------------------------------------------


def test_recon_identifies_the_platform(fetcher):
    profile = recon(fetcher)
    platforms = {p["platform"] for p in profile.platforms}
    assert "WordPress" in platforms
    assert profile.generator == "WordPress 6.4.2"


def test_recon_finds_the_sitemap_and_groups_urls(fetcher):
    profile = recon(fetcher)
    assert profile.sitemaps
    assert len(profile.urls) == 8
    patterns = {g["pattern"] for g in profile.url_groups}
    assert "/services/<...>" in patterns
    assert "/locations/<...>" in patterns


def test_recon_enumerates_the_wordpress_rest_api(fetcher):
    profile = recon(fetcher)
    by_name = {ct.name: ct for ct in profile.content_types}

    assert by_name["service"].total == 3
    assert by_name["location"].total == 2
    assert by_name["page"].total == 2
    assert by_name["service"].accessible
    assert "nags_prefix" in by_name["service"].fields
    assert any("WordPress REST API is open" in n for n in profile.notes)


def test_recon_extracts_structured_business_data(fetcher):
    profile = recon(fetcher)
    assert "AutoRepair" in profile.jsonld_types
    assert "PostalAddress" in profile.jsonld_types
    assert any("JSON-LD" in note for note in profile.notes)


# --- drafting a harvest config ---------------------------------------------


def test_draft_prefers_the_json_api_over_scraping(fetcher):
    profile = recon(fetcher)
    config, warnings = draft_config(profile)

    names = {c.name for c in config.collections}
    assert {"service", "location", "page"} <= names
    assert all(c.extract.type == "json" for c in config.collections)
    assert all(c.discover.api is not None for c in config.collections)
    assert any("REST API" in w or "JSON API" in w for w in warnings)


def test_draft_keeps_custom_fields(fetcher):
    """ACF/meta fields are the valuable part for a trade site — never drop them."""
    profile = recon(fetcher)
    config, warnings = draft_config(profile)

    service = next(c for c in config.collections if c.name == "service")
    paths = {f.path for f in service.extract.fields}
    assert {"nags_prefix", "base_price"} <= paths

    custom = next(f for f in service.extract.fields if f.path == "nags_prefix")
    assert custom.note and "custom field" in custom.note
    assert any("nags_prefix" in w for w in warnings)


def test_draft_skips_empty_and_internal_content_types(fetcher):
    profile = recon(fetcher)
    config, _ = draft_config(profile)
    names = {c.name for c in config.collections}
    assert "attachment" not in names and "media" not in names  # internal + empty
    assert "post" not in names  # zero rows


def test_draft_falls_back_to_html_when_there_is_no_api(fetcher):
    profile = recon(fetcher)
    profile.content_types = []  # pretend the REST API was closed
    config, warnings = draft_config(profile)

    assert config.collections
    assert all(c.extract.type == "html" for c in config.collections)
    assert any("generic guess" in w for w in warnings)
    # Every generated selector is flagged for review.
    assert any(f.note for c in config.collections for f in c.extract.fields)


# --- extraction -------------------------------------------------------------


def test_extract_html_reads_selectors_and_attributes(fetcher, site):
    from types import SimpleNamespace as F

    resp = fetcher.get(f"{site.url}/services/adas-calibration/")
    fields = [
        F(to="url", source="url", selector=None, const=None, attr="text", many=False, required=False, path=None),
        F(to="title", source="selector", selector="h1", const=None, attr="text", many=False, required=False, path=None),
        F(to="desc", source="selector", selector='meta[name="description"]', const=None,
          attr="content", many=False, required=False, path=None),
        F(to="images", source="selector", selector="main img", const=None, attr="src",
          many=True, required=False, path=None),
    ]
    got = extract_html(resp.text, resp.url, fields)

    assert got["title"] == "ADAS Calibration"
    assert "Clearview" in got["desc"]
    # Relative src resolved against the page URL.
    assert got["images"] == [f"{site.url}/wp-content/uploads/adas-calibration.jpg"]


def test_extract_reads_jsonld_by_type_and_path(fetcher, site):
    from types import SimpleNamespace as F

    resp = fetcher.get(f"{site.url}/services/rock-chip-repair/")
    fields = [
        F(to="phone", source="jsonld", path="telephone", selector=None, const=None,
          attr="text", many=False, required=False),
        F(to="city", source="jsonld", path="address.addressLocality", selector=None,
          const=None, attr="text", many=False, required=False),
        F(to="name", source="jsonld", path="AutoRepair.name", selector=None, const=None,
          attr="text", many=False, required=False),
    ]
    got = extract_html(resp.text, resp.url, fields)

    assert got["phone"] == "(555) 010-2030"
    assert got["city"] == "Springfield"
    assert got["name"] == "Clearview Auto Glass"


def test_extract_json_resolves_dotted_paths():
    from types import SimpleNamespace as F

    record = {"id": 7, "title": {"rendered": "Windshield"}, "tags": [{"n": "a"}, {"n": "b"}]}
    fields = [
        F(to="id", path="id", source="selector", const=None, required=False),
        F(to="title", path="title.rendered", source="selector", const=None, required=False),
        F(to="tags", path="tags.n", source="selector", const=None, required=False),
        F(to="missing", path="nope.deeper", source="selector", const=None, required=False),
    ]
    got = extract_json(record, fields)

    assert got == {"id": 7, "title": "Windshield", "tags": ["a", "b"], "missing": None}


def test_links_are_resolved_and_filtered(fetcher, site):
    resp = fetcher.get(site.url)
    links, _skipped = links_from(resp.text, resp.url, r"/services/")
    assert len(links) == 3
    assert all(link.startswith(f"{site.url}/services/") for link in links)


def test_destructive_links_are_never_followed():
    html = """
      <a href="/customers">Customers</a>
      <a href="/customers/4/edit">Edit</a>
      <a href="/logout">Sign out</a>
      <a href="/quotes/9/delete">Delete</a>
      <a href="/invoices/3/refund">Refund</a>
      <a href="/invoices/3" data-method="delete">Remove</a>
      <a href="/jobs/7/close" data-confirm="Are you sure?">Close</a>
      <a href="mailto:ops@example.test">Email</a>
      <a href="javascript:void(0)">Menu</a>
    """
    links, skipped = links_from(html, "https://app.example.test/dash")

    # mailto:/javascript: are not links to anywhere, so they are simply dropped.
    assert links == [
        "https://app.example.test/customers",
        "https://app.example.test/customers/4/edit",
    ]
    refused = dict(skipped)
    assert set(refused) == {
        "https://app.example.test/logout",
        "https://app.example.test/quotes/9/delete",
        "https://app.example.test/invoices/3/refund",
        "https://app.example.test/invoices/3",
        "https://app.example.test/jobs/7/close",
    }
    assert refused["https://app.example.test/invoices/3"] == "data-method=delete"
    assert refused["https://app.example.test/jobs/7/close"] == "has a confirmation prompt"


# --- harvest ----------------------------------------------------------------


def _api_config(site) -> HarvestConfig:
    return HarvestConfig.model_validate(
        {
            "site": {"base_url": site.url, "rate_limit_rps": 0},
            "collections": [
                {
                    "name": "services",
                    "key": "id",
                    "discover": {
                        "api": {"url": f"{site.url}/wp-json/wp/v2/services", "per_page": 2}
                    },
                    "extract": {
                        "type": "json",
                        "fields": [
                            {"to": "id", "path": "id"},
                            {"to": "slug", "path": "slug"},
                            {"to": "title", "path": "title.rendered"},
                            {"to": "body_html", "path": "content.rendered"},
                            {"to": "nags_prefix", "path": "nags_prefix"},
                            {"to": "base_price", "path": "base_price"},
                        ],
                    },
                }
            ],
        }
    )


def test_harvest_pages_through_the_api(site, tmp_path):
    """per_page=2 over 3 records must paginate, not stop at the first page."""
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(_api_config(site), staging, cache_dir=tmp_path / "c")
        rows = staging.sample("services", 10)

    assert report.total_stored == 3
    assert report.total_failed == 0
    titles = {r["title"] for r in rows}
    assert titles == {"Windshield Replacement", "Rock Chip Repair", "ADAS Calibration"}
    assert {r["nags_prefix"] for r in rows} == {"DW", "RC", "CAL"}


def test_harvest_records_the_page_url_not_the_api_url(site, tmp_path):
    """`_url` feeds the redirect map, so it must be the permalink.

    The fetch URL for an API collection is a paginated endpoint and would be
    identical for every record — worthless for redirects.
    """
    config = _api_config(site)
    config.collections[0].extract.fields.append(
        __import__("eag_migrator.web.harvest", fromlist=["FieldSpec"]).FieldSpec(
            to="link", path="link"
        )
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        harvest(config, staging, cache_dir=tmp_path / "c")
        rows = staging.sample("services", 10)

    urls = {r["_url"] for r in rows}
    assert len(urls) == 3  # one per record, not one shared endpoint
    assert all(u.startswith(f"{site.url}/services/") for u in urls)
    assert not any("wp-json" in u for u in urls)


def test_harvest_is_idempotent(site, tmp_path):
    config = _api_config(site)
    with Staging(tmp_path / "s.sqlite") as staging:
        harvest(config, staging, cache_dir=tmp_path / "c")
        assert staging.count("services") == 3
        harvest(config, staging, cache_dir=tmp_path / "c")
        assert staging.count("services") == 3  # updated in place, not duplicated


def test_harvest_html_via_sitemap(site, tmp_path):
    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": site.url, "rate_limit_rps": 0},
            "collections": [
                {
                    "name": "services",
                    "discover": {"sitemap": {"match": "/services/"}},
                    "extract": {
                        "type": "html",
                        "fields": [
                            {"to": "url", "source": "url"},
                            {"to": "title", "selector": "h1"},
                            {"to": "phone", "source": "jsonld", "path": "telephone"},
                        ],
                    },
                }
            ],
        }
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c")
        rows = staging.sample("services", 10)

    assert report.total_stored == 3
    assert {r["title"] for r in rows} == {
        "Windshield Replacement", "Rock Chip Repair", "ADAS Calibration"
    }
    assert all(r["phone"] == "(555) 010-2030" for r in rows)


def test_harvest_crawls_when_there_is_no_sitemap_match(site, tmp_path):
    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": site.url, "rate_limit_rps": 0},
            "collections": [
                {
                    "name": "locations",
                    "discover": {"crawl": {"start": "/", "follow": "/locations/", "max_depth": 1}},
                    "extract": {
                        "type": "html",
                        "fields": [
                            {"to": "url", "source": "url"},
                            {"to": "title", "selector": "h1"},
                        ],
                    },
                }
            ],
        }
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        harvest(config, staging, cache_dir=tmp_path / "c")
        rows = staging.sample("locations", 10)

    assert {r["title"] for r in rows} == {"Springfield", "Shelbyville"}


def test_harvest_records_extraction_failures_without_stopping(site, tmp_path):
    config = HarvestConfig.model_validate(
        {
            "site": {"base_url": site.url, "rate_limit_rps": 0},
            "collections": [
                {
                    "name": "services",
                    "discover": {"sitemap": {"match": "/services/"}},
                    "extract": {
                        "type": "html",
                        "fields": [
                            {"to": "url", "source": "url"},
                            {"to": "nope", "selector": ".does-not-exist", "required": True},
                        ],
                    },
                }
            ],
        }
    )
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(config, staging, cache_dir=tmp_path / "c")

    result = report.collections[0]
    assert result.discovered == 3 and result.failed == 3 and result.stored == 0
    assert "matched nothing" in result.errors[0]["error"]


def test_limit_caps_the_harvest(site, tmp_path):
    with Staging(tmp_path / "s.sqlite") as staging:
        report = harvest(_api_config(site), staging, cache_dir=tmp_path / "c", limit=2)
    assert report.total_stored == 2


# --- staging ----------------------------------------------------------------


def test_staging_rejects_unsafe_identifiers(tmp_path):
    with Staging(tmp_path / "s.sqlite") as staging:
        with pytest.raises(ValueError, match="invalid collection name"):
            staging.ensure_table("services; DROP TABLE x", ["a"])
        with pytest.raises(ValueError, match="invalid field name"):
            staging.ensure_table("services", ['a" , (SELECT 1)--'])


def test_staging_adds_columns_when_the_config_grows(tmp_path):
    with Staging(tmp_path / "s.sqlite") as staging:
        staging.ensure_table("things", ["a"])
        staging.insert("things", ["a"], [{"a": "1", "_key": "k1", "_url": "u", "_fetched_at": "t"}])
        staging.ensure_table("things", ["a", "b"])
        staging.insert(
            "things", ["a", "b"], [{"a": "2", "b": "x", "_key": "k2", "_url": "u", "_fetched_at": "t"}]
        )
        rows = staging.sample("things", 10)

    assert len(rows) == 2
    assert rows[0]["b"] is None and rows[1]["b"] == "x"


def test_scaffold_uses_the_upstream_id_not_the_scraper_row_number(site, tmp_path):
    """`_id` is a row number the harvester assigned; the real identity is `id`.

    Mapping `_id` to legacy_id would silently record meaningless lineage.
    """
    from eag_migrator.db import build_engine
    from eag_migrator.discovery import profile_database
    from eag_migrator.scaffold import build_draft

    staging_path = tmp_path / "staging.sqlite"
    with Staging(staging_path) as staging:
        harvest(_api_config(site), staging, cache_dir=tmp_path / "c")

    v3_path = tmp_path / "v3.sqlite"
    import sqlite3

    sqlite3.connect(v3_path).executescript(
        "CREATE TABLE services (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " legacy_id INTEGER, slug TEXT, title TEXT, source_url TEXT);"
    )

    v2 = profile_database(build_engine(f"sqlite:///{staging_path}"), "v2", sample_rows=0)
    v3 = profile_database(build_engine(f"sqlite:///{v3_path}"), "v3", sample_rows=0)
    mapping, _ = build_draft(v2, v3)

    service = mapping.get("service")
    legacy = next(f for f in service.fields if f.to == "legacy_id")
    assert legacy.from_ == "id"

    # Paging still uses the staging key, and nothing lands on v3's generated id.
    assert service.source.key == "_id"
    assert all(f.to != "id" for f in service.fields)
    assert not any(f.from_ == "_id" for f in service.fields)


def test_staging_is_a_usable_v2_database(site, tmp_path):
    """The whole point: harvested output plugs into the existing SQL pipeline."""
    from eag_migrator.db import build_engine
    from eag_migrator.discovery import profile_database

    path = tmp_path / "staging.sqlite"
    with Staging(path) as staging:
        harvest(_api_config(site), staging, cache_dir=tmp_path / "c")

    profile = profile_database(build_engine(f"sqlite:///{path}"), "v2", sample_rows=2)
    table = profile.table("services")

    assert table is not None
    assert table.row_count == 3
    assert table.primary_key == ["_id"]  # pageable and resumable
    assert {c.name for c in table.columns} >= {"_id", "_key", "_url", "title", "nags_prefix"}
    assert "nags" in table.domain_hits  # the auto-glass detector still fires
