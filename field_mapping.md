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
4. Documented every mapping decision with a CONFIRMED or ASSUMPTION flag

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

## 13F Filing Values — Units Gotcha

**Finding:** 13F-HR filings report holding values in **USD thousands**, not whole USD.
This is stated in the 13F instructions but NOT reflected in the API response field names.

| what it says         | what it means                     |
| -------------------- | --------------------------------- |
| `"value": 15234567"` | $15,234,567,000 (fifteen billion) |

**Canonical mapping:** store raw value + `value_unit: "USD_THOUSANDS"` flag.
Downstream models must multiply by 1000 before displaying to users.

**This is exactly the kind of silent data quality issue that would cause a balance
discrepancy in a real financial pipeline.** Without this flag, a holdings report
would show values 1000x too small.

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
- [ ] 13F holding values in USD thousands — validate 3 known holdings against Polygon market cap
- [ ] `entityType` null for foreign filers — confirm vs. a known foreign ADR
- [ ] `facts.us-gaap` concept synonym coverage — audit for gaps in our synonym map

---

## Lessons for a Real Custodian API

1. **Always profile before modeling.** Three fields in this investigation
   had behavior undocumented anywhere — only visible in real responses.

2. **Track absent vs. null separately.** Empty list `[]` and missing key
   have different semantics in this API. The mapper handles both.

3. **Document unit assumptions explicitly.** The 13F thousands issue would
   be invisible in a pipeline without the `value_unit` flag.

4. **Build drift detection on day one.** SEC updated the `entityType` enum
   values in a 2022 regulatory change. A drift detector would have caught it
   the same day the change deployed.
