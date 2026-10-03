# Whole-App Audit — October 2026

Scope: does the app still help the store, do its features connect, and where do parallel
mechanisms compete. Analysis only; no code was changed.

Earlier audits (`AUDIT_VERIFICATION.md`, `KEYWORD_AUDIT_PLAN.md`) covered write-path bugs and
the keyword pipeline. Their fixes are not repeated here. This audit looks one level up.

**How it was done:** five parallel code reviews (recommendation engines, content writers,
scoring rules, frontend experience, content pipelines), read-only queries against the live
database (store 2, a2ztobacco.com), a fetch of three live storefront pages, and a check of
current Google guidance. Findings marked ✔ were confirmed by reading the code on both sides;
findings marked ~ are inferred and should be confirmed before fixing. Nothing was exercised
in the browser.

---

## 1. Bottom line

The app has become very good at one thing — polishing on-page text for products and
collections — and that job is finished. 749 of 750 products are optimized and the average
on-page score is about 98. Meanwhile Google sends the store about 280 clicks a month.

The score and the outcome have come apart. Between 4 Aug and 3 Oct the store's average SEO
score fell from 84.2 to 82.2 while weekly clicks rose from 40 to 101. The app measures what
it can rewrite, not what is holding the store back.

Three things follow:

1. **The app is aimed at a finished problem.** What limits the store now is ranking position
   (68% of impressions are on page two or lower), a blog that carries most of the traffic but
   is not managed after publishing, and things no part of the app measures (indexing, speed,
   links, reviews).
2. **Your fear of competing mechanisms is justified.** About ten buttons can rewrite the same
   product description with different rules, and the main one erases what the others added.
   Six definitions of cannibalization, two of "CTR underperformer", and six separately
   rendered "SEO scores" coexist.
3. **Features do not hand work to each other.** Only two of roughly fifteen recommendation
   engines put actionable rows on Home. Published articles never register their keyword, so
   the gap tools keep recommending topics you already covered. Most changes are never
   recorded, so the app cannot tell you what worked.

---

## 2. What the live data says

| Fact (store 2) | Value | Why it matters |
|---|---|---|
| Products optimized | 749 of 750; none in the last 28 days | On-page work is saturated |
| Last recorded change event | 11 Aug 2026 | Either no product work since, or it is unrecorded |
| Google clicks, September | ~282 (blog 146, product 63, home 37, collection 33) | The blog out-earns 750 products and 205 collections combined |
| Impressions by position, 28 days | 1–3: 2.6K · 4–10: 17.5K · 11–20: 18.4K · 21+: 41.6K | Position is the constraint, not snippet text |
| Products with any click, 28 days | 42 of 750 | |
| Collections with any click, 28 days | 25 of 205 | Collections should carry head terms; they barely rank |
| Articles | 264 (201 news, 63 blog); 105 published in July; up to 9 in one day | Volume pattern Google's scaled-content policy targets |
| Articles with zero impressions, 90 days | about 134 of 264 | Half the blog is dead weight nobody reviews |
| Top 15 pages by clicks | 12 are articles | |
| Desktop vs mobile, 28 days | Desktop 41.9K impressions, 38 clicks, avg position 31.8; mobile 37.9K, 254 clicks, 17.4 | The app blends devices; desktop drags every CTR figure |
| Content gaps | 1,301 new, 256 addressed | Backlog nobody works |
| Keyword assignments | 2,829 proposed, 3 accepted | The strategy plan is unused |
| Queue dismissals | 0 | Snooze/dismiss unused |
| AI tasks, last 30 days | Almost all article work; 1 product optimize | You have moved to content; the app's core has not |
| Organic orders, September | 53 orders, about $7.4K | Landing page is blank on most rows, so revenue cannot be tied to pages |
| Issues, 3 Sep → 3 Oct | 54 → 145 with no product work | Worth explaining before trusting the Issues count |

