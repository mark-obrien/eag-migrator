"""Fixture databases shaped like a plausible auto-glass shop.

These stand in for EAG v2/v3 until the real schemas turn up. SQLite is used so
the test suite runs without docker; the engine itself is dialect-agnostic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from eag_migrator.adapters import SqlSink, SqlSource
from eag_migrator.config import Settings
from eag_migrator.db import build_engine
from eag_migrator.mapping import Mapping
from eag_migrator.runner import Runner
from eag_migrator.state import RunState

V2_DDL = """
CREATE TABLE tbl_customer (
    cust_id       INTEGER PRIMARY KEY,
    email_address TEXT,
    first_name    TEXT,
    last_name     TEXT,
    phone_number  TEXT,
    zip           TEXT,
    acct_status   TEXT,
    date_created  TEXT,
    comments      TEXT,
    deleted       INTEGER DEFAULT 0
);

CREATE TABLE tbl_vehicle (
    veh_id     INTEGER PRIMARY KEY,
    cust_id    INTEGER REFERENCES tbl_customer(cust_id),
    vin_number TEXT,
    veh_year   TEXT,
    veh_make   TEXT,
    veh_model  TEXT
);

CREATE TABLE tbl_workorder (
    wo_id        INTEGER PRIMARY KEY,
    cust_id      INTEGER REFERENCES tbl_customer(cust_id),
    veh_id       INTEGER REFERENCES tbl_vehicle(veh_id),
    part_no      TEXT,
    total_amount TEXT,
    sched_date   TEXT,
    mobile_flag  TEXT
);

CREATE TABLE migrations (
    id      INTEGER PRIMARY KEY,
    version TEXT
);
"""

V3_DDL = """
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id   INTEGER,
    email       TEXT NOT NULL,
    full_name   TEXT,
    phone       TEXT,
    postal_code TEXT,
    status      TEXT NOT NULL,
    created_at  DATETIME,
    notes       TEXT
);

CREATE TABLE vehicles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    vin         TEXT,
    year        INTEGER,
    make        TEXT,
    model       TEXT
);

CREATE TABLE work_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id      INTEGER NOT NULL,
    vehicle_id       INTEGER,
    nags_part_number TEXT,
    total_amount     NUMERIC(12, 2),
    scheduled_for    DATETIME,
    is_mobile        BOOLEAN,
    source_system    TEXT
);
"""

CUSTOMERS = [
    (1, "  Dana@Example.COM ", "Dana", "Reyes", "(555) 123-4567", "94107", "A",
     "2021-03-04 09:15:00", "<p>Prefers <b>mobile</b> service</p>", 0),
    (2, "sam.oyelaran@example.com", "Sam", "Oyelaran", "555-987-6543", "94107-2233", "I",
     "2022-07-19 14:02:11", None, 0),
    (3, "kit@example.org", "Kit", "Nakamura", "5551112222", "10001", "H",
     "0000-00-00 00:00:00", "  ", 0),
    # No email: v3 declares it NOT NULL, so this row must fail loudly.
    (4, None, "Nil", "Person", "5553334444", "60601", "A", "2023-01-01 00:00:00", None, 0),
    # Soft-deleted: excluded by the mapping's WHERE clause.
    (5, "gone@example.com", "Gone", "Away", "5555555555", "30301", "I",
     "2020-01-01 00:00:00", None, 1),
]

VEHICLES = [
    (10, 1, "1hgcm82633a004352", "2003", "honda", "accord"),
    (11, 1, "5NPE24AF1FH-012345", "2015", "hyundai", "sonata"),
    (12, 2, None, "2019", "ford", "f-150"),
    (13, 3, "JH4KA7561PC008269", "1993", "acura", "legend"),
]

WORK_ORDERS = [
    (100, 1, 10, "dw01234 gtyn", "$449.99", "2024-05-01 08:00:00", "Y"),
    (101, 1, 11, "fw02345", "1,250.00", "2024-05-03 13:30:00", "N"),
    (102, 2, 12, "dw03456", "310.50", "2024-06-11 10:00:00", "Y"),
    (103, 3, 13, None, "0", "2024-06-12 16:45:00", "n"),
]

MAPPING_YAML = """
version: 1
defaults:
  batch_size: 2
  on_error: record
