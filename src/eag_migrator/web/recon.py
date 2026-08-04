"""Reconnaissance: work out what the v2 site is and where its data really lives.

The same idea as database discovery, pointed at HTTP instead. Before writing a
single selector, find out:

  * what platform built the site (WordPress, Shopify, Wix, Next.js, ...)
  * whether it already exposes a JSON API we can read instead of scraping HTML
  * what URLs exist, from robots.txt and sitemaps
  * what structured data (JSON-LD) is already sitting in the markup

Scraping rendered HTML is the last resort, not the first. A WordPress site
publishes its entire content model at /wp-json/wp/v2 by default; a Shopify
store publishes /products.json. Finding that turns a fragile scrape into a
clean, paginated, typed export.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .fetcher import Fetcher

# --- platform fingerprints --------------------------------------------------
# (label, kind, pattern) where kind is html | header | cookie
SIGNATURES: list[tuple[str, str, str]] = [
    ("WordPress", "html", r"/wp-content/|/wp-includes/|wp-json"),
    ("WordPress", "header", r"wp-json"),
    ("WooCommerce", "html", r"woocommerce|wc-ajax"),
    ("Elementor", "html", r"elementor"),
    ("Divi", "html", r"et_pb_|divi"),
    ("Shopify", "html", r"cdn\.shopify\.com|/cdn/shop/|Shopify\.theme"),
    ("Shopify", "header", r"shopify"),
    ("Wix", "html", r"static\.wixstatic\.com|wix-code|_wixCssStates"),
    ("Wix", "header", r"x-wix-"),
    ("Squarespace", "html", r"squarespace\.com|static1\.squarespace"),
    ("Squarespace", "header", r"squarespace"),
    ("Webflow", "html", r"assets\.website-files\.com|uploads-ssl\.webflow\.com|data-wf-page"),
    ("Duda", "html", r"/_dm/|dudamobile|dudaone"),
    ("GoDaddy Website Builder", "html", r"img1\.wsimg\.com|godaddy"),
    ("Joomla", "html", r"/media/jui/|joomla"),
    ("Drupal", "html", r"/sites/default/files/|drupal-settings-json"),
    ("Drupal", "header", r"drupal"),
    ("Magento", "html", r"/static/version|mage/|Magento_"),
    ("BigCommerce", "html", r"cdn\d*\.bigcommerce\.com"),
    ("HubSpot CMS", "html", r"hs-scripts\.com|hubspot"),
    ("Next.js", "html", r"__NEXT_DATA__|/_next/static"),
    ("Nuxt", "html", r"__NUXT__|/_nuxt/"),
    ("Gatsby", "html", r"___gatsby|page-data\.json"),
    ("React (SPA)", "html", r"react(-dom)?(\.production)?\.min\.js|data-reactroot"),
    ("Angular", "html", r"ng-version="),
    ("Vue", "html", r"data-v-[0-9a-f]{8}"),
    ("Cloudflare", "header", r"cloudflare"),
]

# Endpoints worth probing blind. Cheap, and the payoff is a real API.
API_PROBES: list[tuple[str, str]] = [
    ("/wp-json/", "WordPress REST API root"),
    ("/wp-json/wp/v2/types", "WordPress content types"),
    ("/?rest_route=/", "WordPress REST API (no pretty permalinks)"),
    ("/products.json", "Shopify products feed"),
    ("/collections/all/products.json", "Shopify collection feed"),
    ("/feed/", "RSS feed"),
    ("/rss.xml", "RSS feed"),
    ("/graphql", "GraphQL endpoint"),
    ("/api", "generic API root"),
    ("/api/v1", "generic API root"),
    ("/sitemap.xml", "sitemap"),
    ("/sitemap_index.xml", "sitemap index"),
]

# JSON-LD types that carry real business data for a glass shop.
VALUABLE_LD_TYPES = {
    "LocalBusiness", "AutoRepair", "AutoGlass", "AutomotiveBusiness", "Organization",
    "Service", "Product", "Offer", "Review", "AggregateRating", "FAQPage",
    "Question", "Place", "PostalAddress", "OpeningHoursSpecification", "Person",
    "Article", "BlogPosting", "WebPage",
}


@dataclass
class Endpoint:
    url: str
    label: str
    status: int
    content_type: str
    bytes: int = 0
    note: str | None = None
    sample_keys: list[str] = field(default_factory=list)
    item_count: int | None = None
    total_available: int | None = None

    @property
    def usable(self) -> bool:
        return 200 <= self.status < 300 and self.bytes > 0


@dataclass
class ContentType:
    """A WordPress post type / Shopify resource — a content collection."""

    name: str
    label: str
    rest_url: str
    total: int | None = None
    accessible: bool = False
    fields: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class SiteProfile:
    base_url: str
    generated_at: str
    final_url: str = ""
    status: int = 0
    platforms: list[dict[str, Any]] = field(default_factory=list)
    generator: str | None = None
    endpoints: list[Endpoint] = field(default_factory=list)
    content_types: list[ContentType] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    url_groups: list[dict[str, Any]] = field(default_factory=list)
    jsonld_types: dict[str, int] = field(default_factory=dict)
    jsonld_samples: list[dict[str, Any]] = field(default_factory=list)
    embedded_state: list[str] = field(default_factory=list)
    robots_blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best_api(self) -> Endpoint | None:
        usable = [e for e in self.endpoints if e.usable and "json" in e.content_type]
        return usable[0] if usable else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- helpers ----------------------------------------------------------------


def _detect_platforms(html: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    header_blob = " ".join(f"{k}: {v}" for k, v in headers.items()).lower()
    hits: dict[str, list[str]] = {}
    for label, kind, pattern in SIGNATURES:
        haystack = html if kind == "html" else header_blob
        match = re.search(pattern, haystack, re.IGNORECASE)
        if match:
            hits.setdefault(label, []).append(match.group(0)[:60])
    return [
        {"platform": label, "evidence": sorted(set(ev))[:3]}
        for label, ev in sorted(hits.items())
    ]


def _url_groups(urls: list[str], limit: int = 40) -> list[dict[str, Any]]:
    """Collapse URLs into path patterns so a 4,000-URL sitemap is readable."""
    counter: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for url in urls:
        path = urlparse(url).path.strip("/")
        parts = path.split("/") if path else []
        if not parts:
            key = "/"
        elif len(parts) == 1:
            key = "/<page>"
        else:
            # Keep the first segment, generalise the rest.
            key = "/" + parts[0] + "/<...>" * (len(parts) - 1)
        counter[key] += 1
        examples.setdefault(key, url)
    return [
        {"pattern": key, "count": count, "example": examples[key]}
        for key, count in counter.most_common(limit)
    ]


def _parse_sitemap(text: str) -> tuple[list[str], list[str]]:
    """Returns (page urls, nested sitemap urls)."""
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return [], []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    pages = [e.text.strip() for e in root.findall(".//sm:url/sm:loc", ns) if e.text]
    nested = [e.text.strip() for e in root.findall(".//sm:sitemap/sm:loc", ns) if e.text]
    if not pages and not nested:  # some sitemaps omit the namespace
        pages = [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]
    return pages, nested


def _extract_jsonld(soup: BeautifulSoup) -> list[Any]:
    out: list[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _walk_ld_types(node: Any, counter: Counter[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        for label in [t] if isinstance(t, str) else (t or []):
            if isinstance(label, str):
                counter[label] += 1
        for value in node.values():
            _walk_ld_types(value, counter)
    elif isinstance(node, list):
        for item in node:
            _walk_ld_types(item, counter)


def _sample_keys(payload: Any, limit: int = 25) -> tuple[list[str], int | None]:
    """Field names and item count from a JSON payload."""
    if isinstance(payload, list):
        first = payload[0] if payload else None
        keys = sorted(first.keys())[:limit] if isinstance(first, dict) else []
        return keys, len(payload)
    if isinstance(payload, dict):
        for wrapper in ("data", "items", "results", "products", "records"):
            inner = payload.get(wrapper)
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                return sorted(inner[0].keys())[:limit], len(inner)
        return sorted(payload.keys())[:limit], None
    return [], None


# --- the main routine -------------------------------------------------------


def recon(
    fetcher: Fetcher,
    *,
    max_urls: int = 3000,
    probe_apis: bool = True,
) -> SiteProfile:
    base = fetcher.base_url
    profile = SiteProfile(
        base_url=base,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )

    home = fetcher.get(base)
    profile.status = home.status
    profile.final_url = home.url
    if not home.ok:
        profile.notes.append(
            f"homepage returned HTTP {home.status} — check the URL, or the site may "
            f"be blocking automated clients"
        )
        return profile

    soup = BeautifulSoup(home.text, "lxml")

    # --- platform -----------------------------------------------------------
    profile.platforms = _detect_platforms(home.text, home.headers)
    gen = soup.find("meta", attrs={"name": "generator"})
    if gen and gen.get("content"):
        profile.generator = gen["content"]

    for marker, label in (
        ("__NEXT_DATA__", "Next.js __NEXT_DATA__ (full page props as JSON)"),
        ("__NUXT__", "Nuxt __NUXT__ state blob"),
        ("window.__INITIAL_STATE__", "__INITIAL_STATE__ redux blob"),
        ("application/json", "inline application/json script blocks"),
    ):
        if marker in home.text:
            profile.embedded_state.append(label)

    # --- structured data ----------------------------------------------------
    counter: Counter[str] = Counter()
    for block in _extract_jsonld(soup):
        _walk_ld_types(block, counter)
        if len(profile.jsonld_samples) < 3:
            profile.jsonld_samples.append(block if isinstance(block, dict) else {"list": block})
    profile.jsonld_types = dict(counter.most_common())
    valuable = [t for t in counter if t in VALUABLE_LD_TYPES]
    if valuable:
        profile.notes.append(
            "JSON-LD on the homepage already carries structured business data: "
            + ", ".join(sorted(valuable))
        )

    # --- robots + sitemaps --------------------------------------------------
    declared = fetcher.sitemaps()
    candidates = declared or [urljoin(base + "/", "sitemap.xml"), urljoin(base + "/", "sitemap_index.xml")]
    seen_sitemaps: set[str] = set()
    urls: list[str] = []
    queue = list(candidates)

    while queue and len(urls) < max_urls and len(seen_sitemaps) < 60:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        resp = fetcher.get(sm_url)
        if not resp.ok or "xml" not in resp.content_type and "<urlset" not in resp.text[:2000]:
            continue
        profile.sitemaps.append(sm_url)
        pages, nested = _parse_sitemap(resp.text)
        urls.extend(pages)
        queue.extend(nested)

    profile.urls = urls[:max_urls]
    profile.url_groups = _url_groups(profile.urls)
    if not profile.sitemaps:
        profile.notes.append(
            "no sitemap found — URL discovery will need a crawl (`eagm harvest` "
            "with a `crawl:` discovery block)"
        )

    # --- API probing --------------------------------------------------------
    if probe_apis:
        for path, label in API_PROBES:
            resp = fetcher.get(urljoin(base + "/", path.lstrip("/")))
            if resp.status in (0, 999) or resp.status >= 400:
                continue
            endpoint = Endpoint(
                url=resp.url,
                label=label,
                status=resp.status,
                content_type=resp.content_type,
                bytes=len(resp.text),
            )
            if resp.is_json:
                try:
                    keys, count = _sample_keys(resp.json())
                    endpoint.sample_keys = keys
                    endpoint.item_count = count
                except ValueError:
                    endpoint.note = "advertised JSON but did not parse"
            profile.endpoints.append(endpoint)

        _enumerate_wordpress(fetcher, profile)
        _enumerate_shopify(fetcher, profile)

    profile.robots_blocked = list(fetcher.stats.blocked_by_robots)
    if profile.robots_blocked:
        profile.notes.append(
            f"{len(profile.robots_blocked)} URL(s) disallowed by robots.txt and skipped"
        )

    if not profile.content_types and not profile.best_api:
        profile.notes.append(
            "no JSON API found — harvesting will have to parse rendered HTML, which "
            "is more fragile. Run `eagm capture` to check for XHR endpoints the "
            "page calls at runtime."
        )

    return profile


def _enumerate_wordpress(fetcher: Fetcher, profile: SiteProfile) -> None:
    """If this is WordPress, its whole content model is readable without auth."""
    types_resp = fetcher.get(urljoin(fetcher.base_url + "/", "wp-json/wp/v2/types"))
    if not types_resp.ok or not types_resp.is_json:
        return
    try:
        types = types_resp.json()
    except ValueError:
        return
    if not isinstance(types, dict):
        return

    profile.notes.append(
        "WordPress REST API is open — every content type below can be exported as "
        "paginated JSON with no scraping at all."
    )

    for name, meta in types.items():
        if not isinstance(meta, dict):
            continue
        rest_base = meta.get("rest_base") or name
        namespace = meta.get("rest_namespace") or "wp/v2"
        url = urljoin(fetcher.base_url + "/", f"wp-json/{namespace}/{rest_base}?per_page=1")
        ct = ContentType(
            name=name,
            label=(meta.get("name") or name),
            rest_url=urljoin(fetcher.base_url + "/", f"wp-json/{namespace}/{rest_base}"),
        )
        probe = fetcher.get(url)
        ct.accessible = probe.ok and probe.is_json
        if probe.ok:
            total = probe.headers.get("x-wp-total")
            if total and total.isdigit():
                ct.total = int(total)
            try:
                payload = probe.json()
                if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    ct.fields = sorted(payload[0].keys())
            except ValueError:
                pass
        elif probe.status in (401, 403):
            ct.note = f"requires authentication (HTTP {probe.status})"
        else:
            ct.note = f"HTTP {probe.status}"
        profile.content_types.append(ct)

    profile.content_types.sort(key=lambda c: -(c.total or 0))


def _enumerate_shopify(fetcher: Fetcher, profile: SiteProfile) -> None:
    resp = fetcher.get(urljoin(fetcher.base_url + "/", "products.json?limit=1"))
    if not resp.ok or not resp.is_json:
        return
    try:
        payload = resp.json()
    except ValueError:
        return
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        return
    profile.content_types.append(
        ContentType(
            name="products",
            label="Shopify products",
            rest_url=urljoin(fetcher.base_url + "/", "products.json"),
            accessible=True,
            fields=sorted(products[0].keys()) if products else [],
            note="paginate with ?limit=250&page=N",
        )
    )
    profile.notes.append("Shopify products.json is open — products can be exported directly.")


# --- reporting --------------------------------------------------------------


def render_markdown(profile: SiteProfile) -> str:
    lines = [f"# EAG v2 site recon — {profile.base_url}\n"]
    lines.append(f"- Final URL: {profile.final_url or '—'}")
    lines.append(f"- HTTP status: {profile.status}")
    lines.append(f"- Generated: {profile.generated_at}")
    lines.append(f"- URLs discovered: **{len(profile.urls):,}**\n")

    lines.append("## Platform\n")
    if profile.generator:
        lines.append(f"- `<meta generator>`: **{profile.generator}**")
    if profile.platforms:
        lines.append("")
        lines.append("| Platform | Evidence |")
        lines.append("|---|---|")
        for p in profile.platforms:
            lines.append(f"| {p['platform']} | `{'`, `'.join(p['evidence'])}` |")
    else:
        lines.append("_No platform fingerprint matched._")
    if profile.embedded_state:
        lines.append("\nEmbedded state blobs (often easier to read than the HTML):\n")
        for blob in profile.embedded_state:
            lines.append(f"- {blob}")
    lines.append("")

    if profile.content_types:
        lines.append("## Content types available as JSON\n")
        lines.append("**This is where the data should come from.** No HTML parsing needed.\n")
        lines.append("| Type | Label | Rows | Accessible | Endpoint |")
        lines.append("|---|---|---:|---|---|")
        for ct in profile.content_types:
            state = "yes" if ct.accessible else (ct.note or "no")
            lines.append(
                f"| `{ct.name}` | {ct.label} | {ct.total if ct.total is not None else '?'} | "
                f"{state} | `{ct.rest_url}` |"
            )
        lines.append("")
        for ct in profile.content_types:
            if ct.fields:
                lines.append(f"### `{ct.name}` fields\n")
                lines.append("`" + "`, `".join(ct.fields) + "`\n")

    if profile.endpoints:
        lines.append("## Endpoints probed\n")
        lines.append("| Endpoint | What | Status | Type | Bytes | Items |")
        lines.append("|---|---|---:|---|---:|---:|")
        for e in profile.endpoints:
            lines.append(
                f"| `{e.url}` | {e.label} | {e.status} | {e.content_type or '—'} | "
                f"{e.bytes:,} | {e.item_count if e.item_count is not None else '—'} |"
            )
        lines.append("")

    if profile.jsonld_types:
        lines.append("## Structured data (JSON-LD) on the homepage\n")
        lines.append("| @type | Occurrences |")
        lines.append("|---|---:|")
        for name, count in profile.jsonld_types.items():
            lines.append(f"| {name} | {count} |")
        lines.append("")

    if profile.url_groups:
        lines.append("## URL shape\n")
        lines.append("| Pattern | Count | Example |")
        lines.append("|---|---:|---|")
        for g in profile.url_groups:
            lines.append(f"| `{g['pattern']}` | {g['count']:,} | {g['example']} |")
        lines.append("")

    if profile.notes:
        lines.append("## Notes\n")
        for note in profile.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)
