"""Durable run state: checkpoints, id mapping, write journal, errors.

Kept in its own SQLite file rather than in the v3 database, so a rollback can
never be defeated by the thing it is rolling back, and so nothing pollutes the
target schema.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,
    mapping_hash  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    last_key   TEXT,
    processed  INTEGER NOT NULL DEFAULT 0,
    written    INTEGER NOT NULL DEFAULT 0,
    skipped    INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    done       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, entity)
);

CREATE TABLE IF NOT EXISTS id_map (
    entity     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    target_id  TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (entity, source_id)
);

CREATE TABLE IF NOT EXISTS journal (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    target_table TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    action       TEXT NOT NULL,
    prior_row    TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_run_idx ON journal (run_id, seq);

CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    source_id  TEXT,
    stage      TEXT NOT NULL,
    message    TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS errors_run_idx ON errors (run_id, entity);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class Checkpoint:
    entity: str
    last_key: str | None
    processed: int
    written: int
    skipped: int
    failed: int
    done: bool


class RunState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> RunState:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # --- runs ---------------------------------------------------------------

    def start_run(self, mode: str, mapping_hash: str, note: str | None = None) -> str:
        run_id = f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.conn.execute(
            "INSERT INTO runs (run_id, mode, mapping_hash, started_at, status, note)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, mode, mapping_hash, _now(), "running", note),
        )
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE run_id = ?",
            (_now(), status, run_id),
        )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return cur.fetchone()

    def latest_run(self, mode: str | None = None) -> sqlite3.Row | None:
        if mode:
            cur = self.conn.execute(
                "SELECT * FROM runs WHERE mode = ? ORDER BY started_at DESC LIMIT 1", (mode,)
            )
        else:
            cur = self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
        return cur.fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    # --- checkpoints --------------------------------------------------------

    def get_checkpoint(self, run_id: str, entity: str) -> Checkpoint | None:
        cur = self.conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? AND entity = ?", (run_id, entity)
        )
        row = cur.fetchone()
        if not row:
            return None
        return Checkpoint(
            entity=row["entity"],
            last_key=row["last_key"],
            processed=row["processed"],
            written=row["written"],
            skipped=row["skipped"],
            failed=row["failed"],
            done=bool(row["done"]),
        )

    def save_checkpoint(self, run_id: str, cp: Checkpoint) -> None:
        self.conn.execute(
            """
            INSERT INTO checkpoints
                (run_id, entity, last_key, processed, written, skipped, failed, done, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, entity) DO UPDATE SET
                last_key = excluded.last_key,
                processed = excluded.processed,
                written = excluded.written,
                skipped = excluded.skipped,
                failed = excluded.failed,
                done = excluded.done,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                cp.entity,
                None if cp.last_key is None else str(cp.last_key),
                cp.processed,
                cp.written,
                cp.skipped,
                cp.failed,
                int(cp.done),
                _now(),
            ),
        )

    def checkpoints(self, run_id: str) -> list[Checkpoint]:
        cur = self.conn.execute(
            "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY entity", (run_id,)
        )
        return [
            Checkpoint(
                entity=r["entity"],
                last_key=r["last_key"],
                processed=r["processed"],
                written=r["written"],
                skipped=r["skipped"],
                failed=r["failed"],
                done=bool(r["done"]),
            )
            for r in cur.fetchall()
        ]

    # --- id map -------------------------------------------------------------

    def record_ids(self, entity: str, run_id: str, pairs: list[tuple[Any, Any]]) -> None:
        if not pairs:
            return
        now = _now()
        self.conn.executemany(
            "INSERT INTO id_map (entity, source_id, target_id, run_id, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(entity, source_id) DO UPDATE SET"
            "   target_id = excluded.target_id, run_id = excluded.run_id",
            [(entity, str(s), str(t), run_id, now) for s, t in pairs],
        )

    def lookup_id(self, entity: str, source_id: Any) -> str | None:
        cur = self.conn.execute(
            "SELECT target_id FROM id_map WHERE entity = ? AND source_id = ?",
            (entity, str(source_id)),
        )
        row = cur.fetchone()
        return row["target_id"] if row else None

    def id_map_size(self, entity: str) -> int:
        cur = self.conn.execute("SELECT COUNT(*) c FROM id_map WHERE entity = ?", (entity,))
        return int(cur.fetchone()["c"])

    def sample_id_pairs(self, entity: str, limit: int) -> list[tuple[str, str]]:
        cur = self.conn.execute(
            "SELECT source_id, target_id FROM id_map WHERE entity = ?"
            " ORDER BY RANDOM() LIMIT ?",
            (entity, limit),
        )
        return [(r["source_id"], r["target_id"]) for r in cur.fetchall()]

    # --- journal (rollback) -------------------------------------------------

    def journal(
        self,
        run_id: str,
        entity: str,
        target_table: str,
        target_key: str,
        entries: list[tuple[Any, str, dict[str, Any] | None]],
    ) -> None:
        """entries: (target_id, action, prior_row_or_None)"""
        if not entries:
            return
        now = _now()
        self.conn.executemany(
            "INSERT INTO journal"
            " (run_id, entity, target_table, target_key, target_id, action, prior_row, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    entity,
                    target_table,
                    target_key,
                    str(tid),
                    action,
                    json.dumps(prior, default=str) if prior is not None else None,
                    now,
                )
                for tid, action, prior in entries
            ],
        )

    def journal_entries_desc(self, run_id: str) -> Iterator[sqlite3.Row]:
        """Newest first — the order a rollback must undo them in."""
        cur = self.conn.execute(
            "SELECT * FROM journal WHERE run_id = ? ORDER BY seq DESC", (run_id,)
        )
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                break
            yield from rows

    def journal_count(self, run_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) c FROM journal WHERE run_id = ?", (run_id,)
        )
        return int(cur.fetchone()["c"])

    def clear_journal(self, run_id: str) -> None:
        self.conn.execute("DELETE FROM journal WHERE run_id = ?", (run_id,))

    def forget_ids(self, run_id: str) -> None:
        self.conn.execute("DELETE FROM id_map WHERE run_id = ?", (run_id,))

    # --- errors -------------------------------------------------------------

    def record_error(
        self,
        run_id: str,
        entity: str,
        source_id: Any,
        stage: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO errors (run_id, entity, source_id, stage, message, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                entity,
                None if source_id is None else str(source_id),
                stage,
                message,
                json.dumps(payload, default=str) if payload else None,
                _now(),
            ),
        )

    def errors(self, run_id: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM errors WHERE run_id = ? ORDER BY id"
        params: tuple[Any, ...] = (run_id,)
        if limit:
            sql += " LIMIT ?"
            params = (run_id, limit)
        return self.conn.execute(sql, params).fetchall()

    def error_summary(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT entity, stage, message, COUNT(*) AS count FROM errors"
            " WHERE run_id = ? GROUP BY entity, stage, message ORDER BY count DESC",
            (run_id,),
        ).fetchall()
