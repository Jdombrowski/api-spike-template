# API Investigation Spike: SEC EDGAR + Polygon.io

[![CI](https://github.com/Jdombrowski/api-spike-template/actions/workflows/ci.yml/badge.svg)](https://github.com/Jdombrowski/api-spike-template/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/Jdombrowski/api-spike-template/graph/badge.svg)](https://codecov.io/gh/Jdombrowski/api-spike-template)

A data engineering spike demonstrating how to approach **poorly-documented
financial APIs** — profiling real responses, detecting schema drift, building
explicit canonical mappings, and cross-validating across sources.

Built as preparation for working with custodian APIs (Schwab, Fidelity, Pershing)
in a wealth management / RIA data platform context.

---

## The Problem This Addresses

Financial APIs are inconsistently documented:

- Fields appear in some responses but not others with no explanation
- The same concept appears under different names across endpoints
- Units (USD vs. USD thousands) are unstated in field names
- Schemas change with regulatory updates and no changelog

The naive approach — write a scraper, eyeball a few responses, ship — produces
pipelines that silently corrupt data or break on schema changes.

This spike demonstrates a systematic investigation methodology instead.

---
## Example Output
<img width="856" height="800" alt="image" src="https://github.com/user-attachments/assets/718d8356-3b87-4daa-8aef-9e9a9a0862f6" />
<img width="1185" height="790" alt="image" src="https://github.com/user-attachments/assets/86afa8f3-3acb-41a0-97b0-22acf10cbe4e" />
<img width="547" height="344" alt="image" src="https://github.com/user-attachments/assets/64d2a8af-084c-4fc7-8124-fe97af664c6a" />
<img width="1105" height="148" alt="image" src="https://github.com/user-attachments/assets/1d7ef208-76be-4e7c-a9b4-d1594f04a3e3" />






## Quick Start

```bash
make init          # create venv, install deps, scaffold .env

# edit .env:
#   POLYGON_API_KEY=your_key       (free tier at polygon.io)
#   EDGAR_USER_AGENT="Name email"  (required by SEC rate-limiting policy)

make test          # 171 unit tests, no network calls
make run           # ingest entity data for 3 default filers
make run-holdings  # parse 13F positions + cross-validate against Polygon prices
make run-bulk      # download 8 quarters of DERA 13F bulk data (~1.6 GB, cached)
make report        # rich terminal summary of all findings
make export        # write findings to timestamped CSVs in data/exports/
```

To target specific filers:

```bash
make run-cik CIK=0001067983
make run-holdings CIK=0001067983 VALIDATE_TOP=25
make run-bulk QUARTERS=4 CIK="0001067983 0001364742"
```

---

## Project Structure

```
api-spike-template/
├── src/
│   ├── config.py               Configuration: API keys, paths, circuit breaker,
│   │                           rate limit params — all env-overridable
│   ├── pipeline.py             Stage 1: ingest → profile → drift → map →
│   │                           reconcile for entity-level data
│   ├── holdings_pipeline.py    Stage 2: 13F InfoTable XML → positions →
│   │                           Polygon price cross-validation
│   ├── holdings_api.py         Stage 4: FastAPI serving layer — /filers,
│   │                           /holdings, /timeline, /changes, /holders
│   ├── report.py               Query layer + rich terminal report + CSV export
│   ├── ingest/
│   │   ├── http_client.py      Resilient HTTP: exponential backoff + full jitter,
│   │   │                       per-host circuit breaker, proactive rate limiting,
│   │   │                       response snapshot saving
│   │   ├── edgar_client.py     SEC EDGAR: companyfacts, submissions, 13F filing
│   │   │                       index + InfoTable XML retrieval
│   │   ├── edgar_bulk.py       Stage 4: DERA bulk downloader — 8-quarter history
│   │   │                       via quarterly ZIP files, local cache, CIK filter
│   │   └── polygon_client.py   Polygon.io: ticker details, daily OHLCV bars,
│   │                           CUSIP → ticker lookup, name search fallback
│   ├── profile/
│   │   └── profiler.py         Schema profiler: field presence, nullability,
│   │                           type consistency, anomaly detection, markdown export
│   ├── schema/
│   │   ├── canonical_mapper.py Entity field mapping (CONFIRMED/ASSUMPTION/UNMAPPED
│   │   │                       tiers), cross-source reconciliation
│   │   ├── holdings_mapper.py  13F InfoTable XML → CanonicalHolding, quarter-end
│   │   │                       derivation, value unit detection
│   │   └── drift_detector.py   Baseline learning + live schema checking with
│   │                           ERROR/WARNING/INFO severity classification
│   └── storage/
│       └── db.py               SQLite (Postgres-ready): raw_responses (append-only),
│                               canonical_facts, drift_events, reconciliation, holdings
├── tests/
│   ├── test_core.py            Profiler, drift detector, mapper, reconciliation
│   ├── test_pipeline.py        End-to-end pipeline with mocked network calls
│   ├── test_holdings_pipeline.py  XML parsing, cross-validation, DB layer, validate_top
│   ├── test_bulk.py            DERA bulk parser, quarter helpers, DB idempotency,
│   │                           delta/timeline/holders queries
│   ├── test_holdings_api.py    FastAPI endpoints: happy paths, 404s, query params
│   ├── test_report.py          All query functions, report(), export()
│   ├── test_edgar_client.py    Live EDGAR integration tests (make test-edgar)
│   └── test_polygon_client.py  Live Polygon integration tests (make test-polygon)
├── docs/
│   ├── field_mapping.md        Investigation findings: every mapping decision,
│   │                           assumption, and discovered data quality issue
│   └── edgar_profile.md        Generated schema inventory and anomaly report
├── data/
│   ├── baselines/              Drift detector snapshots (committed — tracked over time)
│   ├── dera/                   Cached DERA quarterly ZIPs (gitignored, ~200 MB each)
│   ├── samples/                Raw API response snapshots (gitignored)
│   └── db.sqlite               Investigation database (gitignored)
└── Makefile                    Self-documenting — run `make help` for all targets
```

---

## Investigation Methodology

### Stage 1 — Entity pipeline

1. **Get real data first.** `make run` ingests live responses for a sample of
   filers (large asset managers + mid-size RIAs). Raw responses are saved to
   `data/samples/` and the append-only `raw_responses` table.

2. **Profile before modeling.** The `Profiler` surfaces field presence rates,
   nullability, type inconsistencies, and anomalies across the real sample.
   Results are written to `docs/edgar_profile.md`.

3. **Cross-reference to validate.** EDGAR provides entity metadata. Polygon
   provides the same entities from a separate source. CIK and name agreement
   across both validates our field interpretations. Divergence is an investigation
   finding.

4. **Make explicit mapping decisions.** `canonical_mapper.py` maps every source
   field to a canonical name. Fields are flagged `CONFIRMED` (validated across
   sources), `ASSUMPTION` (inferred, needs validation), or `UNMAPPED` (seen but
   not yet mapped). Unknown fields are surfaced — never silently dropped.

5. **Build drift detection in.** After profiling, a baseline is saved. Every
   subsequent ingestion is checked against it. Schema changes produce `ERROR`,
   `WARNING`, or `INFO` events logged to the database.

### Stage 2 — Holdings pipeline

1. **Parse 13F positions.** `make run-holdings` fetches 13F-HR InfoTable XML
   from EDGAR, parses each `<infoTable>` element into a `CanonicalHolding`, and
   stores all positions with their raw values.

2. **Cross-validate against market prices.** For the top N positions by reported
   value (default 10, to respect Polygon's free-tier rate limit), the pipeline
   resolves CUSIP → ticker, fetches the quarter-end closing price, and computes:

   ```
   ratio = (shares × closing_price) / reported_value_usd
   CLOSE     → within 15%   (rounding + price-date drift acceptable)
   DIVERGENT → outside 15%  (investigate: wrong date, non-equity, ADR ratio)
   NO_PRICE  → ticker unresolved or no bar found
   ```

### Stage 4 — Bulk history + API

1. **Build a multi-quarter dataset without per-filing API calls.** `make run-bulk`
   downloads DERA structured data ZIPs directly from SEC (~200 MB/quarter), parses
   SUBMISSION + INFOTABLE TSVs into the same holdings schema as Stage 2, and filters
   to target CIKs at parse time. ZIPs are cached locally so subsequent runs against
   a different CIK list re-parse from disk.

2. **Serve the dataset via a REST API.** `make serve` starts FastAPI on port 8000
   with auto-generated OpenAPI docs. Endpoints cover per-filer positions, timeline,
   quarter-over-quarter changes (>5% threshold), and cross-filer ownership by CUSIP.

---

## Key Findings

Full narrative in [`docs/field_mapping.md`](docs/field_mapping.md). The most
significant discoveries:

**13F value units are filing-era dependent.**
The SEC specifies USD thousands, but recent EDGAR XML submissions use full dollar
values with no explicit unit tag. Without detection logic, cross-validation produces
a systematic 1000× error — all positions appear DIVERGENT with ratio ≈ 0.001.
The pipeline infers the unit from `value / shares`: an implied price above $5
indicates full USD, below $5 indicates thousands.

**CUSIP is the correct primary key for securities, not issuer name.**
Name-based ticker resolution produced systematic DIVERGENT results because 13F
issuer names use legal entity formats (`"AMAZON COM INC"`, `"ALPHABET INC CL C
CAPITAL STOCK"`) that don't match Polygon's normalized names. CUSIP is a stable
9-character identifier present in every 13F row — use it first, name search as
fallback only.

**Quarter-end dates are often non-trading days.**
December 31 and September 30 frequently fall on weekends. A point-in-time price
query for the exact quarter-end returns no bars, producing NO_PRICE for entire
quarters. The pipeline uses a 7-day lookback and takes the nearest preceding close.

**`absent` and `null` have different semantics.**
An empty `tickers` list means "no tickers" (private company). A missing `tickers`
key means the field wasn't returned (older filing format). The profiler tracks
both separately — collapsing them causes incorrect nullability assumptions downstream.

**`entityType` enum changed in 2022.**
Values shifted from undocumented integers to strings (`"operating"`, `"funds"`,
`"holding"`). Drift detection would catch this on the day it deployed. A pipeline
without it silently broke.

**Entity metadata and financial data live on different endpoints.**
EDGAR's `companyfacts` endpoint returns only XBRL financial data — `cik`,
`entityName`, and `facts`. Ticker symbols, SIC codes, exchange listings, and
state of incorporation are on the `submissions` endpoint instead. Using
`companyfacts` as the entity source means ticker resolution silently returns
empty for every entity, causing all downstream Polygon cross-referencing to be
skipped. Custodian APIs have the same pattern: account balance and account
metadata are often separate endpoints with different auth scopes.

**EDGAR and Polygon use different ticker formats for share classes.**
EDGAR returns class-share tickers with hyphens (`BRK-B`, `BRK-A`). Polygon
requires dot notation (`BRK.B`, `BRK.A`) and returns HTTP 400 with "Invalid
ticker" — not a 404, not a data-quality warning, a hard error. No documentation
mentions the discrepancy. The fix is a one-character substitution, but finding it
requires knowing to look for a format mismatch rather than a missing symbol.

**CIK alone does not identify a publicly-traded entity.**
CIK `0001364742` in EDGAR is "BlackRock Finance, Inc." — a non-public operating
subsidiary — not "BlackRock, Inc." (BLK, CIK `0002012383`). A pipeline that
trusts CIK-to-company lookups without verifying the returned entity name will
silently fetch the wrong entity, find no ticker, and skip all cross-validation.
The correct CIK for the publicly-traded parent must be confirmed from the
`submissions` response, not assumed from external reference data.

**Drift baselines must be invalidated when the source endpoint changes.**
Switching from `companyfacts` to `submissions` as the primary EDGAR source
generated 78 drift events across 3 entities — all noise. The baseline had been
trained on `companyfacts` fields (`entityName`, `cik`); every real `submissions`
field appeared as "new", and `entityName` appeared as "missing". The count looked
alarming but contained zero signal. Baselines need to be versioned or invalidated
whenever the underlying source schema changes, not just when the API changes.

---

## Architecture Notes

**Why append-only `raw_responses`?**
Same reason financial systems use immutable event logs: if the canonical mapper
has a bug or the schema changes, you can replay from raw without re-fetching from
the API. Raw data is the source of truth; canonical data is derived.

**Why SQLite instead of Postgres?**
Same schema, simpler local setup. All SQL is Postgres-compatible — swap
`sqlite3` for `psycopg2` + a connection pool and the rest of the codebase is
unchanged. The `canonical_facts` table structure mirrors a dbt mart model directly.

**Why proactive rate limiting instead of relying on 429s?**
Polygon's free tier allows 5 requests/minute. Reacting to 429s means you've
already burned a request and introduced unpredictable delay. A per-source
minimum interval enforced before each request keeps throughput predictable and
avoids repeated backoff cycles. Override with `POLYGON_RATE_LIMIT_RPM=60` for
paid plans.

**Why `validate_top` instead of validating all positions?**
BlackRock and Vanguard file 13Fs with 4,000+ positions. Validating all of them
at 5 req/min would take hours. Sorting by reported value and validating the top N
covers the economically significant positions — the ones where a data quality
issue would actually matter — while keeping runtime predictable.

**Why bulk DERA download instead of per-filing API calls for history?**
Eight quarters of history via the per-filing path would require thousands of
individual EDGAR requests — one index fetch per filing, one XML fetch per
InfoTable. DERA publishes pre-joined structured datasets: SUBMISSION.tsv joined
with INFOTABLE.tsv in a single ZIP, covering every 13F filer that quarter. One
download replaces thousands of API calls, and the ZIPs are cached locally so
re-running against a different CIK list is free. The tradeoff is download size
(~200 MB/quarter) vs. API rate-limit risk. For 8+ quarters, bulk wins.

---

## Relevance to Custodian API Work

| EDGAR                      | Custodian equivalent                 |
| -------------------------- | ------------------------------------ |
| CIK                        | account_id                           |
| `companyfacts` endpoint    | account balance / position endpoint  |
| 13F InfoTable XML          | position file / holding report       |
| XBRL concept taxonomy      | custodian's proprietary field naming |
| Nightly index updates      | Nightly SFTP file drops              |
| Regulatory schema changes  | Custodian API version updates        |
| USD vs USD_THOUSANDS units | Custodian-specific value multipliers |

The same methodology — profile, triangulate, map explicitly, detect drift —
applies directly to Schwab, Fidelity, and Pershing integrations.
