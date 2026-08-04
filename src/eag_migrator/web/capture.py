"""Network capture: watch what the page actually calls at runtime.

`recon` probes endpoints we guess at. This drives a real browser and records
every request the site makes on its own — which is how you find the private
JSON API behind a React front end, the search backend, the quote calculator,
the store-locator feed. Those endpoints are usually far better structured than
the HTML they end up rendering.

Requires the Chromium binary. The docker image only ships it when built with
`--build-arg WITH_BROWSER=true` (it adds several hundred MB), so `eagm capture`
says so plainly rather than failing with an import error.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

MAX_BODY = 200_000
# Docker gives a container 64MB of /dev/shm by default, which Chromium
# exhausts on a heavy page and dies with an opaque crash. compose raises
# shm_size too; this flag makes it survive anywhere.
CHROMIUM_ARGS = ["--disable-dev-shm-usage"]
# Headers that carry authentication and must be replayed to reach the API,
# but must never be written to a report file.
AUTH_HEADERS = {"authorization", "x-csrf-token", "x-xsrf-token", "x-api-key",
                "x-auth-token", "x-requested-with", "x-tenant-id", "x-account-id"}
INTERESTING_TYPES = {"xhr", "fetch", "websocket", "eventsource"}
STATIC_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|eot|css|mp4|webm|m4a|mp3)(\?|$)", re.I
)

# Links an explore pass must never follow.
#
# This browses a live scheduling, quoting and payments system while signed
# in as a real user, so a naive crawler could void an invoice, archive a
# customer, email someone, or log itself out mid-run. Anything that reads as
# a state change is skipped and reported rather than followed. It is a
# denylist, so it is not a guarantee — but the failure mode of missing a
# screen is trivial next to the failure mode of triggering one.
UNSAFE_LINK = re.compile(
    r"(^|[/?&#_.-])("
    r"log[ _-]?out|sign[ _-]?out|"                        # would end the session
    r"delete|destroy|remove|trash|archive|purge|"
    r"cancel|void|refund|chargeback|reverse|"
    r"send|e[ _-]?mail|notify|sms|text|dispatch|remind|"   # contacts real people
    r"approve|reject|decline|accept|confirm|finali[sz]e|"
    r"pay|charge|capture|checkout|invoice_now|"
    r"impersonate|switch[ _-]?user|become|"
    r"reset|regenerate|rotate|revoke|disable|deactivate|"
    r"merge|restore|publish|unpublish"
    r")([/?&#_.-]|$)",
    re.IGNORECASE,
)
NON_PAGE_SCHEME = re.compile(r"^(mailto:|tel:|javascript:|blob:|data:)", re.I)
DOWNLOAD_EXT = re.compile(r"\.(pdf|csv|xlsx?|zip|docx?|pptx?)(\?|$)", re.I)


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium binary is not installed."""


def find_chromium() -> Path | None:
    """Locate a usable Chromium, ignoring Playwright's pinned build number.

    Checked in order: EAGM_CHROMIUM_PATH, any chromium under
    PLAYWRIGHT_BROWSERS_PATH, then the usual system locations.
    """
    import os

    explicit = os.environ.get("EAGM_CHROMIUM_PATH")
    if explicit and Path(explicit).exists():
        return Path(explicit)

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    if root.is_dir():
        # The `chromium` symlink, if the image provides one.
        link = root / "chromium"
        if link.exists() and not link.is_dir():
            return link
        # Otherwise the newest chromium-<build>/chrome-linux/chrome.
        builds = sorted(
            (p for p in root.glob("chromium-*/chrome-linux/chrome") if p.exists()),
            reverse=True,
        )
        if builds:
            return builds[0]

    for candidate in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if Path(candidate).exists():
            return Path(candidate)
    return None


@dataclass
class CapturedCall:
    method: str
    url: str
    pattern: str
    resource_type: str
    status: int = 0
    content_type: str = ""
    size: int = 0
    request_body: str | None = None
    response_keys: list[str] = field(default_factory=list)
    item_count: int | None = None
    sample: Any = None
    is_graphql: bool = False
    operation: str | None = None
    pagination: dict[str, Any] = field(default_factory=dict)
    """Inferred pagination shape, if the query string gives it away."""
    record_path: str = ""
    """Dotted path to the list inside the response, e.g. 'data'."""

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type


@dataclass
class SkippedLink:
    url: str
    reason: str
    text: str = ""


