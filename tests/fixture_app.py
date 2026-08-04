"""A fake auto-glass *application*, served over real HTTP.

Scheduling, quoting and payments behind a login — the shape EAG v2 actually
is. Deliberately not EAG: this is a fictional shop.

What it exercises:
  * a login wall: anonymous requests get the login page, never data
  * a session cookie plus a CSRF header the SPA sends on every XHR
  * a client-rendered shell whose data only arrives over fetch()
  * three pagination styles: page numbers, offset/limit, and cursors
  * a POST search endpoint
  * cardholder data on the payments endpoint, so the scrubber has to earn it
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USERNAME = "owner@clearview.example"
PASSWORD = "correct-horse"
SESSION_COOKIE = "cv_session"
SESSION_VALUE = "s3ss10n-abc123"
CSRF_HEADER = "x-csrf-token"
CSRF_VALUE = "csrf-tok-987"

CUSTOMERS = [
    {"id": 5001, "name": "Dana Reyes", "email": "Dana@Example.COM",
     "phone": "(555) 123-4567", "postal_code": "94107", "status": "active",
     "created_at": "2021-03-04T09:15:00Z"},
    {"id": 5002, "name": "Sam Oyelaran", "email": "sam.oyelaran@example.com",
     "phone": "555-987-6543", "postal_code": "94107-2233", "status": "inactive",
     "created_at": "2022-07-19T14:02:11Z"},
    {"id": 5003, "name": "Kit Nakamura", "email": "kit@example.org",
     "phone": "5551112222", "postal_code": "10001", "status": "hold",
     "created_at": "2023-01-11T10:00:00Z"},
    {"id": 5004, "name": "Robin Vale", "email": "robin@example.net",
     "phone": "5554443333", "postal_code": "60601", "status": "active",
     "created_at": "2023-05-02T08:30:00Z"},
    {"id": 5005, "name": "Ash Whitfield", "email": "ash@example.com",
     "phone": "5559990000", "postal_code": "30301", "status": "active",
     "created_at": "2024-02-14T11:45:00Z"},
]

QUOTES = [
    {"id": 9001, "customer": {"id": 5001, "name": "Dana Reyes"},
     "vin": "1hgcm82633a004352", "nags_part": "dw01234 gtyn",
     "total": "$449.99", "status": "accepted", "created_at": "2024-05-01T08:00:00Z",
     "notes": "Customer read card over the phone: 4111 1111 1111 1111"},
    {"id": 9002, "customer": {"id": 5001, "name": "Dana Reyes"},
     "vin": "5NPE24AF1FH012345", "nags_part": "fw02345",
     "total": "1,250.00", "status": "sent", "created_at": "2024-05-03T13:30:00Z",
     "notes": "ADAS recalibration required"},
    {"id": 9003, "customer": {"id": 5002, "name": "Sam Oyelaran"},
     "vin": None, "nags_part": "dw03456", "total": "310.50",
     "status": "draft", "created_at": "2024-06-11T10:00:00Z", "notes": None},
    {"id": 9004, "customer": {"id": 5003, "name": "Kit Nakamura"},
     "vin": "JH4KA7561PC008269", "nags_part": "cal-001", "total": "249.00",
     "status": "accepted", "created_at": "2024-06-12T16:45:00Z", "notes": None},
]

APPOINTMENTS = [
    {"id": 7001, "quote_id": 9001, "technician": "Lee Park", "starts_at": "2024-05-06T09:00:00Z",
     "duration_min": 90, "location": "mobile", "state": "completed"},
    {"id": 7002, "quote_id": 9002, "technician": "Jo Márquez", "starts_at": "2024-05-08T13:00:00Z",
     "duration_min": 150, "location": "shop", "state": "completed"},
    {"id": 7003, "quote_id": 9004, "technician": "Lee Park", "starts_at": "2024-06-15T08:00:00Z",
     "duration_min": 120, "location": "mobile", "state": "scheduled"},
]

# The payments screen. A migrator must take the tokens and the last four and
# nothing else — these raw values exist purely to prove the guard works.
PAYMENTS = [
    {"id": 3001, "quote_id": 9001, "amount": "449.99", "currency": "USD",
     "processor": "stripe", "processor_ref": "ch_3PabcdEFGH",
     "card_number": "4111111111111111", "cvv": "123",
     "card_brand": "visa", "exp_month": 4, "exp_year": 2027,
     "paid_at": "2024-05-06T10:35:00Z"},
    {"id": 3002, "quote_id": 9002, "amount": "1250.00", "currency": "USD",
     "processor": "stripe", "processor_ref": "ch_3PijklMNOP",
     "card_number": "5555 5555 5555 4444", "cvv": "999",
     "card_brand": "mastercard", "exp_month": 11, "exp_year": 2026,
     "paid_at": "2024-05-08T16:02:00Z"},
]

LOGIN_HTML = """<!DOCTYPE html>
<html><head><title>Sign in — Clearview Ops</title></head>
<body>
  <h1>Sign in</h1>
  <form method="POST" action="/login">
    <input type="email" name="email" id="email" placeholder="Email">
    <input type="password" name="password" id="password" placeholder="Password">
    <button type="submit">Log in</button>
  </form>
  <p>Forgot your password?</p>
