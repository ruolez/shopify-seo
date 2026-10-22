# Search Console pull — a2ztobacco.com — 2026-08-25

| File | What |
|---|---|
| [gsc-performance-summary.md](gsc-performance-summary.md) | (a) 18–25 Aug vs 10–17 Aug: totals, by section, daily with /blogs/ split, device, country, biggest page/query movers |
| [gsc-performance-by-page.md](gsc-performance-by-page.md) | (a) full by-page export, both windows side by side |
| [gsc-performance-by-query.md](gsc-performance-by-query.md) | (a) full by-query export, both windows side by side |
| [gsc-manual-actions-security.md](gsc-manual-actions-security.md) | (b) Manual Actions / Security Issues — **not API-readable; needs UI check** (links inside) |
| [gsc-page-indexing.md](gsc-page-indexing.md) | (c) URL Inspection sweep of all 1,187 sitemap URLs: coverage totals, by section, sitemap status, full "Crawled – currently not indexed" list |

Source: app's GSC OAuth connection (store 2, scope `webmasters.readonly`), Search Analytics API with `dataState=all`, Sitemaps API, URL Inspection API. Raw JSON kept in the session scratchpad only.

## Headline
- **No site-wide loss in this window.** Clicks 149 vs 141 (+6%), impressions 24.3k vs 24.0k (+2%), avg position 23.7 vs 25.4 (better).
- **Not concentrated in /blogs/ — the opposite.** /blogs/ +41% clicks / +27% impressions. Softness is in /products/ (−22% clicks, −16% impr) and /collections/ (−35% clicks, −9% impr), on small absolute numbers (8 clicks each).
- **Manual action / security: unverified** — API can't read those reports; two links to check.
- **Indexing:** 923 / 1,187 sitemap URLs indexed. 194 "Crawled – currently not indexed" (138 products, 44 collections, 10 blogs) — but these had only 58 impressions and 1 click in 10–17 Aug, so they are not where any loss lives. 39 URLs unknown to Google (new), 27 discovered-not-crawled (23 blogs), 3 collection URLs in the sitemap 404, 1 page soft-404.
