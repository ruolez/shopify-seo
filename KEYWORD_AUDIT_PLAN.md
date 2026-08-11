# Keyword Logic Audit & Remediation Plan

**Date:** 2026-08-10 · **Scope:** every path where keywords originate, get organized, drive scoring/issues, feed AI generation, or get written to Shopify. Line numbers reflect the working tree on this date (including uncommitted changes) — re-verify each cite before editing.

---

## 1. How keywords flow through the app (verified map)

### Origination (14 distinct sources)

| Source | Producer | Lands in |
|---|---|---|
| On-page n-grams | `technical_seo_analyzer._extract_keywords` | `detected_keywords`, resolver candidate `"page"` |
| GSC page queries | `gsc_service` pull (6h scheduler) | `gsc_daily_metrics` → resolver `"gsc"` |
| Semrush CSV import | `semrush_import_service` | `semrush_keywords` → resolver `"semrush"` |
| Semrush semantic fallback | `keyword_resolver._semantic_semrush_matches` | resolver `"semrush_semantic"` |
| Strategy mappings | `run_content_audit` → `content_keyword_map` | resolver `"strategy"` |
| Manual pins | `POST /primary-keyword` → `keyword_assignments` | resolver `manual=True` (always wins) |
| Consolidation | `consolidate_master_keywords` (GSC+Semrush union) | `master_keywords` |
| AI families/intent | `keyword_ai_classifier` | `master_keyword_clusters`, `master_keywords.intent` |
| AI-invented | topic discovery, blog wizard, news, brand suggestions, `keywords_used` echoes | various silos |
| Heuristic | `serp_scraper.determine_primary_keyword` (title-derived), striking-distance ILIKE match | transient |

### The canonical path (correct by design — protect it)

`resolve_item_keywords` scores candidates (`100 × relevance × demand × opportunity × specificity × cannibalization`), guards against generic/vendor-only/identity-mismatch picks, and persists to `technical_seo_analyses.keyword_resolution`. Ownership is claimed store-wide per `variant_key` (plural-folded, token-sorted) via `_swap_primary_if_claimed` + `dedupe_primary_ownership`; manual assignments always win. Products/collections optimize flows read it through `get_or_refresh_resolution` (7-day TTL). Articles/landing/home/news are intentional exemptions (assigned or caller-supplied keywords).

### The problem in one sentence

