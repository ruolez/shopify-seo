# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shopify SEO optimization tool — Flask web app that syncs Shopify stores, runs rule-based SEO scoring, and uses AI agents (via OpenRouter) to generate SEO recommendations, optimized descriptions, and image alt text.

## Commands

```bash
# Run everything (Flask + PostgreSQL + Nginx)
docker-compose up

# Rebuild after dependency changes
docker-compose build && docker-compose up

# Restart Flask only (picks up Python changes via volume mount)
docker-compose restart flask-app

# View logs
docker-compose logs -f flask-app

# Direct Flask dev server (requires local Python + running PostgreSQL)
python app/main.py
```

**No test suite exists.** No linter or formatter configured. Verify changes via Docker logs and manual testing through the web UI at `http://localhost:8080` (nginx) or `http://localhost:5000` (direct Flask).

## Architecture

**Stack:** Python 3.11 / Flask 3.1 / PostgreSQL 15 / Vanilla JS / Docker Compose

**Services (docker-compose.yml):**
- `postgres` — port 5432, database `shopify_seo`, persists to `./data/postgres/`
- `flask-app` — Gunicorn (2 workers × 4 threads), port 5000, `./app` volume-mounted for live reload
- `nginx` — port 8080 (external-facing), proxies to flask-app

### Backend (`app/`)

| File | Purpose |
|---|---|
| `main.py` | Flask app factory, blueprint registration, health check |
| `config.py` | Env config: `DATABASE_URL`, `SECRET_KEY`, `ENCRYPTION_KEY` |
| `database.py` | Raw SQL via psycopg2 + RealDictCursor, context managers, table init |
| `auth.py` | bcrypt passwords, Fernet encryption for API keys, default admin setup |
| `shopify_client.py` | Shopify Admin GraphQL API client (products, collections, mutations) |
| `ai_agent.py` | OpenRouter AI agent with tool-calling loop, description generation, product discovery |
| `routes/` | Flask blueprints: auth, dashboard, stores, seo, settings, tasks |
| `services/sync_service.py` | Async (daemon thread) product/collection sync from Shopify |
| `services/seo_analyzer.py` | Rule-based SEO scoring (0-100) for products and collections |
| `services/serp_scraper.py` | Brave Search API integration for competitor SERP context |

### Frontend (`app/static/`, `app/templates/`)

Vanilla JavaScript with no build step. `app.js` provides shared utilities (`apiRequest`, toasts, theme toggle). Page-specific JS files correspond to each route. Templates are Jinja2 with Material Design 3 styling.

## Database

**No ORM** — raw SQL with parameterized queries throughout. All table creation happens in `database.py` on startup.

Key tables:
- `stores` — Shopify connections (access tokens encrypted via Fernet)
- `synced_products` / `synced_collections` — cached Shopify data, UNIQUE on `(store_id, shopify_id)`
- `ai_config` — encrypted OpenRouter + Brave API keys, model selection
- `ai_tasks` — background AI job tracking with JSONB `input`/`result`
- `serp_cache` — TTL-based SERP result cache

Product `images` and `variants` are stored as JSONB columns.

## AI Agent (`ai_agent.py`)

The core AI logic uses an agentic tool-calling loop via OpenRouter (OpenAI-compatible SDK):

- **Main SEO loop** (max 10 iterations): AI calls tools (`get_product_data`, `analyze_seo_score`, etc.) to gather context, then returns structured JSON with recommendations.
- **Description generation**: Separate LLM call with SERP benchmark data to produce HTML descriptions.
- **Product discovery** (`DISCOVERY_TOOL_DEFINITIONS`): When a collection has no products, AI searches `synced_products` by keyword via `search_store_products` tool, selects relevant products, and adds them to manual collections on Shopify via `collectionAddProducts` GraphQL mutation. Smart collections are detected via `ruleSet` query and skipped.

Task types: `product_seo`, `collection_seo`, `store_audit`, `bulk_product_seo`.

## Shopify Integration

`ShopifyClient` wraps the Shopify Admin GraphQL API (version 2024-10). Pagination uses cursor-based approach. Key mutations: `productUpdate`, `collectionUpdate`, `fileUpdate` (image alt text), `collectionAddProducts`. Smart vs manual collections are distinguished by the presence of `ruleSet` on the Collection object.

## Security Notes

- API keys encrypted at rest with Fernet (`ENCRYPTION_KEY` env var)
- Session-based auth with bcrypt password hashing
- Default admin credentials `admin:admin` created on first startup
- All API responses use no-cache headers
