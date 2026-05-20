"""
SEC EDGAR API client.

What we're pulling and why:
  - Company facts (CIK lookup, entity metadata)
  - 13F filings — institutional holdings reports filed by investment managers
    This is directly RIA-relevant: wealth management firms file 13Fs quarterly.

EDGAR is a good stand-in for a custodian API because:
  - Sparse, inconsistent documentation
  - Field names that are not self-explanatory (gaap vs ifrs namespaces)
  - Nested structures that vary across entity types
  - No changelog — endpoints change with SEC regulatory updates
  - Rate limits enforced via User-Agent header (not API key)
"""
import logging
from typing import Any

from src import config
from src.ingest.http_client import get

log = logging.getLogger(__name__)

# SEC requires a descriptive User-Agent — requests without one get blocked
_HEADERS = {"User-Agent": config.EDGAR_USER_AGENT, "Accept": "application/json"}

# Known large RIA / asset managers — useful for sampling diverse 13F data
# CIK numbers are stable identifiers in EDGAR (analogous to account_id in custodian APIs)
SAMPLE_FILERS = {
    "berkshire_hathaway": "0001067983",
    "blackrock":          "0001364742",
    "vanguard":           "0000102909",
    "fidelity":           "0000315066",
    "ares_management":    "0001555280",  # mid-size RIA — closer to Atomic's clients
}


def get_company_facts(cik: str, save_sample: bool = False) -> dict:
    """
    Fetch all reported financial facts for a company.
    Returns a heavily nested structure — a good profiling target.

    Real-world custodian analog: fetching all account metadata for a client.
    The nesting and inconsistency here mirrors what you'd see from Schwab.
    """
    cik_padded = cik.zfill(10)
    url = f"{config.EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    log.info("[edgar] fetching company facts for CIK %s", cik)
    return get(url, source="edgar", headers=_HEADERS,
               save_sample=save_sample, sample_name=f"facts_{cik}")


def get_company_concept(cik: str, concept: str, taxonomy: str = "us-gaap") -> dict:
    """
    Fetch a single reported concept (e.g. Assets, Liabilities) across all periods.
    Useful for seeing how field structure varies across time and filers.

    Interesting concepts to investigate:
      us-gaap/Assets, us-gaap/Liabilities, us-gaap/CashAndCashEquivalentsAtCarryingValue
    """
    cik_padded = cik.zfill(10)
    url = f"{config.EDGAR_BASE}/api/xbrl/companyconcept/CIK{cik_padded}/{taxonomy}/{concept}.json"
    log.info("[edgar] fetching %s/%s for CIK %s", taxonomy, concept, cik)
    return get(url, source="edgar", headers=_HEADERS)


def get_submissions(cik: str, save_sample: bool = False) -> dict:
    """
    Fetch filing history for a company — all form types, dates, accession numbers.
    We use this to find 13F filings specifically.
    """
    cik_padded = cik.zfill(10)
    url = f"{config.EDGAR_BASE}/submissions/CIK{cik_padded}.json"
    log.info("[edgar] fetching submissions for CIK %s", cik)
    return get(url, source="edgar", headers=_HEADERS,
               save_sample=save_sample, sample_name=f"submissions_{cik}")


def get_13f_filings(cik: str) -> list[dict]:
    """
    Extract 13F-HR filings from submission history.
    13F = quarterly institutional holdings report — what positions does this manager hold?
    """
    submissions = get_submissions(cik)
    filings = submissions.get("filings", {}).get("recent", {})

    forms       = filings.get("form", [])
    dates       = filings.get("filingDate", [])
    accessions  = filings.get("accessionNumber", [])
    descriptions = filings.get("primaryDocument", [])

    results = []
    for form, date, acc, doc in zip(forms, dates, accessions, descriptions):
        if form in ("13F-HR", "13F-HR/A"):
            results.append({
                "form":             form,
                "filing_date":      date,
                "accession_number": acc,
                "primary_document": doc,
                "cik":              cik,
            })

    log.info("[edgar] found %d 13F filings for CIK %s", len(results), cik)
    return results


def get_filing_index(cik: str, accession_number: str) -> dict:
    """Fetch the index of documents in a specific filing."""
    acc_clean = accession_number.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/index.json"
    return get(url, source="edgar", headers=_HEADERS)
