"""A fake WordPress auto-glass site, served over real HTTP.

Stands in for the v2 site so recon/harvest are tested against actual traffic —
real robots.txt, real sitemap, real REST pagination with X-WP-Total — rather
than mocked responses. Deliberately not EAG: this is a fictional shop.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SERVICES = [
    {
        "id": 101,
        "slug": "windshield-replacement",
        "title": {"rendered": "Windshield Replacement"},
        "content": {"rendered": "<p>Full <b>windshield</b> replacement, OEM glass.</p>"},
        "excerpt": {"rendered": "<p>OEM windshield replacement.</p>"},
        "status": "publish",
        "date": "2023-02-11T09:00:00",
        "modified": "2024-01-04T11:20:00",
        "parent": 0,
        "menu_order": 1,
        "featured_media": 0,
        "nags_prefix": "DW",
        "base_price": "$349.00",
    },
    {
        "id": 102,
        "slug": "rock-chip-repair",
        "title": {"rendered": "Rock Chip Repair"},
        "content": {"rendered": "<p>Resin injection for chips under an inch.</p>"},
        "excerpt": {"rendered": "<p>Chip repair.</p>"},
        "status": "publish",
        "date": "2023-02-12T09:00:00",
        "modified": "2024-03-19T08:05:00",
        "parent": 0,
        "menu_order": 2,
        "featured_media": 0,
        "nags_prefix": "RC",
        "base_price": "$89.00",
    },
    {
        "id": 103,
        "slug": "adas-calibration",
        "title": {"rendered": "ADAS Calibration"},
        "content": {"rendered": "<p>Static and dynamic recalibration after glass work.</p>"},
        "excerpt": {"rendered": "<p>ADAS recalibration.</p>"},
        "status": "publish",
        "date": "2023-06-01T09:00:00",
        "modified": "2024-05-30T15:45:00",
        "parent": 0,
        "menu_order": 3,
        "featured_media": 0,
        "nags_prefix": "CAL",
        "base_price": "$249.00",
    },
]

LOCATIONS = [
    {
        "id": 201,
        "slug": "springfield",
        "title": {"rendered": "Springfield"},
        "content": {"rendered": "<p>742 Evergreen Terrace</p>"},
        "excerpt": {"rendered": ""},
        "status": "publish",
        "date": "2022-01-05T09:00:00",
        "modified": "2024-02-02T09:00:00",
        "parent": 0,
        "menu_order": 1,
        "featured_media": 0,
        "phone": "(555) 010-2030",
        "zip": "62701",
    },
    {
        "id": 202,
        "slug": "shelbyville",
        "title": {"rendered": "Shelbyville"},
        "content": {"rendered": "<p>15 Main Street</p>"},
        "excerpt": {"rendered": ""},
        "status": "publish",
        "date": "2022-01-06T09:00:00",
        "modified": "2024-02-03T09:00:00",
        "parent": 0,
        "menu_order": 2,
        "featured_media": 0,
        "phone": "555.010.4050",
        "zip": "62565",
    },
]

PAGES = [
    {
        "id": 1,
        "slug": "home",
        "title": {"rendered": "Clearview Auto Glass"},
        "content": {"rendered": "<p>Mobile auto glass service.</p>"},
        "excerpt": {"rendered": ""},
        "status": "publish",
        "date": "2021-01-01T09:00:00",
        "modified": "2024-01-01T09:00:00",
        "parent": 0,
        "menu_order": 0,
        "featured_media": 0,
    },
    {
        "id": 2,
        "slug": "about",
        "title": {"rendered": "About Us"},
        "content": {"rendered": "<p>Family owned since 1998.</p>"},
        "excerpt": {"rendered": ""},
        "status": "publish",
        "date": "2021-01-02T09:00:00",
        "modified": "2024-01-02T09:00:00",
        "parent": 0,
        "menu_order": 1,
        "featured_media": 0,
    },
]

TYPES = {
    "page": {"name": "Pages", "rest_base": "pages", "rest_namespace": "wp/v2"},
    "post": {"name": "Posts", "rest_base": "posts", "rest_namespace": "wp/v2"},
    "service": {"name": "Services", "rest_base": "services", "rest_namespace": "wp/v2"},
    "location": {"name": "Locations", "rest_base": "locations", "rest_namespace": "wp/v2"},
    # Present but empty, so the draft has something to correctly skip.
    "attachment": {"name": "Media", "rest_base": "media", "rest_namespace": "wp/v2"},
}

COLLECTIONS = {
    "pages": PAGES,
    "posts": [],
    "services": SERVICES,
    "locations": LOCATIONS,
    "media": [],
}

JSONLD = {
    "@context": "https://schema.org",
    "@type": "AutoRepair",
    "name": "Clearview Auto Glass",
    "telephone": "(555) 010-2030",
    "address": {
        "@type": "PostalAddress",
        "streetAddress": "742 Evergreen Terrace",
        "addressLocality": "Springfield",
        "addressRegion": "IL",
        "postalCode": "62701",
    },
    "geo": {"@type": "GeoCoordinates", "latitude": 39.7817, "longitude": -89.6501},
    "openingHoursSpecification": [
        {"@type": "OpeningHoursSpecification", "dayOfWeek": "Monday", "opens": "08:00", "closes": "17:00"}
    ],
}


def _service_html(item: dict) -> str:
    return f"""<!DOCTYPE html>
