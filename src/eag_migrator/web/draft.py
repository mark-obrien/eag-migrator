"""Turn a recon profile into a starting harvest config.

Same idea as `eagm scaffold` on the database path: do not make anyone write
selectors from a blank file. If recon found a JSON API, the draft reads from
that. If it only found HTML, the draft sets up generic selectors per URL group
and marks every one of them as needing a look.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .harvest import (
    ApiDiscovery,
    Collection,
    Discovery,
    Extract,
    FieldSpec,
    HarvestConfig,
    SitemapDiscovery,
    Site,
)
from .recon import SiteProfile

# WordPress REST fields worth taking by default.
WP_FIELDS = [
    ("id", "id"),
    ("slug", "slug"),
    ("link", "link"),
    ("title", "title.rendered"),
    ("content_html", "content.rendered"),
    ("excerpt_html", "excerpt.rendered"),
    ("status", "status"),
    ("date", "date"),
    ("modified", "modified"),
    ("parent", "parent"),
    ("menu_order", "menu_order"),
    ("featured_media", "featured_media"),
]

# Generic HTML selectors — a starting point, not an answer.
HTML_FIELDS = [
    FieldSpec(to="url", source="url"),
    FieldSpec(to="title", selector="h1", note="TODO: confirm the page title lives in h1"),
    FieldSpec(
        to="meta_description",
        selector='meta[name="description"]',
        attr="content",
    ),
    FieldSpec(
        to="body_html",
        selector="main, article, .entry-content, #content",
        attr="html",
        note="TODO: narrow this to the real content container — the default is a guess",
    ),
    FieldSpec(to="images", selector="main img, article img", attr="src", many=True),
]

# Business data that is usually in JSON-LD and painful to scrape from markup.
JSONLD_FIELDS = [
    FieldSpec(to="ld_name", source="jsonld", path="name"),
    FieldSpec(to="ld_telephone", source="jsonld", path="telephone"),
    FieldSpec(to="ld_street", source="jsonld", path="address.streetAddress"),
    FieldSpec(to="ld_city", source="jsonld", path="address.addressLocality"),
    FieldSpec(to="ld_region", source="jsonld", path="address.addressRegion"),
    FieldSpec(to="ld_postal_code", source="jsonld", path="address.postalCode"),
    FieldSpec(to="ld_hours", source="jsonld", path="openingHoursSpecification"),
    FieldSpec(to="ld_geo", source="jsonld", path="geo"),
]

# WordPress response noise: bookkeeping, not content.
WP_NOISE = {
    "_links", "guid", "template", "class_list", "yoast_head", "yoast_head_json",
    "comment_status", "ping_status", "generated_slug", "permalink_template",
}

SKIP_TYPES = {
    "attachment", "nav_menu_item", "wp_block", "wp_template", "wp_template_part",
    "wp_navigation", "wp_global_styles", "wp_font_family", "wp_font_face",
    "revision", "custom_css", "customize_changeset", "oembed_cache", "user_request",
}


def _ident(value: str) -> str:
    """A staging table name: letters, digits, underscores."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    if not slug:
        slug = "pages"
    if slug[0].isdigit():
        slug = f"c_{slug}"
    return slug


# Endpoints that are plumbing, not business records.
PLUMBING = re.compile(
    r"/(me|session|whoami|auth|token|refresh|csrf|config|settings|feature|flags|"
    r"health|ping|ready|version|telemetry|analytics|track|log|metrics|notification)s?"
    r"(/|$|\?)",
    re.IGNORECASE,
)

# Field names that identify a record, in preference order.
KEY_CANDIDATES = ("id", "uuid", "guid", "_id", "number", "reference", "ref", "code")


def _collection_name(url: str) -> str:
    """Name a collection after the last meaningful path segment."""
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p and not p.isdigit()]
    # Drop API version and namespace noise: /api/v2/quotes -> quotes
    while parts and re.fullmatch(r"api|v\d+|rest|graphql|public|internal", parts[0], re.I):
        parts.pop(0)
    return _ident(parts[-1] if parts else "records")


