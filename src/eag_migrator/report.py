"""Rendering: console tables for the operator, markdown files for the record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .runner import Report
from .verify import VerifyReport


def save_json(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def save_text(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- migration report -------------------------------------------------------


def print_report(report: Report, console: Console) -> None:
    title = "Dry run — nothing was written" if report.mode == "plan" else "Migration run"
    table = Table(title=f"{title}  ({report.run_id})", header_style="bold")
    table.add_column("Entity")
    table.add_column("Source → target")
    table.add_column("Rows", justify="right")
    table.add_column("OK", justify="right")
    table.add_column("Skipped", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Status")

    for e in report.entities:
        ok = e.processed - e.failed if report.mode == "plan" else e.inserted + e.updated
        status = "[red]aborted[/red]" if e.aborted else ("[green]ok[/green]" if e.ok else "[yellow]errors[/yellow]")
        table.add_row(
            e.name,
            f"{e.source_table} → {e.target_table}",
            f"{e.total_source_rows:,}",
            f"{ok:,}",
            f"{e.skipped + e.already_migrated:,}",
            f"[red]{e.failed:,}[/red]" if e.failed else "0",
            status,
        )
    console.print(table)

    notes = [(e.name, n) for e in report.entities for n in e.notes]
    if notes:
        console.print("\n[bold]Notes and open questions[/bold]")
        for name, note in notes:
            marker = "[yellow]TODO[/yellow]" if note.startswith("TODO") else "•"
            console.print(f"  {marker} [cyan]{name}[/cyan]: {note}")

    errored = [e for e in report.entities if e.errors]
    if errored:
        console.print("\n[bold red]Sample failures[/bold red]")
        for e in errored:
            console.print(f"  [cyan]{e.name}[/cyan] ({e.failed:,} total)")
            for err in e.errors[:5]:
                console.print(
                    f"    id={err.get('source_id')} {err.get('field')}: {err.get('error')}"
                )

    if report.fatal:
        console.print(f"\n[bold red]Run stopped:[/bold red] {report.fatal}")


def render_report_markdown(report: Report) -> str:
    lines = [
        f"# EAG migration {report.mode} — `{report.run_id}`\n",
        f"- Mapping fingerprint: `{report.mapping_hash}`",
        f"- Started: {report.started_at}",
        f"- Finished: {report.finished_at or '(incomplete)'}",
        f"- Rows written: **{report.total_written:,}**",
        f"- Rows failed: **{report.total_failed:,}**\n",
    ]
    if report.mode == "plan":
        lines.append("> Dry run. Nothing was written to v3.\n")
    if report.fatal:
        lines.append(f"> **Run stopped:** {report.fatal}\n")

    lines.append("## Per entity\n")
    lines.append("| Entity | Source | Target | Source rows | Inserted | Updated | Skipped | Failed |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    for e in report.entities:
        lines.append(
            f"| {e.name} | `{e.source_table}` | `{e.target_table}` | {e.total_source_rows:,} | "
            f"{e.inserted:,} | {e.updated:,} | {e.skipped + e.already_migrated:,} | {e.failed:,} |"
        )
    lines.append("")

    todos = [(e.name, n) for e in report.entities for n in e.notes]
    if todos:
        lines.append("## Notes and open questions\n")
        for name, note in todos:
            lines.append(f"- **{name}** — {note}")
        lines.append("")

    for e in report.entities:
        if not e.errors:
            continue
        lines.append(f"## Failures: {e.name} ({e.failed:,} total, showing up to 50)\n")
        lines.append("| Source id | Field | Error |")
        lines.append("|---|---|---|")
        for err in e.errors:
            lines.append(
                f"| `{err.get('source_id')}` | `{err.get('field')}` | {err.get('error')} |"
            )
        lines.append("")

    samples = [e for e in report.entities if e.samples]
    if samples:
        lines.append("## Transformed row samples\n")
        for e in samples:
            lines.append(f"### {e.name}\n")
            lines.append("```json")
            lines.append(json.dumps(e.samples, indent=2, default=str))
            lines.append("```\n")

    return "\n".join(lines)


# --- verification report ----------------------------------------------------


def print_verify(report: VerifyReport, console: Console) -> None:
    table = Table(title=f"Verification of {report.run_id}", header_style="bold")
    table.add_column("Entity")
    table.add_column("Source", justify="right")
    table.add_column("Recorded", justify="right")
    table.add_column("In v3", justify="right")
    table.add_column("Checked", justify="right")
    table.add_column("Mismatched", justify="right")
    table.add_column("Result")

    for e in report.entities:
        table.add_row(
            e.name,
            f"{e.source_rows:,}",
            f"{e.migrated_recorded:,}",
            f"{e.target_rows:,}" if e.target_rows is not None else "?",
            f"{e.checked:,}",
            f"[red]{e.mismatched:,}[/red]" if e.mismatched else "0",
            "[green]pass[/green]" if e.ok else "[red]FAIL[/red]",
        )
    console.print(table)

    for e in report.entities:
        for note in e.notes:
            console.print(f"  • [cyan]{e.name}[/cyan]: {note}")
        for d in e.discrepancies[:10]:
            loc = f".{d.field_name}" if d.field_name else ""
            console.print(
                f"  [red]![/red] [cyan]{e.name}[/cyan]{loc} id={d.source_id}: {d.detail}"
                + (f" (expected {d.expected!r}, got {d.actual!r})" if d.field_name else "")
            )

    console.print(
        "\n[bold green]Verification passed[/bold green]"
        if report.ok
        else "\n[bold red]Verification found discrepancies[/bold red]"
    )


def render_verify_markdown(report: VerifyReport) -> str:
    lines = [
        f"# Verification of run `{report.run_id}`\n",
        f"- Generated: {report.generated_at}",
        f"- Result: **{'PASS' if report.ok else 'FAIL'}**\n",
        "| Entity | Source rows | Recorded | In v3 | Spot-checked | Mismatched | Missing |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for e in report.entities:
        lines.append(
            f"| {e.name} | {e.source_rows:,} | {e.migrated_recorded:,} | "
            f"{e.target_rows if e.target_rows is not None else '?'} | {e.checked:,} | "
            f"{e.mismatched:,} | {e.missing_in_target:,} |"
        )
    lines.append("")

    for e in report.entities:
        if not e.discrepancies and not e.notes:
            continue
        lines.append(f"## {e.name}\n")
        for note in e.notes:
            lines.append(f"- _{note}_")
        if e.discrepancies:
            lines.append("")
            lines.append("| Kind | Source id | Field | Expected | Actual | Detail |")
            lines.append("|---|---|---|---|---|---|")
            for d in e.discrepancies:
                lines.append(
                    f"| {d.kind} | `{d.source_id}` | `{d.field_name or '-'}` | "
                    f"`{d.expected}` | `{d.actual}` | {d.detail} |"
                )
        lines.append("")
    return "\n".join(lines)
