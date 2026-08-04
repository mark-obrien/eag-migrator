"""The mapping file: how v2 rows become v3 rows.

The migrator is entirely driven by this YAML. Nothing about EAG's schema is
hard-coded in Python, so when the real schema turns up you edit config/ and
never touch the engine.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    table: str
    key: str
    """Column used to page and resume. Must be unique and sortable."""
    order_by: str | None = None
    where: str | None = None
    db_schema: str | None = Field(default=None, alias="schema")

    @property
    def sort_column(self) -> str:
        return self.order_by or self.key


class TargetSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    table: str
    key: str = "id"
    db_schema: str | None = Field(default=None, alias="schema")
    conflict: Literal["skip", "update", "error"] = "skip"
    """What to do when a row with the same target key already exists."""
    endpoint: str | None = None
    """Path used by the API sink, e.g. '/api/v3/customers'. Ignored by the SQL sink."""


class FieldMap(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    to: str
    from_: str | list[str] | None = Field(default=None, alias="from")
    const: Any = None
    transform: list[Any] = Field(default_factory=list)
    required: bool = False
    default: Any = None
    note: str | None = None
    """Free-text reminder surfaced in the plan report — use it for open questions."""

    @model_validator(mode="after")
    def _need_a_source(self) -> FieldMap:
        if self.from_ is None and self.const is None and self.default is None:
            raise ValueError(
                f"field '{self.to}': needs one of 'from', 'const' or 'default'"
            )
        return self


class EntityMap(BaseModel):
    name: str
    enabled: bool = True
    source: SourceSpec
    target: TargetSpec
    depends_on: list[str] = Field(default_factory=list)
    fields: list[FieldMap]
    id_map: bool = True
    """Record source-id -> target-id so other entities can resolve foreign keys."""
    batch_size: int | None = None
    note: str | None = None


class Defaults(BaseModel):
    batch_size: int = 500
    on_error: Literal["record", "abort"] = "record"


class Mapping(BaseModel):
    version: int = 1
    defaults: Defaults = Field(default_factory=Defaults)
    entities: list[EntityMap] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Mapping:
        seen: set[str] = set()
        for e in self.entities:
            if e.name in seen:
                raise ValueError(f"duplicate entity name: {e.name}")
            seen.add(e.name)
        for e in self.entities:
            for dep in e.depends_on:
                if dep not in seen:
                    raise ValueError(f"entity '{e.name}' depends on unknown entity '{dep}'")
        return self

    def active(self) -> list[EntityMap]:
        return [e for e in self.entities if e.enabled]

    def get(self, name: str) -> EntityMap | None:
        for e in self.entities:
            if e.name == name:
                return e
        return None

    def topo_order(self, names: list[str] | None = None) -> list[EntityMap]:
        """Dependency order. Raises on cycles so we fail before writing anything."""
        pool = {e.name: e for e in self.active()}
        if names:
            wanted = set(names)
            unknown = wanted - set(pool)
            if unknown:
                raise ValueError(f"unknown or disabled entities: {', '.join(sorted(unknown))}")
            # Pull in dependencies of the requested entities too.
            resolved: set[str] = set()

            def _pull(n: str) -> None:
                if n in resolved or n not in pool:
                    return
                resolved.add(n)
                for d in pool[n].depends_on:
                    _pull(d)

            for n in wanted:
                _pull(n)
            pool = {n: e for n, e in pool.items() if n in resolved}

        ordered: list[EntityMap] = []
        state: dict[str, int] = {}  # 0 = visiting, 1 = done

        def visit(name: str, trail: list[str]) -> None:
            if state.get(name) == 1:
                return
            if state.get(name) == 0:
                cycle = " -> ".join([*trail, name])
                raise ValueError(f"circular dependency: {cycle}")
            state[name] = 0
            for dep in pool[name].depends_on:
                if dep in pool:
                    visit(dep, [*trail, name])
            state[name] = 1
            ordered.append(pool[name])

        for name in pool:
            visit(name, [])
        return ordered

    def fingerprint(self) -> str:
        payload = self.model_dump_json(by_alias=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_mapping(path: Path) -> Mapping:
    if not path.exists():
        raise FileNotFoundError(
            f"No mapping at {path}. Run `eagm scaffold` first to generate a draft "
            f"from the discovered schemas."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Mapping.model_validate(raw)


def dump_mapping(mapping: Mapping, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = mapping.model_dump(by_alias=True, exclude_none=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return path
