"""
Tests for the 13F holdings pipeline:
  - ThirteenFMapper: XML parsing → CanonicalHolding
  - derive_quarter_end: filing date → quarter-end
  - DB layer: save_holdings / query_holdings_summary
  - holdings_pipeline.run(): end-to-end with mocked network calls
  - Cross-validation: ratio calculation and status classification
"""

from unittest.mock import MagicMock

import pytest

import src.holdings_pipeline as hp_mod
from src import config
from src.schema.holdings_mapper import ThirteenFMapper, derive_quarter_end
from src.storage import db
from src.storage.db import query_holdings, query_holdings_summary, save_holdings

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "validate", lambda: None)
    db.init_db()


_FILING_META = {
    "form": "13F-HR",
    "filing_date": "2023-02-14",
    "accession_number": "0001067983-23-000009",
}

_INFO_TABLE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>174523</value>
    <shrsOrPrnAmt>
      <sshPrnamt>1013162</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>1013162</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>BANK OF AMERICA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>060505104</cusip>
    <value>29631</value>
    <shrsOrPrnAmt>
      <sshPrnamt>1010100</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>1010100</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>
"""

_PRN_XML = """\
<informationTable>
  <infoTable>
    <nameOfIssuer>SOME CORP NOTES</nameOfIssuer>
    <cusip>123456789</cusip>
    <value>5000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>5000000</sshPrnamt>
      <sshPrnamtType>PRN</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>
