# EAG v2 → v3 migrator

A dockerized migrator for moving a shop from the Everything Auto Glass v2
platform to v3 — one tenant at a time
(`<shop>.everythingautoglass.com` → `<shop>.eagsoftware.com`).

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

## The dashboard

Everything below can be driven from a browser instead:

```bash
make dashboard             # http://127.0.0.1:19080
```

One page shows connection health, the v2 session, the harvest config, the
mapping and its unanswered TODOs, what is in staging, and every run. The
buttons run the same code the CLI does — there is no second implementation, so
the UI cannot drift from `eagm`.

### Watching it work

Long jobs stream their log line by line over server-sent events — each API
page as it is fetched, each URL as it is crawled, each entity as it migrates —
so you can see what it is doing rather than waiting for a total at the end:

```
13:54:25  started: Harvest v2
13:54:25  customers: 2  page 1: 2 record(s)
13:54:26  customers: 4  page 2: 2 record(s)
13:54:26  customers: 5/5
13:54:27  quotes: 2  page 1: 2 record(s)
```

The log **stays on screen after the job ends** — that is exactly when you want
to read it — and every run is kept under **Activity**, so you can go back to
what a harvest did last week. Logs are also written to `reports/jobs/` as they
happen, so they survive a crash or a restart and can be grepped.

Long jobs also have a **Stop** button. Stopping waits for the current batch to
finish, so progress stays checkpointed and the run resumes cleanly.

A few deliberate constraints:

- **Localhost only.** Compose publishes the port on `127.0.0.1`. This UI holds
  a live session for a customer system and can write to v3.
- **Port 19080, not 8080.** 8080 is the most contended port on a dev machine.
  Set `EAGM_DASHBOARD_PORT` if 19080 clashes too — compose and the CLI both
  read it. A clash is reported with a free port to use, rather than surfacing
  as a traceback; `eagm dashboard --auto-port` just picks one.
- **Set `EAGM_DASHBOARD_TOKEN`** if you expose it anywhere else; every page and
  action then requires it.
- **Writing to v3 and rolling back need typing a confirmation word.** A stray
  click or a re-POSTed form cannot start a migration.
- **Credentials never render.** Cookie and token values are not in the HTML or
  the JSON API, and database URLs are shown with the password stripped.
- **One job at a time.** Two concurrent migrations would interleave writes and
  checkpoints.

The CLI remains the full interface — the dashboard covers the common path.

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

The engine v2 runs on is not known up front, so both are defined and neither
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

Local ports, all bound to `127.0.0.1`: dashboard `19080`, v2-mysql `13306`,
v3-mysql `13307`, v2-postgres `15432`, v3-postgres `15433`, adminer `18080`.
Override the dashboard with `EAGM_DASHBOARD_PORT` and adminer with
`ADMINER_PORT`.

### On Windows

`make` is a Unix tool, and `VAR=value command` is POSIX shell syntax — neither
works in PowerShell. Use the bundled script instead, which runs the same
docker compose commands:

```powershell
.\eagm.ps1                 # list the tasks
.\eagm.ps1 build
.\eagm.ps1 dashboard
.\eagm.ps1 recon https://shop.everythingautoglass.com
.\eagm.ps1 cli plan --limit 100
```

If PowerShell refuses to run it, that is the execution policy rather than the
script: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

Settings go in `.env` on every platform, so no task needs an environment
prefix. If you would rather not use the script at all, every task is a plain
compose command:

| Task | Command |
|---|---|
| Build | `docker compose build` |
| Dashboard | `docker compose up -d dashboard` |
| Any `eagm` command | `docker compose run --rm --entrypoint eagm migrator <args>` |

The dashboard needs none of this — once it is up, everything is buttons.

---

## No database, no API

When the only thing you can reach is the site itself, a few commands stand in
front of the normal pipeline. **Which ones depends on whether v2 is a public
site or an application behind a login.**

### If v2 is an application (scheduling, quoting, payments)

This is the EAG case. The data is behind a login, and the way in is the app's
own internal API — the endpoints it calls to render its own list screens.
Those are already paginated, already typed, and already scoped to the account.

Everything runs in the container — `make` wraps `docker compose run`:

```bash
make build                # includes Chromium, which capture and sign-in need

# 1. Sign in. Either use the dashboard's sign-in form, or put the Cookie
#    header from your browser's devtools into .env as EAGM_COOKIE and:
make login URL=https://app.example.com

# 2. Drive the screens you care about and record what they call:
make cli CMD="capture https://app.example.com \
     --path /customers --path /quotes --path /schedule"

# 3. Review config/harvest.draft.yaml — capture wrote it from the endpoints
#    it saw, including their pagination style — then:
mv config/harvest.draft.yaml config/harvest.yaml
make harvest

# 4. From here it is an ordinary v2 database. Set this in .env:
#      V2_DATABASE_URL=sqlite:////app/state/staging.sqlite
make discover && make scaffold && make plan && make migrate && make verify
```

