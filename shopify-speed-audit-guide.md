# Shopify Store Speed Audit with Claude Code — Complete Prompt Engineering Guide

## Overview

This guide provides everything you need to prompt Claude Code to perform an automated, comprehensive speed and performance audit of a Shopify store using the Admin API, external testing tools, and agentic auto-fix capabilities.

---

## Architecture: What You Need

### 1. MCP Servers to Connect

You'll want Claude Code connected to **two** MCP servers:

| MCP Server | Purpose | Install Command |
|---|---|---|
| **Shopify Admin API MCP** (`shopify-mcp`) | Read theme files, apps, scripts, products, metafields via GraphQL | `claude mcp add shopify -- npx shopify-mcp --accessToken YOUR_TOKEN --domain your-store.myshopify.com` |
| **Shopify Dev MCP** (`@shopify/dev-mcp`) | Access Shopify docs, API schemas, best practices | `claude mcp add shopify-dev -- npx -y @shopify/dev-mcp@latest` |

**Optional but recommended:**
- **Playwright MCP** — for browser automation (run Lighthouse programmatically, screenshot pages)
- **File System MCP** — for writing reports and fix scripts locally

### 2. Shopify Custom App Scopes Required

Create a Custom App in your Shopify Admin (`Settings > Apps and Sales Channels > Develop Apps`) with these **Admin API scopes**:

```
read_themes           — Access theme Liquid, JSON, CSS, JS files
write_themes          — Apply fixes to theme files (for auto-fix mode)
read_products         — Analyze product images, variants, metafields
read_script_tags      — Identify injected third-party scripts
read_apps             — List installed apps
read_online_store_pages — Access pages content
read_content          — Access blog/article content
```

### 3. External Tools Claude Code Will Use

Claude Code can install and run these via its terminal:

| Tool | Purpose |
|---|---|
| **Google Lighthouse CLI** (`npm i -g lighthouse`) | Full performance, accessibility, SEO audits |
| **PageSpeed Insights API** | Real-user CrUX data + lab data |
| **curl / httpie** | Measure TTFB, check headers, CDN responses |
| **Shopify Theme Check** (`npm i -g @shopify/theme-check`) | Lint Liquid/JSON for performance anti-patterns |
| **ImageMagick / sharp** | Analyze image dimensions, formats, file sizes |

---

## The CLAUDE.md File

Place this in your project root. Claude Code reads it at the start of every session.

