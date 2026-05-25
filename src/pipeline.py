"""
Main pipeline script — runs the full investigation spike end to end.

Execution order:
  1. Ingest raw responses from EDGAR + Polygon (save to DB + disk)
  2. Profile the responses (understand what fields actually exist)
  3. Detect schema drift against any existing baseline
  4. Map to canonical schema (explicit, documented decisions)
  5. Reconcile across sources (cross-validate field interpretations)
  6. Save reports

Run with:
    python -m src.pipeline

Or investigate a specific entity:
    python -m src.pipeline --cik 0001067983   # Berkshire
"""

import argparse
import logging

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src import config
from src.ingest.edgar_client import (
    SAMPLE_FILERS,
    get_submissions,
)
from src.ingest.polygon_client import get_ticker_details, search_ticker_by_name
from src.profile.profiler import Profiler
from src.schema.canonical_mapper import (
    EdgarCompanyFactsMapper,
    PolygonTickerMapper,
    reconcile_entity,
)
from src.schema.drift_detector import SchemaDriftDetector
from src.storage.db import (
    init_db,
    query_drift_summary,
    query_reconciliation_summary,
    save_canonical,
    save_drift_events,
    save_raw,
    save_reconciliation,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
console = Console()


def run(ciks: list[str], save_samples: bool = True):
    config.validate()
    init_db()

    edgar_mapper = EdgarCompanyFactsMapper()
    polygon_mapper = PolygonTickerMapper()
    edgar_profiler = Profiler("edgar_company_facts", max_depth=config.PROFILE_MAX_DEPTH)

    baseline_path = config.PROJECT_ROOT / "data" / "baselines" / "edgar_facts.json"
    drift_detector = SchemaDriftDetector.load(baseline_path) if baseline_path.exists() else None

    console.print(
        Panel(
            f"[bold]API Investigation Spike[/bold]\n"
            f"Targets: {len(ciks)} entities | Samples: {save_samples}\n"
            f"Drift baseline: {'loaded' if drift_detector else 'not yet created'}",
            title="Atomic Insights · Ambiguous API Spike",
        )
    )

    all_edgar_raw = []
    reconciliations = []
    entity_names: dict[str, str] = {}

    # ── Phase 1: Ingest ────────────────────────────────────────────────────
    console.rule("[bold]Phase 1: Ingest")

    for cik in ciks:
        # EDGAR: entity submissions (includes tickers, entityType, sic, exchanges, etc.)
        try:
            edgar_raw = get_submissions(cik, save_sample=save_samples)
            edgar_raw_id = save_raw("edgar", "submissions", cik, edgar_raw)
            all_edgar_raw.append((cik, edgar_raw, edgar_raw_id))
        except Exception as e:
            console.print(f"  [red]✗[/red] CIK {cik} — EDGAR failed: {e}")
            continue

        entity_name = edgar_raw.get("name", cik)
        entity_names[cik] = entity_name
        console.print(f"\n  [cyan]{entity_name}[/cyan]  [dim]{cik}[/dim]")

        # Polygon: ticker details (cross-reference source)
        # Prefer tickers from the already-fetched EDGAR submissions; fall back to name search
        # EDGAR uses hyphens for class shares (BRK-B); Polygon requires dots (BRK.B)
        edgar_tickers = edgar_raw.get("tickers", [])
        raw_ticker = edgar_tickers[0] if edgar_tickers else search_ticker_by_name(edgar_raw.get("name", ""))
        ticker = raw_ticker.replace("-", ".") if raw_ticker else None
        polygon_raw_id = None
        polygon_raw = None

        if ticker:
            try:
                polygon_raw = get_ticker_details(ticker, save_sample=save_samples)
                polygon_raw_id = save_raw("polygon", "ticker_details", ticker, polygon_raw)
                console.print(f"    [green]✓[/green] EDGAR + Polygon ({ticker})")
            except Exception as e:
                console.print(f"    [yellow]⚠[/yellow] EDGAR only — Polygon failed for {ticker}: {e}")
        else:
            console.print(f"    [yellow]⚠[/yellow] EDGAR only — no ticker resolved")

        reconciliations.append((cik, edgar_raw, edgar_raw_id, polygon_raw, polygon_raw_id))

    # ── Phase 2: Profile ───────────────────────────────────────────────────
    console.rule("[bold]Phase 2: Profile")

    for cik, edgar_raw, _ in all_edgar_raw:
        # Profile the top-level structure (depth=1 to avoid exploding on 'facts')
        edgar_profiler.add({k: v for k, v in edgar_raw.items() if k != "facts"})

    profile_path = config.PROJECT_ROOT / "docs" / "edgar_profile.md"
    edgar_profiler.save_report(profile_path)

    n_fields = len(edgar_profiler._stats)
    anomalies = [(n, s) for n, s in edgar_profiler._stats.items() if s.anomaly]
    if anomalies:
        console.print(f"  [yellow]⚠[/yellow] {n_fields} fields — {len(anomalies)} anomalies:")
        for name, s in anomalies:
            console.print(f"    [yellow]{name}[/yellow]: {s.anomaly}")
    else:
        console.print(f"  [green]✓[/green] {n_fields} fields profiled — no anomalies")
    console.print(f"  [dim]Full report → {profile_path}[/dim]")

    # Set baseline if this is the first run
    if not drift_detector:
        console.print("\n[yellow]No drift baseline exists — creating from this run[/yellow]")
        drift_detector = SchemaDriftDetector("edgar_company_facts", max_depth=config.PROFILE_MAX_DEPTH)
        drift_detector.set_baseline_from_profiler(edgar_profiler)
        drift_detector.save_baseline(baseline_path)
        console.print(f"[dim]Baseline saved → {baseline_path}[/dim]")

    # ── Phase 3: Drift detection ───────────────────────────────────────────
    console.rule("[bold]Phase 3: Drift Detection")

    total_drift_issues = 0
    for cik, edgar_raw, edgar_raw_id in all_edgar_raw:
        top_level = {k: v for k, v in edgar_raw.items() if k != "facts"}
        issues = drift_detector.check(top_level)
        if issues:
            save_drift_events("edgar", issues, edgar_raw_id)
            total_drift_issues += len(issues)
            for issue in issues:
                color = "red" if issue.severity == "ERROR" else "yellow"
                console.print(
                    f"  [{color}]{issue.severity}[/{color}] {issue.field}: {issue.description}"
                )
        else:
            console.print(f"  [green]✓[/green] {entity_names.get(cik, cik)} — no drift")

    console.print(f"\n[dim]Total drift issues: {total_drift_issues}[/dim]")

    # ── Phase 4: Canonical mapping ─────────────────────────────────────────
    console.rule("[bold]Phase 4: Canonical Mapping")

    edgar_canonicals = {}
    polygon_canonicals = {}

    for cik, edgar_raw, edgar_raw_id in all_edgar_raw:
        canonical = edgar_mapper.map(edgar_raw)
        save_canonical(canonical, edgar_raw_id)
        edgar_canonicals[cik] = canonical

        label = entity_names.get(cik, cik)
        if canonical.get("_mapper_assumptions"):
            console.print(f"  [yellow]⚠[/yellow] {label} — assumptions:")
            for a in canonical["_mapper_assumptions"]:
                console.print(f"    · {a}")
        else:
            console.print(f"  [green]✓[/green] {label}")

    for cik, _, edgar_raw_id, polygon_raw, polygon_raw_id in reconciliations:
        if polygon_raw:
            canonical = polygon_mapper.map(polygon_raw)
            save_canonical(canonical, polygon_raw_id)
            polygon_canonicals[cik] = canonical

    # ── Phase 5: Reconciliation ────────────────────────────────────────────
    console.rule("[bold]Phase 5: Cross-Source Reconciliation")

    recon_table = Table(box=box.SIMPLE, show_header=True, header_style="bold", pad_edge=False)
    recon_table.add_column("entity", style="cyan", no_wrap=True)
    recon_table.add_column("cik", justify="center")
    recon_table.add_column("name", justify="center")
    recon_table.add_column("sic", justify="center")
    recon_table.add_column("result", justify="right")

    def _check_cell(check: dict | None) -> str:
        if not check:
            return "[dim]—[/dim]"
        if check["status"] == "MATCH":
            return "[green]✓[/green]"
        if check["status"] == "SIMILAR":
            return "[green]~[/green]"
        return f"[red]✗[/red]"

    for cik, _, edgar_raw_id, polygon_raw, polygon_raw_id in reconciliations:
        label = entity_names.get(cik, cik)
        if cik not in edgar_canonicals or cik not in polygon_canonicals:
            recon_table.add_row(label, "[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]", "[dim]skipped[/dim]")
            continue

        result = reconcile_entity(edgar_canonicals[cik], polygon_canonicals[cik])
        save_reconciliation(cik, result, edgar_raw_id, polygon_raw_id)

        checks = result["checks"]
        overall_color = "green" if result["overall"] == "VALIDATED" else "red"
        recon_table.add_row(
            label,
            _check_cell(checks.get("cik_match")),
            _check_cell(checks.get("name_match")),
            _check_cell(checks.get("sic_match")),
            f"[{overall_color}]{result['overall']}[/{overall_color}]",
        )

    console.print(recon_table)

    # ── Summary ────────────────────────────────────────────────────────────
    console.rule("[bold]Summary")

    drift_rows = list(query_drift_summary())
    recon_rows = list(query_reconciliation_summary())

    summary_table = Table(box=box.SIMPLE, show_header=True, header_style="bold", pad_edge=False)
    summary_table.add_column("source")
    summary_table.add_column("severity")
    summary_table.add_column("count", justify="right")
    for row in drift_rows:
        color = "red" if row["severity"] == "ERROR" else "yellow"
        summary_table.add_row(row["source"], f"[{color}]{row['severity']}[/{color}]", f"×{row['count']}")
    if not drift_rows:
        summary_table.add_row("[dim]—[/dim]", "[dim]none[/dim]", "[dim]0[/dim]")

    outcome_table = Table(box=box.SIMPLE, show_header=True, header_style="bold", pad_edge=False)
    outcome_table.add_column("outcome")
    outcome_table.add_column("count", justify="right")
    for row in recon_rows:
        color = "green" if row["overall_status"] == "VALIDATED" else "red"
        outcome_table.add_row(f"[{color}]{row['overall_status']}[/{color}]", f"×{row['count']}")
    if not recon_rows:
        outcome_table.add_row("[dim]no reconciliations[/dim]", "[dim]0[/dim]")

    console.print("[bold]Drift events[/bold]")
    console.print(summary_table)
    console.print("[bold]Reconciliation outcomes[/bold]")
    console.print(outcome_table)
    console.print(f"[dim]DB → {config.DB_PATH}[/dim]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the API investigation spike")
    parser.add_argument("--cik", nargs="*", help="CIK(s) to investigate")
    parser.add_argument("--no-samples", action="store_true", help="Don't save response snapshots")
    args = parser.parse_args()

    target_ciks = args.cik or list(SAMPLE_FILERS.values())[:3]  # default: first 3 sample filers
    run(target_ciks, save_samples=not args.no_samples)