Everything configurable lives in **`.env`**, which docker compose reads by
itself. Nothing needs a `VAR=value command` prefix — that syntax only works in
a POSIX shell, and it would put credentials in your shell history anyway.

`config/`, `state/`, `profiles/` and `reports/` are bind-mounted, so drafts,
sessions and the staging database live on your machine, not inside the
container.

Docker packages the tool; it does not change what your network can reach. If
the app is only reachable from a particular network or VPN, run the container
there.

`eagm capture` also picks up the CSRF or bearer header the app's JavaScript
attaches and saves it to the session, because cookies alone usually are not
enough to replay those endpoints — without it every call 401s.

#### Letting it find the screens itself

If you do not know the app's routes, add `--explore` (a checkbox in the
dashboard). It follows the app's own navigation from wherever you start,
recording the API behind each screen. Bound it with `--max-pages` and
`--depth`.

```bash
make cli CMD="capture https://app.example.com --explore --max-pages 30"
```

**It will not follow anything that reads as a state change.** It is signed in
to a live scheduling, quoting and payments system, so following "Delete quote",
"Refund" or "Email to customer" would not be a crawl, it would be an incident —
and "Log out" would end the run. Links matching those patterns are skipped, as
are Rails/Turbo links carrying `data-method` or a confirmation prompt, file
downloads, and anything off-site. Every skipped link is listed in the report
with the reason, so nothing is dropped silently.

That is a denylist, so treat it as a strong default rather than a guarantee. If
the app words its destructive actions unusually, drive it with explicit
`--path` values instead.

`config/harvest.app.example.yaml` is a worked example covering all four
pagination styles (page, offset, cursor, POST body).

#### If there is no JSON at all

Older installs render on the server: the customers screen is a `<table>`, and
that table *is* the data. `capture` will find nothing, because there is nothing
to find — so it falls back to reading the markup, and you can also do that
directly without a browser:

```bash
make cli CMD="draft-html https://app.example.com/customers \
     --also /quotes --also /invoices"
```

For each screen it works out the repeated element, one field per column (named
from the table headers), the id the rows carry in `data-id`, and the link to
the next page — then writes them into `config/harvest.draft.yaml` as an
ordinary crawl collection. Review it, correct what it guessed, and harvest.

The part that matters in the config is `rows:`:

```yaml
extract:
  rows: "table.listing tbody tr"     # the repeated element — one record each
  fields:
    - to: id
      selector: "."                  # "." means the row itself, not a child
      attr: data-id
    - to: name
      selector: "td:nth-of-type(1)"
    - to: detail_url
      selector: "td.name a"
      attr: href
```

Without `rows:`, selectors are read against the whole page and a list of fifty
customers gives you **one** record. With it, each row is read relative to
itself. It applies to any repeated block — table rows, cards, list items.

`attr: own_text` reads an element's own text and ignores nested elements. Some
apps split a name only by nesting:

```html
<td>Dana<span class="lname">Reyes</span></td>
```

`attr: text` gives you `Dana Reyes`; `own_text` on the cell gives `Dana` and a
second field on `span.lname` gives `Reyes`.

#### Pagination with nothing to follow

`crawl:` works when there's a next link. When there isn't — an offset in the
path, a page number in the query, or ids that have to be walked one at a time —
use `sequence:`:

```yaml
discover:
  sequence:
    url: /customer/{n}       # or /order?currentPage={n}, or /job/{n}
    start: 0
    step: 25
    stop_after_misses: 2     # consecutive empty pages that mean "the end"
```

No `stop:` is needed. Walking past the end returns pages with no rows, and a
run of those ends it — so the config keeps working as the record count grows.
The miss *budget* rather than stopping at the first one matters when walking
ids: they're sparse wherever a record was ever deleted, and a single gap is a
hole, not the end. A 200 response holding no records counts as a miss, not
just a 404 — the awkward case is the one that ends a walk in practice.

#### When a missing record answers 200

Plenty of apps answer a URL for a record that doesn't exist with a blank
editable form rather than a 404. The status code then tells you nothing, and
walking ids would store thousands of empty shells and never find the end.
`require:` names the fields that only a real record has:

```yaml
extract:
  require: [id]
  fields:
    - to: id
      selector: "div.job"
      attr: data-job-id      # empty string on the blank form
```

A page whose required fields are empty is skipped and counts as a miss. A
`require:` naming a field that isn't extracted is a config error rather than a
silent drop of every record.

#### Child records on a detail page

