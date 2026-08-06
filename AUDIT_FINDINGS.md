# End-to-End Audit Findings — August 2026

Full-application audit: all 24 JS files (503 HTTP call sites) cross-referenced against 407 Flask
routes, all 23 templates, all route files, `ai_agent.py` + 15 services, `database.py` (72 tables),
and the then-uncommitted quality-gate/citation-sources changes. Every finding was verified by
reading both sides of the code before being recorded.

**Overall health:** zero dead buttons (every JS call resolves to a real route with the right
method), zero SQL injection, uniform `{"error": str}` shapes, excellent UPSERT hygiene, secrets
encrypted at rest. The real problems: a handful of genuinely broken features, "reporting lies"
(the app claims work happened when it didn't), and a large residue of stale code from redesigns.

Check items off as they are fixed. Line numbers reflect the code as of the audit date and may
drift as files change.

---

## Tier 1 — Bugs in (then-)uncommitted changes

- [ ] **T1.1** `services/ai_visibility_tracker.py` — new `_host()` (:206) does not strip `www.`
  while the new `_cite_domain()` (:45-52) does; for a store whose `primary_domain` is
  `www.example.com`, `cites_you` / own-site classification never matches.
- [ ] **T1.2** `static/js/ai_seo.js:1845-1847` — `loadRuns()` clears models/competitors/brands/
  gap/breakdown panels but not the new `sources-panel` (:2152); a stale sources card persists
  when switching stores (indefinitely if the new store has no runs).
- [ ] **T1.3** (cosmetic) bulk alt-apply path records `apply_result: {applied: true}` even when
  `_normalize_media_updates` dropped every alt.

## Tier 2 — Truthfulness / reporting integrity

The app claims work happened when it didn't. These poison freshness, impact, and queue views
downstream — the systemic root of "inconsistent numbers".

- [ ] **T2.1** Phantom optimizations (products): `routes/seo_routes.py:1143-1152, 1202-1210` —
  apply endpoint inserts a `seo_change_events` row and bumps `last_optimized_at` (:1232,
  unconditional) even when the payload contained no recognized fields and no Shopify call was
  made. The code comment claims events are "recorded only after the Shopify push succeeds".
- [ ] **T2.2** Same for collections: `routes/seo_routes.py:1475-1520` (bump at :1531).
- [ ] **T2.3** Same for blog apply: `routes/blog_routes.py:419-422` — `if update_parts:` is
  always true; an apply with no recognized fields still calls `update_article({})` and bumps
  `last_optimized_at`.
- [ ] **T2.4** Fake completions on unparsable AI output: `routes/ai_seo_routes.py:998/1016,
  1216-1222, 1368/1378` — `{"raw_response": ...}` passes the `.get("error")`-only check;
  previews crash with KeyError→500; `_execute_ai_seo_batch` pushes empty strings, marks items
  **completed**, and still bumps `last_optimized_at`. `task_runner.is_failed_result` exists and
  is unused here.
- [ ] **T2.5** GSC cannibalization analyze/generate-fix return `{"raw_response": ...}` as HTTP
  200 "success" with no analysis/fix: `routes/gsc_routes.py:386-391, 426-427, 541-550`.
- [ ] **T2.6** Unify the three coexisting "AI failed" definitions on `task_runner.is_failed_result`:
  key-presence check at `routes/technical_seo_routes.py:610` (misclassifies a valid result
  carrying `raw_response` debug info as failed), bespoke check at `routes/brand_routes.py:1381`.
- [ ] **T2.7** "Issue" has three definitions — unify: home/nav badge sums stored
  `tsa.issues_count` (includes `url_has_keyword`, counts any check with score≠100)
  (`services/dashboard_service.py:1163-1176`); Issues tab excludes `url_has_keyword` and flags
  only score&lt;60/70 (`routes/seo_issues_routes.py:139-181`); per-item modal builds a third
  list from `ka.checks` only, omitting content-only issues like schema/duplicates
  (`static/js/seo_issues.js:194-204`; also `routes/seo_routes.py:_count_unified_issues:50-60`).
  The home badge always exceeds the Issues-tab total for the same store.
- [ ] **T2.8** "CTR underperformer" defined twice: `services/dashboard_service.py:401`
  (actual &lt; expected×0.8, ≥200 impressions, top-10 only) vs
  `services/seo_priority_service.py:372` (ctr &lt; expected×0.6, &gt;50 impressions) — same
  page flagged by one engine and not the other, both feeding the home queue.
- [ ] **T2.9** Deleted-store bulk tasks complete as 0/N "success" with no error:
  `routes/brand_routes.py:96-102, 937-940, 1331-1334`; `routes/seo_routes.py:1773-1779,
  2006-2012, 2221-2227`.
- [ ] **T2.10** AI-visibility runs where every model query failed are still marked `completed`
  with zero answers and no surfaced error: `services/ai_visibility_tracker.py:816-820` (and
  :975 reanalyze).
- [ ] **T2.11** Schema repush no-op toasts "started": `static/js/seo.js:3316,3332` toast on
  `resp.ok`, but `/api/ai-seo/schema/repush` also returns `ok: true, message: "Nothing to
  repush"` (`routes/ai_seo_routes.py:1714`); the `message` key is never read.
- [ ] **T2.12** Bulk progress can never reach 100%: bulk-titles (`routes/task_routes.py:739-762,
  883-906`) and image bulk upload/replace (`routes/image_routes.py:206-229, 422-445`) insert
  unvalidated ids with `total = len(ids)` while executors' JOINs silently drop unknown ids —
  batch reads as stuck until the 6-hour sweeper force-fails it. (`create_bulk_task` at
  `task_routes.py:204-210` validates correctly — copy it.)
- [ ] **T2.13** `routes/task_routes.py:226` returns `"total": len(product_ids)` while the DB row
  uses `len(valid_ids)` — client total disagrees with every poll (same at :318).
- [ ] **T2.14** Semrush gap keywords where the store doesn't rank at all counted as top-3 wins:
  `services/semrush_import_service.py:537-541` stores own position unguarded (Semrush uses 0 =
  not ranking, proven by `NULLIF(position, 0)` at :926) while `get_overview:898` counts
  `position <= 3`.
- [ ] **T2.15** Activity feed mislabels AI-SEO batches as "Product SEO Optimization":
  `bulk_tasks.task_type` has `DEFAULT 'product'` (`database.py:360-365`) and the GEO/fact-boost/
  schema-repush creators set only `kind` (`routes/ai_seo_routes.py:1299,1541,1780`), so the feed
  discriminator `r["task_type"] or r["kind"]` (`routes/task_routes.py:1189`) always resolves to
  `'product'`; the `BULK_TASK_META` entries at :1103-1105 are unreachable.
- [ ] **T2.16** Interlinking apply batches finishing as `completed_with_errors`
  (`routes/interlinking_routes.py:1095`) vanish from the Activity feed — the ai_tasks branch
  lists only `status IN ('completed','failed')` (`routes/task_routes.py:1166`) though app.js
  defines a badge for it and the sync_jobs branch (:1206) includes it.
- [ ] **T2.17** Local data lies about blog SEO fields: content-gap blog publish records
  `meta_title`/`meta_description` in `synced_articles` that were never sent to Shopify
  (`services/content_gap_service.py:813-822`; `create_article` accepts no SEO fields, no
  follow-up call). Collection branch (:743) does it correctly.

## Tier 3 — Broken features

- [ ] **T3.1** Content-gap image generation has never worked:
  `services/content_gap_service.py:765, 798` calls `client.upload_collection_image(...)` /
  `client.upload_image(...)` — neither exists on `ShopifyClient` (only `staged_upload_create` /
  `upload_staged_file` / `update_collection_image(url)`); `AttributeError` swallowed as "image
  generation failed".
- [ ] **T3.2** Publishing a content gap as a **page** crashes every time:
  `services/content_gap_service.py:834-836` inserts/updates `synced_pages.synced_at` — column
  does not exist (`synced_pages` has `created_at`/`updated_at`).
- [ ] **T3.3** Fresh-database bootstrap broken: `database.py:786-791` runs
  `ALTER TABLE content_gaps` ~400 lines before `CREATE TABLE content_gaps` (:1182) and the
  `DO $$` guard catches only `duplicate_column`, not `undefined_table` — on an empty DB
  `init_tables()` raises and the app never boots (`main.py:123` unguarded). Latent on existing DBs.
- [ ] **T3.4** "Optimize existing" on a gap with no brief → 500:
  `services/content_gap_service.py:865` `gap.get("brief", {})` — key always exists as `None` →
  `.get` raises `AttributeError`. (:608 handles it correctly with `gap.get("brief") or {}`.)
- [ ] **T3.5** AI-SEO entity-authority inflated for every item: `routes/ai_seo_routes.py:317,
  981, 1141, 1363, 1412` pass full `https://…` URL as `store_domain`; the analyzer self-link
  filter (`services/ai_seo_analyzer.py:226-227`) substring-checks against bare hostnames so the
  store's own links always count as outbound authority (+up to 40 pts). Use
  `url_utils.store_host()` (correct at :1117).
- [ ] **T3.6** Image-alt preview ignores `item_type`: `routes/seo_routes.py:693-731` reads only
  `item_id`/`scope`/`text` while JS sends `item_type` (`static/js/seo.js:206,214`) — a
  collection recommendation runs its id against `synced_products` (:719), returning the wrong
  product's images or "No products found".
- [ ] **T3.7** Republish/update of a news article blanks the hero: `routes/news_routes.py:444,
  480-482, 497` hardcodes `"image_url": ""` and the upsert overwrites the stored CDN URL. (Blog
  twin guards this at `routes/blog_routes.py:1461-1465`.)
- [ ] **T3.8** Scoped AI fix silently becomes a full fix:
  `routes/technical_seo_routes.py:527-533, 571` — whitelist-filtered `only_fields` can become
  `[]`, which `if only_fields:` treats as "no restriction".
- [ ] **T3.9** Article/topic product discovery degraded: `ai_agent.py:5276`
  `re.split(r"[,\s]+", f"{topic} {keywords}")` stringifies the keywords **list**; ILIKE terms
  carry `['`, quotes, commas → pre-filter matches nothing → falls back to "300 newest
  products". Should be `" ".join(keywords)`.
- [ ] **T3.10** "View Impact" can send empty `store_id` after a deep link: `static/js/seo.js:
  1291-1293` uses `_getSeoStoreId()` (returns `""`, :951-958); the 400 is swallowed at :1361
  and renders as "No impact data available". (`applyRecommendation`/`confirmApplyAction`
  correctly use `currentItemStoreId || _getSeoStoreId()`.)
- [ ] **T3.11** Draft news save kills resume: `routes/news_routes.py:394-517` — `_step_publish`
  sets session `status='published'` even when `is_published=False`.
- [ ] **T3.12** GSC top-pages/top-queries `sort_by` ignored whenever the live GSC fetch succeeds
  (`routes/gsc_routes.py:228-244` vs `services/gsc_service.py:518-559`); sorting works only on
  the DB fallback.
- [ ] **T3.13** Clearing a field is impossible in technical apply:
  `routes/technical_seo_routes.py:730-731` `if not field or not value: continue` — empty
  string / empty tags can never be applied.
- [ ] **T3.14** Keyword-density false positives: `services/technical_seo_analyzer.py:316-335` —
  fuzzy fallback counts one loose occurrence once per overlapping token window (2-3×
  inflation); short descriptions falsely trip `keyword_stuffing_max` and score 0, and the AI
  fix loop then "fixes" a non-problem.
- [ ] **T3.15** Content-gap intent misclassification: `services/content_gap_service.py:108-128`
  matches signals as raw substrings (`"deal" in "ideal gift"` → transactional, `"top" in
  "laptop"` → commercial); with 4×/3×/1.5× multipliers this reorders the entire gap queue;
  also imported by `keyword_strategy_service.py:10`.
- [ ] **T3.16** `matched_product_id`/`matched_collection_id` dropped by `get_gap`
  (`services/content_gap_service.py:367-382`) so `publish_content:750`'s "add matched product
  to new collection" branch is always falsy — silent permanent no-op.
- [ ] **T3.17** Inbound-link counting wrong: `ai_agent.py:196` (`_get_inbound_links`) —
  `target_patterns` begins with bare `base_path`, making the `?/#/"` variants unreachable and
  prefix-matching longer handles (`/products/foo` matches `/products/foo-bar`); overcounting
  suppresses valid internal-linking recommendations (prompt gates on "fewer than 3 links").
- [ ] **T3.18** Offsite audit failure semantics: `services/offsite_visibility_service.py:309-313`
  — a transient Brave failure on query #1 marks the whole audit failed with a wrong "not
  configured" message; conversely 1 success + 14 failures "completes" silently.
- [ ] **T3.19** GSC image JSONB key mismatch (latent): `routes/gsc_routes.py:516` reads
  `img.get("src")` but sync writes the URL under `"url"` (`shopify_client.py:274-276`) — the
  `src` field in cannibalization-fix previews is always empty.
- [ ] **T3.20** Duplicate grouping normalization drift: `routes/seo_issues_routes.py:294-302`
  uses `.strip().lower()` (no whitespace collapse) vs `duplicate_detector.normalize:15-19`
  (casefold + collapse) — a pair differing by a double space is a duplicate on /duplicates but
  not in the Issues tab.

## Tier 4 — Safety guards & data-loss paths

- [ ] **T4.1** Cannibalization guard fails open: `routes/gsc_routes.py:459-460` wraps the entire
  resolver/ownership check in `except Exception: logger.warning(...)` — any resolver error
  silently disables the protection. Fail closed (cf. `content_gap_routes.py:127-132`).
- [ ] **T4.2** Same gate, opposite polarity in one file: `routes/content_gap_routes.py` —
  `generate_content` fails closed (:120-132, 502) but `optimize_existing` fails **open**
  (:179-196, "proceeding") — and optimize-existing is the flow that rewrites live pages.
- [ ] **T4.3** Keyword-chip re-optimize has no ownership guard: `static/js/seo.js:2178-2182`
  sends any secondary keyword as `target_keyword`; only prompt-level mitigation
  (`ai_agent.py:3053-3063`). Every other retarget path (gsc/content-gap/blog) has a guard.
- [ ] **T4.4** Resolver-bypass endpoint: `POST /api/keyword-strategy/keywords/<id>/assign`
  (`routes/keyword_strategy_routes.py:295-332`) accepts product/collection although its own
  docstring routes them to `POST /api/seo/items/<type>/<id>/primary-keyword` (full resolver
  pipeline) — ownership claiming can be skipped. (Endpoint is also unused by the UI — see T7.)
- [ ] **T4.5** Single-article delete orphans live content: `routes/blog_routes.py:485-493`
  logs-and-ignores a failed Shopify `delete_article`, then deletes the local row — article
  stays live on the storefront, invisible in the app. (Bulk at :531-538 and news at
  `news_routes.py:683-689` do it correctly.)
- [ ] **T4.6** Same swallow on empty-blog landing delete: `routes/blog_routes.py:75-82`
  (`except: pass`) — live orphaned landing article.
- [ ] **T4.7** Landing regen duplicates the live landing article on transient failure:
  `routes/blog_routes.py:108-121 & 995-1008` — `update_article` failure swallowed, then
  unconfirmed `shopify_article_id` triggers a fresh create; the old one becomes an untracked
  live orphan. (news_routes.py:113-125 fixed this pattern.)
- [ ] **T4.8** Synchronous bulk in the request thread: `routes/gsc_routes.py:720-911`
  `bulk_cannibalization_fix` runs AI + Shopify writes inline — gunicorn timeout kills the
  request with some fixes already applied and nothing persisted. Move to a background task.
- [ ] **T4.9** Interlinking partial-failure contradiction:
  `routes/interlinking_routes.py:941-972` — a failure on link N appends `status:"error"` rows
  for **all** links including ones already applied and live on Shopify.
- [ ] **T4.10** `.format()` on user-editable prompts → 500: `routes/gsc_routes.py:525-537,
  787-799, 422-423` — a literal `{`/`}` in the stored cannibalization prompt raises on every
  fix generation. Use `.replace` (cf. `interlinking_routes.py:794-798` and its comment).
- [ ] **T4.11** GSC OAuth: `routes/gsc_routes.py:94-96` — Google may omit `refresh_token` on
  re-grant → `encrypt_value(None)` AttributeError; callback never passes `email` so
  `connected_email` stays NULL. Also :76,98 — raw exception text reflected unencoded into the
  redirect Location (corrupted query string / internal detail leak).
- [ ] **T4.12** `create_store` swallows the connection-test failure: `routes/store_routes.py:
  88-94` — store created silently with NULL shop_name/primary_domain; user learns at sync time.
- [ ] **T4.13** Staged uploads with wrong content type: `routes/brand_routes.py:1067-1068`
  declares webp/jpeg + real filename but calls `upload_staged_file` without
  `content_type`/`filename` (uploads as image/png); `landing_routes.py:1299` passes
  content_type but not filename.
- [ ] **T4.14** Mixed-store bulk bugs: `routes/technical_seo_routes.py:360-372, 411-423` takes
  `run_store_id` from the first item (wrong-store prefetch, skipped ownership dedup for the
  rest); `routes/brand_routes.py:942-952, 1342-1351` bulk executors fetch rows with no
  `store_id` filter while the client is built from the request's store.
- [ ] **T4.15** Multi-store metric collision: `routes/seo_routes.py:295-302, 1365-1372` merges
  per-store GSC/Semrush metric dicts keyed only by `prefix/handle` — same handle in two stores
  → one store's metrics attributed to the other's item.
- [ ] **T4.16** HTML injection into live storefront description: `routes/seo_routes.py:2647-2664`
  interpolates client-supplied `image_url`/`alt_text` into an `<img>` tag unescaped (applied
  via :2686). Low exploitability (single user) but ships publicly.
- [ ] **T4.17** Alt text double-escaped on brand landing pages: `routes/brand_routes.py:657-660`
  (`H&M` → `H&amp;amp;M` live).
- [ ] **T4.18** Empty vendor tags every collection: `routes/brand_routes.py:1512-1514, 1543` —
  missing collection row → `vendor_name=""` → `"" in title` always true → "Related to brand: ".
- [ ] **T4.19** Blog scheduling accepts past `scheduled_at` (publishes on the next 60s tick):
  `routes/blog_routes.py:2079-2171`; news twins reject past dates with rationale
  (`news_routes.py:1341-1347, 1408-1409`).

## Tier 5 — Concurrency, background jobs & lifecycle

- [ ] **T5.1** Leader election returns True on ANY exception: `database.py:105-107`
  (`try_acquire_leader` "assuming leadership") — transient DB error at boot → multiple workers
  run boot recovery and singleton schedulers (duplicate scheduled publishes / GSC pulls).
- [ ] **T5.2** Per-process sync lock: `services/sync_service.py:16-24` uses `threading.Lock`
  under 2 gunicorn workers — two "Sync now" clicks on different workers race the
  `collection_product_map` DELETE + re-insert (:462-477) and stale-product cleanup.
- [ ] **T5.3** Check-then-insert "already running" races (2 workers × 4 threads → duplicate runs
  = real OpenRouter spend): `routes/ai_seo_routes.py:347-358, 810-821, 1931-1938, 2268-2279`;
  `routes/interlinking_routes.py:427-483, 520-600` (no guard at all);
  ai-seo optimize/facts/schema-repush batch creators (no guard);
  `routes/keyword_strategy_routes.py:116-143`; `routes/blog_routes.py:2087-2127` (two clicks →
  two scheduled posts); `routes/store_routes.py:161-185`. DB-enforced single-flight exists only
  for `interlinking_analysis` (`database.py:214-218` partial unique index) — use it as the
  template everywhere.
- [ ] **T5.4** Scan/feed start endpoints 409 on an active run but never sweep stale jobs
  (`routes/ai_seo_routes.py:339, 804, 364-378, 1313`) — after a worker restart mid-scan,
  re-scans 409 until some other endpoint sweeps.
- [ ] **T5.5** Raw `threading.Thread` + never-`running` tasks (no sweeper coverage):
  `routes/landing_routes.py:170, 221-243, 768-790, 806-828, 1204-1324` (five jobs, rows
  inserted `'pending'` and never set `'running'`); `routes/gsc_routes.py:126-136, 161-177,
  322-335` (snapshot thread has no tracking row at all). Route all through `task_runner.spawn`.
- [ ] **T5.6** Sweeper vs long batches: `sweep_stale_jobs` force-fails running jobs (2h/6h) with
  no heartbeat; a legitimately long batch is failed mid-flight, a duplicate can start, then the
  original thread's unconditional final `status='completed'` (`ai_seo_routes.py:1248-1249`,
  `image_routes.py:415-419`, `seo_routes.py:1720-1724`) flips it back — two threads mutating
  the same items.
