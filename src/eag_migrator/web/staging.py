"""The staging database — harvested pages, shaped like a v2 database.

One table per collection, columns named after the extracted fields, plus the
bookkeeping the migrator needs: `_id` to page and resume on, `_key` to
deduplicate re-harvests, `_url` and `_fetched_at` for provenance.

It is a plain SQLite file, so `V2_DATABASE_URL=sqlite:///<path>` turns the
whole scraped site into a source the existing pipeline already understands.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

RESERVED = ("_id", "_key", "_url", "_fetched_at")
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check(name: str, what: str) -> str:
    """Identifiers come from a config file, so validate rather than quote-and-hope."""
    if not SAFE_NAME.match(name):
        raise ValueError(
            f"invalid {what} {name!r}: use letters, digits and underscores, "
            f"starting with a letter or underscore"
        )
    return name


class Staging:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Staging:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -----------------------------------------------------------------------

    def ensure_table(self, name: str, columns: list[str]) -> None:
        table = _check(name, "collection name")
        cols = [_check(c, "field name") for c in columns if c not in RESERVED]

        self.conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{table}" ('
            ' _id INTEGER PRIMARY KEY AUTOINCREMENT,'
            ' _key TEXT NOT NULL UNIQUE,'
            ' _url TEXT,'
            ' _fetched_at TEXT'
            ")"
        )
        existing = {r["name"] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
        for col in cols:
            if col not in existing:
                self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}"')

    def insert(self, name: str, columns: list[str], rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        table = _check(name, "collection name")
        cols = [c for c in dict.fromkeys([*columns, *RESERVED[1:]]) if c != "_id"]
        for col in cols:
            _check(col, "field name")

        placeholders = ",".join("?" for _ in cols)
        quoted = ",".join(f'"{c}"' for c in cols)
        updates = ",".join(f'"{c}"=excluded."{c}"' for c in cols if c != "_key")

        # Re-harvesting updates in place rather than duplicating.
        sql = (
            f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) '
            f"ON CONFLICT(_key) DO UPDATE SET {updates}"
        )
        payload = [tuple(row.get(c) for c in cols) for row in rows]
        self.conn.executemany(sql, payload)
        return len(rows)

    def count(self, name: str) -> int:
        table = _check(name, "collection name")
        try:
            cur = self.conn.execute(f'SELECT COUNT(*) c FROM "{table}"')
        except sqlite3.OperationalError:
            return 0
        return int(cur.fetchone()["c"])

    def tables(self) -> list[str]:
        return [
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

    def sample(self, name: str, limit: int = 3) -> list[dict[str, Any]]:
        table = _check(name, "collection name")
        return [
            dict(r) for r in self.conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (limit,))
        ]