Live storefront check: pages return fast (~0.3s HTML), canonical tags are correct, product
and collection schema is present. Product and collection pages still emit `FAQPage` schema,
and `/llms.txt` is served.

---

## 3. Competing logic

### 3.1 Ten writers for one description

A product description can be rewritten from: the item modal's AI analysis, Issues "Fix with
AI", AI Search optimize, Duplicates apply, GSC "Rewrite title/meta", GSC cannibalization fix,
Content Gaps "Optimize Existing", Keyword Strategy steps, Keyword Actions "Optimize now", and
Interlinking apply. There is no ordering rule and no "last changed by" marker.

- [ ] ✔ **C1 — Full optimize rewrites blind.** `_generate_description` gives the model only
  the current description's word count, never its text (`ai_agent.py:2481-2491`; collection
  `:2671`, brand `:2837`), and `prompts.product_description` has no preservation clause
  (`prompt_defaults.py:67-82`). Running it after a GEO pass, an interlinking apply or a
  targeted fix erases the Specifications table, Expert Verdict, question headings and injected
  links. Only images are restored. This breaks your stated GEO-preservation requirement on
  the app's most prominent button and on bulk auto-apply.
- [ ] ✔ **C2 — Three philosophies for the same field.** Full optimize: write new, about 1.2×
  competitor length, "exceed competitor average for better rankings". GEO: enhance, add
  tables and question headings (`prompt_defaults.py:1282-1310`). Targeted fix: keep structure,
  output limited to `p, h2, h3, ul, li` (`ai_agent.py:4858-4864`) while also told to preserve
  GEO tables. Each button undoes the previous one's structure.
- [ ] ✔ **C3 — Content-gap "Optimize Existing" contradicts its own prompt.** Its system prompt
  says "Do NOT generate a product description" while the user prompt demands a new one and
  supplies only title and metas (`content_gap_service.py:1131-1165`). Adding one secondary
  keyword can replace the whole description, with no image restore and no primary-keyword
  guard (`:1196-1248`).
- [ ] ✔ **C4 — The local copy diverges from Shopify.** Shopify receives the image-restored
  HTML (`seo_routes.py:1251-1253`); the local mirror stores the raw version (`:1301`). The
  next rewrite works from a copy that has lost the image.
- [ ] ✔ **C5 — No check against live content.** Only intro links read the live body before
  writing (`intro_link_service.py:317-322`). Every other whole-body writer works from the
  mirror, so edits made in Shopify admin since the last sync are overwritten.
- [ ] ✔/~ **C6 — Old previews can be applied later.** Only targeted fixes are single-use.
  Restyle applies a stored body without checking the article changed
  (`blog_routes.py:4021-4050`). The full-optimize apply takes no task reference or content
  hash (inferred from `seo_routes.py:1170-1198`).
- [ ] ✔ **C7 — Two writers for the FAQ metafield.** The SEO "FAQ schema" action
  (`seo_routes.py:996`) and GEO apply (`ai_seo_routes.py:1119`) both overwrite
  `seo.faq_schema_json_ld`. Last click wins, and neither checks the questions are visible on
  the page. Articles have the same pattern (`blog_routes.py:1841`, `:4054`).
- [ ] ✔ **C8 — Interlinking re-emits the whole body** through the model instead of splicing
  (`interlinking_routes.py:935`, `:1292`), with no re-analysis and no change record.
- [ ] ✔ **C9 — Prompts outside Settings.** Keyword Strategy steps
  (`keyword_strategy_service.py:1822`), content-gap prompts (`content_gap_service.py:644`,
  `:1134`) and the targeted-fix description rules (`ai_agent.py:4855`) are hardcoded, so
  editing prompts in Settings changes some flows and not others.

### 3.2 Same concept, different definitions