- [ ] **T5.7** Two competing stale-job sweepers in one file: `routes/ai_seo_routes.py:1253-1271`
  local `_sweep_stale_jobs` (fewer tables, bare `except: pass`) vs the imported task_runner
  one; both used at different call sites.
- [ ] **T5.8** `competitor_check_runs` in neither boot recovery nor `sweep_stale_jobs`
  (`services/competitor_service.py:207,296`) — restart mid-check leaves a ghost `'running'`
  row forever in history.
- [ ] **T5.9** `home_pages` SELECT-then-INSERT race: `routes/homepage_routes.py:37-47` — no
  ON CONFLICT, no exception handling; concurrent first requests → unhandled UniqueViolation 500.
- [ ] **T5.10** `article_wizard_sessions` failure vocabulary split (`'error'` manual at
  `blog_routes.py:1623` vs `'failed'` scheduled at :2068); boot recovery rescues only `auto_*`
  sessions — interrupted manual sessions keep `researching`/`generating` forever.
- [ ] **T5.11** Batch items stuck `'pending'` inside completed batches can never be retried
  (`routes/ai_seo_routes.py:1207-1209, 1463-1465, 1584-1588` — retry selects `'failed'` only);
  `retry_batch` (:1795-1817) also accepts non-AI-SEO batches and mis-runs them.
