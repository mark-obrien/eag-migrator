"""End-to-end: discover -> scaffold -> plan -> run -> verify -> rollback."""

from __future__ import annotations

from sqlalchemy import text

from eag_migrator.db import build_engine
from eag_migrator.discovery import profile_database
from eag_migrator.scaffold import build_draft
from eag_migrator.verify import verify as run_verify


def _rows(url: str, table: str) -> list[dict]:
    engine = build_engine(url)
    with engine.connect() as conn:
        out = [dict(r) for r in conn.execute(text(f"SELECT * FROM {table}")).mappings().all()]
    engine.dispose()
    return out


# --- discovery --------------------------------------------------------------


def test_discovery_finds_tables_keys_and_foreign_keys(v2_url):
    profile = profile_database(build_engine(v2_url), "v2", sample_rows=2)

    names = {t.name for t in profile.tables}
    assert {"tbl_customer", "tbl_vehicle", "tbl_workorder"} <= names

    vehicles = profile.table("tbl_vehicle")
    assert vehicles.primary_key == ["veh_id"]
    assert vehicles.row_count == 4
    assert any(fk.referred_table == "tbl_customer" for fk in vehicles.foreign_keys)


def test_discovery_flags_auto_glass_domain_tables(v2_url):
    profile = profile_database(build_engine(v2_url), "v2", sample_rows=1)
    assert "vin" in profile.table("tbl_vehicle").domain_hits
    assert "customer" in profile.table("tbl_customer").domain_hits


def test_discovery_redacts_sensitive_columns(v2_url):
    import re

    engine = build_engine(v2_url)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE tbl_customer ADD COLUMN password_hash TEXT"))
        conn.execute(text("UPDATE tbl_customer SET password_hash = 'supersecret'"))

    profile = profile_database(
        engine, "v2", sample_rows=3, redactor=re.compile("password|hash", re.I)
    )
    sample = profile.table("tbl_customer").sample
    assert sample and all(row["password_hash"] == "<redacted>" for row in sample)
    # Non-sensitive columns are untouched.
    assert any(row["first_name"] == "Dana" for row in sample)


# --- scaffolding ------------------------------------------------------------


def test_scaffold_drafts_a_valid_mapping_from_two_profiles(v2_url, v3_url):
    v2 = profile_database(build_engine(v2_url), "v2", sample_rows=0)
    v3 = profile_database(build_engine(v3_url), "v3", sample_rows=0)

    mapping, warnings = build_draft(v2, v3)

    names = {e.name for e in mapping.entities}
    assert {"customer", "vehicle"} <= names

    # The framework's own migrations table is drafted but switched off.
    migrations = mapping.get("migration")
    assert migrations is not None and migrations.enabled is False

    # It orders customers before vehicles, having read the foreign key.
    order = [e.name for e in mapping.topo_order()]
    assert order.index("customer") < order.index("vehicle")

    # And it wires the FK into a lookup rather than copying the raw id.
    vehicle = mapping.get("vehicle")
    fk_field = next(f for f in vehicle.fields if f.to == "customer_id")
    assert any(isinstance(s, dict) and "lookup" in s for s in fk_field.transform)
    assert isinstance(warnings, list)


def test_scaffold_never_maps_the_v2_key_onto_a_generated_v3_key(v2_url, v3_url):
    """Carrying cust_id into customers.id would collide with existing v3 rows."""
    v2 = profile_database(build_engine(v2_url), "v2", sample_rows=0)
    v3 = profile_database(build_engine(v3_url), "v3", sample_rows=0)
    mapping, _ = build_draft(v2, v3)

    customer = mapping.get("customer")
    assert all(f.to != "id" for f in customer.fields)

    legacy = next(f for f in customer.fields if f.to == "legacy_id")
    assert legacy.from_ == "cust_id"
    assert "v3 generates its own" in (legacy.note or "")


def test_scaffold_warns_when_the_v2_key_has_nowhere_to_land(v2_url, v3_url):
    """No legacy-id column in v3 means the v2 id is lost — say so, loudly."""
    engine = build_engine(v3_url)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE customers DROP COLUMN legacy_id"))
    engine.dispose()

    v2 = profile_database(build_engine(v2_url), "v2", sample_rows=0)
    v3 = profile_database(build_engine(v3_url), "v3", sample_rows=0)
    mapping, warnings = build_draft(v2, v3)

    customer = mapping.get("customer")
    assert all(f.to != "id" for f in customer.fields)
    assert "nowhere to land" in " ".join(warnings)
    assert "no legacy-id column" in (customer.note or "")


