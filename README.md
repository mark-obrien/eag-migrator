# EAG v2 → v3 migrator

A dockerized migrator for moving Everything Auto Glass from the v2 site to v3.

**It is built discovery-first**, because the schema is not known up front. You
point it at v2 — a database if you have credentials, otherwise the live website
— and it works out what is there: the tables, the keys, the foreign-key graph,
the framework that built it, and whether the site already publishes JSON. Then
it drafts the mapping for you. Nothing about EAG is hard-coded; the engine is
driven entirely by `config/mapping.yaml`.

---

## Two ways in

**If you can reach v2's database or API**, go to [Quick start](#quick-start).

**If you cannot** — no credentials, no DB access, only a login to the app
itself — read v2 over HTTP instead. See
[No database, no API](#no-database-no-api). It signs in, finds the app's own
internal API, and harvests it into a local staging database; from there the
pipeline below is identical.

## What you need to supply

The tool discovers the rest, but it cannot invent these:

1. **A way to read v2** — a read-only database user, a `.sql` dump in
   `db/v2-seed/`, or (failing both) the URL of the live site.
2. **A connection to v3** — or its dump in `db/v3-seed/`, or a base URL if you
   would rather write through v3's HTTP API.
3. **Answers to the questions the draft mapping asks.** Every guess it makes is
   marked `note: TODO`. Those are for whoever knows the business rules.

Credentials go in `.env`, which is gitignored. Do not commit dumps or harvested
content — `db/*-seed/` and `state/` are gitignored for the same reason.

> **Before extracting from a system you do not own**, get written
> authorisation from whoever is accountable for the account, and check the
> vendor's terms of service. This matters more here than for a marketing site:
> the records are customer PII and payment history. If EAG owns the account and
> is leaving a vendor who will not provide an export, that is a normal
> migration — but it is EAG's authorisation to give.
>
> The tool is built to behave: it rate-limits, identifies itself, caches every
> response so re-runs generate no new traffic, and refuses to store cardholder
> data. None of that is a substitute for permission.

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

## No database, no API

When the only thing you can reach is the site itself, a few commands stand in
front of the normal pipeline. **Which ones depends on whether v2 is a public
site or an application behind a login.**

### If v2 is an application (scheduling, quoting, payments)

This is the EAG case. The data is behind a login, and the way in is the app's
own internal API — the endpoints it calls to render its own list screens.
Those are already paginated, already typed, and already scoped to the account.

```bash
# 1. Sign in with your own browser, copy the Cookie header from devtools:
eagm login https://app.example.com --cookies 'sid=…; csrf=…'

# 2. Drive the screens you care about and record what they call:
eagm capture https://app.example.com \
     --path /customers --path /quotes --path /schedule

# 3. Review config/harvest.draft.yaml — capture wrote it from the endpoints
#    it saw, including their pagination style — then:
mv config/harvest.draft.yaml config/harvest.yaml
eagm harvest

# 4. From here it is an ordinary v2 database:
export V2_DATABASE_URL=sqlite:////app/state/staging.sqlite
eagm discover --side v2 && eagm scaffold && eagm plan && eagm run && eagm verify
```

`eagm capture` also picks up the CSRF or bearer header the app's JavaScript
attaches and saves it to the session, because cookies alone usually are not
enough to replay those endpoints — without it every call 401s.

`config/harvest.app.example.yaml` is a worked example covering all four
pagination styles (page, offset, cursor, POST body).

#### Authentication

Three ways, in order of preference:

| | How | When |
|---|---|---|
| **Cookie import** | `eagm login <url> --cookies 'sid=…'` | Default. Sign in yourself; no password is ever handled here. Also accepts a path to a cookie-manager JSON export. |
| **Bearer token** | `EAGM_AUTH_TOKEN=…` | The app issues API tokens. |
| **Form login** | `eagm login <url> --form` with `EAGM_USERNAME`/`EAGM_PASSWORD` | Unattended runs only. Drives the real login page, so it survives CSRF tokens and JS-built forms — but not MFA. |

The session is written to `state/session.json` mode 0600 and gitignored. It is
live credentials to a system holding customer data: **delete it when the
migration is done.** Nothing prints cookie or token values, including the
reports.

If a session expires mid-harvest the run stops with a clear error rather than
quietly storing a wall of login pages.

#### robots.txt and authenticated apps

Applications routinely `Disallow: /` so search engines do not index a
logged-in area. That rule addresses crawlers of public content, not an
authenticated user exporting their own records — but it is still the
operator's stated preference, so nothing here decides for you. Set
`respect_robots: false` in the harvest config deliberately, and the run
records that you did.

### If v2 is a public site

```bash
eagm recon https://the-v2-site.example    # what is it? does it already expose JSON?
eagm capture https://the-v2-site.example  # what does it call at runtime?
mv config/harvest.draft.yaml config/harvest.yaml
eagm harvest

export V2_DATABASE_URL=sqlite:////app/state/staging.sqlite
eagm discover --side v2 && eagm scaffold && eagm plan && eagm run && eagm verify
```

`recon` tells you which case you are in: if the front door is a login screen it
says so and stops, rather than profiling the login page and reporting "no JSON
API found" as though that meant something.

### Scraping HTML is the last resort

`recon` checks, in order of how much you would rather have it:

1. **A JSON API.** WordPress publishes its entire content model at
   `/wp-json/wp/v2` with no authentication — `recon` enumerates every post
   type, reports the row count from `X-WP-Total`, and lists the fields. Shopify
   publishes `/products.json`. Finding either turns a fragile scrape into a
   clean, paginated, typed export.
2. **JSON-LD.** Structured `schema.org` data already in the markup — business
   name, phone, address, opening hours, services, reviews. It does not move
   when the theme changes, so prefer it over CSS selectors.
3. **Sitemaps**, for a complete URL inventory grouped into path patterns.
4. **HTML selectors**, only if none of the above exist.

It also fingerprints the platform (WordPress, Shopify, Wix, Squarespace,
Webflow, Duda, Drupal, Magento, Next.js, and others) so you know what you are
dealing with, and drafts `config/harvest.draft.yaml` from what it found.

### `eagm capture` — analysing the site's network calls

`recon` probes endpoints we guess at. `capture` drives a real Chromium, scrolls
each page to trigger lazy loading, and records **what the site calls on its
own** — the private JSON API behind a React front end, the store-locator feed,
the quote calculator. It reports each endpoint with its response shape, item
count and a sample, and writes a HAR you can open in devtools.

This is usually how you find the good data on a site that looks unscrapeable.

Chromium is not in the image by default (it adds several hundred MB):

```bash
make build-browser        # or: docker compose build --build-arg WITH_BROWSER=true
make capture URL=https://the-v2-site.example
```

If you already have Chrome or Chromium somewhere, point `EAGM_CHROMIUM_PATH` at
it and skip the rebuild.

### Being a good citizen

Every request goes through one client that obeys `robots.txt` (including
`Crawl-delay`), rate-limits, identifies itself honestly, and **caches every
response to disk**. Iterating on selectors costs the site nothing, because the
second run reads from `state/webcache/`. A URL disallowed by `robots.txt` is
never fetched, even if it appears in the sitemap.

### Cardholder data is blocked, not trusted to the config

v2 takes payments, so a careless selector on a payments screen could drag PANs
and CVVs into a SQLite file on a laptop and pull the whole project into PCI DSS
scope. That is prevented at the staging layer, where no mapping mistake can
reach:

- **CVV/CVC, PINs and track data** are dropped entirely. PCI DSS forbids
  retaining them after authorisation, in any form.
- **Card and bank account numbers** are reduced to their last four digits.
- **A card number in a field nobody thought to name carefully** — a PAN typed
  into a free-text note on a quote — is caught by a value-level sweep, using a
  Luhn check plus a card-issuer prefix so job numbers are not false-flagged.

What survives is what a v3 system should actually hold: the processor's token,
the brand, the last four. The harvest report lists exactly what was removed.

The same report lists the **personal data** each collection contains, for your
processing record or DPA. That detection is deliberately narrow — on a
scheduling record `state` means "scheduled", not a US state, and a report full
of false positives is one nobody reads.

This is a safety net, not a compliance programme. Migrating a payments system
is still a conversation with whoever is accountable for that data.

### The staging database

`harvest` writes one table per collection into `state/staging.sqlite`, with the
extracted fields as columns plus `_id` (pages and resumes), `_key`
(deduplicates re-harvests), `_url` (the record's canonical permalink — this is
your redirect map) and `_fetched_at`.

Because it is a plain SQLite file, it *is* a v2 database. Everything downstream
— discovery, scaffolding, transforms, dry run, resume, rollback, verification —
works on it with no special cases. Re-running `harvest` updates rows in place
rather than duplicating them.

`config/harvest.example.yaml` is a worked example of all four discovery modes
(`api`, `sitemap`, `crawl`, `static`).

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
| `eagm login <url>` | Store an authenticated session for the v2 app |
| `eagm recon <url>` | Inspect the live v2 site; detect a login wall |
| `eagm capture <url>` | Record the app's own API calls and draft a harvest config |
| `eagm harvest` | Pull the site into `state/staging.sqlite` |
| `eagm staging` | Show what is in the staging database |
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

105 tests, in four groups:

- **The database path** — transforms plus the full pipeline (discover,
  scaffold, plan, run, verify, rollback) against fixture databases shaped like
  an auto glass shop: customers → vehicles → work orders, with the messy data
  you would expect (zero-dates, `$1,250.00`, `Y`/`N` flags, lowercase VINs,
  HTML in notes, a soft-deleted row, a row with no email).
- **The public-site path** — recon, drafting, extraction and harvesting
  against a fake WordPress auto-glass site **served over real HTTP**, with a
  real `robots.txt`, a real sitemap and real `X-WP-Total` pagination. Includes
  a test that a `robots.txt`-disallowed URL in the sitemap is never fetched.
- **The application path** — a fake scheduling/quoting/payments app behind a
  login, also over real HTTP: a session cookie plus a CSRF header, an app shell
  that renders nothing until its XHRs land, and all four pagination styles.
  Covers the login wall (anonymous requests get no data), expired sessions
  failing loudly, and the cardholder-data guard — including asserting that no
  PAN appears anywhere in the staging file's bytes.
- **Capture** — drives real Chromium, signs in, finds the internal API, and
  checks the config it drafts actually harvests without hand-editing. Skipped
  automatically when no browser is available.

The database tests run on SQLite so no containers are needed, but the engine is
dialect-agnostic — all database access goes through SQLAlchemy.

---

## Layout

```
config/mapping.yaml        the mapping — the only thing that knows about EAG
config/harvest.yaml        what to pull off the live site (web path only)
profiles/                  discovered schemas + site recon (gitignored: real data)
reports/                   plan/run/verify/capture output (gitignored)
state/migration.sqlite     checkpoints, id map, rollback journal (gitignored)
state/staging.sqlite       harvested site content (gitignored)
state/webcache/            cached HTTP responses (gitignored)
state/session.json         v2 app credentials, mode 0600 (gitignored)
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
  web/
    fetcher.py             the HTTP client: auth, robots, rate limit, cache
    session.py             authenticated sessions
    login.py               cookie import and form login
    safety.py              cardholder-data guard, PII reporting
    recon.py               platform fingerprinting, login-wall and API discovery
    capture.py             browser-driven network capture
    draft.py               draft-harvest-config generator
    extract.py             CSS / JSON-path / JSON-LD extraction
    harvest.py             crawl and pull into staging
    staging.py             the staging database
```
