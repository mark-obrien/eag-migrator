"""The migration engine: plan (dry run) and apply.

Guarantees this is built around:

  * Dry run       `plan()` reads and transforms everything but writes nothing.
  * Resumable     progress is checkpointed per entity by source key.
  * Idempotent    re-running skips rows already recorded in the id map.
  * Reversible    every write is journalled before the target commits.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from .adapters.base import Sink, Source, WriteResult
from .config import Settings
from .mapping import EntityMap, FieldMap, Mapping
from .state import Checkpoint, RunState
from .transforms import TransformError, apply_chain


class RowError(Exception):
    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field_name = field_name
        self.message = message


@dataclass
class Ctx:
    """Per-entity context handed to transforms."""

    state: RunState
    run_id: str
    entity_name: str
    plan_mode: bool = False
    unresolved_lookups: set[str] = field(default_factory=set)

    def note_unresolved_lookup(self, entity: str) -> None:
        self.unresolved_lookups.add(entity)


@dataclass
class EntityOutcome:
    name: str
    source_table: str
    target_table: str
    total_source_rows: int = 0
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    already_migrated: int = 0
    existing_in_target: int | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.aborted


@dataclass
class Report:
    mode: str
    run_id: str
    mapping_hash: str
    started_at: str
    finished_at: str | None = None
    entities: list[EntityOutcome] = field(default_factory=list)
    fatal: str | None = None

    @property
    def total_failed(self) -> int:
        return sum(e.failed for e in self.entities)

    @property
    def total_written(self) -> int:
        return sum(e.inserted + e.updated for e in self.entities)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


NOW_TOKEN = "@now"


class Runner:
    def __init__(
        self,
        settings: Settings,
        mapping: Mapping,
        source: Source,
        sink: Sink,
        state: RunState,
        *,
        progress: Callable[[str, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.mapping = mapping
        self.source = source
        self.sink = sink
        self.state = state
        self.progress = progress or (lambda *_: None)
        # Checked between batches. Stopping there is clean: the batch has
        # committed and been checkpointed, so `--resume` picks up exactly where
        # it left off.
        self.should_stop = should_stop or (lambda: False)

    # --- row building -------------------------------------------------------

    def build_row(
        self,
        entity: EntityMap,
        src: dict[str, Any],
        ctx: Ctx,
        *,
        defaults_applied: set[str] | None = None,
    ) -> dict[str, Any]:
        """Turn one v2 row into one v3 row.

        defaults_applied, if given, collects the fields that fell back to their
        `default:`. Verification needs this: a field defaulted to "@now" cannot
        be re-derived later and must not be diffed.
        """
        out: dict[str, Any] = {}
        # Only subtrees built from a nested `to:` are pruned. A flat field whose
        # value happens to be a list — `from: [a, b]` with no transform — is left
        # exactly as it was.
        nested_roots: set[str] = set()
        # Leaf names under each root that came from `const:` — a type code or a
        # hardcoded country, set whether or not the entry holds anything.
        const_leaves: dict[str, set[str]] = {}
        for fmap in entity.fields:
            value = self._extract(fmap, src)
            try:
                if fmap.transform:
                    value = apply_chain(value, fmap.transform, row=src, ctx=ctx)
            except TransformError as exc:
                raise RowError(fmap.to, str(exc)) from exc

            if value is None and fmap.default is not None:
                value = _resolve_default(fmap.default)
                if defaults_applied is not None:
                    defaults_applied.add(fmap.to)

            if fmap.required and value is None:
                raise RowError(fmap.to, "required field resolved to null")

            if _is_nested(fmap.to):
                tokens = _path_tokens(fmap.to)
                root = str(tokens[0])
                _assign_path(out, fmap.to, value)
                nested_roots.add(root)
                if fmap.const is not None:
                    const_leaves.setdefault(root, set()).add(str(tokens[-1]))
            else:
                out[fmap.to] = value
        for root in nested_roots:
            out[root] = _prune(out[root], frozenset(const_leaves.get(root, ())))
        return out

    @staticmethod
    def _extract(fmap: FieldMap, src: dict[str, Any]) -> Any:
        if fmap.const is not None:
            return fmap.const
        if fmap.from_ is None:
            return None
        if isinstance(fmap.from_, list):
            missing = [c for c in fmap.from_ if c not in src]
            if missing:
                raise RowError(fmap.to, f"source columns not found: {', '.join(missing)}")
            return [src[c] for c in fmap.from_]
        if fmap.from_ not in src:
            raise RowError(fmap.to, f"source column '{fmap.from_}' not found")
        return src[fmap.from_]

    # --- dry run ------------------------------------------------------------

    def plan(self, entity_names: list[str] | None = None, limit: int = 0) -> Report:
        run_id = self.state.start_run("plan", self.mapping.fingerprint())
        report = Report(
            mode="plan",
            run_id=run_id,
            mapping_hash=self.mapping.fingerprint(),
            started_at=_now_iso(),
        )
        try:
            for entity in self.mapping.topo_order(entity_names):
                report.entities.append(self._plan_entity(entity, run_id, limit))
        except Exception as exc:  # noqa: BLE001
            report.fatal = f"{type(exc).__name__}: {exc}"

        report.finished_at = _now_iso()
        self.state.finish_run(run_id, "failed" if report.fatal else "completed")
        return report

    def _plan_entity(self, entity: EntityMap, run_id: str, limit: int) -> EntityOutcome:
        out = EntityOutcome(
            name=entity.name,
            source_table=entity.source.table,
            target_table=entity.target.table,
        )
        if entity.note:
            out.notes.append(entity.note)
        for fmap in entity.fields:
            if fmap.note:
                out.notes.append(f"{fmap.to}: {fmap.note}")

        ctx = Ctx(state=self.state, run_id=run_id, entity_name=entity.name, plan_mode=True)

        # Structural checks first — these are mapping bugs, not data problems.
        try:
            src_cols = set(self.source.columns(entity))
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f"cannot read source table '{entity.source.table}': {exc}")
            out.aborted = True
            return out

        referenced: set[str] = set()
        for fmap in entity.fields:
            if isinstance(fmap.from_, list):
                referenced.update(fmap.from_)
            elif fmap.from_:
                referenced.add(fmap.from_)
        missing = sorted(referenced - src_cols)
        if missing:
            out.notes.append(
                f"mapping reads columns absent from '{entity.source.table}': {', '.join(missing)}"
            )
            out.aborted = True
            return out

        if entity.source.key not in src_cols:
            out.notes.append(f"source.key '{entity.source.key}' is not a column of the source table")
            out.aborted = True
            return out

        try:
            tgt_cols = set(self.sink.columns(entity))
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f"cannot read target table '{entity.target.table}': {exc}")
            tgt_cols = set()
        if tgt_cols:
            unknown = sorted({f.to for f in entity.fields} - tgt_cols)
            if unknown:
                out.notes.append(
                    f"mapping writes columns absent from '{entity.target.table}': "
                    f"{', '.join(unknown)}"
                )
                out.aborted = True
                return out
            # Leaving the target's own key unmapped is the normal case — v3
            # generates it — so it is not worth reporting.
            unmapped = sorted(
                tgt_cols - {f.to for f in entity.fields} - {entity.target.key}
            )
            if unmapped:
                out.notes.append(f"target columns left unmapped: {', '.join(unmapped)}")

        try:
            out.total_source_rows = self.source.count(entity)
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f"row count failed: {exc}")

        batch_size = entity.batch_size or self.settings.batch_size
        built: list[dict[str, Any]] = []

        for batch in self.source.stream(entity, after_key=None, batch_size=batch_size):
            for src in batch:
                source_id = src.get(entity.map_key)
                out.processed += 1
                try:
                    row = self.build_row(entity, src, ctx)
                except RowError as exc:
                    out.failed += 1
                    if len(out.errors) < 50:
                        out.errors.append(
                            {"source_id": _s(source_id), "field": exc.field_name, "error": exc.message}
                        )
                    self.state.record_error(
                        run_id, entity.name, source_id, "transform", str(exc)
                    )
                    continue

                built.append(row)
                if len(out.samples) < 5:
                    out.samples.append({k: _s(v) for k, v in row.items()})

                if limit and out.processed >= limit:
                    break
            if limit and out.processed >= limit:
                break
            if self.should_stop():
                out.notes.append(f"stopped on request after {out.processed:,} row(s)")
                out.aborted = True
                break

        # How many of these would collide with rows already in v3?
        probe = getattr(self.sink, "probe_existing", None)
        if probe and built:
            try:
                keys = [r.get(entity.target.key) for r in built]
                out.existing_in_target = probe(entity, [k for k in keys if k is not None])
            except Exception as exc:  # noqa: BLE001
                out.notes.append(f"could not probe target for existing rows: {exc}")

        if ctx.unresolved_lookups:
            out.notes.append(
                "lookups not verifiable in a dry run (nothing migrated yet): "
                + ", ".join(sorted(ctx.unresolved_lookups))
            )

        if out.existing_in_target:
            out.notes.append(
                f"{out.existing_in_target} row(s) already exist in the target; "
                f"conflict policy is '{entity.target.conflict}'"
            )

        return out

    # --- apply --------------------------------------------------------------

    def apply(
        self,
        entity_names: list[str] | None = None,
        *,
        resume_run_id: str | None = None,
        limit: int = 0,
    ) -> Report:
        if resume_run_id:
            existing = self.state.get_run(resume_run_id)
            if not existing:
                raise ValueError(f"no such run: {resume_run_id}")
            if existing["mapping_hash"] != self.mapping.fingerprint():
                raise ValueError(
                    f"run {resume_run_id} used a different mapping "
                    f"({existing['mapping_hash']} vs {self.mapping.fingerprint()}). "
                    f"Resuming across mapping edits would produce inconsistent rows; "
                    f"start a fresh run instead."
                )
            run_id = resume_run_id
            self.state.conn.execute(
                "UPDATE runs SET status = 'running', finished_at = NULL WHERE run_id = ?",
                (run_id,),
            )
        else:
            run_id = self.state.start_run("apply", self.mapping.fingerprint())

        report = Report(
            mode="apply",
            run_id=run_id,
            mapping_hash=self.mapping.fingerprint(),
            started_at=_now_iso(),
        )

        status = "completed"
        try:
            for entity in self.mapping.topo_order(entity_names):
                outcome = self._apply_entity(entity, run_id, limit)
                report.entities.append(outcome)
                if outcome.aborted:
                    status = "failed"
                    break
        except Exception as exc:  # noqa: BLE001
            report.fatal = f"{type(exc).__name__}: {exc}"
            status = "failed"

        if report.total_failed and self.settings.on_error == "abort":
            status = "failed"

        report.finished_at = _now_iso()
        self.state.finish_run(run_id, status)
        return report

    def _apply_entity(self, entity: EntityMap, run_id: str, limit: int) -> EntityOutcome:
        out = EntityOutcome(
            name=entity.name,
            source_table=entity.source.table,
            target_table=entity.target.table,
        )
        ctx = Ctx(state=self.state, run_id=run_id, entity_name=entity.name)
        batch_size = entity.batch_size or self.settings.batch_size

        cp = self.state.get_checkpoint(run_id, entity.name) or Checkpoint(
            entity=entity.name, last_key=None, processed=0, written=0, skipped=0, failed=0, done=False
        )
        if cp.done:
            out.notes.append("already completed in this run — skipped")
            out.processed = cp.processed
            out.inserted = cp.written
            out.skipped = cp.skipped
            out.failed = cp.failed
            return out

        if cp.last_key is not None:
            out.notes.append(f"resuming after {entity.source.key} = {cp.last_key}")
        out.processed, out.skipped, out.failed = cp.processed, cp.skipped, cp.failed
        out.inserted = cp.written

        try:
            out.total_source_rows = self.source.count(entity)
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f"row count failed: {exc}")

        try:
            stream = self.source.stream(
                entity, after_key=cp.last_key, batch_size=batch_size
            )
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f"cannot read source: {exc}")
            out.aborted = True
            return out

        for batch in stream:
            prepared: list[tuple[Any, dict[str, Any]]] = []
            last_key = batch[-1][entity.source.sort_column]

            for src in batch:
                source_id = src.get(entity.map_key)
                out.processed += 1

                # Idempotency: never write a source row twice.
                if entity.id_map and self.state.lookup_id(entity.name, source_id) is not None:
                    out.already_migrated += 1
                    continue

                try:
                    row = self.build_row(entity, src, ctx)
                except RowError as exc:
                    out.failed += 1
                    self.state.record_error(
                        run_id, entity.name, source_id, "transform", str(exc)
                    )
                    if len(out.errors) < 50:
                        out.errors.append(
                            {"source_id": _s(source_id), "field": exc.field_name, "error": exc.message}
                        )
                    if self.settings.on_error == "abort":
                        out.aborted = True
                        out.notes.append(f"aborted on first error (ON_ERROR=abort): {exc}")
                        self._checkpoint(run_id, entity, out, cp.last_key, done=False)
                        return out
                    continue

                prepared.append((source_id, row))

            if prepared:
                try:
                    results = self._write(entity, run_id, prepared)
                except Exception as exc:  # noqa: BLE001
                    out.aborted = True
                    out.notes.append(f"write failed, stopping this entity: {exc}")
                    self.state.record_error(run_id, entity.name, None, "write", str(exc))
                    self._checkpoint(run_id, entity, out, cp.last_key, done=False)
                    return out

                self._tally(out, results)
                for res in results:
                    if res.action == "failed":
                        self.state.record_error(
                            run_id, entity.name, res.source_id, "write", res.error or "unknown"
                        )
                        if len(out.errors) < 50:
                            out.errors.append(
                                {"source_id": _s(res.source_id), "field": "-", "error": res.error}
                            )

                if entity.id_map:
                    self.state.record_ids(
                        entity.name,
                        run_id,
                        [
                            (r.source_id, r.target_id)
                            for r in results
                            if r.target_id is not None and r.action in ("inserted", "updated", "skipped")
                        ],
                    )

            # Checkpoint only after the target has committed: a crash re-runs
            # this batch, and the id map makes that a no-op.
            cp.last_key = _s(last_key)
            self._checkpoint(run_id, entity, out, cp.last_key, done=False)
            self.progress(entity.name, out.processed, out.total_source_rows)

            if self.should_stop():
                out.aborted = True
                out.notes.append(
                    f"stopped on request after {out.processed:,} row(s); "
                    f"resume with `eagm run --resume {run_id}`"
                )
                return out

            if self.settings.on_error == "abort" and out.failed:
                out.aborted = True
                out.notes.append("aborted after write errors (ON_ERROR=abort)")
                return out

            if limit and out.processed >= limit:
                out.notes.append(f"stopped early at --limit {limit}")
                return out

        self._checkpoint(run_id, entity, out, cp.last_key, done=True)
        return out

    def _write(
        self, entity: EntityMap, run_id: str, prepared: list[tuple[Any, dict[str, Any]]]
    ) -> list[WriteResult]:
        def journal(results: list[WriteResult]) -> None:
            entries = [
                (r.target_id, r.action, r.prior_row)
                for r in results
                if r.action in ("inserted", "updated") and r.target_id is not None
            ]
            self.state.journal(
                run_id, entity.name, entity.target.table, entity.target.key, entries
            )

        return self.sink.write_batch(entity, prepared, pre_commit=journal)

    @staticmethod
    def _tally(out: EntityOutcome, results: Iterable[WriteResult]) -> None:
        for res in results:
            if res.action == "inserted":
                out.inserted += 1
            elif res.action == "updated":
                out.updated += 1
            elif res.action == "skipped":
                out.skipped += 1
            elif res.action == "failed":
                out.failed += 1

    def _checkpoint(
        self,
        run_id: str,
        entity: EntityMap,
        out: EntityOutcome,
        last_key: Any,
        *,
        done: bool,
    ) -> None:
        self.state.save_checkpoint(
            run_id,
            Checkpoint(
                entity=entity.name,
                last_key=None if last_key is None else str(last_key),
                processed=out.processed,
                written=out.inserted + out.updated,
                skipped=out.skipped,
                failed=out.failed,
                done=done,
            ),
        )

    # --- rollback -----------------------------------------------------------

    def rollback(self, run_id: str) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        if not run:
            raise ValueError(f"no such run: {run_id}")
        if run["mode"] != "apply":
            raise ValueError(f"run {run_id} was a {run['mode']} run — nothing was written")

        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in self.state.journal_entries_desc(run_id):
            key = (row["entity"], row["target_table"], row["target_key"])
            grouped.setdefault(key, []).append(
                {
                    "target_id": row["target_id"],
                    "action": row["action"],
                    "prior_row": _load_json(row["prior_row"]),
                }
            )

        undone = 0
        details: list[dict[str, Any]] = []
        # Reverse dependency order: children before the parents they point at.
        order = {e.name: i for i, e in enumerate(self.mapping.topo_order())}
        for (entity_name, table, key_col) in sorted(
            grouped, key=lambda k: -order.get(k[0], 0)
        ):
            entries = grouped[(entity_name, table, key_col)]
            count = self.sink.undo(entity_name, table, key_col, entries)
            undone += count
            details.append({"entity": entity_name, "table": table, "rows_undone": count})

        self.state.forget_ids(run_id)
        self.state.clear_journal(run_id)
        self.state.finish_run(run_id, "rolled_back")
        return {"run_id": run_id, "rows_undone": undone, "detail": details}


# --- nested target paths ------------------------------------------------------
#
# A JSON API does not take a flat row. v3's job wants `vehicle` as an object and
# `billingAddress` as another; its customer wants `addressList` and `phoneList`
# as arrays of objects. So `to:` accepts a path — `vehicle.vin`,
# `addressList[0].city` — and the row is assembled into that shape. A `to:` with
# no dot or bracket behaves exactly as it always did, which is what a database
# target needs, since a column called `vehicle.vin` does not exist.

_SEGMENT = re.compile(r"([^.\[\]]+)((?:\[\d+\])*)$")


def _is_nested(path: str) -> bool:
    return "." in path or "[" in path


def _path_tokens(path: str) -> list[str | int]:
    """`addressList[0].city` -> ['addressList', 0, 'city']."""
    tokens: list[str | int] = []
    for part in path.split("."):
        match = _SEGMENT.fullmatch(part)
        if not match:
            raise RowError(path, f"malformed target path {path!r}")
        tokens.append(match.group(1))
        tokens.extend(int(i) for i in re.findall(r"\[(\d+)\]", match.group(2)))
    return tokens


def _assign_path(out: dict[str, Any], path: str, value: Any) -> None:
    tokens = _path_tokens(path)
    cursor: Any = out
    for position, token in enumerate(tokens[:-1]):
        empty: Any = [] if isinstance(tokens[position + 1], int) else {}
        if isinstance(token, int):
            while len(cursor) <= token:
                cursor.append(None)
            if cursor[token] is None:
                cursor[token] = empty
            cursor = cursor[token]
        else:
            if cursor.get(token) is None:
                cursor[token] = empty
            cursor = cursor[token]
    last = tokens[-1]
    if isinstance(last, int):
        while len(cursor) <= last:
            cursor.append(None)
    cursor[last] = value


def _blank(value: Any, const_keys: frozenset[str] = frozenset()) -> bool:
    """Is there any actual data here?

    Fields set from `const:` do not count. An address entry is typically
    `addressType: 2` plus a `const` country, and those are set whether or not
    the customer has a second address — so counting them as data means nothing
    is ever pruned, and every customer is sent an empty second address labelled
    "type 2". A dict of nothing but const keys is blank.
    """
    if value is None or value == "":
        return True
    if isinstance(value, dict):
        return all(
            _blank(v, const_keys) for k, v in value.items() if k not in const_keys
        )
    if isinstance(value, list):
        return all(_blank(v, const_keys) for v in value)
    return False


def _prune(value: Any, const_keys: frozenset[str] = frozenset()) -> Any:
    """Drop array entries that carry no data.

    A customer with one address still has an `addressList[1].*` mapping, and
    without this every such customer would be sent a second address object full
    of nulls. Only entries inside arrays are dropped — object keys are left
    alone, so a field mapped to null still reaches the API as null.
    """
    if isinstance(value, dict):
        return {k: _prune(v, const_keys) for k, v in value.items()}
    if isinstance(value, list):
        return [_prune(v, const_keys) for v in value if not _blank(v, const_keys)]
    return value


def _resolve_default(value: Any) -> Any:
    if value == NOW_TOKEN:
        return dt.datetime.now(dt.timezone.utc)
    return value


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _s(value: Any) -> Any:
    """Make a value safe for JSON reports."""
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


def _load_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    import json

    try:
        return json.loads(raw)
    except ValueError:
        return None