@dataclass
class CaptureReport:
    base_url: str
    generated_at: str
    pages_visited: list[str] = field(default_factory=list)
    skipped_links: list[SkippedLink] = field(default_factory=list)
    calls: list[CapturedCall] = field(default_factory=list)
    har_path: str | None = None
    notes: list[str] = field(default_factory=list)
    auth_headers: dict[str, str] = field(default_factory=dict)
    """Auth-bearing headers the app's own JS sent. Never written to disk."""
    authenticated: bool = False

    @property
    def api_calls(self) -> list[CapturedCall]:
        return [c for c in self.calls if c.is_json or c.is_graphql]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Bearer tokens and session cookies must not land in reports/.
        data["auth_headers"] = sorted(self.auth_headers)
        return data


def _pattern(url: str) -> str:
    """Collapse a URL to its shape: keep param names, drop values and numeric ids."""
    parsed = urlparse(url)
    path = re.sub(r"/\d+(?=/|$)", "/<id>", parsed.path)
    path = re.sub(
        r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)",
        "/<uuid>",
        path,
        flags=re.I,
    )
    keys = sorted(k for k, _ in parse_qsl(parsed.query))
    query = "&".join(f"{k}=…" for k in keys)
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


PAGE_PARAMS = {"page", "p", "pagenumber", "page_number", "pagina"}
PER_PAGE_PARAMS = {"per_page", "perpage", "limit", "page_size", "pagesize", "size", "take", "count"}
OFFSET_PARAMS = {"offset", "skip", "start", "from"}
CURSOR_PARAMS = {"cursor", "after", "next", "next_cursor", "page_token", "continuation"}


def _infer_pagination(url: str, body: Any = None) -> dict[str, Any]:
    """Read the pagination style off whatever the app actually sent.

    Query string for GETs; the JSON body for POST search/list endpoints, which
    is where those keep their paging.
    """
    params = {k.lower(): str(v) for k, v in parse_qsl(urlparse(url).query)}
    if isinstance(body, dict):
        params.update(
            {k.lower(): str(v) for k, v in body.items()
             if isinstance(v, (str, int, float))}
        )
    if not params:
        return {}

    per_page = next((k for k in params if k in PER_PAGE_PARAMS), None)
    found: dict[str, Any] = {}

    cursor = next((k for k in params if k in CURSOR_PARAMS), None)
    page = next((k for k in params if k in PAGE_PARAMS), None)
    offset = next((k for k in params if k in OFFSET_PARAMS), None)

    if cursor:
        found = {"style": "cursor", "cursor_param": cursor}
    elif page:
        found = {"style": "page", "page_param": page}
    elif offset:
        found = {"style": "offset", "offset_param": offset}
    elif per_page:
        found = {"style": "page", "page_param": "page"}

    if found and per_page:
        found["per_page_param"] = per_page
        raw = params.get(per_page, "")
        if raw.isdigit():
            found["observed_per_page"] = int(raw)
    return found


def _summarise(payload: Any) -> tuple[list[str], int | None, Any, str]:
    """Returns (field names, item count, sample record, path to the list)."""
    if isinstance(payload, list):
        first = payload[0] if payload else None
        keys = sorted(first.keys()) if isinstance(first, dict) else []
        return keys, len(payload), first, ""
    if isinstance(payload, dict):
        for wrapper in ("data", "items", "results", "records", "rows", "hits",
                        "edges", "content", "products", "list"):
            inner = payload.get(wrapper)
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                return sorted(inner[0].keys()), len(inner), inner[0], wrapper
        return sorted(payload.keys()), None, payload, ""
    return [], None, payload, ""


LINK_SCRIPT = """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.href,
  method: (a.getAttribute('data-method') || '').toLowerCase(),
  confirm: a.getAttribute('data-confirm') || a.getAttribute('data-turbo-confirm') || '',
  text: (a.textContent || '').trim().slice(0, 60),
}))"""


def _classify_links(
    raw: list[dict[str, Any]], origin: str, seen: set[str]
) -> tuple[list[str], list[SkippedLink]]:
    """Split the page's links into safe-to-visit and must-not-touch.

    Reads the live DOM, so it works on a client-rendered app as long as it uses
    real anchors — which almost all of them do, for accessibility and so that
    middle-click works.
    """
    safe: list[str] = []
    skipped: list[SkippedLink] = []
    host = urlparse(origin).netloc

    for item in raw:
        href = (item.get("href") or "").split("#")[0]
        text = item.get("text") or ""
        if not href or NON_PAGE_SCHEME.match(href):
            continue
        if urlparse(href).netloc != host:
            continue
        if href in seen or href in safe:
            continue

        # Rails/Turbo mark destructive links with a verb or a confirm prompt.
        if item.get("method") and item["method"] != "get":
            skipped.append(SkippedLink(href, f"data-method={item['method']}", text))
            continue
        if item.get("confirm"):
            skipped.append(SkippedLink(href, "has a confirmation prompt", text))
            continue
        if UNSAFE_LINK.search(urlparse(href).path + "?" + (urlparse(href).query or "")):
            skipped.append(SkippedLink(href, "looks like a state change", text))
            continue
        if DOWNLOAD_EXT.search(href):
            skipped.append(SkippedLink(href, "file download", text))
            continue
        if STATIC_EXT.search(href):
            continue
        safe.append(href)

    return safe, skipped


