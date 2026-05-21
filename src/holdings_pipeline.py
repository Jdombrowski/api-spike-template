"""
13F Holdings extraction pipeline.

Fetches 13F-HR filings from EDGAR, parses the InfoTable XML into positions,
then attempts Polygon price cross-validation to confirm the reported USD values.

Runs after `make run` has populated entity data. Decoupled by design —
adding holdings doesn't alter the entity pipeline.

    make run-holdings                              # default 3 filers, 1 filing each
    make run-holdings CIK=0001067983               # Berkshire only
    make run-holdings CIK="0001067983 0001364742"  # multiple CIKs

Cross-validation logic:
    value_reported (USD thousands) × 1000 ≈ shares_held × closing_price_at_quarter_end
    ratio = (shares × price) / (value_reported × 1000)
    CLOSE     → ratio within 15% of 1.0  (rounding / price-date drift acceptable)
    DIVERGENT → ratio outside 15%        (investigate: wrong price date, non-equity, etc.)
    NO_PRICE  → ticker not resolved or Polygon returned no bar for that date
"""

import argparse
import logging

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src import config
from src.ingest.edgar_client import SAMPLE_FILERS, get_13f_document, get_13f_filings
from src.ingest.polygon_client import get_daily_bars, lookup_ticker_by_cusip, search_ticker_by_name
from src.schema.canonical_mapper import CanonicalHolding
from src.schema.holdings_mapper import ThirteenFMapper
from src.storage.db import init_db, query_holdings_summary, save_holdings, save_raw

log = logging.getLogger(__name__)
console = Console()

_CLOSE_THRESHOLD = 0.15  # within 15% of 1.0 → CLOSE


# ── Public entry point ─────────────────────────────────────────────────────


