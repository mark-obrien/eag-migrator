# EAG v2 → v3 migrator

A dockerized migrator for moving Everything Auto Glass from the v2 site to v3.

**It is built discovery-first**, because the schema is not known up front. You
point it at the v2 database and it works out what is there: the tables, the
keys, the foreign-key graph, even which framework generated the schema. Then it
drafts the mapping for you. Nothing about EAG is hard-coded — the engine is
driven entirely by `config/mapping.yaml`.

---

## What you need to supply

The tool discovers the rest, but it cannot invent these:

1. **A connection to v2** — a read-only user is enough and is what you want.
   Alternatively a `.sql` dump dropped in `db/v2-seed/`, which the local v2
   container loads on first boot.
2. **A connection to v3** — or its dump in `db/v3-seed/`, or a base URL if you
   would rather write through v3's HTTP API.
3. **Answers to the questions the draft mapping asks.** Every guess it makes is
   marked `note: TODO`. Those are for whoever knows the business rules.

Credentials go in `.env`, which is gitignored. Do not commit dumps — `db/*-seed/`
is gitignored for the same reason.

---

## Quick start

```bash
make setup                 # creates .env from .env.example
$EDITOR .env               # put your real connection details in
make build

# Bring up whichever local databases you need (see "Which profile?" below)
docker compose --profile v2-mysql --profile v3-postgres up -d

make doctor                # can we reach both sides?
make discover              # introspect both, write profiles/
make scaffold              # draft config/mapping.draft.yaml

# --- review the draft, answer every TODO, then: ---
mv config/mapping.draft.yaml config/mapping.yaml

make plan                  # dry run: transform everything, write nothing
make migrate               # for real
make verify                # prove it landed correctly
```

If anything goes wrong: `make cli CMD="rollback <run-id>"`.

### Which profile?

You did not know which engine EAG runs on, so both are defined and neither
starts unless you ask for it:

| You need | Command |
|---|---|
| v2 on MySQL, v3 on Postgres | `docker compose --profile v2-mysql --profile v3-postgres up -d` |
| Both on MySQL | `docker compose --profile v2-mysql --profile v3-mysql up -d` |
| Both on Postgres | `docker compose --profile v2-postgres --profile v3-postgres up -d` |
| Neither — both are remote | just set the URLs in `.env`; start nothing |
| A browser DB client too | add `--profile tools`, then visit `localhost:18080` |

Set `COMPOSE_PROFILES=v2-mysql,v3-postgres` in `.env` to make `make up` do the
right thing without repeating the flags.

Local ports: v2-mysql `13306`, v2-postgres `15432`, v3-postgres `15433`,
v3-mysql `13307`, adminer `18080`.

---

## The workflow in detail

### 1. `eagm discover` — find out what EAG actually is

Introspects a live database and writes two files per side:

- `profiles/v2.json` — machine-readable, feeds the scaffolder
- `profiles/v2.md` — a readable report: tables by size, the columns of anything
  business-critical, the foreign-key graph, tables with no primary key

It also reports which framework the schema looks like (WordPress, Laravel,
Rails, Django, Magento, Prisma and a dozen others), and flags tables whose
names or columns match auto-glass vocabulary — `nags`, `vin`, `windshield`,
`adas`, `calibration`, `work_order`, `claim`.

Sensitive columns (`password`, `hash`, `token`, `ssn`, `card`, …) are redacted
from samples by default, so `profiles/v2.md` is safe to email to whoever owns
the system. Tune the list with `REDACT_PATTERNS` in `.env`.

### 2. `eagm scaffold` — draft the mapping

Matches v2 tables and columns against v3 by name similarity, infers transforms
from the target column types, wires foreign keys into `lookup` steps, and
orders entities so parents migrate before children.

**Treat the output as a questionnaire, not a config.** Every guess carries a
`note`, and every dropped column is listed in `reports/scaffold-warnings.txt`.
It deliberately refuses to map the v2 primary key onto a generated v3 key —
that would carry v2 ids into v3 and collide with rows already there. It routes
the old id to a `legacy_id`-style column if one exists, and tells you if none
does.

### 3. `eagm plan` — the dry run

Reads and transforms **every** row and writes nothing. It reports:

- rows that would fail, with the field and reason for each
- mapping columns that do not exist on either side (this blocks the entity)
- target columns nothing maps onto, and NOT NULL ones with no source
- how many rows already exist in v3 and what the conflict policy would do
- samples of the actual transformed output

Exit code is non-zero if anything would fail, so it drops straight into CI.

### 4. `eagm run` — migrate

Writes to v3. `--limit N` to migrate a slice first; `--entity customers` to do
one at a time.

### 5. `eagm verify` — prove it worked

Three independent checks per entity:

1. **Counts** — source rows vs rows recorded vs rows now in v3
2. **Spot-check** — re-derives N random rows from v2 and diffs them field by
   field against what is actually in v3
3. **Lookup health** — foreign keys that resolved to nothing

