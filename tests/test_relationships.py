"""Inferring relationships a schema never declared.

Needed far more often than it sounds: MyISAM tables carry no foreign keys at
all, plenty of older applications never declared them, and data harvested from
an API has no constraints by definition. Without this the mapping draft wires
nothing together and every child row points at a v2 id that means nothing in
v3.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eag_migrator.db import build_engine
from eag_migrator.discovery import profile_database
from eag_migrator.scaffold import build_draft

# No FOREIGN KEY clauses anywhere — exactly what a harvested or MyISAM v2 looks
# like. `quotes.customer_id` points at `customers.id`, not the `_id` row number
# the harvester assigned.
V2_DDL = """
CREATE TABLE customers (
    _id INTEGER PRIMARY KEY AUTOINCREMENT, _key TEXT UNIQUE, _url TEXT,
    _fetched_at TEXT, id INTEGER, name TEXT, email TEXT
);
CREATE TABLE quotes (
    _id INTEGER PRIMARY KEY AUTOINCREMENT, _key TEXT UNIQUE, _url TEXT,
    _fetched_at TEXT, id INTEGER, customer_id INTEGER, total TEXT,
    job_number TEXT
);
CREATE TABLE appointments (
    _id INTEGER PRIMARY KEY AUTOINCREMENT, _key TEXT UNIQUE, _url TEXT,
    _fetched_at TEXT, id INTEGER, quote_id INTEGER, technician TEXT
);
"""

V3_DDL = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, legacy_id INTEGER,
    name TEXT, email TEXT
);
CREATE TABLE quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, legacy_id INTEGER,
    customer_id INTEGER NOT NULL, total NUMERIC(10,2), job_number TEXT
);
CREATE TABLE appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, legacy_id INTEGER,
    quote_id INTEGER NOT NULL, technician TEXT
);
"""


