# Application Audit — Verified Findings & Action Plan

**This is the single source of truth.** It supersedes `AUDIT_FINDINGS.md`. Every finding from
the original August 2026 audit was independently re-verified against the code (both sides of
every claim read, whole app searched for compensating context). Findings that turned out to be
wrong were removed to the "No action needed" section at the bottom; findings whose details were
wrong have been rewritten here with the **corrected** facts, so you can fix straight from this
document without cross-referencing anything.

Each item has:
- **Problem** — what is actually wrong, as verified in the code.
- **Fix** — concrete action steps with file references.
- **Benefit** — what improves for you as the user of the app, in plain words.

Check items off as they are fixed. Line numbers reflect the code at verification time and may
drift.

**Verification stats:** 144 original findings → 104 confirmed as written, 37 confirmed with
corrections (folded in below), 3 refuted (bottom section), plus 8 new issues discovered during
verification (integrated below, marked **NEW**).

---

## Recommended fix order

1. **Part 1** — bugs in the current uncommitted changes (cheap; do before committing).
2. **Part 2** — truthfulness fixes (the app claims work happened when it didn't — this poisons
   every freshness, impact, and queue number downstream; the root of "inconsistent numbers").
3. **Part 3** — broken features (things that simply don't work).
4. **Part 4** — safety guards & data-loss paths.
5. **Part 5** — one issue definition, one keyword key, one length rule (consistency invariants).
6. **Part 6** — store-selector unification, then the dead-code sweep.
7. **Part 7** — concurrency & background-job hardening.
8. **Parts 8–10** — convention cleanup, security hardening, database indexes.

---

# Part 1 — Bugs in current uncommitted changes

- [x] **1.1 AI-visibility "cites you" never matches for www domains**
  **Problem:** `_host()` (`services/ai_visibility_tracker.py:206`) does not strip `www.`, but
  `_cite_domain()` (:48-55) does. For a store whose `primary_domain` is `www.example.com`, the
  `cites_you` flag and "own site" source classification can structurally never match.
  `_lookup_official_domain` (:724) has the same flaw.
  **Fix:** Strip `www.` inside `_host()` (one line), so both sides normalize identically.
  **Benefit:** The AI Search report will correctly show when AI models cite *your own store*,
  instead of always counting your site as "not cited".

- [x] **1.2 Stale "Sources" card when switching stores**
  **Problem:** `loadRuns()` (`static/js/ai_seo.js:1845-1847`) clears the models, competitors,
  brands, gap and breakdown panels but not the newer `sources-panel` (:2152). Switching to a
  store with no runs leaves the previous store's sources card on screen indefinitely.
  **Fix:** Add `"sources-panel"` to the clear list in `loadRuns()`.
  **Benefit:** No more seeing another store's citation sources on the wrong store's page.

- [x] **1.3 Alt-text apply reports "applied" when nothing was applied** *(also bumps freshness)*
  **Problem:** In `routes/technical_seo_routes.py`, when `_normalize_media_updates` drops every
  alt entry (nothing resolvable), no Shopify call is made — yet the bulk executor (~:1130-1134)
  still records `apply_result: {applied: true}`, bumps `last_optimized_at` (:1117/:1128), and
  marks the item completed. The single-item apply endpoint (~:752-806) has the same no-op bump.
  **Fix:** In both paths, only record success and bump `last_optimized_at` when at least one
  Shopify field or media update was actually sent.
  **Benefit:** The app stops telling you an image fix happened when it didn't, so the "recently
  optimized" and queue views stay honest.

---

# Part 2 — Truthfulness: the app must never claim work it didn't do

These all share one theme: a change event is recorded or `last_optimized_at` is bumped without a
real Shopify write. Fixing them makes every downstream number (freshness, impact, queues)
trustworthy again.

- [x] **2.1 Phantom product optimizations**
  **Problem:** The product apply endpoint (`routes/seo_routes.py`) inserts a `seo_change_events`
  row (:1202-1210) and bumps `last_optimized_at` unconditionally (:1232) even when the payload
  contained no recognized fields and no Shopify call was made. The code comment claims events
  are "recorded only after the Shopify push succeeds" — that only guards *failed* pushes, not
  absent ones.
  **Fix:** Guard both the event insert and the timestamp bump on "at least one field was
  actually pushed to Shopify". Reject empty-payload applies with a 400.
  **Benefit:** "Optimized" means optimized. Items stop disappearing from your work queue when
  nothing actually changed on the store.

- [x] **2.2 Phantom collection optimizations**
  **Problem:** Identical structure in the collection apply (`seo_routes.py:1475-1520`, bump at
  :1531).
  **Fix:** Same guard as 2.1.
  **Benefit:** Same as 2.1, for collections.

- [x] **2.3 Phantom blog-article optimizations**
  **Problem:** In `routes/blog_routes.py`, `"last_optimized_at = NOW()"` is appended (:419)
  *before* the `if update_parts:` check (:422), making it always true; an apply with no
  recognized fields still calls `update_article({})` (an empty Shopify mutation) and bumps the
  timestamp.
  **Fix:** Build `update_parts` from real fields only; skip the Shopify call and the bump when
  there are none.
  **Benefit:** Blog freshness data stays honest; no pointless empty API calls to Shopify.

- [x] **2.4 GSC cannibalization returns garbage as success**
  **Problem:** `_ai_json_call` (`routes/gsc_routes.py:379-391`) returns `{"raw_response": ...}`
  when the AI reply contains no JSON. Both the analyze endpoint (:426-427) and generate-fix
  (:541-550) return that as HTTP 200 "success" with no analysis or fix in it.
  **Fix:** Check for the `raw_response` key (use `task_runner.is_failed_result`) and return a
  502 with a clear "AI returned an unusable response — try again" error.
  **Benefit:** When the AI stumbles, you see a clear retry message instead of an empty result
  that looks like "no problems found".

- [x] **2.5 Unify the "AI failed" definition**
  **Problem:** Three coexisting definitions of "the AI call failed": `task_runner.
  is_failed_result` (unused in the files below), a key-presence check at
  `routes/technical_seo_routes.py:609` (and :1031), and a bespoke check at
  `routes/brand_routes.py:1381`. Verified: no *actual* misclassification occurs today — but the
  duplication guarantees future drift.
  **Fix:** Replace both local checks with `task_runner.is_failed_result`.
  **Benefit:** One consistent rule for what counts as an AI failure, so error handling can't
  silently diverge between pages in the future.

- [x] **2.6 Three different definitions of "issue" — numbers never agree**
  **Problem (verified, direction corrected):** The home/nav badge sums stored
  `tsa.issues_count` (`services/dashboard_service.py:1163-1176`), which counts every check with
  an issue message including `url_has_keyword`. The Issues tab
  (`routes/seo_issues_routes.py:139-181`) excludes `url_has_keyword` and content-dedup keys and
  flags only scores <60/70 — but it also *adds* duplicate-meta, schema, and live-content issues.
  The per-item modal (`static/js/seo_issues.js:194-204`) builds a third list from `ka.checks`
  only. So the three surfaces disagree in *both* directions (the original audit's claim that
  the badge always exceeds the tab was wrong — but disagreement is confirmed).
  **Fix:** Define one server-side "issue list" function used by all three surfaces: the badge
  count, the Issues-tab query, and the modal (have the modal fetch the server list instead of
  rebuilding it in JS). Include the schema/duplicate categories consistently.
  **Benefit:** The issue count on the sidebar, the Issues tab, and each item's popup will
  finally show the same number — no more "which number is right?".

- [x] **2.7 Two definitions of "CTR underperformer"**
  **Problem:** `services/dashboard_service.py:401` (actual < expected×0.8, ≥200 impressions,
  top-10 only) vs `services/seo_priority_service.py:372` (ctr < expected×0.6, >50 impressions,
  no position limit; additionally only fires when no other recommendation exists). Both feed
  the same home "Quick wins" queue.
  **Fix:** Extract one `is_ctr_underperformer(...)` helper with a single threshold set; call it
  from both engines.
  **Benefit:** A page is either a CTR opportunity or it isn't — the home page stops flagging
  and unflagging the same page depending on which engine looked at it.

- [x] **2.8 Deleted-store bulk tasks finish as "success" with 0 results**
  **Problem:** Six bulk executors (`routes/brand_routes.py:96-102, 937-940, 1331-1334`;
  `routes/seo_routes.py:1773-1779, 2006-2012, 2221-2227`) mark the task `completed` and return
  when the store row is missing — no error, 0/N done, all items left pending.
  **Fix:** Mark the task `failed` with error "Store no longer exists" instead.
  **Benefit:** Rare, but when it happens you'll see an honest failure instead of a green
  checkmark on a job that did nothing.

- [x] **2.9 AI-visibility runs where every model query failed still show "completed"**
  **Problem:** Every model-query exception is swallowed with a warning
  (`services/ai_visibility_tracker.py:816-820`) and the run always ends `completed` — a run
  with zero answers looks like a clean run, and the summary (asked == planned) actively masks
  it.
  **Fix:** Count failures; if all queries failed, mark the run `failed` with the last error;
  if some failed, store a partial-failure note surfaced in the run summary.
  **Benefit:** You can tell the difference between "AI models don't mention your brand" and
  "the check itself broke" — critical distinction for trusting the report.

- [x] **2.10 Schema repush no-op toasts "started"**
  **Problem:** `/api/ai-seo/schema/repush` can return `ok: true, message: "Nothing to repush"`
  (`routes/ai_seo_routes.py:1714`), but the JS (`static/js/seo.js:3316, 3332`) toasts "started
  — progress in the background tasks panel" on any OK response; `message` is never read.
  **Fix:** In both callers, show `resp.message` when `resp.batches` is empty.
  **Benefit:** No more hunting the tasks panel for a job that was never created.

- [x] **2.11 Bulk progress bars silently under-report** *(corrected consequence)*
  **Problem:** Bulk-titles (`routes/task_routes.py:739-762, 883-906`) and image bulk
  upload/replace (`routes/image_routes.py:206-229, 422-445`) insert unvalidated ids with
  `total = len(ids)`; executors' JOINs silently drop unknown ids. Verified correction: the
  batch does **not** hang — executors mark it `completed` unconditionally — but progress shows
  e.g. 8/10 forever and the orphaned `bulk_task_items` stay `pending` permanently.
  **Fix:** Copy the validation pattern from `create_bulk_task` (`task_routes.py:204-210`):
  validate ids against the DB first, insert only valid ones, set `total` accordingly.
  **Benefit:** Progress bars reach 100% and reflect reality.

- [x] **2.12 API "total" disagrees between creation response and polling**
  **Problem:** `routes/task_routes.py:226` (and :318) return `total: len(submitted_ids)` while
  the DB row stores `len(valid_ids)` — every subsequent poll disagrees with the creation
  response.
  **Fix:** Return `len(valid_ids)` in the creation response.
  **Benefit:** Consistent numbers between "job started" and "job progress".

- [x] **2.13 Semrush: keywords you don't rank for counted as top-3 wins**
  **Problem:** In the gap import (`services/semrush_import_service.py:537-541`), your own
  position is stored unguarded — Semrush uses `0` = "not ranking" (proven by
  `NULLIF(position, 0)` at :926) — while competitor positions two lines above ARE guarded.
  `get_overview` (:898) then counts `position <= 3` as a top-3 win, and 0s poison AVG(position).
  **Fix:** Apply the same `val and val > 0` guard (store NULL for 0), and backfill:
  `UPDATE semrush_keywords SET position = NULL WHERE position = 0`.
  **Benefit:** Your Semrush overview stops overstating how many keywords you win — the numbers
  become real.

- [x] **2.14 Activity feed mislabels every AI-SEO batch as "Product SEO Optimization"**
  **Problem:** `bulk_tasks.task_type` defaults to `'product'` (`database.py:360-365`) and the
  GEO / fact-boost / schema-repush creators set only `kind`
  (`routes/ai_seo_routes.py:1299, 1541, 1780`), so the feed discriminator
  `r["task_type"] or r["kind"]` (`routes/task_routes.py:1189`) always resolves to `'product'`.
  Bonus verified effect: they're also mis-bucketed into the "SEO" filter group instead of
  "AI Search".
  **Fix:** Set `task_type` = the kind in those three INSERT statements (or change the
  discriminator to prefer `kind`).
  **Benefit:** The Activity feed tells you what each job actually was, and filters work.

- [x] **2.15 Interlinking batches that finish "with errors" vanish from Activity**
  **Problem:** Interlinking apply batches can end `completed_with_errors`
  (`routes/interlinking_routes.py:1095`), but the Activity feed's ai_tasks branch lists only
  `('completed','failed')` (`routes/task_routes.py:1166`) — even though app.js has a
  "Done w/ errors" badge and the sync_jobs branch includes the status.
  **Fix:** Add `'completed_with_errors'` to the ai_tasks branch status list.
  **Benefit:** Jobs that partially failed stay visible so you can review what went wrong.

- [x] **2.16 Content-gap blog publish records SEO fields never sent to Shopify**
  **Problem:** The blog branch (`services/content_gap_service.py:813-822`) writes
  `meta_title`/`meta_description` into `synced_articles` but `create_article` accepts no SEO
  fields and no follow-up call is made — local data claims metadata Shopify never received.
  (`news_routes.py:469` shows the correct pattern: call `set_article_seo` right after create.)
  **Fix:** After `create_article`, call `client.set_article_seo(...)` with the meta fields.
  **Benefit:** Articles published from Content Gaps actually get their SEO title/description on
  your live store — not just in the app's database.

---

# Part 3 — Broken features

- [x] **3.1 Content-gap image generation has never worked**
  **Problem:** `services/content_gap_service.py:765, 798` call `client.upload_collection_image`
  / `client.upload_image` — neither exists on `ShopifyClient`. The `AttributeError` is
  swallowed and logged as "image generation failed", so generated images are thrown away every
  time.
  **Fix:** Use the real upload pipeline: `staged_upload_create` → `upload_staged_file` → then
  `update_collection_image(url)` for collections / article image field for articles (copy the
  working pattern from `brand_routes.py` or `news_routes.py`).
  **Benefit:** AI-generated images for content-gap publishes finally appear on your store.

- [x] **3.2 Publishing a content gap as a Page crashes every time (and orphans a live page)**
  **Problem:** `services/content_gap_service.py:834-836` writes `synced_pages.synced_at` — the
  column doesn't exist. Verified aggravation: the Shopify page is created *before* the crash,
  so every attempt leaves a live page on your store that the app doesn't track, and the gap
  never reaches `published`.
  **Fix:** Change `synced_at` to `updated_at` in the insert/upsert.
  **Benefit:** "Publish as page" works, and no more stray untracked pages accumulating on the
  store.

- [x] **3.3 A fresh database cannot boot the app**
  **Problem:** `database.py:786-791` runs `ALTER TABLE content_gaps` ~400 lines before
  `CREATE TABLE content_gaps` (:1183), and the guard catches only `duplicate_column`, not
  `undefined_table`. On an empty database `init_tables()` raises and the app never starts
  (`main.py:123` is unguarded). Harmless on existing databases — until you need to rebuild.
  **Fix:** Move the ALTER after the CREATE (or add `undefined_table` to the exception guard).
  **Benefit:** Disaster recovery works — you can stand the app up on a fresh database again.

- [x] **3.4 "Optimize existing" on a gap with no brief → error 500**
  **Problem:** `services/content_gap_service.py:865` uses `gap.get("brief", {})`, but the key
  always exists as `None` (nullable column), so `.get(...)` on `None` raises. Line :608 already
  does it correctly with `gap.get("brief") or {}`.
  **Fix:** Change to `(gap.get("brief") or {})`.
  **Benefit:** Optimizing an existing page from a content gap works even when no brief was
  generated.

- [x] **3.5 AI-SEO "entity authority" score inflated for every item**
  **Problem:** Five call sites in `routes/ai_seo_routes.py` (:317, :981, :1141, :1363, :1412)
  pass the full `https://…` URL as `store_domain`; the analyzer's self-link filter
  (`services/ai_seo_analyzer.py:227`) substring-checks bare hostnames, so
  `"https://example.com"` never matches `"example.com"` and your own internal links count as
  outbound authority (+up to 40 points).
  **Fix:** Pass `url_utils.store_host(store)` instead (already done correctly at :1117).
  **Benefit:** AI-SEO scores stop being artificially inflated — you see which pages genuinely
  lack authoritative outbound references.

- [x] **3.6 Republishing a news article wipes its stored hero-image URL**
  **Problem:** The update path in `routes/news_routes.py` hand-builds the result with hardcoded
  `"image_url": ""` (:444), so the upsert (:497) overwrites the stored CDN URL with an empty
  string on every republish. (This blanks the *local record*; the blog twin guards it at
  `blog_routes.py:1461-1465`.)
  **Fix:** Fall back to the session's `hero_image_url` / previously stored value when the API
  result has no image URL (copy the blog guard).
  **Benefit:** News articles keep their hero image association in the app after updates.

- [x] **3.7 Scoped AI fix can silently become a full fix (latent API bug)**
  **Problem:** `routes/technical_seo_routes.py:527-533, 571` — if every entry in `only_fields`
  is filtered out by the whitelist, the empty list is treated as "no restriction" and the AI
  fixes everything. Verified: unreachable from the current UI (the only caller sends valid
  fields), but any future/API caller trips it.
  **Fix:** If the filtered list is empty but the original wasn't, return 400 "no valid fields".
  **Benefit:** A request to fix one field can never quietly rewrite the whole item.

- [x] **3.8 Article product discovery degraded by stringified keyword list**
  **Problem:** `ai_agent.py:5276` does `re.split(r"[,\s]+", f"{topic} {keywords}")` where
  `keywords` is a **list** — its `repr` contaminates edge tokens with `['`, quotes, commas,
  wasting slots in the 8-term cap. Verified correction: it's *degraded*, not dead — topic words
  and interior keyword words still match.
  **Fix:** Use `f"{topic} {' '.join(keywords)}"`.
  **Benefit:** Blog-article product linking picks from better-matched products more often.

- [x] **3.9 "View Impact" fails after deep links from Home/GSC**
  **Problem:** `static/js/seo.js:1291-1293` uses only `_getSeoStoreId()` (can return `""` after
  a deep link that omits `store_id` — dashboard.js:389 and gsc.js:535 both omit it), producing
  a 400. Verified correction: the panel shows the server's "store_id and item_id required"
  message, not "No impact data available" — still broken for the user.
  **Fix:** Use `currentItemStoreId || _getSeoStoreId()` like `applyRecommendation` does; also
  include `store_id` in the dashboard/gsc deep links.
  **Benefit:** "View Impact" works no matter how you navigated to the item.

- [x] **3.10 Draft news article gets mislabeled "published" internally**
  **Problem (impact corrected):** `_step_publish` (`routes/news_routes.py:515`) sets the wizard
  session `status='published'` even when saving a draft (`is_published=False`). Verified: there
  is no resume feature this breaks, but the status is a lie, and separately there is **no UI at
  all to later publish a saved draft** (articles list offers only Images/Delete).
  **Fix:** Set status `'draft'` when `is_published` is false; add a "Publish" action for draft
  articles in the news list.
  **Benefit:** Drafts are labeled as drafts, and you can actually publish them later without
  redoing the wizard.

- [x] **3.11 GSC top-pages/top-queries sorting silently ignored**
  **Problem:** `sort_by` is applied only in the DB fallback; when the live GSC fetch succeeds
  (`services/gsc_service.py:518-559`) results return in GSC's default order. Latent today (the
  UI doesn't send `sort_by`) but the API contract is broken.
  **Fix:** Sort the live results by `sort_by` before returning (or remove the parameter).
  **Benefit:** Sorting behaves the same whether data came live from Google or from cache.

- [x] **3.12 Clearing a field is impossible in technical apply**
  **Problem:** `routes/technical_seo_routes.py:730-731` — `if not field or not value: continue`
  skips empty strings and empty lists, so no fix can ever blank a field (e.g. remove a bad meta
  or clear tags).
  **Fix:** Distinguish "absent" from "empty": `if not field or value is None: continue`, and
  let explicit `""`/`[]` through.
  **Benefit:** Fixes that *remove* bad content (not just replace it) become possible.

- [x] **3.13 False "keyword stuffing" alarms on short descriptions**
  **Problem:** The fuzzy fallback in `_keyword_density`
  (`services/technical_seo_analyzer.py:316-335`) counts one loose occurrence once per
  overlapping token window — 2-3× inflation. Short descriptions falsely trip
  `keyword_stuffing_max` (default 4.0), score 0, and the AI fix loop is then told to "fix" a
  non-problem.
  **Fix:** Count non-overlapping occurrences (advance the window past a match, or divide by
  window multiplicity).
  **Benefit:** No more phantom "keyword stuffing" warnings on perfectly fine short
  descriptions — and no AI rewrites triggered by them.

- [x] **3.14 Content-gap intent misclassified by substring matching**
  **Problem (impact bounded):** `classify_intent` (`services/content_gap_service.py:108-119`)
  matches signals as raw substrings — `"deal" in "ideal gift"` → transactional (4× multiplier
  vs the 3× default). Verified: individual scores shift by bounded ratios (up to ~2.7×),
  perturbing gap ordering; also imported by `keyword_strategy_service`.
  **Fix:** Match on word boundaries (split the keyword into tokens, or use `\b` regexes).
  **Benefit:** Content-gap priorities reflect real buyer intent, not accidental letter
  overlaps.

- [x] **3.15 "Add matched product to new collection" never happens**
  **Problem:** `get_gap` (`services/content_gap_service.py:367-382`) omits
  `matched_product_id`/`matched_collection_id` from its return dict, and `publish_content`
  (:750) fetches the gap only through it — the branch is permanently falsy, with no log.
  **Fix:** Add both fields to `get_gap`'s return dict.
  **Benefit:** New collections published from gaps start with their matched product in them,
  as designed.

- [x] **3.16 Internal-link counting overcounts, suppressing link recommendations**
  **Problem:** `_get_inbound_links` (`ai_agent.py:196`) puts bare `base_path` first in
  `target_patterns`, so the loop always matches it and the `?/#/"` delimiter variants are dead
  code — `/products/foo` matches `/products/foo-bar` too. The inflated count feeds the prompt
  gate "do NOT recommend internal_linking if 3+ inbound links".
  **Fix:** Remove the bare pattern; match `base_path` followed by a delimiter (`"`, `'`, `?`,
  `#`, or end-of-attribute).
  **Benefit:** Pages that genuinely lack internal links get the linking recommendations they
  should.

- [x] **3.17 Offsite audit: wrong error on failure, silent success on near-total failure**
  **Problem:** `services/offsite_visibility_service.py:309-313` — if query #1 fails (any
  transient Brave error returns None), the whole audit fails with the misleading "Brave Search
  is not configured" message; conversely 1 success + 14 failures completes silently with a
  scorecard built from a fraction of the data.
  **Fix:** Distinguish "no API key" from "search failed"; track a failure count; fail the run
  only on total failure (with the real error), and surface partial-failure counts in the
  results.
  **Benefit:** Offsite audit errors tell you what actually went wrong, and a mostly-failed
  audit can't masquerade as a clean one.