def _strip_params(url: str, names: set[str]) -> str:
    parsed = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in names]
    query = urlencode(kept)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))


def draft_from_capture(report: Any, base_url: str) -> tuple[HarvestConfig, list[str]]:
    """Build a harvest config from the endpoints the app called itself.

    For an application — scheduling, quoting, payments — this is the real
    export path. The endpoints the UI uses to render its own list screens are
    already paginated, already typed, and already scoped to the account.
    """
    warnings: list[str] = []
    collections: list[Collection] = []
    seen_names: set[str] = set()

    list_calls = [
        c
        for c in report.api_calls
        if c.item_count and c.item_count > 0 and not PLUMBING.search(c.url)
    ]
    detail_calls = [
        c for c in report.api_calls if c.item_count is None and not PLUMBING.search(c.url)
    ]

    for call in list_calls:
        pagination = dict(call.pagination)
        style = pagination.pop("style", None)
        observed = pagination.pop("observed_per_page", None)

        consumed = {
            v.lower()
            for k, v in pagination.items()
            if k.endswith("_param") and isinstance(v, str)
        }
        clean_url = _strip_params(call.url, consumed)

        api_kwargs: dict[str, Any] = {"url": clean_url, "record_path": call.record_path}
        if call.method == "POST":
            api_kwargs["method"] = "POST"
            try:
                body = json.loads(call.request_body or "{}")
            except ValueError:
                body = {}
            # Drop the paging keys — the harvester sets those itself each page.
            api_kwargs["body"] = (
                {k: v for k, v in body.items() if k.lower() not in consumed}
                if isinstance(body, dict)
                else {}
            )

        if style:
            api_kwargs["style"] = style
            api_kwargs.update(pagination)
            api_kwargs["per_page"] = observed or 100
        else:
            api_kwargs["style"] = "page"
            warnings.append(
                f"{clean_url}: no pagination parameters were visible in the request. "
                f"It may return everything at once (set style: single) or paginate "
                f"a way this could not see — check the response for a total count."
            )

        name = _collection_name(call.url)
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)

        available = list(call.response_keys)
        key = next((k for k in KEY_CANDIDATES if k in available), None)
        if not key:
            warnings.append(
                f"{name}: no obvious id field in {available[:8]} — set `key:` so "
                f"re-harvesting updates rows instead of duplicating them."
            )

        fields = [FieldSpec(to=_ident(k), path=k) for k in available]
        nested = [
            f.to
            for f, k in zip(fields, available)
            if isinstance(call.sample, dict) and isinstance(call.sample.get(k), (dict, list))
        ]
        if nested:
            warnings.append(
                f"{name}: {', '.join(nested)} are nested objects, stored as JSON. "
                f"Use a dotted path (e.g. customer.email) to pull out what you need."
            )

        collections.append(
            Collection(
                name=name,
                key=key,
                discover=Discovery(api=ApiDiscovery(**api_kwargs)),
                extract=Extract(type="json", fields=fields),
                note=(
                    f"From a {call.method} the app made itself; the first page held "
                    f"{call.item_count} record(s). Confirm the pagination before a full run."
                ),
            )
        )

    if detail_calls:
        warnings.append(
            "Detail endpoints seen but not drafted (they return one record, so they "
            "need an id from a list collection): "
            + ", ".join(sorted({c.pattern for c in detail_calls}))[:400]
        )

    if not collections:
        warnings.append(
            "No list endpoints were captured. Drive more of the app with repeated "
            "--path options (the customers list, the quotes list, the schedule) so "
            "there is something to record."
        )

    parsed = urlparse(base_url)
    config = HarvestConfig(
        version=1,
        site=Site(
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            rate_limit_rps=1.0,
            respect_robots=False,
            requires_auth=True,
            max_pages=5000,
        ),
        collections=collections,
    )
    return config, warnings