<html><head>
<meta name="generator" content="WordPress 6.4.2">
<meta name="description" content="{item['title']['rendered']} from Clearview Auto Glass.">
<link rel="stylesheet" href="/wp-content/themes/clearview/style.css">
<script type="application/ld+json">{json.dumps(JSONLD)}</script>
</head><body>
<header><a href="/">Home</a> <a href="/services/">Services</a></header>
<main class="entry-content">
  <h1>{item['title']['rendered']}</h1>
  {item['content']['rendered']}
  <p class="price">{item.get('base_price', '')}</p>
  <img src="/wp-content/uploads/{item['slug']}.jpg" alt="">
</main>
</body></html>"""


def _home_html() -> str:
    links = "".join(
        f'<a href="/services/{s["slug"]}/">{s["title"]["rendered"]}</a>' for s in SERVICES
    )
    return f"""<!DOCTYPE html>
<html><head>
<meta name="generator" content="WordPress 6.4.2">
<meta name="description" content="Mobile auto glass, windshield replacement and ADAS calibration.">
<link rel="https://api.w.org/" href="/wp-json/">
<script src="/wp-includes/js/jquery.js"></script>
<script type="application/ld+json">{json.dumps(JSONLD)}</script>
</head><body>
<main><h1>Clearview Auto Glass</h1>{links}
<a href="/locations/springfield/">Springfield</a>
<a href="/locations/shelbyville/">Shelbyville</a>
</main></body></html>"""


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{b}/</loc></url>
  <url><loc>{b}/about/</loc></url>
  <url><loc>{b}/services/windshield-replacement/</loc></url>
  <url><loc>{b}/services/rock-chip-repair/</loc></url>
  <url><loc>{b}/services/adas-calibration/</loc></url>
  <url><loc>{b}/locations/springfield/</loc></url>
  <url><loc>{b}/locations/shelbyville/</loc></url>
  <url><loc>{b}/private/secret/</loc></url>
</urlset>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep pytest output clean
        pass

    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, payload, status: int = 200, extra: dict[str, str] | None = None) -> None:
        self._send(json.dumps(payload), status, "application/json; charset=utf-8", extra)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        base = f"http://{self.headers.get('Host', '127.0.0.1')}"

        if path == "/robots.txt":
            self._send(
                f"User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap.xml\n",
                ctype="text/plain; charset=utf-8",
            )
            return

        if path == "/sitemap.xml":
            self._send(SITEMAP.format(b=base), ctype="application/xml; charset=utf-8")
            return

        if path == "/":
            self._send(_home_html())
            return

        if path == "/app/":
            # A client-rendered page: the content only exists after an XHR.
            # This is what `eagm capture` is for.
            self._send(
                "<!DOCTYPE html><html><head><title>Quote tool</title></head>"
                "<body><div id=root>Loading…</div><script>"
                "fetch('/wp-json/wp/v2/services?per_page=100')"
                ".then(r=>r.json())"
                ".then(d=>{document.getElementById('root').textContent="
                "d.map(s=>s.title.rendered).join(', ')});"
                "</script></body></html>"
            )
            return

        if path == "/wp-json/":
            self._json({"name": "Clearview Auto Glass", "namespaces": ["wp/v2"], "routes": {}})
            return

        if path == "/wp-json/wp/v2/types":
            self._json(TYPES)
            return

        if path.startswith("/wp-json/wp/v2/"):
            resource = path[len("/wp-json/wp/v2/"):].strip("/")
            if resource not in COLLECTIONS:
                self._json({"code": "rest_no_route"}, status=404)
                return
            prefix = {
                "pages": "", "services": "services/", "locations": "locations/",
                "posts": "blog/", "media": "media/",
            }[resource]
            # Real WordPress returns the canonical permalink on every record.
            items = [
                {**item, "link": f"{base}/{prefix}{item['slug']}/"}
                for item in COLLECTIONS[resource]
            ]
            per_page = int(query.get("per_page", ["10"])[0])
            page = int(query.get("page", ["1"])[0])
            start = (page - 1) * per_page
            chunk = items[start:start + per_page]
            if page > 1 and not chunk:
                self._json({"code": "rest_post_invalid_page_number"}, status=400)
                return
            self._json(
                chunk,
                extra={
                    "X-WP-Total": str(len(items)),
                    "X-WP-TotalPages": str(max(1, -(-len(items) // per_page))),
                },
            )
            return

        for item in SERVICES:
            if path == f"/services/{item['slug']}/":
                self._send(_service_html(item))
                return
        for item in LOCATIONS:
            if path == f"/locations/{item['slug']}/":
                self._send(_service_html(item))
                return
        if path == "/about/":
            self._send(_service_html(PAGES[1]))
            return
        if path.startswith("/private/"):
            self._send("<html><body>secret</body></html>")
            return

        self._send("<html><body>Not found</body></html>", status=404)


class FixtureSite:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FixtureSite:
        self.thread.start()
        return self

    def __exit__(self, *_) -> None:
        self.server.shutdown()
        self.server.server_close()