- [x] **3.18 Dead `src` field in cannibalization-fix payloads (cleanup)**
  **Problem (scope corrected):** `routes/gsc_routes.py:516` reads `img.get("src")` but sync
  stores the URL under `"url"` — so `src` is always empty. Verified: nothing renders it (the
  preview uses only `alt`/`media_id`), so this is dead weight, not a broken image preview.
  **Fix:** Either read `img.get("url")` or drop the field.
  **Benefit:** Cleanliness only; prevents someone later "using" a field that is always empty.

- [x] **3.19 Duplicates page and Issues tab disagree on what's a duplicate**
  **Problem:** `routes/seo_issues_routes.py:294-302` groups with `.strip().lower()` while
  `duplicate_detector.normalize` casefolds and collapses whitespace — a pair differing by a
  double space is a duplicate on /duplicates but not in the Issues tab. The Issues-tab
  grouping also covers only products+collections while Duplicates covers articles/pages too.
  **Fix:** Import and use `duplicate_detector.normalize` in the Issues-tab grouping; align the
  covered item types (or annotate the difference in the UI).
  **Benefit:** Both surfaces agree on which items are duplicates.

---

# Part 4 — Safety guards & data-loss paths

- [ ] **4.1 Cannibalization guard fails open on any error**
  **Problem:** `routes/gsc_routes.py:459-460` wraps the entire resolver/ownership check in
  `except Exception: logger.warning(...)` — any resolver error silently disables the
  protection and the fix is generated and applied anyway.
  **Fix:** Fail closed: return 502 "keyword safety check failed — try again" (copy
  `content_gap_routes.py:127-132`, which does exactly this with an explanatory comment).
  **Benefit:** A broken safety check can never silently let two pages be optimized into
  competing for the same keyword.

