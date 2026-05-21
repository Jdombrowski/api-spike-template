"""
13F Holdings REST API.

Serves the holdings DB populated by `make run-bulk` and `make run-holdings`.
Auto-generated OpenAPI docs at http://localhost:8000/docs

    make serve          # start on port 8000 with hot-reload
"""
import logging
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.storage.db import (
    _conn,
    init_db,
    query_holdings,
    query_holdings_delta,
    query_holdings_summary,
    query_portfolio_timeline,
    query_security_holders,
)

log = logging.getLogger(__name__)

app = FastAPI(
    title="13F Holdings API",
    description=(
        "Institutional 13F holdings data sourced from SEC EDGAR. "
        "Populated by `make run-bulk` (8-quarter history) and "
        "`make run-holdings` (current quarter with Polygon cross-validation)."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


# ── Filers ─────────────────────────────────────────────────────────────────

@app.get(
    "/filers",
    summary="List tracked filers",
    response_description="One entry per CIK with aggregate position stats",
)
def list_filers() -> list[dict]:
    """
    Returns every CIK that has holdings in the database, with total
    position count, number of quarters covered, AUM estimate, and
    the most recent quarter-end date.
    """
    return query_holdings_summary()


@app.get(
    "/filers/{cik}/holdings",
    summary="Holdings for a filer",
    response_description="Positions sorted by reported value descending",
)
def get_holdings(
    cik: str,
    quarter: Annotated[str | None, Query(
        description="Quarter-end date (YYYY-MM-DD). Defaults to most recent.",
        example="2024-09-30",
    )] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[dict]:
    """
    Returns all positions held by a filer in a given quarter.
    If `quarter` is omitted, returns the most recently loaded quarter.
    """
    if quarter:
        with _conn() as con:
            rows = con.execute(
                "SELECT * FROM holdings WHERE cik=? AND as_of_date=? "
                "ORDER BY value_reported DESC LIMIT ?",
                (cik, quarter, limit),
            ).fetchall()
        result = [dict(r) for r in rows]
        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"No holdings for CIK {cik} in quarter {quarter}",
            )
        return result

    rows = query_holdings(cik=cik, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No holdings found for CIK {cik}")
    return rows


@app.get(
    "/filers/{cik}/timeline",
    summary="Quarter-by-quarter portfolio summary",
    response_description="AUM and position count per quarter, oldest first",
)
def get_timeline(cik: str) -> list[dict]:
    """
    Returns total reported value (USD millions) and position count for each
    quarter on record — useful for charting AUM over time.
    """
    rows = query_portfolio_timeline(cik)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No holdings found for CIK {cik}")
    return rows


@app.get(
    "/filers/{cik}/changes",
    summary="Quarter-over-quarter position changes",
    response_description="NEW, EXITED, INCREASED, and DECREASED positions only",
)
def get_changes(
    cik: str,
    from_quarter: Annotated[str, Query(
        description="Starting quarter-end date (YYYY-MM-DD)",
        example="2024-06-30",
    )],
    to_quarter: Annotated[str, Query(
        description="Ending quarter-end date (YYYY-MM-DD)",
        example="2024-09-30",
    )],
) -> list[dict]:
    """
    Returns positions that changed materially (>5%) between two quarters,
    plus new entries and full exits. UNCHANGED positions are excluded.
    """
    rows = query_holdings_delta(cik, from_quarter, to_quarter)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No changes found for CIK {cik} between {from_quarter} and {to_quarter}",
        )
    return rows


# ── Securities ─────────────────────────────────────────────────────────────

@app.get(
    "/securities/{cusip}/holders",
    summary="Who holds a given security",
    response_description="Filers holding this CUSIP, sorted by share count",
)
def get_holders(
    cusip: str,
    quarter: Annotated[str | None, Query(
        description="Quarter-end date (YYYY-MM-DD). Defaults to most recent available.",
        example="2024-09-30",
    )] = None,
) -> list[dict]:
    """
    Returns all filers that held a given CUSIP in a quarter, sorted by
    shares held descending. Useful for seeing concentrated ownership.
    """
    rows = query_security_holders(cusip=cusip.upper(), quarter=quarter)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No holders found for CUSIP {cusip}",
        )
    return rows


# ── Health ─────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}
