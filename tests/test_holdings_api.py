"""
Tests for the 13F Holdings REST API (holdings_api.py).

Uses FastAPI TestClient backed by an isolated tmp DB — no network calls,
no real DERA data. Covers happy paths, 404s, query-param filtering,
and the /health check.
"""

import pytest
from fastapi.testclient import TestClient

from src import config
from src.holdings_api import app
from src.storage import db
from src.storage.db import save_holdings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "validate", lambda: None)
    db.init_db()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── Seed helper ────────────────────────────────────────────────────────────


def _holding(
    cik="0001067983",
    cusip="037833100",
    issuer="APPLE INC",
    as_of_date="2024-09-30",
    accession="ACC-2024Q3",
    shares=300_000_000.0,
    value=69_000_000.0,
):
    return {
        "cik": cik,
        "accession_number": accession,
        "form_type": "13F-HR",
        "filing_date": as_of_date,
        "as_of_date": as_of_date,
        "issuer_name": issuer,
        "cusip": cusip,
        "ticker": None,
        "shares_held": shares,
        "value_reported": value,
        "value_unit": "USD_THOUSANDS",
        "price_at_filing": None,
        "value_estimated": None,
        "validation_ratio": None,
        "validation_status": None,
    }


# ── /health ────────────────────────────────────────────────────────────────


class TestHealth:
    def test_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ── /filers ────────────────────────────────────────────────────────────────


class TestListFilers:
    def test_empty_db_returns_empty_list(self, client):
        r = client.get("/filers")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_one_entry_per_cik(self, client):
        save_holdings([_holding("CIK1"), _holding("CIK2")])
        r = client.get("/filers")
        assert r.status_code == 200
        ciks = {row["cik"] for row in r.json()}
        assert ciks == {"CIK1", "CIK2"}

    def test_position_count_is_correct(self, client):
        save_holdings(
            [
                _holding(cusip="CUSIP1"),
                _holding(cusip="CUSIP2", accession="ACC-2"),
            ]
        )
        r = client.get("/filers")
        assert r.json()[0]["position_count"] == 2

    def test_sorted_by_value_descending(self, client):
        save_holdings([_holding("SMALL", value=1_000.0)])
        save_holdings([_holding("BIG", value=999_000_000.0)])
        r = client.get("/filers")
        ciks = [row["cik"] for row in r.json()]
        assert ciks[0] == "BIG"


# ── /filers/{cik}/holdings ─────────────────────────────────────────────────


class TestGetHoldings:
    def test_returns_holdings_for_cik(self, client):
        save_holdings([_holding()])
        r = client.get("/filers/0001067983/holdings")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_404_for_unknown_cik(self, client):
        r = client.get("/filers/UNKNOWN/holdings")
        assert r.status_code == 404

    def test_quarter_filter_returns_matching_rows(self, client):
        save_holdings(
            [
                _holding(as_of_date="2024-06-30", accession="ACC-Q2"),
                _holding(as_of_date="2024-09-30", accession="ACC-Q3"),
            ]
        )
        r = client.get("/filers/0001067983/holdings?quarter=2024-06-30")
        assert r.status_code == 200
        assert all(row["as_of_date"] == "2024-06-30" for row in r.json())

    def test_quarter_filter_404_for_missing_quarter(self, client):
        save_holdings([_holding(as_of_date="2024-09-30")])
        r = client.get("/filers/0001067983/holdings?quarter=2020-03-31")
        assert r.status_code == 404

    def test_limit_param_restricts_rows(self, client):
        save_holdings([_holding(cusip=f"CUSIP{i}", accession=f"ACC-{i}") for i in range(10)])
        r = client.get("/filers/0001067983/holdings?limit=3")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_sorted_by_value_descending(self, client):
        save_holdings(
            [
                _holding(cusip="LOW", value=1_000.0, accession="ACC-1"),
                _holding(cusip="HIGH", value=500_000.0, accession="ACC-2"),
            ]
        )
        r = client.get("/filers/0001067983/holdings")
        rows = r.json()
        assert rows[0]["cusip"] == "HIGH"


# ── /filers/{cik}/timeline ─────────────────────────────────────────────────