- [ ] **4.2 Same gate, opposite polarity in content-gap routes**
  **Problem:** `generate_content` fails closed (:120-132) but `optimize_existing` fails open
  (:179-196, "proceeding") — and the optimize-existing suggestions are applied to live pages
  by an apply route that has **no gate at all** (verified), so this fail-open check is the only
  protection on that flow.
  **Fix:** Make `optimize_existing` fail closed like its sibling.
  **Benefit:** The flow that rewrites live pages gets the same keyword-conflict protection as
  the flow that creates new ones.

- [ ] **4.3 Keyword-chip re-optimize has no ownership guard** *(mechanism corrected)*
  **Problem:** Clicking any keyword chip (`static/js/seo.js:2177-2183`) triggers a re-optimize
  with **no server-side ownership check**. Verified correction: the chip sends the keyword as
  free-text `priority_context`, not `target_keyword` — so even the prompt-level "resolved
  primary wins" mitigation the audit credited does not apply to this path. Every other
  retarget path (gsc/content-gap/blog) has a guard.
  **Fix:** Route chip re-optimizes through the same ownership check the other retarget paths
  use (reject or warn when another page owns the keyword), and send the keyword as a proper
  `target_keyword` so prompt-side handling applies.
  **Benefit:** You can't accidentally point two pages at the same keyword with one click.

- [ ] **4.4 Lightweight assign endpoint bypasses parts of the resolver pipeline**
  **Problem (corrected):** `POST /api/keyword-strategy/keywords/<id>/assign`
  (`routes/keyword_strategy_routes.py:295-332`) accepts product/collection although its own
  docstring routes them to the full primary-keyword pipeline. Verified: ownership IS still
  claimed (the audit overstated that), but the path skips demotion of the item's previous
  primary (an item can own two keywords), the `content_keyword_map` mirror, and
  re-resolution. Also verified: **no UI calls this endpoint at all.**
  **Fix:** Since it's unused, either delete it (preferred — see Part 6 orphans) or restrict it
  to article/page as the docstring says.
  **Benefit:** One consistent path for keyword assignment; no back door that leaves items
  owning stale keywords.

- [ ] **4.5 Deleting one article can orphan it live on the storefront**
  **Problem:** `routes/blog_routes.py:485-493` logs-and-ignores a failed Shopify delete, then
  deletes the local row — the article stays live on your store but becomes invisible in the
  app. Verified worse: if no client is available, the local row is deleted **without even
  attempting** the Shopify delete. (Bulk delete :528-538 and news delete do it correctly.)
  **Fix:** Copy the bulk/news pattern: delete the local row only when Shopify confirms deletion
  or confirms the article is already gone; otherwise return 502 and keep the row.
  **Benefit:** Nothing can be live on your store yet invisible in the app.

- [ ] **4.6 Same swallow on empty-blog landing delete**
  **Problem:** `routes/blog_routes.py:75-82` — `except Exception: pass` around the Shopify
  delete, then the tracking row is removed regardless. A transient failure leaves a live,
  untracked landing article.
  **Fix:** Same gone-vs-transient discrimination as 4.5.
  **Benefit:** Same as 4.5.