def capture(
    base_url: str,
    paths: list[str],
    *,
    har_path: Path | None = None,
    scroll: bool = True,
    wait_ms: int = 2500,
    timeout_ms: int = 30_000,
    user_agent: str | None = None,
    session: Any = None,
    explore: bool = False,
    max_pages: int = 25,
    depth: int = 2,
) -> CaptureReport:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on install shape
        raise BrowserUnavailable(
            "playwright is not installed. Rebuild the image with "
            "`docker compose build --build-arg WITH_BROWSER=true`, or "
            "`pip install playwright && playwright install chromium` locally."
        ) from exc

    report = CaptureReport(
        base_url=base_url,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        authenticated=session is not None,
    )
    seen: dict[tuple[str, str], CapturedCall] = {}

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        except Exception as first_error:  # noqa: BLE001
            # Playwright insists on the exact Chromium build it was pinned to.
            # An image that already ships a different build is common, so look
            # for one rather than demanding a several-hundred-MB download.
            found = find_chromium()
            if not found:
                raise BrowserUnavailable(
                    f"could not launch Chromium: {first_error}\n"
                    "Rebuild the image with `--build-arg WITH_BROWSER=true`, run "
                    "`playwright install chromium`, or point EAGM_CHROMIUM_PATH at "
                    "an existing Chrome/Chromium binary."
                ) from first_error
            try:
                browser = pw.chromium.launch(
                    headless=True, executable_path=str(found), args=CHROMIUM_ARGS
                )
            except Exception as exc:  # noqa: BLE001
                raise BrowserUnavailable(
                    f"could not launch Chromium at {found}: {exc}"
                ) from exc

        ctx_args: dict[str, Any] = {"ignore_https_errors": True}
        if session is not None:
            ctx_args["storage_state"] = session.to_storage_state()
        if user_agent:
            ctx_args["user_agent"] = user_agent
        if har_path:
            har_path.parent.mkdir(parents=True, exist_ok=True)
            ctx_args["record_har_path"] = str(har_path)
            ctx_args["record_har_content"] = "embed"

        context = browser.new_context(**ctx_args)
        page = context.new_page()

        def on_response(response: Any) -> None:
            try:
                request = response.request
                rtype = request.resource_type
                url = response.url
            except Exception:  # noqa: BLE001 - the page can navigate mid-event
                return

            if rtype not in INTERESTING_TYPES and not _looks_like_api(url, response):
                return
            if STATIC_EXT.search(url):
                return

            key = (request.method, _pattern(url))
            if key in seen:
                return

            content_type = (response.header_value("content-type") or "").split(";")[0].lower()
            call = CapturedCall(
                method=request.method,
                url=url,
                pattern=key[1],
                resource_type=rtype,
                status=response.status,
                content_type=content_type,
            )

            try:
                report.auth_headers.update(
                    {
                        k: v
                        for k, v in request.all_headers().items()
                        if k.lower() in AUTH_HEADERS
                    }
                )
            except Exception:  # noqa: BLE001
                pass

            post = None
            try:
                post = request.post_data
            except Exception:  # noqa: BLE001
                pass
            parsed_body: Any = None
            if post:
                call.request_body = post[:2000]
                try:
                    parsed_body = json.loads(post)
                except ValueError:
                    parsed_body = None
                if "query" in post and "{" in post:
                    call.is_graphql = True
                    if isinstance(parsed_body, dict):
                        call.operation = parsed_body.get("operationName")

            call.pagination = _infer_pagination(url, parsed_body)

            try:
                body = response.body()
                call.size = len(body)
                if "json" in content_type and len(body) <= MAX_BODY:
                    payload = json.loads(body)
                    (call.response_keys, call.item_count, sample,
                     call.record_path) = _summarise(payload)
                    call.sample = _trim(sample)
            except Exception:  # noqa: BLE001 - bodies are often unavailable
                pass

            seen[key] = call

        page.on("response", on_response)

        queue: list[tuple[str, int]] = [
            (
                path if path.startswith("http")
                else base_url.rstrip("/") + "/" + path.lstrip("/"),
                0,
            )
            for path in paths
        ]
        visited: set[str] = set()

        while queue:
            url, level = queue.pop(0)
            if url in visited or len(visited) >= max_pages:
                continue
            visited.add(url)

            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                report.pages_visited.append(url)
            except Exception as exc:  # noqa: BLE001
                report.notes.append(f"{url}: {type(exc).__name__}: {exc}")
                continue

            if scroll:
                # Lazy-loaded lists only call their API once they come into view.
                try:
                    for _ in range(4):
                        page.mouse.wheel(0, 4000)
                        page.wait_for_timeout(400)
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(wait_ms)

            if not explore or level >= depth:
                continue
            try:
                links = page.evaluate(LINK_SCRIPT)
            except Exception:  # noqa: BLE001
                continue
            safe, skipped = _classify_links(links, base_url, visited)
            for entry in skipped:
                if not any(s.url == entry.url for s in report.skipped_links):
                    report.skipped_links.append(entry)
            for link in safe:
                if link not in visited:
                    queue.append((link, level + 1))

        context.close()
        browser.close()

    if har_path:
        report.har_path = str(har_path)
    report.calls = sorted(seen.values(), key=lambda c: (not c.is_json, c.pattern))

    if not report.api_calls:
        report.notes.append(
            "no JSON/XHR calls seen — the site is probably server-rendered, so "
            "harvesting will parse HTML. Check the recon report for JSON-LD, which "
            "is usually cleaner than scraping the markup."
        )
    return report