class TestGetTimeline:
    def test_returns_one_row_per_quarter(self, client):
        save_holdings(
            [
                _holding(as_of_date="2024-03-31", accession="Q1"),
                _holding(as_of_date="2024-06-30", accession="Q2"),
                _holding(as_of_date="2024-09-30", accession="Q3"),
            ]
        )
        r = client.get("/filers/0001067983/timeline")
        assert r.status_code == 200
        assert len(r.json()) == 3

    def test_ordered_oldest_first(self, client):
        save_holdings(
            [
                _holding(as_of_date="2024-09-30", accession="Q3"),
                _holding(as_of_date="2024-03-31", accession="Q1"),
            ]
        )
        r = client.get("/filers/0001067983/timeline")
        dates = [row["as_of_date"] for row in r.json()]
        assert dates == sorted(dates)

    def test_404_for_unknown_cik(self, client):
        r = client.get("/filers/UNKNOWN/timeline")
        assert r.status_code == 404


# ── /filers/{cik}/changes ──────────────────────────────────────────────────


class TestGetChanges:
    def test_new_position_appears_in_changes(self, client):
        save_holdings([_holding(cusip="OLD", as_of_date="2024-06-30", accession="Q2")])
        save_holdings(
            [
                _holding(cusip="OLD", as_of_date="2024-09-30", accession="Q3"),
                _holding(cusip="NEW", as_of_date="2024-09-30", accession="Q3"),
            ]
        )
        r = client.get("/filers/0001067983/changes?from_quarter=2024-06-30&to_quarter=2024-09-30")
        assert r.status_code == 200
        change_types = {row["change_type"] for row in r.json()}
        assert "NEW" in change_types

    def test_404_when_no_changes(self, client):
        r = client.get("/filers/UNKNOWN/changes?from_quarter=2024-06-30&to_quarter=2024-09-30")
        assert r.status_code == 404

    def test_requires_both_quarter_params(self, client):
        r = client.get("/filers/0001067983/changes?from_quarter=2024-06-30")
        assert r.status_code == 422


# ── /securities/{cusip}/holders ────────────────────────────────────────────


class TestGetHolders:
    def test_returns_all_holders_for_cusip(self, client):
        save_holdings(
            [
                _holding(cik="CIK1", cusip="CUSIP1", accession="A1"),
                _holding(cik="CIK2", cusip="CUSIP1", accession="A2"),
            ]
        )
        r = client.get("/securities/CUSIP1/holders")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_404_for_unknown_cusip(self, client):
        r = client.get("/securities/UNKNOWN/holders")
        assert r.status_code == 404

    def test_cusip_lookup_is_case_insensitive(self, client):
        save_holdings([_holding(cusip="037833100")])
        r = client.get("/securities/037833100/holders")
        assert r.status_code == 200

    def test_quarter_filter_returns_correct_rows(self, client):
        save_holdings(
            [
                _holding(cik="CIK1", cusip="CUSIP1", as_of_date="2024-06-30", accession="A1"),
                _holding(cik="CIK1", cusip="CUSIP1", as_of_date="2024-09-30", accession="A2"),
            ]
        )
        r = client.get("/securities/CUSIP1/holders?quarter=2024-06-30")
        assert r.status_code == 200
        assert all(row["as_of_date"] == "2024-06-30" for row in r.json())

    def test_defaults_to_most_recent_quarter(self, client):
        save_holdings(
            [
                _holding(cik="CIK1", cusip="CUSIP1", as_of_date="2024-06-30", accession="A1"),
                _holding(cik="CIK1", cusip="CUSIP1", as_of_date="2024-09-30", accession="A2"),
            ]
        )
        r = client.get("/securities/CUSIP1/holders")
        assert r.json()[0]["as_of_date"] == "2024-09-30"

    def test_sorted_by_shares_descending(self, client):
        save_holdings(
            [
                _holding(cik="SMALL", cusip="CUSIP1", shares=100.0, accession="A1"),
                _holding(cik="BIG", cusip="CUSIP1", shares=999_999.0, accession="A2"),
            ]
        )
        r = client.get("/securities/CUSIP1/holders")
        rows = r.json()
        assert rows[0]["cik"] == "BIG"
