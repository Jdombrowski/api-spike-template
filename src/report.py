"""
Investigation report and CSV export.

    make report   → rich terminal summary of all findings
    make export   → timestamped CSVs in data/exports/

Query functions are intentionally public — they're the kind of thing you'd
call from a runbook, a Jupyter notebook, or a downstream dashboard.
"""
import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from src import config
from src.storage.db import _conn, init_db, query_holdings_summary

log = logging.getLogger(__name__)
console = Console()


# ── Query helpers ──────────────────────────────────────────────────────────

def query_ingestion_summary() -> dict:
    """High-level counts: how many entities, which sources, when last run."""
    with _conn() as con:
        entity_count = con.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM raw_responses"
        ).fetchone()[0]
        source_rows = con.execute(
            "SELECT source, COUNT(*) AS n FROM raw_responses GROUP BY source"
        ).fetchall()
        last_run = con.execute(
            "SELECT MAX(ingested_at) FROM raw_responses"
        ).fetchone()[0]
    return {
        "entity_count": entity_count,
        "sources": {r["source"]: r["n"] for r in source_rows},
        "last_run": last_run,
    }


def query_entities() -> list[dict]:
    """
    One row per EDGAR entity (latest run only) with its latest reconciliation status.
    Joins canonical_facts → reconciliation; Polygon-only entities are excluded.
    """
    with _conn() as con:
        rows = con.execute("""
            SELECT
                cf.cik,
                cf.entity_name,
                cf.entity_type,
                cf.ticker,
                cf.sic_code,
                cf.created_at,
                r.overall_status
            FROM canonical_facts cf
            INNER JOIN (
                SELECT cik, MAX(id) AS latest_id
                FROM canonical_facts
                WHERE source = 'edgar_companyfacts'
                GROUP BY cik
            ) latest ON cf.id = latest.latest_id
            LEFT JOIN reconciliation r ON cf.cik = r.entity_id
            ORDER BY cf.entity_name
        """).fetchall()
    return [dict(r) for r in rows]


def query_reconciliation_detail() -> list[dict]:
    """Full per-check breakdown for every reconciliation run, newest first."""
    with _conn() as con:
        rows = con.execute("""
            SELECT entity_id, overall_status, checks_json, reconciled_at
            FROM reconciliation
            ORDER BY reconciled_at DESC
        """).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["checks"] = json.loads(d.pop("checks_json"))
        results.append(d)
    return results


def query_drift_by_entity() -> dict[str, list[dict]]:
    """Drift events keyed by CIK, joined through raw_responses for the entity_id."""
    with _conn() as con:
        rows = con.execute("""
            SELECT
                rr.entity_id AS cik,
                de.severity,
                de.field,
                de.description,
                de.detected_at
            FROM drift_events de
            JOIN raw_responses rr ON de.raw_response_id = rr.id
            ORDER BY de.severity, de.field
        """).fetchall()
    result: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        result.setdefault(d["cik"], []).append(d)
    return result


def query_open_assumptions() -> list[dict]:
    """
    Pull live assumption annotations out of mapped_json in canonical_facts.
    These are the fields flagged at runtime by the mapper — not hardcoded here.
    """
    with _conn() as con:
        rows = con.execute(
            "SELECT cik, entity_name, mapped_json FROM canonical_facts "
            "WHERE source = 'edgar_companyfacts'"
        ).fetchall()
    seen: set[str] = set()
    assumptions = []
    for r in rows:
        mapped = json.loads(r["mapped_json"])
        for assumption in mapped.get("_mapper_assumptions", []):
            if assumption not in seen:
                seen.add(assumption)
                assumptions.append({
                    "cik":         r["cik"],
                    "entity_name": r["entity_name"],
                    "assumption":  assumption,
                })
    return assumptions


# ── Terminal report ────────────────────────────────────────────────────────