def test_scaffold_infers_transforms_from_target_types(v2_url, v3_url):
    v2 = profile_database(build_engine(v2_url), "v2", sample_rows=0)
    v3 = profile_database(build_engine(v3_url), "v3", sample_rows=0)
    mapping, _ = build_draft(v2, v3)

    vehicle = mapping.get("vehicle")
    year = next(f for f in vehicle.fields if f.to == "year")
    assert "int" in year.transform


# --- dry run ----------------------------------------------------------------


def test_plan_writes_nothing_and_reports_the_bad_row(runner, v3_url):
    report = runner.plan()

    assert report.fatal is None
    customers = next(e for e in report.entities if e.name == "customers")
    # 4 live customers (the soft-deleted one is filtered out by WHERE).
    assert customers.total_source_rows == 4
    assert customers.processed == 4
    # The customer with no email fails its required check.
    assert customers.failed == 1
    assert customers.errors[0]["field"] == "email"

    # Crucially: nothing was written.
    assert _rows(v3_url, "customers") == []


def test_plan_flags_a_mapping_that_writes_a_column_v3_does_not_have(runner, mapping, v3_url):
    mapping.get("customers").fields[0].to = "no_such_column"
    report = runner.plan(["customers"])

    customers = report.entities[0]
    assert customers.aborted
    assert any("absent from" in note for note in customers.notes)
    assert _rows(v3_url, "customers") == []


def test_plan_flags_a_mapping_that_reads_a_column_v2_does_not_have(runner, mapping):
    mapping.get("customers").fields[1].from_ = "nope"
    report = runner.plan(["customers"])
    assert report.entities[0].aborted


# --- apply ------------------------------------------------------------------


def test_apply_migrates_and_transforms_every_layer(runner, v3_url):
    report = runner.apply()
    assert report.fatal is None

    customers = _rows(v3_url, "customers")
    assert len(customers) == 3  # 4 live rows, 1 rejected for a missing email

    dana = next(c for c in customers if c["legacy_id"] == 1)
    assert dana["email"] == "dana@example.com"
    assert dana["full_name"] == "Dana Reyes"
    assert dana["phone"] == "+15551234567"
    assert dana["status"] == "active"
    assert dana["notes"] == "Prefers mobile service"

    kit = next(c for c in customers if c["legacy_id"] == 3)
    assert kit["status"] == "on_hold"
    assert kit["created_at"] is not None  # the zero-date fell back to @now
    assert kit["notes"] is None  # whitespace-only became NULL

    # The soft-deleted customer never came across.
    assert all(c["legacy_id"] != 5 for c in customers)


def test_apply_resolves_foreign_keys_to_new_ids(runner, v3_url):
    """The FK must follow the customer's *new* v3 id, not carry the v2 id over.

    v3 is pre-seeded so its autoincrement starts well past the v2 ids: if the
    migrator were copying cust_id straight through, every vehicle would point
    at a customer that does not exist.
    """
    engine = build_engine(v3_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers (id, legacy_id, email, status) "
                "VALUES (5000, NULL, 'existing@example.com', 'active')"
            )
        )
    engine.dispose()

    runner.apply()

    customers = {c["legacy_id"]: c["id"] for c in _rows(v3_url, "customers")}
    vehicles = _rows(v3_url, "vehicles")

    accord = next(v for v in vehicles if v["model"] == "Accord")
    assert accord["customer_id"] == customers[1]
    assert accord["customer_id"] > 5000  # remapped, not copied from cust_id = 1
    assert accord["vin"] == "1HGCM82633A004352"
    assert accord["year"] == 2003
    assert accord["make"] == "Honda"


def test_rows_whose_parent_failed_are_rejected_not_orphaned(runner, v3_url):
    """Customer 4 fails on email, so nothing may point at a customer that isn't there."""
    runner.apply()

    vehicles = _rows(v3_url, "vehicles")
    valid_ids = {c["id"] for c in _rows(v3_url, "customers")}
    assert all(v["customer_id"] in valid_ids for v in vehicles)

    work_orders = _rows(v3_url, "work_orders")
    assert all(w["customer_id"] in valid_ids for w in work_orders)
    assert all(w["source_system"] == "eag-v2" for w in work_orders)

    # _rows() reads with raw SQL, so SQLite hands back its storage types (1/0
    # for BOOLEAN) rather than the Python values SQLAlchemy would convert to.
    mobile = next(w for w in work_orders if w["nags_part_number"] == "DW01234GTYN")
    assert bool(mobile["is_mobile"]) is True
    assert float(mobile["total_amount"]) == 449.99

    fixed = next(w for w in work_orders if w["nags_part_number"] == "FW02345")
    assert bool(fixed["is_mobile"]) is False


