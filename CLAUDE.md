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

**Tests:** pure-logic specs live beside the code as `app/services/*_spec.py` and run inside the container:

```bash
docker compose exec -T flask-app python -m pytest app/services/*_spec.py -q
```

They are named `_spec.py`, so pytest only collects them when passed by path. No linter or formatter is configured. Python changes need `docker compose restart flask-app`; verify through the web UI at `http://localhost:8080` (nginx) or `http://localhost:5000` (direct Flask).

## Writing to Shopify — the apply pipeline

Every change to a page's title, body or meta tags goes through `app/services/apply_pipeline.py`:

```python
apply_item_changes(store_id, item_type, item_id, fields, source=..., change_type=..., client=...)
```

- `item_type`: `product | collection | article | page`. `fields` uses canonical keys for all types: `title`, `body`, `meta_title`, `meta_description`, `summary` (articles). Tags and other extras go in `extra_fields`.
- It reads the live item first, refuses when Shopify holds edits the mirror has not seen, refuses rewrites that drop spec tables, verdict/FAQ sections, internal links or the resolved primary keyword, mirrors what Shopify stored, re-scores, and records full before/after values in `seo_change_events`.
- A refusal raises `ApplyRefused` (a `ValueError`); routes return it with `refusal_response()` (HTTP 409). `force: true` in the request body means "apply anyway"; `apiRequest` in `app.js` asks the user centrally.
- Undo: `revert_change(event_id)` / `POST /api/changes/<id>/revert`. History per item: `GET /api/changes`.
- Pass `restore_media=False` for hand edits and restyles (a person may remove an image on purpose).
- New code that writes content must use this function — do not call `update_product` / `update_collection` / `update_article` / `update_page` directly for content fields.
- The product/collection FAQ schema metafield has one writer: `apply_pipeline.set_faq_schema` (publishes only questions visible on the page).

## Publishing articles

Blog posts and Product News share one calendar and one review gate (`app/services/publish_gate.py`). New drafts take `next_open_slot(store_id)`; the schedulers publish only sessions with `reviewed_at` set (the Approve button in each Scheduled tab). Nothing publishes unattended without approval.

Blog auto mode (Blog → Create Post → "Auto mode: import briefs") imports a text file of briefs (`app/services/blog_import.py` parses it; `POST /api/blog/import/preview` then `/import/schedule`). With "Approve on import" ticked the import is the approval (`reviewed_at` set at insert). The file's H1, SEO title, meta description and URL slug live in `article_wizard_sessions.locked_fields` and win over the AI: the outline, the QA gate (`validate_named_fields(..., locked=)` reports, never rewrites) and publish all run `apply_locked_fields`; a locked slug that Shopify reports as taken fails the session instead of getting a `-1` suffix. Keywords keep the file's order (first = primary). Any new step that writes a title, meta or handle for a wizard session must call `apply_locked_fields` too.

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

Vanilla JavaScript with no build step. `app.js` provides shared utilities (`apiRequest`, toasts, theme toggle); `ui.js` holds shared components (modals, tabs, store selector, ⌘K palette, `UISerp` search-snippet renderer). Page-specific JS files correspond to each route. Templates are Jinja2.

Visual system: `DESIGN.md` (Nov 2026 "Results Page" redesign; product context in `PRODUCT.md`). Navigation lives in one place, `app/nav.py` (groups Today / Fix / Create / Research / Setup): the sidebar, page header (group, title, purpose line, related links), browser title and ⌘K all read it via the `inject_nav` context processor in `main.py`. A new page = an entry in `nav.py`; its template only fills `content` (plus `page_actions` if needed). Page-level buttons go in the header via `data-page-action` on the element. The last block of `style.css` ("Results Page component layer") is the shared component vocabulary; keep new pages on its classes.

## Scores

`seo_score` is the **on-page** score (content + keyword alignment) — what an edit can change. Search performance is reported beside it as `search_score`, never blended into the headline. `unified_score` keeps the three-pillar blend for ranking opportunities and before/after comparisons. One colour scale everywhere: 80 / 50 (`scoreColor` in `app.js`). Queue rows are ranked in one unit, estimated monthly clicks (`ctr_benchmarks.estimate_click_gain`).

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
