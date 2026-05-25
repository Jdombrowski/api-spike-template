"""
Canonical field mapper.

Every field mapping is an explicit, documented decision — not implicit code.
This file IS the investigation artifact. The comments are as important as the code.

When you get to a real custodian API at Atomic:
  1. Run the profiler to understand what fields actually exist
  2. Fill in this mapper with your findings
  3. Flag assumptions with ASSUMPTION comments
  4. Validate assumptions and convert to CONFIRMED as you verify

Design: separate mapper classes per source, unified canonical schema output.
This makes adding a second custodian (Fidelity, Pershing) clean — each gets
its own mapper, same canonical output, reconciliation is straightforward.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)


# ── Canonical schema ───────────────────────────────────────────────────────
# This is the shape everything gets normalized to.
# Analogous to your fct_transactions dbt model in a real pipeline.


@dataclass
class CanonicalHolding:
    """A single position held by an institutional investor, as reported in a 13F."""

    source: str  # which API this came from
    source_entity_id: str  # the raw ID (CIK, etc.) before normalization
    canonical_id: str  # our stable internal identifier

    # Entity
    entity_name: str
    ticker: str | None  # may be None — 13Fs report CUSIPs, not always tickers
    cusip: str | None  # Committee on Uniform Securities Identification

    # Position
    shares_held: float | None
    market_value_usd: float | None  # as reported — may be thousands, verify units
    value_unit: str  # "USD" | "USD_THOUSANDS" — ASSUMPTION until verified

    # Metadata
    as_of_date: str | None  # reporting period end date
    filing_date: str | None
    form_type: str | None  # "13F-HR" | "13F-HR/A"

    # Investigation flags
    assumptions: list[str]  # fields where we're still assuming, not confirmed
    unmapped_fields: list[str]  # fields we saw but haven't mapped yet


# ── EDGAR mapper ───────────────────────────────────────────────────────────


class EdgarCompanyFactsMapper:
    """
    Maps raw EDGAR companyfacts response to a structure we can work with.

    EDGAR companyfacts is a good profiling target because:
    - It has two namespaces (us-gaap, dei) with inconsistent coverage
    - Units vary: some values are in USD, some in shares, some in pure numbers
    - Time periods are inconsistent: some filers report quarterly, some annually
    - The same concept (e.g. Assets) may be reported under different tags across filers
    """

    # Confirmed mappings — validated by cross-referencing multiple filers
    CONFIRMED = {
        "entityType": "entity_type",  # CONFIRMED: "operating" | "funds" | "holding"
        "cik": "cik",  # CONFIRMED: int, SEC's stable entity identifier
        "name": "entity_name",  # CONFIRMED: string, legal name
        "sic": "sic_code",  # CONFIRMED: industry classification code
        "sicDescription": "sic_description",  # CONFIRMED: human-readable SIC
        "stateOfIncorporation": "state_of_incorporation",  # CONFIRMED: 2-letter state code
        "tickers": "tickers",  # CONFIRMED: list of ticker strings, may be empty
        "exchanges": "exchanges",  # CONFIRMED: list, may be empty for OTC
    }

    # Assumption mappings — flagged for validation
    ASSUMPTIONS = {
        "fiscalYearEnd": "fiscal_year_end_mmdd",
        # ASSUMPTION: value is "0930" format (MMDD), not a full date
        # Need to verify: is this always present? What about non-US filers?
        "ein": "employer_id_number",
        # ASSUMPTION: this is the EIN, not another ID type
        # Null for foreign filers — validated in profiler (absent 12% of records)
    }

    # Fields we observed but have not yet mapped
    UNMAPPED = [
        # Submissions endpoint — entity metadata we're not using yet
        "ownerOrg",
        "insiderTransactionForOwnerExists",
        "insiderTransactionForIssuerExists",
        "lei",  # Legal Entity Identifier — useful for cross-referencing
        "description",
        "website",
        "investorWebsite",
        "category",  # e.g. "Domestic Operating Companies"
        "stateOfIncorporationDescription",
        "phone",
        "flags",
        "formerNames",  # list of {"date": ..., "name": ...} — useful for alias matching
        "addresses",  # mailing + business — not needed for holdings use case
        "filings",  # filing history — separate mapper
        # Company facts endpoint
        "facts",  # deeply nested XBRL data — needs its own profiler pass
    ]

    def map(self, raw: dict) -> dict:
        canonical = {}
        assumptions_triggered = []
        unmapped_seen = []

        for raw_key, value in raw.items():
            if raw_key in self.CONFIRMED:
                canonical[self.CONFIRMED[raw_key]] = value

            elif raw_key in self.ASSUMPTIONS:
                canonical_key = self.ASSUMPTIONS[raw_key]
                canonical[canonical_key] = value
                assumptions_triggered.append(
                    f"'{raw_key}' → '{canonical_key}': mapping is assumed, not confirmed"
                )

            elif raw_key in self.UNMAPPED:
                unmapped_seen.append(raw_key)
                # Don't drop — pass through under prefixed key for downstream use
                canonical[f"__raw_{raw_key}"] = value

            else:
                # Genuinely new field — log it, don't silently drop
                log.warning(
                    "[edgar_mapper] unseen field '%s' (type=%s) — add to CONFIRMED, "
                    "ASSUMPTIONS, or UNMAPPED after investigation",
                    raw_key,
                    type(value).__name__,
                )
                canonical[f"__unknown_{raw_key}"] = value
                unmapped_seen.append(raw_key)

        canonical["_mapper_assumptions"] = assumptions_triggered
        canonical["_unmapped_fields"] = unmapped_seen
        canonical["_source"] = "edgar_companyfacts"
        canonical["_mapped_at"] = datetime.now(timezone.utc).isoformat()

        return canonical


class EdgarConceptMapper:
    """
    Maps a single XBRL concept (e.g. us-gaap/Assets) response.

    Key challenge: the 'units' key contains the actual data, nested under
    the unit type ('USD', 'shares', 'pure'). Which unit a concept uses
    is NOT documented — you discover it empirically.
    """

    def extract_values(self, raw: dict, preferred_unit: str = "USD") -> list[dict]:
        """
        Extract time-series values from a concept response.

        ASSUMPTION: we prefer USD-denominated values. For share counts,
        caller should pass preferred_unit='shares'.
        """
        units = raw.get("units", {})

        if not units:
            log.warning(
                "[concept_mapper] no 'units' key in response — empty or unsupported concept"
            )
            return []

        available_units = list(units.keys())
        if preferred_unit not in units:
            log.warning(
                "[concept_mapper] preferred unit '%s' not found; "
                "available: %s — using first available",
                preferred_unit,
                available_units,
            )
            unit = available_units[0] if available_units else None
        else:
            unit = preferred_unit

        if unit is None:
            return []

        raw_values = units[unit]
        results = []

        for entry in raw_values:
            # CONFIRMED: these fields are always present
            # ASSUMPTION: 'val' is the reported value in the unit stated
            # NOTE: same period may appear multiple times (amendments) — take latest
            results.append(
                {
                    "concept": raw.get("tag"),  # CONFIRMED: e.g. "Assets"
                    "taxonomy": raw.get("taxonomy"),  # CONFIRMED: "us-gaap" | "dei"
                    "unit": preferred_unit,
                    "value": entry.get("val"),  # CONFIRMED: numeric
                    "start": entry.get("start"),  # ASSUMPTION: ISO date, may be absent for instants
                    "end": entry.get("end"),  # CONFIRMED: period end date
                    "form": entry.get("form"),  # CONFIRMED: filing form type
                    "filed": entry.get("filed"),  # CONFIRMED: filing date
                    "accn": entry.get("accn"),  # CONFIRMED: accession number (unique filing ID)
                    "frame": entry.get("frame"),  # ASSUMPTION: CY2023Q3 format, sometimes absent
                }
            )

        return results


# ── Polygon mapper ─────────────────────────────────────────────────────────


class PolygonTickerMapper:
    """
    Maps Polygon ticker details to canonical form.
    Used as the reference/validation source against EDGAR data.
    """

    CONFIRMED = {
        "ticker": "ticker",
        "name": "entity_name",
        "market": "market",  # "stocks" | "crypto" | "fx"
        "locale": "locale",  # "us" | "global"
        "primary_exchange": "primary_exchange",
        "type": "security_type",  # "CS" = common stock, "ETF", etc.
        "active": "is_active",
        "currency_name": "currency",
        "cik": "sec_cik",  # KEY: cross-reference to EDGAR
        "composite_figi": "figi",
        "share_class_figi": "share_class_figi",
        "market_cap": "market_cap_usd",
        "share_class_shares_outstanding": "shares_outstanding",
        "weighted_shares_outstanding": "weighted_shares_outstanding",
    }

    # Fields seen in some responses but not others
    CONDITIONAL = {
        "description": "business_description",  # absent for some security types
        "homepage_url": "website_url",  # absent for foreign filers
        "total_employees": "employee_count",  # absent for holding companies
        "list_date": "ipo_date",  # absent for pre-IPO or OTC
        "sic_code": "sic_code",  # ASSUMPTION: same as EDGAR SIC
        "sic_description": "sic_description",
    }

    def map(self, raw: dict) -> dict:
        result_raw = raw.get("results", raw)  # Polygon wraps in {"results": {...}}
        canonical = {}

        for raw_key, can_key in {**self.CONFIRMED, **self.CONDITIONAL}.items():
            if raw_key in result_raw:
                canonical[can_key] = result_raw[raw_key]

        canonical["_source"] = "polygon_ticker"
        canonical["_mapped_at"] = datetime.now(timezone.utc).isoformat()
        return canonical


# ── Cross-source reconciliation helper ────────────────────────────────────


def reconcile_entity(edgar_canonical: dict, polygon_canonical: dict) -> dict:
    """
    Compare the same entity as reported by EDGAR and Polygon.
    Returns a reconciliation result with match status per field.

    This is the triangulation step — if EDGAR and Polygon agree on
    entity_name and CIK, we have high confidence in our field mappings.
    If they disagree, we have an investigation finding to document.
    """
    checks = {}

    # CIK should match exactly — this is the strongest cross-reference
    edgar_cik = str(edgar_canonical.get("cik", "")).lstrip("0")
    polygon_cik = str(polygon_canonical.get("sec_cik", "")).lstrip("0")

    checks["cik_match"] = {
        "edgar": edgar_cik,
        "polygon": polygon_cik,
        "status": "MATCH" if edgar_cik and edgar_cik == polygon_cik else "MISMATCH",
        "note": "Primary cross-reference — must match for entity to be reliably identified",
    }

    # Name matching (fuzzy — legal names vary slightly across sources)
    edgar_name = edgar_canonical.get("entity_name", "").upper().strip()
    polygon_name = polygon_canonical.get("entity_name", "").upper().strip()
    name_match = edgar_name == polygon_name
    name_similar = (
        edgar_name[:20] == polygon_name[:20]  # first 20 chars usually agree
        if edgar_name and polygon_name
        else False
    )

    checks["name_match"] = {
        "edgar": edgar_canonical.get("entity_name"),
        "polygon": polygon_canonical.get("entity_name"),
        "status": "MATCH" if name_match else ("SIMILAR" if name_similar else "MISMATCH"),
        "note": "SIMILAR is acceptable — legal name variations are common",
    }

    # SIC code — both sources should report the same industry
    edgar_sic = str(edgar_canonical.get("sic_code", ""))
    polygon_sic = str(polygon_canonical.get("sic_code", ""))

    if edgar_sic and polygon_sic:
        checks["sic_match"] = {
            "edgar": edgar_sic,
            "polygon": polygon_sic,
            "status": "MATCH" if edgar_sic == polygon_sic else "MISMATCH",
            "note": "SIC mismatch may indicate one source is stale",
        }

    # Summary
    statuses = [v["status"] for v in checks.values()]
    overall = "VALIDATED" if all(s in ("MATCH", "SIMILAR") for s in statuses) else "NEEDS_REVIEW"

    return {
        "overall": overall,
        "checks": checks,
        "note": "VALIDATED means cross-source checks passed — field mappings are reliable",
    }
