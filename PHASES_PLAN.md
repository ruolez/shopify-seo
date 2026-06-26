# SEO Platform Evolution — Implementation Plan

## Phase 1: Google Search Console Integration + SEO Progress Dashboard

### 1.1 GSC OAuth & Configuration
- [x] Add `google-auth-oauthlib` and `google-api-python-client` to `requirements.txt`
- [x] Add GSC config fields to settings page (Google Cloud Client ID, Client Secret)
- [x] Store GSC OAuth credentials encrypted in `ai_config` table (new columns: `gsc_client_id_enc`, `gsc_client_secret_enc`)
- [x] Settings UI: GSC configuration section with client ID/secret inputs

### 1.2 GSC OAuth Flow
- [x] Create `gsc_credentials` table (store_id, access_token_enc, refresh_token_enc, token_expiry, gsc_property, connected_email, created_at, updated_at)
- [x] Create `app/services/gsc_service.py` — OAuth flow, token refresh, API client factory
- [x] Implement OAuth initiation endpoint: `GET /api/gsc/auth/start` (redirects to Google consent)
- [x] Implement OAuth callback endpoint: `GET /api/gsc/auth/callback` (exchanges code for tokens, stores encrypted)
- [x] Implement property selection: `GET /api/gsc/properties` (list verified sites), `POST /api/gsc/properties/select` (save chosen property)
- [x] Implement disconnect: `DELETE /api/gsc/auth` (revoke tokens, delete credentials)

### 1.3 GSC Data Storage & Pulling
- [x] Create `gsc_daily_metrics` table with UNIQUE constraint on (store_id, date, page, query, device, country)
- [x] Create `gsc_data_pulls` table (id, store_id, pull_type, date_from, date_to, rows_pulled, status, error_message, started_at, completed_at)
- [x] Implement data pull service: fetch daily metrics from GSC API, UPSERT into `gsc_daily_metrics`
- [x] Implement backfill: pull last 90 days of data on first connection
- [x] Implement incremental pull: daily pull of last 5 days (data finalizes with 2-3 day delay)
- [x] Background thread for scheduled pulls (every 6 hours via PullScheduler)

### 1.4 GSC Routes & API Endpoints
- [x] Create `app/routes/gsc_routes.py` blueprint
- [x] `GET /gsc` — render GSC dashboard template
- [x] `GET /api/gsc/status` — connection status, last pull time, property info
- [x] `POST /api/gsc/pull` — trigger manual data pull
- [x] `GET /api/gsc/overview` — aggregated metrics with period-over-period comparison
- [x] `GET /api/gsc/top-pages` — top pages by clicks/impressions
- [x] `GET /api/gsc/top-queries` — top queries by clicks/impressions
- [x] `GET /api/gsc/page-detail?page=URL` — all queries driving traffic to a specific page
- [x] `GET /api/gsc/query-detail?query=KEYWORD` — all pages ranking for a specific query
- [x] `GET /api/gsc/opportunities` — high impressions + low CTR pages (quick wins)
- [x] `GET /api/gsc/cannibalization` — queries with 2+ pages ranking (keyword overlap)

### 1.5 GSC Dashboard Frontend
- [x] Create `app/templates/gsc.html` — dashboard layout with cards, filters, tables, charts
- [x] Create `app/static/js/gsc.js` — dashboard interactivity
- [x] Overview cards: total clicks, impressions, avg CTR, avg position (with trend arrows vs previous period)
- [x] Date range picker (7d, 28d, 90d)
- [x] Top Pages table: URL, clicks, impressions, CTR, position with clickable detail
- [x] Top Queries table: query, clicks, impressions, CTR, position with clickable detail
- [x] Opportunities tab: high-impression/low-CTR pages with potential clicks and suggested action
- [x] Cannibalization tab: overlapping keyword/page pairs with severity badges
- [x] Page detail modal: click a page → see all queries driving traffic
- [x] Query detail modal: click a query → see all pages ranking
- [x] Trend charts: clicks/impressions over time (SVG line chart with toggles)
- [x] Add "Search Console" nav item to sidebar under new "Measure" section