```markdown
# CLAUDE.md — Shopify Store Speed Audit Agent

## Project Context
This project audits and optimizes the speed/performance of a Shopify store.
Store: [YOUR-STORE].myshopify.com
Admin API Token: (configured via MCP server)

## Role
You are a senior Shopify performance engineer. Your job is to:
1. Perform a comprehensive speed audit of the storefront
2. Identify every performance weakness with specifics
3. Rank issues by impact (Critical / High / Medium / Low)
4. Provide exact fix instructions for each issue
5. When asked, automatically apply fixes

## Audit Methodology (follow this order)

### Phase 1: External Performance Testing
- Run Lighthouse CLI against homepage, a product page, and a collection page (mobile profile)
- Capture: LCP, FID/INP, CLS, TBT, FCP, Speed Index, TTFB
- Use PageSpeed Insights API for CrUX real-user data if available
- Test with: `lighthouse URL --output=json --chrome-flags="--headless" --preset=perf`

### Phase 2: Theme Code Analysis (via Admin API)
- Pull all theme assets via GraphQL (themes > assets)
- Analyze theme.liquid / layout files for:
  - Render-blocking scripts in <head>
  - Inline CSS/JS bloat
  - Missing defer/async on script tags
  - Excessive Liquid loops/iterations
  - Unused CSS (compare selectors to template usage)
- Check section files for:
  - Unoptimized image rendering (missing width/height, no lazy loading)
  - Large DOM from complex sections
  - Missing preload hints for hero images

### Phase 3: Third-Party Script Audit
- List all script_tags via Admin API
- Identify all external JS loaded in theme.liquid and layout files
- Categorize by: analytics, marketing, chat, reviews, social, other
- Measure estimated blocking time per script
- Flag: scripts without async/defer, scripts from slow CDNs, duplicate libraries

### Phase 4: App Impact Analysis
- List installed apps
- Cross-reference with injected ScriptTags
- Identify apps that inject storefront code
- Estimate each app's performance cost
- Flag apps with known performance issues

### Phase 5: Asset Optimization Check
- Catalog all images (product images, theme assets, section images)
- Check: format (WebP vs PNG/JPG), dimensions vs display size, file sizes
- Check: CSS files (size, minification, unused rules)
- Check: JS files (size, minification, tree-shaking potential)
- Check: Font loading strategy (preload, font-display, number of font files)

### Phase 6: Shopify Platform Checks
- Verify CDN headers (cache-control, Shopify CDN URLs)
- Check HTTP/2 usage
- Verify image CDN transformations are being used
- Check for redirect chains
- Verify preconnect/dns-prefetch hints for third-party origins

## Output Format
For each issue found, report:
```
### [SEVERITY] Issue Title
- **What**: Clear description of the problem
- **Where**: Exact file(s) and line(s) affected
- **Impact**: Which Core Web Vital(s) affected and estimated improvement
- **Fix**: Step-by-step instructions to resolve
- **Auto-fixable**: Yes/No (if Yes, describe what the agent would change)
```

## Auto-Fix Rules
When in auto-fix mode:
- ALWAYS back up the original file before modifying
- Make minimal, targeted changes
- Validate Liquid syntax after every change using Theme Check
- Test that the fix doesn't break functionality
- Log every change made with before/after
- NEVER modify checkout or payment-related code
- NEVER delete apps — only recommend removal

## Key Shopify Performance Facts
- Shopify uses Cloudflare CDN globally — no need to add external CDN
- Shopify auto-serves WebP when browser supports it via image CDN
- Shopify auto-minifies JS via the asset pipeline
- theme.liquid is rendered on every page — bloat here is catastrophic
- Apps often leave behind code even after uninstall — check for orphaned snippets
- Shopify's speed score formula: [(product * 31) + (collection * 33) + (home * 13)] / 77
- Target: Lighthouse mobile >= 60 (Theme Store minimum), ideally 70+

## GraphQL Queries to Use

### Get all theme assets:
```graphql
{
  themes(first: 5) {
    edges {
      node {
        id
        name
        role
        files(first: 250, filenames: ["*.liquid", "*.json", "*.js", "*.css"]) {
          edges {
            node {
              filename
              size
              contentType
              body {
                ... on OnlineStoreThemeFileBodyText {
                  content
                }
              }
            }
          }
        }
      }
    }
  }
}
```

### Get script tags:
```graphql
{
  scriptTags(first: 50) {
    edges {
      node {
        id
        src
        displayScope
        createdAt
      }
    }
  }
}
```

### Get product images for optimization check:
```graphql
{
  products(first: 50) {
    edges {
      node {
        title
        images(first: 10) {
          edges {
            node {
              url
              width
              height
              altText
            }
          }
        }
      }
    }
  }
}
```
```

---

## The Master Prompt

Copy-paste this into Claude Code to kick off the audit:

```
I need you to perform a comprehensive speed and performance audit of my Shopify store.

**Store URL**: https://tobacco-nation.myshopify.com
**Store public URL**: https://a2ztobacco.com

You have Admin API access via the Shopify MCP server. Here's what I need:

## Step 1: Baseline Performance Test
Run Google Lighthouse against these three pages (mobile, simulated throttling):
- Homepage
- A product page (pick the one with the most content)
- A collection page (pick the largest collection)

Record all Core Web Vitals (LCP, INP, CLS) plus supporting metrics (FCP, TBT, Speed Index, TTFB).

## Step 2: Deep Theme Analysis
Using the Admin API, pull the live theme's files and analyze:
- All render-blocking resources in the <head>
- JavaScript bundle sizes and loading strategies
- CSS file sizes and whether there's unused CSS
- Liquid template complexity (nested loops, excessive logic)
- Image handling (lazy loading, explicit dimensions, proper srcset)
- Font loading strategy
- Preload/preconnect hints

## Step 3: Third-Party Script Audit
- Pull all ScriptTags via the API
- Scan theme.liquid and layout files for external script URLs
- Categorize each script (analytics, marketing, chat widget, reviews, etc.)
- Estimate the blocking impact of each
- Identify duplicate libraries (e.g., multiple jQuery versions)

## Step 4: App Performance Impact
- List all installed apps
- Identify which ones inject storefront code
- Cross-reference with ScriptTags and theme snippets
- Rate each app's likely performance impact

## Step 5: Asset Optimization Review
- Check product image sizes, formats, and whether they use Shopify's image CDN transforms
- Review theme image assets for oversized files
- Check for unminified CSS/JS

## Step 6: Generate Report
Create a structured report with:
- Executive summary with overall health score
- Each issue categorized as Critical / High / Medium / Low
- Specific file names and line numbers where applicable
- Estimated performance improvement for each fix
- Whether the fix can be auto-applied by you

## Step 7: Fix Mode (ask me first)
After presenting the report, ask me which issues I want you to auto-fix.
For each fix:
1. Show me exactly what will change (diff preview)
2. Wait for my approval
3. Apply the fix via the Admin API
4. Re-run the affected test to verify improvement

Be thorough. I want to find EVERY performance issue, not just the obvious ones.
Start with Step 1 now.
```