def draft_config(profile: SiteProfile) -> tuple[HarvestConfig, list[str]]:
    """Returns (config, warnings)."""
    warnings: list[str] = []
    collections: list[Collection] = []

    usable_types = [
        ct
        for ct in profile.content_types
        if ct.accessible and ct.name not in SKIP_TYPES and (ct.total is None or ct.total > 0)
    ]

    # --- preferred path: a real JSON API -----------------------------------
    for ct in usable_types:
        available = set(ct.fields)
        fields = [
            FieldSpec(to=to, path=path)
            for to, path in WP_FIELDS
            if not available or path.split(".")[0] in available
        ]

        # Anything the API returns that the standard list does not cover — ACF
        # and custom meta land here, and for a glass shop that is usually the
        # data worth migrating. Dropping it silently would be the worst outcome.
        covered = {path.split(".")[0] for _, path in WP_FIELDS}
        extra = sorted(available - covered - WP_NOISE)
        for name in extra:
            fields.append(
                FieldSpec(
                    to=_ident(name),
                    path=name,
                    note="TODO: custom field — confirm the shape (may be an object or list)",
                )
            )
        if extra:
            warnings.append(
                f"{ct.name}: custom field(s) included as-is: {', '.join(extra)}. "
                f"Nested values may need a deeper path."
            )

        if not fields:
            fields = [FieldSpec(to=_ident(k), path=k) for k in sorted(available)[:20]]

        collections.append(
            Collection(
                name=_ident(ct.name),
                discover=Discovery(
                    api=ApiDiscovery(url=ct.rest_url, per_page=100, record_path="")
                ),
                extract=Extract(type="json", fields=fields),
                key="id",
                note=(
                    f"{ct.label}: {ct.total if ct.total is not None else '?'} records "
                    f"straight from the REST API — no HTML parsing."
                ),
            )
        )

    if collections:
        warnings.append(
            f"{len(collections)} collection(s) read from the JSON API. Confirm the "
            f"field list — custom fields (ACF, meta) are usually NOT in the default "
            f"REST response and may need ?_fields= or a plugin endpoint."
        )

    # --- fallback: HTML per URL group --------------------------------------
    if not collections:
        groups = [
            g
            for g in profile.url_groups
            if g["count"] >= 2 and g["pattern"] not in ("/", "/<page>")
        ][:8]

        if not groups and profile.urls:
            groups = [{"pattern": "/<page>", "count": len(profile.urls), "example": profile.urls[0]}]

        for group in groups:
            segment = group["pattern"].strip("/").split("/")[0] or "pages"
            match = f"^{re.escape('/' + segment)}/" if segment != "<page>" else r"^/[^/]+/?$"
            collections.append(
                Collection(
                    name=_ident(segment),
                    discover=Discovery(sitemap=SitemapDiscovery(match=match)),
                    extract=Extract(type="html", fields=[*HTML_FIELDS, *JSONLD_FIELDS]),
                    note=(
                        f"TODO: {group['count']} URL(s) like {group['example']} — open one, "
                        f"check every selector below against it."
                    ),
                )
            )

        if collections:
            warnings.append(
                "No JSON API was found, so these collections parse HTML. Every "
                "selector is a generic guess — open one page of each collection and "
                "correct them before harvesting the whole site."
            )

    if not collections:
        warnings.append(
            "Nothing to harvest: no JSON API and no URLs discovered. Run "
            "`eagm capture <url>` to see whether the page loads its content over "
            "XHR, and check whether the site is behind a bot wall."
        )

    parsed = urlparse(profile.final_url or profile.base_url)
    config = HarvestConfig(
        version=1,
        site=Site(
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            rate_limit_rps=1.0,
            respect_robots=True,
            max_pages=2000,
        ),
        collections=collections,
    )
    return config, warnings
