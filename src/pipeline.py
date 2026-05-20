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
import json
import logging
import sys
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich import print as rprint

from src import config
from src.ingest.edgar_client import (
    get_company_facts, get_submissions, get_13f_filings, SAMPLE_FILERS
)
from src.ingest.polygon_client import get_ticker_details, resolve_cik_to_ticker
from src.profile.profiler import Profiler
from src.schema.drift_detector import SchemaDriftDetector
from src.schema.canonical_mapper import (
    EdgarCompanyFactsMapper, PolygonTickerMapper, reconcile_entity
)
from src.storage.db import (
    init_db, save_raw, save_canonical, save_drift_events,
    save_reconciliation, query_drift_summary, query_reconciliation_summary
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log     = logging.getLogger(__name__)
console = Console()


def run(ciks: list[str], save_samples: bool = True):
    config.validate()
    init_db()

    edgar_mapper   = EdgarCompanyFactsMapper()
    polygon_mapper = PolygonTickerMapper()
    edgar_profiler = Profiler("edgar_company_facts", max_depth=1)

    baseline_path = config.PROJECT_ROOT / "data" / "baselines" / "edgar_facts.json"
    drift_detector = (
        SchemaDriftDetector.load(baseline_path)
        if baseline_path.exists()
        else None
    )

    console.print(Panel(
        f"[bold]API Investigation Spike[/bold]\n"
        f"Targets: {len(ciks)} entities | Samples: {save_samples}\n"
        f"Drift baseline: {'loaded' if drift_detector else 'not yet created'}",
        title="Atomic Insights · Ambiguous API Spike"
    ))

    all_edgar_raw  = []
    reconciliations = []

    # ── Phase 1: Ingest ────────────────────────────────────────────────────
    console.rule("[bold]Phase 1: Ingest")

    for cik in ciks:
        console.print(f"\n[cyan]→ CIK {cik}[/cyan]")

        # EDGAR: company facts
        try:
            edgar_raw = get_company_facts(cik, save_sample=save_samples)
            edgar_raw_id = save_raw("edgar", "companyfacts", cik, edgar_raw)
            all_edgar_raw.append((cik, edgar_raw, edgar_raw_id))
            console.print(f"  [green]✓[/green] EDGAR facts ({len(edgar_raw)} top-level keys)")
        except Exception as e:
            console.print(f"  [red]✗[/red] EDGAR facts failed: {e}")
            continue

        # Polygon: ticker details (cross-reference source)
        ticker = resolve_cik_to_ticker(cik)
        polygon_raw_id = None
        polygon_raw    = None

        if ticker:
            try:
                polygon_raw     = get_ticker_details(ticker, save_sample=save_samples)
                polygon_raw_id  = save_raw("polygon", "ticker_details", ticker, polygon_raw)
                console.print(f"  [green]✓[/green] Polygon ticker {ticker}")
            except Exception as e:
                console.print(f"  [yellow]⚠[/yellow] Polygon failed for {ticker}: {e}")
        else:
            console.print(f"  [yellow]⚠[/yellow] Could not resolve ticker for CIK {cik} — skipping Polygon")

        reconciliations.append((cik, edgar_raw, edgar_raw_id, polygon_raw, polygon_raw_id))

    # ── Phase 2: Profile ───────────────────────────────────────────────────
    console.rule("[bold]Phase 2: Profile")

    for cik, edgar_raw, _ in all_edgar_raw:
        # Profile the top-level structure (depth=1 to avoid exploding on 'facts')
        edgar_profiler.add({k: v for k, v in edgar_raw.items() if k != "facts"})

    edgar_profiler.report()

    # Save profile report
    profile_path = config.PROJECT_ROOT / "docs" / "edgar_profile.md"
    edgar_profiler.save_report(profile_path)
    console.print(f"\n[dim]Profile saved → {profile_path}[/dim]")

    # Set baseline if this is the first run
    if not drift_detector:
        console.print("\n[yellow]No drift baseline exists — creating from this run[/yellow]")
        drift_detector = SchemaDriftDetector("edgar_company_facts")
        drift_detector.set_baseline_from_profiler(edgar_profiler)
        drift_detector.save_baseline(baseline_path)
        console.print(f"[dim]Baseline saved → {baseline_path}[/dim]")

    # ── Phase 3: Drift detection ───────────────────────────────────────────
    console.rule("[bold]Phase 3: Drift Detection")

    total_drift_issues = 0
    for cik, edgar_raw, edgar_raw_id in all_edgar_raw:
        top_level = {k: v for k, v in edgar_raw.items() if k != "facts"}
        issues    = drift_detector.check(top_level)
        if issues:
            save_drift_events("edgar", issues, edgar_raw_id)
            total_drift_issues += len(issues)
            for issue in issues:
                color = "red" if issue.severity == "ERROR" else "yellow"
                console.print(f"  [{color}]{issue.severity}[/{color}] {issue.field}: {issue.description}")
        else:
            console.print(f"  [green]✓[/green] CIK {cik} — no drift detected")

    console.print(f"\n[dim]Total drift issues: {total_drift_issues}[/dim]")

    # ── Phase 4: Canonical mapping ─────────────────────────────────────────
    console.rule("[bold]Phase 4: Canonical Mapping")

    edgar_canonicals  = {}
    polygon_canonicals = {}

    for cik, edgar_raw, edgar_raw_id in all_edgar_raw:
        canonical = edgar_mapper.map(edgar_raw)
        save_canonical(canonical, edgar_raw_id)
        edgar_canonicals[cik] = canonical

        if canonical.get("_mapper_assumptions"):
            console.print(f"  [yellow]assumptions for CIK {cik}:[/yellow]")
            for a in canonical["_mapper_assumptions"]:
                console.print(f"    · {a}")
        else:
            console.print(f"  [green]✓[/green] CIK {cik} mapped cleanly")

    for cik, _, edgar_raw_id, polygon_raw, polygon_raw_id in reconciliations:
        if polygon_raw:
            canonical = polygon_mapper.map(polygon_raw)
            save_canonical(canonical, polygon_raw_id)
            polygon_canonicals[cik] = canonical

    # ── Phase 5: Reconciliation ────────────────────────────────────────────
    console.rule("[bold]Phase 5: Cross-Source Reconciliation")

    for cik, _, edgar_raw_id, polygon_raw, polygon_raw_id in reconciliations:
        if cik not in edgar_canonicals or cik not in polygon_canonicals:
            console.print(f"  [dim]CIK {cik}: skipped (missing one source)[/dim]")
            continue

        result = reconcile_entity(edgar_canonicals[cik], polygon_canonicals[cik])
        save_reconciliation(cik, result, edgar_raw_id, polygon_raw_id)

        status_color = "green" if result["overall"] == "VALIDATED" else "red"
        console.print(
            f"  [{status_color}]{result['overall']}[/{status_color}]  CIK {cik}"
        )
        for check_name, check in result["checks"].items():
            icon = "✓" if check["status"] in ("MATCH", "SIMILAR") else "✗"
            color = "green" if icon == "✓" else "red"
            console.print(
                f"    [{color}]{icon}[/{color}] {check_name}: "
                f"edgar={check.get('edgar')} polygon={check.get('polygon')}"
            )

    # ── Summary ────────────────────────────────────────────────────────────
    console.rule("[bold]Summary")
    console.print("\n[bold]Drift events by severity:[/bold]")
    for row in query_drift_summary():
        console.print(f"  {row['source']:10s}  {row['severity']:8s}  ×{row['count']}")

    console.print("\n[bold]Reconciliation outcomes:[/bold]")
    for row in query_reconciliation_summary():
        console.print(f"  {row['overall_status']:15s}  ×{row['count']}")

    console.print(
        f"\n[dim]Raw responses, canonical records, drift events, and reconciliation "
        f"results saved to {config.DB_PATH}[/dim]"
    )
    console.print(
        f"[dim]Profile report → docs/edgar_profile.md[/dim]\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the API investigation spike")
    parser.add_argument("--cik", nargs="*", help="CIK(s) to investigate")
    parser.add_argument("--no-samples", action="store_true", help="Don't save response snapshots")
    args = parser.parse_args()

    target_ciks = args.cik or list(SAMPLE_FILERS.values())[:3]  # default: first 3 sample filers
    run(target_ciks, save_samples=not args.no_samples)
