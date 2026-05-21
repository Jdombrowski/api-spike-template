"""
Pipeline orchestration tests — all 5 phases end-to-end.

Network calls are replaced with fixture data so these run offline.
The DB is redirected to a tmp_path so nothing touches data/db.sqlite.
"""

import json
from unittest.mock import MagicMock

import pytest

import src.pipeline as pipeline_mod
from src import config
from src.storage import db

# ── Shared fixture data ────────────────────────────────────────────────────

SAMPLE_CIK = "0001067983"

_EDGAR_RAW = {
    "cik": 1067983,
    "name": "BERKSHIRE HATHAWAY INC",
    "entityType": "operating",
    "sic": "6331",
    "sicDescription": "Fire, Marine & Casualty Insurance",
    "stateOfInc": "DE",
    "tickers": ["BRK-A", "BRK-B"],
    "exchanges": ["NYSE"],
    "fiscalYearEnd": "1231",
    "ein": "470813844",
    "facts": {},
}

_POLYGON_RAW = {
    "status": "OK",
    "results": {
        "ticker": "BRK-A",
        "name": "Berkshire Hathaway Inc.",
        "market": "stocks",
        "locale": "us",
        "primary_exchange": "XNYS",
        "type": "CS",
        "active": True,
        "currency_name": "usd",
        "cik": "1067983",
        "market_cap": 700_000_000_000,
    },
}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Redirect all file I/O to tmp_path and skip API key validation."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "validate", lambda: None)
    (tmp_path / "data" / "baselines").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    db.init_db()


# ── Happy path ─────────────────────────────────────────────────────────────


class TestPipelineHappyPath:
    def test_runs_without_error(self, monkeypatch):
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details", lambda ticker, save_sample=False: _POLYGON_RAW
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

    def test_saves_raw_responses_for_both_sources(self, monkeypatch):
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details", lambda ticker, save_sample=False: _POLYGON_RAW
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        with db._conn() as con:
            rows = con.execute("SELECT source FROM raw_responses").fetchall()
        sources = {r["source"] for r in rows}
        assert "edgar" in sources
        assert "polygon" in sources

    def test_saves_canonical_facts(self, monkeypatch):
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details", lambda ticker, save_sample=False: _POLYGON_RAW
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        with db._conn() as con:
            count = con.execute("SELECT COUNT(*) FROM canonical_facts").fetchone()[0]
        assert count >= 1

    def test_reconciliation_is_validated(self, monkeypatch):
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details", lambda ticker, save_sample=False: _POLYGON_RAW
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        with db._conn() as con:
            row = con.execute("SELECT overall_status FROM reconciliation").fetchone()
        assert row["overall_status"] == "VALIDATED"

    def test_creates_drift_baseline_on_first_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details", lambda ticker, save_sample=False: _POLYGON_RAW
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        baseline = tmp_path / "data" / "baselines" / "edgar_facts.json"
        assert baseline.exists()
        data = json.loads(baseline.read_text())
        assert "fields" in data


# ── EDGAR failure ──────────────────────────────────────────────────────────


class TestPipelineEdgarFailure:
    def test_edgar_404_skips_cik_without_crashing(self, monkeypatch):
        """A failed EDGAR fetch should log and skip — not abort the whole run."""
        monkeypatch.setattr(
            "src.pipeline.get_company_facts",
            MagicMock(side_effect=LookupError("404 not found")),
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        with db._conn() as con:
            count = con.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
        assert count == 0

    def test_multiple_ciks_edgar_partial_failure(self, monkeypatch):
        """Failure on one CIK should not prevent others from being processed."""
        second_cik = "0001364742"
        second_edgar = {**_EDGAR_RAW, "cik": 1364742, "name": "BLACKROCK INC", "tickers": ["BLK"]}
        second_polygon = {
            "status": "OK",
            "results": {**_POLYGON_RAW["results"], "ticker": "BLK", "cik": "1364742"},
        }

        call_count = {"n": 0}

        def edgar_sometimes_fails(cik, save_sample=False):
            call_count["n"] += 1
            if cik == SAMPLE_CIK:
                raise LookupError("404")
            return second_edgar

        monkeypatch.setattr("src.pipeline.get_company_facts", edgar_sometimes_fails)
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details", lambda ticker, save_sample=False: second_polygon
        )

        pipeline_mod.run([SAMPLE_CIK, second_cik], save_samples=False)

        with db._conn() as con:
            rows = con.execute(
                "SELECT entity_id FROM raw_responses WHERE source='edgar'"
            ).fetchall()
        assert any(r["entity_id"] == second_cik for r in rows)


# ── Missing Polygon data ───────────────────────────────────────────────────


class TestPipelineNoPolygon:
    def test_polygon_failure_still_saves_edgar_canonical(self, monkeypatch):
        """Polygon error must not prevent EDGAR canonical from being saved."""
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details",
            MagicMock(side_effect=PermissionError("403 auth error")),
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        with db._conn() as con:
            rows = con.execute(
                "SELECT source FROM canonical_facts WHERE source='edgar_companyfacts'"
            ).fetchall()
        assert len(rows) == 1

    def test_no_reconciliation_without_polygon(self, monkeypatch):
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: _EDGAR_RAW
        )
        monkeypatch.setattr(
            "src.pipeline.get_ticker_details",
            MagicMock(side_effect=PermissionError("403 auth error")),
        )
        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        with db._conn() as con:
            count = con.execute("SELECT COUNT(*) FROM reconciliation").fetchone()[0]
        assert count == 0

    def test_no_ticker_in_edgar_skips_polygon(self, monkeypatch):
        """If EDGAR returns no tickers and name search returns None, Polygon is skipped."""
        no_ticker_raw = {**_EDGAR_RAW, "tickers": []}
        monkeypatch.setattr(
            "src.pipeline.get_company_facts", lambda cik, save_sample=False: no_ticker_raw
        )
        monkeypatch.setattr("src.pipeline.search_ticker_by_name", lambda name: None)
        monkeypatch.setattr("src.pipeline.get_ticker_details", MagicMock())

        pipeline_mod.run([SAMPLE_CIK], save_samples=False)

        # get_ticker_details should never have been called
        pipeline_mod.get_ticker_details.assert_not_called()  # type: ignore[attr-defined]