A detail page often holds repeating sub-lists — line items, notes, payments.
Set `rows:` to the repeating element and the page becomes a child collection.
The catch is the parent's id, which is on the page and *not* in the row.
`source: page` reads the document rather than the row:

```yaml
rows: "div.part-numbers > div.part-number"
require: [job_id]
fields:
  - to: job_id
    selector: "div.job"
    attr: data-job-id
    source: page           # out of the row, into the page
  - to: part_number
    selector: ".job-nags-part-number"
```

Several collections over the same URLs cost one crawl, not several — the HTTP
cache means the second and third read from disk.

The crawler that walks the pagination has the same refusals as `--explore`: no
"Delete", no "Refund", no "Log out", nothing carrying `data-method` or a
confirmation prompt. Each refusal is reported on the collection.

#### Letting a model read the markup

The drafter above is heuristics. It handles tables and obvious card lists, and
does badly on nested `<div>` soup with no useful classes. `--assist` (a
checkbox in the dashboard) adds a model to **that step only**:

```bash
make cli CMD="draft-html https://app.example.com/customers --assist"
```

Three properties make this safe to point at a payments system:

**No page content leaves the machine.** The request is built from a skeleton,
not the page. Every text node becomes a type placeholder and every
data-carrying attribute is masked *before* the request exists, so the model
sees

```html
<tr class="customer" data-id="NUM(4)">
  <td class="email">EMAIL</td>
  <td class="total">MONEY</td>
```

and never a name, an address, a VIN or a card number. Shape is the entire
question, and the values were only ever noise. Repeated rows are collapsed to
the first three, `<script>` blocks and prose-carrying attributes like `title=`
are dropped whole, and digits in `href`s are masked so `/customers/5001`
arrives as `/customers/0000`.

**One call per screen, never per record.** Bulk extraction stays CSS
selectors. Resume, verify and rollback all assume the same page gives the same
rows every run, and a model reading 5,000 pages does not — you would find out
during `verify`, unable to tell whether v3 was wrong or the scrape was.

**Its answer is run, not trusted.** The proposed selectors are executed
against the page and scored by how many fields actually produce a value on
most rows. The heuristics are scored the same way, and the better one wins —
a draw goes to the heuristics, because they cost nothing and never drift.
Selectors that match nothing, invalid CSS, an API failure and a missing key
all fall back rather than stopping, and the draft says which was used and why.

Needs `ANTHROPIC_API_KEY` in `.env`. Without it nothing calls out, and
`--assist` is the only thing that ever would.

#### Authentication

Three ways, in order of preference:

| | How | When |
|---|---|---|
| **Supply the login** | Type the username and password into the dashboard, or `eagm login <url> --form` with `EAGM_USERNAME`/`EAGM_PASSWORD` | Simplest. Drives the app's real login page in a headless browser, so it survives CSRF tokens, hashed field names and JS-built forms. Cannot get past MFA or a captcha. |
| **Cookie import** | `eagm login <url> --cookies 'sid=…'`, or `EAGM_COOKIE` | No password is ever handled. Use this when the app has MFA, or when you would rather not hand credentials to a tool. Also accepts a cookie-manager JSON export. |
| **Bearer token** | `EAGM_AUTH_TOKEN=…` | The app issues API tokens. |

With the credential route, the password fills the form and is then dropped —
only the resulting cookies and auth headers are stored. It is not written to
the session file, the job log or any report, and the username is masked in the
log. Give it a `success_selector` (a CSS selector only present once signed in)
so a failed login is detected properly rather than silently saving a session
for the login page.

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

# set V2_DATABASE_URL=sqlite:////app/state/staging.sqlite in .env, then:
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

Chromium ships in the image by default, because reading v2 over HTTP is the
primary path here and both capture and "sign in for me" need a real browser.
`eagm doctor` and the dashboard both report whether it is present, so a missing
browser shows up before you start a job rather than part-way through one.

If you only ever migrate database-to-database, `make build-slim` leaves it out
and saves a few hundred MB. To add it back later:

```bash
make build-browser        # rebuilds and restarts the dashboard
```

If you already have Chrome or Chromium somewhere, point `EAGM_CHROMIUM_PATH` at
it instead.

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

### 1. `eagm discover` — find out what v2 actually is

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

## Relationships the schema never declared

An application's value is in its links — quotes belong to customers,
appointments to quotes, payments to quotes. But plenty of v2 systems never
declare a foreign key: MyISAM cannot, many older applications simply did not,
and data harvested from an API has no constraints by definition.

So discovery deduces them, and then **proves them against the data**. Naming
alone would be a guess, so each candidate is checked by sampling distinct child
values and counting how many exist in the candidate parent column; it is only
kept if nearly all of them do. `quotes.customer_id` whose values match nothing
in `customers` is rejected, and `job_number` is not turned into a relationship
just because it ends in `_number`.