- [ ] **T5.12** `bulk_apply_tags` / `generate_collections` create `bulk_tasks` rows but never
  insert `bulk_task_items` (`routes/seo_routes.py:1727-1757, 1787-1811`) — no per-item results.
- [ ] **T5.13** Brave rate-limit lock not used by image search:
  `services/serp_scraper.py:481-501` reads/writes `_brave_last_call` without `_brave_lock`
  (web search :277-285 uses it) — concurrent image+web search can trip 429s.
- [ ] **T5.14** Competitor check failures silently vanish: `services/competitor_service.py:214`
  `google_failures` never incremented/read; the `except` at :224 can't fire (brave_search
  swallows internally) — failed keywords just missing from "completed" runs.
- [ ] **T5.15** Naive/aware timestamp mixing (latent): `routes/news_routes.py:421, 509-510`
  write tz-aware datetimes into naive TIMESTAMP columns (everything else writes naive UTC);
  `routes/ai_seo_routes.py:156` compares DB `NOW()` to app-container `datetime.now()`.
- [ ] **T5.16** Per-worker unbounded caches: `routes/dashboard_routes.py:9-43` `_badge_cache`,
  `routes/seo_routes.py:188` `_store_baselines_cache` — never invalidated on writes; 2 workers
  serve divergent counts for up to 60s/1h.