---

## Advanced: Fully Autonomous Mode Prompt

If you want Claude Code to run the entire audit AND fix everything without asking, use this variant:

```
Run a complete Shopify speed audit on my store ([YOUR-STORE].myshopify.com)
using Admin API access. Follow these rules:

AUDIT: Run Lighthouse on homepage, product, and collection pages. Pull all theme
files. Analyze scripts, apps, images, fonts, and CSS. Check for every known
Shopify performance anti-pattern.

FIX AUTOMATICALLY:
- Add defer/async to non-critical scripts
- Add loading="lazy" to below-fold images
- Add explicit width/height to images missing them
- Add preconnect hints for third-party domains
- Remove orphaned app snippets (code from uninstalled apps)
- Optimize font-display settings to "swap"
- Add fetchpriority="high" to hero/LCP images
- Move non-critical CSS to be loaded asynchronously

DO NOT AUTO-FIX (recommend only):
- App removal
- Theme replacement
- Anything involving checkout
- Structural Liquid refactoring

For each change: back up the original, apply the fix, validate with Theme Check,
and log the before/after. Generate a final report showing all changes made and
estimated impact.
```

---

## Sub-Agent Strategy (for Complex Stores)

For large stores, split the work across Claude Code sub-agents:

```
/agents/lighthouse-tester.md    — Runs Lighthouse tests, collects metrics
/agents/theme-analyzer.md      — Analyzes Liquid, CSS, JS via Admin API
/agents/script-auditor.md      — Catalogs and evaluates third-party scripts
/agents/image-optimizer.md     — Checks all image assets across the store
/agents/report-generator.md    — Synthesizes all findings into the final report
```

Each agent gets its own context window, preventing context exhaustion on large themes with hundreds of files.

---

## Common Issues This Audit Will Catch

| Issue | CWV Impact | Typical Improvement |
|---|---|---|
| Render-blocking JS in `<head>` | LCP, TBT | 10-30% LCP improvement |
| Unoptimized hero image (no preload, wrong format) | LCP | 0.5-2s LCP improvement |
| Too many third-party scripts | TBT, INP | 20-50% TBT reduction |
| No lazy loading on below-fold images | LCP, Speed Index | 15-25% Speed Index improvement |
| Orphaned app code after uninstall | TBT, file size | Variable (sometimes dramatic) |
| Excessive Liquid loops (e.g., mega menus) | TTFB, LCP | 100-500ms TTFB improvement |
| Font files without font-display: swap | LCP, FCP | 0.2-1s FCP improvement |
| Missing preconnect for 3P origins | LCP, FCP | 100-300ms per origin |
| Oversized product images | LCP, Speed Index | 20-40% bandwidth reduction |
| Unused CSS loaded globally | FCP, LCP | 5-15% FCP improvement |
| Duplicate JavaScript libraries | TBT, file size | Significant parse-time savings |
| Missing image dimensions (width/height) | CLS | Direct CLS fix |
| Layout shifts from dynamically injected content | CLS | Direct CLS fix |

---

## Tips for Best Results

1. **Start with `@shopify/dev-mcp`**: Have Claude Code call the `introspect` tool first so it has up-to-date Shopify API knowledge.

2. **Use `--dangerously-skip-permissions` carefully**: For fully autonomous mode, Claude Code needs this flag to run without approval prompts. Only use on stores where you have full control.

3. **Run in a project directory**: Create a dedicated folder so Claude Code can write reports, logs, and backups to disk.

4. **Pin the API version**: Tell Claude Code to use a specific Shopify API version (e.g., `2026-01`) to avoid deprecation surprises.

5. **Test on a development theme first**: For auto-fixes, have Claude Code target an unpublished theme copy, not the live theme. Promote it only after validation.

6. **Iterate**: Speed optimization is not one-and-done. Run the audit monthly, especially after installing new apps or making theme changes.
