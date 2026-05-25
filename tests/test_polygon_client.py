"""
Live integration tests for the Polygon.io client.

Run with:  make test-polygon

These hit the real API. They require POLYGON_API_KEY in .env and a network
connection. They are intentionally excluded from the default `make test` suite.

Each class fetches its response once via a class-scoped fixture to avoid
hammering the free-tier rate limit (5 req/min).
"""

import pytest

from src import config
from src.ingest import polygon_client

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def require_api_key():
    if not config.POLYGON_API_KEY:
        pytest.skip("POLYGON_API_KEY not set — skipping live Polygon tests")


class TestGetTickerTypes:
    @pytest.fixture(scope="class")
    def response(self):
        return polygon_client.get_ticker_types()

    def test_returns_list_of_types(self, response):
        assert response.get("status") == "OK"
        assert isinstance(response.get("results", []), list)
        assert len(response["results"]) > 0

    def test_each_type_has_code_and_description(self, response):
        for t in response["results"]:
            assert "code" in t, f"Missing 'code' in type entry: {t}"
            assert "description" in t, f"Missing 'description' in type entry: {t}"


class TestGetTickerDetails:
    TICKER = "AAPL"

    @pytest.fixture(scope="class")
    def response(self):
        return polygon_client.get_ticker_details(self.TICKER)

    def test_returns_ok_status(self, response):
        assert response.get("status") == "OK", f"Unexpected status: {response.get('status')}"

    def test_results_has_expected_fields(self, response):
        r = response.get("results", {})
        assert r.get("ticker") == self.TICKER
        assert "name" in r
        assert "market_cap" in r or "description" in r

    def test_cik_is_present(self, response):
        """Polygon returns the SEC CIK — critical for EDGAR cross-reference."""
        r = response.get("results", {})
        assert r.get("cik") or r.get("sec_cik"), (
            "No CIK returned for AAPL — field name may have changed"
        )


class TestGetDailyBars:
    TICKER = "AAPL"

    @pytest.fixture(scope="class")
    def response(self):
        return polygon_client.get_daily_bars(self.TICKER)

    def test_returns_ok_status(self, response):
        # Free-tier returns "DELAYED"; paid returns "OK" — both mean success
        assert response.get("status") in ("OK", "DELAYED"), (
            f"Unexpected status: {response.get('status')}"
        )

    def test_results_is_nonempty_list(self, response):
        bars = response.get("results", [])
        assert isinstance(bars, list)
        assert len(bars) > 0, "Expected at least one bar in the default 90-day window"

    def test_each_bar_has_ohlcv(self, response):
        """All OHLCV fields must be present — missing any breaks downstream valuation."""
        for bar in response["results"]:
            for field in ("o", "h", "l", "c", "v"):
                assert field in bar, f"Bar missing '{field}': {bar}"
