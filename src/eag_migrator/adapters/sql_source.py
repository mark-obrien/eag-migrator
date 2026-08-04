"""Read side: keyset-paginated streaming out of the v2 database."""

from __future__ import annotations

from typing import Any, Iterator

from sqlalchemy import Engine, MetaData, Table, asc, inspect, select, text

from ..mapping import EntityMap


class SqlSource:
    def __init__(self, engine: Engine, default_schema: str | None = None) -> None:
        self.engine = engine
        self.default_schema = default_schema
        self._tables: dict[tuple[str, str | None], Table] = {}

    def _schema(self, entity: EntityMap) -> str | None:
        return entity.source.db_schema or self.default_schema

    def table(self, entity: EntityMap) -> Table:
        key = (entity.source.table, self._schema(entity))
        if key not in self._tables:
            md = MetaData()
            self._tables[key] = Table(
                entity.source.table, md, autoload_with=self.engine, schema=key[1]
            )
        return self._tables[key]

    def columns(self, entity: EntityMap) -> list[str]:
        insp = inspect(self.engine)
        return [
            c["name"]
            for c in insp.get_columns(entity.source.table, schema=self._schema(entity))
        ]

    def count(self, entity: EntityMap) -> int:
        tbl = self.table(entity)
        stmt = select(tbl.c[entity.source.key])
        if entity.source.where:
            stmt = stmt.where(text(entity.source.where))
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(text("COUNT(*)")).select_from(stmt.subquery())
                ).scalar_one()
            )

    def fetch_one(self, entity: EntityMap, key_value: Any) -> dict[str, Any] | None:
        tbl = self.table(entity)
        col = tbl.c[entity.source.key]
        with self.engine.connect() as conn:
            row = conn.execute(
                select(tbl).where(col == _coerce(key_value, col)).limit(1)
            ).mappings().first()
        return dict(row) if row else None

    def stream(
        self, entity: EntityMap, *, after_key: Any | None, batch_size: int
    ) -> Iterator[list[dict[str, Any]]]:
        """Keyset pagination — stable and cheap even millions of rows in.

        OFFSET would get quadratically slower and can silently skip rows if the
        source is still being written to, so we page on the sort column instead.
        """
        tbl = self.table(entity)
        sort_col = tbl.c[entity.source.sort_column]
        cursor = after_key

        with self.engine.connect().execution_options(stream_results=True) as conn:
            while True:
                stmt = select(tbl)
                if entity.source.where:
                    stmt = stmt.where(text(entity.source.where))
                if cursor is not None:
                    stmt = stmt.where(sort_col > _coerce(cursor, sort_col))
                stmt = stmt.order_by(asc(sort_col)).limit(batch_size)

                rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
                if not rows:
                    return
                yield rows
                cursor = rows[-1][entity.source.sort_column]
                if len(rows) < batch_size:
                    return


def _coerce(value: Any, column: Any) -> Any:
    """Checkpoints round-trip through SQLite as text; put the type back."""
    if not isinstance(value, str):
        return value
    try:
        py_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return value
    if py_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    if py_type is float:
        try:
            return float(value)
        except ValueError:
            return value
    return value
