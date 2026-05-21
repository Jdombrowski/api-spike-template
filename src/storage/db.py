"""
SQLite storage layer.

Intentionally minimal — the schema mirrors what you'd run on Postgres at Atomic.
Swap sqlite3 for psycopg2 + connection pool and the rest of the code is unchanged.

Tables:
  raw_responses    — immutable log of every API response (the "event log" pattern)
  canonical_facts  — normalized, mapped records (derived from raw)
  drift_events     — schema drift alerts (the audit trail for data quality)
  reconciliation   — cross-source match results
"""

import json
import logging
import sqlite3
from contextlib import contextmanager

from src import config

log = logging.getLogger(__name__)


@contextmanager
def _conn():
    """Context manager for SQLite connections — mirrors how you'd use a PG connection pool."""
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row  # dict-like rows
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _conn() as con:
        con.executescript("""
            -- Immutable log of raw API responses
            -- Append-only: never update or delete rows here
            -- Same pattern as payment_events in a financial pipeline
            CREATE TABLE IF NOT EXISTS raw_responses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT    NOT NULL,          -- "edgar" | "polygon"
                endpoint     TEXT    NOT NULL,          -- which endpoint
                entity_id    TEXT,                      -- CIK, ticker, etc.
                response_json TEXT   NOT NULL,          -- full raw response
                ingested_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Canonical records derived from raw responses
            -- Rebuilt from raw_responses on schema changes (never lose raw)
            CREATE TABLE IF NOT EXISTS canonical_facts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source         TEXT    NOT NULL,
                entity_name    TEXT,
                cik            TEXT,
                ticker         TEXT,
                sic_code       TEXT,
                entity_type    TEXT,
                mapped_json    TEXT    NOT NULL,        -- full canonical record
                raw_response_id INTEGER REFERENCES raw_responses(id),
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Drift alerts — every schema deviation gets logged
            CREATE TABLE IF NOT EXISTS drift_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT    NOT NULL,
                severity    TEXT    NOT NULL,           -- ERROR | WARNING | INFO
                field       TEXT    NOT NULL,
                description TEXT    NOT NULL,
                raw_response_id INTEGER REFERENCES raw_responses(id),
                detected_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Cross-source reconciliation results
            CREATE TABLE IF NOT EXISTS reconciliation (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id       TEXT    NOT NULL,
                overall_status  TEXT    NOT NULL,       -- VALIDATED | NEEDS_REVIEW
                checks_json     TEXT    NOT NULL,
                edgar_raw_id    INTEGER REFERENCES raw_responses(id),
                polygon_raw_id  INTEGER REFERENCES raw_responses(id),
                reconciled_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- 13F positions parsed from InfoTable XML or DERA bulk TSV
            -- value_reported unit varies by filing era — check value_unit column
            CREATE TABLE IF NOT EXISTS holdings (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                cik                 TEXT    NOT NULL,
                accession_number    TEXT,
                form_type           TEXT,
                filing_date         TEXT,
                as_of_date          TEXT,               -- authoritative quarter-end date
                issuer_name         TEXT,
                cusip               TEXT,
                ticker              TEXT,
                shares_held         REAL,
                value_reported      REAL,
                value_unit          TEXT    NOT NULL DEFAULT 'USD_THOUSANDS',
                price_at_filing     REAL,
                value_estimated     REAL,
                validation_ratio    REAL,
                validation_status   TEXT,               -- CLOSE | DIVERGENT | NO_PRICE
                raw_response_id     INTEGER REFERENCES raw_responses(id),
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),

                -- Schema-level constraint: one position per (filer, filing, security).
                -- Rare PRN+SH duplicates on the same CUSIP within one filing are silently
                -- dropped on re-ingest — acceptable for portfolio-level analysis.
                UNIQUE(cik, accession_number, cusip)
            );

            -- Quarter-over-quarter delta and timeline queries
            CREATE INDEX IF NOT EXISTS idx_holdings_cik_quarter
                ON holdings(cik, as_of_date);

            -- Cross-filer "who holds this security" queries
            CREATE INDEX IF NOT EXISTS idx_holdings_cusip
                ON holdings(cusip);
        """)
    log.info("[storage] database initialized at %s", config.DB_PATH)


