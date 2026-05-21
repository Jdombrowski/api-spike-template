"""
13F InfoTable XML mapper.

Parses the SEC EDGAR InfoTable XML document into CanonicalHolding records.
Kept separate from canonical_mapper.py because the input format (XML vs dict)
and the domain (positions vs entity metadata) are distinct enough to warrant it.

Key findings documented here:
  - Values are in USD THOUSANDS — CONFIRMED per SEC 13F instructions
  - sshPrnamtType "SH" = shares held; "PRN" = principal amount (bonds/notes)
  - The same CUSIP can appear multiple times (different share classes, PRN vs SH)
  - ticker is NOT present in the InfoTable — requires separate CUSIP→ticker resolution
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date

from src.schema.canonical_mapper import CanonicalHolding

log = logging.getLogger(__name__)


def derive_quarter_end(filing_date: str | None) -> str | None:
    """
    Derive the reporting quarter-end date from a 13F filing date.

    13Fs are due 45 days after quarter-end, so the filing month tells us
    which quarter was just reported:
      Jan–Mar  → Q4 of prior year  (Dec 31)
      Apr–Jun  → Q1               (Mar 31)
      Jul–Sep  → Q2               (Jun 30)
      Oct–Dec  → Q3               (Sep 30)
    """
    if not filing_date:
        return None
    try:
        d = date.fromisoformat(filing_date)
        if d.month <= 3:
            return f"{d.year - 1}-12-31"
        elif d.month <= 6:
            return f"{d.year}-03-31"
        elif d.month <= 9:
            return f"{d.year}-06-30"
        else:
            return f"{d.year}-09-30"
    except ValueError:
        return None


class ThirteenFMapper:
    """
    Maps a raw 13F-HR InfoTable XML string to a list of CanonicalHolding records.

    Usage:
        mapper   = ThirteenFMapper(cik, filing_meta)
        holdings = mapper.parse(xml_text)
    """

    # Tags with direct scalar mappings to CanonicalHolding fields
    _CONFIRMED = {
        "nameOfIssuer":         "entity_name",
        "titleOfClass":         "title_of_class",
        "cusip":                "cusip",
        "value":                "market_value_usd",   # CONFIRMED: USD thousands
        "investmentDiscretion": "investment_discretion",
    }

    # Tags we see but intentionally don't map — kept for investigation completeness
    _KNOWN_UNMAPPED = {"votingAuthority"}

    def __init__(self, cik: str, filing_meta: dict):
        self.cik         = cik
        self.filing_date = filing_meta.get("filing_date")
        self.form_type   = filing_meta.get("form")
        self.accession   = filing_meta.get("accession_number", "")

    def parse(self, xml_text: str) -> list[CanonicalHolding]:
        """Parse InfoTable XML and return one CanonicalHolding per <infoTable> element."""
        # Strip namespace declarations so findall() works without a prefix
        cleaned = re.sub(r'\s*xmlns(?::\w+)?="[^"]*"', "", xml_text)
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise ValueError(f"[holdings_mapper] XML parse error for CIK {self.cik}: {exc}") from exc

        results = [
            h for elem in root.findall(".//infoTable")
            if (h := self._map_element(elem)) is not None
        ]
        log.info("[holdings_mapper] parsed %d holdings for CIK %s", len(results), self.cik)
        return results

    def _map_element(self, elem: ET.Element) -> CanonicalHolding | None:
        assumptions: list[str] = []
        unmapped:    list[str] = []

        name  = (elem.findtext("nameOfIssuer") or "").strip()
        cusip = (elem.findtext("cusip") or "").strip()

        value_reported = _parse_int(elem.findtext("value"), "value", assumptions)
        shares, prnamt_type = _parse_shares(elem, assumptions)

        # Flag tags we haven't seen before
        known = set(self._CONFIRMED) | self._KNOWN_UNMAPPED | {"shrsOrPrnAmt"}
        for child in elem:
            tag = child.tag.split("}")[-1]   # strip XML namespace prefix if present
            if tag not in known:
                unmapped.append(tag)

        value_unit = _infer_value_unit(value_reported, shares, assumptions)

        return CanonicalHolding(
            source           = "edgar_13f",
            source_entity_id = self.cik,
            canonical_id     = f"{self.cik}:{self.accession}:{cusip}",
            entity_name      = name,
            ticker           = None,             # not in 13F — needs CUSIP resolution
            cusip            = cusip or None,
            shares_held      = float(shares) if shares is not None else None,
            market_value_usd = float(value_reported) if value_reported is not None else None,
            value_unit       = value_unit,
            as_of_date       = derive_quarter_end(self.filing_date),
            filing_date      = self.filing_date,
            form_type        = self.form_type,
            assumptions      = assumptions,
            unmapped_fields  = unmapped,
        )


# ── Private helpers ────────────────────────────────────────────────────────

def _infer_value_unit(
    value: int | None, shares: int | None, assumptions: list[str]
) -> str:
    """
    Detect whether <value> is in USD thousands (SEC standard) or full USD.

    The SEC 13F instructions specify thousands, but recent EDGAR XML submissions
    have been observed using full dollar values. Heuristic: if value/shares >= $5,
    the implied per-share price is in a plausible stock-price range, meaning the
    value is already in full USD. Below that threshold, the per-share figure is
    sub-dollar, which matches the thousands convention.

    Edge case: stocks priced above $5,000/share (e.g. BRK.A) would be
    misidentified when the filing truly uses thousands — log as assumption.
    """
    if value is None or shares is None or shares == 0:
        return "USD_THOUSANDS"
    implied_price = value / shares
    if implied_price >= 5.0:
        assumptions.append(
            f"value_unit inferred as USD (not USD_THOUSANDS): "
            f"value/shares={implied_price:.2f} exceeds thousands threshold"
        )
        return "USD"
    return "USD_THOUSANDS"


def _parse_int(text: str | None, field: str, assumptions: list[str]) -> int | None:
    if not text:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        assumptions.append(f"'{field}' value '{text!r}' could not be parsed as int")
        return None


def _parse_shares(
    elem: ET.Element, assumptions: list[str]
) -> tuple[int | None, str | None]:
    shrs = elem.find("shrsOrPrnAmt")
    if shrs is None:
        return None, None
    prnamt_type = (shrs.findtext("sshPrnamtType") or "SH").strip()
    shares = _parse_int(shrs.findtext("sshPrnamt"), "sshPrnamt", assumptions)
    if prnamt_type != "SH":
        assumptions.append(
            f"sshPrnamtType='{prnamt_type}': amount is principal (bonds/notes), not share count"
        )
    return shares, prnamt_type
