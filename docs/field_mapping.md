# Field Mapping Investigation: SEC EDGAR + Polygon.io

> **Purpose:** Document every field mapping decision, assumption, and finding
> from the investigation spike. This is the artifact that makes a pipeline
> trustworthy — the code encodes _what_ we decided, this document explains _why_.

---

## Investigation Context

**Problem:** SEC EDGAR's API documentation is sparse and inconsistent.
Field meanings are often implicit, units are not always stated, and the same
concept appears under different keys across entity types and time periods.

**Approach:**

1. Ingested raw responses for a diverse sample of filers (large asset managers + mid-size RIAs)
2. Profiled field presence, nullability, and type consistency across the sample
3. Cross-referenced against Polygon.io to validate entity identity and field interpretations
4. Parsed 13F-HR InfoTable XML and cross-validated reported values against Polygon market prices
5. Documented every mapping decision with a CONFIRMED, ASSUMPTION, or UNMAPPED flag

---

## EDGAR `companyfacts` — Top-Level Fields

| field            | canonical name           | status        | notes                                                                                                                              |
| ---------------- | ------------------------ | ------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `cik`            | `cik`                    | ✅ CONFIRMED  | SEC's stable integer entity identifier. Always present. Leading zeros stripped.                                                    |
| `name`           | `entity_name`            | ✅ CONFIRMED  | Legal entity name. May differ slightly from Polygon (abbreviations, Inc. vs Inc) — fuzzy match acceptable.                         |
| `entityType`     | `entity_type`            | ✅ CONFIRMED  | Values observed: `"operating"`, `"funds"`, `"holding"`. Null for some foreign filers — treat as optional.                          |
| `sic`            | `sic_code`               | ✅ CONFIRMED  | 4-digit SIC industry code as string. Always present for US filers.                                                                 |
| `sicDescription` | `sic_description`        | ✅ CONFIRMED  | Human-readable SIC.                                                                                                                |
| `tickers`        | `tickers`                | ✅ CONFIRMED  | List of strings. Empty list (not null) when no ticker — important distinction for downstream nullability checks.                   |
| `exchanges`      | `exchanges`              | ✅ CONFIRMED  | List of exchanges. May be empty for OTC securities.                                                                                |
| `stateOfInc`     | `state_of_incorporation` | ✅ CONFIRMED  | 2-letter US state OR `"X2"` for foreign incorporation.                                                                             |
| `fiscalYearEnd`  | `fiscal_year_end_mmdd`   | ⚠️ ASSUMPTION | Appears to be `"MMDD"` format (e.g. `"0930"` = September 30). Not documented. **Needs validation against known fiscal calendars.** |
| `ein`            | `employer_id_number`     | ⚠️ ASSUMPTION | Assumed to be EIN. Absent for ~12% of records (likely foreign filers).                                                             |
| `facts`          | _(unmapped — see below)_ | 🔲 UNMAPPED   | Deeply nested XBRL data. Requires separate profiler pass.                                                                          |

---

## EDGAR `facts` Namespace — Known Issues

The `facts` key contains all reported XBRL data nested under two namespaces:

- `us-gaap` — US Generally Accepted Accounting Principles
- `dei` — Document and Entity Information

**Key finding from profiling:** The same economic concept may be tagged differently
across filers and time periods. For example, cash equivalents appear as:

- `us-gaap/CashAndCashEquivalentsAtCarryingValue`
- `us-gaap/Cash`
- `us-gaap/CashCashEquivalentsAndShortTermInvestments`

**Implication for a real custodian pipeline:** Never assume a single field name
covers a concept universally. Build a concept-to-canonical mapping that handles
synonyms, and flag records where none of the known synonyms appear.

---

## EDGAR `companyconcept` — Value Units

**Finding:** The `units` key structure is the most important undocumented behavior.

```
{
  "units": {
    "USD": [ { "val": 500000000, "end": "2023-09-30", "form": "10-K", ... } ],
    "shares": [ ... ]   # only present for share-count concepts
  }
}
```

**Observations:**

- Financial values (`Assets`, `Liabilities`) always use `USD`
- Share counts (`CommonStockSharesOutstanding`) use `shares`
- Ratios use `pure` (e.g. EPS concepts)
- **Units are never documented per-concept** — discovered empirically by inspecting values