def test_rerunning_is_idempotent(runner, v3_url):
    runner.apply()
    first = _rows(v3_url, "customers")

    second_report = runner.apply()
    second = _rows(v3_url, "customers")

    assert len(second) == len(first)
    assert {c["id"] for c in second} == {c["id"] for c in first}
    customers = next(e for e in second_report.entities if e.name == "customers")
    assert customers.already_migrated == 3
    assert customers.inserted == 0


def test_interrupted_run_resumes_where_it_stopped(runner, v3_url):
    partial = runner.apply(["customers"], limit=2)
    assert len(_rows(v3_url, "customers")) == 2

    resumed = runner.apply(["customers"], resume_run_id=partial.run_id)
    assert len(_rows(v3_url, "customers")) == 3

    customers = next(e for e in resumed.entities if e.name == "customers")
    assert any("resuming after" in note for note in customers.notes)


def test_resuming_after_editing_the_mapping_is_refused(runner, mapping, v3_url):
    partial = runner.apply(["customers"], limit=2)
    mapping.get("customers").fields[1].required = False  # changes the fingerprint

    try:
        runner.apply(["customers"], resume_run_id=partial.run_id)
    except ValueError as exc:
        assert "different mapping" in str(exc)
    else:
        raise AssertionError("resuming across a mapping edit should be refused")


# --- verification -----------------------------------------------------------


def test_verify_passes_on_a_clean_migration(runner, mapping, state):
    report = runner.apply()
    result = run_verify(runner, mapping, state, report.run_id, sample_size=50)

    vehicles = next(e for e in result.entities if e.name == "vehicles")
    assert vehicles.checked == 4
    assert vehicles.mismatched == 0
    assert vehicles.ok

    # customers legitimately fails its count check: one source row was rejected.
    customers = next(e for e in result.entities if e.name == "customers")
    assert customers.mismatched == 0
    assert any(d.kind == "count" for d in customers.discrepancies)


def test_verify_does_not_flag_clock_generated_defaults(runner, mapping, state):
    """Customer 3's zero-date fell back to `default: "@now"`.

    Re-deriving that at verify time yields a different timestamp. That is not a
    discrepancy, and reporting it as one would train people to ignore the
    report.
    """
    report = runner.apply()
    result = run_verify(runner, mapping, state, report.run_id, sample_size=50)

    customers = next(e for e in result.entities if e.name == "customers")
    assert customers.mismatched == 0
    assert not any(d.field_name == "created_at" for d in customers.discrepancies)
    assert any("not diffed" in note and "created_at" in note for note in customers.notes)


def test_verify_detects_tampering_after_the_fact(runner, mapping, state, v3_url):
    report = runner.apply()

    engine = build_engine(v3_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE vehicles SET make = 'Tampered' WHERE model = 'Accord'"))
    engine.dispose()

    result = run_verify(runner, mapping, state, report.run_id, sample_size=50)
    vehicles = next(e for e in result.entities if e.name == "vehicles")
    assert vehicles.mismatched == 1
    assert not result.ok
    bad = next(d for d in vehicles.discrepancies if d.field_name == "make")
    assert bad.expected == "Honda" and bad.actual == "Tampered"


def test_verify_detects_rows_deleted_from_the_target(runner, mapping, state, v3_url):
    report = runner.apply()

    engine = build_engine(v3_url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM vehicles WHERE model = 'Accord'"))
    engine.dispose()

    result = run_verify(runner, mapping, state, report.run_id, sample_size=50)
    vehicles = next(e for e in result.entities if e.name == "vehicles")
    assert vehicles.missing_in_target == 1


# --- rollback ---------------------------------------------------------------


def test_rollback_removes_everything_the_run_wrote(runner, v3_url):
    report = runner.apply()
    assert _rows(v3_url, "customers")

    result = runner.rollback(report.run_id)

    assert result["rows_undone"] > 0
    assert _rows(v3_url, "customers") == []
    assert _rows(v3_url, "vehicles") == []
    assert _rows(v3_url, "work_orders") == []


def test_rollback_leaves_pre_existing_rows_alone(runner, v3_url):
    engine = build_engine(v3_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers (legacy_id, email, status) "
                "VALUES (999, 'preexisting@example.com', 'active')"
            )
        )
    engine.dispose()

    report = runner.apply()
    runner.rollback(report.run_id)

    survivors = _rows(v3_url, "customers")
    assert len(survivors) == 1
    assert survivors[0]["email"] == "preexisting@example.com"


def test_rollback_clears_the_id_map_so_a_retry_starts_clean(runner, state, v3_url):
    report = runner.apply()
    assert state.id_map_size("customers") == 3

    runner.rollback(report.run_id)
    assert state.id_map_size("customers") == 0

    runner.apply()
    assert len(_rows(v3_url, "customers")) == 3