### 1.6 SEO Progress Dashboard
- [x] Create `seo_score_history` table (id, store_id, item_type, item_id, shopify_id, title, score, breakdown, recorded_at)
- [x] Implement score snapshot service: iterate all products/collections, record current SEO scores
- [x] `POST /api/dashboard/snapshot` — trigger manual score snapshot
- [x] `GET /api/dashboard/score-trends` — score distribution over time (avg score, good/ok/poor counts)
- [x] `GET /api/dashboard/improvements` — items with biggest score changes (positive and negative)
- [x] SEO Scores tab in GSC dashboard with score trend chart and improvements table
- [x] GSC summary card on main dashboard page (clicks, impressions, CTR, position)
- [x] CSS: metric cards, clickable rows, badge-info styles

### 1.7 Phase 1 Enhancements
- [x] Device filter (Desktop/Mobile/Tablet) on all GSC queries and dashboard
- [x] Country filter with dynamic country list loaded from stored data
- [x] URL-to-product matching — `GET /api/gsc/page-matches` maps GSC pages to synced products/collections by handle
- [x] Available countries endpoint — `GET /api/gsc/countries`
- [x] Automated daily score snapshots via `SnapshotScheduler` (runs every 24 hours, skips if already snapped today)
- [x] `_build_filters()` helper for clean device/country SQL injection across all queries

### 1.8 Enhanced Cannibalization (Detection → Analysis → Fix → Apply → Verify)
- [x] URL-to-product/collection matching in cannibalization results with type badges
- [x] Winner identification (highlighted row, green badge)
- [x] Severity scoring (high/medium/low based on page count + impressions)
- [x] AI Analysis button per cannibalization group — generates winner recommendation + per-page actions
- [x] Action types: redirect, differentiate, de-optimize, merge, keep — with color-coded cards
- [x] "Generate Fix" button per actionable recommendation — AI rewrites meta title/description targeting suggested keyword
- [x] Review panel showing current vs proposed values (editable before apply)
- [x] "Apply to Shopify" button — pushes changes via Shopify GraphQL API + updates local DB
- [x] Prompts stored in `prompt_defaults.py` (`prompts.cannibalization_analysis`, `prompts.cannibalization_fix`) — editable in Settings

---

## Phase 2: Semrush CSV Import + Cannibalization Detector

### 2.1 CSV Import Infrastructure
- [x] Create `semrush_imports` table (id, store_id, import_type, filename, row_count, status, created_at)
- [x] Create `semrush_keywords` table with UPSERT support, position change tracking, JSONB competitor positions
- [x] Create `semrush_audit_issues` table (store_id, import_id, page_url, issue_type, severity, description)
- [x] Indexes on (store_id), (store_id, keyword), (store_id, url), (import_id)

### 2.2 CSV Upload & Parsing
- [x] Create `app/services/semrush_import_service.py` — auto-detect CSV type from column header fingerprinting
- [x] Detect Position Tracking exports (keyword, position, search volume, CPC, URL, traffic, SERP features)
- [x] Detect Organic Research exports (keyword, position, traffic, volume, URL, competition)
- [x] Detect Site Audit exports (page URL, issue type, severity, description)
- [x] Detect Keyword Gap exports (keyword, volume, CPC, competition, competitor positions via JSONB)
- [x] Handle UTF-8 BOM, delimiter auto-detection (comma/semicolon/tab), column name aliases
- [x] Batch processing (500 rows per transaction) for large CSVs
- [x] UPSERT with automatic `previous_position` tracking on re-import
- [x] Create `app/routes/semrush_routes.py` blueprint with 10 endpoints
- [x] `POST /api/semrush/upload/preview` — preview + auto-detect before import
- [x] `POST /api/semrush/upload` — full import with multipart form
- [x] `GET /api/semrush/imports`, `DELETE /api/semrush/imports/<id>`
- [x] `GET /api/semrush/keywords` — paginated, sortable, searchable, filterable by source type
- [x] `GET /api/semrush/keywords/cross-reference` — merged with GSC query data (LATERAL JOIN)
- [x] `GET /api/semrush/audit-issues`, `GET /api/semrush/audit-summary`
- [x] `GET /api/semrush/overview` — total keywords, avg position, volume, position buckets

