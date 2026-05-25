"""
SEC EDGAR DERA 13F bulk dataset downloader and parser.

Downloads quarterly 13F structured data directly from SEC DERA, bypassing
per-filing API calls and rate limits entirely. Produces the same holdings
row format as holdings_pipeline.py so both paths feed the same table.

DERA dataset base URL:
    https://www.sec.gov/files/structureddata/data/form-13f-data-sets/

Filename convention (changed in 2024):
    2023 and earlier: {year}q{n}_form13f.zip
    2024 and later:   filing-date-range format, e.g. 01mar2024-31may2024_form13f.zip
    See _quarter_filename() for the full mapping.

Each ZIP contains two TSV files:
    SUBMISSION.tsv  — one row per 13F filing (CIK, period, accession number)
    INFOTABLE.tsv   — one row per position across ALL filers for that quarter

ZIPs are cached under data/dera/ — re-running against a new CIK list re-parses
the local cache rather than re-downloading (~150–250 MB per quarter).

Usage:
    make run-bulk                                    # default CIKs, 8 quarters
    make run-bulk QUARTERS=4 CIK="0001067983"
"""

import argparse
import calendar
import csv
import io
import logging
import zipfile
from datetime import date

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import track
from rich.table import Table

from src import config
from src.ingest.edgar_client import SAMPLE_FILERS
from src.ingest.http_client import get_bytes
from src.schema.holdings_mapper import _infer_value_unit
from src.storage.db import init_db, query_holdings_summary, save_holdings

log = logging.getLogger(__name__)
console = Console()

_DERA_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"
_HEADERS = {"User-Agent": config.EDGAR_USER_AGENT}
_CACHE_DIR = config.PROJECT_ROOT / "data" / "dera"


# ── Public entry point ─────────────────────────────────────────────────────


def run(ciks: list[str], quarters: int = 8) -> None:
    config.validate()
    init_db()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    quarter_list = list(_iter_quarters(quarters))

    console.print(
        Panel(
            f"[bold]13F DERA Bulk Ingest[/bold]\n"
            f"Targets: {len(ciks)} CIK(s)  |  {quarters} quarters  "
            f"({quarter_list[-1][0]} Q{quarter_list[-1][1]} "
            f"→ {quarter_list[0][0]} Q{quarter_list[0][1]})\n"
            f"Cache: {_CACHE_DIR}",
            title="Stage 4 · Bulk Holdings Pipeline",
        )
    )

    target_ciks = set(ciks)
    total_saved = 0

    for year, quarter in track(quarter_list, description="Ingesting quarters..."):
        saved = _ingest_quarter(year, quarter, target_ciks)
        total_saved += saved

    # ── Summary ────────────────────────────────────────────────────────────
    console.rule("[bold]Holdings Summary")
    console.print(f"  Positions saved this run: [bold]{total_saved:,}[/bold]\n")

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("CIK", style="dim")
    table.add_column("Quarters", justify="right")
    table.add_column("Positions", justify="right")
    table.add_column("Value (USD B)", justify="right")
    table.add_column("Latest quarter")

    for r in query_holdings_summary():
        table.add_row(
            r["cik"],
            str(r.get("quarter_count") or "—"),
            f"{r.get('position_count') or 0:,}",
            f"${(r.get('total_value_millions') or 0) / 1000:,.1f}",
            r.get("latest_filing") or "—",
        )
    console.print(table)


# ── Per-quarter ingest ─────────────────────────────────────────────────────


def _quarter_filename(year: int, quarter: int) -> str:
    """
    Return the DERA ZIP filename for a given (year, quarter) pair.

    SEC changed the naming convention starting in 2024:
      2023 and earlier: {year}q{n}_form13f.zip
      2024 and later:   filing-date-range format, e.g. 01mar2024-31may2024_form13f.zip

    The date ranges reflect when filings are received, not the period of report:
      Q1 (Jan-Mar period): filed Mar-May  → 01mar{y}-31may{y}
      Q2 (Apr-Jun period): filed Jun-Aug  → 01jun{y}-31aug{y}
      Q3 (Jul-Sep period): filed Sep-Nov  → 01sep{y}-30nov{y}
      Q4 (Oct-Dec period): filed Dec-Feb  → 01dec{y}-{feb}{y+1}
    """
    if year <= 2023:
        return f"{year}q{quarter}_form13f.zip"
    if quarter == 1:
        return f"01mar{year}-31may{year}_form13f.zip"
    if quarter == 2:
        return f"01jun{year}-31aug{year}_form13f.zip"
    if quarter == 3:
        return f"01sep{year}-30nov{year}_form13f.zip"
    # Q4: spans into next year; Feb end-day depends on leap year
    feb_end = 29 if calendar.isleap(year + 1) else 28
    return f"01dec{year}-{feb_end:02d}feb{year + 1}_form13f.zip"


