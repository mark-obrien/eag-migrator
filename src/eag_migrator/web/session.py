"""Authenticated sessions.

EAG v2 is an application, not a brochure: scheduling, quoting and payments all
sit behind a login, so every useful endpoint needs credentials. Three ways to
get them, in order of preference:

  1. **Cookie import** — you log in with your own browser and export the
     cookies. Nothing here ever sees a password. Start here.
  2. **Bearer token** — for apps that issue one.
  3. **Form login** — the migrator drives the login page itself. Needs
     credentials in the environment, so use it only for unattended runs.

The session file holds live credentials to a system with customer data. It is
written 0600, kept in state/ (gitignored) and never echoed to a report.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Headers worth carrying over from a captured request; anything else is noise
# or actively harmful to replay (Content-Length, Host, ...).
REPLAYABLE_HEADERS = {
    "authorization",
    "x-csrf-token",
    "x-xsrf-token",
    "x-requested-with",
    "x-api-key",
    "x-auth-token",
    "x-tenant-id",
    "x-account-id",
    "accept",
    "accept-language",
}

SECRET_HEADERS = {"authorization", "x-api-key", "x-auth-token", "cookie"}


@dataclass
class Session:
    origin: str
    cookies: list[dict[str, Any]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    captured_at: float = 0.0

    # --- construction -------------------------------------------------------

    @classmethod
    def from_cookie_header(cls, header: str, origin: str) -> Session:
        """Parse a raw `Cookie:` header — the copy-paste path from devtools."""
        domain = urlparse(origin).hostname or ""
        cookies: list[dict[str, Any]] = []
        for part in header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": domain,
                    "path": "/",
                }
            )
        return cls(origin=origin, cookies=cookies, captured_at=time.time())

    @classmethod
    def from_browser_export(cls, payload: Any, origin: str) -> Session:
        """Accept the JSON that Cookie-Editor / EditThisCookie produce."""
        domain = urlparse(origin).hostname or ""
        items = payload if isinstance(payload, list) else payload.get("cookies", [])
        cookies = []
        for item in items:
            if not isinstance(item, dict) or "name" not in item:
                continue
            cookies.append(
                {
                    "name": item["name"],
                    "value": item.get("value", ""),
                    "domain": item.get("domain") or domain,
                    "path": item.get("path") or "/",
                }
            )
        return cls(origin=origin, cookies=cookies, captured_at=time.time())

    @classmethod
    def from_storage_state(cls, state: dict[str, Any], origin: str) -> Session:
        """Playwright's storage_state, as written after a browser login."""
        cookies = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            for c in state.get("cookies", [])
        ]
        return cls(origin=origin, cookies=cookies, captured_at=time.time())

    @classmethod
    def from_env(cls, origin: str) -> Session | None:
        """EAGM_AUTH_TOKEN / EAGM_COOKIE, for CI and unattended runs."""
        token = (os.getenv("EAGM_AUTH_TOKEN") or "").strip()
        cookie = (os.getenv("EAGM_COOKIE") or "").strip()
        if not token and not cookie:
            return None
        session = (
            cls.from_cookie_header(cookie, origin)
            if cookie
            else cls(origin=origin, captured_at=time.time())
        )
        if token:
            session.headers["Authorization"] = (
                token if token.lower().startswith("bearer ") else f"Bearer {token}"
            )
        return session

    # --- persistence --------------------------------------------------------

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "origin": self.origin,
                    "cookies": self.cookies,
                    "headers": self.headers,
                    "captured_at": self.captured_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # Live credentials to a system holding customer data.
        os.chmod(path, 0o600)
        return path

    @classmethod
    def load(cls, path: Path) -> Session:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            origin=data.get("origin", ""),
            cookies=data.get("cookies", []),
            headers=data.get("headers", {}),
            captured_at=data.get("captured_at", 0.0),
        )

    # --- use ----------------------------------------------------------------

    def cookie_header(self) -> str:
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies)

    def as_request_headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        return headers

    def to_storage_state(self) -> dict[str, Any]:
        return {
            "cookies": [
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", urlparse(self.origin).hostname or ""),
                    "path": c.get("path", "/"),
                    "expires": -1,
                    "httpOnly": False,
                    "secure": self.origin.startswith("https"),
                    "sameSite": "Lax",
                }
                for c in self.cookies
            ],
            "origins": [],
        }

    def adopt_headers(self, headers: dict[str, str]) -> int:
        """Take the auth-bearing headers off a captured request.

        SPAs commonly send a bearer token or CSRF header that the cookie jar
        alone does not carry; without these the replayed calls 401.
        """
        added = 0
        for name, value in headers.items():
            lowered = name.lower()
            if lowered in REPLAYABLE_HEADERS and lowered != "accept-language":
                if lowered == "accept" and "json" not in value.lower():
                    continue
                if self.headers.get(name) != value:
                    self.headers[name] = value
                    added += 1
        return added

    @property
    def age_hours(self) -> float:
        return (time.time() - self.captured_at) / 3600 if self.captured_at else 0.0

    def describe(self) -> str:
        """Safe to print: names only, never values."""
        bits = []
        if self.cookies:
            bits.append(f"{len(self.cookies)} cookie(s): " + ", ".join(
                c["name"] for c in self.cookies[:6]
            ))
        secret = [h for h in self.headers if h.lower() in SECRET_HEADERS]
        other = [h for h in self.headers if h.lower() not in SECRET_HEADERS]
        if secret:
            bits.append(f"{len(secret)} auth header(s) present (values withheld)")
        if other:
            bits.append("headers: " + ", ".join(other))
        if self.captured_at:
            bits.append(f"captured {self.age_hours:.1f}h ago")
        return "; ".join(bits) or "empty session"
