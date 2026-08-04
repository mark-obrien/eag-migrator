"""Source and sink interfaces.

You were not sure whether the migrator should talk to v3 over SQL or over its
HTTP API, so that choice lives entirely behind this boundary: swap SqlSink for
ApiSink and nothing else in the engine changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from ..mapping import EntityMap


@dataclass
class WriteResult:
    """One attempted row write."""

    source_id: Any
    target_id: Any | None
    action: str  # inserted | updated | skipped | failed
    prior_row: dict[str, Any] | None = None
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    def count(self, entity: EntityMap) -> int:
        """Total rows matching the entity's source filter."""

    def stream(
        self, entity: EntityMap, *, after_key: Any | None, batch_size: int
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield batches ordered by the entity's sort column, resuming after after_key."""

    def columns(self, entity: EntityMap) -> list[str]:
        """Column names available on the source table."""


@runtime_checkable
class Sink(Protocol):
    def columns(self, entity: EntityMap) -> list[str]:
        """Column names the target accepts (empty list = unknown / unchecked)."""

    def write_batch(
        self,
        entity: EntityMap,
        rows: list[tuple[Any, dict[str, Any]]],
        pre_commit: Callable[[list[WriteResult]], None] | None = None,
    ) -> list[WriteResult]:
        """Write (source_id, target_row) pairs. Must not raise for per-row problems.

        pre_commit runs with the results while the target transaction is still
        open, so the rollback journal is durable before the writes are.
        """

    def undo(
        self, entity_name: str, table: str, key_column: str, entries: list[dict[str, Any]]
    ) -> int:
        """Reverse journalled writes. entries carry target_id, action, prior_row."""

    def close(self) -> None: ...