</body></html>"""

# The app shell: renders nothing itself, loads everything over XHR.
APP_HTML = """<!DOCTYPE html>
<html><head><title>Clearview Ops</title></head>
<body>
<div class="dashboard" id="root">Loading…</div>
<script>
const H = {'%(csrf_header)s': '%(csrf_value)s', 'Accept': 'application/json'};
async function boot() {
  await fetch('/api/v2/session', {headers: H});
  await fetch('/api/v2/customers?page=1&per_page=2', {headers: H});
  await fetch('/api/v2/quotes?limit=2&offset=0', {headers: H});
  await fetch('/api/v2/appointments?limit=2', {headers: H});
  await fetch('/api/v2/payments?page=1&per_page=50', {headers: H});
  await fetch('/api/v2/search', {method: 'POST', headers:
    Object.assign({'Content-Type': 'application/json'}, H),
    body: JSON.stringify({q: '', type: 'customer', page: 1, per_page: 2})});
  document.getElementById('root').textContent = 'ready';
}
boot();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        pass

    # --- helpers ------------------------------------------------------------

    def _send(self, body: str, status: int = 200,
              ctype: str = "text/html; charset=utf-8",
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
        self._json_headers = extra
        self._send(json.dumps(payload), status, "application/json; charset=utf-8", extra)

    @property
    def signed_in(self) -> bool:
        return f"{SESSION_COOKIE}={SESSION_VALUE}" in (self.headers.get("Cookie") or "")

    def _deny(self) -> None:
        """API calls get a 401; page requests get the login screen."""
        if self.path.startswith("/api/"):
            self._json({"error": "unauthenticated"}, status=401)
        else:
            self._send(LOGIN_HTML, status=200)

    def _page(self, items: list, query: dict) -> tuple[list, dict]:
        """Slice by whichever pagination style the caller used.

        A cursor API hands back the *first* cursor on the first request, before
        the client has one to send — so `limit` alone selects this branch.
        """
        cursor_style = (
            "cursor" in query
            or "after" in query
            or ("limit" in query and "page" not in query and "offset" not in query)
        )
        if cursor_style:
            cursor = int((query.get("cursor") or query.get("after") or ["0"])[0] or 0)
            size = int((query.get("limit") or ["2"])[0])
            chunk = items[cursor:cursor + size]
            nxt = cursor + size
            return chunk, {"meta": {"next_cursor": nxt if nxt < len(items) else None}}
        if "offset" in query:
            offset = int(query.get("offset", ["0"])[0])
            size = int((query.get("limit") or query.get("per_page") or ["2"])[0])
            return items[offset:offset + size], {}
        page = int(query.get("page", ["1"])[0])
        size = int((query.get("per_page") or query.get("limit") or ["2"])[0])
        start = (page - 1) * size
        return items[start:start + size], {}

    # --- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/robots.txt":
            self._send("User-agent: *\nDisallow: /\n", ctype="text/plain; charset=utf-8")
            return

        if path in ("/login", "/login/"):
            self._send(LOGIN_HTML)
            return

        if not self.signed_in:
            self._deny()
            return

        if path in ("/", "/app", "/app/", "/customers", "/quotes", "/schedule"):
            self._send(APP_HTML % {"csrf_header": CSRF_HEADER, "csrf_value": CSRF_VALUE})
            return

        if path.startswith("/api/"):
            if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                self._json({"error": "missing csrf token"}, status=403)
                return

            if path == "/api/v2/session":
                self._json({"user": {"id": 1, "email": USERNAME}, "tenant": "clearview"})
                return

            table = {
                "/api/v2/customers": CUSTOMERS,
                "/api/v2/quotes": QUOTES,
                "/api/v2/appointments": APPOINTMENTS,
                "/api/v2/payments": PAYMENTS,
            }.get(path)

            if table is not None:
                chunk, extra = self._page(table, query)
                body = {"data": chunk, "total": len(table)}
                body.update(extra)
                self._json(body)
                return

            self._json({"error": "not found"}, status=404)
            return

        self._send("<html><body>Not found</body></html>", status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if parsed.path in ("/login", "/login/"):
            # Form bodies are percent-encoded, so the '@' arrives as %40.
            form = parse_qs(raw)
            ok = (
                form.get("email", [""])[0] == USERNAME
                and form.get("password", [""])[0] == PASSWORD
            )
            if not ok:
                self._send(LOGIN_HTML, status=200)
                return
            payload = b""
            self.send_response(302)
            self.send_header("Location", "/app/")
            self.send_header(
                "Set-Cookie", f"{SESSION_COOKIE}={SESSION_VALUE}; Path=/; HttpOnly"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.write(payload)
            return

        if not self.signed_in:
            self._deny()
            return

        if parsed.path == "/api/v2/search":
            if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                self._json({"error": "missing csrf token"}, status=403)
                return
            try:
                body = json.loads(raw or "{}")
            except ValueError:
                body = {}
            page = int(body.get("page", 1))
            size = int(body.get("per_page", 2))
            start = (page - 1) * size
            self._json({"results": CUSTOMERS[start:start + size], "total": len(CUSTOMERS)})
            return

        self._json({"error": "not found"}, status=404)


class FixtureApp:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def cookie_header(self) -> str:
        return f"{SESSION_COOKIE}={SESSION_VALUE}"

    def __enter__(self) -> FixtureApp:
        self.thread.start()
        return self

    def __exit__(self, *_) -> None:
        self.server.shutdown()
        self.server.server_close()
