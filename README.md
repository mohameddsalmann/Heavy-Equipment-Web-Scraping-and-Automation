# Mario — Heavy Equipment Web Scraping & Automation

**Automated marketplace discovery · Multi-source data extraction · Continuous monitoring · Workflow-driven alerts**

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Web Scraping](https://img.shields.io/badge/Web%20Scraping-Playwright%20%2B%20BeautifulSoup-45BA4B)
![Automation](https://img.shields.io/badge/Automation-Async%20Monitoring%20%2B%20n8n-EA4B71)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-4169E1?logo=postgresql&logoColor=white)

**Mario** is a Python-based **web scraping and automation backend** that discovers heavy-equipment listings, standardizes data from multiple marketplaces, matches equipment against a configurable watchlist, and tracks listings over time. When relevant listings or price changes are detected, it prepares notification events for an **n8n → Telegram** automation workflow.

Instead of manually checking marketplaces, Mario connects **data collection, filtering, persistence, monitoring, and alert delivery** into a repeatable pipeline.

> **Project scope:** A purpose-built heavy-equipment marketplace monitor—not a universal scraping service, no-code scraper, or AI/LLM-powered matching engine.

## Highlights

- **Browser-based web scraping:** Collect MachineryTrader listings using **Playwright**, with **BeautifulSoup** and **JSON-LD/HTML** extraction strategies.
- **Multi-source discovery:** Integrate Facebook Marketplace listing collection through **Apify** or **Bright Data** providers.
- **Automated monitoring:** Run recurring, non-overlapping Facebook scans with configurable polling intervals, bounded concurrency, failure backoff, and graceful shutdown.
- **Configurable matching:** Identify relevant **Caterpillar (CAT)** and **CASE** equipment using make/model aliases, exclusion rules, confidence thresholds, and price-anomaly checks.
- **Change detection:** Persist standardized listings, deduplicate repeated discoveries, and track price updates in **PostgreSQL**.
- **Event-driven notifications:** Expose pending alerts via **FastAPI**, then use **n8n** to deliver Telegram messages and acknowledge outcomes.
- **Operational visibility:** Capture scan results, errors, classification counts, and latency metrics with structured logging.

## Architecture

```text
            DATA COLLECTION / WEB SCRAPING
┌──────────────────────────┐   ┌────────────────────────────┐
│ MachineryTrader          │   │ Facebook Marketplace       │
│ Playwright + BeautifulSoup│   │ Apify / Bright Data         │
└─────────────┬────────────┘   └─────────────┬──────────────┘
              └────────────────┬─────────────┘
                               ▼
                   Normalized listing data
                               │
                               ▼
                 YAML watchlist + rules matcher
                   ACCEPT / REVIEW / REJECT
                               │
                               ▼
                 PostgreSQL + async SQLAlchemy
                 Deduplication / price history
                               │
                               ▼
                        Alert event outbox
                               │
                               ▼
                    FastAPI /api/alerts/*
                               │
                               ▼
                       n8n automation
                               │
                               ▼
                     Telegram notifications
```

**Automation boundary:** The included continuous-monitoring worker runs **Facebook Marketplace** scans. **MachineryTrader** has a separate one-shot worker; use an external scheduler if recurring scans are needed there. Telegram delivery is orchestrated through the included **n8n workflow**, not a built-in background Telegram sender.

## Core capabilities

| Component | Implementation | Engineering purpose |
| --- | --- | --- |
| **Web scraping** | Playwright browser automation; BeautifulSoup; HTML/JSON-LD parsing | Extract and structure MachineryTrader equipment listings |
| **Provider integrations** | Facebook Marketplace via Apify / Bright Data; HTTPX | Connect multiple acquisition sources through a normalized data model |
| **Monitoring automation** | Async polling, non-overlapping cycles, backoff, graceful shutdown | Reduce manual searches and handle repeated collection cycles |
| **Regional discovery** | YAML-defined US/Canada regions and configurable search queries | Target specific equipment markets |
| **Listing intelligence** | Manufacturer/model normalization, aliases, negative terms, price plausibility | Classify results as `ACCEPT`, `REVIEW`, or `REJECT` |
| **Persistence & deduplication** | PostgreSQL, async SQLAlchemy, Alembic | Track new listings, updates, and price history |
| **Alert automation** | FastAPI outbox + n8n + Telegram | Route relevant listing events to a messaging channel |
| **Diagnostics & tests** | structlog, scan metrics, pytest, pytest-asyncio | Inspect pipeline behavior and verify core logic |

## Technology stack

**Language & backend:** Python, asyncio, FastAPI, Pydantic  
**Scraping & integrations:** Playwright, BeautifulSoup, HTTPX, Apify, Bright Data  
**Data & configuration:** PostgreSQL, SQLAlchemy, Alembic, YAML  
**Automation & messaging:** Continuous polling worker, n8n, Telegram Bot API  
**Testing & diagnostics:** pytest, pytest-asyncio, structlog

## Getting started

### 1. Clone and install

**Prerequisites:** Python 3.10+, PostgreSQL for persistent scans, and provider credentials for Facebook Marketplace scans.

```bash
git clone https://github.com/mohameddsalmann/mario.git
cd mario
python -m venv .venv
```

Activate the virtual environment on macOS/Linux:

```bash
source .venv/bin/activate
```

Or on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the dependencies and Playwright's Chromium browser:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Configure environment variables

For macOS/Linux:

```bash
cp .env.example .env
```

For Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Set your local configuration in `.env`:

```dotenv
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/equipment_monitor
MONITOR_INTERVAL_SECONDS=120

# Facebook Marketplace: Apify
FACEBOOK_PROVIDER=apify
APIFY_TOKEN=your_apify_token

# Or use Bright Data instead:
# FACEBOOK_PROVIDER=brightdata
# BRIGHTDATA_API_KEY=your_brightdata_key
# BRIGHTDATA_DATASET_ID=your_dataset_id

# Telegram / n8n setup
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

**Note:** The committed `.env.example` references Docker-network hosts such as `postgres` and `redis`. Replace these hostnames when running the app directly on your machine. Do not commit actual tokens or passwords.

For local development, you can start a PostgreSQL 16 instance:

```bash
docker run --name mario-postgres -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=equipment_monitor -p 5432:5432 postgres:16-alpine
```

These example credentials are for **local development only**. The repository's `docker-compose.test.yml` creates a separate **test database**, not a complete production stack.

### 3. Run a MachineryTrader scraping job

```bash
# Inspect results without persisting them
python -m app.workers.run_once --query "CAT wheel loader" --max-pages 1 --no-save

# Save matched listings to PostgreSQL
python -m app.workers.run_once --query "CAT wheel loader" --max-pages 1
```

Use `--no-headless` to make the Chromium browser visible during a scan. Actual results depend on the source website's availability, markup, and access controls.

### 4. Run a Facebook Marketplace scan

Configure your **Apify** token (or Bright Data credentials) first:

```bash
python -m app.workers.facebook_run_once \
  --provider apify \
  --regions us_texas \
  --queries "CAT 966H" \
  --max-results 10 \
  --no-save
```

Remove `--no-save` to persist listings. Apify / Bright Data can have account requirements, usage limits, and costs.

### 5. Automate recurring monitoring

```bash
# Demonstration: up to three Facebook scan cycles
python -m app.workers.monitor \
  --interval 120 \
  --provider apify \
  --regions us_texas \
  --queries "CAT 966H" \
  --max-results 10 \
  --max-cycles 3
```

Omit `--max-cycles` to continue monitoring until stopped. The worker prevents overlapping scans and backs off after failures; **the polling interval is not a guaranteed discovery latency**.

### 6. Start the FastAPI service

With the database configured and running:

```bash
uvicorn app.main:app --reload
```

- **Interactive API docs:** http://localhost:8000/docs
- **Health check:** http://localhost:8000/health

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Check service health |
| `GET /api/alerts/pending?limit=25` | Retrieve pending notification events |
| `POST /api/alerts/{alert_id}/delivered` | Mark an alert as delivered |
| `POST /api/alerts/{alert_id}/fail` | Record a delivery failure (e.g. `{"error":"Telegram request failed"}`) |

## n8n → Telegram workflow automation

The project contains an importable workflow: **[`config/n8n_telegram_workflow.json`](config/n8n_telegram_workflow.json)**.

```text
n8n Schedule Trigger (every minute)
                 │
                 ▼
      GET /api/alerts/pending
                 │
                 ▼
        Process pending events
                 │
                 ▼
          Telegram Bot API
                 │
         ┌───────┴────────┐
         ▼                ▼
    Delivered?          Failed?
         │                │
         ▼                ▼
 POST /delivered      POST /fail
```

**Setup:** Import the JSON into n8n, connect your Telegram bot, configure the chat ID, and update the FastAPI URL so n8n can reach the service.

**Before deploying:** Validate item splitting, field mappings, and success/failure branches with your n8n version. The example uses `http://localhost:8000`; when n8n runs in Docker or on another machine, `localhost` points to **that environment**, not necessarily the host running FastAPI. Also restrict access to the API—the checked-in app does not implement an authentication layer for its alert endpoints.

## Configuration

| File | Responsibility |
| --- | --- |
| [`config/watchlist.yaml`](config/watchlist.yaml) | Search queries, target models, aliases, exclusions, confidence thresholds |
| [`config/facebook_regions.yaml`](config/facebook_regions.yaml) | Regional Facebook Marketplace search grid for the US and Canada |
| [`.env.example`](.env.example) | App, database, provider, and notification settings |
| [`config/n8n_telegram_workflow.json`](config/n8n_telegram_workflow.json) | n8n → Telegram workflow template |

The default watchlist targets **Caterpillar (CAT)** and **CASE** wheel loaders and backhoe loaders. Default classification thresholds are **0.70 for `ACCEPT`** and **0.50 for `REVIEW`**. Matching is **rules-based and deterministic**, not an LLM inference pipeline.

## Repository structure

```text
app/
├── api/          # FastAPI alert endpoints
├── database/     # SQLAlchemy models and async DB sessions
├── matcher/      # Manufacturer/model normalization and matching rules
├── providers/    # MachineryTrader + Facebook / Apify / Bright Data
├── services/     # Listing persistence, deduplication, alerts
└── workers/      # One-shot collection jobs and continuous monitoring
config/           # YAML watchlist, region grid, n8n workflow
alembic/          # Database migrations
scripts/          # Telegram-related helper scripts
tests/            # Scraping, matching, persistence, and API tests
```

## Testing

Integration tests expect **PostgreSQL 16+**. Start the dedicated test database and run pytest:

```bash
docker compose -f docker-compose.test.yml up -d
python -m pytest -q
docker compose -f docker-compose.test.yml down
```

You can override the test database URL with `TEST_DATABASE_URL`. Otherwise, tests use:

```text
postgresql+asyncpg://test_user:test_password@localhost:5433/equipment_monitor_test
```

> Tests exist in the repository, but no passing test result is claimed here without running the suite.

## Current scope & operational considerations

- **Implemented sources:** MachineryTrader and Facebook Marketplace provider integrations (Apify / Bright Data). The `MACHINIO_ENABLED` setting does **not** mean a Machinio provider has been implemented.
- **Source reliability:** Layout changes, access restrictions, rate limits, provider quotas, and outages can interrupt collection.
- **Automation boundaries:** Facebook has a continuous worker; MachineryTrader is one-shot unless scheduled externally; Telegram notifications require n8n configuration.
- **Production hardening:** Add API authentication, HTTPS, secret management, appropriate network isolation, and monitoring before exposing the service publicly.
- **Responsible scraping:** Respect marketplace terms, access controls, rate limits, and applicable privacy and data-protection requirements.

---

**Keywords:** `web-scraping` · `python-automation` · `playwright` · `beautifulsoup` · `marketplace-monitoring` · `data-extraction` · `fastapi` · `postgresql` · `n8n` · `telegram-bot`

**Repository:** https://github.com/mohameddsalmann/mario