def run(ciks: list[str], max_filings: int = 1, validate_top: int = 10) -> None:
    config.validate()
    init_db()

    console.print(
        Panel(
            f"[bold]13F Holdings Extraction[/bold]\n"
            f"Targets: {len(ciks)} CIK(s)  |  Most recent {max_filings} filing(s) each\n"
            f"Cross-validation: top {validate_top} positions by reported value per filing",
            title="Stage 2 · Holdings Pipeline",
        )
    )

    for cik in ciks:
        console.print(f"\n[cyan]→ CIK {cik}[/cyan]")
        try:
            filings = get_13f_filings(cik)[:max_filings]
        except Exception as exc:
            console.print(f"  [red]✗[/red] Could not fetch filings list: {exc}")
            continue

        if not filings:
            console.print("  [yellow]⚠[/yellow] No 13F filings found for this CIK")
            continue

        for filing in filings:
            _process_filing(cik, filing, validate_top)

    # ── Summary ────────────────────────────────────────────────────────────
    console.rule("[bold]Holdings Summary")
    rows = query_holdings_summary()
    if not rows:
        console.print("  [dim]No holdings stored yet.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("CIK", style="dim")
    table.add_column("Positions", justify="right")
    table.add_column("Validated", justify="right")
    table.add_column("Value (USD M)", justify="right")
    table.add_column("Latest filing")

    for r in rows:
        validated = r.get("validated_count") or 0
        total = r.get("position_count") or 0
        table.add_row(
            r["cik"],
            str(total),
            f"{validated}/{total}",
            f"${r.get('total_value_millions') or 0:,.1f}",
            r.get("latest_filing") or "—",
        )
    console.print(table)


# ── Per-filing processing ──────────────────────────────────────────────────


def _process_filing(cik: str, filing: dict, validate_top: int) -> None:
    acc = filing["accession_number"]
    console.print(f"  [dim]{filing.get('form', '')}[/dim]  {filing['filing_date']}  {acc}")

    try:
        xml_text = get_13f_document(cik, acc)
    except Exception as exc:
        console.print(f"    [red]✗[/red] Could not fetch InfoTable XML: {exc}")
        return

    raw_id = save_raw(
        "edgar",
        "13f_infotable",
        cik,
        {"accession": acc, "xml_bytes": len(xml_text)},
    )

    mapper = ThirteenFMapper(cik, filing)
    try:
        holdings = mapper.parse(xml_text)
    except Exception as exc:
        console.print(f"    [red]✗[/red] XML parse failed: {exc}")
        return

    console.print(f"    parsed {len(holdings)} positions")

    sorted_holdings = sorted(holdings, key=lambda h: h.market_value_usd or 0, reverse=True)
    to_validate = sorted_holdings[:validate_top]
    remainder = sorted_holdings[validate_top:]

    validated = _cross_validate(to_validate)
    skipped = [{**_to_dict(h), "validation_status": "NO_PRICE"} for h in remainder]
    save_holdings(validated + skipped, raw_id)

    n_close = sum(1 for h in validated if h.get("validation_status") == "CLOSE")
    n_diverge = sum(1 for h in validated if h.get("validation_status") == "DIVERGENT")
    n_no_price = sum(1 for h in validated if h.get("validation_status") == "NO_PRICE") + len(
        skipped
    )
    console.print(
        f"    [green]✓[/green] cross-validation: "
        f"[green]{n_close} CLOSE[/green]  "
        f"[red]{n_diverge} DIVERGENT[/red]  "
        f"[dim]{n_no_price} NO_PRICE[/dim]"
        + (f"  [dim]({len(skipped)} skipped)[/dim]" if skipped else "")
    )


# ── Cross-validation ───────────────────────────────────────────────────────


def _cross_validate(holdings: list[CanonicalHolding]) -> list[dict]:
    """
    Attempt price validation for each position via Polygon.
    Returns list of dicts ready for save_holdings().
    """
    results = []
    for h in holdings:
        row = _to_dict(h)

        if h.shares_held is None or h.market_value_usd is None:
            row["validation_status"] = "NO_PRICE"
            results.append(row)
            continue

        ticker = _resolve_ticker(h)
        if not ticker:
            row["validation_status"] = "NO_PRICE"
            results.append(row)
            continue

        row["ticker"] = ticker
        price = _fetch_closing_price(ticker, h.as_of_date)
        if price is None:
            row["validation_status"] = "NO_PRICE"
            results.append(row)
            continue

        multiplier = 1000 if h.value_unit == "USD_THOUSANDS" else 1
        estimated = h.shares_held * price
        reported_full_usd = h.market_value_usd * multiplier
        ratio = estimated / reported_full_usd if reported_full_usd else None

        row["price_at_filing"] = price
        row["value_estimated"] = estimated
        row["validation_ratio"] = ratio

        if ratio is None:
            row["validation_status"] = "NO_PRICE"
        elif abs(1 - ratio) <= _CLOSE_THRESHOLD:
            row["validation_status"] = "CLOSE"
        else:
            row["validation_status"] = "DIVERGENT"
            log.warning(
                "[xval] DIVERGENT %-35s  ticker=%-6s  shares=%.0f  "
                "price=$%.2f  estimated=$%.0f  reported=$%.0f  ratio=%.3f",
                h.entity_name,
                ticker,
                h.shares_held,
                price,
                estimated,
                reported_full_usd,
                ratio,
            )

        results.append(row)

    return results


def _resolve_ticker(holding: CanonicalHolding) -> str | None:
    """CUSIP → ticker (exact), falling back to name search (fuzzy)."""
    if holding.cusip:
        ticker = lookup_ticker_by_cusip(holding.cusip)
        if ticker:
            log.info("[resolve] %-40s CUSIP %-10s → %s", holding.entity_name, holding.cusip, ticker)
            return ticker
        log.warning("[resolve] %-40s CUSIP %-10s → no match", holding.entity_name, holding.cusip)

    if not holding.entity_name:
        log.warning("[resolve] no entity name and no CUSIP match — skipping")
        return None

    try:
        ticker = search_ticker_by_name(holding.entity_name)
        if ticker:
            log.warning("[resolve] %-40s name search (fuzzy) → %s", holding.entity_name, ticker)
        else:
            log.warning("[resolve] %-40s name search → no match", holding.entity_name)
        return ticker
    except Exception as exc:
        log.warning("[resolve] %-40s name search failed: %s", holding.entity_name, exc)
        return None


def _fetch_closing_price(ticker: str, as_of_date: str | None) -> float | None:
    """
    Return the most recent closing price on or before as_of_date.

    Uses a 7-day lookback so quarter-ends that fall on weekends or holidays
    (e.g. Dec 31, Sep 30) resolve to the nearest preceding trading day.
    """
    if not as_of_date:
        return None
    try:
        from datetime import date, timedelta

        from_d = str(date.fromisoformat(as_of_date) - timedelta(days=7))
        resp = get_daily_bars(ticker, from_date=from_d, to_date=as_of_date)
        bars = resp.get("results", [])
        return bars[-1]["c"] if bars else None  # last bar = most recent close ≤ as_of_date
    except Exception:
        return None


def _to_dict(h: CanonicalHolding) -> dict:
    """Flatten a CanonicalHolding to a dict matching the holdings table schema."""
    acc = h.canonical_id.split(":")[1] if ":" in h.canonical_id else None
    return {
        "cik": h.source_entity_id,
        "accession_number": acc,
        "form_type": h.form_type,
        "filing_date": h.filing_date,
        "as_of_date": h.as_of_date,
        "issuer_name": h.entity_name,
        "cusip": h.cusip,
        "ticker": h.ticker,
        "shares_held": h.shares_held,
        "value_reported": h.market_value_usd,
        "value_unit": h.value_unit,
        "price_at_filing": None,
        "value_estimated": None,
        "validation_ratio": None,
        "validation_status": None,
    }


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Extract and cross-validate 13F holdings")
    parser.add_argument("--cik", nargs="*", help="CIK(s) to process (space-separated)")
    parser.add_argument("--max-filings", type=int, default=1, help="Filings per CIK (default: 1)")
    parser.add_argument(
        "--validate-top",
        type=int,
        default=10,
        help="Top N positions to cross-validate per filing (default: 10)",
    )
    args = parser.parse_args()

    target_ciks = args.cik or list(SAMPLE_FILERS.values())[:3]
    run(target_ciks, max_filings=args.max_filings, validate_top=args.validate_top)