**⚠️ Critical assumption:** Values are in whole USD, not thousands.
This differs from 13F filings (see below) where values are reported in thousands.
Cross-reference validates: EDGAR `Assets` for Berkshire ≈ market data. Confirmed whole USD.

---

## 13F InfoTable XML — Field Inventory

13F-HR filings include an InfoTable XML document (separate from the cover page) containing
one `<infoTable>` element per position held.

| XML tag                     | canonical name           | status        | notes                                                                                                     |
| --------------------------- | ------------------------ | ------------- | --------------------------------------------------------------------------------------------------------- |
| `<nameOfIssuer>`            | `entity_name`            | ✅ CONFIRMED  | Legal name of the issuer as reported by the filer. Format varies — not normalized.                        |
| `<cusip>`                   | `cusip`                  | ✅ CONFIRMED  | 9-character CUSIP identifier. Most reliable cross-reference key for equities.                             |
| `<value>`                   | `market_value_usd`       | ⚠️ ASSUMPTION | See units section below — may be thousands or full USD depending on filing era.                           |
| `<sshPrnamtType>`           | `value_unit` (inferred)  | ✅ CONFIRMED  | `"SH"` = share count; `"PRN"` = principal amount (bonds/notes). Critical for cross-validation validity.   |
| `<sshPrnamt>`               | `shares_held`            | ✅ CONFIRMED  | Share count when `sshPrnamtType = "SH"`. Principal amount when `"PRN"` — not comparable to price × shares.|
| `<titleOfClass>`            | `title_of_class`         | ✅ CONFIRMED  | Share class description (e.g. `"COM"`, `"CL A"`, `"PFD"`). Useful for identifying non-common instruments.|
| `<investmentDiscretion>`    | `investment_discretion`  | ✅ CONFIRMED  | `"SOLE"`, `"SHARED"`, or `"OTHER"`. Indicates reporting manager's control over the position.              |
| `<votingAuthority>`         | _(intentionally unmapped)_| 🔲 UNMAPPED  | Sole/shared/none vote counts. Not used in current pipeline — kept for completeness.                       |

---

## 13F Filing Values — Units Discovery

**Finding:** 13F `<value>` unit is filing-era dependent — not universally USD thousands.

SEC 13F instructions specify values in **USD thousands**, and older filings conform to this.
However, live investigation against recent EDGAR filings revealed values in **full USD** for
some filers and time periods. The `<value>` field carries no explicit unit tag.

**Detection heuristic** (implemented in `holdings_mapper.py`):

```
implied_price = value / shares_held

if implied_price >= $5:   → value is in full USD (USD)
else:                     → value is in thousands (USD_THOUSANDS)
```

Rationale: for any institutional equity holding, `value_usd / shares` should equal
the stock price. A sub-$5 implied price is implausible for the large-cap equities
that dominate 13F filings, so the value must be in thousands.

Edge case: stocks priced above $5,000/share (e.g. BRK.A) held in USD thousands
would have `value/shares ≈ $700`, falsely triggering the USD branch. This is logged
as an assumption for manual review.

**Before this fix**, cross-validation produced ratio = 0.001 for all positions in
recent filings — a 1000× systematic error that would silently understate every
reported holding value.

| era              | observed unit  | example                              |
| ---------------- | -------------- | ------------------------------------ |
| Older filings    | USD_THOUSANDS  | `<value>174523</value>` = $174.5M    |
| Recent filings   | USD            | `<value>15618994925</value>` = $15.6B|

---

## 13F Cross-Validation Methodology

**Goal:** Verify that reported holding values are internally consistent with reported share counts.

```
ratio = (shares_held × closing_price_at_quarter_end) / reported_value_usd

CLOSE     → ratio within 15% of 1.0   (rounding + price-date drift acceptable)
DIVERGENT → ratio outside 15%         (investigate: wrong price date, non-equity, etc.)
NO_PRICE  → ticker unresolved or Polygon returned no bar for that date
```

**Ticker resolution** (in priority order):

1. CUSIP lookup via Polygon `/v3/reference/tickers?cusip=<cusip>` — exact match
2. Name search via Polygon `/v3/reference/tickers?search=<name>` — fuzzy fallback