- [ ] ✔ **D1 — Cannibalization has six definitions.** Home count (≥10 impressions, 30 days,
  `dashboard_service.py:1164`), GSC page (≥3 per page, `gsc_routes.py:328`), priority warning
  (other page at position ≤10, `seo_priority_service.py:152`), content-gap risk
  (`content_gap_service.py:11-92`), Keyword Strategy report (two primaries,
  `keyword_strategy_service.py:1189`), coverage conflicts (`keyword_coverage.py:358`). The
  Home number never matches the page it links to.
- [ ] ✔ **D2 — "CTR underperformer" has two definitions.** Home: ≥200 impressions, top 10,
  below 0.8× expected (`ctr_benchmarks.py:42-47`). GSC page: ≥10 impressions, any position,
  below 0.5× (`gsc_service.py:723-736`). Home links to a list that shows different rows.
- [ ] ✔ **D3 — Two "gap" universes.** Content Gaps is built from Semrush gap rows and a title
  substring match; the Keyword Strategy matrix is built from ownership. Content Gaps checks
  ownership only at apply time (`content_gap_service.py:1090`). Home counts only the first.
- [ ] ✔ **D4 — Score colours differ by page.** 80/50 in shared helpers (`app.js:170-199`),
  70/40 in Blog, Landing Pages and Brands. In the Brands modal a 72 is green in the header and
  amber in the bars (`brands.js:849` vs `:863`).
- [ ] ✔ **D5 — One product, several "SEO scores".** The SEO list shows the unified score;
  Duplicates, Brands and the AI store audit show the content pillar alone; keyword coverage
  labels the keyword pillar alone as "SEO score" (`keyword_coverage.py:237`).
- [ ] ✔ **D6 — "Low" means different things**: below 60/70/100 in the issue scanner, 50/70 in
  priorities, 70 in coverage, 60 or 50 for AI readiness.
- [ ] ✔ **D7 — Length and structure rules differ by flow.** Article meta title: 35–46 in the
  scorer, 60 in the validator, "max 60" in topic prompts, 50–60 in content-gap publish. Meta
  description good band: 120–160 for products, 140–160 for landing. Landing word count: 300
  scores 100, the prompt demands 500. Body `<h1>`: required on landing pages, flagged and
  stripped on products. Headings: two of any level on products; two H2 plus an H3 on articles.
- [ ] ✔ **D8 — The Home queue sorts on mixed units.** Priority rows carry Semrush monthly
  volume × a flat rate; CTR rows carry a 28-day click gap; summary rows carry nothing. All are
  sorted together (`dashboard_service.py:672`). A page can appear twice
  (`priority:` and `ctr:` keys are not deduplicated).
- [ ] ✔ **D9 — Two lifecycle clocks.** The impact verdict lands after 9–16 days
  (`impact_service.py:18-23`); the queue hides the item for 28 days regardless and never
  reads the verdict. A page that got worse stays locked, then returns with the same advice.
- [ ] ✔ **D10 — Five outcome trackers that do not talk.** `impact_service`, the "optimized"
  score comparison, cannibalization verification, gap closures, and
  `keyword_strategy_changes`.

---

## 4. Disconnections

- [ ] ✔ **X1 — Most changes are never recorded.** `seo_change_events` is written only from
  the full optimize, targeted fix and bulk paths. GEO apply, cannibalization fixes, brand
  apply, content-gap optimize, interlinking, intro links, article edits, restyle and landing
  publish write none — though several still start the 28-day lock. The record also truncates
  values to 200 characters, so it cannot restore anything. The only undo is in Keyword
  Strategy, and it does not check the field still holds what it wrote
  (`keyword_strategy_service.py:2342-2396`).
- [ ] ✔ **X2 — Articles are second-class everywhere except creation.** They have no keyword
  column, so a published post never claims its keyword and the gap tools keep recommending
  it. They are absent from the priority queue, the impact tracker and the shared item modal.
  Keyword Strategy's open/optimize buttons do nothing for articles
  (`keyword_strategy.js:715-720`). Yet articles bring most of the traffic.
