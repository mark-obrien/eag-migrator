"""Schema discovery.

This is the entry point for the whole project. We do not know what EAG v2 is
built on, so rather than assume a schema we introspect the live database and
write a profile: tables, columns, types, keys, foreign-key graph, row counts,
redacted samples, plus a best-effort guess at the framework that produced it.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, MetaData, Table, inspect, select

from .db import qualified, row_count

# --- Framework fingerprints -------------------------------------------------
# Table names (or suffixes, for prefixed installs like WordPress) that give
# away which framework generated the schema.
FINGERPRINTS: list[tuple[str, list[str], str]] = [
    ("WordPress", ["posts", "postmeta", "options", "term_taxonomy"], "suffix"),
    ("WooCommerce", ["woocommerce_order_items", "wc_orders", "wc_order_product_lookup"], "exact"),
    ("Laravel", ["migrations", "failed_jobs", "personal_access_tokens", "password_reset_tokens"], "exact"),
    ("Ruby on Rails", ["schema_migrations", "ar_internal_metadata"], "exact"),
    ("Django", ["django_migrations", "django_content_type", "auth_user"], "exact"),
    ("ASP.NET / EF Core", ["__efmigrationshistory"], "exact"),
    ("Magento", ["catalog_product_entity", "core_config_data", "sales_order"], "exact"),
    ("Drupal", ["node_field_data", "watchdog", "config"], "exact"),
    ("Joomla", ["content", "extensions", "usergroups"], "suffix"),
    ("PrestaShop", ["product_lang", "customer", "configuration"], "suffix"),
    ("Sequelize (Node)", ["sequelizemeta"], "exact"),
    ("Knex (Node)", ["knex_migrations"], "exact"),
    ("TypeORM", ["typeorm_metadata", "migrations"], "exact"),
    ("Prisma", ["_prisma_migrations"], "exact"),
    ("Strapi", ["strapi_core_store_settings", "strapi_migrations"], "exact"),
    ("Directus", ["directus_collections", "directus_fields"], "exact"),
    ("CodeIgniter", ["ci_sessions"], "exact"),
]

# Marker tables that half the world ships. On their own they prove nothing, so
# a fingerprint resting only on these is suppressed rather than reported as a
# confident guess.
GENERIC_MARKERS = {
    "migrations", "config", "sessions", "cache", "jobs", "customer",
    "options", "content", "users", "user",
}

# Auto-glass domain signals. NAGS is the industry-standard parts catalogue, so
# a nags* column or table is a strong hint about where the real value lives.
DOMAIN_SIGNALS = [
    "nags", "windshield", "glass", "vin", "vehicle", "make", "model", "trim",
    "quote", "estimate", "work_order", "workorder", "job", "claim", "insur",
    "adas", "calibrat", "technician", "tech", "appointment", "schedule", "dispatch",
    "customer", "invoice", "payment", "part", "inventory", "location", "shop",
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, set):
        return sorted(str(v) for v in value)
    return value


@dataclass
class ColumnProfile:
    name: str
    type: str
    nullable: bool
    default: str | None = None
    autoincrement: bool = False
    primary_key: bool = False
    comment: str | None = None


@dataclass
class ForeignKeyProfile:
    columns: list[str]
    referred_table: str
    referred_columns: list[str]
    referred_schema: str | None = None


@dataclass
class TableProfile:
    name: str
    schema: str | None
    columns: list[ColumnProfile] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKeyProfile] = field(default_factory=list)
    unique_constraints: list[list[str]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    row_count: int | None = None
    sample: list[dict[str, Any]] = field(default_factory=list)
    domain_hits: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class DatabaseProfile:
    side: str
    dialect: str
    server_version: str | None
    schema: str | None
    generated_at: str
    tables: list[TableProfile] = field(default_factory=list)
    detected_frameworks: list[dict[str, Any]] = field(default_factory=list)
    table_prefix: str | None = None

    def table(self, name: str) -> TableProfile | None:
        for t in self.tables:
            if t.name == name:
                return t
        return None


def _detect_prefix(names: list[str]) -> str | None:
    """Find a common table prefix like 'wp_' or 'eag_' if one dominates."""
    candidates: dict[str, int] = {}
    for n in names:
        m = re.match(r"^([a-z0-9]{1,12}_)", n)
        if m:
            candidates[m.group(1)] = candidates.get(m.group(1), 0) + 1
    if not candidates:
        return None
    prefix, count = max(candidates.items(), key=lambda kv: kv[1])
    # Only call it a prefix if it covers a real majority of the schema.
    return prefix if count >= max(3, len(names) * 0.5) else None


def _detect_frameworks(names: list[str], prefix: str | None) -> list[dict[str, Any]]:
    lowered = {n.lower() for n in names}
    stripped = {n[len(prefix):].lower() for n in names if prefix and n.startswith(prefix)}

    found: list[dict[str, Any]] = []
    for label, markers, mode in FINGERPRINTS:
        pool = stripped if mode == "suffix" and stripped else lowered
        hits = [m for m in markers if m in pool or m in lowered]
        if not hits:
            continue
        distinctive = [h for h in hits if h not in GENERIC_MARKERS]
        if not distinctive and len(hits) < 2:
            continue
        found.append(
            {
                "framework": label,
                "matched_tables": hits,
                "confidence": round(len(hits) / len(markers), 2),
                "distinctive_matches": distinctive,
            }
        )
    found.sort(key=lambda f: (-len(f["distinctive_matches"]), -f["confidence"], f["framework"]))
    return found


def _domain_hits(table: str, columns: list[str]) -> list[str]:
    haystack = " ".join([table.lower(), *[c.lower() for c in columns]])
    return sorted({sig for sig in DOMAIN_SIGNALS if sig in haystack})


def profile_database(
    engine: Engine,
    side: str,
    *,
    schema: str | None = None,
    sample_rows: int = 5,
    with_counts: bool = True,
    redactor: re.Pattern[str] | None = None,
    only: list[str] | None = None,
) -> DatabaseProfile:
    insp = inspect(engine)

    server_version = None
    try:
        raw = engine.dialect.server_version_info
        if raw:
            server_version = ".".join(str(p) for p in raw)
    except Exception:  # noqa: BLE001
        pass

    names = sorted(insp.get_table_names(schema=schema))
    if only:
        wanted = set(only)
        names = [n for n in names if n in wanted]

    prefix = _detect_prefix(names)
    profile = DatabaseProfile(
        side=side,
        dialect=engine.dialect.name,
        server_version=server_version,
        schema=schema,
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        detected_frameworks=_detect_frameworks(names, prefix),
        table_prefix=prefix,
    )

    for name in names:
        tp = TableProfile(name=name, schema=schema)
        try:
            pk = insp.get_pk_constraint(name, schema=schema) or {}
            pk_cols = list(pk.get("constrained_columns") or [])
            tp.primary_key = pk_cols

            for col in insp.get_columns(name, schema=schema):
                tp.columns.append(
                    ColumnProfile(
                        name=col["name"],
                        type=str(col.get("type")),
                        nullable=bool(col.get("nullable", True)),
                        default=(str(col["default"]) if col.get("default") is not None else None),
                        autoincrement=bool(col.get("autoincrement") or False),
                        primary_key=col["name"] in pk_cols,
                        comment=col.get("comment"),
                    )
                )

            for fk in insp.get_foreign_keys(name, schema=schema):
                tp.foreign_keys.append(
                    ForeignKeyProfile(
                        columns=list(fk.get("constrained_columns") or []),
                        referred_table=fk.get("referred_table") or "",
                        referred_columns=list(fk.get("referred_columns") or []),
                        referred_schema=fk.get("referred_schema"),
                    )
                )

            for uc in insp.get_unique_constraints(name, schema=schema):
                tp.unique_constraints.append(list(uc.get("column_names") or []))

            for ix in insp.get_indexes(name, schema=schema):
                tp.indexes.append(
                    {
                        "name": ix.get("name"),
                        "columns": list(ix.get("column_names") or []),
                        "unique": bool(ix.get("unique")),
                    }
                )

            tp.domain_hits = _domain_hits(name, [c.name for c in tp.columns])

            if with_counts:
                tp.row_count = row_count(engine, name, schema)

            if sample_rows > 0:
                tp.sample = _sample(engine, name, schema, sample_rows, redactor)

        except Exception as exc:  # noqa: BLE001 - one bad table must not kill discovery
            tp.error = f"{type(exc).__name__}: {exc}"

        profile.tables.append(tp)

    return profile


def _sample(
    engine: Engine,
    table: str,
    schema: str | None,
    limit: int,
    redactor: re.Pattern[str] | None,
) -> list[dict[str, Any]]:
    md = MetaData()
    tbl = Table(table, md, autoload_with=engine, schema=schema)
    with engine.connect() as conn:
        rows = conn.execute(select(tbl).limit(limit)).mappings().all()

    out: list[dict[str, Any]] = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if redactor and redactor.search(key):
                clean[key] = "<redacted>" if value is not None else None
            else:
                val = _jsonable(value)
                if isinstance(val, str) and len(val) > 200:
                    val = val[:200] + f"... (+{len(val) - 200} chars)"
                clean[key] = val
        out.append(clean)
    return out


def save_profile(profile: DatabaseProfile, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(profile), indent=2, default=_jsonable), encoding="utf-8")
    return path


def load_profile(path: Path) -> DatabaseProfile:
    data = json.loads(path.read_text(encoding="utf-8"))
    tables = []
    for t in data.get("tables", []):
        tables.append(
            TableProfile(
                name=t["name"],
                schema=t.get("schema"),
                columns=[ColumnProfile(**c) for c in t.get("columns", [])],
                primary_key=t.get("primary_key", []),
                foreign_keys=[ForeignKeyProfile(**fk) for fk in t.get("foreign_keys", [])],
                unique_constraints=t.get("unique_constraints", []),
                indexes=t.get("indexes", []),
                row_count=t.get("row_count"),
                sample=t.get("sample", []),
                domain_hits=t.get("domain_hits", []),
                error=t.get("error"),
            )
        )
    return DatabaseProfile(
        side=data["side"],
        dialect=data["dialect"],
        server_version=data.get("server_version"),
        schema=data.get("schema"),
        generated_at=data["generated_at"],
        tables=tables,
        detected_frameworks=data.get("detected_frameworks", []),
        table_prefix=data.get("table_prefix"),
    )


def render_markdown(profile: DatabaseProfile) -> str:
    """Human-readable report — this is what you send to whoever owns EAG."""
    lines: list[str] = []
    lines.append(f"# EAG {profile.side} schema profile\n")
    lines.append(f"- Engine: **{profile.dialect}** {profile.server_version or ''}".rstrip())
    lines.append(f"- Schema: `{profile.schema or '(default)'}`")
    lines.append(f"- Tables: **{len(profile.tables)}**")
    if profile.table_prefix:
        lines.append(f"- Common table prefix: `{profile.table_prefix}`")
    total = sum(t.row_count or 0 for t in profile.tables)
    lines.append(f"- Total rows: **{total:,}**")
    lines.append(f"- Generated: {profile.generated_at}\n")

    lines.append("## Detected framework\n")
    if profile.detected_frameworks:
        lines.append("| Framework | Confidence | Matched tables |")
        lines.append("|---|---|---|")
        for f in profile.detected_frameworks:
            lines.append(
                f"| {f['framework']} | {f['confidence']:.0%} | "
                f"`{'`, `'.join(f['matched_tables'])}` |"
            )
    else:
        lines.append("_No known framework fingerprint matched — likely a bespoke schema._")
    lines.append("")

    ranked = sorted(profile.tables, key=lambda t: (-(t.row_count or 0), t.name))
    lines.append("## Tables by size\n")
    lines.append("| Table | Rows | Cols | PK | FKs | Domain signals |")
    lines.append("|---|---:|---:|---|---:|---|")
    for t in ranked:
        pk = ", ".join(t.primary_key) or "—"
        sig = ", ".join(t.domain_hits[:5]) or "—"
        lines.append(
            f"| `{t.name}` | {t.row_count if t.row_count is not None else '?':,} | "
            f"{len(t.columns)} | {pk} | {len(t.foreign_keys)} | {sig} |"
            if isinstance(t.row_count, int)
            else f"| `{t.name}` | ? | {len(t.columns)} | {pk} | {len(t.foreign_keys)} | {sig} |"
        )
    lines.append("")

    interesting = [t for t in ranked if t.domain_hits and (t.row_count or 0) > 0]
    if interesting:
        lines.append("## Likely business-critical tables\n")
        lines.append("Tables whose names or columns match auto-glass domain vocabulary.\n")
        for t in interesting[:25]:
            lines.append(f"### `{t.name}` — {t.row_count:,} rows" if isinstance(t.row_count, int) else f"### `{t.name}`")
            lines.append("")
            lines.append("| Column | Type | Null | PK |")
            lines.append("|---|---|---|---|")
            for c in t.columns:
                lines.append(
                    f"| `{c.name}` | {c.type} | {'yes' if c.nullable else 'NO'} | "
                    f"{'✓' if c.primary_key else ''} |"
                )
            if t.foreign_keys:
                lines.append("")
                lines.append("Foreign keys:")
                for fk in t.foreign_keys:
                    lines.append(
                        f"- `{', '.join(fk.columns)}` → "
                        f"`{fk.referred_table}({', '.join(fk.referred_columns)})`"
                    )
            lines.append("")

    orphans = [t for t in profile.tables if not t.primary_key and not t.error]
    if orphans:
        lines.append("## Tables with no primary key\n")
        lines.append("These need an explicit `source.key` in the mapping — the migrator")
        lines.append("cannot page or resume through them otherwise.\n")
        for t in orphans:
            lines.append(f"- `{t.name}`")
        lines.append("")

    errored = [t for t in profile.tables if t.error]
    if errored:
        lines.append("## Tables that failed to introspect\n")
        for t in errored:
            lines.append(f"- `{t.name}`: {t.error}")
        lines.append("")

    return "\n".join(lines)
