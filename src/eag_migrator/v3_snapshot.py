"""Read v3 as it actually is, into local files you can analyze.

Read-only. Every request is a GET, or a search POST that only returns a list,
so this never changes the tenant. It pulls the reference tables the mapping
has to point at — pricing profiles, locations, payment terms, users — with
their *real* UUIDs and codes, plus whatever customers and jobs already exist,
and writes each to `reports/v3-snapshot/` as JSON to look through.

It also writes a combined `state/v3_snapshot.json` of just the reference data,
which the local mock serves in place of its synthetic seed — so a rehearsal
runs against v3's true ids, and the mapping you validate is the one that will
work in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters.api_sink import _unwrap

# Which snapshot keys are reference data the mock should serve.
REFERENCE_KEYS = ("pricing_profiles", "locations", "payment_terms", "users", "installers")


@dataclass
class Target:
    name: str
    path: str
    method: str = "GET"
    paged: bool = False
    body: dict | None = None


# The endpoints the survey found. Reference first (small, always useful), then
# the record lists (paged).
TARGETS = [
    Target("pricing_profiles", "/api/V1/pricing-profiles"),
    Target("locations", "/api/V1/locations/names/"),
    Target("payment_terms", "/api/V1/customers/paymentterms/"),
    Target("users", "/api/V1/identity/users"),
    Target("installers", "/api/V1/identity/users/installers"),
    Target("profile", "/api/V1/identity/profile"),
    Target("products", "/api/V1/products"),
    Target("customers", "/api/V1/customers", paged=True),
    # GET, not POST /api/V1/jobs/search. The search endpoint answers an empty
    # body with an empty list, so the snapshot reported "jobs: 0" on a tenant
    # holding 561 of them — and reporting an empty collection as a fact is
    # worse than failing, because it reads as "v3 has no jobs yet" and hides
    # the schema. `OPTIONS /api/V1/jobs` allows GET and POST; GET returns the
    # lot. Presumably search wants criteria nobody has established.
    Target("jobs", "/api/V1/jobs", paged=True),
]

MAX_PAGES = 1000


def _fetch(client: Any, target: Target) -> dict[str, Any]:
    """One endpoint, following pagination. Returns {ok, data, problem, pages}."""
    if not target.paged:
        try:
            resp = client.request(target.method, target.path, json=target.body)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "problem": f"{type(exc).__name__}: {exc}", "data": None}
        try:
            ok, problem, data = _unwrap(resp.json())
        except ValueError:
            return {"ok": False, "problem": f"HTTP {resp.status_code}, not JSON", "data": None}
        return {"ok": ok, "problem": problem, "data": data, "pages": 1}

    records: list[Any] = []
    page = 1
    while page <= MAX_PAGES:
        path = target.path
        body = dict(target.body or {})
        if target.method == "GET":
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}page={page}&pageSize=100"
        else:
            body.update({"page": page, "pageSize": 100})
        try:
            resp = client.request(target.method, path, json=body if target.method != "GET" else None)
            ok, problem, data = _unwrap(resp.json())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "problem": f"{type(exc).__name__}: {exc}",
                    "data": records, "pages": page}
        if not ok:
            return {"ok": False, "problem": problem, "data": records, "pages": page}

        chunk = data.get("data") if isinstance(data, dict) else data
        records.extend(chunk or [])
        if not (isinstance(data, dict) and data.get("hasNextPage")):
            break
        page += 1
    return {"ok": True, "problem": None, "data": records, "pages": page}


def snapshot(client: Any, say: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Pull every target. Returns {name: {ok, data, problem, pages}}."""
    out: dict[str, Any] = {}
    for target in TARGETS:
        result = _fetch(client, target)
        out[target.name] = result
        count = _count(result["data"])
        if result["ok"]:
            say(f"{target.name}: {count} record(s)")
        else:
            say(f"{target.name}: {result['problem']}")
    return out


def _count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1
    return 0


def save(result: dict[str, Any], reports_dir: Path, snapshot_file: Path) -> dict[str, int]:
    """Write per-endpoint JSON for analysis, and the reference file for the mock."""
    out_dir = reports_dir / "v3-snapshot"
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, res in result.items():
        (out_dir / f"{name}.json").write_text(
            json.dumps(res["data"], indent=2, default=str), encoding="utf-8"
        )
        counts[name] = _count(res["data"])

    reference = {
        key: result[key]["data"]
        for key in REFERENCE_KEYS
        if result.get(key, {}).get("ok") and isinstance(result[key]["data"], list)
    }
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(reference, indent=2, default=str), encoding="utf-8")
    return counts


def field_keys(result: dict[str, Any]) -> dict[str, list[str]]:
    """The columns each collection actually has — the shape, for analysis."""
    keys: dict[str, list[str]] = {}
    for name, res in result.items():
        rows = res["data"]
        sample = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else None)
        if isinstance(sample, dict):
            keys[name] = sorted(sample)
    return keys


def load_reference(snapshot_file: Path) -> dict[str, list] | None:
    """The reference data the mock serves, if a snapshot has been taken."""
    if not snapshot_file.exists():
        return None
    try:
        data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