- [ ] **4.7 Landing regen can duplicate the live landing article**
  **Problem:** `routes/blog_routes.py:108-121` and :995-1008 — an `update_article` failure is
  swallowed, then the unconfirmed id triggers a fresh create; the old article stays live and
  untracked. `news_routes.py:112-125` already fixed this pattern ("a transient error must not
  mint news-1").
  **Fix:** Copy the news pattern: only recreate when the error confirms the article is gone;
  otherwise re-raise.
  **Benefit:** No duplicate landing pages appearing on your store after a hiccup.

- [ ] **4.8 Bulk cannibalization fix runs inside the web request** *(failure mode corrected)*
  **Problem:** `routes/gsc_routes.py:720-911` runs AI + Shopify writes synchronously.
  Verified correction: persistence is per-item, so completed fixes are NOT lost; the real
  failure is that past ~300s nginx returns a 504 while the backend keeps working — you see a
  false failure while writes continue behind your back.
  **Fix:** Move it to a background task via `task_runner.spawn` with per-item progress, like
  every other bulk flow.
  **Benefit:** Long bulk fixes show live progress instead of timing out with a scary error
  while secretly still running.

- [ ] **4.9 Interlinking partial failure misreports applied links as errors**
  **Problem:** `routes/interlinking_routes.py:941-972` — the Shopify push is one atomic call,
  but if *recording* link N fails afterward, error rows are appended for **all** links,
  including ones already applied and live (some get both an "applied" and an "error" row).
  **Fix:** In the except handler, append error rows only for links not yet recorded as applied.
  **Benefit:** The results list tells you exactly which links are live — no contradictions.

- [ ] **4.10 Editing the cannibalization prompt in Settings can break every fix**
  **Problem:** Three sites in `routes/gsc_routes.py` (:422-423, :525-537, :787-799) use
  `.format()` on user-editable prompt templates. The defaults survive only because their JSON
  examples are `{{`-escaped; the natural user edit (unescaping braces) breaks every call.
  (`interlinking_routes.py:794-798` uses `.replace` with a comment explaining exactly why.)
  **Fix:** Switch all three to `.replace()` placeholder substitution.
  **Benefit:** Editing prompts in Settings can never crash the features that use them.

- [ ] **4.11 OAuth error text reflected raw into the redirect**
  **Problem (scope corrected):** `routes/gsc_routes.py:76, 98` reflect raw exception text
  unencoded into the redirect Location — corrupted query strings and internal-detail leaks.
  Verified: the audit's refresh-token crash is largely defused (`prompt="consent"` forces
  reissue, and the exception is caught into the redirect anyway); the `connected_email` gap is
  cosmetic (no identity scope requested, UI never shows it).
  **Fix:** URL-encode the error parameter (`urllib.parse.quote`); optionally guard
  `refresh_token` presence for defense in depth.
  **Benefit:** OAuth failures land you on a proper error message instead of a garbled URL.

- [ ] **4.12 Adding a store with bad credentials silently "succeeds"**
  **Problem:** `routes/store_routes.py:88-94` swallows the connection-test failure and creates
  the store with NULL shop name/domain; you first learn at sync time.
  **Fix:** Return the test failure to the user (block creation, or create with a clear
  "connection failed — check token" warning in the response and UI).
  **Benefit:** Typos in tokens/domains are caught the moment you add the store.

- [ ] **4.13 Staged uploads pass defaults instead of real content type (tidy-up)**
  **Problem (impact corrected):** `routes/brand_routes.py:1067-1068` declares webp/jpeg + real
  filename to Shopify but calls `upload_staged_file` with default `image/png`/`image.png`;
  `landing_routes.py:1299` omits filename; verified third site `seo_routes.py:2703-2705`.
  Verified: Shopify's staged-upload form parameters carry the authoritative type, so this
  plausibly has **no observable effect** — fix as consistency hardening.
  **Fix:** Pass `content_type=` and `filename=` at all three call sites.
  **Benefit:** Uploads are declared consistently end-to-end; immune to Shopify tightening
  validation later.

- [ ] **4.14 Bulk endpoints mix stores**
  **Problem:** `routes/technical_seo_routes.py:360-372, 411-423` accept mixed-store selections
  but take `run_store_id` from the first item — other stores' items get the wrong store's
  demand data in prefetch, and the ownership-dedup pass runs only for store #1.
  `routes/brand_routes.py:942-952, 1342-1351` bulk executors fetch rows by id with **no
  store_id filter** while the Shopify client comes from the request's store — a cross-store id
  would be optimized with the wrong store's keywords and written to the wrong store.
  **Fix:** Add `AND store_id = %s` to the bulk row fetches; either reject mixed-store
  selections in technical bulk or group items per store and prefetch/dedup per group.
  **Benefit:** With multiple stores connected, bulk jobs can never cross wires between them.

- [ ] **4.15 Multi-store metric collision on the SEO list pages**
  **Problem:** `routes/seo_routes.py:295-302, 1365-1372` merge per-store GSC/Semrush metric
  dicts keyed only by `prefix/handle`. Verified: this is the **routine** path — the shipped
  frontend never sends `store_id` to these list endpoints — so with two stores sharing a
  handle, one store's clicks/impressions display on the other's item.
  **Fix:** Key the merged dicts by `(store_id, path)` and look up with the item's store_id.
  **Benefit:** Each store's traffic numbers appear only on that store's items.

- [ ] **4.16 Alt text interpolated unescaped into live collection HTML**
  **Problem (scope corrected):** `routes/seo_routes.py:2647-2664` interpolates `alt_text` into
  an `<img>` tag unescaped and pushes it live. Verified: `image_url` is server-derived (not
  client-controlled, as the audit claimed) — but `alt_text` is, and even a benign
  AI-generated alt containing a quote corrupts the tag.
  **Fix:** `html.escape(alt_text, quote=True)` before interpolation.
  **Benefit:** Collection descriptions can't be corrupted (or scripted) by a quote in alt
  text.

- [ ] **4.17 "H&M" renders as "H&amp;amp;M" on brand landing pages**
  **Problem:** `routes/brand_routes.py:657-660` escapes already-escaped strings a second time
  when composing alt/aria-label text. Verified bonus defect: the `[:125]` truncation happens
  *between* the two escapes and can slice an entity in half.
  **Fix:** Compose alt text from the **raw** values, truncate, then escape exactly once at the
  end.
  **Benefit:** Brand names with `&` display correctly on your live landing pages.

- [ ] **4.18 Empty vendor name tags every collection as "Related to brand: " (latent)**
  **Problem:** `routes/brand_routes.py:1512-1514, 1543` — a missing/empty collection title
  yields `vendor_name=""`, and `"" in title` is always true, so every collection (up to the
  10-cap) gets a blank "Related to brand: " suggestion. Verified: reachable only when a brand
  collection has an empty title — latent, not the common path.
  **Fix:** `if vendor_name and vendor_name.lower() in coll_title_lower:`.
  **Benefit:** Interlink suggestions never include nonsense blank-brand entries.

- [ ] **4.19 Blog scheduling accepts past dates and publishes immediately**
  **Problem:** `routes/blog_routes.py:2079-2171` accepts any `scheduled_at` (schedule and
  reschedule); the scheduler publishes anything past-due on its next 60-second tick. The news
  twins reject past dates with the comment "a mistyped date must not silently publish
  immediately".
  **Fix:** Copy the news validation into both blog endpoints.
  **Benefit:** A typo in a schedule date can't instantly publish a post you meant to schedule
  for next month.

---

# Part 5 — One definition of everything (scoring, keywords, lengths)

## Keyword-resolver bypasses

- [ ] **5.1 Collection image-alt generation ignores the resolver** *(impact corrected)*
  **Problem:** `_generate_image_for_collection` (`routes/seo_routes.py:2592`) derives keywords
  from raw n-grams, never the resolver. Verified correction: there is **no scoring mismatch**
  (the collection analyzer has no `image_alt_has_keyword` check) — the issue is consistency:
  alts get baked with a keyword that may differ from the page's actual resolved target.
  **Fix:** Fetch the resolved primary (fall back to n-grams only if none) before generating.
  **Benefit:** Collection image alt text reinforces the keyword the page is actually
  targeting.

- [ ] **5.2 Priority queue hands out unresolved keywords**
  **Problem:** `services/seo_priority_service.py:332-335` — with no stored resolved primary,
  the fallback hands out the top unowned Semrush keyword as `target_keyword` without
  triggering resolution (mitigations exist: foreign-owner filter + GSC cannibalization check).
  **Fix:** Trigger `get_or_refresh_resolution` for items reaching this fallback (or mark the
  handed-out keyword as "unresolved suggestion" in the queue UI).
  **Benefit:** The home queue's suggested keywords go through the same vetting as everywhere
  else.

- [ ] **5.3 SERP scraper discards the resolver keyword for collections** *(worse than audited)*
  **Problem:** Verified beyond the original claim: for `entity_type == "collection"`,
  `determine_search_queries` (`services/serp_scraper.py:203-204`) **unconditionally** uses the
  meta-title/title heuristic and ignores `target_keywords` — the resolver keyword carefully
  passed by the collection and brand SEO flows is always thrown away. Product flows fall back
  silently only when no keyword is passed.
  **Fix:** Honor `target_keywords[0]` for collections exactly as for products; log when any
  heuristic fallback fires.
  **Benefit:** Collection SEO benchmarks against the keyword you're actually targeting, not a
  guess from the title.

- [ ] **5.4 Resolver failure silently overwrites good stored resolutions** *(aggravation found)*
  **Problem:** `routes/technical_seo_routes.py:84-85` swallows resolver exceptions, the
  analysis proceeds n-gram-scored, and — verified aggravation — the upsert's ON CONFLICT
  clause **overwrites a previously good `keyword_resolution` with NULL**.
  **Fix:** On resolver failure, preserve the existing stored resolution (use
  `COALESCE(EXCLUDED.keyword_resolution, technical_seo_analyses.keyword_resolution)` or skip
  the column) and surface a warning in the analysis result.
  **Benefit:** One transient error can no longer erase an item's keyword targeting.

