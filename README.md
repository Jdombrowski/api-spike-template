# API Investigation Spike: SEC EDGAR + Polygon.io

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

## Quick Start

```bash
make init          # create venv, install deps, scaffold .env

# edit .env:
#   POLYGON_API_KEY=your_key       (free tier at polygon.io)
#   EDGAR_USER_AGENT="Name email"  (required by SEC rate-limiting policy)

make test          # 94 unit tests, no network calls
make run           # ingest entity data for 3 default filers
make run-holdings  # parse 13F positions + cross-validate against Polygon prices
make report        # rich terminal summary of all findings
make export        # write findings to timestamped CSVs in data/exports/
```

To target specific filers:

```bash
make run-cik CIK=0001067983
make run-holdings CIK=0001067983 VALIDATE_TOP=25
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
│   ├── report.py               Query layer + rich terminal report + CSV export
│   ├── ingest/
│   │   ├── http_client.py      Resilient HTTP: exponential backoff + full jitter,
│   │   │                       per-host circuit breaker, proactive rate limiting,
│   │   │                       response snapshot saving
│   │   ├── edgar_client.py     SEC EDGAR: companyfacts, submissions, 13F filing
│   │   │                       index + InfoTable XML retrieval
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
│   ├── test_report.py          All query functions, report(), export()
│   ├── test_edgar_client.py    Live EDGAR integration tests
│   └── test_polygon_client.py  Live Polygon integration tests
├── docs/
│   ├── field_mapping.md        Investigation findings: every mapping decision,
│   │                           assumption, and discovered data quality issue
│   └── edgar_profile.md        Generated schema inventory and anomaly report
├── data/
│   ├── baselines/              Drift detector snapshots (committed — tracked over time)
│   ├── samples/                Raw API response snapshots (gitignored)
│   └── db.sqlite               Investigation database (gitignored)
└── Makefile                    Self-documenting — run `make help` for all targets
```

---

## Investigation Methodology

**Stage 1 — Entity pipeline**

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

**Stage 2 — Holdings pipeline**

6. **Parse 13F positions.** `make run-holdings` fetches 13F-HR InfoTable XML
   from EDGAR, parses each `<infoTable>` element into a `CanonicalHolding`, and
   stores all positions with their raw values.

7. **Cross-validate against market prices.** For the top N positions by reported
   value (default 10, to respect Polygon's free-tier rate limit), the pipeline
   resolves CUSIP → ticker, fetches the quarter-end closing price, and computes:

   ```
   ratio = (shares × closing_price) / reported_value_usd
   CLOSE     → within 15%   (rounding + price-date drift acceptable)
   DIVERGENT → outside 15%  (investigate: wrong date, non-equity, ADR ratio)
   NO_PRICE  → ticker unresolved or no bar found
   ```

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

---

## Relevance to Custodian API Work

| EDGAR                      | Custodian equivalent                  |
| -------------------------- | ------------------------------------- |
| CIK                        | account_id                            |
| `companyfacts` endpoint    | account balance / position endpoint   |
| 13F InfoTable XML          | position file / holding report        |
| XBRL concept taxonomy      | custodian's proprietary field naming  |
| Nightly index updates      | Nightly SFTP file drops               |
| Regulatory schema changes  | Custodian API version updates         |
| USD vs USD_THOUSANDS units | Custodian-specific value multipliers  |

The same methodology — profile, triangulate, map explicitly, detect drift —
applies directly to Schwab, Fidelity, and Pershing integrations.