- [ ] **T5.17** Task-type registration gaps: `'article_image'` has no `AI_TASK_META` entry
  (renders via fallback with no owning-page link, `routes/task_routes.py:1052-1089`);
  `routes/blog_routes.py:194-233` inserts a sync task as `'running'` (everything else inserts
  `'pending'`).

## Tier 6 — Keyword & scoring invariants

Invariant verdicts: resolver-everywhere **holds with 4 bypasses**; unified scoring **holds**;
ownership-key semantics **violated**; char-length rules **violated** in ~15 places.

### Resolver bypasses
- [ ] **T6.1** Collection image-alt generation uses raw n-grams, never the resolver:
  `routes/seo_routes.py:2592` (`_generate_image_for_collection`) — the keyword baked into
  generated alts can differ from the resolved primary that `image_alt_has_keyword` later
  scores against. The one remaining content-writing path off-resolver.
- [ ] **T6.2** Priority-queue fallback hands out a demand-only keyword without triggering
  resolution: `services/seo_priority_service.py:332-335` (`target_keyword = unowned[0]`).
- [ ] **T6.3** SERP-scraper title heuristics remain as silent fallbacks:
  `services/serp_scraper.py:137-162, 195-210` when callers pass no `target_keywords`.
- [ ] **T6.4** Resolver exception silently persists an n-gram-scored analysis with
  `keyword_resolution = NULL`: `routes/technical_seo_routes.py:84-85` +
  `services/technical_seo_analyzer.py:346, 593`.
