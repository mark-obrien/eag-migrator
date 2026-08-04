"""Credentials for talking to v3's HTTP API.

The same shape as the v2 side: a target may authenticate with a bearer token
or, when it issues no token at all, with the session cookie from a signed-in
browser. Storing that in `state/v3_session.json` rather than `.env` keeps it
out of a file people edit and share, puts it at mode 0600 next to the v2
session, and means the dashboard can set it without rewriting `.env`.

Environment always wins, so an unattended run can be configured entirely from
`.env` with no stored session.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import STATE_DIR, Settings

V3_SESSION_FILE = STATE_DIR / "v3_session.json"


@dataclass
class V3Credentials:
    base_url: str | None
    token: str | None = None
    cookie: str | None = None
    source: str = "none"
    """Where the credential came from, for reporting. Never the value."""

    @property
    def ready(self) -> bool:
        return bool(self.base_url) and bool(self.token or self.cookie)

    def describe(self) -> str:
        if not self.base_url:
            return "no base URL set"
        if self.token:
            return f"bearer token ({self.source})"
        if self.cookie:
            return f"session cookie ({self.source})"
        return "no credential — expect 401"


def load_credentials(settings: Settings, path: Path | None = None) -> V3Credentials:
    path = path or V3_SESSION_FILE
    base_url = settings.v3_api_base_url
    token = settings.v3_api_token
    cookie = settings.v3_api_cookie
    source = "environment"

    if not (token or cookie) or not base_url:
        stored = _load_session(path)
        if stored is not None:
            base_url = base_url or stored.origin
            if not token and not cookie:
                cookie = stored.cookie_header()
                token = stored.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                source = "saved session"

    return V3Credentials(
        base_url=base_url,
        token=token or None,
        cookie=cookie or None,
        source=source if (token or cookie) else "none",
    )


def save_cookie(base_url: str, cookie_header: str, path: Path | None = None) -> Path:
    """Store a signed-in browser's Cookie header for v3."""
    from .web.session import Session

    path = path or V3_SESSION_FILE
    session = Session.from_cookie_header(cookie_header, base_url.rstrip("/"))
    if not session.cookies:
        raise ValueError(
            "no cookies found in that header — paste the whole Cookie line from "
            "devtools, e.g. 'name=value; other=value'"
        )
    return session.save(path)


def forget(path: Path | None = None) -> bool:
    path = path or V3_SESSION_FILE
    if path.exists():
        path.unlink()
        return True
    return False


def status(settings: Settings, path: Path | None = None) -> dict:
    """For the dashboard. Names and ages only — never a credential value."""
    path = path or V3_SESSION_FILE
    creds = load_credentials(settings, path)
    stored = _load_session(path)
    return {
        "base_url": creds.base_url,
        "ready": creds.ready,
        "describe": creds.describe(),
        "from_env": bool(settings.v3_api_token or settings.v3_api_cookie),
        "saved": stored is not None,
        "names": stored.describe() if stored else "",
        "age_hours": round(stored.age_hours, 1) if stored else None,
        "stale": bool(stored and stored.age_hours > 12),
    }


def _load_session(path: Path):
    if not path.exists():
        return None
    try:
        from .web.session import Session

        return Session.load(path)
    except Exception:  # noqa: BLE001 - a corrupt session is "no session"
        return None


def client(settings: Settings, path: Path | None = None):
    """An httpx client pointed at v3, or a clear error about what is missing."""
    from .adapters.api_sink import build_client

    creds = load_credentials(settings, path)
    if not creds.base_url:
        raise ValueError(
            "v3's API base URL is not set. Set V3_API_BASE_URL in .env, or save "
            "a session from the dashboard."
        )
    return build_client(creds.base_url, creds.token, settings.v3_api_timeout, creds.cookie)


def env_hint() -> str:
    return (
        "Open v3 in a signed-in browser tab, then devtools > Network > any "
        "request > copy the whole Cookie header."
    )
