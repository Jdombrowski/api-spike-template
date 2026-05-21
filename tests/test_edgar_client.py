"""
Live integration tests for the SEC EDGAR client.

Run with:  make test-edgar

These hit the real EDGAR API. No API key required, but a valid User-Agent
must be set (EDGAR_USER_AGENT in .env) — SEC blocks anonymous requests.

Assertions are strictly against raw API response structure — no internal
field mappings or transformation logic tested here.
"""
import pytest

from src import config
from src.ingest import edgar_client

CIK = edgar_client.SAMPLE_FILERS["berkshire_hathaway"]


@pytest.fixture(autouse=True)
def require_user_agent():
    default = "Mozilla/5.0 (API Spike Investigation"
    if default in config.EDGAR_USER_AGENT:
        pytest.skip("EDGAR_USER_AGENT is still the placeholder — set a real value in .env")


class TestGetSubmissions:
    @pytest.fixture(scope="class")
    def response(self):
        return edgar_client.get_submissions(CIK)

    def test_entity_name_present(self, response):
        assert "name" in response

    def test_cik_present(self, response):
        assert "cik" in response

    def test_recent_filings_fields(self, response):
        """These are the raw EDGAR fields our 13F extraction reads — must not change."""
        recent = response.get("filings", {}).get("recent", {})
        for field in ("form", "filingDate", "accessionNumber", "primaryDocument"):
            assert field in recent, f"Raw EDGAR field '{field}' missing from filings.recent"


class TestGetCompanyConcept:
    @pytest.fixture(scope="class")
    def response(self):
        return edgar_client.get_company_concept(CIK, "Assets")

    def test_label_present(self, response):
        assert "label" in response or "description" in response

    def test_units_present(self, response):
        assert "units" in response

    def test_usd_values_present(self, response):
        usd = response.get("units", {}).get("USD", [])
        assert isinstance(usd, list)
        assert len(usd) > 0
