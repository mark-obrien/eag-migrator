"""Write side: through v3's HTTP API instead of its database.

Use this when you want v3's own validation and business logic to run on every
migrated record. Slower than the SQL sink, but the target can never end up in a
state v3 itself would reject.

Enable by setting V3_API_BASE_URL, and give each entity a `target.endpoint`.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..mapping import EntityMap
from .base import WriteResult

RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

# Envelope keys that carry the real outcome. An API that answers HTTP 200 with
# {"succeeded": false, "messages": [...]} is rejecting the write — recording
# that as an insert loses the record silently and leaves rollback nothing to
# undo, which is the worst failure this tool can have.
OK_KEYS = ("succeeded", "success", "ok", "isSuccess")
ERROR_KEYS = ("messages", "errors", "error", "message", "detail", "title")
# Where a created record's identifier tends to live.
ID_FIELDS = ("id", "key", "uuid", "guid", "_id")


# What a target says when the record is already there. There is no status code
# for it inside a 200 envelope, so the wording is all there is to go on — but
# matching a few phrases is far better than the alternative of calling every
# refusal a skip.
_CONFLICT_HINTS = ("already exists", "already registered", "duplicate", "conflict")


def _looks_like_conflict(problem: str | None) -> bool:
    return bool(problem) and any(hint in problem.lower() for hint in _CONFLICT_HINTS)


class BearerWithRefresh(httpx.Auth):
    """Bearer auth that renews itself when the target says the session is over.

    v3's access token lasts about an hour, which a migration of a few thousand
    records will outlive. `GET /api/V1/tokens/refresh/` answers with
    `Set-Cookie: AccessToken=…` — the body only says "ok" — so the new token is
    read off the response rather than out of JSON. httponly keeps it from
    browser scripts, not from a client like this one.

    The renewal window is fixed at login: refreshing issues a fresh token but
    does not extend the right to refresh again, so this buys hours, not
    forever. When the window has closed the 401 stands and the run stops with
    the target's own message rather than silently writing nothing.
    """

    requires_response_body = False

    def __init__(self, token: str | None, refresh_url: str | None,
                 cookie: str | None = None) -> None:
        self.token = token
        self.refresh_url = refresh_url
        self.cookie = cookie
        self.refreshes = 0

    def _apply(self, request: httpx.Request) -> None:
        if self.token:
            value = self.token
            request.headers["Authorization"] = (
                value if value.lower().startswith("bearer ") else f"Bearer {value}"
            )
        if self.cookie:
            request.headers["Cookie"] = self.cookie

    def auth_flow(self, request: httpx.Request) -> Any:
        self._apply(request)
        response = yield request
        if response.status_code != 401 or not self.refresh_url:
            return

        renewal = httpx.Request("GET", self.refresh_url)
        self._apply(renewal)
        renewed = yield renewal
        fresh = renewed.cookies.get("AccessToken") if renewed.status_code < 400 else None
        if not fresh:
            return  # the window has closed; let the original 401 stand

        self.token = fresh
        self.refreshes += 1
        self._apply(request)
        yield request


def build_client(
    base_url: str,
    token: str | None = None,
    timeout: int = 30,
    cookie: str | None = None,
    refresh_path: str | None = None,
) -> httpx.Client:
    """One place that knows how to authenticate against the target.

    Shared with `eagm api-get` / `api-post`, so probing the API exercises the
    same credentials and headers a real run would use.
    """
    # Accept only. Content-Type is NOT a default: httpx sets it on requests that
    # actually carry a body, and sending it on a bodyless GET makes v3 try to
    # parse nothing — "The input does not contain any JSON tokens" — so every
    # GET came back 422. That is how the snapshot reported "jobs: 0" against a
    # tenant holding 561 of them, and why the fix looked right when it was
    # checked against an empty tenant, where 0 was also the true answer.
    headers = {"Accept": "application/json"}
    if token:
        # Devtools shows the header as "Bearer eyJ…", so that whole string is
        # what gets pasted. Sending it unchanged would produce "Bearer Bearer
        # eyJ…" and a 401 with nothing to say why. Matches what the v2 side
        # already does in Session.from_env.
        headers["Authorization"] = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
    # Some APIs authenticate with an HttpOnly session cookie and issue no token
    # at all, so a signed-in browser session is the only credential available.
    if cookie:
        headers["Cookie"] = cookie
    base = base_url.rstrip("/")
    if refresh_path:
        # The credential moves onto the auth flow so it can be replaced mid-run;
        # leaving it in the default headers as well would pin the stale one.
        headers.pop("Authorization", None)
        headers.pop("Cookie", None)
        return httpx.Client(
            base_url=base,
            headers=headers,
            timeout=timeout,
            auth=BearerWithRefresh(token, base + refresh_path, cookie),
        )
    return httpx.Client(base_url=base, headers=headers, timeout=timeout)


class ApiSink:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: int = 30,
        id_field: str | None = None,
        cookie: str | None = None,
        refresh_path: str | None = None,
    ) -> None:
        self.client = build_client(base_url, token, timeout, cookie, refresh_path)
        self.id_field = id_field

    def columns(self, entity: EntityMap) -> list[str]:
        # No schema to introspect over HTTP; column validation is skipped.
        return []

    def _endpoint(self, entity: EntityMap) -> str:
        if not entity.target.endpoint:
            raise ValueError(
                f"entity '{entity.name}': the API sink needs target.endpoint "
                f"(e.g. /api/v3/{entity.target.table})"
            )
        return entity.target.endpoint

    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return self.client.post(path, json=payload)

    def write_batch(
        self,
        entity: EntityMap,
        rows: list[tuple[Any, dict[str, Any]]],
        pre_commit: Callable[[list[WriteResult]], None] | None = None,
    ) -> list[WriteResult]:
        path = self._endpoint(entity)
        results: list[WriteResult] = []

        for source_id, row in rows:
            try:
                resp = self._post(path, _jsonify(row))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    WriteResult(source_id, None, "failed", error=f"{type(exc).__name__}: {exc}", payload=row)
                )
                continue

            # A 409 is "already there" — a real skip under conflict: skip. A 422
            # is the server rejecting the payload (a required field missing, a
            # bad value), which is a FAILURE, not a skip, whatever the conflict
            # policy says. Lumping 422 in with 409 hid 59 rejected records in a
            # production run as harmless "skipped" counts — the same mistake as
            # treating a succeeded:false envelope as a skip, one layer down.
            if resp.status_code == 409 and entity.target.conflict == "skip":
                results.append(WriteResult(source_id, None, "skipped", payload=row))
                continue

            if resp.status_code >= 400:
                results.append(
                    WriteResult(
                        source_id,
                        None,
                        "failed",
                        error=f"HTTP {resp.status_code}: {resp.text[:300]}",
                        payload=row,
                    )
                )
                continue

            try:
                body = resp.json()
            except Exception:  # noqa: BLE001 - a 2xx with no JSON body is still a success
                results.append(WriteResult(source_id, None, "inserted", payload=row))
                continue

            ok, problem, record = _unwrap(body)
            if not ok:
                # `conflict: skip` means "that record is already there", not
                # "ignore anything the server objects to". Treating every
                # rejection as a skip hides validation errors behind a clean
                # run: a rehearsal of 961 customers came back ok/961-skipped
                # while the API had refused every single one for a missing
                # field. A conflict is a skip; everything else is a failure,
                # whatever the policy says.
                if entity.target.conflict == "skip" and _looks_like_conflict(problem):
                    results.append(WriteResult(source_id, None, "skipped", payload=row))
                else:
                    results.append(
                        WriteResult(
                            source_id, None, "failed",
                            error=f"HTTP {resp.status_code} but rejected: {problem}",
                            payload=row,
                        )
                    )
                continue

            results.append(
                WriteResult(source_id, _find_id(record, self.id_field), "inserted", payload=row)
            )

        # There is no transaction to hold open over HTTP: writes are already
        # durable by the time we get here, so journalling happens after.
        if pre_commit is not None:
            pre_commit(results)
        return results

    def undo(
        self, entity_name: str, table: str, key_column: str, entries: list[dict[str, Any]]
    ) -> int:
        """DELETE each inserted row against the entity's real endpoint.

        Two things this refuses to do, both learned the hard way. It will not
        guess a path: `/<table>` is not `/api/V1/<table>`, and the host's
        front end answers unknown paths with 200, so a guessed DELETE reported
        success while removing nothing. And a 2xx is not taken at face value:
        that same catch-all serves HTML, and v3 can answer 200 with
        `succeeded: false`. A row counts as undone only when the target says
        so in JSON (or with a bare 204), or says it was already gone.
        """
        affected = 0
        for entry in entries:
            if entry["action"] != "inserted" or entry.get("target_id") in (None, "", "None"):
                continue
            endpoint = entry.get("endpoint")
            if not endpoint:
                continue  # nothing to undo against; leave the journal intact
            try:
                resp = self.client.delete(f"{endpoint.rstrip('/')}/{entry['target_id']}")
            except Exception:  # noqa: BLE001 - report totals, do not abort the rollback
                continue
            if resp.status_code == 404 or resp.status_code == 204:
                affected += 1
                continue
            if resp.status_code >= 400:
                continue
            if "json" not in resp.headers.get("content-type", ""):
                continue  # an HTML 200 is the catch-all, not a deletion
            try:
                body = resp.json()
            except ValueError:
                continue
            ok, _problem, _record = _unwrap(body)
            if ok:
                affected += 1
        return affected

    def close(self) -> None:
        self.client.close()


def _unwrap(body: Any) -> tuple[bool, str | None, Any]:
    """Read an envelope's real outcome: (accepted, why not, the record).

    A body with a boolean outcome key decides for itself. Anything else is a
    2xx taken at face value.
    """
    if not isinstance(body, dict):
        return True, None, body

    for name in OK_KEYS:
        flag = body.get(name)
        if isinstance(flag, bool):
            if flag:
                return True, None, body.get("data", body)
            return False, _problem(body) or f"{name}=false", body

    return True, None, body


def _problem(body: dict[str, Any]) -> str:
    for name in ERROR_KEYS:
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
        if isinstance(value, (list, tuple)) and value:
            return "; ".join(str(v) for v in value)[:300]
        if isinstance(value, dict) and value:
            return str(value)[:300]
    return ""


def _find_id(record: Any, preferred: str | None) -> Any:
    """The identifier the target assigned, so the id map can point at it."""
    if isinstance(record, list):
        record = record[0] if len(record) == 1 else None
    # Some endpoints answer with the new key by itself rather than the record:
    # v3's POST /api/V1/jobs returns {"data": "01a089eb-8b33-...", "succeeded":
    # true}, where the customer endpoint returns the whole object. Without this
    # every job was recorded with no target id, which leaves rollback unable to
    # delete what it wrote and a re-run unable to tell the job was already
    # migrated — the exact hole that made the previous import unrecoverable.
    if isinstance(record, (str, int)) and str(record).strip():
        return record
    if not isinstance(record, dict):
        return None
    if preferred:
        return record.get(preferred)
    for name in ID_FIELDS:
        if record.get(name) not in (None, ""):
            return record[name]
    return None


def _jsonify(row: dict[str, Any]) -> dict[str, Any]:
    import datetime as dt
    import decimal

    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            out[key] = value.isoformat()
        elif isinstance(value, decimal.Decimal):
            out[key] = str(value)
        elif isinstance(value, (bytes, bytearray)):
            out[key] = value.decode("utf-8", "replace")
        else:
            out[key] = value
    return out