- [ ] ✔ **X3 — Only two engines put actionable rows on Home.** Coverage gaps, the assignment
  plan, AI readiness, off-site visibility, feed readiness and anomalies never appear; the
  rest appear as "N things, go to page X" rows that always sort last.
- [ ] ✔ **X4 — Insight pages stop at the data.** Semrush tables, Competitors, and GSC Top
  Pages/Queries have no action. Interlinking, AI Search and Duplicates cannot open the item
  they are about.
- [ ] ✔ **X5 — Four content backlogs, four generators.** Topic suggestions, content gaps, the
  gap matrix and AI-visibility ideas are separate lists. Blog wizard, Content Gaps wizard,
  Keyword Actions and Landing Pages each generate and publish. The Content Gaps article path
  skips research, validation, template, scheduling and IndexNow, and its insert omits
  `article_type` (`content_gap_service.py:938`), so the post is live on Shopify but missing
  from the Blog list until a restart.
- [ ] ✔ **X6 — Revenue is display-only.** No engine weights by it, and most order rows have
  no landing page.
- [ ] ✔ **X7 — Drops are detected but never queued.** Anomalies go to a feed; they are not
  matched to a recent change on the same page.
- [ ] ✔ **X8 — AI readiness goes stale.** Its score is written only by AI Search flows, so it
  stays high after another tool wipes the GEO sections. AI visibility prompts are invented by
  the model from product types, not taken from your keywords, and are not re-measured after a
  change.
- [ ] ✔ **X9 — Recommendations depend on buttons.** Content-gap discovery, the content audit,
  the assignment plan and Semrush import are manual. Striking-distance advice is only as
  fresh as the last Semrush CSV (last import 28 Aug).
- [ ] ✔ **X10 — Dead links and counters.** "Open Research Topics" points at a blog tab that
  does not exist (`keyword_actions.js:867`); the GSC cannibalization "View in SEO" link is a
  bare `/seo` (`gsc.js:760`); Home's "Schema pushed" bar links to neither schema screen; a
  dashboard counter reads a topic status that is never set (`dashboard_service.py:964`).
- [ ] ✔ **X11 — The empty queue is a dead end.** It always says everything is being
  re-measured, even when nothing is (`dashboard.js:260-264`).

---

## 5. Alignment with current SEO practice

What changed this year, from Google's own documentation and reporting on it:

- Google published an AI-search guide (15 May 2026) stating that optimizing for AI search "is
  still SEO". It says llms.txt, special schema, content chunking and AI-specific rewrites are
  not needed, and that non-commodity content, indexability, page experience and Merchant
  Center data are what help.
- FAQ rich results were retired for all sites on 7 May 2026. The markup is harmless but earns
  nothing in Google.
- Three spam updates ran in 2026 (March, June, August) and a fourth began 24 September.
  Scaled content abuse — many low-value pages, AI-written or not — is a named policy with
  manual actions since 2025. Commentary on the 2026 core updates emphasizes pages that add
  something the top results lack.

### Rules to change

- [ ] ✔ **S1 — Remove fabricated testimonials and statistics.** The landing prompt asks for
  "3 realistic, detailed testimonials with roles" and "specific, credible numbers"
  (`prompt_defaults.py:1629-1630`) and renders them as attributed quotes. This is invented
  review content on a live page — a trust and legal exposure, and against every other prompt
  in the app. Highest-priority single fix.
- [ ] ✔ **S2 — Unattended publishing with no budget.** "Schedule" writes and publishes a post
  nobody has to read; the publish-time keyword conflict is only logged
  (`blog_scheduler.py:88-104`). News auto-batch publishes any number at once. With 105
  articles in July and half of all articles earning no impressions, this is the store's main
  exposure to the spam updates.
- [ ] ✔ **S3 — Word-count ladders reward padding.** Articles need 1,500 words for full marks;
  products 300; prompts say to exceed the competitor average "for better rankings". Google
  has no length preference.