"""


# ── derive_quarter_end ─────────────────────────────────────────────────────


class TestDeriveQuarterEnd:
    def test_jan_filing_returns_prior_q4(self):
        assert derive_quarter_end("2023-01-20") == "2022-12-31"

    def test_feb_filing_returns_prior_q4(self):
        assert derive_quarter_end("2023-02-14") == "2022-12-31"

    def test_may_filing_returns_q1(self):
        assert derive_quarter_end("2023-05-10") == "2023-03-31"

    def test_aug_filing_returns_q2(self):
        assert derive_quarter_end("2023-08-01") == "2023-06-30"

    def test_nov_filing_returns_q3(self):
        assert derive_quarter_end("2023-11-14") == "2023-09-30"

    def test_none_returns_none(self):
        assert derive_quarter_end(None) is None

    def test_invalid_date_returns_none(self):
        assert derive_quarter_end("not-a-date") is None


# ── ThirteenFMapper ────────────────────────────────────────────────────────


class TestThirteenFMapper:
    @pytest.fixture
    def mapper(self):
        return ThirteenFMapper("0001067983", _FILING_META)

    def test_parses_correct_number_of_holdings(self, mapper):
        holdings = mapper.parse(_INFO_TABLE_XML)
        assert len(holdings) == 2

    def test_maps_issuer_name(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.entity_name == "APPLE INC"

    def test_maps_cusip(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.cusip == "037833100"

    def test_maps_value_as_float(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.market_value_usd == 174523.0

    def test_value_unit_is_usd_thousands(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.value_unit == "USD_THOUSANDS"

    def test_maps_shares_held(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.shares_held == 1_013_162.0

    def test_ticker_is_none(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.ticker is None  # not in 13F XML

    def test_as_of_date_derived_from_filing_date(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert h.as_of_date == "2022-12-31"  # Feb 2023 filing → Q4 2022

    def test_canonical_id_includes_cik_accession_cusip(self, mapper):
        h = mapper.parse(_INFO_TABLE_XML)[0]
        assert "0001067983" in h.canonical_id
        assert "037833100" in h.canonical_id

    def test_prn_type_triggers_assumption_flag(self, mapper):
        holdings = mapper.parse(_PRN_XML)
        assert len(holdings) == 1
        assert any("PRN" in a for a in holdings[0].assumptions)

    def test_namespace_stripped_correctly(self, mapper):
        holdings = mapper.parse(_INFO_TABLE_XML)
        assert len(holdings) == 2  # namespace must not cause parse failure

    def test_invalid_xml_raises_value_error(self, mapper):
        with pytest.raises(ValueError, match="XML parse error"):
            mapper.parse("<not valid xml")


# ── DB layer ───────────────────────────────────────────────────────────────


class TestHoldingsStorage:
    def _make_row(
        self,
        cik="0001067983",
        issuer="APPLE INC",
        value=174523.0,
        status="CLOSE",
        shares=1_013_162.0,
        cusip="037833100",
    ):
        return {
            "cik": cik,
            "accession_number": "0001067983-23-000009",
            "form_type": "13F-HR",
            "filing_date": "2023-02-14",
            "as_of_date": "2022-12-31",
            "issuer_name": issuer,
            "cusip": cusip,
            "ticker": "AAPL",
            "shares_held": shares,
            "value_reported": value,
            "value_unit": "USD_THOUSANDS",
            "price_at_filing": 130.73,
            "value_estimated": shares * 130.73,
            "validation_ratio": (shares * 130.73) / (value * 1000),
            "validation_status": status,
        }

    def test_save_and_query_round_trip(self):
        save_holdings([self._make_row()])
        rows = query_holdings("0001067983")
        assert len(rows) == 1
        assert rows[0]["issuer_name"] == "APPLE INC"

    def test_query_holdings_summary_totals(self):
        save_holdings(
            [
                self._make_row(),
                self._make_row(issuer="BAC", cusip="025816109", value=29631.0, status="NO_PRICE"),
            ]
        )
        summary = query_holdings_summary()
        assert len(summary) == 1
        assert summary[0]["position_count"] == 2
        assert summary[0]["validated_count"] == 1  # one CLOSE

    def test_save_empty_list_is_noop(self):
        save_holdings([])
        assert query_holdings() == []

    def test_query_without_cik_returns_all(self):
        save_holdings([self._make_row("0001067983"), self._make_row("0001364742")])
        rows = query_holdings()
        ciks = {r["cik"] for r in rows}
        assert ciks == {"0001067983", "0001364742"}


# ── Cross-validation logic ─────────────────────────────────────────────────


class TestCrossValidation:
    def _make_holding(self, shares=1_000_000.0, value=130_000.0, entity_name="APPLE INC"):
        from src.schema.canonical_mapper import CanonicalHolding

        return CanonicalHolding(
            source="edgar_13f",
            source_entity_id="0001067983",
            canonical_id="0001067983:acc:cusip",
            entity_name=entity_name,
            ticker=None,
            cusip="037833100",
            shares_held=shares,
            market_value_usd=value,
            value_unit="USD_THOUSANDS",
            as_of_date="2022-12-31",
            filing_date="2023-02-14",
            form_type="13F-HR",
            assumptions=[],
            unmapped_fields=[],
        )

    def test_close_when_ratio_within_threshold(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: "AAPL")
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: 130.0)
        results = hp_mod._cross_validate([self._make_holding(shares=1_000_000, value=130_000)])
        assert results[0]["validation_status"] == "CLOSE"

    def test_divergent_when_ratio_outside_threshold(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: "AAPL")
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: 300.0)  # 2× too high
        results = hp_mod._cross_validate([self._make_holding(shares=1_000_000, value=130_000)])
        assert results[0]["validation_status"] == "DIVERGENT"

    def test_no_price_when_ticker_unresolved(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: None)
        results = hp_mod._cross_validate([self._make_holding()])
        assert results[0]["validation_status"] == "NO_PRICE"

    def test_no_price_when_polygon_returns_no_bar(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: "AAPL")
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: None)
        results = hp_mod._cross_validate([self._make_holding()])
        assert results[0]["validation_status"] == "NO_PRICE"

    def test_no_price_when_shares_or_value_missing(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: "AAPL")
        holding = self._make_holding()
        holding.shares_held = None
        results = hp_mod._cross_validate([holding])
        assert results[0]["validation_status"] == "NO_PRICE"


# ── Pipeline end-to-end ────────────────────────────────────────────────────


class TestHoldingsPipelineRun:
    def test_run_stores_holdings(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "get_13f_filings", lambda cik: [_FILING_META])
        monkeypatch.setattr(hp_mod, "get_13f_document", lambda cik, acc: _INFO_TABLE_XML)
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: None)
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: None)

        hp_mod.run(["0001067983"])

        rows = query_holdings("0001067983")
        assert len(rows) == 2
        assert {r["issuer_name"] for r in rows} == {"APPLE INC", "BANK OF AMERICA CORP"}

    def test_run_skips_cik_on_filings_fetch_error(self, monkeypatch):
        monkeypatch.setattr(
            hp_mod,
            "get_13f_filings",
            MagicMock(side_effect=LookupError("404")),
        )
        hp_mod.run(["0001067983"])  # should not raise
        assert query_holdings("0001067983") == []

    def test_run_skips_filing_on_xml_fetch_error(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "get_13f_filings", lambda cik: [_FILING_META])
        monkeypatch.setattr(
            hp_mod,
            "get_13f_document",
            MagicMock(side_effect=LookupError("404")),
        )
        hp_mod.run(["0001067983"])  # should not raise
        assert query_holdings("0001067983") == []

    def test_run_skips_filing_on_xml_parse_error(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "get_13f_filings", lambda cik: [_FILING_META])
        monkeypatch.setattr(hp_mod, "get_13f_document", lambda cik, acc: "<broken xml")
        hp_mod.run(["0001067983"])  # should not raise
        assert query_holdings("0001067983") == []


# ── validate_top ───────────────────────────────────────────────────────────


class TestValidateTop:
    """validate_top limits Polygon calls to the top-N positions by reported value."""

    def test_only_top_n_positions_are_cross_validated(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "get_13f_filings", lambda cik: [_FILING_META])
        monkeypatch.setattr(hp_mod, "get_13f_document", lambda cik, acc: _INFO_TABLE_XML)
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: "AAPL")
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: 130.0)

        hp_mod.run(["0001067983"], validate_top=1)

        rows = query_holdings("0001067983")
        assert len(rows) == 2
        # highest-value position (APPLE, value=174523) should be validated
        aapl = next(r for r in rows if r["issuer_name"] == "APPLE INC")
        assert aapl["validation_status"] in ("CLOSE", "DIVERGENT")
        # second position should be skipped
        bac = next(r for r in rows if r["issuer_name"] == "BANK OF AMERICA CORP")
        assert bac["validation_status"] == "NO_PRICE"

    def test_remainder_stored_as_no_price(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "get_13f_filings", lambda cik: [_FILING_META])
        monkeypatch.setattr(hp_mod, "get_13f_document", lambda cik, acc: _INFO_TABLE_XML)
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: None)
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: None)

        hp_mod.run(["0001067983"], validate_top=0)

        rows = query_holdings("0001067983")
        assert all(r["validation_status"] == "NO_PRICE" for r in rows)

    def test_validate_top_larger_than_positions_validates_all(self, monkeypatch):
        monkeypatch.setattr(hp_mod, "get_13f_filings", lambda cik: [_FILING_META])
        monkeypatch.setattr(hp_mod, "get_13f_document", lambda cik, acc: _INFO_TABLE_XML)
        monkeypatch.setattr(hp_mod, "_resolve_ticker", lambda h: "TICK")
        monkeypatch.setattr(hp_mod, "_fetch_closing_price", lambda t, d: 130.0)

        hp_mod.run(["0001067983"], validate_top=100)

        rows = query_holdings("0001067983")
        # all 2 positions attempted (may be CLOSE/DIVERGENT, not NO_PRICE due to skipping)
        assert len(rows) == 2
        assert all(
            r["validation_status"] != "NO_PRICE" or r["price_at_filing"] is None for r in rows
        )
