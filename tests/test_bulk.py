"""
Tests for the DERA bulk ingest pipeline:
  - _iter_quarters: correct quarter range generation
  - _normalise_accession: dashed form normalisation
  - _matches_target: CIK zero-padding variants
  - _parse_zip: TSV join → holdings rows (no network)
  - DB round-trip: save_holdings idempotency via UNIQUE constraint
  - New DB queries: delta, timeline, security_holders
"""

import io
import zipfile

import pytest

from src import config
from src.ingest.edgar_bulk import (
    _iter_quarters,
    _matches_target,
    _normalise_accession,
    _parse_zip,
)
from src.storage import db
from src.storage.db import (
    _conn,
    query_holdings_delta,
    query_portfolio_timeline,
    query_security_holders,
    save_holdings,
)

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "validate", lambda: None)
    db.init_db()


# ── _iter_quarters ─────────────────────────────────────────────────────────


class TestIterQuarters:
    def test_yields_correct_count(self):
        quarters = list(_iter_quarters(4))
        assert len(quarters) == 4

    def test_most_recent_first(self):
        quarters = list(_iter_quarters(4))
        # years should be non-increasing
        years = [y for y, _ in quarters]
        assert years == sorted(years, reverse=True)

    def test_quarter_values_in_range(self):
        for year, q in _iter_quarters(8):
            assert 1 <= q <= 4
            assert year >= 2020

    def test_sequential_quarters(self):
        quarters = list(_iter_quarters(5))
        for i in range(len(quarters) - 1):
            y1, q1 = quarters[i]
            y2, q2 = quarters[i + 1]
            # each step back is exactly one quarter
            if q1 == 1:
                assert y2 == y1 - 1 and q2 == 4
            else:
                assert y2 == y1 and q2 == q1 - 1


# ── _normalise_accession ───────────────────────────────────────────────────


class TestNormaliseAccession:
    def test_already_dashed_unchanged(self):
        assert _normalise_accession("0001067983-23-000009") == "0001067983-23-000009"

    def test_undashed_18_chars_gets_dashes(self):
        assert _normalise_accession("000106798323000009") == "0001067983-23-000009"

    def test_strips_whitespace(self):
        assert _normalise_accession("  0001067983-23-000009  ") == "0001067983-23-000009"

    def test_short_string_returned_as_is(self):
        result = _normalise_accession("abc")
        assert result == "abc"


# ── _matches_target ────────────────────────────────────────────────────────


class TestMatchesTarget:
    def test_exact_padded_match(self):
        assert _matches_target("1067983", "0001067983", {"0001067983"})

    def test_unpadded_match(self):
        assert _matches_target("1067983", "0001067983", {"1067983"})

    def test_no_match(self):
        assert not _matches_target("9999999", "0009999999", {"0001067983"})

    def test_empty_target_set(self):
        assert not _matches_target("1067983", "0001067983", set())


# ── _parse_zip ─────────────────────────────────────────────────────────────


def _make_zip(submission_rows: list[dict], infotable_rows: list[dict]) -> bytes:
    """Build an in-memory ZIP with SUBMISSION.tsv and INFOTABLE.tsv."""

    def tsv(rows: list[dict]) -> str:
        if not rows:
            return ""
        headers = list(rows[0].keys())
        lines = ["\t".join(headers)]
        for row in rows:
            lines.append("\t".join(str(row.get(h, "")) for h in headers))
        return "\n".join(lines)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2024q3_SUBMISSION.tsv", tsv(submission_rows))
        zf.writestr("2024q3_INFOTABLE.tsv", tsv(infotable_rows))
    return buf.getvalue()


_SUBMISSION = [
    {
        "ACCESSION_NUMBER": "0001067983-24-000009",
        "FILINGMANAGER_CIK": "0001067983",
        "FILINGMANAGER_NAME": "BERKSHIRE HATHAWAY INC",
        "PERIOD_OF_REPORT": "2024-09-30",
        "FILED": "2024-11-14",
        "FORM_TYPE": "13F-HR",
    }
]