@pytest.fixture
def v2(tmp_path: Path) -> str:
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(V2_DDL)
    conn.executemany(
        "INSERT INTO customers (_key, id, name, email) VALUES (?,?,?,?)",
        [(f"c{i}", 5000 + i, f"Customer {i}", f"c{i}@example.com") for i in range(1, 6)],
    )
    conn.executemany(
        "INSERT INTO quotes (_key, id, customer_id, total, job_number) VALUES (?,?,?,?,?)",
        [(f"q{i}", 9000 + i, 5000 + ((i - 1) % 5) + 1, "100.00", f"JOB-{i:04d}")
         for i in range(1, 9)],
    )
    conn.executemany(
        "INSERT INTO appointments (_key, id, quote_id, technician) VALUES (?,?,?,?)",
        [(f"a{i}", 7000 + i, 9000 + i, "Lee Park") for i in range(1, 5)],
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


@pytest.fixture
def v3(tmp_path: Path) -> str:
    path = tmp_path / "v3.sqlite"
    sqlite3.connect(path).executescript(V3_DDL)
    return f"sqlite:///{path}"


# --- inference --------------------------------------------------------------


def test_relationships_are_inferred_when_the_schema_declares_none(v2):
    profile = profile_database(build_engine(v2), "v2", sample_rows=0)

    quotes = profile.table("quotes")
    assert quotes.foreign_keys == []          # nothing declared
    assert len(quotes.inferred_foreign_keys) == 1

    fk = quotes.inferred_foreign_keys[0]
    assert fk.columns == ["customer_id"]
    assert fk.referred_table == "customers"
    assert fk.inferred and fk.confidence == 1.0
    assert "matched" in (fk.evidence or "")


def test_inference_targets_the_application_id_not_the_scraper_row_number(v2):
    """`customers._id` is a harvest artefact; children reference `customers.id`."""
    profile = profile_database(build_engine(v2), "v2", sample_rows=0)
    fk = profile.table("quotes").inferred_foreign_keys[0]

    assert fk.referred_columns == ["id"]
    assert fk.referred_columns != ["_id"]


def test_inference_is_proved_against_data_not_just_naming(v2, tmp_path):
    """A plausibly-named column whose values match nothing must be rejected."""
    conn = sqlite3.connect(str(v2).replace("sqlite:///", ""))
    conn.execute("UPDATE quotes SET customer_id = customer_id + 900000")
    conn.commit()
    conn.close()

    profile = profile_database(build_engine(v2), "v2", sample_rows=0)
    assert profile.table("quotes").inferred_foreign_keys == []


def test_unrelated_id_like_columns_are_not_invented_as_relationships(v2):
    """`job_number` matches the <base>_<suffix> shape but there is no job table."""
    profile = profile_database(build_engine(v2), "v2", sample_rows=0)
    inferred = {
        fk.columns[0]
        for t in profile.tables
        for fk in t.inferred_foreign_keys
    }
    assert "job_number" not in inferred


def test_declared_keys_are_not_duplicated_by_inference(tmp_path):
    path = tmp_path / "declared.sqlite"
    sqlite3.connect(path).executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE quotes (id INTEGER PRIMARY KEY, customer_id INTEGER"
        " REFERENCES customers(id));"
        "INSERT INTO customers VALUES (1,'a');"
        "INSERT INTO quotes VALUES (10,1);"
    )
    profile = profile_database(build_engine(f"sqlite:///{path}"), "v2", sample_rows=0)
    quotes = profile.table("quotes")

    assert len(quotes.foreign_keys) == 1
    assert quotes.inferred_foreign_keys == []   # already declared, not re-added


# --- what the scaffolder does with them -------------------------------------


def test_scaffold_wires_inferred_relationships_into_lookups(v2, v3):
    v2p = profile_database(build_engine(v2), "v2", sample_rows=0)
    v3p = profile_database(build_engine(v3), "v3", sample_rows=0)
    mapping, warnings = build_draft(v2p, v3p)

    quote = mapping.get("quote")
    fk_field = next(f for f in quote.fields if f.to == "customer_id")

    step = next(s for s in fk_field.transform if isinstance(s, dict) and "lookup" in s)
    assert step["lookup"]["entity"] == "customer"
    assert "inferred relationship" in (fk_field.note or "")
    assert any("not declared" in w for w in warnings)


def test_scaffold_orders_entities_by_the_inferred_graph(v2, v3):
    v2p = profile_database(build_engine(v2), "v2", sample_rows=0)
    v3p = profile_database(build_engine(v3), "v3", sample_rows=0)
    mapping, _ = build_draft(v2p, v3p)

    order = [e.name for e in mapping.topo_order()]
    assert order.index("customer") < order.index("quote")
    assert order.index("quote") < order.index("appointment")


def test_scaffold_keys_the_id_map_on_the_application_id(v2, v3):
    """Lookups resolve on the value children carry, not the harvester's row id."""
    v2p = profile_database(build_engine(v2), "v2", sample_rows=0)
    v3p = profile_database(build_engine(v3), "v3", sample_rows=0)
    mapping, _ = build_draft(v2p, v3p)

    customer = mapping.get("customer")
    assert customer.source.key == "_id"      # paging and resume
    assert customer.id_map_from == "id"      # what lookups resolve against
    assert customer.map_key == "id"


# --- end to end -------------------------------------------------------------


def test_relationships_survive_the_migration(v2, v3, tmp_path):
    """The real proof: v3 rows point at v3 ids, and every link resolves."""
    from eag_migrator.adapters import SqlSink, SqlSource
    from eag_migrator.config import Settings
    from eag_migrator.mapping import Mapping
    from eag_migrator.runner import Runner
    from eag_migrator.state import RunState

    v2p = profile_database(build_engine(v2), "v2", sample_rows=0)
    v3p = profile_database(build_engine(v3), "v3", sample_rows=0)
    mapping, _ = build_draft(v2p, v3p)

    # The draft marks inferred lookups optional; a real cutover would confirm
    # them and set required: true. Do that here so a broken link fails loudly.
    for entity in mapping.entities:
        for fmap in entity.fields:
            for step in fmap.transform:
                if isinstance(step, dict) and "lookup" in step:
                    step["lookup"]["required"] = True

    settings = Settings(v2_url=v2, v3_url=v3, batch_size=3)
    with RunState(tmp_path / "state.sqlite") as state:
        runner = Runner(
            settings,
            Mapping.model_validate(mapping.model_dump(by_alias=True)),
            SqlSource(build_engine(v2)),
            SqlSink(build_engine(v3)),
            state,
        )
        report = runner.apply()

    assert report.total_failed == 0

    conn = sqlite3.connect(v3.replace("sqlite:///", ""))
    conn.row_factory = sqlite3.Row
    customers = {r["legacy_id"]: r["id"] for r in conn.execute("SELECT * FROM customers")}
    quotes = {r["legacy_id"]: r for r in conn.execute("SELECT * FROM quotes")}
    appointments = list(conn.execute("SELECT * FROM appointments"))

    assert len(customers) == 5 and len(quotes) == 8 and len(appointments) == 4

    # Every quote points at a real v3 customer id, remapped from the v2 one.
    for legacy_id, row in quotes.items():
        assert row["customer_id"] in customers.values()
    # And the two-hop chain holds: appointment -> quote -> customer.
    quote_ids = {r["id"] for r in quotes.values()}
    for appt in appointments:
        assert appt["quote_id"] in quote_ids


# --- tenant parameterisation ------------------------------------------------


def test_config_expands_environment_references(monkeypatch):
    """One config, many shops: <tenant>.everythingautoglass.com."""
    from eag_migrator.config import expand_env

    monkeypatch.setenv("TENANT", "zephyrglass")
    assert (
        expand_env("url: https://${TENANT}.everythingautoglass.com")
        == "url: https://zephyrglass.everythingautoglass.com"
    )
    assert expand_env("x: ${UNSET_THING:-fallback}") == "x: fallback"


def test_unset_reference_fails_loudly(monkeypatch):
    """Substituting an empty string would silently produce a bad URL."""
    from eag_migrator.config import expand_env

    monkeypatch.delenv("DEFINITELY_UNSET", raising=False)
    with pytest.raises(ValueError, match="DEFINITELY_UNSET"):
        expand_env("url: https://${DEFINITELY_UNSET}.example.com")


def test_harvest_config_honours_the_tenant_variable(tmp_path, monkeypatch):
    from eag_migrator.web.harvest import load_config

    monkeypatch.setenv("TENANT", "zephyrglass")
    path = tmp_path / "harvest.yaml"
    path.write_text(
        "version: 1\n"
        "site:\n"
        "  base_url: https://${TENANT}.everythingautoglass.com\n"
        "  requires_auth: true\n"
        "collections: []\n"
    )
    config = load_config(path)
    assert config.site.base_url == "https://zephyrglass.everythingautoglass.com"