- [ ] **T6.5** Quality-gate keyword fallback can lock in the AI's own choice:
  `ai_agent.py:2946-2948` — with no target and no stored resolution, the validator enforces the
  AI's `target_keywords.primary`.
- [ ] **T6.6** Stored-primary lookups without `store_id` filter (safe only because item_id is a
  global serial): `ai_agent.py:2935-2941, 3543-3548`; `routes/task_routes.py:627-631`.

### Ownership-key schism
- [ ] **T6.7** `services/keyword_coverage.py:58` (`get_ownership_map`) joins resolver primaries
  by **exact string** while primaries are `_norm`-ed and `master_keywords` only `LOWER()`-ed —
  punctuation/double-space keywords show unowned though a page owns them.
- [ ] **T6.8** Three granularities for "same keyword": `variant_key` (dedup/conflicts/claiming),
  `_norm` (priority-queue, blog-wizard, gsc/content-gap guards), exact string (coverage map).
  A product owning "dutch masters" blocks page-level resolution of "dutch master" but does NOT
  trigger the blog-wizard/retarget guards for it. Standardize on one key.
- [ ] **T6.9** Cannibalization substring self-test: `services/seo_priority_service.py:158`
  `this_path not in page_path` treats `products/x-large` as self for `products/x` — misses
  real cannibalization (resolver's own check at `keyword_resolver.py:465` compares exact URLs).

### Scoring band drift
- [ ] **T6.10** `static/js/keyword_strategy.js:586` colors page score green≥80/amber≥60/red&lt;60 —
  every other surface uses 80/50 bands.
- [ ] **T6.11** Dashboard coverage hard-codes ok-bands `meta_title BETWEEN 20 AND 75`,
  `meta_desc BETWEEN 100 AND 200` (`services/dashboard_service.py:800-801`) — the title band
  matches nothing else in the app (live ok-band is 30–70) and ignores settings.
- [ ] **T6.12** Dead settings keys mislead: `prompt_defaults.py:1214-1215, 1237-1238`
  (`rules.technical_*` → `meta_title_length`/`meta_desc_length`) are never read by the analyzer
  (which reads `rules.product_seo`/`collection_seo` at analyzer :415/:655) and disagree with
  the live bands (ok 40–70 vs 30–70). Editing them in Settings silently does nothing.
- [ ] **T6.13** AI tools report a different "SEO score" than the UI: `ai_agent.py:700/1338,
  751/1391` `analyze_seo_score` returns only the content pillar under the name "seo score";
  store-audit average (:1354) likewise.
- [ ] **T6.14** `services/blog_seo_analyzer.py:15-38` re-implements the `score_length` ladder
  inline; in-code default `md_good [140,160]` differs from the settings default `[120,160]`
  (settings win today).

### Char-length harmonization (one source of truth)
- [ ] **T6.15** Blog/news prompt targets fail the app's own article scorer:
  `prompts.blog_seo_suggestions` (50–60, `prompt_defaults.py:639,649`) and
  `prompts.news_analyze` (40–60, :908-909) vs `rules.article_seo` (good 35–46, ok≤55,
  :1170-1171) — titles at 56–60 score "poor" on arrival; the validator's universal "aim ~50"
  (`services/ai_content_validator.py:78`) makes it worse for articles (article targets are
  40–46 + theme suffix per `prompts.article_seo:422`).
- [ ] **T6.16** Quality gate covers only `product_seo`/`collection_seo` (`ai_agent.py:6250-6259`)
  and `technical_seo_fix` (:4661). Ungated: `brand_seo` (:6268-6274), GEO
  `_generate_geo_content` (:2171-2172 — no clamp at all), content-gap publishes (hard 60/160
  clamps only). Extend the gate.
- [ ] **T6.17** Landing/home-page validators clamp title only when &gt;70 (61–70 passes a 60 max)
  and truncate mid-word: `ai_agent.py:2876-2882, 2910-2915` — use rephrase-first +
  `clamp_word_boundary` like the validator.
- [ ] **T6.18** Prompts steering toward overshoot the gate then rephrases: SERP block
  (`services/serp_scraper.py:838-843` "aim 50-60… maximize snippet real estate"),
  `prompts.dedupe_group`, `prompts.cannibalization_fix` (145–160),
  `prompts.keyword_strategy_fix`, content-gap brief prompt (`prompt_defaults.py:1641-1642`).
  Align on ~50/~150 aims.
- [ ] **T6.19** Frontend counters disagree with backend scoring: `static/js/homepage.js:84-85`
  and `static/js/landing_pages.js:105-107` mark description "good" only at 140–160 while
  backend accepts 120–160 — a 125-char description shows amber but scores 100.
- [ ] **T6.20** `_enforce_meta_limits` (`routes/technical_seo_routes.py:22-31`) hand-rolls
  `ai_output_guard._clamp` — identical today, will desynchronize on any band change.
- [ ] **T6.21** Two positional alt-pairing heuristics can disagree when counts mismatch:
  generation-time (`ai_agent.py:6222-6247`) vs apply-time
  (`routes/technical_seo_routes.py:464-519`).

## Tier 7 — Stale UI, dead code & orphaned endpoints

### The one real UX conflict
- [ ] **T7.1** Two competing store selectors on the same screen: global topbar StoreSelector
  (`templates/base.html:21` + `ui.js:227-238` bindLegacy shim) vs unsynced page-local store
  dropdowns on 8 pages — `ai_seo.html:9`, `duplicates.html:9`, `gsc.html:12`,
  `semrush.html:11`, `competitors.html:11`, `content_gaps.html:11`, `homepage.html:10`,
  `landing_pages.html:18`. Switching one doesn't move the other; the two can disagree on one
  screen. Migrate all 8 to the `store:changed` pattern (cf. seo_issues/dashboard/brands).
- [ ] **T7.2** Sidebar badge that can never light up: `templates/_sidebar.html:17`
  `data-badge="optimize"` — `get_nav_badges` (`services/dashboard_service.py:782-791`) never
  emits an `optimize` key. Either wire an aggregate count or delete the span.

### Orphaned endpoints (verified: no JS, template, or backend caller)
- [ ] **T7.3** Superseded keyword-strategy fix flow: `POST /fix/<id>/generate`,
  `GET /fix/<id>/preview`, `POST /fix/<id>/apply`, `POST /fix/bulk-apply`
  (`routes/keyword_strategy_routes.py:541,587,598,613`; only `/fix/<id>/revert` :623 is live);
  `POST /keywords/<id>/assign` (:295, superseded by the primary-keyword endpoint — see T4.4).
- [ ] **T7.4** Orphaned cluster management: `POST /clusters/generate`, `PUT /clusters/<id>`,
  `POST /clusters/merge` (`keyword_strategy_routes.py:365,378,388`).
- [ ] **T7.5** Old GSC pull: `POST /api/gsc/pull` (`gsc_routes.py:161`; JS uses `/pull-sync`);
  `GET /api/gsc/page-matches` (:309).
- [ ] **T7.6** Merged-away technical-seo list surface: `GET /products` (:135),
  `GET /collections` (:207), `GET /analysis/<type>/<id>` (:432), `POST /bulk-fix-apply` (:911)
  in `routes/technical_seo_routes.py`.
- [ ] **T7.7** Blog: `GET/POST /api/blog/blogs` (`blog_routes.py:238,260`),
  `POST /articles/<id>/move` (:432).
- [ ] **T7.8** AI-SEO batches: `GET /batches`, `POST /batches/clear`, `DELETE /batches/<id>`
  (`ai_seo_routes.py:1718,1820,1840`; only `/batches/<id>/retry` is called).
- [ ] **T7.9** Misc: `POST /api/dashboard/revenue/pull` (`dashboard_routes.py:128`),
  `GET /api/settings/defaults/<path:key>` (`settings_routes.py:351`),
  `GET /api/keyword-strategy/runs/<id>` (:665), `POST /api/schema/batch/generate|push`
  (`schema_routes.py:283,288` — note schema_routes' 410-tombstone pattern at :283-290 is the
  model for retiring the rest).

### Dead JS / templates
- [ ] **T7.10** `static/js/keyword_strategy.js:1606-1612` legacy `generateFix`/`previewFix`/
  `applyFix` shims + the never-opened Preview Fix modal
  (`templates/keyword_strategy.html:694-707`, `#ks-preview-modal`).
- [ ] **T7.11** `static/js/seo.js:1674` `confirmDeleteCollection()`; :3579 `loadTagsForStore()`;
  :955 `_getSeoStoreId` reads nonexistent `#store-select` (harmless fallback).
- [ ] **T7.12** `static/js/blog.js:2520` full `revertSeoSnippet` flow (button removed, logic
  kept); :1819 `getSelectedLandingBlogId` reads nonexistent `#landing-blog-select`; :1884
  `getStoreDomain()`; :1895 fallback to nonexistent `#store-select`.
- [ ] **T7.13** `static/js/app.js:134` `showLoading()`; dead 1-line `static/js/auth.js` loaded
  by no template.

### Dead Python
- [ ] **T7.14** `ai_agent.py:1455` `_handle_discovery_tool_call` — zero call sites; the whole
  discovery tool-loop is gone (update CLAUDE.md's "DISCOVERY_TOOL_DEFINITIONS" section too).
- [ ] **T7.15** `services/serp_scraper.py:358-477` `google_search` + `GOOGLE_SEARCH_DELAY`,
  `_google_last_call`, `GOOGLE_USER_AGENTS` (~120 dead lines); :236 `_find_product_in_results`
  unreachable (two-phase self-search never wired — `product_query` always None, the "YOUR
  PRODUCT'S CURRENT SERP PRESENCE" prompt block :786-806 can never render).
- [ ] **T7.16** `shopify_client.py:1432` `create_url_redirect` defined **twice** (:1842 silently
  shadows it; different raise/idempotent semantics) — delete the first; also dead
  `is_smart_collection` (:500), `get_product_metafield` singular (:941), `list_theme_assets`
  (:1922).
- [ ] **T7.17** Dead prompt settings still editable in Settings UI (editing does nothing):
  `prompts.product_discovery` (`prompt_defaults.py:148`, exposed in `settings.js:287`),
  `prompts.action_generate_alts` (:376 — also instructs product-name-in-every-alt, the exact
  stuffing the quality gate prevents; live inline replacement at `seo_routes.py:762-786` is
  correct), `prompts.keyword_strategy_plan`/`keyword_strategy_fix` (:1691,1704).
- [ ] **T7.18** `store_audit` task branch (`ai_agent.py:5778`) + settings-exposed prompt with no
  UI or route that ever creates the task.
- [ ] **T7.19** `prompts.keyword_phase1` (`prompt_defaults.py:156-171`) mandates paginating via
  `get_all_product_summaries` while `ai_agent.py:3297-3327` now embeds the full catalog and
  says "you do NOT need to fetch anything" — contradictory orders in one conversation.
- [ ] **T7.20** Tool-schema promises vs handlers: `get_keyword_opportunities` promises CTR
  opportunities, returns only striking-distance (`ai_agent.py:793-808` vs :1405-1412);
  `get_store_summary` promises "top issues", returns only counts (:717 vs :1340-1364).
- [ ] **T7.21** Dead imports/locals: unused `import threading` in seo/image/homepage/competitor/
  content_gap/task/technical_seo/brand routes; `image_routes.py:11` `sweep_stale_jobs` unused;
  `seo_routes.py:697-698, 864-865` unused `_gs`/`_defs`; `landing_routes.py:11`
  `extract_page_content`, :280 nonexistent `shopify_page_id` read, :837 inner `import html`;
  `technical_seo_routes.py:1193-1199` `_get_stored_analysis` (zero callers);
  `ai_seo_routes.py` `_published_site_schema_types`, dup datetime imports,
  `_fact_density_score` ignored params, `_score_items` return ignored; `gsc_routes.py:6`
  `url_for`; `keyword_strategy_routes.py:141-143` unreachable 500;
  `blog_routes.py` `s.id as sid` never read (:376,:511), phantom `backup` (:2927/:2940),
  store-specific `_KNOWN_OLD_HANDLES` in shared code (:3014);
  `services/offsite_visibility_service.py:208` unused `brand_terms`, :161 docstring promises a
  3-tuple, returns 2.

### Orphaned DB objects
- [ ] **T7.22** Tables never read or written: `seo_audits` (`database.py:188-197`),
  `blog_migrations` (:624-636), `ai_excluded_brands` (:1452-1461, migration-only, re-runs every
  boot).
- [ ] **T7.23** Dead columns: `master_keyword_clusters.parent_cluster_id` (:1267),
  `semrush_keywords.data_timestamp` (:1158), `competitors.notes` (:1214). Write-only:
  `ai_mention_results.tokens_used`, `synced_blogs.comment_policy`,
  `master_keywords.ai_classified_at`.
- [ ] **T7.24** Cosmetic payload bloat: `duplicates.js:275` sends `change_type: "duplicate_fix"`
  that article/page apply routes drop (no `seo_change_events` row → article/page dedupe fixes
  never show in Impact views — `blog_routes.py:371-393`, `landing_routes.py:249-293`, unlike
  product/collection at `seo_routes.py:1096,1429`); `seo.js:199` sends unread `item_id`;
  `duplicates.js:204` posts whole duplicate items (unread fields).

### Terminology (cosmetic)
- [ ] **T7.25** "AI Search" (sidebar `_sidebar.html:44`, cmdk) vs "AI SEO — GEO · AEO · LLMO"
  (`ai_seo.html:3,5`) — both on the same page; "Brands" vs "Brand Collections"; "Home Page" vs
  "Home Page Carousel"; "Home" vs tab title "Dashboard"; `_sidebar.html:81` label "Insights"
  with `data-group="research"`.

## Tier 8 — Convention drift (fix as a policy, then sweep)

- [ ] **T8.1** Success-shape drift: `{"ok": true}` vs `{"success": true}` vs both vs
  `{"status":"started"}`/`{"updated":true}`/`{"assigned":true}` vs bare arrays vs raw AI JSON.
  Pick one envelope.
- [ ] **T8.2** Missing-`store_id` handling drift: 400 vs `200 []`/`{}` vs 404-with-None across
  sibling endpoints; three near-identical `_get_store_id()` copies with different fallback
  chains; seo_routes list endpoints scan ALL stores when store_id omitted.
- [ ] **T8.3** Status-code drift: "already running" 409 vs 400 (`ai_seo_routes.py:1902`);
  upstream failure 502 vs 400 vs 500; `interlinking_routes.py:1332-1353` returns 500 for the
  same reason strings `apply_fix` maps to 404/422; unvalidated numeric input → 500 instead of
  400 (`technical_seo_routes.py:323,325`; `interlinking_routes.py:550`;
  `keyword_strategy_routes.py:330`; `seo_issues_routes.py:441-442,484`);
  `update_store`/`delete_store` return ok:true for nonexistent ids
  (`store_routes.py:109-140`).
- [ ] **T8.4** Side-effectful GETs: `GET /api/images/scan?refresh=1` (Shopify fetches + DB
  writes, `image_routes.py:100-102`); landing seo GETs write `seo_score`
  (`landing_routes.py:194-207, 522-541`); `GET /api/brands/collections` runs link-repair
  writes (`brand_routes.py:221`).
- [ ] **T8.5** Copy-then-diverge duplicates to consolidate: `_regen_landing_async`
  (`blog_routes.py:32-138`) vs `run_landing` (:866-1041); `_parse_scheduled_at` byte-identical
  twice (`blog_routes.py:1896`, `news_routes.py:1311`); blog↔news scheduled-publish quartet
  (already drifted — T4.19); technical-seo apply single vs bulk
  (`technical_seo_routes.py:717-835` vs :1051-1151, field-name drift `value` vs
  `proposed_value`); brand apply duplicated (:1251-1288 vs :1384-1407); landing ai-optimize
  duplicated (:210-244 vs :755-791).
- [ ] **T8.6** Duplicate polling surfaces: `GET /api/ai-seo/scan/<id>` vs `GET /api/tasks/<id>`;
  `GET /api/ai-seo/optimize/batch/<id>` vs `GET /api/tasks/bulk/<id>` (only task_routes
  versions sweep). Two divergent bulk-executor frameworks on the same tables.
- [ ] **T8.7** Two cannibalization systems with no shared code: `/api/gsc/cannibalization*`
  (GSC-query based, writes fixes) vs `/api/keyword-strategy/audit/cannibalization`
  (content_keyword_map, read-only).
- [ ] **T8.8** Blueprint boundary leaks: `/api/dashboard/{snapshot,score-trends,improvements}`
  live in `gsc_routes.py:322-356` (13 siblings in dashboard_routes);
  `/api/seo/items/.../primary-keyword` lives in the technical blueprint.
- [ ] **T8.9** Two landing-page pipelines (brand `brand_landing_pages` vs builder
  `landing_pages`) publishing Shopify Pages via unrelated code/version models.
- [ ] **T8.10** `item_type` unvalidated in technical routes (any string → treated as collection,
  stored verbatim) while siblings validate; `get_analysis` (:432-438) queries without store
  scope (`technical_seo_routes.py:273-295, 432, 522`).
- [ ] **T8.11** Hardcoded inline prompt where siblings use editable settings:
  image-alt preview prompt (`seo_routes.py:762-773`) vs internal_linking/faq `prompts.*`.
- [ ] **T8.12** `request.json` at `blog_routes.py:2288` (415 on wrong content type) vs
  `get_json(silent=True)` everywhere else.
- [ ] **T8.13** Settings frozen at import: `services/technical_seo_analyzer.py:12,18`
  `STOP_WORDS`/`NGRAM_MULTIPLIER` (edits inert until restart); asymmetric `NGRAM_MULTIPLIER[n]`
  (:211, KeyError-able) vs `.get(n, 2.0)` (:250); product scorer iterates `for k in weights`
  (:578, KeyError on custom weights) vs collection `for k in checks` (:783).
- [ ] **T8.14** Service→route→service circular import triangle:
  `keyword_resolver.get_or_refresh_resolution:657` ↔ `technical_seo_routes` ↔
  `analysis_refresh` (works via deferred imports; fragile).

## Tier 9 — Security (single-user LAN context; all low)

- [ ] **T9.1** No `MAX_CONTENT_LENGTH` anywhere; Semrush CSV upload buffers the whole body into
  RAM before the 50MB check (`routes/semrush_routes.py:34-36, 58-60`).
- [ ] **T9.2** `POST /api/tasks` accepts arbitrary `task_type`/`input` and spawns `run_task` —
  authenticated OpenRouter budget burn with zero validation (`routes/task_routes.py:56-75`).
- [ ] **T9.3** Server-side GET of arbitrary external image URLs (Brave results steered by user
  keywords), no scheme/IP/redirect validation (`routes/brand_routes.py:1030-1035`).
- [ ] **T9.4** No rate limiting/lockout on `/api/login` + documented default `admin:admin`
  (`routes/auth_routes.py:16-37`, `auth.py:43-50`) — fine on LAN, not if exposed.
- [ ] **T9.5** `templates/dashboard.html:164` loads Chart.js from cdn.jsdelivr.net — the only
  external asset; charts silently break offline. Vendor it.

Positive: API keys/OAuth tokens Fernet-encrypted and never serialized into responses; GSC CSRF
`state` checked with `session.pop`; no path traversal; auth enforced globally via
`before_request` (`main.py:75-84`).

## Tier 10 — Performance (indexes)

- [ ] **T10.1** `ai_tasks` — no status index; append-only (zero DELETEs in the codebase);
  `/api/tasks/active?count_only` (`task_routes.py:1141-1155`) seq-scans it (and `bulk_tasks`)
  on every UI poll. Add partial index `(status) WHERE status IN ('pending','running')` + one on
  `completed_at`; consider pruning.
- [ ] **T10.2** `bulk_task_items.bulk_task_id` — FK with no index; polled repeatedly during
  bulk runs, never pruned.
- [ ] **T10.3** `collection_product_map` — no product-side index; reverse lookups by
  `(store_id, product_shopify_id)` (`seo_routes.py:711`, `landing_routes.py:1071,1248`,
  `ai_agent.py:336`) can only use the `store_id` prefix of the UNIQUE index → per-store scan
  in nested loops.

---

## Recommended fix order

1. **Tier 1** — uncommitted-change bugs (cheap, blocks the current commit).
2. **Tier 2** — truthfulness cluster (systemic; poisons every freshness/impact/queue view).
3. **Tier 3** — broken features.
4. **Tier 4** — guards & data-loss paths (fail-closed cannibalization guard, ownership guard on
   keyword chips, delete-swallow fixes).
5. **T2.7/T2.8 + Tier 6** — one issue definition, one ownership key, one char-length source of
   truth, extend the quality gate.
6. **T7.1** store-selector unification, then the dead-code sweep (rest of Tier 7).
7. **Tier 5** — DB-enforced single-flight (copy the `interlinking_analysis` partial-index
   pattern), route background work through `task_runner.spawn`, leader-election fail-closed.
8. **Tiers 8–10** — convention policy + sweep, security hardening, indexes.
