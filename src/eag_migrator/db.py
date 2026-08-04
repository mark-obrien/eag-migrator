"""Engine construction and small cross-dialect helpers.

Everything here is dialect-agnostic on purpose: we do not know yet whether EAG
v2 is MySQL, Postgres or SQL Server, so all access goes through SQLAlchemy.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url


def build_engine(url: str, *, readonly: bool = False) -> Engine:
    """Create an Engine with sensible streaming defaults for bulk reads."""
    u = make_url(url)
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "future": True}

    if u.drivername.startswith("mysql"):
        # Server-side cursors keep large tables out of the client's memory.
        kwargs["connect_args"] = {"charset": "utf8mb4"}
    elif u.drivername.startswith("postgresql"):
        kwargs["connect_args"] = {}

    engine = create_engine(url, **kwargs)
    if readonly:
        engine.echo = False
    return engine


def dialect_of(url: str) -> str:
    """'mysql', 'postgresql', 'mssql', 'sqlite', ..."""
    return make_url(url).get_backend_name()


def safe_identifier(engine: Engine, name: str) -> str:
    """Quote an identifier for the target dialect."""
    return engine.dialect.identifier_preparer.quote(name)


def qualified(engine: Engine, table: str, schema: str | None = None) -> str:
    if schema:
        return f"{safe_identifier(engine, schema)}.{safe_identifier(engine, table)}"
    return safe_identifier(engine, table)


def probe(engine: Engine) -> tuple[bool, str]:
    """Check the connection. Returns (ok, message) rather than raising."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, f"connected to {engine.url.render_as_string(hide_password=True)}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return False, f"{type(exc).__name__}: {exc}"


def row_count(engine: Engine, table: str, schema: str | None = None, where: str | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {qualified(engine, table, schema)}"
    if where:
        sql += f" WHERE {where}"
    with engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())
