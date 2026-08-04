"""A deliberately polite HTTP client.

Everything that touches the live site goes through here, so the rules live in
one place: obey robots.txt, honour Crawl-delay, rate-limit, identify ourselves
honestly, and cache on disk so iterating on selectors does not mean hammering
someone else's server.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

DEFAULT_UA = (
    "eag-migrator/0.1 (site migration tooling; contact the site owner who "
    "commissioned this migration)"
)


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    text: str
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    errors: int = 0
    unauthorized: int = 0
    blocked_by_robots: list[str] = field(default_factory=list)


class Fetcher:
    def __init__(
        self,
        base_url: str,
        *,
        cache_dir: Path | None = None,
        user_agent: str = DEFAULT_UA,
        rate_limit_rps: float = 1.0,
        respect_robots: bool = True,
        timeout: float = 20.0,
        use_cache: bool = True,
        session: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.origin = f"{urlparse(self.base_url).scheme}://{urlparse(self.base_url).netloc}"
        self.user_agent = user_agent
        self.min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self.respect_robots = respect_robots
        self.use_cache = use_cache
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

        self.stats = FetchStats()
        self.session = session
        self._last_request = 0.0
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._robots_loaded = False

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if session is not None:
            headers.update(session.as_request_headers())

        self.client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    @property
    def authenticated(self) -> bool:
        return self.session is not None and bool(
            self.session.cookies or self.session.headers
        )

    # --- robots -------------------------------------------------------------

    def _load_robots(self) -> None:
        if self._robots_loaded:
            return
        self._robots_loaded = True
        parser = urllib.robotparser.RobotFileParser()
        url = urljoin(self.origin + "/", "robots.txt")
        try:
            resp = self.client.get(url)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                self._robots = parser
            else:
                # No robots.txt means no restrictions expressed.
                self._robots = None
        except httpx.HTTPError:
            self._robots = None

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        self._load_robots()
        if self._robots is None:
            return True
        return self._robots.can_fetch(self.user_agent, url)

    def crawl_delay(self) -> float:
        """Crawl-delay from robots.txt, if the site asks for one."""
        if not self.respect_robots:
            return 0.0
        self._load_robots()
        if self._robots is None:
            return 0.0
        try:
            delay = self._robots.crawl_delay(self.user_agent)
            return float(delay) if delay else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def sitemaps(self) -> list[str]:
        self._load_robots()
        if self._robots is None:
            return []
        try:
            return list(self._robots.site_maps() or [])
        except Exception:  # noqa: BLE001
            return []

    # --- fetching -----------------------------------------------------------

    def _cache_path(self, url: str) -> Path | None:
        if not (self.cache_dir and self.use_cache):
            return None
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def get(self, url: str, *, allow_offsite: bool = False) -> Response:
        full = urljoin(self.base_url + "/", url) if not url.startswith("http") else url

        if not allow_offsite and urlparse(full).netloc != urlparse(self.origin).netloc:
            return Response(full, 0, {}, "", elapsed_ms=0)

        cached = self._cache_path(full)
        if cached and cached.exists():
            payload = json.loads(cached.read_text(encoding="utf-8"))
            self.stats.cache_hits += 1
            return Response(
                url=payload["url"],
                status=payload["status"],
                headers=payload["headers"],
                text=payload["text"],
                from_cache=True,
            )

        if not self.allowed(full):
            self.stats.blocked_by_robots.append(full)
            return Response(full, 999, {}, "")

        self._throttle()
        started = time.monotonic()
        try:
            raw = self.client.get(full)
        except httpx.HTTPError as exc:
            self.stats.errors += 1
            return Response(full, 0, {}, f"{type(exc).__name__}: {exc}")

        self.stats.requests += 1
        resp = Response(
            url=str(raw.url),
            status=raw.status_code,
            headers={k.lower(): v for k, v in raw.headers.items()},
            text=raw.text,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

        if resp.status in (401, 403):
            self.stats.unauthorized += 1

        if cached and resp.ok:
            cached.write_text(
                json.dumps(
                    {
                        "url": resp.url,
                        "status": resp.status,
                        "headers": resp.headers,
                        "text": resp.text,
                    }
                ),
                encoding="utf-8",
            )
        return resp

    def post_json(self, url: str, payload: dict[str, Any]) -> Response:
        """For APIs that paginate over POST, and for GraphQL."""
        full = urljoin(self.base_url + "/", url) if not url.startswith("http") else url

        cached = None
        if self.cache_dir and self.use_cache:
            digest = hashlib.sha256(
                (full + json.dumps(payload, sort_keys=True)).encode("utf-8")
            ).hexdigest()[:24]
            cached = self.cache_dir / f"post-{digest}.json"
            if cached.exists():
                data = json.loads(cached.read_text(encoding="utf-8"))
                self.stats.cache_hits += 1
                return Response(
                    data["url"], data["status"], data["headers"], data["text"], from_cache=True
                )

        self._throttle()
        try:
            raw = self.client.post(full, json=payload)
        except httpx.HTTPError as exc:
            self.stats.errors += 1
            return Response(full, 0, {}, f"{type(exc).__name__}: {exc}")

        self.stats.requests += 1
        resp = Response(
            url=str(raw.url),
            status=raw.status_code,
            headers={k.lower(): v for k, v in raw.headers.items()},
            text=raw.text,
        )
        if resp.status in (401, 403):
            self.stats.unauthorized += 1
        if cached and resp.ok:
            cached.write_text(
                json.dumps(
                    {"url": resp.url, "status": resp.status,
                     "headers": resp.headers, "text": resp.text}
                ),
                encoding="utf-8",
            )
        return resp

    def robots_blocks_base(self) -> bool:
        """Does robots.txt disallow the app itself?

        Apps routinely `Disallow: /` because they do not want search engines
        indexing a logged-in area. That rule addresses crawlers of public
        content, not an authenticated user exporting their own records — but
        it is the operator's stated preference, so surface it rather than
        quietly deciding either way.
        """
        return self.respect_robots and not self.allowed(self.base_url + "/")

    def _throttle(self) -> None:
        interval = max(self.min_interval, self.crawl_delay())
        if interval <= 0:
            return
        wait = interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
