"""
Tests for the investigation tools.

Not exhaustive — focused on the behaviors that matter most:
  - Profiler correctly detects anomalies
  - Drift detector catches the failure modes you'd see in a real custodian API
  - Canonical mapper flags unknown fields rather than silently dropping them
"""
import pytest
from src.profile.profiler import Profiler
from src.schema.drift_detector import SchemaDriftDetector
from src.schema.canonical_mapper import EdgarCompanyFactsMapper, reconcile_entity


# ── Profiler ───────────────────────────────────────────────────────────────

class TestProfiler:

    def test_tracks_nullability(self):
        p = Profiler("test")
        p.add({"amount": 100.0})
        p.add({"amount": None})
        p.add({"amount": 200.0})

        s = p._stats["amount"]
        assert s.null    == 1
        assert s.present == 2
        assert s.null_pct == pytest.approx(33.33, rel=0.01)

    def test_detects_absent_vs_null(self):
        """Absent and null must be tracked separately — they mean different things."""
        p = Profiler("test")
        p.add({"amount": 100.0})
        p.add({})                  # field absent entirely
        p.add({"amount": None})    # field present but null

        s = p._stats["amount"]
        assert s.absent == 1
        assert s.null   == 1
        assert s.anomaly is not None   # should flag both-absent-and-null

    def test_detects_type_inconsistency(self):
        """String/number type flip is a common custodian API issue."""
        p = Profiler("test")
        p.add({"market_value": "1234.56"})  # string (common in custodian APIs)
        p.add({"market_value": 1234.56})    # float
        p.add({"market_value": "2000.00"})

        assert not p._stats["market_value"].type_consistent
        assert p._stats["market_value"].anomaly is not None

    def test_baseline_export(self):
        p = Profiler("test")
        p.add({"name": "Berkshire", "cik": 1067983, "sic": None})
        baseline = p.to_baseline()

        assert "name"  in baseline
        assert "cik"   in baseline
        assert baseline["sic"]["nullable"] is True
        assert baseline["name"]["types"] == ["str"]


# ── Drift Detector ─────────────────────────────────────────────────────────

class TestDriftDetector:

    def _make_detector(self, sample_records: list[dict]) -> SchemaDriftDetector:
        p = Profiler("test")
        for r in sample_records:
            p.add(r)
        d = SchemaDriftDetector("test")
        d.set_baseline_from_profiler(p)
        return d

    def test_clean_record_no_issues(self):
        d = self._make_detector([
            {"name": "Berkshire", "cik": 1067983},
            {"name": "Blackrock",  "cik": 1364742},
        ])
        assert d.check({"name": "Vanguard", "cik": 102909}) == []

    def test_detects_missing_required_field(self):
        d = self._make_detector([
            {"name": "Berkshire", "cik": 1067983},
            {"name": "Blackrock",  "cik": 1364742},
        ])
        issues = d.check({"name": "Vanguard"})  # cik missing
        errors = [i for i in issues if i.severity == "ERROR"]
        assert any(i.field == "cik" for i in errors)

    def test_detects_new_field(self):
        d = self._make_detector([{"name": "Berkshire", "cik": 1067983}])
        issues = d.check({"name": "Vanguard", "cik": 102909, "new_field": "surprise"})
        infos  = [i for i in issues if i.severity == "INFO"]
        assert any(i.field == "new_field" for i in infos)

    def test_detects_type_change(self):
        """cik goes from int to string — silent corruption risk."""
        d = self._make_detector([{"cik": 1067983, "name": "Berkshire"}])
        issues = d.check({"cik": "1067983", "name": "Berkshire"})   # now a string
        errors = [i for i in issues if i.severity == "ERROR"]
        assert any(i.field == "cik" for i in errors)

    def test_detects_unexpected_null(self):
        d = self._make_detector([
            {"cik": 1067983, "name": "Berkshire"},
            {"cik": 1364742, "name": "Blackrock"},
        ])
        issues = d.check({"cik": None, "name": "Vanguard"})   # cik was never null
        warnings = [i for i in issues if i.severity == "WARNING"]
        assert any(i.field == "cik" for i in warnings)


# ── Canonical Mapper ───────────────────────────────────────────────────────

class TestEdgarMapper:

    def test_maps_confirmed_fields(self):
        mapper = EdgarCompanyFactsMapper()
        result = mapper.map({"name": "Berkshire Hathaway Inc", "cik": 1067983, "sic": "6331"})
        assert result["entity_name"] == "Berkshire Hathaway Inc"
        assert result["cik"]         == 1067983

    def test_flags_unknown_fields(self):
        """Unknown fields must not be silently dropped — they need investigation."""
        mapper = EdgarCompanyFactsMapper()
        result = mapper.map({"name": "Test Corp", "totally_new_field": "value"})
        # Should appear prefixed, not dropped
        assert "__unknown_totally_new_field" in result

    def test_tracks_assumptions(self):
        mapper = EdgarCompanyFactsMapper()
        result = mapper.map({"fiscalYearEnd": "0930"})
        assert len(result["_mapper_assumptions"]) > 0


# ── Reconciliation ─────────────────────────────────────────────────────────

class TestReconciliation:

    def test_validated_when_cik_and_name_match(self):
        edgar   = {"cik": 1067983, "entity_name": "BERKSHIRE HATHAWAY INC"}
        polygon = {"sec_cik": "1067983", "entity_name": "BERKSHIRE HATHAWAY INC"}
        result  = reconcile_entity(edgar, polygon)
        assert result["overall"] == "VALIDATED"

    def test_needs_review_on_cik_mismatch(self):
        edgar   = {"cik": 1067983, "entity_name": "BERKSHIRE HATHAWAY INC"}
        polygon = {"sec_cik": "9999999", "entity_name": "BERKSHIRE HATHAWAY INC"}
        result  = reconcile_entity(edgar, polygon)
        assert result["overall"] == "NEEDS_REVIEW"
        assert result["checks"]["cik_match"]["status"] == "MISMATCH"
