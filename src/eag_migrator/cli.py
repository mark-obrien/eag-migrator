"""Command line interface.

Typical order of operations:

    eagm doctor                 # can we reach both databases?
    eagm discover --side both   # what is actually in them?
    eagm scaffold               # draft a mapping from the two schemas
    #   ... edit config/mapping.yaml, answer every TODO ...
    eagm plan                   # dry run: transform everything, write nothing
    eagm run                    # migrate for real
    eagm verify                 # prove it landed correctly
    eagm rollback <run-id>      # undo it if it did not
"""

import json
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .adapters import ApiSink, SqlSink, SqlSource
from .config import CONFIG_DIR, PROFILES_DIR, REPORTS_DIR, STATE_DIR, Settings, ensure_dirs, load_settings
from .db import build_engine, probe
from .discovery import load_profile, profile_database, render_markdown, save_profile
from .mapping import Mapping, dump_mapping, load_mapping
from .report import (
    print_report,
    print_verify,
    render_report_markdown,
    render_verify_markdown,
    save_json,
    save_text,
)
from .runner import Runner
from .scaffold import build_draft
from .state import RunState
from .transforms import REGISTRY
from .verify import verify as run_verify

app = typer.Typer(
    add_completion=False,
    help="EAG v2 -> v3 migrator. Discovery first: introspect, draft, dry run, migrate, verify.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_MAPPING = CONFIG_DIR / "mapping.yaml"
STATE_DB = STATE_DIR / "migration.sqlite"


def _settings() -> Settings:
    ensure_dirs()
    return load_settings()


def _source(settings: Settings) -> SqlSource:
    return SqlSource(build_engine(settings.url_for("v2")), settings.v2_schema)


def _sink(settings: Settings):
    if settings.v3_api_base_url:
        console.print(f"[dim]sink: v3 HTTP API at {settings.v3_api_base_url}[/dim]")
        return ApiSink(
            settings.v3_api_base_url, settings.v3_api_token, settings.v3_api_timeout
        )
    console.print("[dim]sink: v3 database (SQL)[/dim]")
    return SqlSink(build_engine(settings.url_for("v3")), settings.v3_schema)


def _runner(settings: Settings, mapping: Mapping, state: RunState) -> Runner:
    def progress(entity: str, done: int, total: int) -> None:
        pct = f"{done / total:.0%}" if total else "?"
        console.print(f"[dim]  {entity}: {done:,}/{total:,} ({pct})[/dim]")

    return Runner(settings, mapping, _source(settings), _sink(settings), state, progress=progress)


@app.command()
def version() -> None:
    """Print the migrator version."""
    console.print(f"eag-migrator {__version__}")


@app.command()
def doctor() -> None:
    """Check that both databases are reachable before doing anything else."""
    settings = _settings()
    ok = True
    for side in ("v2", "v3"):
        try:
            url = settings.url_for(side)
        except ValueError as exc:
            console.print(f"[red]✗[/red] {side}: {exc}")
            ok = False
            continue
        good, message = probe(build_engine(url))
        console.print(f"{'[green]✓[/green]' if good else '[red]✗[/red]'} {side}: {message}")
        ok = ok and good

    if settings.v3_api_base_url:
        console.print(f"[dim]v3 API configured: {settings.v3_api_base_url}[/dim]")

    console.print(f"[dim]state db: {STATE_DB}[/dim]")
    console.print(f"[dim]mapping:  {DEFAULT_MAPPING} "
                  f"({'present' if DEFAULT_MAPPING.exists() else 'not created yet'})[/dim]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def discover(
    side: str = typer.Option("both", help="v2, v3 or both"),
    samples: int = typer.Option(5, help="Sample rows per table (0 to disable)"),
    counts: bool = typer.Option(True, help="Count rows per table (slow on huge tables)"),
    only: Optional[List[str]] = typer.Option(None, "--only", help="Limit to these tables"),
) -> None:
    """Introspect a live database and write a schema profile plus a readable report.

    This is what you run when you do not know what EAG is built on. It writes
    profiles/<side>.json (machine-readable) and profiles/<side>.md (send this
    to whoever owns the system).
    """
    settings = _settings()
    sides = ["v2", "v3"] if side == "both" else [side]
    redactor = settings.redactor()

    for s in sides:
        if s not in ("v2", "v3"):
            console.print(f"[red]unknown side '{s}' — use v2, v3 or both[/red]")
            raise typer.Exit(2)
        try:
            engine = build_engine(settings.url_for(s))
        except ValueError as exc:
            console.print(f"[yellow]skipping {s}: {exc}[/yellow]")
            continue

        console.print(f"[bold]Discovering {s}...[/bold]")
        try:
            profile = profile_database(
                engine,
                s,
                schema=settings.schema_for(s),
                sample_rows=samples,
                with_counts=counts,
                redactor=redactor,
                only=list(only) if only else None,
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]discovery failed for {s}: {exc}[/red]")
            raise typer.Exit(1) from exc

        json_path = save_profile(profile, PROFILES_DIR / f"{s}.json")
        md_path = save_text(render_markdown(profile), PROFILES_DIR / f"{s}.md")

        console.print(
            f"  {len(profile.tables)} tables, "
            f"{sum(t.row_count or 0 for t in profile.tables):,} rows"
        )
        if profile.detected_frameworks:
            top = profile.detected_frameworks[0]
            console.print(
                f"  looks like [bold]{top['framework']}[/bold] "
                f"({top['confidence']:.0%} of its marker tables present)"
            )
        else:
            console.print("  no framework fingerprint matched — bespoke schema")
        if profile.table_prefix:
            console.print(f"  table prefix: [bold]{profile.table_prefix}[/bold]")
        console.print(f"  wrote {json_path} and {md_path}\n")


@app.command()
def scaffold(
    out: Path = typer.Option(CONFIG_DIR / "mapping.draft.yaml", help="Where to write the draft"),
    min_rows: int = typer.Option(1, help="Skip source tables with fewer rows than this"),
) -> None:
    """Draft a mapping YAML by matching the v2 and v3 profiles against each other.

    Everything it guesses is marked with a note. Treat the output as a
    questionnaire, not a finished config.
    """
    _settings()
    v2_path = PROFILES_DIR / "v2.json"
    if not v2_path.exists():
        console.print("[red]No v2 profile. Run `eagm discover --side v2` first.[/red]")
        raise typer.Exit(1)

    v2 = load_profile(v2_path)
    v3_path = PROFILES_DIR / "v3.json"
    v3 = load_profile(v3_path) if v3_path.exists() else None
    if v3 is None:
        console.print(
            "[yellow]No v3 profile — target tables and columns will be placeholders.[/yellow]"
        )

    mapping, warnings = build_draft(v2, v3, min_rows=min_rows)
    path = dump_mapping(mapping, out)

    enabled = len([e for e in mapping.entities if e.enabled])
    console.print(
        f"[green]Drafted {len(mapping.entities)} entities "
        f"({enabled} enabled) → {path}[/green]"
    )
    todo = sum(1 for e in mapping.entities for f in e.fields if f.note) + sum(
        1 for e in mapping.entities if e.note
    )
    console.print(f"  {todo} field(s)/entity(ies) carry a note to review")

    if warnings:
        console.print(f"\n[bold yellow]{len(warnings)} thing(s) need a human decision[/bold yellow]")
        for w in warnings[:40]:
            console.print(f"  • {w}")
        if len(warnings) > 40:
            console.print(f"  ... and {len(warnings) - 40} more")
        save_text("\n".join(warnings), REPORTS_DIR / "scaffold-warnings.txt")
        console.print(f"\n  full list: {REPORTS_DIR / 'scaffold-warnings.txt'}")

    console.print(
        f"\nReview it, then: [bold]mv {path} {DEFAULT_MAPPING}[/bold] and run [bold]eagm plan[/bold]"
    )


@app.command()
def plan(
    mapping_file: Path = typer.Option(DEFAULT_MAPPING, "--mapping", help="Mapping YAML"),
    entities: Optional[List[str]] = typer.Option(None, "--entity", help="Limit to these entities"),
    limit: int = typer.Option(0, help="Stop after N source rows per entity (0 = all)"),
) -> None:
    """Dry run. Reads and transforms everything, writes nothing, reports what would happen."""
    settings = _settings()
    mapping = load_mapping(mapping_file)

    with RunState(STATE_DB) as state:
        runner = _runner(settings, mapping, state)
        report = runner.plan(list(entities) if entities else None, limit=limit)

    print_report(report, console)
    save_json(report.to_dict(), REPORTS_DIR / f"plan-{report.run_id}.json")
    path = save_text(render_report_markdown(report), REPORTS_DIR / f"plan-{report.run_id}.md")
    console.print(f"\n[dim]report: {path}[/dim]")

    blocked = [e.name for e in report.entities if e.aborted]
    if blocked:
        console.print(f"[red]Blocked entities: {', '.join(blocked)}[/red]")
    raise typer.Exit(1 if (report.fatal or blocked or report.total_failed) else 0)


@app.command()
def run(
    mapping_file: Path = typer.Option(DEFAULT_MAPPING, "--mapping", help="Mapping YAML"),
    entities: Optional[List[str]] = typer.Option(None, "--entity", help="Limit to these entities"),
    limit: int = typer.Option(0, help="Stop after N source rows per entity (0 = all)"),
    resume: Optional[str] = typer.Option(None, help="Resume an interrupted run by id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Migrate for real. Writes to v3."""
    settings = _settings()
    mapping = load_mapping(mapping_file)

    if not yes and not resume:
        target = settings.v3_api_base_url or settings.url_for("v3")
        console.print(f"[bold yellow]This writes to v3:[/bold yellow] {target}")
        if not typer.confirm("Proceed?"):
            raise typer.Exit(1)

    with RunState(STATE_DB) as state:
        runner = _runner(settings, mapping, state)
        report = runner.apply(
            list(entities) if entities else None, resume_run_id=resume, limit=limit
        )

    print_report(report, console)
    save_json(report.to_dict(), REPORTS_DIR / f"run-{report.run_id}.json")
    path = save_text(render_report_markdown(report), REPORTS_DIR / f"run-{report.run_id}.md")
    console.print(f"\n[dim]report: {path}[/dim]")
    console.print(f"[dim]run id: {report.run_id}[/dim]")

    if report.fatal or report.total_failed:
        console.print(
            f"\nTo undo everything this run wrote: "
            f"[bold]eagm rollback {report.run_id}[/bold]"
        )
        raise typer.Exit(1)
    console.print(f"\nNext: [bold]eagm verify --run {report.run_id}[/bold]")


@app.command()
def verify(
    run_id: Optional[str] = typer.Option(None, "--run", help="Run id (default: latest apply)"),
    mapping_file: Path = typer.Option(DEFAULT_MAPPING, "--mapping", help="Mapping YAML"),
    sample: int = typer.Option(50, help="Rows per entity to re-derive and diff"),
    entities: Optional[List[str]] = typer.Option(None, "--entity"),
) -> None:
    """Compare v2 and v3 after a migration: counts, a row-level diff, and lookup health."""
    settings = _settings()
    mapping = load_mapping(mapping_file)

    with RunState(STATE_DB) as state:
        if not run_id:
            latest = state.latest_run("apply")
            if not latest:
                console.print("[red]No apply run found. Run `eagm run` first.[/red]")
                raise typer.Exit(1)
            run_id = latest["run_id"]
            console.print(f"[dim]verifying latest apply run: {run_id}[/dim]")

        runner = _runner(settings, mapping, state)
        report = run_verify(
            runner,
            mapping,
            state,
            run_id,
            sample_size=sample,
            entity_names=list(entities) if entities else None,
        )

    print_verify(report, console)
    save_json(report.to_dict(), REPORTS_DIR / f"verify-{run_id}.json")
    path = save_text(render_verify_markdown(report), REPORTS_DIR / f"verify-{run_id}.md")
    console.print(f"[dim]report: {path}[/dim]")
    raise typer.Exit(0 if report.ok else 1)


@app.command()
def rollback(
    run_id: str = typer.Argument(..., help="Run id to undo"),
    mapping_file: Path = typer.Option(DEFAULT_MAPPING, "--mapping", help="Mapping YAML"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Undo a run: delete the rows it inserted, restore the rows it updated."""
    settings = _settings()
    mapping = load_mapping(mapping_file)

    with RunState(STATE_DB) as state:
        run = state.get_run(run_id)
        if not run:
            console.print(f"[red]No such run: {run_id}[/red]")
            raise typer.Exit(1)
        journalled = state.journal_count(run_id)
        console.print(
            f"Run [bold]{run_id}[/bold] ({run['mode']}, {run['status']}) "
            f"journalled [bold]{journalled:,}[/bold] write(s)."
        )
        if journalled == 0:
            console.print("[yellow]Nothing to undo.[/yellow]")
            raise typer.Exit(0)
        if not yes and not typer.confirm("Undo all of them?"):
            raise typer.Exit(1)

        runner = _runner(settings, mapping, state)
        result = runner.rollback(run_id)

    console.print(f"[green]Rolled back {result['rows_undone']:,} row(s)[/green]")
    for detail in result["detail"]:
        console.print(f"  {detail['entity']} ({detail['table']}): {detail['rows_undone']:,}")


@app.command()
def runs(limit: int = typer.Option(20, help="How many to list")) -> None:
    """List previous runs and their checkpoints."""
    _settings()
    with RunState(STATE_DB) as state:
        rows = state.list_runs(limit)
        if not rows:
            console.print("No runs yet.")
            return
        # The run id must survive narrow terminals intact — it is what you
        # paste into `eagm rollback` and `eagm run --resume`. The id already
        # starts with the timestamp, so a separate Started column is redundant.
        table = Table(header_style="bold")
        table.add_column("Run id", no_wrap=True, overflow="fold")
        table.add_column("Mode")
        table.add_column("Status")
        table.add_column("Done", justify="right")
        table.add_column("Wrote", justify="right")
        table.add_column("Failed", justify="right")
        for row in rows:
            cps = state.checkpoints(row["run_id"])
            table.add_row(
                row["run_id"],
                row["mode"],
                row["status"],
                f"{sum(1 for c in cps if c.done)}/{len(cps)}",
                f"{sum(c.written for c in cps):,}",
                f"{sum(c.failed for c in cps):,}",
            )
        console.print(table)


@app.command()
def errors(
    run_id: str = typer.Argument(..., help="Run id"),
    full: bool = typer.Option(False, help="List every error instead of a summary"),
) -> None:
    """Show what went wrong in a run."""
    _settings()
    with RunState(STATE_DB) as state:
        if full:
            rows = state.errors(run_id)
            for row in rows:
                console.print(
                    f"[cyan]{row['entity']}[/cyan] id={row['source_id']} "
                    f"({row['stage']}): {row['message']}"
                )
            console.print(f"\n{len(rows):,} error(s)")
            return
        summary = state.error_summary(run_id)
        if not summary:
            console.print("[green]No errors recorded for this run.[/green]")
            return
        table = Table(header_style="bold")
        table.add_column("Entity")
        table.add_column("Stage")
        table.add_column("Count", justify="right")
        table.add_column("Message")
        for row in summary:
            table.add_row(row["entity"], row["stage"], f"{row['count']:,}", row["message"][:90])
        console.print(table)


@app.command()
def transforms() -> None:
    """List the transforms available to the mapping file."""
    table = Table(title="Transforms", header_style="bold")
    table.add_column("Name")
    table.add_column("Purpose")
    for name in sorted(REGISTRY):
        doc = (REGISTRY[name].__doc__ or "").strip().split("\n")[0]
        table.add_row(name, doc)
    console.print(table)


@app.command()
def show(
    mapping_file: Path = typer.Option(DEFAULT_MAPPING, "--mapping", help="Mapping YAML"),
) -> None:
    """Validate the mapping and print the order entities will migrate in."""
    _settings()
    try:
        mapping = load_mapping(mapping_file)
        order = mapping.topo_order()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Mapping is not valid: {exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Mapping is valid[/green] (fingerprint {mapping.fingerprint()})")
    table = Table(title="Migration order", header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Entity")
    table.add_column("Source → target")
    table.add_column("Fields", justify="right")
    table.add_column("Depends on")
    table.add_column("Notes", justify="right")
    for i, entity in enumerate(order, 1):
        notes = sum(1 for f in entity.fields if f.note) + (1 if entity.note else 0)
        table.add_row(
            str(i),
            entity.name,
            f"{entity.source.table} → {entity.target.table}",
            str(len(entity.fields)),
            ", ".join(entity.depends_on) or "—",
            f"[yellow]{notes}[/yellow]" if notes else "0",
        )
    console.print(table)

    disabled = [e.name for e in mapping.entities if not e.enabled]
    if disabled:
        console.print(f"\n[dim]disabled: {', '.join(disabled)}[/dim]")


@app.command(name="export-profile")
def export_profile(
    side: str = typer.Argument("v2", help="v2 or v3"),
    out: Optional[Path] = typer.Option(None, help="Destination .md file"),
) -> None:
    """Re-render a stored profile as markdown (to send to whoever owns EAG)."""
    _settings()
    path = PROFILES_DIR / f"{side}.json"
    if not path.exists():
        console.print(f"[red]No profile at {path}. Run `eagm discover --side {side}` first.[/red]")
        raise typer.Exit(1)
    profile = load_profile(path)
    dest = out or PROFILES_DIR / f"{side}.md"
    save_text(render_markdown(profile), dest)
    console.print(f"[green]Wrote {dest}[/green]")


# ---------------------------------------------------------------------------
# Web source: when there is no database and no API access to v2.
# ---------------------------------------------------------------------------

HARVEST_CONFIG = CONFIG_DIR / "harvest.yaml"
STAGING_DB = STATE_DIR / "staging.sqlite"
WEB_CACHE = STATE_DIR / "webcache"
SESSION_FILE = STATE_DIR / "session.json"


def _load_session(origin: str, required: bool = False):
    """Stored session, else EAGM_AUTH_TOKEN / EAGM_COOKIE, else nothing."""
    from .web.session import Session

    if SESSION_FILE.exists():
        session = Session.load(SESSION_FILE)
        if session.age_hours > 12:
            console.print(
                f"[yellow]Session is {session.age_hours:.0f}h old — if calls start "
                f"returning 401, re-run `eagm login`.[/yellow]"
            )
        return session

    session = Session.from_env(origin)
    if session:
        console.print("[dim]using credentials from the environment[/dim]")
        return session

    if required:
        console.print(
            "[red]No session.[/red] This app needs a login. Run:\n"
            "  [bold]eagm login <url> --cookies '<paste the Cookie header>'[/bold]"
        )
        raise typer.Exit(1)
    return None


@app.command()
def login(
    url: str = typer.Argument(..., help="The v2 app, e.g. https://app.example.com"),
    cookies: Optional[str] = typer.Option(
        None,
        "--cookies",
        help="A raw Cookie header, or a path to a cookie/storage-state JSON export",
    ),
    form: bool = typer.Option(
        False, "--form", help="Drive the login form (needs EAGM_USERNAME/EAGM_PASSWORD)"
    ),
    login_url: str = typer.Option("/login", help="--form only: the login page"),
    success_selector: Optional[str] = typer.Option(
        None, help="--form only: a selector that only exists once signed in"
    ),
    check: bool = typer.Option(True, help="Verify the session actually works"),
) -> None:
    """Store an authenticated session for the v2 app.

    Prefer --cookies: sign in with your own browser, copy the Cookie header
    from devtools, and no password is ever handled here. --form is for
    unattended runs.
    """
    _settings()
    from .web.fetcher import Fetcher
    from .web.login import LoginFailed, LoginSpec, form_login, import_session

    # A cookie on the command line is visible in `ps` and lands in shell
    # history, and quoting a Cookie header through make/compose is its own
    # small nightmare. The environment avoids both.
    if not cookies and not form:
        from .web.session import Session

        env_session = Session.from_env(url)
        if env_session:
            source = "EAGM_AUTH_TOKEN" if env_session.headers else "EAGM_COOKIE"
            console.print(f"[dim]using {source} from the environment[/dim]")
            path = env_session.save(SESSION_FILE)
            console.print(f"[green]Session saved[/green] → {path} (mode 0600)")
            console.print(f"  {env_session.describe()}")
            return

    if not cookies and not form:
        console.print(
            "Give it a session one of two ways:\n\n"
            "  [bold]1. Import from your browser (recommended)[/bold]\n"
            "     Sign in normally, open devtools → Network → any request →\n"
            "     copy the [bold]Cookie[/bold] request header, then either:\n"
            f"       export EAGM_COOKIE='sid=abc; csrf=xyz' && eagm login {url}\n"
            f"       eagm login {url} --cookies 'sid=abc; csrf=xyz'\n"
            "     (the environment form keeps it out of ps and shell history)\n\n"
            "     Or export cookies to JSON with a cookie-manager extension:\n"
            f"       eagm login {url} --cookies ./cookies.json\n\n"
            "  [bold]2. Let the migrator sign in[/bold]\n"
            "     export EAGM_USERNAME=... EAGM_PASSWORD=...\n"
            f"       eagm login {url} --form --success-selector '.dashboard'\n"
        )
        raise typer.Exit(1)

    try:
        if form:
            session = form_login(
                url,
                LoginSpec(login_url=login_url, success_selector=success_selector),
            )
        else:
            session = import_session(cookies, url)
    except LoginFailed as exc:
        console.print(f"[red]Login failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not session.cookies and not session.headers:
        console.print("[red]That produced no cookies or auth headers.[/red]")
        raise typer.Exit(1)

    if check:
        with Fetcher(url, cache_dir=None, use_cache=False, rate_limit_rps=0,
                     respect_robots=False, session=session) as fetcher:
            resp = fetcher.get(url)
        if resp.status in (401, 403):
            console.print(
                f"[red]Session rejected (HTTP {resp.status}).[/red] "
                f"The cookies may be incomplete — copy the whole Cookie header."
            )
            raise typer.Exit(1)
        if "password" in resp.text.lower() and resp.text.lower().count("password") > 1:
            console.print(
                "[yellow]Warning: the response still looks like a login page. "
                "The session may not be valid.[/yellow]"
            )

    path = session.save(SESSION_FILE)
    console.print(f"[green]Session saved[/green] → {path} (mode 0600)")
    console.print(f"  {session.describe()}")
    console.print(
        "\nNext: [bold]eagm capture <url> --path /customers --path /quotes --draft[/bold]\n"
        "[dim]This file holds live credentials to a system with customer data. "
        "It is gitignored; delete it when the migration is done.[/dim]"
    )


@app.command()
def recon(
    url: str = typer.Argument(..., help="The v2 site, e.g. https://example.com"),
    max_urls: int = typer.Option(3000, help="Cap on URLs read from sitemaps"),
    rate: float = typer.Option(1.0, help="Requests per second"),
    robots: bool = typer.Option(True, help="Obey robots.txt"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Ignore the on-disk cache"),
    draft: bool = typer.Option(True, help="Also write a starting harvest config"),
) -> None:
    """Work out what the v2 site is and whether it already exposes JSON.

    Scraping HTML is the last resort. This checks first for a REST API, a
    products feed, sitemaps and JSON-LD — all of which beat parsing markup.
    """
    _settings()
    from .web.fetcher import Fetcher
    from .web.recon import recon as run_recon, render_markdown as render_recon

    console.print(f"[bold]Recon:[/bold] {url}")
    session = _load_session(url)
    with Fetcher(
        url,
        cache_dir=WEB_CACHE,
        rate_limit_rps=rate,
        respect_robots=robots,
        use_cache=not no_cache,
        session=session,
    ) as fetcher:
        profile = run_recon(fetcher, max_urls=max_urls)

    save_json(profile.to_dict(), PROFILES_DIR / "site.json")
    md = save_text(render_recon(profile), PROFILES_DIR / "site.md")

    if profile.status and not 200 <= profile.status < 300:
        console.print(f"[red]Homepage returned HTTP {profile.status}[/red]")

    if profile.requires_auth:
        console.print("\n[bold yellow]This is an application behind a login.[/bold yellow]")
        for item in profile.auth_evidence:
            console.print(f"  • {item}")
        console.print(
            "\nAnonymous crawling will only ever collect the login page. Do this instead:\n"
            f"  [bold]eagm login {url} --cookies '<Cookie header from your browser>'[/bold]\n"
            f"  [bold]eagm capture {url} --path /customers --path /quotes --draft[/bold]\n"
        )

    if profile.platforms:
        console.print("  platform: " + ", ".join(p["platform"] for p in profile.platforms))
    if profile.generator:
        console.print(f"  generator: {profile.generator}")
    console.print(f"  URLs discovered: {len(profile.urls):,}")

    if profile.content_types:
        table = Table(title="Content available as JSON", header_style="bold")
        table.add_column("Type")
        table.add_column("Rows", justify="right")
        table.add_column("Accessible")
        table.add_column("Endpoint", overflow="fold")
        for ct in profile.content_types:
            table.add_row(
                ct.name,
                f"{ct.total:,}" if ct.total is not None else "?",
                "[green]yes[/green]" if ct.accessible else f"[yellow]{ct.note or 'no'}[/yellow]",
                ct.rest_url,
            )
        console.print(table)
    else:
        console.print("[yellow]  no JSON API found by probing[/yellow]")

    for note in profile.notes:
        console.print(f"  • {note}")
    console.print(f"\n[dim]report: {md}[/dim]")

    if draft and profile.requires_auth:
        console.print(
            "[dim]Skipping the harvest draft — there is nothing to draft from until "
            "you are signed in.[/dim]"
        )
    elif draft:
        from .web.draft import draft_config
        from .web.harvest import dump_config

        config, warnings = draft_config(profile)
        path = dump_config(config, CONFIG_DIR / "harvest.draft.yaml")
        console.print(
            f"[green]Drafted {len(config.collections)} collection(s) → {path}[/green]"
        )
        for warning in warnings:
            console.print(f"  [yellow]•[/yellow] {warning}")
        console.print(
            f"\nReview it, then: [bold]mv {path} {HARVEST_CONFIG}[/bold] "
            f"and run [bold]eagm harvest[/bold]"
        )

    if not profile.requires_auth:
        console.print(
            "\n[dim]If the site renders content client-side, run "
            "`eagm capture <url>` to see the API it calls.[/dim]"
        )


@app.command()
def capture(
    url: str = typer.Argument(..., help="The v2 site"),
    path: Optional[List[str]] = typer.Option(
        None, "--path", help="Extra pages to drive (repeatable). Defaults to the homepage."
    ),
    har: bool = typer.Option(True, help="Also write a HAR file"),
    scroll: bool = typer.Option(True, help="Scroll each page to trigger lazy loading"),
    wait: int = typer.Option(2500, help="Milliseconds to idle on each page"),
    draft: bool = typer.Option(
        True, help="Write a harvest config from the endpoints discovered"
    ),
    anonymous: bool = typer.Option(
        False, "--anonymous", help="Ignore any stored session"
    ),
) -> None:
    """Drive a real browser and record the network calls the site makes.

    This is how you find the private JSON API behind a client-rendered site —
    almost always a better migration source than the HTML it renders.
    """
    _settings()
    from .web.capture import BrowserUnavailable, capture as run_capture
    from .web.capture import render_markdown as render_capture

    paths = list(path) if path else ["/"]
    session = None if anonymous else _load_session(url)
    console.print(
        f"[bold]Capturing:[/bold] {url} ({len(paths)} page(s))"
        + (" [green]authenticated[/green]" if session else " [yellow]anonymous[/yellow]")
    )
    if not session and not anonymous:
        console.print(
            "[dim]No session stored. If this app needs a login, run `eagm login` "
            "first or you will only record the login screen.[/dim]"
        )

    try:
        report = run_capture(
            url,
            paths,
            har_path=(REPORTS_DIR / "capture.har") if har else None,
            scroll=scroll,
            wait_ms=wait,
            session=session,
        )
    except BrowserUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    save_json(report.to_dict(), REPORTS_DIR / "capture.json")
    md = save_text(render_capture(report), REPORTS_DIR / "capture.md")

    if report.api_calls:
        table = Table(title="JSON endpoints the site calls", header_style="bold")
        table.add_column("Method")
        table.add_column("Endpoint", overflow="fold")
        table.add_column("Items", justify="right")
        table.add_column("Bytes", justify="right")
        for call in report.api_calls:
            table.add_row(
                call.method,
                call.pattern,
                str(call.item_count) if call.item_count is not None else "—",
                f"{call.size:,}",
            )
        console.print(table)
    else:
        console.print("[yellow]No JSON/XHR calls recorded.[/yellow]")

    for note in report.notes:
        console.print(f"  • {note}")
    console.print(f"\n[dim]report: {md}[/dim]")
    if report.har_path:
        console.print(f"[dim]HAR:    {report.har_path}[/dim]")

    # An SPA usually sends a bearer or CSRF header its cookies do not carry.
    # Without adopting it, every replayed call 401s.
    if session is not None and report.auth_headers:
        added = session.adopt_headers(report.auth_headers)
        if added:
            session.save(SESSION_FILE)
            console.print(
                f"[green]Adopted {added} auth header(s) the app's JavaScript sent[/green] "
                f"— saved to the session so harvest can replay these endpoints."
            )

    if draft and report.api_calls:
        from .web.draft import draft_from_capture
        from .web.harvest import dump_config

        config, warnings = draft_from_capture(report, url)
        path_out = dump_config(config, CONFIG_DIR / "harvest.draft.yaml")
        console.print(
            f"\n[green]Drafted {len(config.collections)} collection(s) → {path_out}[/green]"
        )
        for warning in warnings:
            console.print(f"  [yellow]•[/yellow] {warning}")
        console.print(
            f"\nReview it, then: [bold]mv {path_out} {HARVEST_CONFIG}[/bold] "
            f"and run [bold]eagm harvest[/bold]"
        )


@app.command()
def harvest(
    config_file: Path = typer.Option(HARVEST_CONFIG, "--config", help="Harvest config"),
    collections: Optional[List[str]] = typer.Option(
        None, "--collection", help="Limit to these collections"
    ),
    limit: int = typer.Option(0, help="Stop after N records per collection (0 = all)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Re-fetch instead of using the cache"),
    anonymous: bool = typer.Option(False, "--anonymous", help="Ignore any stored session"),
) -> None:
    """Pull the v2 site into state/staging.sqlite.

    Afterwards point V2_DATABASE_URL at that file and the rest of the migrator
    works exactly as it does against a real database.
    """
    _settings()
    from .web.harvest import AuthExpired, harvest as run_harvest, load_config
    from .web.harvest import render_markdown as render_harvest
    from .web.staging import Staging

    config = load_config(config_file)
    session = (
        None
        if anonymous
        else _load_session(config.site.base_url, required=config.site.requires_auth)
    )
    console.print(
        f"[bold]Harvesting[/bold] {config.site.base_url} "
        f"at {config.site.rate_limit_rps}/s"
        + ("" if config.site.respect_robots else " [yellow](ignoring robots.txt)[/yellow]")
    )

    def say(name: str, done: int, total: int) -> None:
        console.print(f"[dim]  {name}: {done:,}/{total:,}[/dim]")

    try:
        with Staging(STAGING_DB) as staging:
            report = run_harvest(
                config,
                staging,
                cache_dir=WEB_CACHE,
                use_cache=not no_cache,
                collections=list(collections) if collections else None,
                limit=limit,
                progress=say,
                session=session,
            )
    except AuthExpired as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    table = Table(title="Harvest", header_style="bold")
    table.add_column("Collection")
    table.add_column("Found", justify="right")
    table.add_column("Fetched", justify="right")
    table.add_column("Stored", justify="right")
    table.add_column("Failed", justify="right")
    for c in report.collections:
        table.add_row(
            c.name,
            f"{c.discovered:,}",
            f"{c.fetched:,}",
            f"{c.stored:,}",
            f"[red]{c.failed:,}[/red]" if c.failed else "0",
        )
    console.print(table)

    for c in report.collections:
        for note in c.notes:
            console.print(f"  • [cyan]{c.name}[/cyan]: {note}")
        for err in c.errors[:3]:
            console.print(f"  [red]![/red] [cyan]{c.name}[/cyan] {err['url']}: {err['error']}")

    for c in report.collections:
        for item in c.scrubbed.summary():
            style = "red" if "dropped" in item else "yellow"
            if item.startswith("personal data"):
                style = "dim"
            console.print(f"  [{style}]•[/{style}] [cyan]{c.name}[/cyan]: {item}")
    for note in report.notes:
        console.print(f"  • {note}")

    console.print(
        f"\n{report.requests_made:,} request(s) made, "
        f"{report.cache_hits:,} served from cache"
        + (" [green](authenticated)[/green]" if report.authenticated else "")
    )
    md = save_text(render_harvest(report), REPORTS_DIR / "harvest.md")
    console.print(f"[dim]report: {md}[/dim]")

    console.print(
        f"\nThe staging database is now your v2 source:\n"
        f"  [bold]export V2_DATABASE_URL=sqlite:///{STAGING_DB}[/bold]\n"
        f"  [bold]eagm discover --side v2 && eagm scaffold[/bold]"
    )
    raise typer.Exit(1 if report.total_failed else 0)


@app.command()
def staging(
    sample: int = typer.Option(0, help="Show N sample rows per collection"),
) -> None:
    """Show what is currently in the staging database."""
    _settings()
    from .web.staging import Staging

    if not STAGING_DB.exists():
        console.print(f"[yellow]No staging database yet at {STAGING_DB}.[/yellow]")
        console.print("Run `eagm recon <url>` then `eagm harvest`.")
        raise typer.Exit(1)

    with Staging(STAGING_DB) as store:
        tables = store.tables()
        if not tables:
            console.print("[yellow]Staging database is empty.[/yellow]")
            raise typer.Exit(1)

        table = Table(title=f"Staging — {STAGING_DB}", header_style="bold")
        table.add_column("Collection")
        table.add_column("Rows", justify="right")
        for name in tables:
            table.add_row(name, f"{store.count(name):,}")
        console.print(table)

        if sample:
            for name in tables:
                console.print(f"\n[bold cyan]{name}[/bold cyan]")
                console.print(json.dumps(store.sample(name, sample), indent=2, default=str)[:4000])

    console.print(f"\n[dim]V2_DATABASE_URL=sqlite:///{STAGING_DB}[/dim]")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(0, help="Port (default: $EAGM_DASHBOARD_PORT, else 19080)"),
    auto_port: bool = typer.Option(
        False, "--auto-port", help="If the port is busy, quietly use a free one"
    ),
    reload: bool = typer.Option(False, help="Auto-reload on code changes"),
) -> None:
    """Serve the web dashboard: drive and watch the whole migration in a browser.

    Binds to localhost by default. This UI holds a session for a live customer
    system and can write to v3 — set EAGM_DASHBOARD_TOKEN before exposing it
    anywhere else.
    """
    import os

    _settings()
    try:
        import uvicorn
    except ImportError as exc:
        console.print(
            "[red]The dashboard needs extra packages.[/red] Rebuild the image, or "
            "`pip install fastapi uvicorn jinja2 python-multipart markdown`."
        )
        raise typer.Exit(1) from exc

    from .dashboard.net import configured_port, find_free, is_free

    port = port or configured_port()
    if not is_free(port, host):
        alternative = find_free(host, near=port)
        if not auto_port:
            console.print(
                f"[red]Port {port} is already in use.[/red] Something else is "
                f"listening there.\n"
                f"  Try:  [bold]eagm dashboard --port {alternative}[/bold]\n"
                f"  Or:   [bold]eagm dashboard --auto-port[/bold]\n"
                f"  Or set [bold]EAGM_DASHBOARD_PORT={alternative}[/bold] in .env "
                f"(docker compose reads it too)."
            )
            raise typer.Exit(1)
        console.print(f"[yellow]Port {port} was busy; using {alternative}.[/yellow]")
        port = alternative

    if host not in ("127.0.0.1", "localhost") and not os.getenv("EAGM_DASHBOARD_TOKEN"):
        console.print(
            f"[yellow]Binding to {host} with no token set.[/yellow] Anyone who can "
            f"reach this port can migrate or roll back. Under docker compose that "
            f"is fine — the port is published to 127.0.0.1 only. Anywhere else, "
            f"set EAGM_DASHBOARD_TOKEN."
        )
    console.print(f"[bold]Dashboard:[/bold] http://{host}:{port}")
    if os.getenv("EAGM_DASHBOARD_TOKEN"):
        console.print("[dim]token required — append ?token=… to the URL[/dim]")

    uvicorn.run(
        "eag_migrator.dashboard.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


@app.command(name="profile-summary")
def profile_summary(side: str = typer.Argument("v2", help="v2 or v3")) -> None:
    """Print the headline facts from a stored profile."""
    _settings()
    path = PROFILES_DIR / f"{side}.json"
    if not path.exists():
        console.print(f"[red]No profile at {path}.[/red]")
        raise typer.Exit(1)
    profile = load_profile(path)
    console.print(json.dumps(
        {
            "dialect": profile.dialect,
            "server_version": profile.server_version,
            "tables": len(profile.tables),
            "rows": sum(t.row_count or 0 for t in profile.tables),
            "table_prefix": profile.table_prefix,
            "frameworks": profile.detected_frameworks[:3],
            "largest": [
                {"table": t.name, "rows": t.row_count}
                for t in sorted(profile.tables, key=lambda t: -(t.row_count or 0))[:10]
            ],
        },
        indent=2,
    ))


if __name__ == "__main__":
    app()
