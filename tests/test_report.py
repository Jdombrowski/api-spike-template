"""
Tests for the report and export layer.

Uses the same isolated_env pattern as test_pipeline.py — tmp DB, no real files.
"""

import csv
import json

import pytest

from src import config
from src import report as report_mod
from src.storage import db
from src.storage.db import _conn


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    db.init_db()


# ── Seed helpers ───────────────────────────────────────────────────────────


def _seed_entity(
    cik="0001067983",
    name="BERKSHIRE HATHAWAY INC",
    entity_type="operating",
    ticker="BRK-A",
    sic_code="6331",
):
    """Insert a minimal set of rows that a real pipeline run would produce."""
    mapped = {
        "_source": "edgar_companyfacts",
        "cik": cik,
        "entity_name": name,
        "entity_type": entity_type,
        "ticker": ticker,
        "sic_code": sic_code,
        "_mapper_assumptions": [
            "'fiscalYearEnd' → 'fiscal_year_end_mmdd': mapping is assumed, not confirmed"
        ],
        "_unmapped_fields": [],
    }
    with _conn() as con:
        raw_id = con.execute(
            "INSERT INTO raw_responses (source, endpoint, entity_id, response_json) VALUES (?,?,?,?)",
            ("edgar", "companyfacts", cik, json.dumps({"name": name})),
        ).lastrowid
        con.execute(
            "INSERT INTO canonical_facts "
            "(source, entity_name, cik, ticker, sic_code, entity_type, mapped_json, raw_response_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "edgar_companyfacts",
                name,
                cik,
                ticker,
                sic_code,
                entity_type,
                json.dumps(mapped),
                raw_id,
            ),
        )
        return raw_id


def _seed_reconciliation(cik="0001067983", status="VALIDATED"):
    checks = {
        "cik_match": {
            "edgar": cik.lstrip("0"),
            "polygon": cik.lstrip("0"),
            "status": "MATCH",
            "note": "",
        },
        "name_match": {
            "edgar": "BERKSHIRE",
            "polygon": "BERKSHIRE",
            "status": "SIMILAR",
            "note": "",
        },
    }
    with _conn() as con:
        con.execute(
            "INSERT INTO reconciliation (entity_id, overall_status, checks_json) VALUES (?,?,?)",
            (cik, status, json.dumps(checks)),
        )


def _seed_drift(raw_id: int | None, severity="WARNING", field="ein", description="unexpected null"):
    with _conn() as con:
        con.execute(
            "INSERT INTO drift_events (source, severity, field, description, raw_response_id) VALUES (?,?,?,?,?)",
            ("edgar", severity, field, description, raw_id),
        )


# ── query_ingestion_summary ────────────────────────────────────────────────


class TestQueryIngestionSummary:
    def test_empty_db_returns_zero_entities(self):
        s = report_mod.query_ingestion_summary()
        assert s["entity_count"] == 0
        assert s["last_run"] is None

    def test_counts_distinct_entities(self):
        _seed_entity("0001067983")
        _seed_entity("0001364742", name="BLACKROCK INC", ticker="BLK")
        s = report_mod.query_ingestion_summary()
        assert s["entity_count"] == 2
        assert "edgar" in s["sources"]


# ── query_entities ─────────────────────────────────────────────────────────


class TestQueryEntities:
    def test_returns_empty_list_with_no_data(self):
        assert report_mod.query_entities() == []

    def test_returns_seeded_entity(self):
        _seed_entity()
        rows = report_mod.query_entities()
        assert len(rows) == 1
        assert rows[0]["entity_name"] == "BERKSHIRE HATHAWAY INC"
        assert rows[0]["cik"] == "0001067983"

    def test_includes_reconciliation_status_when_present(self):
        _seed_entity()
        _seed_reconciliation(status="VALIDATED")
        rows = report_mod.query_entities()
        assert rows[0]["overall_status"] == "VALIDATED"

    def test_overall_status_is_none_without_polygon(self):
        _seed_entity()
        rows = report_mod.query_entities()
        assert rows[0]["overall_status"] is None


# ── query_reconciliation_detail ────────────────────────────────────────────