def _looks_like_api(url: str, response: Any) -> bool:
    if re.search(r"/(api|wp-json|graphql|rest|ajax)(/|\?|$)", url, re.I):
        return True
    try:
        return "json" in (response.header_value("content-type") or "").lower()
    except Exception:  # noqa: BLE001
        return False


def _trim(value: Any, depth: int = 0) -> Any:
    """Keep samples small enough to read in a report."""
    if depth > 2:
        return "…"
    if isinstance(value, dict):
        return {k: _trim(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_trim(v, depth + 1) for v in value[:3]]
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    return value


def render_markdown(report: CaptureReport) -> str:
    lines = [f"# Network capture — {report.base_url}\n"]
    lines.append(f"- Generated: {report.generated_at}")
    lines.append(f"- Pages driven: {len(report.pages_visited)}")
    if report.skipped_links:
        lines.append(f"- Links not followed: {len(report.skipped_links)}")
    lines.append(f"- Calls recorded: **{len(report.calls)}** "
                 f"({len(report.api_calls)} returning JSON)")
    if report.har_path:
        lines.append(f"- HAR: `{report.har_path}` (open in browser devtools)")
    lines.append("")

    if report.api_calls:
        lines.append("## JSON endpoints the site calls itself\n")
        lines.append("These are the migration source to prefer over scraping HTML.\n")
        lines.append("| Method | Endpoint | Status | Items | Bytes |")
        lines.append("|---|---|---:|---:|---:|")
        for c in report.api_calls:
            lines.append(
                f"| {c.method} | `{c.pattern}` | {c.status} | "
                f"{c.item_count if c.item_count is not None else '—'} | {c.size:,} |"
            )
        lines.append("")

        for c in report.api_calls:
            lines.append(f"### {c.method} `{c.pattern}`\n")
            if c.is_graphql:
                lines.append(f"GraphQL operation: `{c.operation or 'anonymous'}`\n")
            if c.request_body:
                lines.append("Request body:\n")
                lines.append("```")
                lines.append(c.request_body[:1200])
                lines.append("```\n")
            if c.response_keys:
                lines.append("Response fields: `" + "`, `".join(c.response_keys) + "`\n")
            if c.sample is not None:
                lines.append("Sample:\n")
                lines.append("```json")
                lines.append(json.dumps(c.sample, indent=2, default=str)[:1500])
                lines.append("```\n")

    other = [c for c in report.calls if c not in report.api_calls]
    if other:
        lines.append("## Other recorded calls\n")
        lines.append("| Method | Endpoint | Type | Status |")
        lines.append("|---|---|---|---:|")
        for c in other:
            lines.append(f"| {c.method} | `{c.pattern}` | {c.resource_type} | {c.status} |")
        lines.append("")

    if report.skipped_links:
        lines.append("## Links deliberately not followed\n")
        lines.append(
            "Explore browses a live system while signed in, so anything that "
            "reads as a state change is left alone.\n"
        )
        lines.append("| Link | Why | Label |")
        lines.append("|---|---|---|")
        for link in report.skipped_links:
            lines.append(f"| `{link.url}` | {link.reason} | {link.text} |")
        lines.append("")

    if report.notes:
        lines.append("## Notes\n")
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)
