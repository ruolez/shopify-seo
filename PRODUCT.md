# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One expert user: the owner of a Shopify store (A2Z Tobacco: cigars, cigarillos, pipe tobacco, wraps, nicotine pouches) who runs the whole SEO operation alone, at a desk, several times a week. Technical (builds and runs the app in Docker), fluent in SEO vocabulary, comfortable with dense screens, keyboard shortcuts and a command palette. No team, no onboarding audience.

## Product Purpose

A private SEO control center for a Shopify store. It mirrors the store (products, collections, articles, pages), scores every page, pulls Google Search Console and Semrush data, and uses AI (via OpenRouter) to propose and apply fixes: titles, metas, descriptions, alt text, internal links, schema, blog and news posts. Success is more organic clicks and revenue, with every change measurable and reversible.

## Positioning

It closes the loop on one real store: the same tool that finds the problem (GSC, Semrush, competitors, AI search visibility) writes the fix to Shopify through one guarded pipeline (refuses unsafe rewrites, records before/after, can undo) and then measures the result. Generic SEO suites report; this one acts and remembers.

## Operating Context

- Work arrives as a ranked queue ("What to do next", ranked by estimated monthly clicks) and as per-area backlogs (issues, internal links, duplicates, missing images, content ideas).
- Content production is a calendar: blog posts and Product News share one schedule and one approval gate; nothing publishes without approval.
- Research surfaces: Search Console, keyword families and ownership, competitors, Semrush CSV imports, AI Search readiness and visibility.
- Long-running AI jobs run in the background and report in a shared Activity drawer.
- Multi-store capable, in practice one store.

## Capabilities and Constraints

- Flask + Jinja templates + vanilla JS, no build step; one shared stylesheet (`app/static/css/style.css`), shell in `base.html` / `_sidebar.html`, shared components in `app/static/js/ui.js` and `app.js`.
- Light and dark themes; localStorage keys and element ids listed in the UI redesign memory must not be renamed.
- Every content write goes through `apply_pipeline.apply_item_changes`; the UI never bypasses it.
- Scores: on-page `seo_score` is the headline; search performance shown beside it; one colour scale 80 / 50.

## Brand Commitments

None binding. The current teal look is explicitly up for replacement (Nov 2026 redesign).

## Evidence on Hand

Real store data in the local database: ~758 products, ~205 collections, ~316 articles, GSC since March 2026, Semrush exports, competitor checks. No testimonials or marketing material; none needed.

## Product Principles

1. Every number leads somewhere: a metric, badge or row links to the place where you act on it.
2. Act first, read second: each screen opens on what to do, with the explanation one step away.
3. Nothing is hidden: every area of the app is reachable in one click from anywhere.
4. Safe by default: writes are previewed, guarded and undoable, and the UI says so.
5. One vocabulary: the same control looks and behaves the same on every page.