def _ingest_quarter(year: int, quarter: int, target_ciks: set[str]) -> int:
    label = f"{year}q{quarter}"
    cache_path = _CACHE_DIR / f"{label}_form13f.zip"

    if cache_path.exists():
        console.print(f"\n[cyan]→ {year} Q{quarter}[/cyan]  [dim](cached)[/dim]")
        zip_bytes = cache_path.read_bytes()
    else:
        url = f"{_DERA_BASE}/{_quarter_filename(year, quarter)}"
        console.print(f"\n[cyan]→ {year} Q{quarter}[/cyan]  {url}")
        try:
            zip_bytes = get_bytes(url, source="edgar", headers=_HEADERS, timeout=300)
        except LookupError:
            console.print("  [yellow]⚠[/yellow] Dataset not yet published — skipping")
            return 0
        except Exception as exc:
            console.print(f"  [red]✗[/red] Download failed: {exc}")
            return 0
        cache_path.write_bytes(zip_bytes)
        console.print(f"  cached → {cache_path.name} ({len(zip_bytes) / 1_048_576:.1f} MB)")

    try:
        rows = _parse_zip(zip_bytes, target_ciks)
    except Exception as exc:
        console.print(f"  [red]✗[/red] Parse failed: {exc}")
        return 0

    if not rows:
        console.print("  [dim]No positions for target CIKs[/dim]")
        return 0

    save_holdings(rows)
    console.print(f"  [green]✓[/green] {len(rows):,} positions saved")
    return len(rows)


# ── Quarter range helper ───────────────────────────────────────────────────


def _iter_quarters(n: int):
    """Yield the last N complete (year, quarter) pairs, most recent first."""
    today = date.today()
    month = today.month
    if month <= 3:
        year, q = today.year - 1, 4
    elif month <= 6:
        year, q = today.year, 1
    elif month <= 9:
        year, q = today.year, 2
    else:
        year, q = today.year, 3

    for _ in range(n):
        yield year, q
        q -= 1
        if q == 0:
            q, year = 4, year - 1


# ── ZIP parsing ────────────────────────────────────────────────────────────


def _parse_zip(zip_bytes: bytes, target_ciks: set[str]) -> list[dict]:
    """
    Parse a DERA quarterly ZIP into holding dicts filtered to target_ciks.

    Joins SUBMISSION.tsv (filing metadata) with INFOTABLE.tsv (positions)
    on ACCESSION_NUMBER. PERIODOFREPORT is the authoritative quarter-end
    date — no derive_quarter_end heuristic needed here.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        submissions = _read_tsv(zf, _find_member(zf, "SUBMISSION"))
        infotable = _read_tsv(zf, _find_member(zf, "INFOTABLE"))

    # Build accession → filing metadata index for target CIKs only
    sub_index: dict[str, dict] = {}
    for row in submissions:
        cik_raw = row.get("CIK", "").lstrip("0") or "0"
        cik_padded = cik_raw.zfill(10)
        if not _matches_target(cik_raw, cik_padded, target_ciks):
            continue
        acc = _normalise_accession(row.get("ACCESSION_NUMBER", ""))
        sub_index[acc] = {
            "cik": cik_padded,
            "filing_date": row.get("FILING_DATE", ""),
            "as_of_date": row.get("PERIODOFREPORT", ""),
            "form_type": row.get("SUBMISSIONTYPE", "13F-HR"),
        }

    if not sub_index:
        return []

    rows: list[dict] = []
    for pos in infotable:
        acc = _normalise_accession(pos.get("ACCESSION_NUMBER", ""))
        meta = sub_index.get(acc)
        if not meta:
            continue

        try:
            shares = float(pos.get("SSHPRNAMT") or 0) or None
            value = float(pos.get("VALUE") or 0) or None
        except (ValueError, TypeError):
            shares, value = None, None

        assumptions: list[str] = []
        value_unit = _infer_value_unit(
            int(value) if value is not None else None,
            int(shares) if shares is not None else None,
            assumptions,
        )

        rows.append(
            {
                "cik": meta["cik"],
                "accession_number": acc,
                "form_type": meta["form_type"],
                "filing_date": meta["filing_date"],
                "as_of_date": meta["as_of_date"],
                "issuer_name": (pos.get("NAMEOFISSUER") or "").strip(),
                "cusip": (pos.get("CUSIP") or "").strip() or None,
                "ticker": None,
                "shares_held": shares,
                "value_reported": value,
                "value_unit": value_unit,
                "price_at_filing": None,
                "value_estimated": None,
                "validation_ratio": None,
                "validation_status": None,
            }
        )

    return rows


# ── TSV / ZIP helpers ──────────────────────────────────────────────────────


def _find_member(zf: zipfile.ZipFile, keyword: str) -> str:
    """Return the ZIP member whose filename contains keyword (case-insensitive)."""
    for name in zf.namelist():
        if keyword.upper() in name.upper():
            return name
    raise LookupError(f"No member matching '{keyword}' found in ZIP. Members: {zf.namelist()}")


def _read_tsv(zf: zipfile.ZipFile, member: str) -> list[dict]:
    """Read a TSV member into a list of dicts with upper-cased column names."""
    with zf.open(member) as f:
        text = f.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    return [{k.strip().upper(): (v or "").strip() for k, v in row.items()} for row in reader]


def _normalise_accession(raw: str) -> str:
    """Normalise to dashed form: 0001234567-24-000001."""
    clean = raw.strip().replace(" ", "")
    if "-" not in clean and len(clean) == 18:
        return f"{clean[:10]}-{clean[10:12]}-{clean[12:]}"
    return clean


def _matches_target(cik_raw: str, cik_padded: str, target_ciks: set[str]) -> bool:
    """Match a CIK against the target set regardless of zero-padding."""
    return bool(target_ciks & {cik_raw, cik_padded, cik_raw.zfill(10)})


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Ingest SEC DERA 13F bulk datasets")
    parser.add_argument("--cik", nargs="*", help="CIK(s) to filter (space-separated)")
    parser.add_argument(
        "--quarters", type=int, default=8, help="Number of quarters to ingest (default: 8)"
    )
    args = parser.parse_args()

    target_ciks = args.cik or list(SAMPLE_FILERS.values())[:3]
    run(target_ciks, quarters=args.quarters)