def save_raw(source: str, endpoint: str, entity_id: str | None, response: dict) -> int:
    """Persist a raw API response. Returns the row ID."""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO raw_responses (source, endpoint, entity_id, response_json) VALUES (?,?,?,?)",
            (source, endpoint, entity_id, json.dumps(response)),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


def save_canonical(canonical: dict, raw_id: int | None = None):
    """Persist a canonical (mapped) record."""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO canonical_facts
              (source, entity_name, cik, ticker, sic_code, entity_type, mapped_json, raw_response_id)
            VALUES (?,?,?,?,?,?,?,?)
        """,
            (
                canonical.get("_source"),
                canonical.get("entity_name"),
                str(canonical.get("cik", "")) or None,
                canonical.get("ticker"),
                str(canonical.get("sic_code", "")) or None,
                canonical.get("entity_type"),
                json.dumps(canonical),
                raw_id,
            ),
        )


def save_drift_events(source: str, issues: list, raw_id: int | None = None):
    """Log drift detection results."""
    if not issues:
        return
    with _conn() as con:
        con.executemany(
            """
            INSERT INTO drift_events (source, severity, field, description, raw_response_id)
            VALUES (?,?,?,?,?)
        """,
            [(source, i.severity, i.field, i.description, raw_id) for i in issues],
        )
    log.warning("[storage] logged %d drift event(s) from %s", len(issues), source)


def save_reconciliation(
    entity_id: str, result: dict, edgar_raw_id: int | None, polygon_raw_id: int | None
):
    with _conn() as con:
        con.execute(
            """
            INSERT INTO reconciliation
              (entity_id, overall_status, checks_json, edgar_raw_id, polygon_raw_id)
            VALUES (?,?,?,?,?)
        """,
            (
                entity_id,
                result["overall"],
                json.dumps(result["checks"]),
                edgar_raw_id,
                polygon_raw_id,
            ),
        )


def query_drift_summary() -> list[dict]:
    """Return drift event counts by severity — useful for a quality dashboard."""
    with _conn() as con:
        rows = con.execute("""
            SELECT source, severity, COUNT(*) as count
            FROM drift_events
            GROUP BY source, severity
            ORDER BY source, severity
        """).fetchall()
        return [dict(r) for r in rows]


def query_reconciliation_summary() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT overall_status, COUNT(*) as count
            FROM reconciliation
            GROUP BY overall_status
        """).fetchall()
        return [dict(r) for r in rows]