entities:
  - name: customers
    source:
      table: tbl_customer
      key: cust_id
      where: "deleted = 0"
    target:
      table: customers
      key: id
      conflict: skip
    fields:
      - to: legacy_id
        from: cust_id
        transform: [int]
      - to: email
        from: email_address
        transform: [trim, email]
        required: true
      - to: full_name
        from: [first_name, last_name]
        transform:
          - concat: {sep: " "}
      - to: phone
        from: phone_number
        transform: [phone]
      - to: postal_code
        from: zip
        transform:
          - postal_code: {region: US}
      - to: status
        from: acct_status
        transform:
          - map:
              values: {A: active, I: inactive, H: on_hold, "*": inactive}
        required: true
      - to: created_at
        from: date_created
        transform: [to_datetime]
        default: "@now"
      - to: notes
        from: comments
        transform: [strip_html, trim, nullif_empty]

  - name: vehicles
    source:
      table: tbl_vehicle
      key: veh_id
    target:
      table: vehicles
      key: id
      conflict: skip
    depends_on: [customers]
    fields:
      - to: customer_id
        from: cust_id
        transform:
          - lookup: {entity: customers, required: true}
        required: true
      - to: vin
        from: vin_number
        transform: [vin]
      - to: year
        from: veh_year
        transform: [int]
      - to: make
        from: veh_make
        transform: [trim, title]
      - to: model
        from: veh_model
        transform: [trim, title]

  - name: work_orders
    source:
      table: tbl_workorder
      key: wo_id
    target:
      table: work_orders
      key: id
      conflict: skip
    depends_on: [customers, vehicles]
    fields:
      - to: customer_id
        from: cust_id
        transform:
          - lookup: {entity: customers, required: true}
        required: true
      - to: vehicle_id
        from: veh_id
        transform:
          - lookup: {entity: vehicles, required: false}
      - to: nags_part_number
        from: part_no
        transform: [nags]
      - to: total_amount
        from: total_amount
        transform:
          - decimal: {places: 2}
      - to: scheduled_for
        from: sched_date
        transform: [to_datetime]
      - to: is_mobile
        from: mobile_flag
        transform: [bool]
        default: false
      - to: source_system
        const: "eag-v2"
"""


def _exec_script(engine, script: str) -> None:
    with engine.begin() as conn:
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            conn.execute(text(stmt))


@pytest.fixture
def v2_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'v2.db'}"
    engine = build_engine(url)
    _exec_script(engine, V2_DDL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tbl_customer VALUES "
                "(:a,:b,:c,:d,:e,:f,:g,:h,:i,:j)"
            ),
            [dict(zip("abcdefghij", row)) for row in CUSTOMERS],
        )
        conn.execute(
            text("INSERT INTO tbl_vehicle VALUES (:a,:b,:c,:d,:e,:f)"),
            [dict(zip("abcdef", row)) for row in VEHICLES],
        )
        conn.execute(
            text("INSERT INTO tbl_workorder VALUES (:a,:b,:c,:d,:e,:f,:g)"),
            [dict(zip("abcdefg", row)) for row in WORK_ORDERS],
        )
        conn.execute(text("INSERT INTO migrations VALUES (1, '2021_01_01')"))
    engine.dispose()
    return url


@pytest.fixture
def v3_url(tmp_path: Path) -> str:
    url = f"sqlite:///{tmp_path / 'v3.db'}"
    engine = build_engine(url)
    _exec_script(engine, V3_DDL)
    engine.dispose()
    return url


@pytest.fixture
def settings(v2_url: str, v3_url: str) -> Settings:
    return Settings(v2_url=v2_url, v3_url=v3_url, batch_size=2, on_error="record")


@pytest.fixture
def mapping() -> Mapping:
    import yaml

    return Mapping.model_validate(yaml.safe_load(MAPPING_YAML))


@pytest.fixture
def state(tmp_path: Path):
    st = RunState(tmp_path / "state.sqlite")
    yield st
    st.close()


@pytest.fixture
def runner(settings: Settings, mapping: Mapping, state: RunState) -> Runner:
    return Runner(
        settings,
        mapping,
        SqlSource(build_engine(settings.v2_url)),
        SqlSink(build_engine(settings.v3_url)),
        state,
    )
