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
  * a server-rendered list screen under /legacy — no JSON anywhere, which is
    what an older v2 install actually looks like
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

# A navigable app shell: each screen loads only its own endpoint over XHR, and
# the nav includes the destructive links a real ops app has — an explore pass
# must find the screens and leave those alone.
SCREEN_HTML = """<!DOCTYPE html>
<html><head><title>%(title)s — Clearview Ops</title></head>
<body>
<nav class="dashboard">
  <a href="/customers">Customers</a>
  <a href="/quotes">Quotes</a>
  <a href="/schedule">Schedule</a>
  <a href="/payments">Payments</a>
  <a href="/settings">Settings</a>
  <a href="/quotes/9001/delete">Delete quote</a>
  <a href="/quotes/9001/send">Email to customer</a>
  <a href="/payments/3001/refund">Refund</a>
  <a href="/customers/5001/archive" data-confirm="Are you sure?">Archive</a>
  <a href="/subscriptions/1" data-method="delete">Cancel plan</a>
  <a href="/reports/quotes.pdf">Download PDF</a>
  <a href="mailto:ops@clearview.example">Contact</a>
  <a href="https://example.org/external">External</a>
  <a href="/logout">Log out</a>
</nav>
<div id="root">Loading…</div>
<script>
const H = {'%(csrf_header)s': '%(csrf_value)s', 'Accept': 'application/json'};
fetch('%(endpoint)s', {headers: H}).then(r => r.json())
  .then(d => { document.getElementById('root').textContent = 'ready'; });
</script>
</body></html>"""

SCREENS = {
    "/": ("Dashboard", "/api/v2/session"),
    "/customers": ("Customers", "/api/v2/customers?page=1&per_page=2"),
    "/quotes": ("Quotes", "/api/v2/quotes?limit=2&offset=0"),
    "/schedule": ("Schedule", "/api/v2/appointments?limit=2"),
    "/payments": ("Payments", "/api/v2/payments?page=1&per_page=50"),
    "/settings": ("Settings", "/api/v2/session"),
}

# The original single-page shell, kept for the tests that drive /app/ directly.
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


# The server-rendered half of the app: one table, many records, paginated with
# ordinary links. No fetch(), no JSON — extraction has to come out of the HTML.
LIST_HTML = """<!DOCTYPE html>
<html><head><title>Customers — Clearview Ops</title></head>
<body>
<nav><a href="/legacy/customers">Customers</a> <a href="/logout">Log out</a></nav>
<table class="listing">
  <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>ZIP</th><th></th></tr></thead>
  <tbody>
%(rows)s
  </tbody>
</table>
%(pager)s
</body></html>"""

LIST_ROW = """    <tr class="customer" data-id="%(id)s">
      <td class="name"><a href="/legacy/customers/%(id)s">%(name)s</a></td>
      <td class="email">%(email)s</td>
      <td class="phone">%(phone)s</td>
      <td class="zip">%(postal_code)s</td>
      <td><a href="/legacy/customers/%(id)s/delete" data-confirm="Delete?">Delete</a></td>
    </tr>"""

LIST_PAGE_SIZE = 2

# A record detail page. The nasty part is what a *missing* id returns: HTTP
# 200 and a blank editable form, so the status code says nothing and the only
# evidence is an empty data-job-id. Real apps do this.
DETAIL_HTML = """<!DOCTYPE html>
<html><head><title>Job — Clearview Ops</title></head><body>
<div class="job" data-job-id="%(id)s">
  <p class="tab-card-header">First Name</p>
  <p class="tab-card-content" id="job-customer-fname">%(fname)s</p>
  <p class="tab-card-content" id="job-customer-lname">%(lname)s</p>
  <p class="tab-card-content" id="job-vehicle-vin">%(vin)s</p>
  <div class="part-numbers">%(parts)s</div>
</div>
</body></html>"""

DETAIL_PART = """
    <div class="part-number tab-sub-container">
      <div class="job-nags-part-number">%(part)s</div>
      <p class="job-nags-part-description">%(desc)s</p>
    </div>"""

# Ids 7001-7003 exist (they match APPOINTMENTS), with a gap at 7004+.
# Which customer each job belongs to. The app knows; it just never says so in
# a way you could join on. 5004 and 5005 own nothing.
RELATED = {7001: 5001, 7002: 5001, 7003: 5003}

JOB_PARTS = {
    7001: [("dw01234", "Windshield"), ("cal-001", "Calibration")],
    7002: [("fw02345", "Door glass")],
    7003: [],
}