The resolver architecture is sound, but **consumers bypass it** (stale reads, exemptions that shouldn't be, four incompatible normalizations, apply paths without guards), and **nothing invalidates it** when content, GSC data, or ownership changes — so the same item can carry different "primary keywords" in different features, and stale/wrong keywords can reach the live store.

---

## 2. Google guidance alignment (research, Aug 2026 — official sources)

What Google actually says (developers.google.com/search docs, current):

- **Keyword stuffing is a spam policy** ("unnatural repetition"); alt-text keyword-stuffing is explicitly named spam. Upper-bound repetition guards are defensible; density *targets* are not ("Keyword density is not a ranking factor. Never has been." — Mueller).
- **No official character limits** for titles or meta descriptions — truncation is pixel-based display behavior. "Title too long" is a truncation-risk *warning*, never a ranking error. Metas are rewritten by Google ~63% of the time and are not a ranking factor (CTR/snippet lever only).
- **Titles:** unique, descriptive, no repeated words/phrases, no boilerplate that varies by one token across pages. Nothing official supports keyword-must-be-first-word; mechanical front-loading produces the boilerplate patterns Google rewrites.
- **Cannibalization is not a Google concept** — multi-page ranking for one query is fine per Mueller; the real problems are near-duplicate/thin content. One-keyword-per-page is a legitimate *organizational* convention (which is how this app uses it), not a ranking rule.
- **Scaled content abuse** (March 2024 spam policy): mass AI generation "without adding value per page" is the named risk for bulk-rewrite tools. Mitigations: per-item grounding, uniqueness across the batch, review steps.
- **AI Overviews** (~48% of queries, −58–61% CTR when present): value shifts from exact-match placement to topic coverage, citability, complete merchant structured data. Our store-calibrated CTR curve already absorbs much of this.

**Verdict on this app:** direction is right (anti-stuffing guards, hero-only alt policy, GEO preservation, calibrated CTR, demand-grounded resolver). The misalignments are: the keyword pillar still puts **52% of its weight on exact-match placement of one phrase** vs 2% on anti-stuffing; several prompts contradict the hero-only alt policy and mandate repetition counts; and one flow string-concats keyword-first boilerplate titles.

---

## 3. Remediation plan

Severity: 🔴 wrong data can reach Shopify or corrupt the ledger · 🟠 inconsistent results across features · 🟡 alignment/cleanup.

### Phase 1 — Stop stale/wrong keywords reaching Shopify (write-path correctness)

- [x] 🔴 **`technical_seo_fix` runs on stale resolutions.** `create_fix_task` reuses the existing analysis untouched (`technical_seo_routes.py:613-632`); `run_task` reads `analysis["keyword_resolution"]` directly (`ai_agent.py:4547`), bypassing the 7-day TTL and the manual-assignment-supersede check. Fix: call `get_or_refresh_resolution` in the fix path (or re-resolve when older than TTL / superseded).
- [x] 🔴 **Scoped meta fixes can silently drop the primary keyword.** Length-only fixes narrow `failing_checks` to length (`ai_agent.py:4471-4481`) and `find_violations` has no meta-keyword-presence check (`ai_content_validator.py:82-93`). Add a "primary keyword still present in meta title" violation (presence, not position) when the input meta contained it.
- [x] 🔴 **`apply_cannibalization_fix` has no guard at apply time.** Ownership guard runs at generate only; apply writes client-supplied `new_title`/`new_meta_*` verbatim (`gsc_routes.py:581-645`). Re-run `keyword_ownership_guard` + `guard_shopify_fields` at apply.
- [x] 🔴 **Technical apply accepts arbitrary client `fixes[]`** with no tie to the stored `ai_fixes` row (`technical_seo_routes.py:798-828`) — stale browser tabs can apply superseded proposals. Verify submitted fixes against the stored proposal (hash or server-side lookup).
- [x] 🔴 **`apply_single_step` rewrites *other* pages** (cannibalization victims via AI-authored `item_type`/`item_id`) with no fresh resolution or ownership re-check (`keyword_strategy_service.py:1627`). Guard before write.
- [x] 🔴 **`set_primary_keyword` doesn't dispossess the previous owner** — the old owner's stored resolution keeps the keyword until incidentally re-resolved (`technical_seo_routes.py:924`). After a manual pin, find current owner(s) of that variant_key and re-resolve them.
- [x] 🔴 **Bulk optimize ownership snapshot bugs** (`task_routes.py:405,569`): (a) keyed to `items[0]["store_id"]` — wrong for mixed-store batches; (b) never refreshed mid-run; (c) in-batch claims use the model's *self-reported* primary matched by raw lowercase string, not `variant_key` — "dutch master"/"dutch masters" slip past. Fix all three (per-store maps, claim what was actually written, claim by `variant_key`).
- [x] 🔴 **GEO apply ignores `dropped_keywords`** — informational only (`ai_agent.py:2227`, `ai_seo_routes.py:1034`); an apply that dropped the resolved primary proceeds. Block (or require confirmation) when the resolved primary is in the dropped list.
- [x] 🟠 **`_auto_optimize_generated_collections` seeds no `claimed_keywords` and never refreshes the analysis** (`seo_routes.py:2548-2645`) — a generated batch can converge on one head term and enters the catalog unresolved. Seed claims + call `refresh_item_analysis`.
- [x] 🟠 **Content-gap guard fails open on infrastructure errors** — only `ValueError` propagates; DB/import failures log and proceed unguarded (`content_gap_service.py:1020-1023`). Fail closed. Also: client can send `skip_cannibalization_check: true` (`content_gap_routes.py:177`) — require it to be a deliberate UI action, not a default.
- [x] 🟠 **Alt-balance backstop gaps:** placeholder image alts (`image_routes.py:437,604` — alt = product title, no balance) and collection image alts (`seo_routes.py:2673`, `image_generator.py:300` — no balance, and a dict/string type bug at `seo_routes.py:2684` where `_extract_keywords` dicts are appended to a string list, producing alts like "kw and kw collection"). Route both through `ensure_applied_alt_balance` / fix the type bug.
- [x] 🟡 **Title unclamped in technical apply** — `_enforce_meta_limits` clamps only metas (`technical_seo_routes.py:22-33`); `title` written raw at `:813` (bulk `:1159`). Use `guard_shopify_fields` for parity with `seo_routes`.
- [x] 🟡 **CTR-rewrite fix targets unowned GSC queries** — `fix_context.kind=="ctr_rewrite"` uses raw query phrasing, never ownership-checked; with no stored resolution the GSC query silently becomes the target (`ai_agent.py:4568-4580,4718-4724`). Pass queries through the ownership map; require a resolved primary.
- [x] 🟡 **Blog scheduled publish** re-checks the money-page keyword guard only at approval, not at publish (60s scheduler; `blog_routes.py:1560-1575`). Re-check at publish time.

### Phase 2 — One keyword identity, one ownership ledger (data layer)

- [x] 🔴 **Unify normalization.** Four incompatible identities coexist: `_norm` (punctuation-strip), `variant_key` (plural-fold + token-sort), `_tokenize` (drops apostrophes/&/hyphens), plain `lower()`, plus `ai_agent._norm_kw` (keeps punctuation — can fail to match a resolver primary). Create one shared module (`keyword_identity.py`: `norm()`, `variant_key()`, `tokens()`), migrate every consumer, and make `variant_key` the only ownership identity.
- [x] 🔴 **`keyword_coverage.get_ownership_map` matches by exact string** (`keyword_coverage.py:52-63`) while every guard uses `variant_key` — coverage reports "unowned" for keywords the claim system considers owned by a plural/word-order variant. Align to `variant_key`.
- [x] 🔴 **Domain-property GSC accounts silently zero GSC demand.** `build_prefetch` uses `gsc_property` as a URL prefix with no `sc-domain:` handling (`keyword_resolver.py:70`; only `competitor_service.py:84,201` strips it). If the property is `sc-domain:…`, resolver page-URL lookups never match `gsc_daily_metrics.page` — no GSC candidates, no cannibalization check, cached 7 days. Same pattern at `ai_agent.py:64`, `gsc_service.py:831`, `seo_routes.py:157`. Add a shared `property_to_base_url()` (and verify which property type this store uses).
- [x] 🟠 **Striking-distance URL match uses `ILIKE '%handle%'`** (`seo_priority_service.py:67-72`) — `products/x` inherits `products/x-large`'s keywords. The same bug class was already fixed for cannibalization (comment at `:158`). Use exact-path compare.
- [x] 🟠 **`seo_priority_service` mixes identities in one branch** (`:317-321`): filters `unowned` by `_norm` but tests foreign ownership by `variant_key` — a variant of the page's own primary can be recommended against itself and trip `primary_review_suggested`. Use `variant_key` for both.
- [x] 🟠 **`primary_review_suggested` fires whenever resolved volume is 0** (`s_vol > 3 * 0`; `:347`). Require a volume floor. (Also: this flag plus `keyword_aligned`/`keyword_unresolved` are emitted and consumed by nothing — surface them in the queue UI or drop them.)
- [x] 🟠 **Plan-accepted assignments are never demoted** — demotion only handles `assignment_reason='manual_primary'` (`technical_seo_routes.py:965-972`); one item can accumulate conflicting accepted assignments and the resolver picks newest-by-`updated_at`. Define one demotion rule for all accepted assignments.
- [x] 🟠 **Semrush prefetch cap starves items** — bulk resolution loads only the top-500 store-wide rows by volume (`keyword_resolver.py:99-108`); low-volume URL-matched keywords for a specific item can never enter its candidate set. Fetch per-URL matches unconditionally; cap only the semantic-fallback pool.
- [x] 🟡 **`detected_keywords` holds two incompatible shapes** (resolver candidates with 0-100 scores vs raw n-grams with weights) and the UI renders both identically (`seo.js:2028`). Tag the shape (`source: "resolver"|"ngram"`) and render distinctly.
- [x] 🟡 **Settings drift:** `single_word_collection` code fallback 0.8 vs shipped default 0.6 (`keyword_resolver.py:409` vs `prompt_defaults.py:1188`); `striking_band` exists in settings but the SQL hardcodes 4-20 (`seo_priority_service.py:73`); `_semantic_semrush_matches` generic-words list is hardcoded and store-specific (`keyword_resolver.py:164-166`). Align each.
- [x] 🟡 **Duplicate plural-fold implementations** inside the resolver itself (`variant_key._fold` at `:42-44` vs inline `_stem` at `:393-394`). Collapse to one.

### Phase 3 — Align scoring & prompts with Google guidance

- [x] 🔴 **Rebalance the keyword pillar away from exact-match-everywhere.** Placement checks (`title 0.10 + title_start 0.05 + meta_title 0.08 + meta_desc 0.08 + url 0.06 + body_first_100 0.10 + image_alt 0.10 = 0.57`) dwarf the 0.02 anti-stuffing guard — the gradient tells the optimizer to put one phrase in six places, which is the boilerplate pattern Google explicitly warns against. Proposal: keep *presence* checks for title + one-of(meta title, first-100-words) at full weight; downgrade url/meta-desc/title-position/alt to low-weight or informational; raise anti-stuffing weight; never let more than ~35% of the pillar depend on repeating the exact phrase.
- [x] 🔴 **Fix the title-position/containment split.** `_keyword_position_in_title` compares only the keyword's *first token* (`technical_seo_analyzer.py:300-313`) while `title_has_keyword` uses phrase/token-subset — a title can score 100 on position and 0 on containment, and the validator enforces *only* the positional test (`ai_content_validator.py:96`), so gated output can immediately raise a high-severity issue on re-analysis. Make the validator and analyzer share one containment-first rule; treat position as a soft bonus (Google has no first-word rule).
- [x] 🔴 **Word-boundary matching.** `_keyword_in_text` is substring-based for single tokens ("pack" matches "packaging") and unordered token-subset for phrases (`technical_seo_analyzer.py:289-297`) — false passes across all 8 checks, the resolver's relevance/identity guard, and the content audit. Match on token boundaries.
- [x] 🟠 **Alt-check denominator mismatch:** analyzer counts all images including alt-less (`:545-573`); validator/backstop count only non-empty alts (`ai_content_validator.py:104-115`) — the same product can be "good balance" in one and "stuffing" in the other. Also: zero-alt sets return early in `ensure_applied_alt_balance` and no-image products silently lose 0.20 of the pillar with one issue shown. Unify the denominator; renormalize weights when checks are N/A.
- [x] 🟠 **Density guard math:** binary 100/0 at 4%; occurrences÷words gives multi-word phrases ~n× more headroom; and on short bodies `body_first_100_has_keyword` + `keyword_density` are mutually unsatisfiable (one mention in 20 words = 5%). Count matched *words*, add a minimum-body-length floor before the guard applies, and keep it one-directional (never advise adding repetitions — Google: density isn't a thing).
- [x] 🟠 **Prompt contradictions with the hero-only alt policy** (Google names alt stuffing as spam): `landing_page_seo` "all images … with keyword inclusion" (`prompt_defaults.py:1292`), `landing_page_rewrite` alt-keyword-everywhere (`:1402`), `cannibalization_fix` rewrite-every-alt (`:1609`, currently rescued only by the write-time balancer). Align all three to hero-only.
- [x] 🟠 **Repetition mandates in prompts:** `landing_page_seo` asks for a `target_density` (`:1302` — the only density target in the app), `content_gap` "include the new keyword 2-3 times" (`content_gap_service.py:1066`), `brand_description` "brand name 3-5×" (`:765`), stuffing-fix still mandates first-paragraph + heading placement (`ai_agent.py:4678-4680`). Replace counts with "mention naturally at least once; never force repetition."
- [x] 🟠 **`generate_fixes_for_assignment` produces keyword-first boilerplate:** `f"{keyword.title()} - {meta_title}"[:60]` — mechanical front-loading + mid-word truncation + likely duplication of a keyword already present (`keyword_strategy_service.py:1373-1385`), never validated. Route through the AI fix flow or at minimum dedupe, word-boundary-truncate, and validate.
- [x] 🟠 **Stuffing-fix prompt names the wrong keyword** — uses `detected_kws[0]` while the failing check was scored against the resolution primary (`ai_agent.py:4674` vs `:4744`). Use the resolution primary.
- [x] 🟠 **`brand_seo` contradicts itself:** preamble says "prioritize keywords with volume / striking distance" (`ai_agent.py:3329-3332`) while the injected block says "do NOT substitute a higher-volume keyword"; validation runs against `user_keywords[0]`, not the brand collection's resolved primary (`:6363-6369`); brand keyword suggestions come from Brave SERP word frequency (`brand_routes.py:1432-1495`). Adopt the corrected product/collection preamble; validate against the resolved primary.
- [x] 🟡 **`topic_enrich` lacks the RESERVED MONEY-PAGE KEYWORDS block** that `topic_discovery` has (`ai_agent.py:5874` vs `:6073`) — enriched topics can claim money-page primaries. Add the block.
- [x] 🟡 **Secondary keyword recommendations are not ownership-filtered** — "RECOMMENDED TARGET KEYWORDS" #2-4 are volume-ranked picks that can be other pages' primaries (`ai_agent.py:520-573`). Filter through the ownership map.
- [x] 🟡 **Reframe length checks honestly** (no code change to the bands needed): label title/meta length issues as "may truncate in results," not errors — Google has no character limits and rewrites metas ~63% of the time. Copy change in `ISSUE_DEFS` labels + prompt prose.
- [x] 🟡 **Batch uniqueness guard (scaled-content-abuse mitigation):** bulk optimize has no cross-item similarity check — generated metas can converge on near-identical boilerplate, the one pattern tools under-enforce and Google explicitly polices. Run the existing duplicates detection across a bulk batch's outputs before apply.

### Phase 4 — Lifecycle: triggers, invalidation, staleness

- [x] 🔴 **Shopify sync never invalidates analyses** — content is overwritten wholesale (`sync_service.py:96-110,397-412`) while `technical_seo_analyses` (resolution + checks) survives; detection depends on the user opening the Issues tab (the "outdated" sweep is gated behind a page visit, `seo_issues.js:660`). Fix: run an `outdated`-scoped bulk analyze at the end of `_sync_store_inner` (sync already re-runs interlinking analysis unconditionally — keywords deserve the same).
- [x] 🔴 **Unpublishing/deleting a product releases its keyword claim silently** — sync prunes the analysis row (`sync_service.py:356-368`), freeing the variant_key with no re-dedupe; demoted pages keep their "owned by higher-scoring page" state forever. Trigger an ownership pass (or targeted re-resolve of affected variant_keys) after prune.
- [x] 🟠 **Concurrent product+collection bulk analyses race the ownership ledger** — the Issues-tab path spawns both threads simultaneously (`seo_issues_routes.py:272`) and each type's dedup/swap reads the other type's mid-flight primaries; final ownership is non-deterministic. Serialize the two passes (the Keyword Strategy JS path already does).
- [x] 🟠 **No dedupe after `_execute_bulk_fix_apply`** (`technical_seo_routes.py:1085-1247`) — bulk AI fixes can leave two pages sharing a primary until the next analyze run. Add the ownership pass (and conversely, scope `dedupe_primary_ownership` to the analyzed set instead of store-wide when a sweep touches 3 items).
- [x] 🟠 **Staleness constants disagree:** Keyword Strategy calls an analysis stale at 7 days (`keyword_strategy_routes.py:76`), the Issues tab at 30 (`issue_scanner.py:37`); resolver TTL is 7 days but lazy-only (nothing sweeps it, and no surface displays resolution age). Pick one constant per concept; add a nightly TTL sweep so the freshness strip's claim is true.
- [x] 🟠 **Demand changes never re-resolve:** GSC pulls (6h) and consolidation rewrite the exact data resolution scores are built on, with no invalidation (`gsc_service.py:1241`, `keyword_strategy_service.py:37`). Queue re-resolution for pages whose query set changed materially; auto-run consolidate on the scheduler (its 7-day staleness badge currently depends on a button).
- [x] 🟠 **Priority queue reads stored resolutions with no refresh** (`seo_priority_service.py:211-223`) — the 7-day contract is bypassed for the home queue entirely. Acceptable for read-speed, but pair it with the TTL sweep above so stored rows are never older than the TTL.
- [x] 🟠 **`content_audit` DELETEs the store's whole `content_keyword_map`** including manual-pin mirrors, and resolver relevance (`ckm_relevance`, via `max()`) silently rescores after every audit with no re-resolution (`keyword_strategy_service.py:705`, `keyword_resolver.py:215`). Preserve manual rows; trigger re-resolution after audit.
- [x] 🟡 **Issue-scan cache not invalidated by `refresh_item_analysis`** — just-fixed items keep their issues up to 300s, and stale `fixable_issue_counts` feed the measuring-window decision (`issue_scanner.py:316-319,510-518`; `seo_priority_service.py:246-263`). Bust per-store cache on refresh.
- [x] 🟡 **GSC window lag inconsistency:** resolver/cannibalization end at `today-3`; consolidate and coverage contenders include the 3 partial days (`keyword_strategy_service.py:40`, `keyword_coverage.py:273`) — volumes/positions disagree by construction. Standardize on `today-3`.
- [x] 🟡 **`get_or_refresh_resolution` double-swaps and mutates on read** — it calls `_resolve_and_analyze` (which already swapped) then swaps again (`keyword_resolver.py:678-688`), two extra store scans per refresh; and prompt-building/guard reads write `technical_seo_analyses` as a side effect, re-stamping the `updated_at` that outdated-detection reads. De-dupe the swap; consider a separate `resolved_at`-only stamp on lazy refresh.
- [x] 🟡 **`serp_cache` never invalidated on apply** (24h TTL, boot-only purge) — a re-optimize within 24h benchmarks against the pre-change SERP context (`serp_scraper.py:66-85`, `main.py:128`). Evict page-keyed entries in `refresh_item_analysis`.
- [x] 🟡 **Score history freezes the stored keyword pillar** at snapshot time (`gsc_service.py:1055-1112`) — trends embed whatever staleness existed. Snapshot after the nightly TTL sweep runs.
- [x] 🟡 **Stale-job sweeper (6h) can mark a live full-catalog analyze as failed** while its thread keeps mutating (`task_runner.py:104`) — raise the ceiling for `tseo_analyze` or heartbeat the task.

### Phase 5 — Cleanup & dead code

- [x] 🟡 **Remove the Jaccard `cluster_keywords` route** (`keyword_strategy_routes.py:326`) — no JS calls it, and running it would DELETE the AI family taxonomy (both writers wipe `master_keyword_clusters`).
- [x] 🟡 **Retire the legacy `keyword_taxonomies` silo** (`task_routes.py:147`, read only by `seo_routes.py:1829,2268`) or migrate its consumers to `master_keywords`. *(Resolved as KEEP: inspection showed it is a live feature — seo.js keyword-analysis modal, tag application, and taxonomy-driven collection generation all consume it. It is a separate workflow, not dead code; only documented as distinct from the strategy ledger.)*
- [x] 🟡 **`heading_structure` mislabeled `source:"keyword"`** (`issue_scanner.py:158`) — takes no keyword input; inflates "keyword issues" counts. Relabel `content`.
- [x] 🟡 **Three "issue count" definitions coexist** (`tsa.issues_count`, `_count_unified_issues`, `scan_store_issues`) — the same product shows three numbers across the SEO list, item modal, and Issues tab. Converge on the issue-scanner definition. *(Resolved: `_count_unified_issues` already delegates to the scanner; the third definition — raw `tsa.issues_count` — only surfaced through two dead list endpoints with no UI callers, now removed.)*
- [x] 🟡 **Weight-summation asymmetry:** product analyzer iterates weights (unknown key → `KeyError`), collection iterates checks (orphan weight silently deflates the score); neither normalizes by `sum(weights)` (`technical_seo_analyzer.py:583,788`). Normalize both.
- [x] 🟡 **Drop or consume `on_page_primary`** (stored, never read) and the dead queue flags noted in Phase 2.
- [x] 🟡 **`_extract_keywords` runs unconditionally even when a primary is supplied** and its output is immediately overwritten by resolver candidates (`technical_seo_analyzer.py:350`, `technical_seo_routes.py:95`) — skip when unused.
- [x] 🟡 **Resolver-failure split-brain:** when resolution throws, checks are scored against `primary_kw=None`/top n-gram while the old `keyword_resolution` is COALESCE-kept (`technical_seo_routes.py:86-99,119`) — the row advertises a primary its checks weren't scored against. Stamp the analysis with the keyword actually used.

---

## 4. Validated as correct — do not "fix"

- Resolver architecture: demand-grounded scoring, identity/generic guards, manual-wins, variant_key ownership, blog/money-page separation — matches the intent→funnel→page-type mapping in `seo-keyword-strategy-guide.html` and current best practice.
- Hero-only alt keyword policy + `balance_alt_keyword`/`ensure_applied_alt_balance` backstops (uncommitted work) — exactly Google's alt-stuffing guidance.
- Density as a one-directional stuffing *guard* (not a target); no meta-keywords generation anywhere.
- Store-calibrated CTR curve with post-AI-Overviews fallback — honest under AIO suppression.
- GEO/MUST-PRESERVE prompt blocks; title 50-60 / meta 120-160 as *generation* aims (fine as truncation heuristics — just label them as such in issues).
- One-keyword-per-page as an organizational ledger (not presented as a Google rule); conflict panel surfacing instead of blind auto-fix.
- `keyword_ownership_guard` failing closed on generate paths; RESERVED MONEY-PAGE KEYWORDS block in topic discovery.

## 5. Verification checklist (after each phase)

- [ ] Pick 5 items; confirm the identical primary keyword appears in: item modal Keywords tab, priority queue `target_keyword`, coverage matrix owner, AI prompt block, and the applied meta title.
- [ ] Run bulk analyze (products+collections) → assert zero variant_key collisions across types (`_detect_conflicts` clean except documented twin pages).
- [ ] Sync a store after editing a product title in Shopify admin → analysis refreshes without visiting the Issues tab.
- [ ] Generate + apply one technical fix on an item whose resolution is >7 days old → fix used a fresh resolution.
- [ ] Bulk-optimize 10 products → no two generated meta titles/descriptions near-duplicate; no output lost its resolved primary.
- [ ] Confirm GSC property format for the live store; if `sc-domain:`, verify resolver GSC candidates are non-empty after the Phase 2 fix.
