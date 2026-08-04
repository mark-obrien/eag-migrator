"""Generate a draft mapping from two discovered schemas.

You do not know EAG's schema, so hand-writing a mapping from nothing is not
realistic. This matches v2 tables and columns against v3 by name similarity,
picks plausible transforms from the target column types, wires foreign keys
into `lookup` steps, and leaves a `note:` everywhere it guessed.

The output is a *draft*. Every note is a question for whoever owns EAG.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .discovery import ColumnProfile, DatabaseProfile, TableProfile
from .mapping import Defaults, EntityMap, FieldMap, Mapping, SourceSpec, TargetSpec

# Framework plumbing — real tables, but almost never worth migrating.
NOISE = re.compile(
    r"^(migrations|schema_migrations|ar_internal_metadata|django_migrations|"
    r"_prisma_migrations|knex_migrations(_lock)?|sequelizemeta|typeorm_metadata|"
    r"__efmigrationshistory|sessions|ci_sessions|cache|cache_locks|jobs|job_batches|"
    r"failed_jobs|password_reset(_tokens|s)?|personal_access_tokens|"
    r"telescope_.*|activity_log|.*_log|logs|watchdog|term_relationships)$",
    re.IGNORECASE,
)

MATCH_THRESHOLD = 0.62

# Columns the web harvester adds to every staging table. Their presence means
# the "primary key" is a scraper row number, not the source system's id.
STAGING_MARKERS = {"_id", "_key", "_url", "_fetched_at"}

# Target columns that exist to hold the old system's primary key.
LEGACY_ID = re.compile(
    r"^(legacy|old|external|source|import|v2|prev(ious)?)_?(id|key|ref)$"
    r"|^(id|key|ref)_(legacy|old|external|source|v2)$",
    re.IGNORECASE,
)


def _norm(name: str, prefix: str | None = None) -> str:
    n = name.lower()
    if prefix and n.startswith(prefix.lower()):
        n = n[len(prefix) :]
    n = re.sub(r"[^a-z0-9]", "", n)
    # Crude singularisation so 'customers' matches 'customer'.
    if n.endswith("ies"):
        n = n[:-3] + "y"
    elif n.endswith("ses"):
        n = n[:-2]
    elif n.endswith("s") and not n.endswith("ss"):
        n = n[:-1]
    return n


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a and b and (a in b or b in a):
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _best(needle: str, haystack: dict[str, str]) -> tuple[str | None, float]:
    """haystack maps original-name -> normalised-name."""
    best_name, best_score = None, 0.0
    for original, normalised in haystack.items():
        score = _similarity(needle, normalised)
        if score > best_score:
            best_name, best_score = original, score
    return best_name, best_score


# --- transform inference ----------------------------------------------------

TYPE_HINTS: list[tuple[re.Pattern[str], list[Any]]] = [
    (re.compile(r"\b(datetime|timestamp)\b", re.I), ["to_datetime"]),
    (re.compile(r"\bdate\b", re.I), ["to_date"]),
    (re.compile(r"\b(bool|boolean|bit)\b", re.I), ["bool"]),
    (re.compile(r"\btinyint\(1\)", re.I), ["bool"]),
    (re.compile(r"\b(int|integer|bigint|smallint)\b", re.I), ["int"]),
    (re.compile(r"\b(numeric|decimal|money)\b", re.I), [{"decimal": {"places": 2}}]),
    (re.compile(r"\b(float|double|real)\b", re.I), ["float"]),
    (re.compile(r"\b(json|jsonb)\b", re.I), ["json_decode"]),
]

NAME_HINTS: list[tuple[re.Pattern[str], list[Any]]] = [
    (re.compile(r"e?_?mail", re.I), ["trim", "email"]),
    (re.compile(r"phone|mobile|cell|fax", re.I), ["phone"]),
    (re.compile(r"\bvin\b|vin_?number", re.I), ["vin"]),
    (re.compile(r"nags", re.I), ["nags"]),
    (re.compile(r"zip|postal", re.I), [{"postal_code": {"region": "US"}}]),
    (re.compile(r"slug|permalink", re.I), ["slugify"]),
]


def _infer_transforms(src: ColumnProfile | None, tgt: ColumnProfile | None) -> list[Any]:
    steps: list[Any] = []
    name = (tgt.name if tgt else (src.name if src else "")) or ""

    for pattern, hint in NAME_HINTS:
        if pattern.search(name):
            steps.extend(hint)
            break

    if not steps and tgt is not None:
        for pattern, hint in TYPE_HINTS:
            if pattern.search(tgt.type):
                steps.extend(hint)
                break

    if not steps and src is not None and re.search(r"char|text|varchar", src.type, re.I):
        steps.append("trim")

    if tgt is not None and tgt.nullable and steps and "nullif_empty" not in steps:
        if re.search(r"char|text|varchar", tgt.type, re.I):
            steps.append("nullif_empty")

    # Length-limited target: truncate rather than blow up on insert.
    if tgt is not None:
        m = re.search(r"VARCHAR\((\d+)\)", tgt.type, re.I)
        if m:
            steps.append({"truncate": {"length": int(m.group(1))}})

    return steps


def _is_generated_key(col: ColumnProfile, table: TableProfile | None) -> bool:
    """Would the database assign this column itself on insert?

    SQLAlchemy only reports `autoincrement` reliably on some dialects (SQLite
    never sets it), so fall back to the shape that means "generated" almost
    everywhere: the sole primary key, integer typed.
    """
    if col.autoincrement:
        return True
    if not col.primary_key:
        return False
    if table is not None and len(table.primary_key) != 1:
        return False
    if re.search(r"nextval|identity|autoincrement", col.default or "", re.I):
        return True
    return bool(re.search(r"\b(int|integer|bigint|smallint|serial)\b", col.type, re.I))


def _pick_key(table: TableProfile) -> tuple[str | None, str | None]:
    """Return (key_column, warning)."""
    if len(table.primary_key) == 1:
        return table.primary_key[0], None
    if len(table.primary_key) > 1:
        return (
            table.primary_key[0],
            f"composite primary key {table.primary_key} — source.key uses only "
            f"'{table.primary_key[0]}'; set a unique sortable column or add a WHERE filter",
        )
    for col in table.columns:
        if col.name.lower() in ("id", "uuid", "guid") or col.autoincrement:
            return col.name, "no primary key declared — guessed the paging key"
    if table.columns:
        return (
            table.columns[0].name,
            "no primary key and no id-like column — source.key is a guess and "
            "resume/idempotency will not work until you fix it",
        )
    return None, "table has no columns"


def build_draft(
    v2: DatabaseProfile,
    v3: DatabaseProfile | None = None,
    *,
    min_rows: int = 1,
) -> tuple[Mapping, list[str]]:
    """Returns (mapping, human-readable warnings)."""
    warnings: list[str] = []

    v3_tables: dict[str, TableProfile] = {}
    v3_index: dict[str, str] = {}
    if v3:
        for t in v3.tables:
            v3_tables[t.name] = t
            v3_index[t.name] = _norm(t.name, v3.table_prefix)

    entities: list[EntityMap] = []
    # v2 table name -> entity name, for wiring foreign keys later.
    table_to_entity: dict[str, str] = {}

    candidates = [
        t
        for t in v2.tables
        if not t.error and (t.row_count is None or t.row_count >= min_rows)
    ]
    candidates.sort(key=lambda t: -(t.row_count or 0))

    for table in candidates:
        entity_name = _norm(table.name, v2.table_prefix) or table.name
        # Keep entity names unique and readable.
        base, i = entity_name, 2
        while entity_name in table_to_entity.values():
            entity_name = f"{base}_{i}"
            i += 1
        table_to_entity[table.name] = entity_name

    for table in candidates:
        entity_name = table_to_entity[table.name]
        notes: list[str] = []
        is_noise = bool(NOISE.match(table.name)) or (
            v2.table_prefix and NOISE.match(table.name[len(v2.table_prefix) :])
        )

        key, key_warning = _pick_key(table)
        if key is None:
            warnings.append(f"{table.name}: skipped — {key_warning}")
            continue
        if key_warning:
            notes.append(key_warning)
            warnings.append(f"{table.name}: {key_warning}")

        # Match the target table.
        target_table, score = (None, 0.0)
        if v3_index:
            target_table, score = _best(_norm(table.name, v2.table_prefix), v3_index)
        if not v3_index:
            target_table = _norm(table.name, v2.table_prefix)
            notes.append("TODO: no v3 profile available — target table name is a placeholder")
        elif score < MATCH_THRESHOLD:
            notes.append(
                f"TODO: no confident v3 table match (best guess '{target_table}' at "
                f"{score:.0%}) — set target.table by hand"
            )
            warnings.append(
                f"{table.name}: weak target match '{target_table}' ({score:.0%})"
            )

        tgt_profile = v3_tables.get(target_table or "")
        tgt_cols: dict[str, ColumnProfile] = (
            {c.name: c for c in tgt_profile.columns} if tgt_profile else {}
        )
        tgt_index = {name: _norm(name) for name in tgt_cols}

        # FK column -> referenced entity, for lookup steps.
        fk_targets: dict[str, str] = {}
        fk_confidence: dict[str, float] = {}
        fk_inferred: dict[str, bool] = {}
        depends: list[str] = []
        for fk in [*table.foreign_keys, *table.inferred_foreign_keys]:
            ref_entity = table_to_entity.get(fk.referred_table)
            if not ref_entity or ref_entity == entity_name:
                continue
            for col in fk.columns:
                fk_targets[col] = ref_entity
                fk_confidence[col] = fk.confidence
                fk_inferred[col] = fk.inferred
            if ref_entity not in depends:
                depends.append(ref_entity)
            if fk.inferred:
                warnings.append(
                    f"{table.name}.{', '.join(fk.columns)} -> {fk.referred_table}: "
                    f"relationship inferred, not declared ({fk.confidence:.0%} of "
                    f"sampled values matched). Confirm before migrating."
                )

        fields: list[FieldMap] = []
        used_targets: set[str] = set()

        target_key = "id"
        if tgt_profile and tgt_profile.primary_key:
            target_key = tgt_profile.primary_key[0]

        # Never let a source column land on a generated v3 key — that would
        # carry v2 ids into v3, collide with rows already there, and defeat the
        # remapping. Claim it up front so nothing can match it.
        tgt_key_col = tgt_cols.get(target_key)
        key_is_generated = tgt_key_col is not None and _is_generated_key(tgt_key_col, tgt_profile)
        if key_is_generated:
            used_targets.add(tgt_key_col.name)

        # Which source column carries the old system's identity? Normally the
        # primary key — but a harvested staging table's `_id` is just a row
        # number the scraper assigned. The real identity is the upstream `id`,
        # or the `_key` the harvester deduplicates on.
        source_names = {c.name for c in table.columns}
        legacy_source = key
        if STAGING_MARKERS <= source_names:
            legacy_source = "id" if "id" in source_names else "_key"

        legacy_col = next(
            (c.name for c in tgt_cols.values() if LEGACY_ID.search(c.name)), None
        )

        if key_is_generated:
            src_col = next((c for c in table.columns if c.name == legacy_source), None)
            if legacy_col and src_col is not None:
                used_targets.add(legacy_col)
                fields.append(
                    FieldMap(
                        to=legacy_col,
                        **{"from": legacy_source},
                        transform=_infer_transforms(src_col, tgt_cols.get(legacy_col)),
                        note=(
                            f"v2 identity preserved here; v3 generates its own "
                            f"'{target_key}'."
                        ),
                    )
                )
            elif src_col is not None:
                notes.append(
                    f"TODO: v3 generates '{target_key}', so the v2 identity "
                    f"'{legacy_source}' is not carried over and no legacy-id column "
                    f"exists to hold it. The id map records the pairing, but v3 "
                    f"rows will have no trace of their v2 id — add a column if "
                    f"you need that lineage."
                )
                warnings.append(
                    f"{table.name}: v2 identity '{legacy_source}' has nowhere to land "
                    f"in '{target_table}'"
                )

        # Real columns get first pick of the target columns; the harvester's
        # bookkeeping (`_fetched_at`, `_key`, `_url`) is considered last.
        # Otherwise `_fetched_at` fuzzy-matches a target `created_at` before
        # the actual `created_at` is even reached, and every migrated row
        # silently carries the scrape time as its creation date.
        ordered_columns = sorted(table.columns, key=lambda c: c.name.startswith("_"))

        for col in ordered_columns:
            if key_is_generated and col.name == legacy_source:
                continue  # already routed to the legacy column above

            target_col, col_score = (None, 0.0)
            if tgt_index:
                pool = {n: v for n, v in tgt_index.items() if n not in used_targets}
                target_col, col_score = _best(_norm(col.name), pool)
            if not tgt_index:
                target_col, col_score = col.name, 0.0

            if tgt_index and (target_col is None or col_score < MATCH_THRESHOLD):
                warnings.append(
                    f"{table.name}.{col.name}: no v3 column match — dropped from the draft"
                )
                continue

            used_targets.add(target_col or "")
            tgt_col = tgt_cols.get(target_col or "")

            steps = _infer_transforms(col, tgt_col)
            note: str | None = None

            if col.name in fk_targets:
                steps = [{"lookup": {"entity": fk_targets[col.name], "required": False}}]
                confidence = fk_confidence.get(col.name, 1.0)
                if not fk_inferred.get(col.name, False):
                    note = (
                        f"foreign key -> '{fk_targets[col.name]}'. Set required: true "
                        f"once you are sure every referenced row migrates."
                    )
                else:
                    note = (
                        f"TODO: inferred relationship -> '{fk_targets[col.name]}' "
                        f"({confidence:.0%} of sampled values matched). The schema does "
                        f"not declare it — confirm before migrating."
                    )
            elif tgt_index and col_score < 0.9:
                note = f"TODO: fuzzy column match ({col_score:.0%}) — confirm this pairing"

            # A column the database fills in itself is never 'required' of us.
            generated = bool(tgt_col and _is_generated_key(tgt_col, tgt_profile))
            required = bool(tgt_col and not tgt_col.nullable and not generated)

            fields.append(
                FieldMap(
                    to=target_col or col.name,
                    **{"from": col.name},
                    transform=steps,
                    required=required,
                    note=note,
                )
            )

        if not fields:
            warnings.append(f"{table.name}: skipped — no columns could be matched")
            continue

        # v3 columns nothing maps onto, that v3 insists on having.
        if tgt_profile:
            unmapped_required = [
                c.name
                for c in tgt_profile.columns
                if not c.nullable
                and not _is_generated_key(c, tgt_profile)
                and c.default is None
                and c.name not in used_targets
            ]
            if unmapped_required:
                notes.append(
                    "TODO: v3 requires these columns and v2 has no obvious source: "
                    + ", ".join(unmapped_required)
                )
                warnings.append(
                    f"{table.name}: unmapped NOT NULL target columns: "
                    f"{', '.join(unmapped_required)}"
                )

        if is_noise:
            notes.append("framework plumbing — disabled by default; enable if you need it")

        # `source.key` pages and resumes; `id_map_from` is what sibling rows
        # actually reference. On a harvested table these differ: `_id` is a
        # scraper row number, `id` is the application's own identifier.
        id_map_from = legacy_source if legacy_source != key else None

        entities.append(
            EntityMap(
                name=entity_name,
                enabled=not is_noise,
                id_map_from=id_map_from,
                source=SourceSpec(table=table.name, key=key, **{"schema": v2.schema}),
                target=TargetSpec(
                    table=target_table or table.name,
                    key=target_key,
                    conflict="skip",
                    **{"schema": v3.schema if v3 else None},
                ),
                depends_on=depends,
                fields=fields,
                id_map=True,
                note=" | ".join(notes) if notes else None,
            )
        )

    mapping = Mapping(version=1, defaults=Defaults(), entities=entities)

    # Drop dependencies on entities that were skipped.
    valid = {e.name for e in entities}
    for e in entities:
        pruned = [d for d in e.depends_on if d in valid]
        if len(pruned) != len(e.depends_on):
            warnings.append(
                f"{e.name}: dropped dependency on skipped entity/entities "
                f"{sorted(set(e.depends_on) - valid)}"
            )
        e.depends_on = pruned

    warnings.extend(_break_cycles(entities))
    return mapping, warnings


def _break_cycles(entities: list[EntityMap]) -> list[str]:
    """Circular foreign keys are legal in SQL but cannot be migrated in one pass.

    Drop the back-edge and tell the user, rather than letting topo_order blow up
    on a file they were told to trust.
    """
    warnings: list[str] = []
    by_name = {e.name: e for e in entities}
    state: dict[str, int] = {}

    def visit(name: str, trail: list[str]) -> None:
        if state.get(name) == 1:
            return
        if state.get(name) == 0:
            return
        state[name] = 0
        for dep in list(by_name[name].depends_on):
            if dep not in by_name:
                continue
            if state.get(dep) == 0:  # back-edge: dep is an ancestor of name
                by_name[name].depends_on.remove(dep)
                note = (
                    f"circular reference with '{dep}' — dependency dropped. Migrate "
                    f"this entity first, then backfill the '{dep}' reference in a "
                    f"second pass."
                )
                by_name[name].note = (
                    f"{by_name[name].note} | {note}" if by_name[name].note else note
                )
                warnings.append(f"{name}: {note}")
                continue
            visit(dep, [*trail, name])
        state[name] = 1

    for entity in entities:
        visit(entity.name, [])
    return warnings
