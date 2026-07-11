# App Audit — Prioritized Fix Plan

Audit date: 2026-07-10. Scope: full backend, all Flask blueprints, all services, `shopify_client.py`, sync, AI agent, all frontend JS. All 45 Shopify GraphQL operations validated against the official Admin schema (2026-04).

**How to use this file:** fixes are ordered — work top to bottom. Each fix is one unit of work with a checkbox; multi-part fixes have sub-checkboxes. Check items off as completed. Items confirmed independently by multiple reviewers are marked **(2x)**/**(3x)**.

**Progress:** 46 / 46 fixes complete — ALL PHASES DONE as of 2026-07-11. (Live Shopify *write*-path verification remains a user activity; deferred nice-to-haves are noted inline on Fixes 36, 40, 46.)

---

## Phase 1 — Critical: broken installs, dead features, destructive writes

### [x] Fix 1: Fresh-database bootstrap crash (critical) ✅ 2026-07-10 — verified: init_tables on empty DB creates all 59 tables
**Problem:** `app/database.py:279-284` runs `ALTER TABLE article_wizard_sessions ADD COLUMN shopify_article_id` before that table's `CREATE TABLE IF NOT EXISTS` (~line 458). The DO block only catches `duplicate_column`, so on an empty DB the ALTER raises `undefined_table`, aborting `init_tables` and killing gunicorn at startup. Existing DBs are unaffected, which is why it went unnoticed.
**Implementation:** Move the ALTER block to after the `CREATE TABLE article_wizard_sessions` statement (keep it as a migration for old DBs). Also add `WHEN undefined_table THEN NULL` to the exception handler as a belt-and-suspenders, matching the pattern already used at `database.py:614-626`. Verify by pointing the app at a scratch database (`docker-compose` with a fresh `./data/postgres`) and confirming clean startup.

### [x] Fix 2: Keyword Strategy apply/revert is entirely dead code (critical) (2x) ✅ 2026-07-10 — SQL/import/call fixed + page field mapping; live apply pending user test
**Problem:** `app/services/keyword_strategy_service.py` — three phantom references make every apply/revert fail at runtime:
- `:1533, 1587` select `s.shop_domain` — column doesn't exist (`stores` has `domain`).
- `:1715, 1861` select `s.shopify_domain, s.access_token_encrypted` — neither exists (`domain`, `access_token_enc`).
- `:1759, 1861` `from app.auth import decrypt_api_key` — function doesn't exist (`decrypt_value` does).
- `:1595` passes the whole `update_fields` dict as positional `meta_title` to `set_article_seo(id, meta_title=None, meta_description=None)`.
- `bulk_apply_fixes` (`:1820`) swallows per-item errors and finishes `status='completed'` — UI shows success while zero changes reach Shopify.
**Implementation:**
- [x] Replace all three SQL fragments with `s.domain, s.access_token_enc, s.api_version`; construct `ShopifyClient(row["domain"], decrypt_value(row["access_token_enc"]), row["api_version"])` (these call sites currently also omit `api_version`).
- [x] Change the import to `from app.auth import decrypt_value`.
- [x] Change `:1595` to `client.set_article_seo(shopify_id, update_fields.get("meta_title"), update_fields.get("meta_description"))`.
- [x] Also fixed: `apply_fix`/`revert_fix` page branch now maps `meta_title`/`meta_description` → `seo_title`/`seo_description` (was a silent no-op for pages).
- [x] In `bulk_apply_fixes`, count failures and set run status to `failed` when all items failed; first error stored on the run row.
- [ ] Test end-to-end: apply one step, verify the change lands on Shopify, then revert it. *(User to verify — writes to the live store.)*

### [x] Fix 3: Hardcoded "A2Z" overwrites live article authors (high) (2x) ✅ 2026-07-10 — update_article only sends author when provided; create default kept (A2Z is this store's real brand); image watermark centralized as BRAND_TEXT constant
**Problem:** `app/shopify_client.py:1140-1142` — `update_article` sets `author: {name: fields.get("author", "A2Z")}` unconditionally, so any update that doesn't pass `author` (interlinking injection, template toggles, SEO tweaks, keyword fixes) silently rewrites the live byline to "A2Z". Same default in `create_article` (`:1093`), and "A2Z" branding is baked into `services/image_generator.py:98, 241, 251`.
**Implementation:**
- [x] In `update_article`, only include `author` in `article_input` when the caller provides one. (`create_article` default kept — callers that care pass author explicitly, and A2Z is this store's actual brand.)
- [x] In `image_generator.py`, centralized the brand text as a `BRAND_TEXT` module constant (no visual change; single point of change for multi-store later).
- [x] Grepped remaining `A2Z` literals: prompt_defaults byline rule + news_routes explicit author are intentional brand usage; `_KNOWN_OLD_HANDLES` handled in Fix 45.

### [x] Fix 4: AI output written to Shopify with zero validation (high) ✅ 2026-07-10 — new app/services/ai_output_guard.py wired into all listed apply paths; unit tests pass
**Problem:** Two flavors:
- `interlinking_routes.py:426-451` (used at `:504-508, :584-586` and by `seo_routes.py:684-720`): the AI-rewritten HTML body is pushed to Shopify AND overwrites the local cache — a model refusal/empty/truncated output destroys the live description irrecoverably.
- Meta titles/descriptions/`h1_tag` (which replaces the live product/collection *title*) and description HTML are applied verbatim with no length clamp or script strip: `task_routes.py:363-384, 498-544`, `seo_routes.py:1083-1180, 1384-1473`, `ai_seo_routes.py:465-510`. The discipline exists elsewhere (`ai_agent.py:3126-3133, 3716-3724`) but not on the highest-volume paths.
**Implementation:**
- [x] Created `app/services/ai_output_guard.py`: `sanitize_html`, `clamp_meta_title` (≤70), `clamp_meta_description` (≤200), `validate_injected_html` (non-empty + expected hrefs + ≥80% length).
- [x] Link-injection consumers guarded: `interlinking_routes.apply_fix` (422 on rejection), `apply_all_fixes` (per-group error), `seo_routes._apply_internal_linking` (per-article error) — Shopify and local cache untouched on rejection.
- [x] Clamp/sanitize wired into: `task_routes` bulk product + bulk collection auto-apply, `seo_routes` apply_product_changes + apply_collection_changes (via `_guard_ai_content_fields`), `ai_seo_routes._push_and_rescore` (GEO single + batch).

### [x] Fix 5: GSC OAuth callback never validates `state` (high, security) ✅ 2026-07-10 — verified: forged callback now redirects to /gsc?error=invalid_state
**Problem:** `gsc_routes.py:69-92` — state is stored in the session (`:63`) but never compared on callback; `/api/gsc/auth/callback` is the app's only unauthenticated endpoint (`main.py:64`). A forged callback can bind an attacker-controlled Google property to the store.
**Implementation:** In the callback handler, before exchanging the code: `if request.args.get("state") != session.pop("gsc_oauth_state", None): return jsonify({"error": "Invalid OAuth state"}), 403`. Store the state under that key at `:63`. One function, ~4 lines.

### [x] Fix 6: GSC token refresh is dead code (high) ✅ 2026-07-10 — verified live: proactive refresh fired and persisted a new token_expiry on first GSC call
**Problem:** `gsc_service.py:87-126` — `token_expiry` is fetched from `gsc_credentials` but never passed to `Credentials(...)`, so `creds.expired` is always `False`; the proactive-refresh branch and the stored-token update never run. Every call after the first hour eats a 401 + refresh round trip, and the stored access token stays permanently stale.
**Implementation:** Pass `expiry=cred_row["token_expiry"]` into the `Credentials` constructor (google-auth expects a naive UTC datetime — strip tzinfo if the column is timestamptz). Verify the refresh branch runs by logging when it fires.

---

## Phase 2 — Shopify client modernization (one coordinated pass through `shopify_client.py`)

### [x] Fix 7: Bump API version off EOL 2024-10 + migrate deprecated calls (critical) ✅ 2026-07-11 — store migrated to 2026-01; full live sync passed on the new version
**Problem:** `shopify_client.py:11`, `database.py:54`, `store_routes.py:49,85` pin `2024-10`, unsupported since 2025-10-01 — Shopify silently serves the oldest supported version, which changes quarterly (invisible drift; Fix 8's `blog_id` filter only works because of this). Deprecated ops in use: `productUpdate(input:)` → `product:` (`:299`), `productCreateMedia` → `productUpdate`/`productSet` media (`:765`), `Publication.name` → `Catalog.title` (`:534`).
**Implementation:**
- [x] Default `api_version` now `2026-01` (`DEFAULT_API_VERSION` constant in shopify_client, database default, store_routes fallbacks, keyword_strategy fallbacks); migration `UPDATE stores SET api_version='2026-01' WHERE api_version < '2025-07'` in database.py — applied, store row confirmed on 2026-01.
- [x] `update_product` migrated to `productUpdate(product: $product)` (ProductUpdateInput).
- [x] `create_product_media` deliberately kept on `productCreateMedia`: deprecated but fully documented with examples through 2026-07, and the suggested replacements (`productUpdate`/`productSet`) don't return the newly created media, which image_routes callers require. Revisit if Shopify announces removal.
- [x] `get_publications` now selects `name` + `catalog { title }` (catalog title preferred, legacy name fallback — app channels like Online Store have no catalog). Verified live: "Online Store", "Google & YouTube" resolve.
- [x] All rewritten operations re-validated clean against 2026-01 (zero deprecation warnings on the ops we run except the deliberate productCreateMedia/name cases); live smoke: test_connection, publications, full sync. *(Live product update write — user to verify via any Apply flow.)*

### [x] Fix 8: Article sync `blog_id` filter passes a GID where a numeric ID is expected (high) ✅ 2026-07-11 — verified live: sync now finds 100 articles (old GID filter found 99), all correctly attributed to the news blog
**Problem:** `shopify_client.py:985-987` builds `query: "blog_id:gid://shopify/Blog/N"` (callers `sync_service.py:325, 538`). The filter expects the numeric ID, and it only exists in API ≥2025-04. Silent failure modes: blog syncs 0 articles (stale delete skipped) or the filter is ignored and every blog upserts ALL articles, flip-flopping `synced_articles.blog_id`.
**Implementation:** In `get_articles`, derive the numeric tail: `bid = str(blog_id).rsplit("/", 1)[-1]` before building the filter. Depends on Fix 7 (version ≥2025-04) being in place. Verify: sync a store with 2+ blogs and confirm per-blog article counts match Shopify admin.

### [x] Fix 9: Convert string-built GraphQL to variables (high) (3x) ✅ 2026-07-11 — all four mutations + five paginated list queries converted; schema-validated; sync exercised the queries live
**Problem:** `update_product` (`:274-304`), `update_collection` (`:419-446`), `create_collection` (`:579-594`), `create_blog` (`:923-932`) inline values into the query escaping only `"` (and `\n` for descriptions) — backslash/`\r`/`\t` unescaped, **tags not escaped at all** (`:283`), cursor/`query_filter` interpolated raw in `get_products`/`get_collections`/`get_blogs`/`get_articles`/`get_pages`. Any AI text with a `\` breaks the whole mutation; crafted values can alter it. The same mutations already exist variable-based (`collectionCreate` `:608`) — duplicate divergent implementations.
**Implementation:**
- [x] Rewrote `update_product`, `update_collection`, `create_collection`, `create_blog` with typed variables; all manual escaping deleted.
- [x] `_normalize_tags` helper: accepts list or comma-string (fixes silent tag-drop — Fix 24 satisfied for update_product).
- [x] Converted `get_products`/`get_collections`/`get_blogs`/`get_articles`/`get_pages` to `$first`/`$after`/`$query` variables.
- [x] Inline string-built `collectionCreate` deleted; `create_collection` uses the variables mutation.
- [x] Re-validated with the Shopify dev MCP against 2026-01 — all clean.

### [x] Fix 10: Rate-limit and error handling in `_graphql` (high) (2x) ✅ 2026-07-11 — THROTTLED cost-based retry, 429/5xx backoff, raise-on-exhaustion (never returns None); verified live: 5 RemoteDisconnected blips auto-retried during the full sync
**Problem:** `shopify_client.py:25-52`:
- GraphQL cost throttling arrives as HTTP 200 with `errors[].extensions.code == "THROTTLED"` — line 43 raises immediately, so large-store syncs die mid-way in partial state. The 429 branch almost never fires for GraphQL.
- Three consecutive 429s fall off the loop end → returns `None` → `AttributeError` deep in callers.
- `raise_for_status()` HTTPError (502/503) isn't in the retry `except` clause — one edge blip aborts a long sync.
**Implementation:** Rework `_graphql`'s loop:
- [ ] After parsing `data`, if any error has `extensions.code == "THROTTLED"`: compute sleep from `extensions.cost.throttleStatus` (`(requestedQueryCost - currentlyAvailable) / restoreRate`, min 1s), sleep, retry without consuming a connection-retry attempt (use a separate throttle-retry budget, e.g. 5).
- [ ] Retry `resp.status_code >= 500` and 429 with exponential backoff inside the same loop.
- [ ] After the loop, `raise Exception("Shopify request failed after N retries (rate limited)")` — never fall through to `None`.
- [ ] Remove the inter-page guesswork sleep in `sync_service.py:248` once throttle handling is real (optional; harmless to keep).

### [x] Fix 11: `file_get_url` crashes on null node; GenericFile never resolves (medium) ✅ 2026-07-11 — null node raises a clear "file not found"; GenericFile fragment added
**Problem:** `shopify_client.py:720-744` — `data.get("node", {})` returns `None` when Shopify returns `"node": null` (deleted/bad id) → `AttributeError`; the query only inline-fragments `MediaImage`, so `GenericFile` uploads poll for 20s then time out.
**Implementation:** `node = data.get("node") or {}`; add `... on GenericFile { fileStatus url }` to the query and return `url` for that branch; raise a clear "file not found" error when node is null.

---

## Phase 3 — Task/status integrity (failure must look like failure)

### [x] Fix 12: Failed AI work recorded as success (high) (2x) ✅ 2026-07-11 — verified: unknown-task-type task now lands status=failed with error surfaced
**Problem:** `run_task` signals failure by *returning* `{"error": ...}` or `{"raw_response": ...}`, but:
- `task_routes.py:32-44` (`_execute_task`) always sets `status='completed'`.
- Bulk loops mark errored items `completed` and never increment `failed`: `task_routes.py:558-573, 686-701, 814-829`, `technical_seo_routes.py:731-738, 844-856`, `brand_routes.py:1380-1404`. (`blog_routes.py:727-737` does it right — copy that.)
- `sync_service.py:390-391, 602-603`: all blogs can fail and the sync job still ends `completed`.
- `competitor_routes.py:58-69`: service `{"error": ...}` dicts returned as HTTP 200 → toast shows "undefined keywords, undefined alerts".
**Implementation:**
- [x] `is_failed_result` lives in `app/services/task_runner.py`; `_execute_task`, `_execute_keyword_analysis`, and technical_seo `_execute_fix_task` now set `status='failed'` + error for error results.
- [x] All bulk loops (task_routes ×4 via `item_error` tracking incl. apply failures; technical_seo bulk; brand bulk optimize) mark failed items failed and increment `failed`.
- [x] Sync blog phases track per-blog failures; job `error` column gets "N blog(s) failed to sync: ..." (status stays `completed` so existing pollers terminate; blogs-only sync returns `blogs_failed`/`errors`).
- [x] `competitor_routes.py` returns 400 when the service reports an error (kills the "undefined keywords" toast).

### [x] Fix 13: Background executor threads die outside try → tasks stuck `running` forever (high) (3x) ✅ 2026-07-11 — verified: crashing executor finalized to failed; 3h/7h stuck rows swept
**Problem:** ~10 copy-pasted executor skeletons run `decrypt_value`/`ShopifyClient(...)`/item SELECTs *after* `status='running'` with no enclosing try: `seo_routes.py:1693-1718, 1930-1956, 2147-2170, 2739-2745`, `task_routes.py:296-318, 430-454, 624-641, 752-769`, `ai_seo_routes.py:598, 833`, `image_routes.py:201-232, 373-406`, `technical_seo_routes.py:634-673, 869`, `brand_routes.py:86-120, 925-960, 1321-1363`, `interlinking_routes.py:265-267`, `homepage_routes.py:132-134`. The only stale-job sweeper (`ai_seo_routes.py:653-671`) fires only from AI-SEO endpoints. Daemon threads are also killed by worker restarts mid-run.
**Implementation:**
- [x] `app/services/task_runner.py`: `spawn(table, task_id, fn, *args)` starts the daemon thread and finalizes the row to `failed` on any uncaught crash; `is_failed_result`; `sweep_stale_jobs` (self-throttled to 1/min).
- [x] All 20 tracked executor threads across task/seo/image/ai_seo/technical/brand/interlinking/homepage routes now start via `spawn` (blog_routes' executor was already fully guarded — left as-is).
- [x] Sweeper moved to task_runner (now also covers `keyword_strategy_runs`); called from `/api/tasks/<id>`, `/api/tasks/bulk/<id>`, `/api/blog/bulk-status/<id>`, and the existing ai_seo call sites.
- [x] Verified: deliberately crashing executor → row failed with error; synthetic stuck rows (3h ai_task, 7h bulk_task) swept to failed.

### [x] Fix 14: Frontend pollers — treat `failed` as terminal, survive transient errors, guard double-submits (high) (2x) ✅ 2026-07-11 — shared helpers in app.js; ~25 sites migrated across 9 files; all pages render, node --check clean
**Problem:** Three systemic patterns:
- Pollers stop only on `completed`; `failed` loops forever behind full-screen overlays with no cancel: `seo.js:762-798, 1515-1553, 3776-3824, 4341-4374`, `brands.js:408, 481, 1139`.
- One transient poll error kills the loop silently (spinner stuck, paid result lost): `seo.js:1954, 2394, 2712`, `ai_seo.js:283-301, 584-587, 1102-1106`, `interlinking.js:831-833, 871-873`, `brands.js:914-939`, `blog.js:599-617`, `news.js:238-240`, `stores.js:241-245`, `keyword_strategy.js:879-898`.
- Paid AI trigger buttons lack double-submit guards (double OpenRouter spend, racing pollers): `seo.js:1874-1911, 2339-2377, 2629-2695`, `interlinking.js:840-878`, `homepage.js:171-221`, `blog.js:388-417`, `news.js:400-418, 679-695`, `keyword_strategy.js:143-163`.
**Implementation:**
- [x] `app.js`: `pollTaskUntilDone(url, {intervalMs, maxErrors=5, timeoutMs, onProgress})` (failed→reject with task.error, 5-error tolerance, timeout) + `guardButton(btn, fn)`.
- [x] Migrated: seo.js (3 modal pollers, 4 bulk overlay pollers, 4 analyze buttons guarded), ai_seo.js (3 pollers), interlinking.js (recommendations poller+guard, status poller tolerance), brands.js (poll handle cancelled on modal switch — also resolves the brand-A-into-brand-B bug), blog.js (wizard poller tolerance, analyze guard, bulk poller tolerance), news.js (3 pollers + publish guards), stores.js (sync poller tolerance), keyword_strategy.js (pollRun onFail + Generate Plan re-enabled in all branches, consolidate guard).
- [x] Bulk overlay in seo.html has a ✕ "Hide progress (task keeps running)" button.
- [x] `keyword_strategy.js` Generate Plan re-enabled on success/failed/give-up.

### [x] Fix 15: News publish duplicates + dead links (high) ✅ 2026-07-11 — republish updates instead of duplicating; article links point at the storefront
**Problem:** `news_routes.py:316-357, 977-1032` — `_step_publish` always calls `create_article` even when the session already has `shopify_article_id` (blog wizard does this right at `blog_routes.py:1903-1908`); combined with no button guard (`news.js:400-418, 679-695`) a double-click publishes duplicate live articles. And `news_routes.py:421` returns store-relative `/blogs/news/{handle}` which the JS renders against the app origin → every "view article" link 404s (`news.js:176-177, 431-438, 690-691, 728, 736`).
**Implementation:**
- [x] `_step_publish`: republish with an existing `shopify_article_id` updates the article; the id is persisted immediately after create so any later failure retries as an update.
- [x] `_store_base_url` helper; publish result URL is absolute; `/api/news/articles` returns `store_base_url` and news.js builds every article link from it (done-screen, batch chips, published table).
- [x] Publish buttons guarded via Fix 14.

---

## Phase 4 — Frontend escaping & wrong-target writes

### [x] Fix 16: Quote-safe escaping + kill inline-handler interpolation (critical, XSS + dead buttons) ✅ 2026-07-11 — single quote-safe escapeHtml/escapeAttr in app.js (8 local redefinitions deleted); all data-in-onclick sites migrated to ids/indexes/data-attrs; ai_seo previews sanitized
**Problem:** `escapeHtml` (`app.js:40`) escapes only `& < >` — `'` breaks inline `onclick` JS strings (dead buttons for `Men's Hats`, GSC query `men's pipes`), `"` breaks out of attributes (stored XSS from GSC queries / vendor names / AI output). `escapeAttr` is defined 7× with 3 behaviors; `seo.js:2102` drops the `'` escape and shadows app.js. Worst sink: `blog.js:452` inlines an entire AI HTML body into an onclick. Attribute contexts using `escapeHtml` also silently truncate values like `17" Laptop Sleeve` on save. Sinks: `seo.js:2039-2041, 3507, 3509, 3903-3917, 4588`, `brands.js:184, 190, 212, 215, 831`, `gsc.js:101, 412, 450, 719`, `stores.js:44-45`, `image_manager.js:195`, `blog.js` (~20 sites), `dashboard.js:160`, `content_gaps.js:235, 386-399, 553, 564`, `keyword_strategy.js:861-862`, `semrush.js:318, 450, 582`, `blog.js:1701, 346` (hero image attrs), `landing_pages.js:552, 558` (JSON into `<textarea>`).
**Implementation:**
- [x] `escapeHtml` escapes `& < > " '`; `escapeAttr` is an alias; all 8 local redefinitions deleted (incl. seo.js's `'`-less version that shadowed the page and homepage/landing's `&`-less round-trip-mutating versions). Unit-tested in node.
- [x] Inline-handler interpolation eliminated: blog.js AI-body-in-onclick → `applyArticleAiField` + module cache; gsc.js (4 sites → row-index lookups; GSC queries are third-party strings), brands.js (row/index caches), stores.js/image_manager.js (id lookups), seo.js keyword chips + tag checkboxes (`this.dataset.*`), blog.js autocomplete rows (index caches). Remaining interpolations verified numeric/literal-only.
- [x] `dashboard.js` recommended_action wrapped in escapeHtml.
- [x] `ai_seo.js` `sanitizeAiHtml` (mirrors backend guard) wraps both LLM-HTML preview injections.
- [ ] Spot-test in UI with a product titled `17" Men's "Special" <Hat>` and vendor `O'Brien & Sons`. *(User to verify visually.)*

### [x] Fix 17: Bulk-select operates on the unfiltered list (high) ✅ 2026-07-11 — select-all/shift-range use the rendered visibleProducts; generate-collections skips filter-hidden tags
**Problem:** `seo.js:675-684` — select-all iterates `currentProducts` while the table renders `filtered`; shift-range (`:596, 646-673`) resolves `data-index` (into `filtered`) against `currentProducts`. With a search active, "Bulk Optimize & Apply" pushes AI rewrites (auto_apply) to the entire catalog. Same class: `seo.js:3606-3633` vs `:3725-3734` — generate-collections executes tags hidden by the filter.
**Implementation (done):** `visibleProducts` module var set inside `renderProductsTable`; `toggleSelectAll` + shift-range resolve against it (collections already filter before render, so `currentCollections` is the visible list). `executeGenerateCollections` now skips tags below `minProductsFilter` even when previously selected. The bulk-confirm dialog already shows the selected count.

### [x] Fix 18: Tag→collection ILIKE substring matching adds wrong products to live collections (high) ✅ 2026-07-11 — exact membership over split tag list; real-data proof: %pipe% matched 120 products, exact tag 'pipe' matches 0
**Problem:** `seo_routes.py:1972-1976` — `tags ILIKE '%' || tag || '%'`: tag "red" matches "bordered"; `%`/`_` in tags act as wildcards. Matched products are pushed to Shopify (`:2040`).
**Implementation:** Replace with exact membership over the split list: `WHERE EXISTS (SELECT 1 FROM unnest(string_to_array(p.tags, ',')) t WHERE lower(trim(t)) = lower(%s))`. Apply the same pattern anywhere else tags are LIKE-matched (grep `tags ILIKE`).

---

## Phase 5 — AI agent correctness & cost

### [x] Fix 19: `article_research` — NameError on non-JSON + unbounded catalog prompt (high) ✅ 2026-07-11
**Problem:** `ai_agent.py:4298-4312, 4376` — `rec_word_count`/`content_angles` are unbound when the model returns text with no `{...}` → `NameError` after all the paid Brave/scrape/LLM work (the `'content_angles' in dir()` hack at `:4386` guards only one of the two). And `:4319-4341` — the discovery SELECT has **no LIMIT**, serializing the entire product+collection catalog (incl. tags) into one prompt: 200k+ tokens or a hard context failure on large stores.
**Implementation:**
- [x] Variables initialized before the try; `dir()` hack deleted; parse failures now logged.
- [x] Discovery SELECT pre-filters by topic/keyword terms (title/type/vendor ILIKE) with LIMIT 300, falls back to newest 300; `tags` dropped from the line format; collections capped at 300.

### [x] Fix 20: Wrong-entity context injected into prompts (high) ✅ 2026-07-11 — verified: article item_type returns None from both helpers; landing page_type bound server-side
**Problem:** Two id-aliasing bugs:
- `ai_agent.py:212-213, 355-372, 391-397` — for `item_type="article"`, `_get_image_alt_status`/`_get_faq_schema_status` run the *collection* branch with the article's row id → prompt directives ("FAQ schema already exists — do NOT recommend") from an unrelated collection.
- `ai_agent.py:981-991, 1065-1078, 3742-3747` — landing-page tool defaults `page_type='synced'`; if the model omits the param for a builder page, the handler reads `synced_pages` with a `landing_pages` id — analyzes an unrelated page.
**Implementation:**
- [x] Both helpers guard `item_type not in ("product", "collection") → None`.
- [x] Landing loop overrides `page_type`/`page_id` in `analyze_landing_page_seo` tool args from the task input.

### [x] Fix 21: Wasted LLM spend — description generation after failure, duplicate context computation (high) ✅ 2026-07-11
**Problem:** `ai_agent.py:4832-4870` — description generation (a full LLM call + duplicate search-context pass) runs even when `main_result` is `{"error"...}`/`{"raw_response"...}`; every apply path then discards it. And `_get_search_context_for_item` runs twice per task (`:2624`+`:2028`, `:2709`+`:2211`, `:2795`+`:2390`) — each pass loads and substring-scans every article/product/collection body in the store (`:237-261`).
**Implementation:**
- [x] `run_task` returns early (before all post-processing) when main_result is an error/bare raw_response.
- [x] All three generators accept `search_context=None`; product/collection/brand branches prefetch once and pass it through — the second full-store body scan per task is gone.

### [x] Fix 22: Tool-loop robustness (medium) ✅ 2026-07-11 — all 5 loops: malformed tool args feed an error back to the model; branch on message.tool_calls truthiness
**Problem:** `ai_agent.py:1391, 2850, 3769, 4045, 4810` — `json.loads(tool_call.function.arguments)` unguarded (truncated args from proxied models kill the whole task, wasting all prior iterations); the `or`-guards above each loop enter the branch when `finish_reason == "tool_calls"` but `message.tool_calls` is `None` → `TypeError`.
**Implementation:** In each of the five loops: wrap arg parsing in try/except, feeding `{"error": "invalid tool arguments, please retry"}` back as the tool result (the model self-corrects); change the branch condition to `if choice.message.tool_calls:`. Consider extracting one shared `_run_tool_loop` since the five copies have drifted (optional; fold into Fix 41 if deferred).

### [x] Fix 23: `keyword_analysis` token blowup + case-sensitive tag filtering (medium) ✅ 2026-07-11
**Problem:** `ai_agent.py:2836-2866` — Phase 1 resends the full accumulated history (pages of 200 products) every iteration → O(pages²) prompt tokens on large stores. `:2935-2936` — Phase 2 discards any tag returned with different case/whitespace than the taxonomy, silently under-tagging.
**Implementation:**
- [x] Phase 1 embeds the catalog server-side in one message (2000-line cap with product-type distribution summary beyond); no more agentic pagination resending history.
- [x] Phase 2 filters via canonical lowercase map, mapping back to taxonomy casing.

### [x] Fix 24: `update_product` silently drops non-list tags (medium) ✅ done in Phase 2 (Fix 9 `_normalize_tags`)
**Problem:** `shopify_client.py:282` — `isinstance(fields["tags"], list)` guard skips comma-strings with no error; keyword-strategy passes LLM comma-strings (`keyword_strategy_service.py:1582-1583`) and marks the step applied. (`technical_seo_routes.py:494-498` splits correctly — drift.)
**Implementation:** Covered by Fix 9's normalization (accept list or string, split/strip). Verify the keyword-strategy tag step actually changes tags on Shopify afterward.

### [x] Fix 25: Misc AI-agent correctness batch (medium) ✅ 2026-07-11
- [x] Availability facts are tri-state: only asserted when variant data has the `available` key (product facts + collection member comparison rows).
- [x] `_build_faqpage_jsonld` filters non-dict items (verified: string FAQ items no longer crash).
- [x] Interlinking chunks: parse errors skip the chunk; API errors abort the task with a real error result.
- [x] `keywords_used` always in the product schema (verified live: default skip_tags run returned it); `suggested_tags` still gated.
- [x] Store-audit prompt trimmed to tool-groundable sources with an explicit "never invent findings" instruction; fabricated summary keys removed (no consumers existed).

---

## Phase 6 — Sync integrity

### [x] Fix 26: Blog sync — shared implementation + store lock (medium) ✅ 2026-07-11 — one `_sync_blogs_and_articles` used by both paths (duplicate upsert deleted); verified live: blogs-only sync during a full sync fails fast with a clear lock message; article_type promotion now works in blogs-only sync (59 news-typed articles)
**Problem:** `sync_service.py:490-622` vs `296-397` — `sync_store_blogs` doesn't take `_get_sync_lock(store_id)` (can interleave DELETE/upsert with the daemon full sync) and its article upsert is a drifted near-duplicate: full sync's `ON CONFLICT` promotes `article_type` to `'news'` (`:349`), blog-only sync omits it (`:547-562`).
**Implementation (done):** `_sync_blogs_and_articles(client, store_id, progress_cb)` with the full upsert (article_type promotion, staged-upload filtering, per-blog stale cleanup incl. empty-blog case); `sync_store_blogs` takes the per-store lock (fails fast when a sync is running) and surfaces per-blog errors in its result.

### [x] Fix 27: Sync data-integrity batch (medium) ✅ 2026-07-11 — verified live: full sync clean on the new queries; max_images now 50 (was capped 10), max_variants 14 (was capped 10)
- [x] Stale-row DELETEs run unconditionally after a successful fetch (products, collections, blogs, per-blog articles) — all-deleted edge cases now clean up.
- [x] `_upsert_product_batch` de-dupes each chunk by shopify_id.
- [x] `resourcePublicationsV2(first: 20)`, published if any edge isPublished (264/265 collections now flagged published).
- [x] `media(first: 50)` / `variants(first: 50)` — live-verified within Shopify's query-cost ceiling. *(Known limit: products with >50 images/variants still truncate at 50; inner pagination would be needed for full coverage.)*

### [x] Fix 28: `publish_to_all_channels` publishes to every third-party channel (low) ✅ 2026-07-11 — publishes Online Store only (name-matched via catalog title/legacy name); falls back to publish-everywhere with a warning if the channel can't be identified
**Problem:** `shopify_client.py:542-561` publishes to all `publications(first: 50)` including app channels (Google, Faire…); one channel's userError raises total failure after partial success.
**Implementation:** Fetch publications once, publish only to the Online Store publication (match `catalog` type/name after Fix 7); callers (`seo_routes.py:2034`, `brand_routes.py:161`, `content_gap_service.py:739`) unchanged.

### [x] Fix 29: Delete dead/duplicate Shopify code paths (low) ✅ 2026-07-11
- [ ] Delete `get_collection_products` (`shopify_client.py:351-380`) — unpaginated `first: 50` duplicate of `get_all_collection_products` with different status filtering; no live callers.
- [ ] `blog_routes.py:1227-1241` — replace the private `client._graphql` call (unpaginated `first: 50`) with a proper paginated client method, or delete the endpoint (it's also unused by the frontend — see Fix 40).

---

## Phase 7 — Transactions & state divergence (Shopify vs local DB)

### [x] Fix 30: Persist remote IDs immediately; make retries idempotent (medium) ✅ 2026-07-11
**Problem:** Shopify create → later local persist in a separate transaction; a failure between them mints duplicates on retry:
- `news_routes.py:357-410` (article), `landing_routes.py:606-624` (page), `brand_routes.py:151-201` (smart collection).
- `ai_seo_routes.py:1490-1521` — `aeo_llms_apply` creates a **new** `/pages/llms` page on every publish (Shopify auto-suffixes `llms-1`, `llms-2`).
- `news_routes.py:104-117` — any `update_page` failure on the news hub is treated as "page deleted" → creates `news-1` and repoints the hub.
**Implementation:**
- [x] News article: done in Fix 15. Landing page publish already persisted immediately (verified). Brand smart collections: local persist now happens BEFORE the publish step (a later failure can't lose the Shopify id → no duplicate on re-run).
- [x] llms page: new `client.get_page_by_handle` + update-if-exists (verified live: the existing /pages/llms was found — the next apply would have minted llms-1). News hub: recreates only on a confirmed not-found error; transient failures now propagate instead of minting news-1.

### [x] Fix 31: Never mutate local state when the Shopify write failed (medium) ✅ 2026-07-11
**Problem:** `blog_routes.py:452-491` (bulk article delete: per-article Shopify failures warned, ALL local rows deleted → articles live but invisible until next sync), `landing_routes.py:639-653` (unpublish), `brand_routes.py:382-397` (collection delete returns `{"ok": true}` with the collection still live), `news_routes.py:1169-1187` (template revert clears local `template_suffix` for articles whose Shopify update failed).
**Implementation (done):** blog bulk article delete removes locally only what deleted on Shopify (not-found tolerated as success) and returns `{"deleted", "failed": [...]}`; landing unpublish returns 502 and keeps local published state when Shopify fails; brand collection delete returns 502 without touching local rows on real failures; news template revert clears local `template_suffix` only for articles whose Shopify update succeeded and refuses to delete the theme files until all articles cleared.

### [x] Fix 32: Record change/audit events after the push, not before (medium) ✅ 2026-07-11 — seo_change_events (product + collection apply) and keyword_strategy_changes audit rows now insert only after the Shopify push succeeds
**Problem:** `seo_routes.py:1124-1136, 1426-1438` — `seo_change_events` committed before the Shopify push; a failed push leaves phantom "optimizations" that `/api/seo/optimized` and `/api/seo/impact` report. `keyword_strategy_service.py:1747-1756` — audit rows before apply; `revert_fix` would then overwrite live values with wrong "old" values.
**Implementation:** Compute before-scores in memory, perform the Shopify push, then insert the event row in the same block as the local cache update (which already happens post-push). Same reorder in `apply_fix` (mirror `apply_step_fix:1605`, which does it right).

### [x] Fix 33: Schema push lifecycle (medium) ✅ 2026-07-11 — verified live: regenerate resets pushed flag, /view read-only, push on invalid → 400
**Problem:** `schema_routes.py:172-186` — single regenerate's upsert omits `pushed_to_shopify = FALSE, pushed_at = NULL` (both sibling paths set it: `:63`, `:338`) → batch-push skips regenerated schema while UI says pushed. `:250-286` — `push_schema` has no `is_valid` gate (batch requires it). `seo.js:4112` — opening the product modal POSTs `/generate` (an upsert) as a "preview". `schema_routes.py:458-487` — org settings hardwired `ORDER BY id LIMIT 1` (first store) while consumers read per-store; `ai_seo_routes.py:1657-1713` already implements per-store org settings — duplicate feature.
**Implementation:**
- [x] Regenerate upsert resets `pushed_to_shopify = FALSE, pushed_at = NULL` — batch push re-pushes regenerated schemas.
- [x] `push_schema` returns 400 with validation errors when `is_valid` is false.
- [x] Product modal uses the existing read-only `GET /view` and only POSTs generate when no schema exists yet.
- [x] `/api/schema/settings` accepts `store_id` (GET + POST) with first-store fallback — single-store behavior unchanged, hardwiring removed.

### [x] Fix 34: Homepage publish backup ordering (medium) ✅ 2026-07-11 — pristine backup persisted BEFORE any theme write, and never snapshotted from an already-injected index. Unpublish deliberately stays surgical (remove_section) — restoring a stale full backup could clobber theme edits made since publish; the backup is a manual-recovery artifact.
**Problem:** `homepage_routes.py:331-358` — theme `index.json` is written before the pristine backup persists; a first-publish partial failure poisons the next backup with already-injected content. The stored `index_json_backup` is also never read by any code path.
**Implementation:** Persist the backup (commit) *before* the first theme write; on unpublish, prefer restoring from the backup when present (else keep the current `remove_section_from_index` logic). One-block reorder plus a small restore branch.

---

## Phase 8 — Scoring & data quality

### [x] Fix 35: Composite score re-normalization + unified meta thresholds (high) ✅ 2026-07-11 — unit-verified: GSC-only perfect page now scores 100 (was capped 60), Semrush-only 100 (was capped 40); 55-char title scores 100 in both scorers
**Problem:** `seo_analyzer.py:213-217, 257-297` — pages missing a data source get zeros instead of re-weighting: Semrush-only caps search score at 40, GSC-only at 60, and `compute_composite_score` blends that crippled number at 50-60% weight (a perfect page without one source caps composite ~80). `weights_gsc_only` is also applied to Semrush-only cases. Separately, meta title/description thresholds contradict across the three scorers and the AI prompt: product rules 30-60/120-160, technical rules 50-60/140-160, SERP prompt "50-60 / 145-160" (`prompt_defaults.py` rules vs `serp_scraper.py:791-797`) — the AI's "optimized" output can score *worse* on one dashboard.
**Implementation:**
- [x] `analyze_search_performance` weights only components whose source has data, re-normalized to 1.0; missing sources marked "(excluded from score)" in the breakdown. `compute_composite_score` reads `weights_single_source` (falls back to the legacy `weights_gsc_only` key). No-search-data pages already fell back to on-page-only.
- [x] Canonical thresholds: title good [50,60] ok [30,70]; description good [140,160] ok [100,200] — `rules.product_seo` + `rules.collection_seo` DEFAULTS aligned to the technical scorer and the AI prompts (no stored overrides existed); SERP prompt text aligned to 140-160. (`rules.article_seo` deliberately keeps its shorter headline convention.)

### [x] Fix 36: GSC service correctness batch (medium) ✅ 2026-07-11 — live-verified: overview deltas now like-for-like (clicks delta went from a skewed +418% to an honest +33%), top-pages honors limit/offset, opportunities all ctr<5%
- [x] `_fetch_gsc_by_dimension` stops at the requested row count; `get_top_pages`/`get_top_queries` fetch `limit+offset` live and slice — live and DB paths paginate identically now.
- [x] `get_overview` fetches the previous period live when the current period came live (DB fallback preserved).
- [x] Fresh-window rows (today-2..today) are deleted once per pull before re-inserting — dropped page/query combos no longer linger.
- [x] Opportunities CTR predicate moved into SQL HAVING; limit applies to the filtered set.
- [x] Backfill/pull/snapshot threads log exceptions (pull rows were already failed by pull_data's own handler).
- [x] Bulk cannibalization fix: local alt updates keyed by `media_id` (positional indexing hit wrong images); no-change guard skips stamping `last_optimized_at`/fix rows (returns status "skipped"). *(Left synchronous — converting to the bulk_tasks pattern is UI-visible; revisit if runs exceed ~10 items regularly.)*

### [x] Fix 37: Keyword/master-data correctness batch (medium) ✅ 2026-07-11 — all seven items; brand_rank unit-verified (list ordinal 2 vs line index 5)
- [x] `add_manual_keyword` passes NULL for omitted volume — re-adding a keyword no longer wipes its Semrush volume.
- [x] `consolidate_master_keywords` preserves `source='manual'` provenance.
- [x] `get_changes` titles actually attached now, batched into one `id = ANY(...)` query per item type.
- [x] `force_type` rebuilds the column map against the forced signature and errors clearly when required columns are missing; import route now has the preview's filename/extension/50MB checks.
- [x] `brand_rank` counts list-item ordinals (unit-verified).
- [x] `declining` gap type restored (GSC position >15 while Semrush-ranked ≤20); overview count is reachable again.
- [x] Cannibalization gate fails closed with a 502 + explicit override hint; only the risk check is inside the try.

---

## Phase 9 — Infrastructure & performance

### [x] Fix 38: Secrets hygiene — encryption key (medium) (2x) ✅ 2026-07-11 — verified: stack recreated on .env with the SAME Fernet key; decryption + live Shopify call succeed
**Problem:** `docker-compose.yml:17-18` hardcodes `ENCRYPTION_KEY` (and `SECRET_KEY`) in the repo — encryption-at-rest is theater for anyone with repo access. If the env var is missing, `app/auth.py:12-19` silently generates a random per-process key: the 2 gunicorn workers get *different* keys post-fork (cross-worker decrypt failures) and everything encrypted is unrecoverable after restart.
**Implementation:**
- [x] `.env` (gitignored, SAME values — data stays decryptable) + `.env.example` with generation instructions; compose uses `env_file` + `${POSTGRES_PASSWORD:?}` interpolation. NOTE: the old key remains in git history — rotating it (decrypt-all/re-encrypt script) is a recommended follow-up if the repo is ever shared.
- [x] `_get_fernet` raises RuntimeError with a clear message when ENCRYPTION_KEY is missing; SECRET_KEY logs a prominent warning and stays random-per-process (dev convenience).

### [x] Fix 39: Shared-state and scheduler safety (medium) ✅ 2026-07-11 — verified: second process denied leadership; interlinking status consistent + dedup guard works end-to-end
- [x] Interlinking running-state tracked in `ai_tasks` (task_type `interlinking_analysis`, 30-min staleness window, spawned via task_runner) — status consistent across workers; verified live: trigger → running:true, duplicate trigger rejected, completes → task row `completed`.
- [x] Leader election via session-level `pg_try_advisory_lock` on a dedicated connection (`db.try_acquire_leader`); boot recovery + serp purge + all three schedulers run only in the leader. `init_tables`' internal lock switched to `pg_advisory_xact_lock` (the session variant would have leaked on the new pool). PullScheduler skips stores pulled within 90% of the interval.

### [x] Fix 40: Database connection pool + N+1 batching (medium) ✅ 2026-07-11 — pool verified under 40 concurrent requests; clustering on 3,540 real keywords: identical results to the old algorithm, 0.4s
**Problem:** `database.py:16-35` opens a fresh TCP connection per `get_cursor` block, and hot loops do it per row: `keyword_strategy_service.py:398` (per-keyword volume, called 2× per cluster member), `:915-929`, `:1194-1204`, `content_gap_service.py:206`, `ai_visibility_tracker.py:462`, `competitor_service.py:441-473`. `cluster_keywords` (`:302-403`) is additionally O(n²) pure-Python Jaccard in the request thread — hangs at ~10k keywords.
**Implementation:**
- [x] Fork-aware `ThreadedConnectionPool(2, 16)` (per-pid rebuild — an inherited pool under gunicorn --preload would share sockets across processes); getconn retries briefly on exhaustion instead of failing bursts; rollback-on-exception semantics preserved.
- [x] `cluster_keywords`: volumes prefetched in the main query (kills the 2-per-member connection storm), member updates batched via `id = ANY(...)`.
- [x] Inverted token index with a 500-member stop-token cap — verified against the old all-pairs algorithm on real data: identical 53 clusters. *(Deliberately NOT moved to a background thread — at 0.4s it doesn't need one. ai_visibility inserts left per-row: each follows a multi-second LLM call and now uses a pooled checkout. Remaining minor per-row lookups in cannibalization/assignment/competitor reports are now pool-cheap; batch later only if profiling says so.)*

### [x] Fix 41: Small infra batch (medium/low) ✅ 2026-07-11 — serp_cache purged 1,550→45 rows at boot
- [x] serp_cache purged at leader boot (1,550 → 45 rows on first run).
- [x] Brave limiter: slot reserved under a `threading.Lock` (concurrent callers queue); non-timeout exceptions get the advertised retries with backoff.
- [x] `image_generator` has a module-level `import re` (local copies removed — incl. the missing one that NameError'd brand logos on data-URI responses).
- [x] `settings_manager` logs the swallowed fetch failure with traceback.
- [x] Technical product scorer uses `weights.get(k, 0)` (matches the collection scorer).
- [x] 500 handler returns a generic message; detail stays in the logs.

---

## Phase 10 — Route & API consistency

### [x] Fix 42: Route consistency batch (medium/low) ✅ 2026-07-11 — verified: empty-body POSTs 400, wizard PUTs 404, bogus bulk ids rejected, dead endpoints 404
- [ ] **Unguarded `request.get_json()` (~50 sites 500 on empty body):** standardize `data = request.get_json(silent=True) or {}` — blog_routes (23 sites), landing_routes (9), gsc_routes (5), keyword_strategy_routes (7), settings/store/task/content_gap/competitor routes. Mechanical sed-style pass, verify each still validates required keys with a 400.
- [ ] **Store scoping:** add `AND store_id = %s` wherever an item id and store_id are both known — `ai_seo_routes.py:409, 564, 760, 806`, `brand_routes.py:941-951, 1342-1351`; `news_routes.py:504, 605-606, 699-700` update by `shopify_id` alone despite `UNIQUE(store_id, shopify_id)`. Prevents a stale frontend store selector writing via the wrong store's client.
- [ ] **Enqueue/JOIN mismatch:** `task_routes.py:196-207`, `image_routes.py:207-218` — validate requested ids against the store (`WHERE id = ANY(%s) AND store_id = %s`) *before* inserting bulk items, and set `total` to the validated count, so `completed+failed == total` always terminates.
- [ ] **Response-shape drift:** standardize on `{"ok": true, ...}` (blog_routes' 12× `{"success": true}` and the bare `{"task_id"}` returns); make wizard PUTs 404 when `UPDATE` affects 0 rows (`blog_routes.py:1527, 1578, 1743, 1826`, `news_routes.py:785-803`); split `dashboard_stats` (`dashboard_routes.py:19-56`) into store-scoped counts when `store_id` is passed.
- [ ] **Landing builder:** catch the `UNIQUE(store_id, handle)` violation and return 400 "title already used" (`landing_routes.py:316-374`); reject empty handles; make section edits update one section by id in SQL (`jsonb_set`) or add an `updated_at` optimistic-lock check (`:389-501`); on reorder, append sections missing from the submitted order instead of dropping them (`:489-500`).
- [ ] **Input validation 500s:** wrap `int(request.args.get(...))` sites (`seo_routes.py:358, 445`) in try→400; use `type=int` for store_id parsing (`news_routes.py:483`, `image_routes.py:27`); whitelist `item_type in ("product", "collection")` → else 400 before every table-ternary (`gsc_routes.py:523, 682`, technical_seo_routes several) — garbage currently falls into the collection branch.

---

## Phase 11 — Frontend UX correctness

### [x] Fix 43: Frontend behavior batch (medium) ✅ 2026-07-11 — content-gaps 409 'Optimize Anyway' flow, GSC country reset + stale-response guards, landing pollTask on status (not substrings) with save-failure abort, AI Filter failure recovery, brand modal dialogs/refresh, blog/homepage fixes
- [ ] `content_gaps.js:222, 470-482, 592-606` + `content_gap_routes.py:160-188` — "Optimize Existing Page Instead" dead-ends on high-risk keywords (route 409s with no `error` key, no proceed path). Return `{"error", "warning", "requires_confirmation": true}` in the 409 body; frontend shows an "Optimize Anyway" confirm that resends with `skip_cannibalization_check: true` (mirror `wizardGenerateContent:332`).
- [ ] `gsc.js:4, 136-150` — reset `currentCountry = ""` (and re-render the select) in `onStoreSelected`; stale filter currently silently distorts all numbers after a store switch.
- [ ] `gsc.js:182-209, 386-401, 424-438` — add a monotonic request token per dashboard load; discard responses whose token ≠ latest (stale slow responses currently overwrite fresh filters). Same guard in `blog.js:49-72`.
- [ ] `ai_seo.js:44-49, 997-1007` — on store change, re-run `loadAeo()`/`loadActivity()` when those tabs are active; stale `window._aeo` currently serves the previous store's robots.txt/llms.txt downloads and previews.
- [ ] `landing_pages.js:1014-1032` — classify poll failure by `task.status === 'failed'`, not error-message substring; `:973-999` — `await onComplete(result)` and toast on error (paid results currently orphaned); `:762-786, 810-826` — make `saveBuilderPage` rethrow and abort publish on save failure (stale content currently published after "Failed to save").
- [ ] `blog.js:13` — move `setupModalDismiss` from seo.js to app.js (currently a ReferenceError on every blog page load, killing the rest of the handler); `:932-942, 1022-1036, 1288-1291` — move pre-step wizard saves inside the existing try/catch with UI reset on failure.
- [ ] `seo.js:2165, 2189` — scope the Apply Changes button selector to the product modal (`#product-modal .btn-primary` or a button id); it currently disables the hidden schema modal's button → no feedback + double-submit double-writes (collection variant at `:2506` is correct — copy it).
- [ ] `brands.js:914-939, 654-659` — store the poll interval in module state; clear it on modal close/reopen (brand A's results currently render in brand B's modal with brand A's apply id).
- [ ] `news.js` link fix is in Fix 15; button guards in Fix 14.

### [x] Fix 44: Frontend polish batch (low) ✅ 2026-07-11 — search inputs keep focus/caret across re-renders, semrush store toast, blog empty-state sync button, wizard null-poll fix, homepage slide-identity image writes + save toast, dead globals removed
- [ ] Search inputs rebuilt mid-typing killing focus: re-render table body only, not the input (`seo.js:626-632, 511-515`, `technical_seo.js:202-206, 311-322, 470-474, 576-587`).
- [ ] `seo.js:1114-1181` — impact modal: backend returns `{"message"}, 404`; return it under `error` (or read `data.message` in apiRequest) so the friendly copy shows instead of "Request failed (404)".
- [ ] `seo.js:3643-3712` — AI Filter: reset `aiFilterApplied` and restore selections in the catch (one failure currently disables the feature until reload).
- [ ] `seo.js:4112` — covered by Fix 33 (read-only preview).
- [ ] `brands.js:546-585` — give delete-confirm its own dialog state (title/button text currently leak from the logo flow: "Generate" button that deletes); `:599-615` — refresh the table after single-logo success; `:239` — attach the container listener once (guard flag).
- [ ] `semrush.js:139-140` — toast "Select a store first" instead of silent no-op.
- [ ] `blog.js:56-63` — add a Sync button to the empty state (first-run dead end); `:1227-1272` — capture the session id when the poll starts (stop polling `/api/blog/wizard/null` after reset).
- [ ] `homepage.js:196-221, 329-347` — write generated images by slide id, not captured index; `:281-285` — try/catch + toast in `save` (silent data loss today).
- [ ] `gsc.js:982-1006` — update `cannibalizationFixes` after single-fix apply (badges stale until reload).
- [ ] `interlinking.js:98-99` — declare or delete `currentBrokenLinks`/`fixedBrokenLinks` (implicit globals).
- [ ] `brand_routes.py:648-654` — stop double-escaping alt text; `:1065-1067` — pass the actual content type (webp/jpeg) to `upload_staged_file` (landing_routes does it right).
- [ ] `gsc_routes.py:594-599, 782-787` — include `action`/`target_keyword`/`item_type`/`item_id` in the `ON CONFLICT DO UPDATE` set so re-fixes don't keep stale metadata.

---

## Phase 12 — Dead code & consolidation

### [x] Fix 45: Delete dead code (low) ✅ 2026-07-11 — dead AI tool defs + discovery fn (~7KB), cannibalization/hierarchy/consolidate/debug endpoints (~21KB), dead validate branches, silent excepts now logged. Kept: `detect-old-blogs` + `_KNOWN_OLD_HANDLES` (live, used by the Redirects UI); `duplicate_detector.py` untouched (user's in-progress work).
- [ ] `ai_agent.py:756-854, 1362-1422` — `KEYWORD_STRATEGY_TOOL_DEFINITIONS` (no handlers exist), `_discover_related_products` + `DISCOVERY_TOOL_DEFINITIONS` + `prompts.product_discovery` (~130 lines, never called). Update CLAUDE.md's "product discovery" description to match.
- [ ] Dead endpoints (zero frontend references, verified): `/api/seo/cannibalization` (`seo_routes.py:368`), `/api/seo/collections/hierarchy/<store_id>` (`seo_routes.py:2402`), `/api/blog/consolidate` + `/preview` + `/status` (`blog_routes.py:769, 794, 976` — ~200 lines incl. thread), `/api/blog/cleanup-landing-duplicates` (`blog_routes.py:1209`), `/api/blog/debug-theme-meta` (`blog_routes.py:2375`), `schema_routes.py:105-114` `_get_store_domain`, `landing_routes.py:278` dead column ref, `:812` unused import.
- [ ] `ai_agent.py:2519-2524` — dead `< 30`/`< 100` branches in `_validate_landing_page_generate` (truncation can't fix too-short): either pad/regenerate or delete the condition.
- [ ] `keyword_strategy_service.py:684-687, 733` — fix the 500-vs-5000 comment drift; remove unused `primary_tracker` param.
- [ ] `content_gap_service.py:775, 813` — build the article URL from the actual blog handle; `ORDER BY id LIMIT 1` → deterministic blog choice (prefer handle `news`, else newest); log `publish_to_all_channels` failures instead of `except: pass`.
- [ ] `blog_routes.py:2436` — move `_KNOWN_OLD_HANDLES` to per-store settings or delete.
- [ ] `duplicate_detector.py:11` — rename `seo_title` → `meta_title` in `DUPLICATE_FIELDS` (or document the mapping) before pages are added to the feature's scope.

### [x] Fix 46: Consolidate drifted duplicate utilities (low) ✅ 2026-07-11 — highest-value items done: `url_utils.url_path_key` fixes the Semrush↔GSC URL-matching bug; analyzer HTML strip no longer merges words across tags; metafieldsSet mutation deduped (4→1). Deferred (maintenance-only, behavior-change risk): stop-words unification, LLM-JSON extractor helper, cannibalization-detector unification, interlinking JSONB blob slimming.
- [ ] **HTML stripping (4 impls):** `seo_analyzer.py:56, 115` (no-space replacement — concatenates words, inflates counts), `technical_seo_analyzer.py:56-59`, `serp_scraper.py` (BS4), `landing_page_generator.py:419-424` → one `strip_html(text)` in a shared module (space-replacement + whitespace collapse).
- [ ] **Stop words (2 impls):** `serp_scraper.py:38-48` frozen hardcode vs `technical_seo_analyzer.py:7-11` settings-driven-but-frozen-at-import → one settings-backed function read per call.
- [ ] **URL→handle matching (4 naive + 1 robust):** `gsc_service.py:754, 1164-1184`, `content_gap_service.py:860-879`, `semrush_import_service.py:906-921` all break on http/www mismatches; `interlinking_analyzer.classify_href` is correct → extract it to `services/url_utils.py` and use everywhere. (This directly fixes Semrush URLs failing to match GSC pages → composite search scores keyed under full URLs.)
- [ ] **Cannibalization detection (3 impls, 3 threshold sets):** `gsc_service.get_cannibalization`, `keyword_strategy_service.get_cannibalization_report`, `content_gap_service.check_cannibalization_risk` → one service function with one threshold config; the UIs currently give conflicting verdicts for the same keyword.
- [ ] **LLM JSON extraction (~6 copies):** one `extract_json(text)` helper (code-fence aware + brace-slice fallback) used by content_gap_service (×3), ai_visibility_tracker (×2), keyword_strategy_service.
- [ ] **`metafieldsSet` mutation string (4 copies in shopify_client.py):** one module-level constant.
- [ ] **`interlinking_analyzer.py:614-657`** — stop storing `all_links` + full per-item lists in one `UNIQUE(store_id)` JSONB row (tens of MB on large stores); store summaries + a separate links table, or gzip the blob.

---

## Verified non-issues (checked during audit — don't re-litigate)
- No SQL injection: every f-string SQL site interpolates only whitelisted ternary identifiers or `%s` placeholder lists (each verified).
- All frontend API paths/methods/payload keys match their routes except the items listed above.
- `database.get_conn`/`get_cursor` never leak connections; rollback-on-exception correct.
- Blueprints registered once; no route conflicts; `PUBLIC_PATHS` prefix matching has no unintended matches.
- Default rule weights sum to 1.0; `ON CONFLICT` targets match UNIQUE constraints everywhere checked.
- Schedulers run once today (gunicorn `--preload`) — fragility addressed in Fix 39.
- Prompt templates using `.format()` double their JSON braces correctly and fall back to defaults on user-template KeyError.
