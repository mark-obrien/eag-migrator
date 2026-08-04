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
