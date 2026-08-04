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


class ApiSink:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: int = 30,
        id_field: str | None = None,
        cookie: str | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Some APIs authenticate with an HttpOnly session cookie and issue no
        # token at all, so a signed-in browser session is the only credential
        # available.
        if cookie:
            headers["Cookie"] = cookie
        self.client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)
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

            if resp.status_code in (409, 422) and entity.target.conflict == "skip":
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
                if entity.target.conflict == "skip":
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
        """Best-effort DELETE against the same endpoint. Requires v3 to expose one."""
        affected = 0
        for entry in entries:
            if entry["action"] != "inserted" or entry.get("target_id") in (None, "None"):
                continue
            endpoint = entry.get("endpoint") or f"/{table}"
            try:
                resp = self.client.delete(f"{endpoint.rstrip('/')}/{entry['target_id']}")
                if resp.status_code < 400 or resp.status_code == 404:
                    affected += 1
            except Exception:  # noqa: BLE001 - report totals, do not abort the rollback
                continue
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