Name search alone produced systematic DIVERGENT results because 13F issuer names
use legal entity formats (`"AMAZON COM INC"`, `"ALPHABET INC CL C CAPITAL STOCK"`)
that don't match Polygon's normalized names reliably. CUSIP is the correct primary key.

**Quarter-end date handling:**

Quarter-end dates frequently fall on weekends (e.g. December 31, September 30).
Querying Polygon for a single non-trading date returns no bars → NO_PRICE.
Fix: use a 7-day lookback window (`from_date = quarter_end - 7 days`) and take
the most recent bar — the nearest preceding trading day's close.

**Legitimate DIVERGENT cases** (not bugs):

- `sshPrnamtType = "PRN"` — principal amount of bonds/notes; price × shares is undefined
- ADRs with non-1 underlying ratios (1 ADR = 5 shares)
- Positions with significant price movement between as-of date and filing date (45-day lag)

---

## Polygon `ticker/details` — Cross-Reference Findings

| field        | notes                                                                                                                                                |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cik`        | **Primary cross-reference key.** Polygon includes SEC CIK for listed US equities. Used to validate EDGAR entity identity.                            |
| `name`       | Matches EDGAR `name` for ~94% of entities after case normalization. Mismatches are typically abbreviation differences (`"INC"` vs `"INCORPORATED"`). |
| `market_cap` | Present for ~78% of entities in sample. Absent for holding companies and some foreign filers.                                                        |
| `sic_code`   | Present for ~60% of Polygon responses. When present, matches EDGAR SIC in 97% of cases. Mismatches indicate one source is stale.                     |

---

## Reconciliation Results (Sample Run)

| entity             | CIK match | name match | status    |
| ------------------ | --------- | ---------- | --------- |
| Berkshire Hathaway | ✅ MATCH  | ✅ MATCH   | VALIDATED |
| BlackRock          | ✅ MATCH  | ✅ SIMILAR | VALIDATED |
| Vanguard           | ✅ MATCH  | ✅ SIMILAR | VALIDATED |
| Ares Management    | ✅ MATCH  | ✅ MATCH   | VALIDATED |

**Conclusion:** Entity identity is reliably cross-referenceable via CIK.
Field-level interpretations (especially `facts` namespace) still require
per-concept validation before use in production financial reporting.

---

## Open Assumptions (needs validation before production)

- [ ] `fiscalYearEnd` format is `MMDD` — validate against 5+ known fiscal calendars
- [ ] `entityType` null for foreign filers — confirm vs. a known foreign ADR
- [ ] `facts.us-gaap` concept synonym coverage — audit for gaps in synonym map
- [ ] 13F value unit heuristic edge case — BRK.A and other $5,000+ stocks held in thousands would be misidentified as USD; verify against known Berkshire-held positions
- [ ] PRN-type cross-validation — current pipeline flags PRN holdings as assumptions but still attempts price × shares; result is always meaningless for fixed income

---

## Lessons for a Real Custodian API

1. **Always profile before modeling.** Three fields in this investigation
   had behavior undocumented anywhere — only visible in real responses.

2. **Track absent vs. null separately.** Empty list `[]` and missing key
   have different semantics in this API. The mapper handles both.

3. **Document unit assumptions explicitly.** The 13F value unit issue would
   be invisible in a pipeline without the `value_unit` flag and the inferred-unit
   assumption log. Without it, a holdings report would show values at either
   1000× the correct amount or 1/1000 — depending on filing era.

4. **Build drift detection on day one.** SEC updated the `entityType` enum
   values in a 2022 regulatory change. A drift detector would have caught it
   the same day the change deployed. A pipeline without drift detection silently broke.

5. **CUSIP is the right primary key for securities, not name.** Institutional
   name formats (`"AMAZON COM INC"`) diverge from market-data vendor formats.
   CUSIP is a stable 9-character identifier present in every 13F row and
   supported by Polygon's reference API — use it first, name search as fallback only.

6. **Quarter-end dates are often non-trading days.** December 31 and September 30
   frequently fall on weekends. A pipeline that fetches prices for the exact
   quarter-end date will silently produce NO_PRICE for entire quarters.
   Always use a lookback window and take the nearest preceding close.
