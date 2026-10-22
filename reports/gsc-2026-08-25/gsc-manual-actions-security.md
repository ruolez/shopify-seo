# GSC Manual Actions & Security Issues — a2ztobacco.com (checked 2026-08-25)

Property: `https://a2ztobacco.com/`

## Status: ⚠️ NOT VERIFIED — needs a 10-second look in the GSC UI

The Search Console API does not expose the Manual Actions or Security Issues reports (only Search Analytics, Sitemaps, Sites and URL Inspection). The app's OAuth scope (`webmasters.readonly`) therefore cannot read them, and the Claude-in-Chrome extension was not connected during this run, so the UI could not be read on your behalf.

Open these two links (one look each rules a penalty in or out):

- Manual actions: https://search.google.com/search-console/manual-actions?resource_id=https%3A%2F%2Fa2ztobacco.com%2F
- Security issues: https://search.google.com/search-console/security-issues?resource_id=https%3A%2F%2Fa2ztobacco.com%2F

Expected "clean" state: both pages say **"No issues detected"**.

## Indirect evidence from the API (does not replace the UI check)

| Signal | Observation | Read |
|---|---|---|
| Site-wide clicks 18–25 Aug vs 10–17 Aug | 149 vs 141 (+6%), impressions 24,339 vs 23,963 (+2%) | No site-wide collapse — a site-wide manual action or a security/hacked flag normally produces a sharp, broad drop |
| Home page URL Inspection | verdict PASS, "Submitted and indexed", crawled 2026-08-25 05:51 UTC, `crawledAs: MOBILE` | Home still crawlable and indexed |
| Loss concentrated where? | `/products/` −22% clicks / −16% impressions, `/collections/` −35% / −9%; `/blogs/` **+41% / +27%** | Not concentrated in `/blogs/`; the softness is in commerce pages, blogs are growing |

Also: Google emails property owners/users when a manual action or security issue is issued ("Search Console: manual action…" / "Security issues detected on…"). Absence of such an email is a weak but useful second signal.

## To make this automatic next time

Nothing in the API will surface these reports; the only automation paths are (1) a logged-in browser session reading the two UI pages, or (2) a Gmail filter on Search Console alert emails. If you want, connect the Chrome extension and re-run — the check takes one navigation per page.
