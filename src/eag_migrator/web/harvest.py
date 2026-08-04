"""Harvest: pull the v2 site into a local staging database.

The staging database is the whole point. Once the site is in SQLite, it *is* a
v2 database, and everything already built — mapping, transforms, plan, run,
verify, rollback — works on it unchanged:

    eagm harvest                                   # site  -> state/staging.sqlite
    V2_DATABASE_URL=sqlite:////app/state/staging.sqlite
    eagm discover --side v2 && eagm scaffold       # then the normal pipeline

It also means the site gets read once. Every re-run of the mapping works off
local data, not more traffic to someone else's server.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import urljoin, urlparse

import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import expand_env
from .extract import (
    ExtractError,
    extract_html,
    extract_json,
    flatten,
    links_from,
    select_rows,
)
from .fetcher import DEFAULT_UA, Fetcher
from .safety import ScrubReport, scrub
from .session import Session
from .staging import Staging


class AuthExpired(RuntimeError):
    """The session stopped being valid part-way through a harvest."""


# --- configuration ----------------------------------------------------------


class FieldSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to: str
    selector: str | None = None
    """CSS selector, for `type: html`."""
    path: str | None = None
    """Dotted path, for `type: json` or `source: jsonld`."""
    attr: str = "text"
    """text | html | any HTML attribute name (href, src, content, ...)."""
    many: bool = False
    source: Literal["selector", "url", "jsonld", "page"] = "selector"
    """Where the selector is evaluated.

    `selector` means relative to the row when `rows:` is set. `page` means the
    whole document even inside a row — which is how a child record on a detail
    page reaches the id of its parent, since that id is not inside the row.
    """
    const: Any = None
    required: bool = False
    note: str | None = None


class SitemapDiscovery(BaseModel):
    match: str
    """Regex tested against each sitemap URL."""
    limit: int | None = None


class ApiDiscovery(BaseModel):
    url: str
    style: Literal["page", "offset", "cursor", "single"] = "page"
    """How the endpoint paginates. `single` = one request, no pagination."""
    method: Literal["GET", "POST"] = "GET"
    body: dict[str, Any] | None = None
    """Request body for POST endpoints (GraphQL, search APIs)."""

    per_page: int = 100
    page_param: str = "page"
    per_page_param: str = "per_page"
    offset_param: str = "offset"
    cursor_param: str = "cursor"
    cursor_path: str | None = None
    """Where the next cursor lives in the response, e.g. 'meta.next_cursor'."""

    max_pages: int = 200
    record_path: str = ""
    """Dotted path to the list inside the payload; blank if the payload is the list."""


class CrawlDiscovery(BaseModel):
    start: str
    follow: str | None = None
    """Regex: which links to enqueue."""
    keep: str | None = None
    """Regex: which URLs become records. Defaults to `follow`."""
    max_depth: int = 3
    limit: int = 500


class UrlDiscovery(BaseModel):
    urls: list[str]


class SequenceDiscovery(BaseModel):
    """Walk a counter in the URL: offset pages, page numbers, or record ids.

    For screens with no link to follow — `/customer/0`, `/customer/25`, … —
    and for enumerating detail pages by id when the list screen does not show
    every record. Ids are sparse, so it stops after a run of misses rather
    than at the first one.
    """

    url: str
    """Must contain {n}, e.g. '/customer/{n}' or '/order?currentPage={n}'."""
    start: int = 1
    stop: int | None = None
    """Last value, inclusive. Leave unset to run until the misses add up."""
    step: int = 1
    stop_after_misses: int = 25
    """Consecutive URLs that 404 or hold no records before giving up."""

    @model_validator(mode="after")
    def _has_placeholder(self) -> SequenceDiscovery:
        if "{n}" not in self.url:
            raise ValueError(f"sequence url must contain {{n}}: {self.url!r}")
        if self.step == 0:
            raise ValueError("sequence step cannot be 0")
        return self


DISCOVERY_KINDS = ("sitemap", "api", "crawl", "static", "sequence")


class Discovery(BaseModel):
    sitemap: SitemapDiscovery | None = None
    api: ApiDiscovery | None = None
    crawl: CrawlDiscovery | None = None
    static: UrlDiscovery | None = None
    sequence: SequenceDiscovery | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Discovery:
        chosen = [n for n in DISCOVERY_KINDS if getattr(self, n)]
        if len(chosen) != 1:
            raise ValueError(
                "discovery needs exactly one of: " + ", ".join(DISCOVERY_KINDS)
                + f" (got {chosen or 'none'})"
            )
        return self


class Extract(BaseModel):
    type: Literal["html", "json"] = "html"
    rows: str | None = None
    """CSS selector for the element a list screen repeats — a table row, a card.

    Without it a page yields one record, which is right for a detail page and
    useless for a list of 50 customers. With it, field selectors are read
    relative to each matching element.
    """
    fields: list[FieldSpec]
    require: list[str] = Field(default_factory=list)
    """Field names that must hold a value, or the record is not one.

    Some apps answer a URL for a record that does not exist with HTTP 200 and
    a blank form rather than a 404 — so status code cannot tell you whether
    there is a record there, and walking ids would store thousands of empty
    shells. Name the field that is only ever filled on a real record, and a
    page without it is skipped and counted as a miss.
    """

    @model_validator(mode="after")
    def _require_names_exist(self) -> Extract:
        known = {f.to for f in self.fields}
        unknown = [n for n in self.require if n not in known]
        if unknown:
            raise ValueError(
                f"require names a field that is not extracted: {', '.join(unknown)} "
                f"(fields are {', '.join(sorted(known)) or 'none'})"
            )
        return self


class Collection(BaseModel):
    name: str
    enabled: bool = True
    discover: Discovery
    extract: Extract
    key: str | None = None
    """Field that uniquely identifies a record. Defaults to the URL."""
    note: str | None = None


class Site(BaseModel):
    base_url: str
    rate_limit_rps: float = 1.0
    respect_robots: bool = True
    requires_auth: bool = False
    """Set by recon when the app is behind a login. Harvest refuses without
    a session rather than silently storing a wall of login pages."""
    user_agent: str = DEFAULT_UA
    max_pages: int = 2000


class HarvestConfig(BaseModel):
    version: int = 1
    site: Site
    collections: list[Collection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> HarvestConfig:
        seen: set[str] = set()
        for c in self.collections:
            if c.name in seen:
                raise ValueError(f"duplicate collection name: {c.name}")
            seen.add(c.name)
        return self

    def active(self) -> list[Collection]:
        return [c for c in self.collections if c.enabled]


def load_config(path: Path) -> HarvestConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"No harvest config at {path}. Run `eagm recon <url>` first — it writes "
            f"a starting config based on what it finds."
        )
    raw = expand_env(path.read_text(encoding="utf-8"))
    return HarvestConfig.model_validate(yaml.safe_load(raw) or {})


_FIELD_DEFAULTS = {"attr": "text", "many": False, "source": "selector", "required": False}


def dump_config(config: HarvestConfig, path: Path) -> Path:
    """Write the config, omitting field-level defaults.

    A draft with 60 collections is unreadable if every field spells out
    `attr: text`, `many: false`, `source: selector`, `required: false`.
    """
    data = config.model_dump(by_alias=True, exclude_none=True)
    for collection in data.get("collections", []):
        if collection.get("enabled") is True:
            collection.pop("enabled")
        for spec in collection.get("extract", {}).get("fields", []):
            for key, default in _FIELD_DEFAULTS.items():
                if spec.get(key) == default:
                    spec.pop(key, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return path


# --- results ----------------------------------------------------------------


@dataclass
class CollectionResult:
    name: str
    discovered: int = 0
    fetched: int = 0
    stored: int = 0
    failed: int = 0
    from_cache: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    scrubbed: ScrubReport = field(default_factory=ScrubReport)


@dataclass
class HarvestReport:
    base_url: str
    started_at: str
    finished_at: str | None = None
    staging_path: str = ""
    collections: list[CollectionResult] = field(default_factory=list)
    requests_made: int = 0
    cache_hits: int = 0
    robots_blocked: int = 0
    authenticated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def total_stored(self) -> int:
        return sum(c.stored for c in self.collections)

    @property
    def total_failed(self) -> int:
        return sum(c.failed for c in self.collections)


# --- discovery --------------------------------------------------------------


def _sitemap_urls(fetcher: Fetcher, limit: int) -> list[str]:
    from .recon import _parse_sitemap  # local import: shared parser, avoids a cycle

    declared = fetcher.sitemaps() or [
        urljoin(fetcher.base_url + "/", "sitemap.xml"),
        urljoin(fetcher.base_url + "/", "sitemap_index.xml"),
    ]
    urls: list[str] = []
    queue, seen = list(declared), set()
    while queue and len(urls) < limit and len(seen) < 60:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        resp = fetcher.get(sm)
        if not resp.ok:
            continue
        pages, nested = _parse_sitemap(resp.text)
        urls.extend(pages)
        queue.extend(nested)
    return urls


def _sequence_urls(fetcher: Fetcher, spec: SequenceDiscovery, cap: int) -> list[str]:
    """Every URL the counter produces, bounded. Misses are handled while fetching."""
    urls: list[str] = []
    n = spec.start
    while len(urls) < cap:
        if spec.stop is not None and (n > spec.stop if spec.step > 0 else n < spec.stop):
            break
        urls.append(urljoin(fetcher.base_url + "/", spec.url.format(n=n).lstrip("/")))
        n += spec.step
    return urls


def _crawl_urls(
    fetcher: Fetcher, spec: CrawlDiscovery, cap: int, refused: dict[str, str] | None = None
) -> list[str]:
    start = urljoin(fetcher.base_url + "/", spec.start.lstrip("/"))
    keep = re.compile(spec.keep or spec.follow) if (spec.keep or spec.follow) else None

    seen: set[str] = set()
    kept: list[str] = []
    queue: list[tuple[str, int]] = [(start, 0)]
    # A crawl runs signed in. Delete, refund, send and logout links are
    # recorded and left alone rather than followed.
    skipped: dict[str, str] = {}

    while queue and len(kept) < min(spec.limit, cap):
        url, depth = queue.pop(0)
        if url in seen or depth > spec.max_depth:
            continue
        seen.add(url)

        resp = fetcher.get(url)
        if not resp.ok:
            continue
        if keep is None or keep.search(url):
            kept.append(url)
        if depth < spec.max_depth:
            links, refused_here = links_from(resp.text, url, spec.follow)
            for link, why in refused_here:
                if link not in skipped:
                    skipped[link] = why
            for link in links:
                if urlparse(link).netloc == urlparse(fetcher.origin).netloc and link not in seen:
                    queue.append((link, depth + 1))
    if refused is not None:
        refused.update(skipped)
    return kept


def _api_records(
    fetcher: Fetcher,
    spec: ApiDiscovery,
    cap: int,
    on_page: Any = None,
) -> Iterator[tuple[str, Any]]:
    """Page through a JSON endpoint, yielding (source_url, record).

    Handles the four shapes an application API realistically uses: page
    numbers, offset/limit, opaque cursors, and endpoints that just return
    everything at once.
    """
    from .extract import _resolve_path

    emitted = 0
    cursor: Any = None

    for index in range(spec.max_pages):
        params: list[str] = []
        body = dict(spec.body) if spec.body else None

        if spec.style == "page":
            params = [f"{spec.per_page_param}={spec.per_page}",
                      f"{spec.page_param}={index + 1}"]
        elif spec.style == "offset":
            params = [f"{spec.per_page_param}={spec.per_page}",
                      f"{spec.offset_param}={index * spec.per_page}"]
        elif spec.style == "cursor":
            params = [f"{spec.per_page_param}={spec.per_page}"]
            if cursor:
                params.append(f"{spec.cursor_param}={cursor}")

        if spec.method == "POST" and body is not None and spec.style != "single":
            # Pagination for POST endpoints goes in the body, not the query.
            if spec.style == "page":
                body[spec.page_param] = index + 1
                body[spec.per_page_param] = spec.per_page
            elif spec.style == "offset":
                body[spec.offset_param] = index * spec.per_page
                body[spec.per_page_param] = spec.per_page
            elif spec.style == "cursor" and cursor:
                body[spec.cursor_param] = cursor
            params = []

        url = spec.url
        if params:
            url += ("&" if "?" in url else "?") + "&".join(params)

        resp = (
            fetcher.post_json(url, body or {})
            if spec.method == "POST"
            else fetcher.get(url)
        )

        if resp.status in (401, 403):
            raise AuthExpired(
                f"{resp.url} returned HTTP {resp.status}. The session is not valid "
                f"for this endpoint — re-run `eagm login`."
            )
        if not resp.ok or not resp.is_json:
            return
        try:
            payload = resp.json()
        except ValueError:
            return

        records = _resolve_path(payload, spec.record_path) if spec.record_path else payload
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list) or not records:
            return

        if on_page:
            on_page(index + 1, len(records), resp.url, resp.from_cache)

        for record in records:
            yield resp.url, record
            emitted += 1
            if emitted >= cap:
                return

        if spec.style == "single":
            return
        if spec.style == "cursor":
            cursor = _resolve_path(payload, spec.cursor_path) if spec.cursor_path else None
            if not cursor:
                return
        elif len(records) < spec.per_page:
            return


# --- the harvester ----------------------------------------------------------


def harvest(
    config: HarvestConfig,
    staging: Staging,
    *,
    cache_dir: Path | None = None,
    use_cache: bool = True,
    collections: list[str] | None = None,
    limit: int = 0,
    progress: Any = None,
    session: Session | None = None,
) -> HarvestReport:
    report = HarvestReport(
        base_url=config.site.base_url,
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        staging_path=str(staging.path),
    )
    say = progress or (lambda *_: None)

    with Fetcher(
        config.site.base_url,
        cache_dir=cache_dir,
        user_agent=config.site.user_agent,
        rate_limit_rps=config.site.rate_limit_rps,
        respect_robots=config.site.respect_robots,
        use_cache=use_cache,
        session=session,
    ) as fetcher:
        if config.site.requires_auth and not fetcher.authenticated:
            raise AuthExpired(
                "this app requires a login and no session was supplied. Run "
                "`eagm login` first, or set site.requires_auth: false if the "
                "content really is public."
            )
        if fetcher.authenticated and fetcher.robots_blocks_base():
            report.notes.append(
                "robots.txt disallows this path. That rule is aimed at search "
                "crawlers, not an authenticated user exporting their own records "
                "— but it is the operator's stated preference. Proceeding because "
                "respect_robots is off."
            )

        wanted = set(collections) if collections else None
        for collection in config.active():
            if wanted and collection.name not in wanted:
                continue
            result = _harvest_collection(
                fetcher, collection, staging, config.site.max_pages, limit, say
            )
            report.collections.append(result)

        report.requests_made = fetcher.stats.requests
        report.cache_hits = fetcher.stats.cache_hits
        report.robots_blocked = len(fetcher.stats.blocked_by_robots)
        report.authenticated = fetcher.authenticated

    report.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return report


def _harvest_collection(
    fetcher: Fetcher,
    collection: Collection,
    staging: Staging,
    max_pages: int,
    limit: int,
    say: Any,
) -> CollectionResult:
    result = CollectionResult(name=collection.name)
    if collection.note:
        result.notes.append(collection.note)

    cap = limit or max_pages
    columns = [f.to for f in collection.extract.fields]
    staging.ensure_table(collection.name, columns)
    rows: list[dict[str, Any]] = []

    disc = collection.discover

    # --- JSON API: no HTML parsing at all ----------------------------------
    if disc.api:
        def page_note(page: int, count: int, url: str, cached: bool) -> None:
            say(
                collection.name,
                result.fetched + count,
                0,
                f"page {page}: {count} record(s)" + (" (cached)" if cached else ""),
            )

        for source_url, record in _api_records(fetcher, disc.api, cap, page_note):
            result.discovered += 1
            result.fetched += 1
            try:
                extracted = extract_json(record, collection.extract.fields, url=source_url)
            except ExtractError as exc:
                result.failed += 1
                if len(result.errors) < 50:
                    result.errors.append({"url": source_url, "error": str(exc)})
                continue
            rows.append(_finalise(extracted, collection, source_url, result.scrubbed))
            if len(result.samples) < 3:
                result.samples.append(dict(rows[-1]))
        say(collection.name, result.fetched, result.discovered)
        result.stored = staging.insert(collection.name, columns, rows)
        return result

    # --- URL-based: sitemap, crawl or a fixed list -------------------------
    if disc.sitemap:
        pattern = re.compile(disc.sitemap.match)
        urls = [u for u in _sitemap_urls(fetcher, max_pages) if pattern.search(u)]
        if disc.sitemap.limit:
            urls = urls[: disc.sitemap.limit]
        if not urls:
            result.notes.append(
                f"sitemap matched nothing for /{disc.sitemap.match}/ — check the "
                f"pattern against the URL shapes in the recon report"
            )
    elif disc.crawl:
        refused: dict[str, str] = {}
        urls = _crawl_urls(fetcher, disc.crawl, cap, refused)
        if refused:
            result.notes.append(
                f"{len(refused)} link(s) not followed (state changes, logout): "
                + ", ".join(list(refused)[:5])
            )
    elif disc.sequence:
        urls = _sequence_urls(fetcher, disc.sequence, cap)
    else:
        urls = [
            urljoin(fetcher.base_url + "/", u.lstrip("/")) if not u.startswith("http") else u
            for u in (disc.static.urls if disc.static else [])
        ]

    urls = urls[:cap]
    result.discovered = len(urls)

    # A counter walks past the end of the data — and over gaps in it, because
    # ids are sparse wherever records have ever been deleted. A run of misses
    # is the end; a single one is a hole.
    miss_budget = disc.sequence.stop_after_misses if disc.sequence else 0
    misses = 0

    for i, url in enumerate(urls, 1):
        say(collection.name, i, len(urls), url)
        resp = fetcher.get(url)
        if resp.from_cache:
            result.from_cache += 1
        if not resp.ok:
            result.failed += 1
            if len(result.errors) < 50:
                result.errors.append({"url": url, "error": f"HTTP {resp.status}"})
            if miss_budget:
                misses += 1
                if misses >= miss_budget:
                    result.discovered = i
                    result.notes.append(
                        f"stopped at {url} after {misses} in a row with nothing on "
                        f"them — raise stop_after_misses if the gap was real"
                    )
                    break
            continue
        result.fetched += 1

        try:
            if collection.extract.type == "json":
                found = [extract_json(resp.json(), collection.extract.fields, url=url)]
            elif collection.extract.rows:
                soup = BeautifulSoup(resp.text, "lxml")
                elements = select_rows(resp.text, collection.extract.rows, soup)
                # Walking a counter, an empty page is the expected way to find
                # the end. Only flag it when every page was supposed to have rows.
                if not elements and not miss_budget and len(result.notes) < 5:
                    result.notes.append(
                        f"rows selector {collection.extract.rows!r} matched nothing on "
                        f"{url} — check it against the real markup"
                    )
                found = [
                    extract_html(
                        resp.text, url, collection.extract.fields, soup=soup, node=element
                    )
                    for element in elements
                ]
            else:
                found = [
                    extract_html(
                        resp.text, url, collection.extract.fields,
                        soup=BeautifulSoup(resp.text, "lxml"),
                    )
                ]
        except (ExtractError, ValueError) as exc:
            result.failed += 1
            if len(result.errors) < 50:
                result.errors.append({"url": url, "error": str(exc)})
            continue

        if collection.extract.require:
            found = [
                record
                for record in found
                if all(
                    record.get(name) not in (None, "", [], {})
                    for name in collection.extract.require
                )
            ]

        if miss_budget:
            if found:
                misses = 0
            else:
                misses += 1
                if misses >= miss_budget:
                    result.discovered = i
                    result.notes.append(
                        f"stopped at {url} after {misses} in a row with nothing on "
                        f"them — raise stop_after_misses if the gap was real"
                    )
                    break

        for position, extracted in enumerate(found):
            rows.append(
                _finalise(extracted, collection, url, result.scrubbed, position)
            )
            if len(result.samples) < 3:
                result.samples.append(dict(rows[-1]))

    say(collection.name, result.fetched, result.discovered)
    result.stored = staging.insert(collection.name, columns, rows)
    return result


CANONICAL_URL_FIELDS = ("link", "permalink", "url", "guid", "canonical_url")


def _finalise(
    extracted: dict[str, Any],
    collection: Collection,
    url: str,
    report: ScrubReport | None = None,
    position: int = 0,
) -> dict[str, Any]:
    # Cardholder data is removed here, before anything is written, so no
    # mapping or selector mistake downstream can put it on disk.
    extracted, scrubbed = scrub(extracted)
    if report is not None:
        report.merge(scrubbed)

    row = {k: flatten(v) for k, v in extracted.items()}

    # `_url` should be the page this record represents, not where we happened to
    # fetch it. For an API collection the fetch URL is a paginated endpoint —
    # useless for the redirect map, which is half the point of keeping the URL.
    canonical = url
    for name in CANONICAL_URL_FIELDS:
        value = row.get(name)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            canonical = value
            break

    key = row.get(collection.key) if collection.key else None
    row["_url"] = canonical
    # Many rows can share a URL, so the position disambiguates them. It is a
    # weak key — re-harvest after the list reorders and rows shuffle — so a
    # real `key:` field matters far more for row-wise collections.
    row["_key"] = (
        str(key)
        if key not in (None, "")
        else (f"{canonical}#{position}" if position or collection.extract.rows else canonical)
    )
    row["_fetched_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return row


def render_markdown(report: HarvestReport) -> str:
    lines = [f"# Harvest — {report.base_url}\n"]
    lines.append(f"- Started: {report.started_at}")
    lines.append(f"- Finished: {report.finished_at or '(incomplete)'}")
    lines.append(f"- Staging database: `{report.staging_path}`")
    lines.append(f"- Records stored: **{report.total_stored:,}**")
    lines.append(f"- Requests made: {report.requests_made:,} "
                 f"(+{report.cache_hits:,} served from cache)")
    if report.robots_blocked:
        lines.append(f"- Skipped by robots.txt: {report.robots_blocked}")
    lines.append("")

    lines.append("| Collection | Discovered | Fetched | Stored | Failed |")
    lines.append("|---|---:|---:|---:|---:|")
    for c in report.collections:
        lines.append(
            f"| {c.name} | {c.discovered:,} | {c.fetched:,} | {c.stored:,} | {c.failed:,} |"
        )
    lines.append("")

    scrubbed = [c for c in report.collections if not c.scrubbed.clean]
    if scrubbed:
        lines.append("## Cardholder data removed\n")
        lines.append(
            "Blocked at the staging layer, before anything was written. This is a "
            "safety net, not a compliance programme.\n"
        )
        for c in scrubbed:
            lines.append(f"- **{c.name}**")
            for item in c.scrubbed.summary():
                lines.append(f"  - {item}")
        lines.append("")

    pii = {n for c in report.collections for n in c.scrubbed.pii_fields}
    if pii:
        lines.append("## Personal data harvested\n")
        lines.append(
            "For your processing record / DPA. These fields were stored:\n"
        )
        lines.append("| Collection | Fields |")
        lines.append("|---|---|")
        for c in report.collections:
            if c.scrubbed.pii_fields:
                lines.append(
                    f"| {c.name} | `" + "`, `".join(sorted(c.scrubbed.pii_fields)) + "` |"
                )
        lines.append("")

    for c in report.collections:
        if c.notes:
            lines.append(f"## {c.name} — notes\n")
            for note in c.notes:
                lines.append(f"- {note}")
            lines.append("")
        if c.errors:
            lines.append(f"## {c.name} — failures ({c.failed:,} total)\n")
            lines.append("| URL | Error |")
            lines.append("|---|---|")
            for err in c.errors[:50]:
                lines.append(f"| {err['url']} | {err['error']} |")
            lines.append("")

    lines.append("## Next step\n")
    lines.append("The staging database is now a v2 database. Point the migrator at it:\n")
    lines.append("```bash")
    lines.append(f"export V2_DATABASE_URL=sqlite:///{report.staging_path}")
    lines.append("eagm discover --side v2")
    lines.append("eagm scaffold")
    lines.append("```")
    return "\n".join(lines)