The scaffolder turns each one into a `lookup` transform, orders entities so
parents migrate first, and marks every inferred link as needing confirmation —
including when the sample matched 100%, because a perfect match on a small
table still is not a declaration.

One subtlety this handles: on a harvested table, `_id` is a row number the
harvester assigned while the application's own `id` is what sibling rows
reference. `source.key` pages and resumes on `_id`; `id_map_from` records the
real identity, so lookups resolve against the value children actually carry.

## Multi-tenant platforms

EAG v2 is one app per shop (`<shop>.everythingautoglass.com`), so both config
files expand `${VAR}` and `${VAR:-default}` from the environment:

```yaml
site:
  base_url: https://${TENANT}.everythingautoglass.com
```

```bash
TENANT=zephyrglass eagm harvest
```

One config serves every shop, and no customer's hostname ends up in a
committed file. An unset variable is an error, not an empty string — a silently
malformed URL is worse than a stopped run.

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
| `eagm dashboard` | Serve the web dashboard (localhost:19080) |
| `eagm doctor` | Check both connections and show where state and config live |
| `eagm login <url>` | Store an authenticated session for the v2 app |
| `eagm recon <url>` | Inspect the live v2 site; detect a login wall |
| `eagm capture <url>` | Record the app's own API calls and draft a harvest config |
| `eagm draft-html <url>` | Read a server-rendered list screen and draft its selectors (`--assist` to add a model) |
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
| `eagm export-profile <side>` | Re-render a stored schema profile as markdown |
| `eagm profile-summary <side>` | Headline facts from a stored profile |
| `eagm version` | Print the version |

Run any of them with `make cli CMD="..."`, or `make shell` for a prompt inside
the container.

---

## Tests

```bash
make test          # in the container
make test-local    # on the host
```

200 tests, in six groups:

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
  PAN appears anywhere in the staging file's bytes. Also covers the
  server-rendered half: a paginated table of customers that has to yield one
  record per row rather than one per page, a draft read straight off that
  markup that harvests without being hand-edited, and a crawl that walks the
  pagination while touching none of the per-row Delete links.
- **Capture** — drives real Chromium, signs in, finds the internal API, and
  checks the config it drafts actually harvests without hand-editing. Includes
  an explore pass over a fixture app whose navigation contains Delete, Refund,
  Email and Log out links, asserting it finds every real screen and touches
  none of those. Skipped automatically when no browser is available.
- **Model-assisted drafting** — that no page content can reach the API: a
  fixture page carrying names, emails, phone numbers, VINs, an amount, an
  inline `<script>` holding a token and a bare PAN, asserted absent from the
  skeleton value by value. Plus the rest of the contract, with the call
  stubbed: selectors that match nothing, invalid CSS, a detail page, an API
  failure and a thinner answer all fall back to the heuristics; the model wins
  only on markup the heuristics genuinely cannot read; and nothing calls out
  at all without `--assist`.
- **The dashboard** — the live log (streaming, surviving completion, written
  to disk, and offsets that stay correct once the in-memory tail is trimmed),
  plus what would actually hurt: that a supplied password never reaches the
  session file, the job log or a page,
  that session values never reach the HTML or the JSON API, that database
  passwords are stripped, that a migration cannot start without its confirmation word, that
  a crafted URL cannot read outside `reports/`, that the token gate holds, and
  that a stopped run stays resumable.

The database tests run on SQLite so no containers are needed, but the engine is
dialect-agnostic — all database access goes through SQLAlchemy.

---

## Layout

```
config/mapping.yaml        the mapping — the only thing that knows about EAG
config/harvest.yaml        what to pull off the live site (web path only)
config/harvest.eag-v2.yaml a starting config for EAG v2, written from a survey
profiles/                  discovered schemas + site recon (gitignored: real data)
reports/                   plan/run/verify/capture output (gitignored)
state/migration.sqlite     checkpoints, id map, rollback journal (gitignored)
state/staging.sqlite       harvested site content (gitignored)
state/webcache/            cached HTTP responses (gitignored)
state/session.json         v2 app credentials, mode 0600 (gitignored)
reports/jobs/              per-run job logs, written live (gitignored)
db/v2-seed/, db/v3-seed/   drop .sql dumps here (gitignored)
src/eag_migrator/
  dashboard/               the web UI (FastAPI + server-rendered templates)
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
    listing.py             reads a list screen's markup into row selectors
    assist.py              optional: a model reads the structure (values stripped)
    extract.py             CSS / JSON-path / JSON-LD extraction
    harvest.py             crawl and pull into staging
    staging.py             the staging database
```
