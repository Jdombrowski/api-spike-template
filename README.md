# API Investigation Spike: SEC EDGAR + Polygon.io

A proof-of-concept data engineering spike demonstrating how to approach
**poorly-documented financial APIs** — profiling real responses, detecting
schema drift, building explicit canonical mappings, and cross-validating
across sources.

Built as preparation for working with custodian APIs (Schwab, Fidelity, Pershing)
in a wealth management / RIA data platform context.

---

## The Problem This Addresses

Financial APIs are often inconsistently documented:
- Fields appear in some responses but not others with no explanation
- The same concept appears under different names across endpoints
- Units (USD vs. USD thousands) are not stated in field names
- Schemas change with regulatory updates and no changelog

The naive approach — write a scraper, eyeball a few responses, ship — produces
pipelines that silently corrupt data or break on schema changes.

This spike demonstrates a systematic investigation methodology instead.

---

## Project Structure

```
api-spike-template/
├── src/
│   ├── config.py              Configuration (API keys, paths, circuit breaker params)
│   ├── pipeline.py            Main orchestration: ingest → profile → drift →
│   │                          map → reconcile → report
│   ├── ingest/
│   │   ├── http_client.py     Resilient HTTP: retry + exponential backoff +
│   │   │                      circuit breaker + response snapshot saving
│   │   ├── edgar_client.py    SEC EDGAR API: company facts, concepts, 13F filings
│   │   └── polygon_client.py  Polygon.io: ticker details, daily bars, CIK↔ticker
│   ├── profile/
│   │   └── profiler.py        Schema profiler: nullability, type consistency,
│   │                          anomalies, markdown report generation
│   ├── schema/
│   │   ├── drift_detector.py  Baseline learning + live checking with severity
│   │   │                      classification (ERROR/WARNING/INFO)
│   │   └── canonical_mapper.py Explicit field mapping with CONFIRMED/ASSUMPTION
│   │                           flags, cross-source reconciliation
│   └── storage/
│       └── db.py              SQLite storage (Postgres-ready schema):
│                              raw_responses, canonical_facts, drift_events,
│                              reconciliation
├── data/
│   ├── samples/               Raw API response snapshots (investigation artifacts)
│   ├── baselines/             Drift detector baselines (auto-generated on first run)
│   └── db.sqlite              Investigation database
├── docs/
│   ├── field_mapping.md       Investigation findings: mapping decisions + rationale
│   └── edgar_profile.md       Generated: schema inventory and anomalies
├── tests/
│   └── test_core.py           Unit tests for investigation pipeline
└── .env.example               Environment template (API keys, etc.)
```

---

## Investigation Methodology

**Step 1 — Get real data first.**
Don't trust docs. Run `python -m src.pipeline` to ingest real responses from
multiple entity types. Raw responses are saved to `data/samples/` for offline
analysis.

**Step 2 — Profile before modeling.**
The `Profiler` class collects 50–100 real responses and surfaces:
- Fields present in docs but absent in reality (and vice versa)
- Nullability rates — which fields are reliably populated
- Type inconsistencies — fields that are sometimes `str`, sometimes `int`
- Fields present in some entity types but not others

**Step 3 — Cross-reference to validate.**
EDGAR gives us entity data. Polygon gives us the same entities from a different
source. If CIK and name agree across both, our field interpretation is reliable.
If they diverge, we have an investigation finding.

**Step 4 — Make explicit mapping decisions.**
`canonical_mapper.py` maps every source field to a canonical name. Fields are
flagged as `CONFIRMED` (validated against multiple sources) or `ASSUMPTION`
(interpretation is inferred, needs further validation). Unknown fields are
surfaced — never silently dropped.

**Step 5 — Build drift detection in.**
After profiling, a baseline is saved. Every subsequent ingestion is checked
against it. Schema changes produce `ERROR` (missing required field),
`WARNING` (unexpected null), or `INFO` (new field appeared) events, all
logged to the database.

---

## Setup

```bash
# Clone and install
git clone <repo>
cd api-spike
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env:
#   POLYGON_API_KEY=your_key    (free at polygon.io)
#   EDGAR_USER_AGENT="Name email@you.com"  (required by SEC)

# Run tests
pytest -v

# Run the full spike
python -m src.pipeline

# Investigate specific entities by CIK
python -m src.pipeline --cik 0001067983 0001364742

# View the investigation report
cat docs/edgar_profile.md
```

---

## Key Findings

See [`docs/field_mapping.md`](docs/field_mapping.md) for the full investigation
narrative. Summary of the most significant findings:

**13F values are in USD thousands, not whole USD.**
The API returns `"value": 15234567` for a position worth $15.2 billion.
This is stated in the SEC's 13F instructions but invisible in the API response.
Without explicit handling, a holdings pipeline would display values 1000× too small.

**`absent` and `null` have different semantics.**
An empty `tickers` list means "no tickers" (e.g. private company).
A missing `tickers` key means the field wasn't returned (e.g. older filing format).
The profiler tracks both separately — collapsing them causes incorrect nullability
assumptions.

**`entityType` enum changed in 2022.**
Values shifted from undocumented integers to strings (`"operating"`, `"funds"`,
`"holding"`). A drift detector would have caught this on the day it deployed.
A pipeline without drift detection silently broke.

---

## Architecture Notes

**Why SQLite instead of Postgres?**
Same schema, simpler local setup. All SQL is Postgres-compatible —
swap `import sqlite3` for `psycopg2` + a connection pool and the rest
of the codebase is unchanged. A seed-stage company would start here
before adding infrastructure complexity.

**Why no dbt?**
dbt shines on top of a real warehouse (Snowflake, BigQuery, Redshift) with
multiple transformers. At this scale, the canonical mapper + SQLite views give
the same guarantees with less overhead. The `canonical_facts` table structure
directly mirrors a dbt mart model — the migration path is clear.

**Why append-only `raw_responses`?**
Same reason financial systems use immutable event logs: if the canonical mapper
has a bug or the schema changes, you can replay from raw without re-fetching
from the API. Raw data is the source of truth. Canonical data is derived.

---

## Relevance to Custodian API Work

SEC EDGAR is a proxy for a custodian API (Schwab, Fidelity, Pershing) because:

| EDGAR | Custodian equivalent |
|-------|---------------------|
| CIK | account_id |
| `companyfacts` endpoint | account balance / position endpoint |
| 13F filings | transaction history files |
| XBRL concept taxonomy | custodian's proprietary field naming |
| Nightly index updates | Nightly SFTP file drops |
| Regulatory schema changes | Custodian API version updates |

The same investigation methodology — profile, triangulate, map explicitly,
detect drift — applies directly.