class TestQueryReconciliationDetail:
    def test_empty_when_no_reconciliation(self):
        assert report_mod.query_reconciliation_detail() == []

    def test_parses_checks_json(self):
        _seed_entity()
        _seed_reconciliation()
        recs = report_mod.query_reconciliation_detail()
        assert len(recs) == 1
        assert isinstance(recs[0]["checks"], dict)
        assert "cik_match" in recs[0]["checks"]

    def test_check_has_status_field(self):
        _seed_entity()
        _seed_reconciliation(status="NEEDS_REVIEW")
        rec = report_mod.query_reconciliation_detail()[0]
        assert rec["overall_status"] == "NEEDS_REVIEW"
        for check in rec["checks"].values():
            assert "status" in check


# ── query_drift_by_entity ──────────────────────────────────────────────────


class TestQueryDriftByEntity:
    def test_empty_when_no_drift(self):
        assert report_mod.query_drift_by_entity() == {}

    def test_groups_by_cik(self):
        raw_id = _seed_entity()
        _seed_drift(raw_id, severity="WARNING", field="ein")
        _seed_drift(raw_id, severity="ERROR", field="cik")

        drift = report_mod.query_drift_by_entity()
        assert "0001067983" in drift
        fields = {d["field"] for d in drift["0001067983"]}
        assert fields == {"ein", "cik"}

    def test_multiple_entities_separated(self):
        raw_a = _seed_entity("0001067983")
        raw_b = _seed_entity("0001364742", name="BLACKROCK INC", ticker="BLK")
        _seed_drift(raw_a, field="ein")
        _seed_drift(raw_b, field="sic")

        drift = report_mod.query_drift_by_entity()
        assert "0001067983" in drift
        assert "0001364742" in drift


# ── query_open_assumptions ─────────────────────────────────────────────────


class TestQueryOpenAssumptions:
    def test_empty_when_no_data(self):
        assert report_mod.query_open_assumptions() == []

    def test_extracts_assumptions_from_mapped_json(self):
        _seed_entity()
        assumptions = report_mod.query_open_assumptions()
        assert len(assumptions) >= 1
        assert any("fiscalYearEnd" in a["assumption"] for a in assumptions)

    def test_deduplicates_across_entities(self):
        _seed_entity("0001067983")
        _seed_entity("0001364742", name="BLACKROCK INC", ticker="BLK")
        assumptions = report_mod.query_open_assumptions()
        texts = [a["assumption"] for a in assumptions]
        assert len(texts) == len(set(texts))


# ── report() (smoke test) ──────────────────────────────────────────────────


class TestReport:
    def test_no_crash_on_empty_db(self):
        report_mod.report()

    def test_no_crash_with_full_data(self):
        raw_id = _seed_entity()
        _seed_reconciliation()
        _seed_drift(raw_id)
        report_mod.report()


# ── export() ──────────────────────────────────────────────────────────────


class TestExport:
    def test_creates_three_csv_files(self, tmp_path):
        _seed_entity()
        _seed_reconciliation()
        out = tmp_path / "exports"
        report_mod.export(out_dir=out)
        csvs = list(out.glob("*.csv"))
        names = {f.name.split("__")[0] for f in csvs}
        assert names == {"canonical_facts", "reconciliation", "drift_events"}

    def test_canonical_csv_has_expected_columns(self, tmp_path):
        _seed_entity()
        out = tmp_path / "exports"
        report_mod.export(out_dir=out)
        canonical = next(out.glob("canonical_facts__*.csv"))
        reader = csv.DictReader(canonical.open())
        assert set(reader.fieldnames or []) >= {"cik", "entity_name", "entity_type", "ticker"}

    def test_reconciliation_csv_flattens_checks(self, tmp_path):
        _seed_entity()
        _seed_reconciliation()
        out = tmp_path / "exports"
        report_mod.export(out_dir=out)
        recon_csv = next(out.glob("reconciliation__*.csv"))
        rows = list(csv.DictReader(recon_csv.open()))
        assert len(rows) == 2  # two checks: cik_match + name_match
        assert all("check" in r for r in rows)

    def test_export_with_no_data_writes_empty_csvs(self, tmp_path):
        out = tmp_path / "exports"
        report_mod.export(out_dir=out)
        for csv_file in out.glob("*.csv"):
            rows = list(csv.DictReader(csv_file.open()))
            assert rows == []