Type differences across engines (MySQL `DATETIME` vs Postgres `timestamptz`,
`Decimal` vs float, `tinyint(1)` vs `bool`) are normalised so they do not show
up as false alarms. Fields generated at migration time (`default: "@now"`) are
excluded from the diff and reported as such, since they cannot be re-derived.

### 6. `eagm rollback <run-id>` — undo

Deletes the rows the run inserted and restores the rows it updated, in reverse
dependency order. Rows that were already in v3 are untouched.

---

## The safety properties

| Property | How |
|---|---|
| **Dry run** | `plan` shares the entire transform path with `run` and simply never calls the sink. What it validates is what will execute. |
| **Resumable** | Progress is checkpointed per entity by source key after each batch commits. `eagm run --resume <run-id>` picks up where it stopped. Paging is keyset-based, so it stays fast millions of rows in and cannot skip rows. |
| **Idempotent** | Every migrated source id is recorded. Re-running skips them. A crash mid-batch re-runs that batch harmlessly. |
| **Reversible** | Every write is journalled to SQLite **before** the target transaction commits, so the journal is always a superset of what was written — undoing a row that was never written is a no-op, which is the safe direction to be wrong in. |

Run state lives in `state/migration.sqlite`, deliberately outside v3: a rollback
can never be defeated by the thing it is rolling back, and nothing pollutes the
target schema.

Resuming a run after editing the mapping is refused — the mapping is
fingerprinted, and a half-and-half migration is worse than starting over.

---

## The mapping file

`config/mapping.example.yaml` is a fully worked example. The shape:

```yaml
entities:
  - name: customers
    source:
      table: tbl_customer
      key: cust_id            # unique + sortable: used for paging and resume
      where: "deleted = 0"    # optional raw SQL filter
    target:
      table: customers
      key: id
      conflict: skip          # skip | update | error
    depends_on: []
    fields:
      - to: email
        from: email_address
        transform: [trim, email]
        required: true

      - to: full_name
        from: [first_name, last_name]     # many columns into one
        transform:
          - concat: {sep: " "}

      - to: status
        from: acct_status
        transform:
          - map:
              values: {A: active, I: inactive, "*": inactive}

  - name: vehicles
    depends_on: [customers]               # forces migration order
    fields:
      - to: customer_id
        from: cust_id
        transform:
          # resolve the v2 id to the id v3 gave that customer
          - lookup: {entity: customers, required: true}
        required: true
```

`eagm transforms` lists everything available. Beyond the usual string, number,
date and JSON handling there are some the domain needs: `vin`, `nags`, `phone`,
`postal_code`, `email`, `strip_html`, and `php_unserialize` for WordPress and
WooCommerce meta.

Adding your own is a few lines in `src/eag_migrator/transforms.py`:

```python
@transform("my_rule")
def _my_rule(value, *, row, ctx, **params):
    return ...
```

---

## Writing through v3's API instead of its database

Set `V3_API_BASE_URL` (and `V3_API_TOKEN`) in `.env` and give each entity a
`target.endpoint`. Every record then goes through v3's own validation and
business logic — slower, but v3 cannot end up in a state it would itself
reject. Nothing else in the mapping changes; the choice lives entirely behind
the sink interface, so it stays reversible.

---

## Commands

| Command | Purpose |
|---|---|
| `eagm doctor` | Check both connections and show where state and config live |
| `eagm discover --side both` | Introspect and profile the databases |
| `eagm scaffold` | Draft a mapping from the profiles |
| `eagm show` | Validate the mapping and print the migration order |
| `eagm plan` | Dry run |
| `eagm run` | Migrate |
| `eagm verify` | Post-migration verification |
| `eagm rollback <run-id>` | Undo a run |
| `eagm runs` | List previous runs |
| `eagm errors <run-id>` | What went wrong, grouped by cause |
| `eagm transforms` | List available transforms |

Run any of them with `make cli CMD="..."`, or `make shell` for a prompt inside
the container.

---

## Tests

```bash
make test          # in the container
make test-local    # on the host
```

42 tests covering the transforms and the full pipeline — discover, scaffold,
plan, run, verify, rollback — against fixture databases shaped like an auto
glass shop (customers → vehicles → work orders, with the messy data you would
expect: zero-dates, `$1,250.00`, `Y`/`N` flags, lowercase VINs, HTML in notes,
a soft-deleted row and a row with no email).

They run on SQLite so no containers are needed, but the engine itself is
dialect-agnostic — all database access goes through SQLAlchemy.

---

## Layout

```
config/mapping.yaml        the mapping — the only thing that knows about EAG
profiles/                  discovered schemas (gitignored: contains real data)
reports/                   plan/run/verify output (gitignored)
state/migration.sqlite     checkpoints, id map, rollback journal (gitignored)
db/v2-seed/, db/v3-seed/   drop .sql dumps here (gitignored)
src/eag_migrator/
  discovery.py             schema introspection and fingerprinting
  scaffold.py              draft-mapping generator
  mapping.py               the mapping schema
  transforms.py            named value transforms
  runner.py                the engine: plan, apply, rollback
  verify.py                post-migration verification
  state.py                 checkpoints, id map, journal
  adapters/                SQL source, SQL sink, HTTP API sink
```