- [ ] **5.5 Quality gate can enforce the AI's own keyword choice**
  **Problem:** `ai_agent.py:2946-2948` — with no target keyword and no stored resolution, the
  validator enforces keyword-first rules against the AI's self-picked
  `target_keywords.primary`. (Deliberate per docstring, but circular.)
  **Fix:** When falling back to the AI's own keyword, skip the keyword-first *enforcement*
  (keep length/structure checks) or trigger resolution first.
  **Benefit:** The quality gate can't lock in a keyword nobody chose.

- [ ] **5.6 Stored-primary lookups don't filter by store (latent)**
  **Problem:** `ai_agent.py:2935-2941, 3543-3548` and `routes/task_routes.py:627-631` query
  `technical_seo_analyses` by item_type/item_id only. Safe today (item ids are global
  serials); breaks silently if ids are ever reused per store.
  **Fix:** Add `AND store_id = %s` to all three queries.
  **Benefit:** Future-proofing — keyword lookups can never cross stores.

## One key for "same keyword"

- [ ] **5.7 Coverage map joins keywords by exact string — misses owned keywords**
  **Problem:** `services/keyword_coverage.py:58` joins resolver primaries (stored `_norm`-ed:
  punctuation stripped, whitespace collapsed) against `master_keywords` (only lowercased) by
  exact string — keywords with punctuation or double spaces show "unowned" although a page
  owns them (verified nuance: only the resolver-primary ownership branch is affected;
  assignment-based ownership joins by id and still works).
  **Fix:** Normalize both sides with `_norm` in the join (compute a normalized column or
  normalize `master_keywords.keyword` on write).
  **Benefit:** The gap matrix stops telling you to create content for keywords you already
  rank pages for.

