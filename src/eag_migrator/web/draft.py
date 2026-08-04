"""Turn a recon profile into a starting harvest config.

Same idea as `eagm scaffold` on the database path: do not make anyone write
selectors from a blank file. If recon found a JSON API, the draft reads from
that. If it only found HTML, the draft sets up generic selectors per URL group
and marks every one of them as needing a look.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

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
