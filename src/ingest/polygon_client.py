"""
Polygon.io API client.

Role in this spike: the "clean reference source" we cross-reference against
EDGAR data to validate our field interpretations.

Real-world custodian analog: this is like having a second data feed
(e.g., Bloomberg or Reuters) to triangulate against your primary custodian
when a field value looks suspicious.

We use:
  - Ticker details     → validate company identity (CIK ↔ ticker mapping)
  - Daily OHLCV        → market prices for cross-referencing holding values
  - Ticker types       → understand security classification fields
"""
import logging
from datetime import date, timedelta
from typing import Any

from src import config
from src.ingest.http_client import get

log = logging.getLogger(__name__)

_BASE   = config.POLYGON_BASE
_PARAMS = {"apiKey": config.POLYGON_API_KEY}


def get_ticker_details(ticker: str, save_sample: bool = False) -> dict:
    """
    Fetch metadata for a ticker — name, CIK, SIC, market cap, description.
    
    Cross-reference use: EDGAR gives us a CIK. Polygon gives us the ticker
    for the same entity. If they match, we've validated our entity mapping.
    If they don't, we have a data quality issue to investigate.
    """
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker.upper()}"
    log.info("[polygon] fetching ticker details: %s", ticker)
    return get(
        url, source="polygon", params=_PARAMS,
        save_sample=save_sample, sample_name=f"ticker_{ticker}"
    )


def get_daily_bars(
    ticker: str,
    from_date: str | None = None,
    to_date: str | None   = None,
    save_sample: bool     = False,
) -> dict:
    """
    Fetch daily OHLCV bars for a ticker.

    Cross-reference use: EDGAR 13F filings report holding values as of quarter-end.
    We can validate those values by: shares_held * closing_price ≈ reported_value.
    If they diverge significantly, the EDGAR value field is either:
      a) using a different price date
      b) including accrued interest or fees
      c) using a non-standard valuation method
    All three are things you'd need to document in your field mapping.
    """
    to   = to_date   or str(date.today())
    frm  = from_date or str(date.today() - timedelta(days=90))
    url  = f"{_BASE}/v2/aggs/ticker/{ticker.upper()}/range/1/day/{frm}/{to}"
    log.info("[polygon] fetching daily bars: %s %s→%s", ticker, frm, to)
    return get(
        url, source="polygon", params={"adjusted": "true", **_PARAMS},
        save_sample=save_sample, sample_name=f"bars_{ticker}"
    )


def get_ticker_types() -> dict:
    """
    Fetch the full taxonomy of ticker types (CS, ETF, WARRANT, etc.)
    Useful for understanding what the 'type' field in EDGAR maps to.
    """
    url = "https://api.polygon.io/v3/reference/tickers/types"
    return get(url, source="polygon", params=_PARAMS)


def search_ticker_by_name(name: str) -> str | None:
    """
    Search Polygon for a ticker by company name.

    Fallback for entities where EDGAR provides no tickers list.
    Result is an assumption — validate against the returned CIK before use.

    Returns ticker string or None if no match found.
    """
    if not name:
        return None
    try:
        log.warning(
            "[polygon] searching by name '%s' — result is an assumption, validate manually", name
        )
        url = "https://api.polygon.io/v3/reference/tickers"
        results = get(url, source="polygon", params={
            "search": name[:30],  # truncate to avoid over-specific query
            "limit": 5,
            **_PARAMS
        })
        hits = results.get("results", [])
        if hits:
            return hits[0].get("ticker")
    except Exception as e:
        log.error("[polygon] name search failed for '%s': %s", name, e)
    return None