# Every destructive URL the fixture is asked to serve lands here. A crawl that
# leaves this empty is the whole point; asserting on it beats inferring from
# response bodies.
TOUCHED: list[str] = []


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

        if path in ("/app", "/app/"):
            self._send(APP_HTML % {"csrf_header": CSRF_HEADER, "csrf_value": CSRF_VALUE})
            return

        # An offset in the path, a fixed page size, no next-link — the shape a
        # PHP-era list screen actually has. Ids are sparse on the detail route
        # below, because records get deleted.
        if path.startswith("/offset/customers"):
            tail = path[len("/offset/customers"):].strip("/")
            offset = int(tail) if tail.isdigit() else 0
            chunk = CUSTOMERS[offset:offset + LIST_PAGE_SIZE]
            self._send(LIST_HTML % {
                "rows": "\n".join(LIST_ROW % c for c in chunk),
                "pager": "",
            })
            return

        # A per-customer fetch, keyed on a customer id that is nowhere near a
        # contiguous range. It lists the customer's jobs — and, like the real
        # thing, the rows carry no job ids at all.
        if path.startswith("/related/"):
            wanted = path.rsplit("/", 1)[-1]
            owned = [a for a in APPOINTMENTS
                     if str(RELATED.get(a["id"], "")) == wanted]
            self._send(
                '<div id="tab-job"><div class="nags-table">'
                '<div class="nags-row nags-header-row">'
                '<div class="nags-column">Vehicle</div>'
                '<div class="nags-column">Stage</div></div>'
                + "".join(
                    f'<div class="nags-row"><div class="nags-column">{a["technician"]}'
                    f'</div><div class="nags-column">{a["state"]}</div></div>'
                    for a in owned
                )
                + "</div></div>"
            )
            return

        if path.startswith("/job/manage/"):
            wanted = path.rsplit("/", 1)[-1]
            job = next((a for a in APPOINTMENTS if str(a["id"]) == wanted), None)
            if job is None:
                # The blank shell. 200, no error, empty id — indistinguishable
                # from a real record by anything but the content.
                self._send(DETAIL_HTML % {
                    "id": "", "fname": "", "lname": "", "vin": "", "parts": "",
                })
                return
            quote = next((q for q in QUOTES if q["id"] == job["quote_id"]), {})
            customer = (quote.get("customer") or {}).get("name", " ").split(" ", 1)
            self._send(DETAIL_HTML % {
                "id": job["id"],
                "fname": customer[0],
                "lname": customer[1] if len(customer) > 1 else "",
                "vin": quote.get("vin") or "",
                "parts": "".join(
                    DETAIL_PART % {"part": p, "desc": d}
                    for p, d in JOB_PARTS.get(job["id"], [])
                ),
            })
            return

        if path.startswith("/record/"):
            wanted = path.rsplit("/", 1)[-1]
            found = next((c for c in CUSTOMERS if str(c["id"]) == wanted), None)
            if not found:
                # Present, but empty — the awkward case. A 404 is easy to spot;
                # a 200 holding no records is what actually ends a walk.
                self._send(LIST_HTML % {"rows": "", "pager": ""})
                return
            self._send(LIST_HTML % {"rows": LIST_ROW % found, "pager": ""})
            return

        if path.rstrip("/") == "/legacy/customers":
            page = int((query.get("page") or ["1"])[0])
            start = (page - 1) * LIST_PAGE_SIZE
            chunk = CUSTOMERS[start:start + LIST_PAGE_SIZE]
            pager = ""
            if start + LIST_PAGE_SIZE < len(CUSTOMERS):
                pager = f'<a href="/legacy/customers?page={page + 1}" rel="next">Next</a>'
            self._send(LIST_HTML % {
                "rows": "\n".join(LIST_ROW % c for c in chunk),
                "pager": pager,
            })
            return

        screen = SCREENS.get(path.rstrip("/") or "/")
        if screen:
            title, endpoint = screen
            self._send(SCREEN_HTML % {
                "title": title, "endpoint": endpoint,
                "csrf_header": CSRF_HEADER, "csrf_value": CSRF_VALUE,
            })
            return

        # Anything an explore pass should never reach. Reaching one is a bug.
        if any(k in path for k in
               ("delete", "send", "refund", "archive", "logout", "subscriptions")):
            TOUCHED.append(path)
            self._send("<html><body>DESTRUCTIVE ACTION PERFORMED</body></html>")
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
