Mario — Heavy Equipment Web Scraping & Automation
![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![Scraping](https://img.shields.io/badge/Web%20Scraping-Playwright%20%7C%20BeautifulSoup-2496ED) ![Automation](https://img.shields.io/badge/Automation-n8n%20%7C%20Monitoring-orange) ![API](https://img.shields.io/badge/API-FastAPI-009688)
Mario is a Python backend for automated heavy-equipment listing discovery, web scraping, monitoring, matching, and notifications. It collects listing information from MachineryTrader and supported Facebook Marketplace scraping providers, filters machines against a configurable watchlist, tracks duplicate and updated listings, and prepares Telegram alerts via n8n.
> This is an equipment-marketplace monitoring system, **not** a universal scraping platform or no-code scraping UI.
Key features
Capability	Implementation
Web scraping and browser automation	Playwright-based MachineryTrader collector with BeautifulSoup, HTML, and JSON-LD parsing
Multi-source extraction	Facebook Marketplace collection through Apify or Bright Data provider integrations
Scheduled automation	Async Facebook monitoring worker with configurable intervals, non-overlapping cycles, backoff, and shutdown handling
Regional searches	Configurable US and Canadian regions and search queries
Intelligent filtering	Deterministic matching by manufacturer, model, aliases, negative terms, and price plausibility; ACCEPT / REVIEW / REJECT classification
Data persistence	Async SQLAlchemy, PostgreSQL, duplicate detection, listing updates, and price history
Notification automation	FastAPI alert outbox, importable n8n workflow, and Telegram delivery
Diagnostics	Structured logs and per-scan metrics, including counts, errors, and scan latency
Data pipeline
```text
MachineryTrader (Playwright + BeautifulSoup)   Facebook Marketplace (Apify / Bright Data)
                      \                          /
                       \                        /
                        Normalized listing data
                                  |
                       YAML watchlist + matcher
                         ACCEPT/REVIEW/REJECT
                                  |
                     PostgreSQL + deduplication
                       + price-change tracking
                                  |
                            Alert outbox
                                  |
                          FastAPI alert API
                                  |
                           n8n workflow
                                  |
                         Telegram messages
```
Automation scope: The continuous worker in this repository monitors Facebook Marketplace. MachineryTrader scraping runs through a separate one-shot worker, which you can schedule externally. Telegram forwarding is implemented as an n8n workflow, not a continuously running built-in Telegram worker.
Tech stack
Python · asyncio · FastAPI · Pydantic · Playwright · BeautifulSoup · HTTPX · Apify · Bright Data · PostgreSQL · SQLAlchemy · Alembic · YAML · n8n · Telegram · pytest
Quick start
1. Clone and install
Requirements: Python 3.10+, PostgreSQL for persistent scans, and optional provider API credentials for Facebook Marketplace.
```bash
git clone https://github.com/mohameddsalmann/mario.git
cd mario
python -m venv .venv
```
Activate `.venv`:
```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1
```
```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```
2. Configure `.env`
```bash
# macOS / Linux
cp .env.example .env

# Windows PowerShell
# Copy-Item .env.example .env
```
The provided `.env.example` uses Docker-network hostnames (`postgres`, `redis`). If you run Python on your computer rather than inside that Docker network, adjust the hostnames. Example for local PostgreSQL:
```dotenv
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/equipment_monitor
MONITOR_INTERVAL_SECONDS=120

# Facebook Marketplace provider: apify or brightdata
FACEBOOK_PROVIDER=apify
APIFY_TOKEN=your_apify_token

# For Bright Data instead:
# FACEBOOK_PROVIDER=brightdata
# BRIGHTDATA_API_KEY=your_brightdata_key
# BRIGHTDATA_DATASET_ID=your_dataset_id

# Configure in the n8n / Telegram environment as needed
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```
For local development, a PostgreSQL 16 database can be started with:
```bash
docker run --name mario-postgres -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=equipment_monitor -p 5432:5432 postgres:16-alpine
```
The example credentials are only for local development. The repository's `docker-compose.test.yml` provisions a separate test database, not an application stack.
3. Scrape MachineryTrader
```bash
# One-off scan; no database writes
python -m app.workers.run_once --query "CAT wheel loader" --max-pages 1 --no-save

# One-off scan; persist matched listings (requires PostgreSQL)
python -m app.workers.run_once --query "CAT wheel loader" --max-pages 1
```
Use `--no-headless` to display the browser while scraping. Live scraping depends on the website's current markup, accessibility, and rate limits.
4. Collect Facebook Marketplace listings
Configure an Apify token (or Bright Data credentials) first:
```bash
python -m app.workers.facebook_run_once --provider apify --regions us_texas --queries "CAT 966H" --max-results 10 --no-save
```
Remove `--no-save` to persist results. Provider access may have usage costs and may be subject to third-party access restrictions.
5. Enable continuous monitoring
```bash
# Run three monitoring cycles, 120 seconds apart where possible
python -m app.workers.monitor --interval 120 --provider apify --regions us_texas --queries "CAT 966H" --max-results 10 --max-cycles 3

# To monitor continuously, omit --max-cycles
```
Monitoring uses non-overlapping scans, failure backoff, and graceful shutdown handling. The polling interval does not guarantee a matching listing will be discovered within that time.
6. Launch FastAPI
With PostgreSQL configured and running:
```bash
uvicorn app.main:app --reload
```
Local API: http://localhost:8000/docs
Endpoint	Purpose
`GET /health`	Health status
`GET /api/alerts/pending?limit=25`	Get unsent notification events
`POST /api/alerts/{alert_id}/delivered`	Confirm delivery
`POST /api/alerts/{alert_id}/fail`	Record a delivery failure; JSON body such as `{"error":"Telegram request failed"}`
n8n → Telegram workflow automation
The repository includes `config/n8n_telegram_workflow.json`. Import it into n8n and configure credentials and network access.
The schedule trigger polls approximately once per minute.
An HTTP request fetches pending alerts from FastAPI.
The workflow passes each event to a Telegram bot.
The workflow attempts to acknowledge delivered events or record failures.
Before production: Review the imported workflow's item-splitting and failure branches in your n8n version. Set valid Telegram credentials and chat ID. The example API URL `http://localhost:8000` must be replaced if n8n executes in Docker or on a different host; `localhost` refers to the n8n runtime's own network environment. The alert API lacks a documented authentication layer, so do not expose it publicly without suitable protections.
Configuration files
Path	Function
`config/watchlist.yaml`	Search terms, target manufacturers and models, aliases, exclusions, matching thresholds
`config/facebook_regions.yaml`	Facebook Marketplace regional search grid
`.env.example`	Environment variables and service settings
`config/n8n_telegram_workflow.json`	n8n notification workflow template
The configured watchlist includes Caterpillar (CAT) and CASE wheel loaders and backhoe loaders. Default matching thresholds are 0.70 (ACCEPT) and 0.50 (REVIEW).
Project structure
```text
app/
  api/           Alert endpoints
  database/      Models and async DB sessions
  matcher/       Manufacturer/model normalization and matching
  providers/     MachineryTrader, Facebook / Apify / Bright Data
  services/      Listing persistence and alert management
  workers/       One-shot scans and continuous Facebook monitoring
config/          Watchlist, region grid, n8n workflow
alembic/         Database migrations
scripts/         Telegram setup helpers
tests/           Scraping, matching, API, deduplication tests
```
Tests
Integration tests expect PostgreSQL 16+, available via the included test-only Compose file:
```bash
docker compose -f docker-compose.test.yml up -d
python -m pytest -q
docker compose -f docker-compose.test.yml down
```
Set `TEST_DATABASE_URL` if your test database differs from `postgresql+asyncpg://test_user:test_password@localhost:5433/equipment_monitor_test`. The repository has tests, but their execution results are not verified by this README.
Scope and limitations
Only the implemented provider adapters are supported. A `MACHINIO_ENABLED` configuration option exists, but no Machinio scraping provider is present in the current codebase.
Facebook scraping depends on Apify or Bright Data credentials and third-party provider compatibility.
Marketplace layouts, anti-automation mechanisms, quotas, and upstream outages can disrupt collection.
This is rules-based equipment matching, not proof of an LLM-powered or machine-learning model.
Keep tokens and credentials outside version control. Respect target-site terms, access controls, rate limits, and applicable privacy laws.
---
Focus: Web Scraping · Python Automation · Marketplace Monitoring · Data Extraction · Deduplication · Workflow Automation · n8n · Telegram Alerts
Repository: https://github.com/mohameddsalmann/mario