- [ ] ✔ **S4 — Blog prompts still mandate keyword repetition** ("in 2–3 H2 headings and the
  conclusion", `prompt_defaults.py:467,718,803`) — the pattern removed from product prompts
  in August — and the article scorer has no stuffing check.
- [ ] ✔ **S5 — Format for its own sake.** A table plus FAQ is scored on every article
  (`blog_seo_analyzer.py:114-120`); news is scored on blog rules it cannot meet.
- [ ] ✔ **S6 — AI-readiness score rewards things with no basis.** FAQPage on every page
  (inside a 22% schema weight); outbound links and vendor-name mentions as "entity authority";
  counting the words "research" and "study" as fact density; `updatedAt` as freshness;
  Google-Extended and training bots as crawlability. Present this score as a bet on
  non-Google assistants, or rebuild it around what Google's guide names.
- [ ] ✔ **S7 — Ratings rewarded without checking their source.** Nothing verifies that
  `aggregateRating` comes from real first-party product reviews.
- [ ] ✔ **S8 — Authorship.** Every article is authored by the organisation "A2Z"
  (`shopify_client.py:1304`); blog posts carry no sources block (news does). Weak
  experience and expertise signals on AI-written content.
- [ ] ✔ **S9 — Meta description is over-weighted** (30–35% of the content pillar plus part of
  the keyword pillar) for something that is not a ranking factor and is usually rewritten.
- [ ] ✔ **S10 — The 40% search pillar is unexplained and perverse at the edge.** A perfect
  page with weak search data tops out at 68 (amber). A page with no impressions scores 100,
  then falls to about 68 when it earns its first ones. The UI never explains the weights.
- [ ] ~ **S11 — Internal-link counters only match relative links** (`blog_seo_analyzer.py:82`),
  so absolute links to your own domain count as zero.
- [ ] ~ **S12 — Feed readiness may not apply to this store.** Google Shopping does not accept
  tobacco products, and AI shopping channels are likely to follow. Confirm eligibility before
  investing further in the feed score.

### Important things nothing measures

Index coverage (which pages Google has actually indexed), Core Web Vitals, redirect and 404
health, crawl bloat from tag and filter URLs, near-duplicate or manufacturer-copied
descriptions (Duplicates finds only exact meta matches), thin collections, out-of-stock
handling, review volume, backlinks and referring domains, click depth, device split, article
decay, and similarity between generated articles.

---

## 6. Helpfulness gaps

- [ ] **H1 — No store-level goal or plan.** Everything is per-item. Nothing says "these are
  the ten keyword families we are trying to win, and here is progress."
- [ ] **H2 — No answer to "what did the app do for me".** No monthly summary, no win rate by
  type of change, no digest.
- [ ] **H3 — No page explains itself.** No template has an intro. Cannibalization, striking
  distance, AEO/GEO, IndexNow and llms.txt appear unexplained; "AI SEO" and "AI Search" are
  both used.
- [ ] **H4 — Too many verbs for one action.** Apply / Push / Publish to Shopify; AI Optimize /
  Optimize with AI / Get AI Recommendations; Fix / Enhance / Rewrite / Generate Fix.
- [ ] **H5 — Live writes without a confirmation step** in Duplicates, Content Gaps, GSC
  cannibalization (including bulk) and Keyword Strategy "apply all".
- [ ] **H6 — Two store selectors on eight pages**; GSC ignores the global one.
- [ ] **H7 — Navigation does not match tasks.** Keyword Strategy and Content Gaps are action
  tools filed under Insights; "Image Manager" only handles missing images; "Home" and
  "Home Page" are different things.
- [ ] **H8 — Overlapping screens.** Five keyword views, three "close to ranking" lists, three
  cannibalization views, two schema screens, three internal-link tools, two issue lists.

---

## 7. Recommended direction

Ordered so each phase makes the next one safer. Sizes are rough.

**Phase 0 — Stop the harm (small).**
S1 testimonials; C1 pass the current description and a shared preservation block into full
optimize; C3; C4; S2 require a human "reviewed" step and a store-wide publish budget for
scheduled and batch publishing; H5 confirmations.

**Phase 1 — One way to change a page (medium-large).**
One apply function per item type that every flow calls: read live content, guard, restore
images and links, write Shopify, write the same value to the mirror, re-score (SEO and AI
readiness), record one full-value history row with a working undo. Previews bound to a
content hash. Interlinking splices instead of re-emitting. One FAQ owner. All prompts in
Settings. Fixes C5–C9, X1, X8.

**Phase 2 — One vocabulary (medium).**
One definitions module for cannibalization, CTR underperformer, gap and "low". One table of
length and structure rules per page type, read by prompts, validator and scorer. One set of
colour bands. One score name per concept. Fixes D1–D7, S3–S5, S9–S10.

**Phase 3 — Articles become first-class (medium).**
Publish writes the article's keyword to the ownership ledger. Articles get the item view,
the queue, impact tracking and change history. One content backlog and one article generator;
Content Gaps and AI Search hand off to it. Fixes X2, X5.

**Phase 4 — One queue in one currency (medium).**
Every engine expresses its recommendation as estimated monthly clicks. Gaps, cannibalization,
drops, decaying articles and link fixes enter the queue as item rows. Items release on the
impact verdict; pages that declined come back with a "review or revert" row. Fixes D8–D10,
X3, X6, X7, X11.

**Phase 5 — Aim at what limits this store (large; the real payoff).**
- A page-two plan: queries at positions 4–20, grouped by the page that should win them,
  collections first.
- Blog management: refresh, merge or remove the half that earns nothing; link the dozen posts
  that do earn traffic into the collections they should feed.
- Measure what is unmeasured: index coverage (Search Console URL Inspection API), Core Web
  Vitals (PageSpeed/CrUX API), backlinks (Semrush export), device split.
- Near-duplicate detection for product descriptions and generated articles.

**Phase 6 — Make it legible (medium).**
Task-based navigation, merged overlapping screens (H8), one explainer per page, one verb set,
a weekly plan and a monthly "what changed and what it earned" report, store-level goals.
Fixes H1–H4, H6–H7, X4, X10.

---

## 8. Sound as built — do not rework

The keyword resolver and ownership ledger; variant-key identity; the store-calibrated CTR
curve; the post-generation validator and output guards; targeted fixes being single-use; the
intro-link splice pattern (the model for Phase 1); the Keyword Actions collection builder (the
only creator that registers with the ledger — the model for Phase 3); single-sourced product
and collection schema; llms.txt treated as informational; no hardcoded models.

## 9. Open questions before coding

1. **Tobacco eligibility:** confirm whether Google Merchant listings and Shopify's AI shopping
   channels accept your catalogue. It decides whether feed readiness is worth keeping.
2. **Publishing volume:** are you willing to cap unattended publishing and add a review step?
   This trades speed for safety against the spam updates.
3. **The dead half of the blog:** refresh, merge or remove? Removal is the strongest signal
   but is not reversible in Google's index quickly.
4. **Why issues rose 54 → 145** in September with no product work — needs a short
   investigation before the Issues tab is trusted as a queue source.

## Sources

- Google Search Central, "AI features and your website":
  https://developers.google.com/search/docs/appearance/ai-features
- Search Engine Journal on Google's May 2026 AI search guide:
  https://www.searchenginejournal.com/googles-new-ai-search-guide-calls-aeo-and-geo-still-seo/575026/
- FAQ rich results retirement, May 2026:
  https://www.techwyse.com/news/ai-search/google-faq-rich-results-deprecated-2026
- September 2026 spam update:
  https://launchcodex.com/blog/seo-geo-ai/google-september-2026-spam-update/
- 2026 update timeline:
  https://www.found.co.uk/blog/seo-landscape-and-google-algorithm-updates/
- Shopify agentic commerce overview:
  https://ecommercefastlane.com/agentic-commerce-for-shopify-2026/
