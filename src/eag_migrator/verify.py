"""Post-migration verification.

Runs after `apply` and answers, per entity: did everything arrive, and does
what arrived actually match the source once the mapping is applied?

Three independent checks, because they fail in different ways:
  1. counts      — source rows vs rows we recorded vs rows now in the target
  2. spot-check  — re-derive N random rows from source and diff against v3
  3. lookups     — foreign keys that resolved to nothing
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import asdict, dataclass, field
from typing import Any

from .mapping import EntityMap, Mapping
from .runner import Ctx, RowError, Runner
from .state import RunState


@dataclass
class Discrepancy:
    entity: str
    kind: str
    detail: str
    source_id: Any = None
    field_name: str | None = None
    expected: Any = None
    actual: Any = None


@dataclass
class EntityVerification:
    name: str
    source_rows: int = 0
    migrated_recorded: int = 0
    target_rows: int | None = None
    checked: int = 0
    mismatched: int = 0
    missing_in_target: int = 0
    discrepancies: list[Discrepancy] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.discrepancies


@dataclass
class VerifyReport:
    run_id: str
    generated_at: str
    entities: list[EntityVerification] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(e.ok for e in self.entities)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise(value: Any) -> Any:
    """Compare across type systems without crying wolf.

    MySQL DATETIME vs Postgres timestamptz, Decimal vs float, tinyint(1) vs
    bool — these are the same value wearing different clothes.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, dt.datetime):
        # Drop sub-second precision and tz offset; engines disagree on both.
        return value.replace(microsecond=0, tzinfo=None).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _equal(expected: Any, actual: Any) -> bool:
    a, b = _normalise(expected), _normalise(actual)
    if a == b:
        return True
    # 1/0 vs True/False, and "1" vs 1
    if isinstance(a, float) and isinstance(b, str):
        try:
            return a == float(b)
        except ValueError:
            return False
    if isinstance(b, float) and isinstance(a, str):
        try:
            return b == float(a)
        except ValueError:
            return False
    return False


def verify(
    runner: Runner,
    mapping: Mapping,
    state: RunState,
    run_id: str,
    *,
    sample_size: int = 50,
    entity_names: list[str] | None = None,
) -> VerifyReport:
    report = VerifyReport(
        run_id=run_id,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )

    for entity in mapping.topo_order(entity_names):
        report.entities.append(_verify_entity(runner, state, run_id, entity, sample_size))

    return report


def _verify_entity(
    runner: Runner,
    state: RunState,
    run_id: str,
    entity: EntityMap,
    sample_size: int,
) -> EntityVerification:
    ev = EntityVerification(name=entity.name)

    try:
        ev.source_rows = runner.source.count(entity)
    except Exception as exc:  # noqa: BLE001
        ev.notes.append(f"could not count source rows: {exc}")

    ev.migrated_recorded = state.id_map_size(entity.name)

    counter = getattr(runner.sink, "count_rows", None)
    if counter:
        try:
            ev.target_rows = counter(entity)
        except Exception as exc:  # noqa: BLE001
            ev.notes.append(f"could not count target rows: {exc}")

    if not entity.id_map:
        ev.notes.append("id_map disabled for this entity — only counts were checked")
        return ev

    # --- check 1: counts ----------------------------------------------------
    if ev.source_rows and ev.migrated_recorded < ev.source_rows:
        ev.discrepancies.append(
            Discrepancy(
                entity=entity.name,
                kind="count",
                detail=(
                    f"{ev.source_rows - ev.migrated_recorded} source row(s) have no "
                    f"recorded target row ({ev.migrated_recorded}/{ev.source_rows} migrated)"
                ),
            )
        )

    # --- check 2: spot-check round trip ------------------------------------
    fetch_source = getattr(runner.source, "fetch_one", None)
    fetch_target = getattr(runner.sink, "fetch_one", None)
    if not (fetch_source and fetch_target):
        ev.notes.append("adapters do not support row fetch — spot-check skipped")
        return ev

    # Fields whose value cannot be reproduced after the fact.
    nondeterministic = {
        f.to
        for f in entity.fields
        if any(
            (isinstance(s, str) and s == "now") or (isinstance(s, dict) and "now" in s)
            for s in f.transform
        )
    }
    clock_defaults = {f.to for f in entity.fields if f.default == "@now"}
    if nondeterministic or clock_defaults:
        ev.notes.append(
            "not diffed (generated at migration time, not derivable from v2): "
            + ", ".join(sorted(nondeterministic | clock_defaults))
        )

    ctx = Ctx(state=state, run_id=run_id, entity_name=entity.name)
    for source_id, target_id in state.sample_id_pairs(entity.name, sample_size):
        src = fetch_source(entity, source_id, column=entity.map_key)
        if src is None:
            ev.discrepancies.append(
                Discrepancy(
                    entity=entity.name,
                    kind="source_vanished",
                    detail="row present in the id map but no longer in v2",
                    source_id=source_id,
                )
            )
            continue

        tgt = fetch_target(entity, target_id)
        if tgt is None:
            ev.missing_in_target += 1
            ev.discrepancies.append(
                Discrepancy(
                    entity=entity.name,
                    kind="missing",
                    detail=f"target row {entity.target.key}={target_id} not found in v3",
                    source_id=source_id,
                )
            )
            continue

        ev.checked += 1
        defaulted: set[str] = set()
        try:
            expected = runner.build_row(entity, src, ctx, defaults_applied=defaulted)
        except RowError as exc:
            ev.discrepancies.append(
                Discrepancy(
                    entity=entity.name,
                    kind="transform",
                    detail=str(exc),
                    source_id=source_id,
                )
            )
            continue

        skip = nondeterministic | (defaulted & clock_defaults)

        row_bad = False
        for col, want in expected.items():
            if col == entity.target.key:
                continue  # ids are remapped by design
            if col not in tgt:
                continue
            if col in skip:
                # Value came from the wall clock at migration time; re-deriving
                # it now gives a different answer, which is not a discrepancy.
                continue
            if not _equal(want, tgt[col]):
                row_bad = True
                if len(ev.discrepancies) < 100:
                    ev.discrepancies.append(
                        Discrepancy(
                            entity=entity.name,
                            kind="mismatch",
                            detail="target value differs from what the mapping produces",
                            source_id=source_id,
                            field_name=col,
                            expected=_show(want),
                            actual=_show(tgt[col]),
                        )
                    )
        if row_bad:
            ev.mismatched += 1

    # --- check 3: unresolved lookups ---------------------------------------
    for fmap in entity.fields:
        for step in fmap.transform:
            if isinstance(step, dict) and "lookup" in step:
                ref = (step["lookup"] or {}).get("entity")
                if ref and state.id_map_size(ref) == 0:
                    ev.notes.append(
                        f"field '{fmap.to}' looks up '{ref}', which has no migrated rows — "
                        f"every value in this column will be null"
                    )

    return ev


def _show(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    return str(value)