def report() -> None:
    """Rich terminal summary of all investigation findings."""
    init_db()   # ensure holdings table exists for DBs created before Stage 2
    summary    = query_ingestion_summary()
    entities   = query_entities()
    recons     = query_reconciliation_detail()
    drift      = query_drift_by_entity()
    assumptions = query_open_assumptions()

    if not entities:
        console.print("[yellow]No data found — run `make run` first.[/yellow]")
        return

    sources_str = "  ".join(f"{s}: {n} call(s)" for s, n in summary["sources"].items())
    console.print(Panel(
        f"[bold]Investigation Report[/bold]\n"
        f"Entities: {summary['entity_count']}  |  {sources_str}\n"
        f"Last ingested: {summary['last_run'] or 'unknown'}",
        title="API Spike · Findings",
    ))

    # ── Entities + reconciliation status ───────────────────────────────────
    console.rule("[bold]Entities")
    _STATUS_COLOR = {"VALIDATED": "green", "NEEDS_REVIEW": "red"}

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("CIK",    style="dim", no_wrap=True)
    table.add_column("Name")
    table.add_column("Type",   style="dim")
    table.add_column("Ticker", style="dim")
    table.add_column("SIC",    style="dim")
    table.add_column("Cross-ref status")

    for e in entities:
        status = e.get("overall_status")
        color  = _STATUS_COLOR.get(status or "", "dim")
        label  = f"[{color}]{status}[/{color}]" if status else "[dim]no polygon data[/dim]"
        table.add_row(
            e.get("cik") or "—",
            e.get("entity_name") or "—",
            e.get("entity_type") or "—",
            e.get("ticker") or "—",
            e.get("sic_code") or "—",
            label,
        )
    console.print(table)

    # ── Reconciliation detail ──────────────────────────────────────────────
    if recons:
        console.rule("[bold]Reconciliation Detail")
        for rec in recons:
            color = _STATUS_COLOR.get(rec["overall_status"], "dim")
            console.print(f"\n  [{color}]{rec['overall_status']}[/{color}]  CIK {rec['entity_id']}")
            for check_name, check in rec["checks"].items():
                match_color = "green" if check["status"] in ("MATCH", "SIMILAR") else "red"
                console.print(
                    f"    [{match_color}]{check['status']:8s}[/{match_color}]  {check_name}"
                    f"  edgar=[dim]{check.get('edgar')}[/dim]"
                    f"  polygon=[dim]{check.get('polygon')}[/dim]"
                )

    # ── Drift events ───────────────────────────────────────────────────────
    console.rule("[bold]Drift Events")
    if not drift:
        console.print("  [green]✓ No drift detected[/green]")
    else:
        _DRIFT_COLOR = {"ERROR": "red", "WARNING": "yellow", "INFO": "dim"}
        for cik, issues in drift.items():
            console.print(f"\n  CIK {cik}")
            for issue in issues:
                color = _DRIFT_COLOR.get(issue["severity"], "dim")
                console.print(
                    f"    [{color}]{issue['severity']:8s}[/{color}]"
                    f"  {issue['field']}: {issue['description']}"
                )

    # ── Open assumptions ───────────────────────────────────────────────────
    console.rule("[bold]Open Assumptions")
    if not assumptions:
        console.print("  [green]✓ No live assumption flags[/green]")
    else:
        for a in assumptions:
            console.print(f"  [yellow]⚠[/yellow]  {a['assumption']}")

    # ── Holdings summary (populated by make run-holdings) ──────────────────
    holdings_rows = query_holdings_summary()
    if holdings_rows:
        console.rule("[bold]13F Holdings")
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("CIK",           style="dim")
        table.add_column("Positions",     justify="right")
        table.add_column("Validated",     justify="right")
        table.add_column("Value (USD M)", justify="right")
        table.add_column("Latest filing")

        for r in holdings_rows:
            validated = r.get("validated_count") or 0
            total     = r.get("position_count")  or 0
            table.add_row(
                r["cik"],
                str(total),
                f"{validated}/{total}",
                f"${r.get('total_value_millions') or 0:,.1f}",
                r.get("latest_filing") or "—",
            )
        console.print(table)

    console.print()


# ── CSV export ─────────────────────────────────────────────────────────────

def export(out_dir: Path | None = None) -> None:
    """Write canonical_facts, reconciliation, and drift_events as timestamped CSVs."""
    out = out_dir or (config.PROJECT_ROOT / "data" / "exports")
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    console.print(f"\n[dim]Writing exports → {out}/[/dim]\n")
    _export_canonical(out, ts)
    _export_reconciliation(out, ts)
    _export_drift(out, ts)
    console.print()


def _export_canonical(out: Path, ts: str) -> None:
    with _conn() as con:
        rows = con.execute("""
            SELECT cik, entity_name, entity_type, ticker, sic_code, source, created_at
            FROM canonical_facts ORDER BY entity_name
        """).fetchall()
    path = out / f"canonical_facts__{ts}.csv"
    _write_csv(path, rows, ["cik", "entity_name", "entity_type", "ticker", "sic_code", "source", "created_at"])
    console.print(f"  [green]✓[/green] {path.name}  ({len(rows)} rows)")


def _export_reconciliation(out: Path, ts: str) -> None:
    flat: list[dict] = []
    for rec in query_reconciliation_detail():
        base = {
            "entity_id":     rec["entity_id"],
            "overall_status": rec["overall_status"],
            "reconciled_at": rec["reconciled_at"],
        }
        for check_name, check in rec["checks"].items():
            flat.append({
                **base,
                "check":         check_name,
                "status":        check["status"],
                "edgar_value":   check.get("edgar"),
                "polygon_value": check.get("polygon"),
                "note":          check.get("note", ""),
            })
    path = out / f"reconciliation__{ts}.csv"
    _write_csv(path, flat, ["entity_id", "overall_status", "check", "status", "edgar_value", "polygon_value", "note", "reconciled_at"])
    console.print(f"  [green]✓[/green] {path.name}  ({len(flat)} rows)")


def _export_drift(out: Path, ts: str) -> None:
    with _conn() as con:
        rows = con.execute("""
            SELECT rr.entity_id AS cik, de.source, de.severity, de.field,
                   de.description, de.detected_at
            FROM drift_events de
            JOIN raw_responses rr ON de.raw_response_id = rr.id
            ORDER BY de.severity, de.field
        """).fetchall()
    path = out / f"drift_events__{ts}.csv"
    _write_csv(path, rows, ["cik", "source", "severity", "field", "description", "detected_at"])
    console.print(f"  [green]✓[/green] {path.name}  ({len(rows)} rows)")


def _write_csv(path: Path, rows: list, fieldnames: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Investigation report and export")
    parser.add_argument(
        "--export", action="store_true",
        help="Write findings to CSV instead of printing terminal report",
    )
    args = parser.parse_args()

    if args.export:
        export()
    else:
        report()