def save_holdings(holdings: list[dict], raw_id: int | None = None) -> None:
    """Persist a batch of parsed + cross-validated holdings."""
    if not holdings:
        return
    with _conn() as con:
        con.executemany(
            """
            INSERT INTO holdings
              (cik, accession_number, form_type, filing_date, as_of_date,
               issuer_name, cusip, ticker, shares_held, value_reported, value_unit,
               price_at_filing, value_estimated, validation_ratio, validation_status,
               raw_response_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            [
                (
                    h.get("cik"),
                    h.get("accession_number"),
                    h.get("form_type"),
                    h.get("filing_date"),
                    h.get("as_of_date"),
                    h.get("issuer_name"),
                    h.get("cusip"),
                    h.get("ticker"),
                    h.get("shares_held"),
                    h.get("value_reported"),
                    h.get("value_unit"),
                    h.get("price_at_filing"),
                    h.get("value_estimated"),
                    h.get("validation_ratio"),
                    h.get("validation_status"),
                    raw_id,
                )
                for h in holdings
            ],
        )
    log.info("[storage] saved %d holdings", len(holdings))


def query_holdings_summary() -> list[dict]:
    """Per-filer totals and cross-validation hit rate."""
    with _conn() as con:
        rows = con.execute("""
            SELECT
                cik,
                COUNT(*)                                                      AS position_count,
                COUNT(DISTINCT as_of_date)                                    AS quarter_count,
                SUM(CASE WHEN validation_status = 'CLOSE' THEN 1 ELSE 0 END) AS validated_count,
                ROUND(SUM(
                    CASE WHEN value_unit = 'USD' THEN value_reported
                         ELSE value_reported * 1000.0 END
                ) / 1000000.0, 1)                                             AS total_value_millions,
                MAX(as_of_date)                                               AS latest_filing
            FROM holdings
            GROUP BY cik
            ORDER BY total_value_millions DESC
        """).fetchall()
    return [dict(r) for r in rows]


def query_holdings(cik: str | None = None, limit: int = 100) -> list[dict]:
    """Return holdings rows sorted by reported value, optionally filtered by CIK."""
    with _conn() as con:
        if cik:
            rows = con.execute(
                "SELECT * FROM holdings WHERE cik = ? ORDER BY value_reported DESC LIMIT ?",
                (cik, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM holdings ORDER BY cik, value_reported DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def query_holdings_delta(cik: str, from_quarter: str, to_quarter: str) -> list[dict]:
    """
    Quarter-over-quarter position changes for a filer.

    Returns NEW, EXITED, INCREASED (>5%), and DECREASED (>5%) positions.
    UNCHANGED positions are excluded — callers get signal, not noise.
    """
    with _conn() as con:
        rows = con.execute(
            """
            WITH
              from_q AS (
                SELECT cusip, issuer_name, shares_held, value_reported, value_unit
                FROM holdings WHERE cik = ? AND as_of_date = ?
              ),
              to_q AS (
                SELECT cusip, issuer_name, shares_held, value_reported, value_unit
                FROM holdings WHERE cik = ? AND as_of_date = ?
              ),
              combined AS (
                SELECT
                  COALESCE(t.cusip, f.cusip)             AS cusip,
                  COALESCE(t.issuer_name, f.issuer_name) AS issuer_name,
                  f.shares_held   AS shares_prev,
                  t.shares_held   AS shares_curr,
                  f.value_reported AS value_prev,
                  t.value_reported AS value_curr,
                  COALESCE(t.value_unit, f.value_unit)   AS value_unit
                FROM from_q f LEFT JOIN to_q t ON t.cusip = f.cusip
                UNION ALL
                SELECT
                  t.cusip, t.issuer_name,
                  NULL, t.shares_held, NULL, t.value_reported, t.value_unit
                FROM to_q t LEFT JOIN from_q f ON f.cusip = t.cusip
                WHERE f.cusip IS NULL
              )
            SELECT
              cusip, issuer_name,
              shares_prev, shares_curr,
              value_prev, value_curr, value_unit,
              CASE
                WHEN shares_prev IS NULL                              THEN 'NEW'
                WHEN shares_curr IS NULL                             THEN 'EXITED'
                WHEN shares_curr > shares_prev * 1.05               THEN 'INCREASED'
                ELSE                                                      'DECREASED'
              END AS change_type
            FROM combined
            WHERE shares_prev IS NULL
               OR shares_curr IS NULL
               OR ABS(shares_curr - shares_prev) / shares_prev > 0.05
            ORDER BY COALESCE(value_curr, 0) DESC
        """,
            (cik, from_quarter, cik, to_quarter),
        ).fetchall()
    return [dict(r) for r in rows]


def query_portfolio_timeline(cik: str) -> list[dict]:
    """Quarter-by-quarter total value and position count for a filer."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT
                as_of_date,
                COUNT(*)  AS position_count,
                ROUND(SUM(
                    CASE WHEN value_unit = 'USD' THEN value_reported
                         ELSE value_reported * 1000.0 END
                ) / 1000000.0, 1) AS total_value_millions
            FROM holdings
            WHERE cik = ?
            GROUP BY as_of_date
            ORDER BY as_of_date
        """,
            (cik,),
        ).fetchall()
    return [dict(r) for r in rows]


def query_security_holders(cusip: str, quarter: str | None = None) -> list[dict]:
    """
    Which filers held a given CUSIP in a given quarter.
    Defaults to the most recent quarter that security appears in.
    """
    with _conn() as con:
        effective_quarter = (
            quarter
            or con.execute(
                "SELECT MAX(as_of_date) FROM holdings WHERE cusip = ?", (cusip,)
            ).fetchone()[0]
        )

        if not effective_quarter:
            return []

        rows = con.execute(
            """
            SELECT cik, issuer_name, shares_held, value_reported, value_unit,
                   as_of_date, validation_status
            FROM holdings
            WHERE cusip = ? AND as_of_date = ?
            ORDER BY shares_held DESC
        """,
            (cusip, effective_quarter),
        ).fetchall()
    return [dict(r) for r in rows]