- [ ] **5.8 Three granularities of keyword identity — guards miss what claiming blocks**
  **Problem (verified with the exact scenario):** `variant_key` (plural-folded, order-sorted)
  drives dedup/claiming; `_norm` drives the priority queue and the blog-wizard/gsc/content-gap
  guards; exact string drives the coverage map. A product owning "dutch masters" **blocks**
  page resolution of "dutch master" (variant_key folds plurals) but does **not** trigger the
  blog-wizard/retarget guards for it (`_norm` doesn't fold plurals).
  **Fix:** Standardize the ownership-guard lookups on `variant_key` (the strictest key):
  build `get_owned_primary_keywords` maps keyed by variant_key and convert guard lookups.
  **Benefit:** If the app considers two keywords "the same" for claiming, every guard and
  every view agrees — no more contradictory warnings.

- [ ] **5.9 Cannibalization self-test uses substring matching — misses real conflicts**
  **Problem:** `services/seo_priority_service.py:157` — `this_path not in page_path` treats
  `products/x-large` as "self" when checking `products/x`, silently excluding a genuinely
  competing page (false negatives only). The resolver's own check compares exact URLs.
  **Fix:** Compare exact paths (strip domain + trailing slash, then `==`), like
  `keyword_resolver.py:465`.
  **Benefit:** Real cannibalization between similarly-named products gets caught.

## One set of scoring bands & length rules

- [ ] **5.10 Keyword Strategy page uses different score colors than everywhere else**
  **Problem:** `static/js/keyword_strategy.js:586` colors green≥80/amber≥60/red<60; every
  other SEO-score surface uses 80/50 (verified across app.js, seo.js, seo_issues.js,
  ai_seo.js, brands.js).
  **Fix:** Change to the 80/50 bands (ideally via a shared `scoreClass()` helper in app.js).
  **Benefit:** The same score shows the same color on every page.

- [ ] **5.11 Dashboard "coverage" uses a title band that matches nothing**
  **Problem:** `services/dashboard_service.py:800-801` hard-codes
  `meta_title BETWEEN 20 AND 75` (nothing else in the app uses 20–75; live ok-band is 30–70)
  and ignores settings. (Verified: the 100–200 desc band happens to match.)
  **Fix:** Read the same `rules.product_seo` thresholds the analyzer uses.
  **Benefit:** The dashboard coverage tile agrees with the item-level scoring.

- [ ] **5.12 Dead settings keys mislead in Settings**
  **Problem:** `rules.technical_*` → `meta_title_length`/`meta_desc_length`
  (`prompt_defaults.py:1214-1215, 1237-1238`) are read by nothing (the analyzer reads
  `rules.product_seo`/`collection_seo`) and disagree with the live bands — editing them does
  nothing, silently.
  **Fix:** Delete the dead keys from the defaults.
  **Benefit:** Every setting you can edit actually does something.

- [ ] **5.13 AI tools report a different "SEO score" than the UI shows**
  **Problem:** `analyze_seo_score` / `analyze_collection_seo_score` / store-audit average
  (`ai_agent.py:1331-1391, 1354`) return only the content pillar under the name "seo score";
  the UI shows the unified 3-pillar blend — the AI reasons about a number you never see.
  **Fix:** Return the unified score (or label the tool result "content_score" and include the
  unified score alongside).
  **Benefit:** When the AI says "score is 62", it matches the 62 you see on screen.

- [ ] **5.14 blog_seo_analyzer re-implements the scoring ladder with a divergent default**
  **Problem:** `services/blog_seo_analyzer.py:15-38` re-implements `score_length` inline with
  an in-code default `[140,160]` vs the settings default `[120,160]` (settings win today, so
  the divergent value is dormant — until someone edits settings incompletely).
  **Fix:** Delete the inline ladder and call the shared `score_length`; drop the divergent
  default.
  **Benefit:** One length-scoring implementation to maintain.

- [ ] **5.15 News prompt targets titles the article scorer calls "poor"**
  **Problem (scope corrected):** `prompts.news_analyze` asks for 40–60-char meta titles while
  `rules.article_seo` scores good 35–46 / ok ≤55 — a 56–60-char title scores "poor" on
  arrival. Verified corrections: `blog_seo_suggestions` targets the blog *landing* meta which
  nothing scores, and the validator never touches articles — so the fix is one prompt.
  **Fix:** Change `prompts.news_analyze` title guidance to 35–46 (+ theme-suffix allowance,
  matching `prompts.article_seo`).
  **Benefit:** Freshly generated news articles arrive already scoring green instead of
  needing an immediate "fix".

- [ ] **5.16 Quality gate skips brand SEO (and rephrase-checks skip GEO)** *(corrected)*
  **Problem:** The rephrase-quality gate covers only `product_seo`/`collection_seo` and
  `technical_seo_fix`. Verified correction: GEO applies **are** clamped via
  `_push_and_rescore`, and brand/content-gap applies via `guard_shopify_fields` — so nothing
  ships unclamped; what brand/GEO/content-gap lack is the smarter rephrase-first validation
  (keyword-first titles, hero-only alt keyword, etc.).
  **Fix:** Extend `_validate_main_result`-style validation to `brand_seo` and GEO generation.
  **Benefit:** Brand and AI-SEO content gets the same quality polish as product content —
  fewer truncated-mid-word titles.

- [ ] **5.17 Homepage/landing meta pushed to Shopify with lenient clamps**
  **Problem:** `ai_agent.py:2876-2882, 2910-2915` clamp title only when >70 (so 61–70 passes a
  60 cap) and truncate mid-word. Verified end-to-end: the homepage save/publish
  (`homepage_routes.py:102-105, 357-361`) and builder-page publish (`landing_routes.py:614-628`)
  push without any further clamp (synced-page apply does compensate).
  **Fix:** Use `clamp_meta_title`/`clamp_meta_description` (word-boundary, correct caps) in
  those validators, and add `guard_shopify_fields` to the homepage/builder publish paths.
  **Benefit:** Home and landing pages never go live with overlong or mid-word-truncated
  titles.

- [ ] **5.18 Prompt length-guidance inconsistency (low priority)** *(conflict downgraded)*
  **Problem:** Several prompts steer to top-of-band (50–60 / 140–160): the SERP block,
  `dedupe_group`, `cannibalization_fix`, `keyword_strategy_fix`, the content-brief prompt.
  Verified correction: there is **no functional conflict** — the validator only enforces hard
  60/160 caps, these targets sit inside the good bands, and most of these flows never pass
  through the validator. It's guidance inconsistency, not a component fight.
  **Fix:** When touching these prompts anyway, align on the ~50/~150 mid-band aims.
  **Benefit:** Generated titles sit safely mid-band instead of flirting with the cap.

- [ ] **5.19 `_enforce_meta_limits` duplicates the clamp helper (already diverging)**
  **Problem:** `routes/technical_seo_routes.py:22-31` hand-rolls `ai_output_guard._clamp`.
  Verified: not quite identical even today — `_clamp` strips trailing punctuation before the
  ellipsis; the copy doesn't. Any band change desynchronizes them further.
  **Fix:** Delete the local copy; import the shared clamps.
  **Benefit:** One truncation rule everywhere.

- [ ] **5.20 Two alt-pairing heuristics (consistency, low priority)** *(impact corrected)*
  **Problem:** Generation-time pairing (`ai_agent.py:6222-6247`) and apply-time
  `_normalize_media_updates` (`technical_seo_routes.py:464-519`) have different fallback
  semantics. Verified correction: they live in disjoint pipelines and never process the same
  payload — no single artifact can experience the disagreement. Consolidate for
  maintainability only.
  **Fix:** Extract one shared pairing function used by both.
  **Benefit:** Future changes to alt pairing only need to be made once.

---

# Part 6 — Stale UI, dead code & orphaned endpoints

- [ ] **6.1 Two competing store selectors on 8 pages (the one real UX conflict)**
  **Problem:** The global topbar StoreSelector and page-local store dropdowns coexist unsynced
  on `ai_seo`, `duplicates`, `gsc`, `semrush`, `competitors`, `content_gaps`, `homepage`, and
  `landing_pages` — verified: none of those 8 JS files reference `store:changed` or
  StoreSelector at all, so switching one selector doesn't move the other and the two can
  disagree on the same screen. Working examples to copy: brands.js, calendar.js, dashboard.js,
  interlinking.js, seo_issues.js.
  **Fix:** Migrate all 8 pages to the `store:changed` event pattern and remove the page-local
  dropdowns.
  **Benefit:** One store picker that works the same everywhere — you can never be looking at
  one store's data while the header says another.

- [ ] **6.2 Sidebar badge that can never light up**
  **Problem:** `templates/_sidebar.html:17` has `data-badge="optimize"`, but `get_nav_badges`
  emits no `optimize` key.
  **Fix:** Either emit an aggregate optimize count or delete the span.
  **Benefit:** No phantom badge slot; the sidebar reflects real counts.

- [ ] **6.3 Delete orphaned endpoints (verified: zero callers each)**
  All of the following were verified orphaned by full-app grep (JS, templates, Python).
  Retire them — use the 410-tombstone pattern from `schema_routes.py:283-290` for any you want
  to keep discoverable, plain deletion otherwise:
  - Keyword-strategy fix flow: `POST /fix/<id>/generate`, `GET /fix/<id>/preview`,
    `POST /fix/<id>/apply`, `POST /fix/bulk-apply` (`keyword_strategy_routes.py:541-613`;
    only `/fix/<id>/revert` is live). Also `POST /keywords/<id>/assign` (:295 — see 4.4).
  - Cluster management: `POST /clusters/generate`, `PUT /clusters/<id>`,
    `POST /clusters/merge` (:365-388) + their service functions.
  - Old GSC pull: `POST /api/gsc/pull` (:161; JS uses `/pull-sync`),
    `GET /api/gsc/page-matches` (:309).
  - Technical-seo list surface: `GET /products` (:135), `GET /collections` (:207),
    `GET /analysis/<type>/<id>` (:432), `POST /bulk-fix-apply` (:911). Also correct the file's
    comment claiming these "power the merged page" — they don't.
  - Blog: `GET/POST /api/blog/blogs` (`blog_routes.py:238, 260` — note the live caller uses
    the *different* `PUT /blogs/<id>` route), `POST /articles/<id>/move` (:432).
  - AI-SEO batches: `GET /batches`, `POST /batches/clear`, `DELETE /batches/<id>`
    (`ai_seo_routes.py:1718, 1820, 1840`; only `/batches/<id>/retry` is called).
  - Misc: `POST /api/dashboard/revenue/pull`, `GET /api/settings/defaults/<path:key>`,
    `GET /api/keyword-strategy/runs/<id>`. (The `POST /api/schema/batch/*` endpoints are
    already deliberate 410 tombstones — delete when convenient.)
  **Benefit:** A smaller, honest API surface — no half-working back doors, less code to audit.

- [ ] **6.4 Delete dead JS & templates (verified definition-only)**
  - `keyword_strategy.js:1607-1612` `generateFix`/`previewFix`/`applyFix` shims + the
    never-opened `#ks-preview-modal` (`keyword_strategy.html:695-707`, apply button unwired).
  - `seo.js`: `confirmDeleteCollection` (:1674), `loadTagsForStore` (:3579), the
    `#store-select` fallback in `_getSeoStoreId` (:955 — element absent from templates that
    load seo.js).
  - `blog.js`: `revertSeoSnippet` flow (:2520, button removed; backend route kept),
    `getSelectedLandingBlogId`'s `#landing-blog-select` read (:1819 — element exists nowhere),
    `getStoreDomain` (:1884), `#store-select` fallback (:1895).
  - `app.js` `showLoading` (:134); `auth.js` (58-byte comment file, loaded by nothing).
  **Benefit:** Less code that looks load-bearing but isn't.

- [ ] **6.5 Delete dead Python (verified zero call sites)**
  - `ai_agent.py:1455` `_handle_discovery_tool_call` (the discovery tool-loop is gone;
    `DISCOVERY_TOOL_DEFINITIONS` no longer exists — also update CLAUDE.md's section on it).
  - `serp_scraper.py:358-477`: `google_search`, `GOOGLE_SEARCH_DELAY`, `_google_last_call`,
    `GOOGLE_USER_AGENTS` (~120 lines); `_find_product_in_results` (:236) and the
    "YOUR PRODUCT'S CURRENT SERP PRESENCE" prompt block (:786-806) — `product_query` is
    hardcoded None so neither can ever run.
  - `shopify_client.py`: first `create_url_redirect` definition (:1432 — the second at :1842
    silently shadows it and is the one callers get; keep the idempotent second one), plus dead
    `is_smart_collection` (:500), `get_product_metafield` singular (:941),
    `list_theme_assets` (:1922).
  - `store_audit` task branch (`ai_agent.py:5778`) + its settings-exposed prompt — nothing
    creates the task.
  **Benefit:** Same as 6.4.

- [ ] **6.6 Dead prompt defaults** *(corrected scope)*
  **Problem:** Four prompt keys are read by nothing in Python: `prompts.product_discovery`,
  `prompts.action_generate_alts`, `prompts.keyword_strategy_plan`, `prompts.keyword_strategy_fix`.
  Verified correction: only `product_discovery` is actually exposed in the Settings UI
  (settings.js:287); the other three are dead defaults not shown anywhere.
  **Fix:** Delete all four from `prompt_defaults.py` and remove `product_discovery` from
  `PROMPT_GROUPS` in settings.js.
  **Benefit:** The Settings prompt list contains only prompts that do something.

- [ ] **6.7 Contradictory instructions in the keyword-analysis conversation**
  **Problem:** `prompts.keyword_phase1` mandates paginating via `get_all_product_summaries`
  while the Phase-1 user prompt embeds the full catalog and says "you do NOT need to fetch
  anything" — both in the same conversation, tool still offered.
  **Fix:** Rewrite `prompts.keyword_phase1` to match the embedded-catalog approach; drop the
  pagination tool from that call's tool list.
  **Benefit:** The AI doesn't waste tokens (your money) re-fetching a catalog it already has.

- [ ] **6.8 Tool schemas promise more than handlers deliver**
  **Problem:** `get_keyword_opportunities` promises CTR opportunities but returns only
  striking-distance; `get_store_summary` promises "top issues" but returns only counts.
  **Fix:** Either implement the promised data or correct the schema descriptions.
  **Benefit:** The AI stops reasoning about data it was promised but never receives.

- [ ] **6.9 Dead imports/locals sweep** *(three items removed after verification)*
  Verified dead — safe to remove: unused `import threading` in seo/image/homepage/competitor/
  content_gap/task/technical_seo/brand routes; `image_routes.py:11` `sweep_stale_jobs` import;
  `seo_routes.py:697-698` `_gs`/`_defs` (**note:** the :864-865 pair IS used — leave it);
  `landing_routes.py:11` `extract_page_content`, :280 nonexistent `shopify_page_id` read,
  :837 unused `import html as _html`; `technical_seo_routes.py:1193` `_get_stored_analysis`;
  `ai_seo_routes.py` `_published_site_schema_types`, duplicate datetime imports,
  `_fact_density_score`'s ignored params, `_score_items`' discarded return;
  `gsc_routes.py:6` `url_for`; `blog_routes.py:376,:511` `s.id as sid`;
  `offsite_visibility_service.py:208` unused `brand_terms` param + :162 docstring promising a
  3-tuple (returns 2).
  **Do NOT remove** (verified live): `_KNOWN_OLD_HANDLES` (used by the detect-old-blogs
  feature via blog.js:2554); `backup` at blog_routes.py:2940 (it is read — though only its
  length; the original theme.liquid is never actually persisted, which is worth a TODO);
  the keyword_strategy_routes 500 handler (reachable on thread-exhaustion).
  **Benefit:** Cleaner code; no time wasted re-investigating the same suspicious lines later.

- [ ] **6.10 Orphaned DB objects**
  Verified never read/written by application code: tables `seo_audits`, `blog_migrations`,
  `ai_excluded_brands` (migration-only INSERT re-runs every boot — make it one-time); dead
  columns `master_keyword_clusters.parent_cluster_id`, `semrush_keywords.data_timestamp`;
  write-only columns `ai_mention_results.tokens_used`, `synced_blogs.comment_policy`,
  `master_keywords.ai_classified_at` (either surface them or stop writing them).
  **Do NOT drop** (verified live): `competitors.notes` — written by ai_seo_routes and returned
  by `get_competitors` (just never rendered in JS; consider showing it).
  **Benefit:** Schema reflects reality; every boot stops re-running a finished migration.

- [ ] **6.11 Duplicate-fix events missing from Impact for articles/pages**
  **Problem (the substantive part of the payload-bloat finding):** duplicates.js sends
  `change_type: "duplicate_fix"`, and product/collection applies record it in
  `seo_change_events` — but the article apply (`blog_routes.py:371-393`) and page apply
  (`landing_routes.py:249-293`) record nothing, so article/page dedupe fixes never appear in
  Impact views. (Verified corrections: `seo.js:199 item_id` IS read by two of three preview
  types; the whole-item POST bloat is cosmetic and server-safe.)
  **Fix:** Add `seo_change_events` inserts to the article and page apply routes (with the
  same push-succeeded guard as Part 2).
  **Benefit:** Impact tracking covers everything you fix, not just products and collections.

- [ ] **6.12 Terminology cleanup (cosmetic)**
  "AI Search" (sidebar/cmdk) vs "AI SEO — GEO · AEO · LLMO" (page); "Brands" vs "Brand
  Collections"; "Home Page" vs "Home Page Carousel"; "Home" vs tab title "Dashboard";
  sidebar group labeled "Insights" with `data-group="research"`. Pick one name per feature and
  sweep.
  **Benefit:** Navigation, command palette, and page titles all use the same names.

---

# Part 7 — Concurrency & background-job hardening

- [ ] **7.1 DB-enforced single-flight for job starts (the umbrella fix)**
  **Problem:** Check-then-insert "already running" guards race under 2 workers × 4 threads —
  duplicate scans/batches = duplicate OpenRouter spend (or duplicate Shopify writes for
  schema-repush). Verified sites: `ai_seo_routes.py:347-358, 810-821, 1931-1938, 2268-2279`;
  `interlinking_routes.py:427-483, 520-600` (**no guard at all**); the ai-seo
  optimize/facts/schema-repush batch creators (no guard); keyword consolidate (no check at
  all service-side); `blog_routes.py:2087-2127` (two clicks → two scheduled posts);
  `store_routes.py:161-185` (guard exists but the running row appears only after a Shopify
  HTTP call — multi-second window).
  **Fix:** Copy the `interlinking_analysis` partial-unique-index pattern
  (`database.py:214-218`) to every job type: partial unique index on
  `(store_id, task_type) WHERE status IN ('pending','running')`, insert-first, catch the
  unique violation as the "already running" response.
  **Benefit:** Double-clicking a button can never start two identical AI jobs and bill you
  twice.

- [ ] **7.2 Per-process sync lock races cross-worker**
  **Problem:** `services/sync_service.py:16-24` uses a `threading.Lock` — each gunicorn worker
  has its own, so two "Sync now" clicks on different workers can race the
  `collection_product_map` DELETE+re-insert and stale-row cleanup.
  **Fix:** Covered by 7.1's DB single-flight on sync jobs, plus insert the `sync_jobs` running
  row *before* the Shopify test call.
  **Benefit:** Concurrent syncs can't corrupt the collection-product mapping.

- [ ] **7.3 Leader election fails open (cross-process only)** *(scope corrected)*
  **Problem:** `database.py:105-107` returns True ("assuming leadership") on any exception.
  Verified correction: gunicorn runs `--preload`, so election happens once in the master — the
  multiple-workers scenario can't occur in this deployment; the hazard is a second container
  or a dev `python app/main.py` against the same DB.
  **Fix:** Return False on exception; log loudly.
  **Benefit:** Duplicate scheduled publishes/GSC pulls become impossible even if you run a
  second instance.

- [ ] **7.4 Stale scan rows block re-scans for hours**
  **Problem:** Scan/feed start endpoints 409 on an active run but never sweep
  (`ai_seo_routes.py:339, 804`), and the status endpoints don't either. Verified refinement:
  after a worker-only death, re-scans 409 for up to ~2h (until the sweeper's threshold or a
  container restart).
  **Fix:** Call `sweep_stale_jobs()` at the top of the start endpoints (like their sibling
  endpoints already do).
  **Benefit:** After a crash you can restart a scan immediately instead of waiting hours.

- [ ] **7.5 Sweeper can fight a legitimately long batch**
  **Problem:** `sweep_stale_jobs` force-fails running jobs at 2h/6h with no heartbeat; a
  long-but-alive batch gets failed, a duplicate can start, then the original thread's
  unconditional final `status='completed'` (`ai_seo_routes.py:1248-1249`,
  `image_routes.py:415-419`, `seo_routes.py:1720-1724`) flips it back — two threads mutating
  the same items.
  **Fix:** (a) add an `updated_at` heartbeat on each item completion and sweep on heartbeat
  age, not creation age; (b) make the final status update conditional
  (`WHERE status = 'running'`).
  **Benefit:** Long bulk jobs finish cleanly; a swept job stays swept.

- [ ] **7.6 Two competing sweepers in one file**
  **Problem:** `ai_seo_routes.py:1253-1271` has a local `_sweep_stale_jobs` (3 tables,
  swallowed errors, no throttle) used at 5 call sites, alongside the imported 8-table
  task_runner sweeper used at others.
  **Fix:** Delete the local one; use `task_runner.sweep_stale_jobs` everywhere.
  **Benefit:** One sweeper whose behavior you can reason about.

- [ ] **7.7 Ghost rows the sweeper never covers**
  **Problem:** (a) `competitor_check_runs` is in neither boot recovery nor the sweeper —
  restart mid-check leaves a `'running'` row forever in history (cosmetic but confusing; the
  manual trigger also runs synchronously in the request thread, a realistic strand vector).
  (b) **NEW (found during verification):** the landing page-sync job inserts `sync_jobs` with
  status `'syncing_pages'` (`landing_routes.py:89-95`), which matches neither the sweeper's
  `('pending','running')` filter nor boot recovery — it can ghost forever.
  **Fix:** Add `competitor_check_runs` to `sweep_stale_jobs`; change `'syncing_pages'` to
  `'running'` (keep the phase in a separate column if needed).
  **Benefit:** No permanently-spinning jobs in history views.

- [ ] **7.8 Raw threads bypass the task framework**
  **Problem (scope corrected):** Five landing jobs and three GSC flows spawn raw
  `threading.Thread`; four landing task rows stay `'pending'` forever (never set `'running'`)
  and the GSC snapshot thread has no tracking row at all. Verified correction: the sweeper
  and boot recovery DO cover `'pending'` ai_tasks and the GSC pull tracks itself in
  `gsc_data_pulls` — so this is consistency/observability, not a coverage hole (except 7.7b).
  **Fix:** Route all eight through `task_runner.spawn`; give the snapshot thread a task row.
  **Benefit:** Every background job shows up in Activity with a live status.

- [ ] **7.9 Stuck-pending batch items can never be retried**
  **Problem:** Batch runners skip missing items without updating their rows; boot recovery
  fails `bulk_tasks` but never touches `bulk_task_items` — items stay `'pending'` inside
  completed batches, and `retry_batch` selects `'failed'` only. Verified correction: retrying
  a non-AI-SEO batch crashes 500 on NULL store_id (it does not silently mis-run it) — still
  wrong, just differently.
  **Fix:** In the runners, mark skipped items `'failed'` with a reason; include `'pending'` in
  retry selection for completed batches; make `retry_batch` reject batches whose `kind` isn't
  an AI-SEO kind (400, not 500).
  **Benefit:** "Retry failed items" actually retries everything that didn't complete.

- [ ] **7.10 Two bulk flows have no per-item results**
  **Problem:** `bulk_apply_tags` (`seo_routes.py:1727-1759`) and `generate_collections`
  (~:1971-1989) create `bulk_tasks` rows but never insert `bulk_task_items` — only aggregate
  counters, no per-item error attribution (16 other creators do it properly).
  **Fix:** Insert items and update them per iteration like the other bulk flows.
  **Benefit:** When 3 of 40 tag applies fail, you can see which 3 and why.

- [ ] **7.11 Brave image search skips the rate-limit lock**
  **Problem:** `brave_image_search` (`serp_scraper.py:481-501`) reads/writes
  `_brave_last_call` without `_brave_lock` (web search uses it) — concurrent searches can
  fire back-to-back and trip 429 retries.
  **Fix:** Wrap the slot reservation in `_brave_lock` like `brave_search` does.
  **Benefit:** Fewer rate-limit stalls when several features search at once.

- [ ] **7.12 Competitor check failures silently vanish**
  **Problem:** `google_failures` (`competitor_service.py:214`) is never incremented or read;
  `brave_search` swallows errors internally and returns None, so failed keywords are simply
  missing from "completed" runs.
  **Fix:** Count None results per keyword; store the failure count on the run and show it.
  **Benefit:** A competitor check that half-failed says so instead of showing thin results.

- [ ] **7.13 Timezone hygiene (latent)**
  **Problem:** `news_routes.py:421, 509-510` write tz-aware datetimes into naive TIMESTAMP
  columns (the file elsewhere carefully normalizes to naive UTC); `ai_seo_routes.py:156`
  compares DB `NOW()` to app `datetime.now()`. Harmless while both containers run UTC; skews
  silently if that ever changes.
  **Fix:** Normalize to naive UTC at the two news writes; use DB `NOW()` on both sides of the
  ai_seo comparison.
  **Benefit:** Time math stays correct no matter how the servers are configured.

- [ ] **7.14 Short-TTL caches can serve divergent counts** *(severity corrected)*
  **Problem:** `_badge_cache` (60s) and `_store_baselines_cache` (1h) are per-worker with no
  write invalidation — the two workers can serve different counts within the TTL. Verified
  correction: growth is bounded by store count ("unbounded" was wrong); staleness is the real
  issue.
  **Fix:** Invalidate `_badge_cache` on writes that change counts (or drop TTL to ~15s);
  accept the 1h baseline TTL or invalidate on GSC pull completion.
  **Benefit:** Sidebar badges update promptly after you fix things.

- [ ] **7.15 Task-type registration gaps (cosmetic)** *(corrected)*
  **Problem:** `'article_image'` has no `AI_TASK_META` entry (renders via fallback with no
  owning-page link). Verified correction: the "inserts as 'running'" inconsistency applies to
  four task types, not one, and is harmless (both statuses are handled everywhere).
  **Fix:** Add the `article_image` meta entry; optionally normalize the four `'running'`
  inserts to `'pending'`.
  **Benefit:** Every task in Activity links back to its page.

---

# Part 8 — Convention cleanup (adopt a policy, then sweep)

- [ ] **8.1 One success envelope** — `{"ok": true}` vs `{"success": true}` vs both vs
  `{"status":"started"}`/`{"updated":true}`/`{"assigned":true}` vs bare arrays vs raw AI JSON
  returned directly (`seo_routes.py:1961`). Pick `{"ok": true, ...}`, sweep, and update JS
  checks. **Benefit:** predictable API; less JS defensive code.
- [ ] **8.2 One missing-`store_id` behavior** — 400 vs `200 []`/`200 null` across siblings;
  **six** helper copies (verified: `_get_store_id` ×5 + `_resolve_store_id`, not three);
  `seo_routes`/`technical_seo_routes` list endpoints scan ALL stores when store_id is omitted
  (see 4.15). Standardize on 400 + one shared helper; make list endpoints require store_id.
  **Benefit:** no accidental cross-store listings; consistent errors.
- [ ] **8.3 One status-code policy** — "already running" is 409 in five places but **200** in
  `interlinking_routes.py:196` and `keyword_strategy_routes.py:411` (verified; the original
  audit's ai_seo:1902 cite was actually a duplicate-prompt 400); upstream failures are
  variously 502/500/400; `fix_broken_link` 500s where `apply_fix` maps the same reasons to
  404/422; unvalidated `int()` casts 500 instead of 400 (`technical_seo_routes.py:322,324`;
  `interlinking_routes.py:550`; `keyword_strategy_routes.py:330`;
  `seo_issues_routes.py:441-442,484`); `update_store`/`delete_store` return ok for
  nonexistent ids. Adopt: 409 running, 502 upstream, 400 bad input, 404 missing.
  **Benefit:** the UI can react correctly to each failure kind.
- [ ] **8.4 No side-effectful GETs** — `GET /api/images/scan?refresh=1` (Shopify fetch + DB
  writes), landing seo GETs write `seo_score`, `GET /api/brands/collections` runs link-repair
  writes. Move mutations to POST. **Benefit:** refreshing a page never silently changes data.
- [ ] **8.5 Consolidate copy-diverged duplicates** — `_regen_landing_async` vs `run_landing`
  (51 identical lines); `_parse_scheduled_at` byte-identical in blog+news; technical-seo
  single vs bulk apply (field-name drift `value` vs `proposed_value` — verified); brand apply
  ×2; landing ai-optimize ×2. Extract shared functions. **Benefit:** fixes land once, not
  twice-or-drift.
- [ ] **8.6 One polling surface per job kind** — `GET /api/ai-seo/scan/<id>` vs
  `GET /api/tasks/<id>`, `GET /api/ai-seo/optimize/batch/<id>` vs `GET /api/tasks/bulk/<id>`
  (only task_routes versions sweep); two copy-paste bulk executor loops on the same tables
  (both spawn via task_runner — "two frameworks" was overstated). Point JS at the task_routes
  versions; consolidate executors. **Benefit:** consistent progress behavior everywhere.
- [ ] **8.7 Two unrelated cannibalization systems** — GSC-based (writes fixes) vs
  keyword-strategy (read-only) share zero code or definitions. At minimum share the
  "competing pages" definition; ideally one service. **Benefit:** both views agree on what's
  cannibalizing.
- [ ] **8.8 Blueprint boundary leaks** — 3 `/api/dashboard/*` routes live in gsc_routes;
  `/api/seo/items/.../primary-keyword` lives in the technical blueprint. Move them.
  **Benefit:** findable code.
- [ ] **8.9 Two landing-page pipelines** — brand (`brand_landing_pages`, timestamp-only) vs
  builder (`landing_pages`, versioned) publish Shopify Pages via unrelated code. Long-term:
  converge on the versioned pipeline. **Benefit:** one place to improve page publishing.
- [ ] **8.10 Validate `item_type` in technical routes** — any string falls into the collection
  branch and is stored verbatim (`technical_seo_routes.py:274-295, 524-525`), while
  `bulk_analyze` and seo_issues validate; `get_analysis` (:432-438) also queries without
  store scope. Add the validation + store scope. **Benefit:** garbage input can't create
  garbage analysis rows.
- [ ] **8.11 Make the image-alt preview prompt editable** — inline f-string at
  `seo_routes.py:762-773` while sibling actions use `prompts.*` settings. **Benefit:** you
  can tune it like every other prompt.
- [ ] **8.12 `request.json` outlier** — `blog_routes.py:2288` 415s on wrong content type;
  everything else uses `get_json(silent=True)` (and keyword_strategy uses `force=True` — a
  third pattern). Standardize on `get_json(silent=True)`. **Benefit:** consistent parsing.
- [ ] **8.13 Settings frozen at import + KeyError-able lookups** — `STOP_WORDS`/
  `NGRAM_MULTIPLIER` evaluated once at import (edits inert until restart); `NGRAM_MULTIPLIER[n]`
  at :211 can KeyError vs `.get(n, 2.0)` at :250; product scorer iterates weights (KeyError on
  custom weight) vs collection iterating checks (safe). Load lazily; use `.get` everywhere.
  **Benefit:** editing stop-words/multipliers takes effect without a restart and can't crash
  scoring.
- [ ] **8.14 Untangle the service↔route import cycle** — verified shape: a 2-cycle
  (`keyword_resolver` ↔ `technical_seo_routes`) plus `analysis_refresh` → routes, held
  together by function-local deferred imports. Move `_resolve_and_analyze`/`_upsert_analysis`
  into a service module both can import. **Benefit:** no fragile import-order landmines.

---

# Part 9 — Security hardening (single-user LAN context; all low urgency)

- [ ] **9.1 Add a Flask-side body limit** *(bounded, not unbounded — corrected)* — nginx
  already caps at 50MB and is the only published port; Flask itself has no
  `MAX_CONTENT_LENGTH` and the CSV handlers `read()` before checking size. Set
  `app.config["MAX_CONTENT_LENGTH"] = 50MB` and check `Content-Length` before reading.
  **Benefit:** defense in depth if the app is ever exposed directly.
- [ ] **9.2 Whitelist `task_type` on `POST /api/tasks`** — arbitrary type/input accepted and
  spawned (bogus types crash before any LLM call, but any *valid* type with attacker-chosen
  input burns OpenRouter budget). Validate against a whitelist + per-type input schema.
  **Benefit:** the generic task endpoint can't be abused to spend your AI budget.
- [ ] **9.3 Validate fetched image URLs** — `brand_routes.py:1030-1035` GETs
  Brave-result URLs with no scheme/private-IP/redirect checks (blind SSRF via search-steered
  results). Require http(s), resolve + reject private ranges, cap redirects.
  **Benefit:** the server can't be steered into fetching internal addresses.
- [ ] **9.4 Login hardening** — no rate limit/lockout anywhere (app or nginx) + documented
  default `admin:admin` with no forced change. Add a simple attempt counter with backoff and
  a first-login password-change prompt. **Benefit:** safe if the port is ever exposed beyond
  the LAN.
- [ ] **9.5 Vendor Chart.js** — `dashboard.html:164` loads it from cdn.jsdelivr.net (the only
  external asset, no `typeof Chart` guard — charts throw offline). Copy the file into
  `static/vendor/`. **Benefit:** dashboard works offline / if the CDN is blocked.

**Verified positives (no action):** API keys/OAuth tokens Fernet-encrypted and never
serialized into responses; GSC CSRF `state` checked with `session.pop`; auth enforced globally
via `before_request`. (Nit: PUBLIC_PATHS matching is `startswith` — fine today.)

---

# Part 10 — Database indexes

- [ ] **10.1 Index task polling** — `ai_tasks` has no status index (the interlinking partial
  index can't serve the poll) and `bulk_tasks` has no secondary index at all; the active-tasks
  count seq-scans both on every UI poll, and `ai_tasks` is genuinely append-only (zero
  DELETEs). Add `CREATE INDEX ... ON ai_tasks (status) WHERE status IN ('pending','running')`
  (same for bulk_tasks) + an index on `completed_at`; consider a pruning job.
  **Benefit:** the UI's background polling stays fast as history grows.
- [ ] **10.2 Index `bulk_task_items.bulk_task_id`** — FK with no index; polled repeatedly
  during runs. (Correction: delete endpoints DO exist — the missing index also slows those
  and the ON DELETE CASCADE.) **Benefit:** batch progress queries and cleanups stay fast.
- [ ] **10.3 Product-side index on `collection_product_map`** — reverse lookups by
  `(store_id, product_shopify_id)` can only use the store_id prefix of the UNIQUE index →
  per-store scans inside joins. Add `CREATE INDEX ... ON collection_product_map
  (store_id, product_shopify_id)`. **Benefit:** product pages and AI context loads that ask
  "which collections is this product in?" stop scanning.

---

# No action needed — findings refuted during verification

These were in the original audit but do **not** hold up against the code. Do not "fix" them.

1. **"Fake completions on unparsable AI output in ai_seo_routes" (orig. T2.4)** — The
   `ai_seo_*` task types dispatch to generators that signal unparsable output via
   `{"error": ...}` or `{"no_change": ...}`, never a bare `{"raw_response"}`. The existing
   `.get("error")` checks are adequate; the claimed KeyError/fake-completion path is
   unreachable. (The narrow real residual — parseable JSON missing description fields pushes
   empty strings — is covered by Part 2's push-succeeded guards.)
2. **"Image-alt preview ignores item_type" (orig. T3.6)** — The `scope` parameter carries the
   product/collection discrimination by design: the JS defaults collection recommendations to
   `collection_products`, the AI prompt mandates it, and the route's collection branch
   resolves correctly through `collection_product_map`. The shipped flow works.
3. **"Frontend counters disagree with backend scoring" (orig. T6.19)** — The pages in question
   are scored by `landing_page_seo_analyzer`, whose good band **is** 140–160 — matching the
   homepage/landing JS counters exactly. The audit compared against the product/article
   scorers, which never score these pages. (The lesser truth — landing bands differ from
   product bands app-wide — is a deliberate-or-not design choice, tracked under Part 5
   consistency work if desired.)
