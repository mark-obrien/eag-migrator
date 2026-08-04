"""Write side: straight into the v3 database."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import Engine, MetaData, Table, delete, inspect, select, update

from ..mapping import EntityMap
from .base import WriteResult


class SinkConfigError(RuntimeError):
    """The mapping asks for something the target schema cannot do."""


class SqlSink:
    def __init__(self, engine: Engine, default_schema: str | None = None) -> None:
        self.engine = engine
        self.default_schema = default_schema
        self._tables: dict[tuple[str, str | None], Table] = {}

    def _schema(self, entity: EntityMap) -> str | None:
        return entity.target.db_schema or self.default_schema

    def table(self, entity: EntityMap) -> Table:
        key = (entity.target.table, self._schema(entity))
        if key not in self._tables:
            md = MetaData()
            self._tables[key] = Table(
                entity.target.table, md, autoload_with=self.engine, schema=key[1]
            )
        return self._tables[key]

    def raw_table(self, name: str, schema: str | None = None) -> Table:
        key = (name, schema or self.default_schema)
        if key not in self._tables:
            md = MetaData()
            self._tables[key] = Table(name, md, autoload_with=self.engine, schema=key[1])
        return self._tables[key]

    def columns(self, entity: EntityMap) -> list[str]:
        insp = inspect(self.engine)
        return [
            c["name"]
            for c in insp.get_columns(entity.target.table, schema=self._schema(entity))
        ]

    # -----------------------------------------------------------------------

    def write_batch(
        self,
        entity: EntityMap,
        rows: list[tuple[Any, dict[str, Any]]],
        pre_commit: Callable[[list[WriteResult]], None] | None = None,
    ) -> list[WriteResult]:
        if not rows:
            return []

        tbl = self.table(entity)
        valid = set(tbl.c.keys())
        key_col = entity.target.key
        if key_col not in valid:
            raise SinkConfigError(
                f"entity '{entity.name}': target key '{key_col}' is not a column of "
                f"'{entity.target.table}'. Columns: {', '.join(sorted(valid))}"
            )

        unknown = {c for _, r in rows for c in r} - valid
        if unknown:
            raise SinkConfigError(
                f"entity '{entity.name}': mapping writes columns that do not exist on "
                f"'{entity.target.table}': {', '.join(sorted(unknown))}"
            )

        results: list[WriteResult] = []
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            # Which of the supplied keys already exist in v3?
            keyed = [(sid, r) for sid, r in rows if r.get(key_col) is not None]
            existing: dict[Any, dict[str, Any]] = {}
            if keyed:
                wanted = [r[key_col] for _, r in keyed]
                for chunk in _chunks(wanted, 500):
                    found = conn.execute(
                        select(tbl).where(tbl.c[key_col].in_(chunk))
                    ).mappings().all()
                    for row in found:
                        existing[row[key_col]] = dict(row)

            pending_inserts: list[tuple[Any, dict[str, Any]]] = []

            for source_id, row in rows:
                key_val = row.get(key_col)

                if key_val is not None and key_val in existing:
                    prior = existing[key_val]
                    if entity.target.conflict == "skip":
                        results.append(
                            WriteResult(source_id, key_val, "skipped", payload=row)
                        )
                        continue
                    if entity.target.conflict == "error":
                        results.append(
                            WriteResult(
                                source_id,
                                key_val,
                                "failed",
                                error=f"target row {key_col}={key_val!r} already exists",
                                payload=row,
                            )
                        )
                        continue
                    # conflict == update
                    changes = {c: v for c, v in row.items() if c != key_col}
                    if changes:
                        conn.execute(
                            update(tbl).where(tbl.c[key_col] == key_val).values(**changes)
                        )
                    results.append(
                        WriteResult(source_id, key_val, "updated", prior_row=prior, payload=row)
                    )
                    continue

                pending_inserts.append((source_id, row))

            if pending_inserts:
                results.extend(self._insert(conn, tbl, key_col, pending_inserts))

            # Journal BEFORE committing the target. A crash here leaves the
            # journal a superset of what was actually written, and undoing a
            # row that was never written is a no-op — the safe direction.
            if pre_commit is not None:
                pre_commit(results)

            trans.commit()
        except Exception:
            trans.rollback()
            raise
        finally:
            conn.close()

        return results

    def _insert(
        self,
        conn: Any,
        tbl: Table,
        key_col: str,
        rows: list[tuple[Any, dict[str, Any]]],
    ) -> list[WriteResult]:
        results: list[WriteResult] = []
        explicit = [(sid, r) for sid, r in rows if r.get(key_col) is not None]
        generated = [(sid, r) for sid, r in rows if r.get(key_col) is None]

        # Explicit keys: one executemany for the whole batch.
        if explicit:
            try:
                conn.execute(tbl.insert(), [r for _, r in explicit])
                results.extend(
                    WriteResult(sid, r[key_col], "inserted", payload=r) for sid, r in explicit
                )
            except Exception:
                # Fall back to per-row so one bad record does not fail the batch.
                for sid, r in explicit:
                    sp = conn.begin_nested()
                    try:
                        conn.execute(tbl.insert(), [r])
                        sp.commit()
                        results.append(WriteResult(sid, r[key_col], "inserted", payload=r))
                    except Exception as exc:  # noqa: BLE001
                        sp.rollback()
                        results.append(
                            WriteResult(sid, None, "failed", error=_msg(exc), payload=r)
                        )

        # Generated keys: per row, so we can read back the id the database chose.
        for sid, r in generated:
            body = {c: v for c, v in r.items() if c != key_col}
            sp = conn.begin_nested()
            try:
                res = conn.execute(tbl.insert().values(**body))
                new_id = None
                if res.inserted_primary_key:
                    new_id = res.inserted_primary_key[0]
                sp.commit()
                results.append(WriteResult(sid, new_id, "inserted", payload=r))
            except Exception as exc:  # noqa: BLE001
                sp.rollback()
                results.append(WriteResult(sid, None, "failed", error=_msg(exc), payload=r))

        return results

    def fetch_one(self, entity: EntityMap, key_value: Any) -> dict[str, Any] | None:
        tbl = self.table(entity)
        col = tbl.c[entity.target.key]
        with self.engine.connect() as conn:
            row = conn.execute(
                select(tbl).where(col == _cast_key(tbl, entity.target.key, key_value)).limit(1)
            ).mappings().first()
        return dict(row) if row else None

    def count_rows(self, entity: EntityMap) -> int:
        from sqlalchemy import func

        tbl = self.table(entity)
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(tbl)).scalar_one())

    def probe_existing(self, entity: EntityMap, keys: list[Any]) -> int:
        """How many of these target keys are already present? Used by the dry run."""
        if not keys:
            return 0
        tbl = self.table(entity)
        key_col = entity.target.key
        total = 0
        with self.engine.connect() as conn:
            for chunk in _chunks(keys, 500):
                total += len(
                    conn.execute(
                        select(tbl.c[key_col]).where(tbl.c[key_col].in_(chunk))
                    ).all()
                )
        return total

    # -----------------------------------------------------------------------

    def undo(
        self, entity_name: str, table: str, key_column: str, entries: list[dict[str, Any]]
    ) -> int:
        """Reverse journalled writes: delete inserts, restore prior values on updates."""
        if not entries:
            return 0
        tbl = self.raw_table(table)
        affected = 0
        with self.engine.begin() as conn:
            insert_ids = [e["target_id"] for e in entries if e["action"] == "inserted"]
            for chunk in _chunks(insert_ids, 500):
                typed = [_cast_key(tbl, key_column, v) for v in chunk]
                res = conn.execute(delete(tbl).where(tbl.c[key_column].in_(typed)))
                affected += res.rowcount or 0

            for entry in entries:
                if entry["action"] != "updated" or not entry.get("prior_row"):
                    continue
                prior = dict(entry["prior_row"])
                key_val = _cast_key(tbl, key_column, entry["target_id"])
                prior.pop(key_column, None)
                prior = {c: v for c, v in prior.items() if c in tbl.c}
                if not prior:
                    continue
                res = conn.execute(
                    update(tbl).where(tbl.c[key_column] == key_val).values(**prior)
                )
                affected += res.rowcount or 0
        return affected

    def close(self) -> None:
        self.engine.dispose()


def _cast_key(tbl: Table, key_column: str, value: Any) -> Any:
    """Journal ids are stored as text; restore the column's real type."""
    if not isinstance(value, str):
        return value
    try:
        py_type = tbl.c[key_column].type.python_type
    except (NotImplementedError, AttributeError, KeyError):
        return value
    if py_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _msg(exc: Exception) -> str:
    text = str(getattr(exc, "orig", exc)).strip().replace("\n", " ")
    return text[:400]