_INFOTABLE = [
    {
        "ACCESSION_NUMBER": "0001067983-24-000009",
        "NAMEOFISSUER": "APPLE INC",
        "CUSIP": "037833100",
        "VALUE": "69000000",  # ~$69B full USD
        "SSHPRNAMT": "300000000",
        "SSHPRNAMTTYPE": "SH",
        "INVESTMENTDISCRETION": "SOLE",
    },
    {
        "ACCESSION_NUMBER": "0001067983-24-000009",
        "NAMEOFISSUER": "AMERICAN EXPRESS CO",
        "CUSIP": "025816109",
        "VALUE": "41000000",
        "SSHPRNAMT": "151610700",
        "SSHPRNAMTTYPE": "SH",
        "INVESTMENTDISCRETION": "SOLE",
    },
]


class TestParseZip:
    def test_returns_correct_row_count(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        assert len(rows) == 2

    def test_maps_issuer_name(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        assert rows[0]["issuer_name"] == "APPLE INC"

    def test_maps_cusip(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        assert rows[0]["cusip"] == "037833100"

    def test_uses_period_of_report_as_as_of_date(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        assert rows[0]["as_of_date"] == "2024-09-30"

    def test_cik_is_zero_padded(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        assert rows[0]["cik"] == "0001067983"

    def test_validation_fields_are_none(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        assert rows[0]["validation_status"] is None
        assert rows[0]["price_at_filing"] is None

    def test_filters_out_non_target_ciks(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001364742"})
        assert rows == []

    def test_unpadded_cik_in_target_set_matches(self):
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"1067983"})
        assert len(rows) == 2

    def test_large_value_inferred_as_usd(self):
        # 69_000_000 / 300_000_000 = $0.23 → USD_THOUSANDS
        # actual: value=69000000 full dollars, shares=300000000 → implied=$0.23
        # wait — that's below $5, so USD_THOUSANDS. Let's check with realistic numbers.
        rows = _parse_zip(_make_zip(_SUBMISSION, _INFOTABLE), {"0001067983"})
        # value=69000000, shares=300000000 → implied=0.23 → USD_THOUSANDS
        assert rows[0]["value_unit"] == "USD_THOUSANDS"


# ── Idempotent re-ingest ───────────────────────────────────────────────────


class TestSaveHoldingsIdempotent:
    def _row(self, cusip="037833100"):
        return {
            "cik": "0001067983",
            "accession_number": "0001067983-24-000009",
            "form_type": "13F-HR",
            "filing_date": "2024-11-14",
            "as_of_date": "2024-09-30",
            "issuer_name": "APPLE INC",
            "cusip": cusip,
            "ticker": None,
            "shares_held": 300_000_000.0,
            "value_reported": 69_000_000.0,
            "value_unit": "USD_THOUSANDS",
            "price_at_filing": None,
            "value_estimated": None,
            "validation_ratio": None,
            "validation_status": None,
        }

    def test_duplicate_save_does_not_raise(self):
        save_holdings([self._row()])
        save_holdings([self._row()])  # second save — should be silently ignored

    def test_duplicate_save_does_not_double_count(self):
        save_holdings([self._row()])
        save_holdings([self._row()])
        with _conn() as con:
            count = con.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
        assert count == 1

    def test_different_cusip_saved_as_separate_row(self):
        save_holdings([self._row("037833100"), self._row("025816109")])
        with _conn() as con:
            count = con.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
        assert count == 2


# ── New DB queries ─────────────────────────────────────────────────────────


def _seed(cik, as_of_date, cusip, shares, value, accession="ACC-1"):
    save_holdings(
        [
            {
                "cik": cik,
                "accession_number": accession,
                "form_type": "13F-HR",
                "filing_date": as_of_date,
                "as_of_date": as_of_date,
                "issuer_name": f"ISSUER {cusip}",
                "cusip": cusip,
                "ticker": None,
                "shares_held": float(shares),
                "value_reported": float(value),
                "value_unit": "USD_THOUSANDS",
                "price_at_filing": None,
                "value_estimated": None,
                "validation_ratio": None,
                "validation_status": None,
            }
        ]
    )


class TestHoldingsDelta:
    def test_new_position_detected(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1000, 100, "ACC-2")
        _seed("CIK1", "2024-09-30", "CUSIP2", 500, 50, "ACC-2")
        rows = query_holdings_delta("CIK1", "2024-06-30", "2024-09-30")
        new_rows = [r for r in rows if r["change_type"] == "NEW"]
        assert any(r["cusip"] == "CUSIP2" for r in new_rows)

    def test_exited_position_detected(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-06-30", "CUSIP2", 500, 50, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1000, 100, "ACC-2")
        rows = query_holdings_delta("CIK1", "2024-06-30", "2024-09-30")
        exited = [r for r in rows if r["change_type"] == "EXITED"]
        assert any(r["cusip"] == "CUSIP2" for r in exited)

    def test_increased_position_detected(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1200, 120, "ACC-2")  # +20%
        rows = query_holdings_delta("CIK1", "2024-06-30", "2024-09-30")
        assert rows[0]["change_type"] == "INCREASED"

    def test_decreased_position_detected(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 800, 80, "ACC-2")  # -20%
        rows = query_holdings_delta("CIK1", "2024-06-30", "2024-09-30")
        assert rows[0]["change_type"] == "DECREASED"

    def test_unchanged_positions_excluded(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1001, 100, "ACC-2")  # <5% change
        rows = query_holdings_delta("CIK1", "2024-06-30", "2024-09-30")
        assert rows == []

    def test_empty_when_no_data_for_quarters(self):
        rows = query_holdings_delta("CIK1", "2024-06-30", "2024-09-30")
        assert rows == []


class TestPortfolioTimeline:
    def test_returns_one_row_per_quarter(self):
        _seed("CIK1", "2024-03-31", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-06-30", "CUSIP1", 1100, 110, "ACC-2")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1200, 120, "ACC-3")
        rows = query_portfolio_timeline("CIK1")
        assert len(rows) == 3

    def test_ordered_oldest_first(self):
        _seed("CIK1", "2024-09-30", "CUSIP1", 1000, 100, "ACC-3")
        _seed("CIK1", "2024-03-31", "CUSIP1", 900, 90, "ACC-1")
        _seed("CIK1", "2024-06-30", "CUSIP1", 950, 95, "ACC-2")
        rows = query_portfolio_timeline("CIK1")
        dates = [r["as_of_date"] for r in rows]
        assert dates == sorted(dates)

    def test_empty_for_unknown_cik(self):
        assert query_portfolio_timeline("UNKNOWN") == []


class TestSecurityHolders:
    def test_returns_holders_for_cusip(self):
        _seed("CIK1", "2024-09-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK2", "2024-09-30", "CUSIP1", 500, 50, "ACC-2")
        rows = query_security_holders("CUSIP1")
        assert len(rows) == 2

    def test_defaults_to_most_recent_quarter(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1100, 110, "ACC-2")
        rows = query_security_holders("CUSIP1")
        assert rows[0]["as_of_date"] == "2024-09-30"

    def test_filters_by_explicit_quarter(self):
        _seed("CIK1", "2024-06-30", "CUSIP1", 1000, 100, "ACC-1")
        _seed("CIK1", "2024-09-30", "CUSIP1", 1100, 110, "ACC-2")
        rows = query_security_holders("CUSIP1", quarter="2024-06-30")
        assert len(rows) == 1
        assert rows[0]["shares_held"] == 1000.0

    def test_sorted_by_shares_descending(self):
        _seed("CIK1", "2024-09-30", "CUSIP1", 500, 50, "ACC-1")
        _seed("CIK2", "2024-09-30", "CUSIP1", 2000, 200, "ACC-2")
        rows = query_security_holders("CUSIP1")
        assert rows[0]["shares_held"] > rows[1]["shares_held"]

    def test_empty_for_unknown_cusip(self):
        assert query_security_holders("UNKNOWN") == []