### 2.3 Semrush Dashboard
- [x] Create `app/templates/semrush.html` with upload zone, overview cards, 4 tabs
- [x] Create `app/static/js/semrush.js` with drag-and-drop upload, preview, import flow
- [x] Drag-and-drop CSV upload zone with file type auto-detection and preview table
- [x] Overview cards: total keywords, avg position, total volume, position buckets (top 3/10/20+)
- [x] Keywords tab: sortable table with keyword, position, change (▲/▼), volume, CPC, traffic, URL
- [x] Cross-Reference tab: Semrush + GSC data side by side per keyword
- [x] Audit Issues tab: severity filter + summary badges + issues table
- [x] Import History tab: list imports with delete capability
- [x] Add "Semrush Data" nav item to sidebar under "Measure" section
- [x] Upload zone CSS (dashed border, drag-over state)
- [x] Nginx `client_max_body_size` increased to 50M for large CSV exports

### 2.4 SEO View Enrichment
- [x] `_get_semrush_metrics_for_store()` + `_attach_semrush_to_item()` in seo_routes.py
- [x] Semrush Vol. column added to product and collection tables in SEO page
- [x] Semrush data attached to product/collection detail endpoints
- [x] `get_semrush_metrics_for_store()` aggregates by URL path for handle matching
- [x] `get_keywords_by_list()` for cannibalization enrichment

---

## Phase 3: Automated Audit Scheduling + Score History

### 3.1 Audit Scheduler
- [ ] Create `audit_runs` table (id, store_id, run_type, status, total_items, completed_items, started_at, completed_at)
- [ ] Create `audit_run_results` table (id, run_id, item_type, item_id, shopify_id, title, score, breakdown, previous_score, score_change)
- [ ] Create `app/services/audit_scheduler.py` — background scheduler for weekly audits
- [ ] Configurable audit schedule (daily/weekly/monthly) via settings
- [ ] Run existing SEO analyzer + technical analyzer on all items

### 3.2 Audit Dashboard
- [ ] `GET /api/audits` — list audit runs with summary stats
- [ ] `GET /api/audits/<id>/results` — detailed results for a run
- [ ] `GET /api/audits/compare` — compare two audit runs side by side
- [ ] Score change alerts: surface items that dropped significantly
- [ ] Priority queue: rank items by "biggest improvement opportunity" (low score + high impressions from GSC)

### 3.3 Score History & Trends
- [ ] Score distribution histogram (how many items at each score bracket)
- [ ] Time-series score trends (daily/weekly avg scores)
- [ ] Before/after cards: show score at time of last AI optimization vs current
- [ ] Correlation view: score changes vs GSC metric changes (did fixing SEO improve traffic?)

---

## Phase 4: Content Gap Analysis + Competitor Monitoring

### 4.1 Content Gap Analyzer
- [ ] Create `content_gaps` table (id, store_id, keyword, search_volume, difficulty, competitor_url, competitor_position, our_url, our_position, gap_type, status, created_at)
- [ ] Import gaps from Semrush CSV (Keyword Gap report)
- [ ] Cross-reference GSC queries: find high-impression queries where we rank poorly (position > 10)
- [ ] AI agent: generate content briefs for gap keywords
- [ ] Match gaps to existing products/collections by relevance

### 4.2 Competitor Monitoring
- [ ] Create `competitors` table (id, store_id, domain, name, created_at)
- [ ] Create `competitor_rankings` table (id, store_id, competitor_id, keyword, position, url, checked_at)
- [ ] Track 3-5 competitor domains per store
- [ ] Weekly position checks using Brave SERP API for tracked keywords
- [ ] Alert when competitor overtakes us for a tracked keyword
- [ ] Comparison dashboard: our position vs competitor positions per keyword

### 4.3 Schema Markup Expansion
- [ ] Add BreadcrumbList schema for all products and collections
- [ ] Add Organization schema (from schema_settings)
- [ ] Add CollectionPage schema for collections
- [ ] Add Article/BlogPosting schema for blog articles
- [ ] Add FAQPage schema for pages with FAQ sections
- [ ] Schema coverage dashboard: % of pages with valid schema by type
