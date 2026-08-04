"""Getting an authenticated session out of the v2 app.

Preferred route is `import`: you log in with your own browser, export the
cookies, and the migrator never handles a password. `form` exists for
unattended runs and drives the login page in a real browser, which is the only
thing that reliably survives CSRF tokens, hashed field names and JS-built
request signing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture import CHROMIUM_ARGS, BrowserUnavailable, find_chromium
from .session import Session


@dataclass
class LoginSpec:
    login_url: str = "/login"
    username_selector: str = 'input[type="email"], input[name*="user"], #email, #username'
    password_selector: str = 'input[type="password"]'
    submit_selector: str = 'button[type="submit"], input[type="submit"]'
    success_selector: str | None = None
    """A selector only present once logged in. Strongly recommended."""
    username_env: str = "EAGM_USERNAME"
    password_env: str = "EAGM_PASSWORD"
    wait_ms: int = 3000


class LoginFailed(RuntimeError):
    pass


def import_session(source: str, origin: str) -> Session:
    """Build a session from a pasted Cookie header or an exported cookie file."""
    candidate = Path(source)
    if candidate.exists():
        raw = candidate.read_text(encoding="utf-8").strip()
        try:
            payload = json.loads(raw)
        except ValueError:
            return Session.from_cookie_header(raw, origin)
        if isinstance(payload, dict) and "cookies" in payload and "origins" in payload:
            return Session.from_storage_state(payload, origin)
        return Session.from_browser_export(payload, origin)
    return Session.from_cookie_header(source, origin)


def form_login(base_url: str, spec: LoginSpec, *, headless: bool = True) -> Session:
    """Drive the app's login form in a real browser and keep the session.

    Also picks up any auth header the app's own JavaScript attaches — SPAs
    frequently send a bearer or CSRF token that the cookie jar alone does not
    carry, and without it every replayed API call 401s.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BrowserUnavailable(
            "playwright is not installed. Rebuild with "
            "`--build-arg WITH_BROWSER=true`, or use `eagm login --cookies` "
            "to import a session from your own browser instead."
        ) from exc

    username = os.getenv(spec.username_env)
    password = os.getenv(spec.password_env)
    if not username or not password:
        raise LoginFailed(
            f"set {spec.username_env} and {spec.password_env} in the environment "
            f"(or use `eagm login --cookies` and avoid handling the password at all)"
        )

    url = spec.login_url if spec.login_url.startswith("http") else (
        base_url.rstrip("/") + "/" + spec.login_url.lstrip("/")
    )
    captured_headers: dict[str, str] = {}

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=headless, args=CHROMIUM_ARGS)
        except Exception:  # noqa: BLE001
            found = find_chromium()
            if not found:
                raise BrowserUnavailable(
                    "no Chromium available; use `eagm login --cookies` instead"
                ) from None
            browser = pw.chromium.launch(
                headless=headless, executable_path=str(found), args=CHROMIUM_ARGS
            )

        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        def on_request(request: Any) -> None:
            try:
                if request.resource_type in ("xhr", "fetch"):
                    captured_headers.update(request.all_headers())
            except Exception:  # noqa: BLE001
                pass

        page.on("request", on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.fill(spec.username_selector, username)
            page.fill(spec.password_selector, password)
            page.click(spec.submit_selector)
            page.wait_for_timeout(spec.wait_ms)

            if spec.success_selector:
                try:
                    page.wait_for_selector(spec.success_selector, timeout=15_000)
                except Exception as exc:  # noqa: BLE001
                    raise LoginFailed(
                        f"logged in but '{spec.success_selector}' never appeared — "
                        f"landed on {page.url}. Wrong credentials, an MFA prompt, "
                        f"or the wrong success_selector."
                    ) from exc
            elif page.url.rstrip("/") == url.rstrip("/"):
                raise LoginFailed(
                    f"still on the login page ({page.url}) — credentials rejected, "
                    f"or the form needs a step this does not do (MFA, captcha). "
                    f"Set success_selector to check properly."
                )

            state = context.storage_state()
        finally:
            context.close()
            browser.close()

    session = Session.from_storage_state(state, base_url)
    session.adopt_headers(captured_headers)
    if not session.cookies and not session.headers:
        raise LoginFailed(
            "login appeared to work but produced no cookies or auth headers"
        )
    return session
