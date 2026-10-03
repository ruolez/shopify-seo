"""AI SEO section — GEO/AEO/LLMO readiness scoring and (later) optimization.

Phase 1: deterministic AI-visibility scoring of synced products/collections,
enriched by a live storefront crawl (schema, robots.txt, llms.txt) as seen by
AI crawlers. Reuses serp_scraper's page fetcher, the ai_tasks daemon pattern,
and the analyzer in services/ai_seo_analyzer.py.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

import requests
from itertools import islice
from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services.task_runner import spawn as spawn_task, sweep_stale_jobs
from app.services import ai_seo_analyzer
from app.services import indexnow_service
from app.services import url_utils
from app.services.serp_scraper import scrape_serp_pages
from app.shopify_client import ShopifyClient

COMPONENT_LABELS = {
    "structured_data": "Structured data",
    "extractability": "Extractability",
    "fact_density": "Fact density",
    "crawlability": "AI crawlability",
    "entity_authority": "Entity authority",
    "freshness": "Freshness",
}

logger = logging.getLogger(__name__)

ai_seo_bp = Blueprint("ai_seo", __name__)

# Crawl in bounded batches so every item is fetched without a single huge,
# heavily-staggered concurrent burst. All items get crawled — no silent cap.
CRAWL_BATCH_SIZE = 100


def _store_base_url(store):
    return url_utils.store_base_url(store)


def _item_url(base_url, resource_type, handle):
    if not base_url or not handle:
        return None
    path = "collections" if resource_type == "collection" else "products"
    return f"{base_url}/{path}/{handle}"


def _fetch_text(url, timeout=15):
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def _fetch_store_crawlability(base_url):
    """Return (robots_txt, llms_txt_present) for the store domain."""
    if not base_url:
        return None, False
    robots = _fetch_text(f"{base_url}/robots.txt")
    llms = _fetch_text(f"{base_url}/llms.txt")
    return robots, bool(llms and llms.strip())


# Live bot-access verification: fetch with real AI-crawler user agents to detect
# WAF/CDN blocking that robots.txt can't reveal. Only bots that actually fetch
# pages are tested (Google-Extended is a robots token, not a fetcher).
AI_BOT_CHECK_AGENTS = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ClaudeBot/1.0; +claudebot@anthropic.com",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot",
}
_CHALLENGE_MARKERS = ("just a moment", "cf-chl", "captcha", "attention required")


def _classify_bot_fetch(url, user_agent):
    """Fetch a URL with a bot UA and classify: ok / blocked / challenged / error.

    A full 200 HTML page is ok even if it mentions "captcha" somewhere (Shopify
    themes ship captcha helpers for forms) — challenge markers only distinguish
    challenged from blocked on failing or suspiciously tiny responses.
    """
    import time

    time.sleep(0.5)
    resp = None
    for attempt in range(2):  # a timeout on one heavy page is not a block — retry once
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": user_agent})
            break
        except Exception as e:
            if attempt:
                logger.info("bot fetch %s failed twice (%s)", url, e)
                return "error"
            time.sleep(1.5)
    body = resp.text or ""
    low = body[:20000].lower()
    has_marker = any(m in low for m in _CHALLENGE_MARKERS)
    if resp.status_code in (403, 429, 503):
        return "challenged" if has_marker else "blocked"
    if resp.status_code != 200:
        return "blocked"
    if len(body) < 5000 or "<html" not in body[:1000].lower():
        return "challenged" if has_marker else "blocked"
    return "ok"


def _run_bot_check(store_id, base_url):
    """Verify AI-crawler access to the homepage + one product page; cache in
    ai_bot_checks. ≤9 sequential requests with 0.5s spacing."""
    if not base_url:
        return None
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle FROM synced_products WHERE store_id = %s AND handle IS NOT NULL ORDER BY id LIMIT 1",
            (store_id,),
        )
        row = cur.fetchone()
    urls = {"home": base_url}
    if row and row.get("handle"):
        urls["product"] = f"{base_url}/products/{row['handle']}"

    control = _classify_bot_fetch(base_url, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    results = {"bots": {}, "control_ok": control == "ok"}
    for bot, ua in AI_BOT_CHECK_AGENTS.items():
        results["bots"][bot] = {label: _classify_bot_fetch(u, ua) for label, u in urls.items()}
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_bot_checks (store_id, results, checked_at) VALUES (%s, %s, NOW())
            ON CONFLICT (store_id) DO UPDATE SET results = EXCLUDED.results, checked_at = NOW()
            """,
            (store_id, json.dumps(results)),
        )
    return results


def _cached_bot_check(store_id, max_age_hours=None):
    """Stored bot-check results, optionally only if fresher than max_age_hours."""
    with db.get_cursor() as cur:
        cur.execute("SELECT results, checked_at FROM ai_bot_checks WHERE store_id = %s", (store_id,))
        row = cur.fetchone()
    if not row:
        return None, None
    if max_age_hours is not None and row["checked_at"]:
        from datetime import timedelta

        if row["checked_at"] < datetime.now() - timedelta(hours=max_age_hours):
            return None, row["checked_at"]
    return row["results"], row["checked_at"]


def _crawl_batch(urls):
    """Concurrently fetch one bounded batch of pages; return {url: metadata}."""
    urls = [u for u in urls if u]
    if not urls:
        return {}
    try:
        pages = asyncio.run(scrape_serp_pages(urls, max_pages=len(urls)))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            pages = loop.run_until_complete(scrape_serp_pages(urls, max_pages=len(urls)))
        finally:
            loop.close()
    return {p["url"]: p for p in pages if p and p.get("url")}


def _load_items(store_id, resource_type):
    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE store_id = %s", (store_id,))
        return cur.fetchall()


def _pushed_schema(store_id):
    """Map shopify resource_id -> list of pushed schema_markup rows (with JSON for richness)."""
    out = {}
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT resource_id, schema_type, schema_json FROM schema_markup WHERE store_id = %s",
            (store_id,),
        )
        for row in cur.fetchall():
            out.setdefault(str(row["resource_id"]), []).append(row)
    return out


def _site_schema_asset(store_id):
    """Published site-schema snippet state.

    Returns {"published", "types", "renders_item_schema", "snippet_sha"}.
    Backward compat: assets published before per-item rendering stored a bare
    JSON list of types — those don't render metafield schema.
    """
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT content, status FROM aeo_assets WHERE store_id = %s AND asset_type = 'site_schema'",
            (store_id,),
        )
        row = cur.fetchone()
    out = {"published": False, "types": [], "renders_item_schema": False, "snippet_sha": None}
    if not row or row.get("status") != "published" or not row.get("content"):
        return out
    out["published"] = True
    try:
        content = json.loads(row["content"])
    except (json.JSONDecodeError, ValueError, TypeError):
        return out
    if isinstance(content, list):  # old format
        out["types"] = content
    elif isinstance(content, dict):
        out["types"] = content.get("types") or []
        out["renders_item_schema"] = bool(content.get("renders_item_schema"))
        out["snippet_sha"] = content.get("snippet_sha")
    return out


def _published_site_schema_types(store_id):
    return _site_schema_asset(store_id)["types"]


def _credited_markup(markup, site_asset, live):
    """Pushed metafield schema only counts toward the score when the published
    snippet actually renders it on the storefront, or the live crawl verified
    the type is present. Prevents crediting invisible schema."""
    if site_asset.get("renders_item_schema"):
        return markup
    live_types = set((live or {}).get("schema_types") or [])
    return [r for r in (markup or []) if r.get("schema_type") in live_types]


def _score_items(store_id, resource_type, items, base_url, crawlability, store_domain):
    """Crawl (in bounded batches) + score every item; upsert into ai_seo_scores."""
    schema_map = _pushed_schema(store_id)
    site_asset = _site_schema_asset(store_id)
    site_types = site_asset["types"]
    scored = 0
    it_iter = iter(items)
    while True:
        batch = list(islice(it_iter, CRAWL_BATCH_SIZE))
        if not batch:
            break
        urls = {it["id"]: _item_url(base_url, resource_type, it.get("handle")) for it in batch}
        crawled = _crawl_batch(list(urls.values()))
        for it in batch:
            it = dict(it)
            it["resource_type"] = resource_type
            live = crawled.get(urls[it["id"]])
            markup = _credited_markup(schema_map.get(str(it.get("shopify_id")), []), site_asset, live)
            result = ai_seo_analyzer.analyze_ai_visibility(
                it,
                live=live,
                schema_markup=markup,
                crawlability=crawlability,
                store_domain=store_domain,
                site_schema_types=site_types,
            )
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai_seo_scores
                        (store_id, resource_type, resource_id, score, breakdown, live_crawl, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, resource_type, resource_id) DO UPDATE SET
                        score = EXCLUDED.score,
                        breakdown = EXCLUDED.breakdown,
                        live_crawl = EXCLUDED.live_crawl,
                        created_at = NOW()
                    """,
                    (
                        store_id,
                        resource_type,
                        it["id"],
                        result["score"],
                        json.dumps(result["breakdown"]),
                        json.dumps(live) if live else None,
                    ),
                )
            scored += 1
    return scored


def _execute_scan(task_id, store_id, resource_type):
    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
            store = cur.fetchone()
        if not store:
            raise ValueError("Store not found")

        base_url = _store_base_url(store)
        robots, llms_present = _fetch_store_crawlability(base_url)
        bot_results, _checked = _cached_bot_check(store_id, max_age_hours=24)
        if bot_results is None:
            try:
                bot_results = _run_bot_check(store_id, base_url)
            except Exception as e:
                logger.warning("Bot access check failed for store %s: %s", store_id, e)
                bot_results = None
        crawlability = ai_seo_analyzer.analyze_store_crawlability(robots, llms_present, bot_results)

        types = [resource_type] if resource_type in ("product", "collection") else ["product", "collection"]
        total = 0
        for rtype in types:
            items = _load_items(store_id, rtype)
            _score_items(store_id, rtype, items, base_url, crawlability, url_utils.store_host(store))
            total += len(items)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps({"scanned": total, "crawlability": crawlability}), task_id),
            )
    except Exception as e:
        logger.exception("AI SEO scan failed: %s", e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@ai_seo_bp.route("/ai-seo")
def ai_seo_page():
    return render_template("ai_seo.html")


@ai_seo_bp.route("/api/ai-seo/scan", methods=["POST"])
def start_scan():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "all")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_tasks WHERE store_id = %s AND task_type = 'ai_seo_scan' AND status IN ('pending','running')",
            (store_id,),
        )
        if cur.fetchone():
            return jsonify({"error": "An AI readiness scan is already running for this store"}), 409
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'ai_seo_scan', %s) RETURNING id",
            (store_id, json.dumps({"type": resource_type})),
        )
        task_id = cur.fetchone()["id"]

    spawn_task("ai_tasks", task_id, _execute_scan, task_id, int(store_id), resource_type)
    return jsonify({"ok": True, "task_id": task_id}), 201


@ai_seo_bp.route("/api/ai-seo/scan/<int:task_id>", methods=["GET"])
def scan_status(task_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(
        {
            "id": task["id"],
            "status": task["status"],
            "result": task["result"],
            "error": task["error"],
        }
    )


_AEO_SETUP_KEYS = ["robots_txt", "site_schema", "indexnow_key", "llms_txt", "a11y"]
_AEO_FOCUS = {"robots_txt": "robots", "site_schema": "site_schema", "indexnow_key": "indexnow"}


def _section_pillars(store_id):
    """Cached pillar summaries — pure DB, no live HTTP or AI calls."""
    pillars = {}
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT ROUND(AVG(score)) AS avg, COUNT(*) AS n, MAX(created_at) AS last FROM ai_seo_scores WHERE store_id = %s",
            (store_id,),
        )
        r = cur.fetchone()
        pillars["readiness"] = {
            "avg_score": int(r["avg"] or 0), "count": r["n"],
            "last_run": r["last"].isoformat() if r["last"] else None,
        }
        cur.execute(
            "SELECT ROUND(AVG(score)) AS avg, COUNT(*) AS n, MAX(created_at) AS last FROM feed_readiness_scores WHERE store_id = %s",
            (store_id,),
        )
        r = cur.fetchone()
        pillars["feed"] = {
            "avg_score": int(r["avg"] or 0), "count": r["n"],
            "last_run": r["last"].isoformat() if r["last"] else None,
        }
        cur.execute(
            "SELECT scorecard, completed_at FROM offsite_runs WHERE store_id = %s AND status = 'completed' ORDER BY id DESC LIMIT 1",
            (store_id,),
        )
        r = cur.fetchone()
        pillars["offsite"] = {
            "scorecard": (r["scorecard"] if r else None) or None,
            "last_run": r["completed_at"].isoformat() if r and r["completed_at"] else None,
        }
        cur.execute(
            """SELECT share_of_voice, summary, status, completed_at FROM ai_tracking_runs
               WHERE store_id = %s AND status IN ('completed','budget_capped') ORDER BY id DESC LIMIT 1""",
            (store_id,),
        )
        r = cur.fetchone()
        summary = (r["summary"] if r else None) or {}
        pillars["visibility"] = {
            "share_of_voice": float(r["share_of_voice"]) if r and r["share_of_voice"] is not None else None,
            "brand_hits": summary.get("brand_hits"),
            "total_answers": summary.get("total_answers"),
            "last_run": r["completed_at"].isoformat() if r and r["completed_at"] else None,
            "last_status": r["status"] if r else None,
        }
        cur.execute("SELECT asset_type, status FROM aeo_assets WHERE store_id = %s", (store_id,))
        statuses = {row["asset_type"]: row["status"] for row in cur.fetchall()}
    bot_results, bot_checked = _cached_bot_check(store_id)
    ok, tested = ai_seo_analyzer.summarize_bot_access(bot_results)
    published = sum(1 for k in _AEO_SETUP_KEYS if statuses.get(k) == "published")
    if tested and ok == tested:
        published += 1
    pillars["aeo"] = {
        "published": published, "total": len(_AEO_SETUP_KEYS) + 1,
        "statuses": statuses,
        "bot_check": {"ok": ok, "tested": tested, "checked_at": bot_checked.isoformat() if bot_checked else None},
    }
    return pillars


def _section_running(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT
                 EXISTS(SELECT 1 FROM ai_tasks WHERE store_id = %(s)s AND task_type = 'ai_seo_scan' AND status IN ('pending','running')) AS scan,
                 EXISTS(SELECT 1 FROM ai_tasks WHERE store_id = %(s)s AND task_type = 'feed_readiness_scan' AND status IN ('pending','running')) AS feed,
                 EXISTS(SELECT 1 FROM offsite_runs WHERE store_id = %(s)s AND status IN ('pending','running')) AS offsite,
                 EXISTS(SELECT 1 FROM ai_tracking_runs WHERE store_id = %(s)s AND status IN ('pending','running')) AS tracking""",
            {"s": store_id},
        )
        r = cur.fetchone()
    return {"scan": r["scan"], "feed": r["feed"], "offsite": r["offsite"], "tracking": r["tracking"]}


def _section_actions(store_id, pillars):
    """The merged 'what to do next' list for the AI Search section — every source
    is cached data; each block is fenced so one failure can't blank the list."""
    high, med, low = [], [], []

    def row(bucket, id_, pillar, priority, icon, title, detail, href=None, action=None):
        bucket.append({
            "id": id_, "pillar": pillar, "priority": priority, "icon": icon,
            "title": title, "detail": detail, "href": href, "action": action,
        })

    try:  # 1. blocked AI crawlers (high)
        bc = pillars["aeo"]["bot_check"]
        if bc["tested"] and bc["ok"] < bc["tested"]:
            n = bc["tested"] - bc["ok"]
            row(high, "aeo:bot_access", "aeo", "high", "gpp_bad",
                f"{n} AI crawler{'s are' if n != 1 else ' is'} being blocked",
                "robots.txt may allow them, but the live fetch fails — answer engines can't read your pages.",
                href="/ai-seo?tab=assets&focus=asset:bot_check")
    except Exception as e:
        logger.warning("overview actions: bot check block failed: %s", e)

    try:  # 2. failed AI batches (high, direct retry)
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT id, kind, failed FROM bulk_tasks
                   WHERE store_id = %s AND kind IN ('geo_optimize','fact_boost')
                     AND status NOT IN ('pending','running') AND (status = 'failed' OR failed > 0)
                     AND created_at > NOW() - INTERVAL '7 days'
                   ORDER BY id DESC LIMIT 2""",
                (store_id,),
            )
            for b in cur.fetchall():
                label = "GEO optimization" if b["kind"] == "geo_optimize" else "fact-density"
                row(high, f"jobs:batch:{b['id']}", "jobs", "high", "error",
                    f"{b['failed'] or 'A'} failed item{'s' if (b['failed'] or 0) != 1 else ''} in a {label} batch",
                    "Retry re-runs only the failed items.",
                    action={"label": "Retry", "endpoint": f"/api/ai-seo/batches/{b['id']}/retry", "method": "POST", "body": {}})
    except Exception as e:
        logger.warning("overview actions: failed batches block failed: %s", e)

    review_covered = None
    try:  # 3+7. feed coverage: review signals (high) + weak components (med)
        if pillars["feed"]["count"]:
            with db.get_cursor() as cur:
                comps = {}
                for c in ("review_signals", "gtin", "images", "product_type", "brand", "description"):
                    cur.execute(
                        f"""SELECT ROUND(AVG((breakdown->'{c}'->>'score')::numeric)) AS avg,
                                   COUNT(*) FILTER (WHERE (breakdown->'{c}'->>'score')::int = 100) AS full_count
                            FROM feed_readiness_scores WHERE store_id = %s""",
                        (store_id,),
                    )
                    comps[c] = cur.fetchone()
            review_covered = int(comps["review_signals"]["full_count"] or 0)
            # avg is NULL when every product's review signal is unverified
            # (no live crawls) — unknown is not a finding.
            if review_covered == 0 and comps["review_signals"]["avg"] is not None:
                row(high, "feed:reviews", "feed", "high", "reviews",
                    "No rating markup on any product",
                    "AI shopping ranks reviewed products higher — if a review app is installed (Stamped, Bizrate), "
                    "confirm its rating metafields are populated; otherwise install one, then re-audit.",
                    href="/ai-seo?tab=feed&focus=eligibility")
            from app.services.feed_readiness_analyzer import UNSYNCED_SIGNALS  # noqa: F401 (import proximity)
            weak = [(c, int(v["avg"] or 0)) for c, v in comps.items()
                    if c != "review_signals" and v["avg"] is not None and int(v["avg"]) < 50][:2]
            for c, avg in weak:
                row(med, f"feed:component:{c}", "feed", "medium", "inventory_2",
                    f"Weak feed data: {c.replace('_', ' ')} averages {avg}/100",
                    "Feed completeness drives AI shopping ranking.",
                    href="/ai-seo?tab=feed")
    except Exception as e:
        logger.warning("overview actions: feed block failed: %s", e)

    try:  # 4. off-site high-priority actions (high, verbatim)
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT actions FROM offsite_runs WHERE store_id = %s AND status = 'completed' ORDER BY id DESC LIMIT 1",
                (store_id,),
            )
            r = cur.fetchone()
        for i, a in enumerate([a for a in ((r["actions"] if r else None) or []) if a.get("priority") == "high"][:3]):
            row(high, f"offsite:action:{i}", "offsite", "high", "public",
                a.get("title") or "Off-site gap",
                a.get("why") or a.get("detail") or "",
                href="/ai-seo?tab=offsite&focus=actions")
    except Exception as e:
        logger.warning("overview actions: offsite block failed: %s", e)

    try:  # 5. zero visibility (high)
        v = pillars["visibility"]
        if (v["total_answers"] or 0) > 0 and (v["brand_hits"] or 0) == 0:
            row(high, "visibility:zero", "visibility", "high", "visibility_off",
                "AI never recommends your store",
                f"0 of {v['total_answers']} buyer answers named you — the gap analysis shows what to change.",
                href="/ai-seo?tab=visibility&sub=actions")
    except Exception as e:
        logger.warning("overview actions: zero-visibility block failed: %s", e)

    try:  # 6. worst readiness items (med)
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT s.resource_type, s.resource_id, s.score, s.breakdown,
                          COALESCE(p.title, c.title) AS title
                   FROM ai_seo_scores s
                   LEFT JOIN synced_products p ON s.resource_type = 'product' AND p.id = s.resource_id
                   LEFT JOIN synced_collections c ON s.resource_type = 'collection' AND c.id = s.resource_id
                   WHERE s.store_id = %s AND s.score < 60
                   ORDER BY s.score ASC LIMIT 4""",
                (store_id,),
            )
            worst = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) AS n FROM ai_seo_scores WHERE store_id = %s AND score < 60", (store_id,),
            )
            below = cur.fetchone()["n"]
        for it in worst[:3]:
            weakest = _weak_signals(it["breakdown"], 1)
            row(med, f"readiness:{it['resource_type']}:{it['resource_id']}", "readiness", "medium", "auto_fix_high",
                f"Optimize “{(it['title'] or 'Untitled')[:60]}” — scores {it['score']}",
                weakest[0] if weakest else "Lowest AI-readiness score in your catalog.",
                href=f"/ai-seo?tab=score&focus=item:{it['resource_type']}:{it['resource_id']}&action=optimize")
        if below > 3:
            row(med, "readiness:more", "readiness", "medium", "checklist",
                f"{below - 3} more items score below 60",
                "Work through them from the AI Readiness tab (bulk optimize supported).",
                href="/ai-seo?tab=score")
    except Exception as e:
        logger.warning("overview actions: readiness block failed: %s", e)

    try:  # 8. gap-analysis recommendations (med) + content ideas (low)
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT gap_analysis FROM ai_tracking_runs
                   WHERE store_id = %s AND gap_analysis IS NOT NULL ORDER BY id DESC LIMIT 1""",
                (store_id,),
            )
            r = cur.fetchone()
        gap = (r["gap_analysis"] if r else None) or {}
        if isinstance(gap, str):
            gap = json.loads(gap)
        for i, imp in enumerate([p for p in gap.get("page_improvements", []) if p.get("priority") == "high"][:2]):
            row(med, f"visibility:gap:{i}", "visibility", "medium", "trending_up",
                imp.get("title") or "Close a competitive gap",
                imp.get("why") or "", href="/ai-seo?tab=visibility&sub=actions")
        for i, idea in enumerate(gap.get("content_ideas", [])[:2]):
            row(low, f"visibility:idea:{i}", "visibility", "low", "post_add",
                f"Content idea: {idea.get('title') or 'buyer guide'}",
                (idea.get("target_query") and f"Targets “{idea['target_query']}”") or idea.get("why") or "",
                href="/ai-seo?tab=visibility&sub=actions")
    except Exception as e:
        logger.warning("overview actions: gap block failed: %s", e)

    try:  # 9. unpublished AEO assets (med) — importance order
        statuses = pillars["aeo"]["statuses"]
        names = {"robots_txt": "robots.txt AI crawler rules", "site_schema": "site-wide Organization schema",
                 "indexnow_key": "IndexNow instant indexing"}
        for key in ("robots_txt", "site_schema", "indexnow_key"):
            if statuses.get(key) != "published":
                row(med, f"aeo:{key}", "aeo", "medium", "rocket_launch",
                    f"Publish {names[key]}",
                    "Preview-first — nothing goes live until you apply it.",
                    href=f"/ai-seo?tab=assets&focus=asset:{_AEO_FOCUS[key]}")
    except Exception as e:
        logger.warning("overview actions: aeo block failed: %s", e)

    try:  # 10. stale / never-run pillars (low, direct run)
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        nudges = [
            ("readiness", "AI readiness scan", "/api/ai-seo/scan", {"type": "all"}),
            ("feed", "feed audit", "/api/ai-seo/feed/scan", {}),
            ("offsite", "off-site audit", "/api/ai-seo/offsite/run", {}),
            ("visibility", "visibility tracking", "/api/ai-seo/tracking/run", {}),
        ]
        for pillar_key, label, endpoint, extra in nudges:
            last = pillars[pillar_key].get("last_run")
            stale = last is None
            if last:
                from datetime import datetime as dt
                parsed = dt.fromisoformat(last)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                stale = parsed < cutoff
            if stale:
                age = "never run" if last is None else "last run over 30 days ago"
                row(low, f"stale:{pillar_key}", pillar_key, "low", "schedule",
                    f"Refresh: {label} {age}",
                    "Data drives the pillar score and this action list.",
                    action={"label": "Run now", "endpoint": endpoint, "method": "POST",
                            "body": {"store_id": store_id, **extra}})
    except Exception as e:
        logger.warning("overview actions: stale block failed: %s", e)

    actions = high + med + low
    return actions[:12], max(0, len(actions) - 12)


@ai_seo_bp.route("/api/ai-seo/overview/actions", methods=["GET"])
def overview_actions():
    """Overview tab payload: pillar summaries + running flags + the merged
    'what to do next' list. Pure DB — safe to poll."""
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    pillars = _section_pillars(store_id)
    actions, more = _section_actions(store_id, pillars)
    return jsonify({
        "pillars": pillars,
        "running": _section_running(store_id),
        "actions": actions,
        "more": more,
    })


@ai_seo_bp.route("/api/ai-seo/overview", methods=["GET"])
def overview():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    store_id = int(store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return jsonify({"error": "Store not found"}), 404

    base_url = _store_base_url(store)
    robots, llms_present = _fetch_store_crawlability(base_url)
    bot_results, _checked = _cached_bot_check(store_id)
    crawl_score, crawl_detail = ai_seo_analyzer.analyze_store_crawlability(robots, llms_present, bot_results)

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT resource_type, COUNT(*) AS n, COALESCE(AVG(score), 0) AS avg_score
            FROM ai_seo_scores WHERE store_id = %s GROUP BY resource_type
            """,
            (store_id,),
        )
        by_type = {r["resource_type"]: {"count": int(r["n"]), "avg_score": round(r["avg_score"])} for r in cur.fetchall()}
        cur.execute(
            "SELECT COUNT(*) AS n, COALESCE(AVG(score), 0) AS avg_score FROM ai_seo_scores WHERE store_id = %s",
            (store_id,),
        )
        totals = cur.fetchone()

    return jsonify(
        {
            "crawlability": {"score": crawl_score, "detail": crawl_detail},
            "llms_txt": llms_present,
            "base_url": base_url,
            "scanned_count": int(totals["n"]),
            "avg_score": round(totals["avg_score"]),
            "by_type": by_type,
        }
    )


@ai_seo_bp.route("/api/ai-seo/items", methods=["GET"])
def list_items():
    store_id = request.args.get("store_id")
    resource_type = request.args.get("type", "product")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    table = "synced_collections" if resource_type == "collection" else "synced_products"

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT s.resource_id, s.score, s.breakdown, s.created_at,
                   i.title, i.handle
            FROM ai_seo_scores s
            JOIN {table} i ON i.id = s.resource_id
            WHERE s.store_id = %s AND s.resource_type = %s
            ORDER BY s.score ASC
            """,
            (int(store_id), resource_type),
        )
        rows = cur.fetchall()

    return jsonify(
        [
            {
                "resource_id": r["resource_id"],
                "title": r["title"],
                "handle": r["handle"],
                "score": r["score"],
                "breakdown": r["breakdown"],
                "scanned_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    )


# ── Feed readiness (agentic-commerce catalog quality) ───────────────────


def _execute_feed_scan(task_id, store_id):
    from app.services import feed_readiness_analyzer

    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE store_id = %s", (store_id,))
            products = [dict(p) for p in cur.fetchall()]
            cur.execute(
                "SELECT resource_id, live_crawl FROM ai_seo_scores WHERE store_id = %s AND resource_type = 'product' AND live_crawl IS NOT NULL",
                (store_id,),
            )
            live_map = {r["resource_id"]: (r["live_crawl"] or {}).get("schema_types") for r in cur.fetchall()}

        scanned = 0
        for p in products:
            result = feed_readiness_analyzer.analyze_feed_readiness(p, live_map.get(p["id"]))
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO feed_readiness_scores (store_id, product_id, score, breakdown, fixables, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, product_id) DO UPDATE SET
                        score = EXCLUDED.score, breakdown = EXCLUDED.breakdown,
                        fixables = EXCLUDED.fixables, created_at = NOW()
                    """,
                    (store_id, p["id"], result["score"], json.dumps(result["breakdown"]), json.dumps(result["fixables"])),
                )
            scanned += 1

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps({"scanned": scanned}), task_id),
            )
    except Exception as e:
        logger.exception("Feed readiness scan failed: %s", e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@ai_seo_bp.route("/api/ai-seo/feed/scan", methods=["POST"])
def feed_scan():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_tasks WHERE store_id = %s AND task_type = 'feed_readiness_scan' AND status IN ('pending','running')",
            (int(store_id),),
        )
        if cur.fetchone():
            return jsonify({"error": "A feed audit is already running for this store"}), 409
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'feed_readiness_scan', '{}') RETURNING id",
            (int(store_id),),
        )
        task_id = cur.fetchone()["id"]
    spawn_task("ai_tasks", task_id, _execute_feed_scan, task_id, int(store_id))
    return jsonify({"ok": True, "task_id": task_id}), 201


@ai_seo_bp.route("/api/ai-seo/feed/overview", methods=["GET"])
def feed_overview():
    from app.services.feed_readiness_analyzer import ELIGIBILITY_CHECKLIST, UNSYNCED_SIGNALS

    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    store_id = int(store_id)
    components = ["gtin", "sku", "images", "product_type", "title", "description", "price", "availability", "brand", "review_signals"]
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n, COALESCE(AVG(score), 0) AS avg_score FROM feed_readiness_scores WHERE store_id = %s",
            (store_id,),
        )
        totals = cur.fetchone()
        coverage = {}
        for c in components:
            cur.execute(
                f"""SELECT ROUND(AVG((breakdown->'{c}'->>'score')::numeric)) AS avg,
                           COUNT(*) FILTER (WHERE (breakdown->'{c}'->>'score')::int = 100) AS full_count
                    FROM feed_readiness_scores WHERE store_id = %s""",
                (store_id,),
            )
            row = cur.fetchone()
            coverage[c] = {
                "avg": int(row["avg"]) if row and row["avg"] is not None else None,
                "full_count": int(row["full_count"] or 0) if row else 0,
            }
    return jsonify(
        {
            "scanned_count": int(totals["n"]),
            "avg_score": round(totals["avg_score"]),
            "coverage": coverage,
            "unsynced": UNSYNCED_SIGNALS,
            "checklist": ELIGIBILITY_CHECKLIST,
        }
    )


@ai_seo_bp.route("/api/ai-seo/feed/items", methods=["GET"])
def feed_items():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT f.product_id AS resource_id, f.score, f.breakdown, f.fixables, f.created_at,
                   p.title, p.handle
            FROM feed_readiness_scores f
            JOIN synced_products p ON p.id = f.product_id
            WHERE f.store_id = %s
            ORDER BY f.score ASC
            """,
            (int(store_id),),
        )
        rows = cur.fetchall()
    return jsonify(
        [
            {
                "resource_id": r["resource_id"],
                "title": r["title"],
                "handle": r["handle"],
                "score": r["score"],
                "breakdown": r["breakdown"],
                "fixables": r["fixables"],
                "scanned_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    )


# ── Phase 2: review-first GEO optimization ──────────────────────────────


def _weak_signals(breakdown, n=3):
    """Return the n lowest-scoring components as 'Label: detail' strings."""
    if not breakdown:
        return []
    ranked = sorted(
        ((k, d) for k, d in breakdown.items() if k in COMPONENT_LABELS),
        key=lambda kv: kv[1].get("score", 0),
    )
    return [f"{COMPONENT_LABELS[k]}: {d.get('detail', '')}" for k, d in ranked[:n]]


def _crawlability_from_breakdown(breakdown):
    """Reuse the crawlability sub-score captured during the last scan."""
    crawl = (breakdown or {}).get("crawlability")
    return (crawl["score"], crawl["detail"]) if crawl else None


def _stored_score(store_id, resource_type, resource_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT score, breakdown, live_crawl FROM ai_seo_scores WHERE store_id = %s AND resource_type = %s AND resource_id = %s",
            (store_id, resource_type, resource_id),
        )
        return cur.fetchone()


def _rescore_item(store_id, resource_type, item, live, crawlability, store_domain):
    site_asset = _site_schema_asset(store_id)
    markup = _credited_markup(
        _pushed_schema(store_id).get(str(item.get("shopify_id")), []), site_asset, live
    )
    result = ai_seo_analyzer.analyze_ai_visibility(
        {**item, "resource_type": resource_type},
        live=live,
        schema_markup=markup,
        crawlability=crawlability,
        store_domain=store_domain,
        site_schema_types=site_asset["types"],
    )
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_seo_scores (store_id, resource_type, resource_id, score, breakdown, live_crawl, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, resource_type, resource_id) DO UPDATE SET
                score = EXCLUDED.score, breakdown = EXCLUDED.breakdown, created_at = NOW()
            """,
            (
                store_id,
                resource_type,
                item["id"],
                result["score"],
                json.dumps(result["breakdown"]),
                json.dumps(live) if live else None,
            ),
        )
    return result


@ai_seo_bp.route("/api/ai-seo/optimize/preview", methods=["POST"])
def optimize_preview():
    from app.ai_agent import run_task

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    resource_id = data.get("id")
    if not store_id or not resource_id:
        return jsonify({"error": "store_id and id are required"}), 400

    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
        cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (resource_id, int(store_id)))
        item = cur.fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404
    item = dict(item)
    store_domain = url_utils.store_host(dict(store)) if store else None

    stored = _stored_score(int(store_id), resource_type, resource_id)
    before_breakdown = stored["breakdown"] if stored else None
    if before_breakdown is None:
        before = ai_seo_analyzer.analyze_ai_visibility({**item, "resource_type": resource_type}, store_domain=store_domain)
        before_breakdown, before_score = before["breakdown"], before["score"]
    else:
        before_score = stored["score"]

    task_type = "ai_seo_collection" if resource_type == "collection" else "ai_seo_product"
    id_key = "collection_id" if resource_type == "collection" else "product_id"
    geo = run_task(
        task_type,
        {id_key: resource_id, "weak_signals": _weak_signals(before_breakdown)},
        store_id,
    )
    if geo.get("error"):
        return jsonify({"error": geo["error"]}), 502

    # Honest projection: reuse the real crawl schema, and only credit the schema
    # we'd push when the published theme snippet actually renders it — pushed
    # metafields are invisible to crawlers otherwise.
    live_after = dict((stored or {}).get("live_crawl") or {}) if stored else {}
    schema_after = list(live_after.get("schema_types") or [])
    if _site_schema_asset(int(store_id))["renders_item_schema"]:
        if geo.get("faqpage_jsonld") and "FAQPage" not in schema_after:
            schema_after.append("FAQPage")
        pushed_type = "CollectionPage" if resource_type == "collection" else "Product"
        if pushed_type not in schema_after:
            schema_after.append(pushed_type)
    live_after["schema_types"] = schema_after
    # Crawlability is store-level (robots.txt/llms.txt) — carry it over unchanged so
    # content optimization never shows a spurious crawlability delta.
    after = ai_seo_analyzer.analyze_ai_visibility(
        {**item, "description": geo["optimized_description_html"], "resource_type": resource_type},
        live=live_after if schema_after else None,
        crawlability=_crawlability_from_breakdown(before_breakdown),
        store_domain=store_domain,
    )

    return jsonify(
        {
            "preview": geo,
            "item": {"id": item["id"], "shopify_id": item.get("shopify_id"), "title": item.get("title")},
            "before_score": before_score,
            "before_breakdown": before_breakdown,
            "after_score": after["score"],
            "after_breakdown": after["breakdown"],
        }
    )


def _push_and_rescore(store, resource_type, item, description_html, meta_title, meta_description, faqpage, client,
                      allow_dropped_primary=False):
    """Push GEO content to Shopify, persist locally, and rescore the item.

    Shared by single-item apply and batch optimize. Raises on Shopify errors,
    and refuses (ValueError) a rewrite that drops the item's resolved primary
    keyword — the preview's dropped_keywords list is informational, this is
    the enforcement. `allow_dropped_primary=True` is the deliberate override.
    """
    from app.services.ai_output_guard import (
        clamp_meta_description,
        clamp_meta_title,
        sanitize_html,
    )

    store_id = store["id"]
    resource_id = item["id"]
    shopify_id = item["shopify_id"]

    if description_html:
        from app.services.ai_output_guard import restore_missing_images
        description_html = sanitize_html(
            restore_missing_images(item.get("description"), description_html))
    meta_title = clamp_meta_title(meta_title) if meta_title else meta_title
    meta_description = clamp_meta_description(meta_description) if meta_description else meta_description

    if not allow_dropped_primary and (description_html or meta_title or meta_description):
        primary = None
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                       FROM technical_seo_analyses
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (store_id, resource_type, resource_id),
                )
                row = cur.fetchone()
            primary = (row or {}).get("kw")
        except Exception:
            logger.warning("Primary lookup failed for %s %s", resource_type, resource_id)
        if primary:
            from app.services.technical_seo_analyzer import _keyword_in_text
            old_text = " ".join([
                re.sub(r"<[^>]+>", " ", item.get("description") or ""),
                item.get("meta_title") or "", item.get("meta_description") or "",
            ])
            # Fields not being pushed keep their current value (mirrors the
            # UPDATE below), so judge the post-apply effective content.
            new_text = " ".join([
                re.sub(r"<[^>]+>", " ", description_html or (item.get("description") or "")),
                meta_title or (item.get("meta_title") or ""),
                meta_description or (item.get("meta_description") or ""),
            ])
            if _keyword_in_text(primary, old_text) and not _keyword_in_text(primary, new_text):
                raise ValueError(
                    f'Rewrite drops the resolved primary keyword "{primary}" from the page — '
                    "regenerate the content, or apply with allow_dropped_primary to override."
                )

    fields = {}
    if description_html:
        fields["description_html"] = description_html
    if meta_title:
        fields["meta_title"] = meta_title
    if meta_description:
        fields["meta_description"] = meta_description

    if fields:
        if resource_type == "collection":
            client.update_collection(shopify_id, fields)
        else:
            client.update_product(shopify_id, fields)
    if faqpage:
        # Anchor the FAQPage into the page's entity graph — the metafield is
        # replayed verbatim by the theme snippet, so @id must be set here.
        page_url = _item_url(_store_base_url(store), resource_type, item.get("handle"))
        if isinstance(faqpage, dict) and page_url:
            faqpage.setdefault("@id", f"{page_url}#faq")
            faqpage.setdefault("mainEntityOfPage", page_url)
        client.set_product_metafield(shopify_id, "seo", "faq_schema_json_ld", json.dumps(faqpage), "json")
        with db.get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, pushed_to_shopify, pushed_at)
                VALUES (%s, %s, %s, 'FAQPage', %s, TRUE, TRUE, NOW())
                ON CONFLICT (store_id, resource_id, schema_type) DO UPDATE
                SET schema_json = EXCLUDED.schema_json, pushed_to_shopify = TRUE, pushed_at = NOW()
                """,
                (store_id, str(shopify_id), resource_type, json.dumps(faqpage)),
            )

    with db.get_cursor() as cur:
        if resource_type == "collection":
            cur.execute(
                "UPDATE synced_collections SET description = %s, meta_title = %s, meta_description = %s, last_optimized_at = NOW() WHERE id = %s",
                (description_html or item.get("description"), meta_title or item.get("meta_title"), meta_description or item.get("meta_description"), resource_id),
            )
        else:
            cur.execute(
                "UPDATE synced_products SET description = %s, meta_title = %s, meta_description = %s, last_optimized_at = NOW() WHERE id = %s",
                (description_html or item.get("description"), meta_title or item.get("meta_title"), meta_description or item.get("meta_description"), resource_id),
            )

    from app.services.analysis_refresh import refresh_item_analysis
    refresh_item_analysis(store_id, resource_type, resource_id)

    stored = _stored_score(store_id, resource_type, resource_id)
    updated_item = {
        **item,
        "description": description_html or item.get("description"),
        "meta_title": meta_title,
        "meta_description": meta_description,
        "last_optimized_at": datetime.now(timezone.utc),
    }

    # Also refresh the structured-data layer. Products get their enrichment
    # metafield (seo.schema_enrichment) regenerated + pushed; collections only
    # get their schema_markup record refreshed — the theme snippet renders
    # CollectionPage JSON-LD natively, so there is nothing to push. Best-effort:
    # a schema failure must not undo the content/meta/FAQ that already applied.
    try:
        if resource_type == "collection":
            from app.services.schema_generator import generate_collection_schema
            col_schema = generate_collection_schema(updated_item, url_utils.store_host(store))
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, pushed_to_shopify, pushed_at)
                    VALUES (%s, %s, 'collection', 'CollectionPage', %s, TRUE, TRUE, NOW())
                    ON CONFLICT (store_id, resource_id, schema_type) DO UPDATE
                    SET schema_json = EXCLUDED.schema_json, pushed_to_shopify = TRUE, pushed_at = NOW()
                    """,
                    (store_id, str(shopify_id), json.dumps(col_schema)),
                )
        else:
            from app.routes.schema_routes import generate_and_push_product_schema
            generate_and_push_product_schema(resource_id, client)
    except Exception as e:
        logger.warning("Schema refresh failed for %s %s: %s", resource_type, resource_id, e)

    return _rescore_item(
        store_id,
        resource_type,
        updated_item,
        (stored or {}).get("live_crawl"),
        _crawlability_from_breakdown((stored or {}).get("breakdown")),
        url_utils.store_host(store),
    )


@ai_seo_bp.route("/api/ai-seo/optimize/apply", methods=["POST"])
def optimize_apply():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    resource_id = data.get("id")
    if not store_id or not resource_id:
        return jsonify({"error": "store_id and id are required"}), 400

    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
        cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (resource_id, int(store_id)))
        item = cur.fetchone()
    if not store or not item:
        return jsonify({"error": "Store or item not found"}), 404
    store, item = dict(store), dict(item)

    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    try:
        result = _push_and_rescore(
            store, resource_type, item,
            data.get("description_html", ""), data.get("meta_title", ""),
            data.get("meta_description", ""), data.get("faqpage_jsonld"), client,
            allow_dropped_primary=bool(data.get("allow_dropped_primary")),
        )
    except Exception as e:
        logger.exception("GEO apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    indexnow_service.ping_async(
        int(store_id), [_item_url(_store_base_url(store), resource_type, item.get("handle"))], "geo_apply"
    )
    return jsonify({"ok": True, "new_score": result["score"], "breakdown": result["breakdown"]})


# ── Phase 2b: batch GEO optimization ────────────────────────────────────


def _execute_ai_seo_batch(batch_id, store_id, resource_type, resource_ids):
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (batch_id,))
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET status = 'failed', completed_at = NOW() WHERE id = %s", (batch_id,))
        return
    store = dict(store)
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    table = "synced_collections" if resource_type == "collection" else "synced_products"
    task_type = "ai_seo_collection" if resource_type == "collection" else "ai_seo_product"
    id_key = "collection_id" if resource_type == "collection" else "product_id"
    item_col = "collection_id" if resource_type == "collection" else "product_id"
    base_url = _store_base_url(store)

    completed = failed = 0
    applied_urls = []
    for rid in resource_ids:
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (rid, int(store_id)))
            item = cur.fetchone()
        if not item:
            failed += 1
            continue
        item = dict(item)
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (item.get("title"), batch_id))
        try:
            stored = _stored_score(store_id, resource_type, rid)
            geo = run_task(task_type, {id_key: rid, "weak_signals": _weak_signals((stored or {}).get("breakdown"))}, store_id)
            if geo.get("error"):
                raise ValueError(geo["error"])
            _push_and_rescore(
                store, resource_type, item,
                geo.get("optimized_description_html", ""), geo.get("meta_title", ""),
                geo.get("meta_description", ""), geo.get("faqpage_jsonld"), client,
            )
            applied_urls.append(_item_url(base_url, resource_type, item.get("handle")))
            completed += 1
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE bulk_task_id = %s AND {item_col} = %s",
                    (
                        json.dumps({
                            "geo_edits_applied": geo.get("geo_edits_applied", []),
                            "preserved_keywords": geo.get("preserved_keywords", []),
                            "dropped_keywords": geo.get("dropped_keywords", []),
                        }),
                        batch_id, rid,
                    ),
                )
        except Exception as e:
            failed += 1
            logger.exception("Batch GEO item %s failed: %s", rid, e)
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE bulk_task_id = %s AND {item_col} = %s",
                    (str(e), batch_id, rid),
                )
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET completed = %s, failed = %s WHERE id = %s", (completed, failed, batch_id))

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'completed', completed_at = NOW(), current_product_title = NULL WHERE id = %s", (batch_id,))
    indexnow_service.ping_async(store_id, applied_urls, "geo_batch")


def _sweep_stale_jobs():
    """Fail any background job that's been 'running' far longer than possible —
    catches daemons killed without a clean restart. Runs cheaply on job start/poll."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status='failed', completed_at=NOW() "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '6 hours'"
            )
            cur.execute(
                "UPDATE ai_tracking_runs SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE ai_tasks SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '2 hours'"
            )
    except Exception:
        pass


@ai_seo_bp.route("/api/ai-seo/optimize/batch", methods=["POST"])
def optimize_batch():
    _sweep_stale_jobs()
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    ids = data.get("ids")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    # "all" → every scanned item of this type for the store.
    if ids in (None, "all", []):
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT resource_id FROM ai_seo_scores WHERE store_id = %s AND resource_type = %s ORDER BY score ASC",
                (int(store_id), resource_type),
            )
            ids = [r["resource_id"] for r in cur.fetchall()]
    ids = [int(i) for i in ids]
    if not ids:
        return jsonify({"error": "No items to optimize"}), 400

    item_col = "collection_id" if resource_type == "collection" else "product_id"
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, store_id, kind, task_type, resource_type) VALUES ('pending', %s, TRUE, %s, 'geo_optimize', 'geo_optimize', %s) RETURNING id",
            (len(ids), int(store_id), resource_type),
        )
        batch_id = cur.fetchone()["id"]
        for rid in ids:
            cur.execute(
                f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                (batch_id, rid),
            )

    spawn_task("bulk_tasks", batch_id, _execute_ai_seo_batch, batch_id, int(store_id), resource_type, ids)
    return jsonify({"ok": True, "batch_id": batch_id, "total": len(ids)}), 201


@ai_seo_bp.route("/api/ai-seo/optimize/batch/<int:batch_id>", methods=["GET"])
def optimize_batch_status(batch_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT status, total, completed, failed, current_product_title FROM bulk_tasks WHERE id = %s", (batch_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify(
        {
            "status": row["status"],
            "total": row["total"],
            "completed": row["completed"],
            "failed": row["failed"],
            "current": row["current_product_title"],
        }
    )


# ── Fact-density boost (research-driven, enrich-only, skips if nothing found) ──

LOW_FACT_DENSITY = 50


def _fact_density_score(store_id, resource_type, resource_id, item, store_domain):
    """fact_density component for old and new description text (content-only)."""
    return ai_seo_analyzer.analyze_ai_visibility(
        {**item, "resource_type": resource_type}, store_domain=store_domain
    )["breakdown"]["fact_density"]["score"]


@ai_seo_bp.route("/api/ai-seo/facts/preview", methods=["POST"])
def facts_preview():
    from app.ai_agent import run_task

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    resource_id = data.get("id")
    if not store_id or not resource_id:
        return jsonify({"error": "store_id and id are required"}), 400

    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
        cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (resource_id, int(store_id)))
        item = cur.fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404
    item = dict(item)
    store_domain = url_utils.store_host(dict(store)) if store else None

    task_type = "ai_seo_facts_collection" if resource_type == "collection" else "ai_seo_facts_product"
    id_key = "collection_id" if resource_type == "collection" else "product_id"
    result = run_task(task_type, {id_key: resource_id}, store_id)
    if result.get("error"):
        return jsonify({"error": result["error"]}), 502

    item_meta = {"id": item["id"], "shopify_id": item.get("shopify_id"), "title": item.get("title")}
    if result.get("no_change"):
        return jsonify({"no_change": True, "reason": result.get("reason"), "item": item_meta})

    before_fd = _fact_density_score(store_id, resource_type, resource_id, item, store_domain)
    after_fd = _fact_density_score(
        store_id, resource_type, resource_id,
        {**item, "description": result["optimized_description_html"]}, store_domain,
    )
    return jsonify(
        {
            "preview": result,
            "item": item_meta,
            "before_fact_density": before_fd,
            "after_fact_density": after_fd,
        }
    )


@ai_seo_bp.route("/api/ai-seo/facts/apply", methods=["POST"])
def facts_apply():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    resource_id = data.get("id")
    description_html = data.get("description_html", "")
    if not store_id or not resource_id or not description_html:
        return jsonify({"error": "store_id, id and description_html are required"}), 400

    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
        cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (resource_id, int(store_id)))
        item = cur.fetchone()
    if not store or not item:
        return jsonify({"error": "Store or item not found"}), 404
    store, item = dict(store), dict(item)

    # Same rule as the batch path: only apply an enrichment that actually raises
    # fact density — otherwise it's churn (and a fake freshness bump) for nothing.
    store_domain = url_utils.store_host(store)
    before_fd = _fact_density_score(store_id, resource_type, resource_id, item, store_domain)
    after_fd = _fact_density_score(
        store_id, resource_type, resource_id, {**item, "description": description_html}, store_domain
    )
    if after_fd <= before_fd:
        return jsonify({
            "ok": False,
            "no_change": True,
            "reason": f"Fact density would not improve ({before_fd} → {after_fd}) — nothing was applied",
            "before_fact_density": before_fd,
            "after_fact_density": after_fd,
        })

    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    try:
        result = _push_and_rescore(store, resource_type, item, description_html, "", "", None, client)
    except Exception as e:
        logger.exception("Fact-boost apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    indexnow_service.ping_async(
        int(store_id), [_item_url(_store_base_url(store), resource_type, item.get("handle"))], "fact_apply"
    )
    return jsonify({"ok": True, "new_score": result["score"], "breakdown": result["breakdown"]})


def _execute_fact_batch(batch_id, store_id, resource_type, resource_ids):
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (batch_id,))
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET status = 'failed', completed_at = NOW() WHERE id = %s", (batch_id,))
        return
    store = dict(store)
    base = _store_base_url(store)
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    table = "synced_collections" if resource_type == "collection" else "synced_products"
    task_type = "ai_seo_facts_collection" if resource_type == "collection" else "ai_seo_facts_product"
    id_key = "collection_id" if resource_type == "collection" else "product_id"
    item_col = "collection_id" if resource_type == "collection" else "product_id"

    completed = failed = 0
    applied_urls = []
    for rid in resource_ids:
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (rid, int(store_id)))
            item = cur.fetchone()
        if not item:
            failed += 1
            continue
        item = dict(item)
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (item.get("title"), batch_id))
        try:
            result = run_task(task_type, {id_key: rid}, store_id)
            if result.get("error"):
                raise ValueError(result["error"])
            # Only apply when it measurably raises fact density — else leave the item alone.
            enriched = False
            if not result.get("no_change"):
                new_html = result["optimized_description_html"]
                before_fd = _fact_density_score(store_id, resource_type, rid, item, base)
                after_fd = _fact_density_score(store_id, resource_type, rid, {**item, "description": new_html}, base)
                if after_fd > before_fd:
                    enriched = True
                else:
                    result["reason"] = "facts found but no measurable fact-density gain"
            if enriched:
                _push_and_rescore(store, resource_type, item, result["optimized_description_html"], "", "", None, client)
                applied_urls.append(_item_url(base, resource_type, item.get("handle")))
            completed += 1
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE bulk_task_id = %s AND {item_col} = %s",
                    (json.dumps({"enriched": enriched, "facts_added": result.get("facts_added", []), "reason": result.get("reason")}), batch_id, rid),
                )
        except Exception as e:
            failed += 1
            logger.exception("Fact-boost batch item %s failed: %s", rid, e)
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE bulk_task_id = %s AND {item_col} = %s",
                    (str(e), batch_id, rid),
                )
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET completed = %s, failed = %s WHERE id = %s", (completed, failed, batch_id))

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'completed', completed_at = NOW(), current_product_title = NULL WHERE id = %s", (batch_id,))
    indexnow_service.ping_async(store_id, applied_urls, "fact_batch")


@ai_seo_bp.route("/api/ai-seo/facts/batch", methods=["POST"])
def facts_batch():
    _sweep_stale_jobs()
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    ids = data.get("ids")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        if ids == "low":
            cur.execute(
                """SELECT resource_id FROM ai_seo_scores
                   WHERE store_id = %s AND resource_type = %s
                   AND (breakdown->'fact_density'->>'score')::int < %s
                   ORDER BY (breakdown->'fact_density'->>'score')::int ASC""",
                (int(store_id), resource_type, LOW_FACT_DENSITY),
            )
            ids = [r["resource_id"] for r in cur.fetchall()]
        elif ids in (None, "all", []):
            cur.execute(
                "SELECT resource_id FROM ai_seo_scores WHERE store_id = %s AND resource_type = %s ORDER BY (breakdown->'fact_density'->>'score')::int ASC",
                (int(store_id), resource_type),
            )
            ids = [r["resource_id"] for r in cur.fetchall()]
    ids = [int(i) for i in ids]
    if not ids:
        return jsonify({"error": "No items to boost"}), 400

    item_col = "collection_id" if resource_type == "collection" else "product_id"
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, store_id, kind, task_type, resource_type) VALUES ('pending', %s, TRUE, %s, 'fact_boost', 'fact_boost', %s) RETURNING id",
            (len(ids), int(store_id), resource_type),
        )
        batch_id = cur.fetchone()["id"]
        for rid in ids:
            cur.execute(f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)", (batch_id, rid))

    spawn_task("bulk_tasks", batch_id, _execute_fact_batch, batch_id, int(store_id), resource_type, ids)
    return jsonify({"ok": True, "batch_id": batch_id, "total": len(ids)}), 201


# ── Schema regenerate + re-push (no LLM — enrichment metafields only) ──


def _execute_schema_repush_batch(batch_id, store_id, resource_type, resource_ids):
    from app.routes.schema_routes import _spec_properties_for
    from app.services.schema_generator import (
        generate_collection_schema,
        generate_product_enrichment,
        validate_enrichment,
    )

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (batch_id,))
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET status = 'failed', completed_at = NOW() WHERE id = %s", (batch_id,))
        return
    store = dict(store)
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    host = url_utils.store_host(store)
    is_collection = resource_type == "collection"
    table = "synced_collections" if is_collection else "synced_products"
    item_col = "collection_id" if is_collection else "product_id"
    schema_type = "CollectionPage" if is_collection else "Product"

    completed = failed = 0
    for rid in resource_ids:
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (rid, int(store_id)))
            item = cur.fetchone()
        if not item:
            failed += 1
            with db.get_cursor() as cur:
                cur.execute("UPDATE bulk_tasks SET completed = %s, failed = %s WHERE id = %s", (completed, failed, batch_id))
            continue
        item = dict(item)
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (item.get("title"), batch_id))
        try:
            if is_collection:
                payload = generate_collection_schema(item, host)
                is_valid, errors = True, []
            else:
                payload = generate_product_enrichment(item, _spec_properties_for(item))
                is_valid, errors = validate_enrichment(payload)

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT schema_json, pushed_to_shopify FROM schema_markup "
                    "WHERE store_id = %s AND resource_id = %s AND schema_type = %s",
                    (int(store_id), item["shopify_id"], schema_type),
                )
                existing = cur.fetchone()
            unchanged = (
                existing
                and existing["pushed_to_shopify"]
                and json.dumps(existing["schema_json"], sort_keys=True) == json.dumps(payload, sort_keys=True)
            )

            if not unchanged:
                with db.get_cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, validation_errors)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (store_id, resource_id, schema_type)
                        DO UPDATE SET schema_json = EXCLUDED.schema_json, is_valid = EXCLUDED.is_valid,
                                      validation_errors = EXCLUDED.validation_errors, updated_at = NOW(),
                                      pushed_to_shopify = FALSE, pushed_at = NULL
                        """,
                        (int(store_id), item["shopify_id"], resource_type, schema_type,
                         json.dumps(payload), is_valid, json.dumps(errors) if errors else None),
                    )
                if not is_valid:
                    raise ValueError(f"Enrichment validation failed: {errors}")
                # Collections have nothing to push — the theme snippet renders
                # CollectionPage natively; the DB record alone carries scoring credit.
                if not is_collection:
                    client.set_product_metafield(
                        item["shopify_id"], "seo", "schema_enrichment", json.dumps(payload), "json"
                    )
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE schema_markup SET pushed_to_shopify = TRUE, pushed_at = NOW() "
                        "WHERE store_id = %s AND resource_id = %s AND schema_type = %s",
                        (int(store_id), item["shopify_id"], schema_type),
                    )
                time.sleep(0.3)

            completed += 1
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() "
                    f"WHERE bulk_task_id = %s AND {item_col} = %s",
                    (json.dumps({"pushed": not unchanged, "unchanged": bool(unchanged)}), batch_id, rid),
                )
        except Exception as e:
            failed += 1
            logger.exception("Schema repush item %s failed: %s", rid, e)
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() "
                    f"WHERE bulk_task_id = %s AND {item_col} = %s",
                    (str(e), batch_id, rid),
                )
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET completed = %s, failed = %s WHERE id = %s", (completed, failed, batch_id))

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'completed', completed_at = NOW(), current_product_title = NULL WHERE id = %s", (batch_id,))


def _schema_repush_candidate_ids(store_id, resource_type, ids):
    """Resolve the ids selector for a repush batch: 'all' → every synced item,
    'stale' → items whose schema record is missing or not pushed."""
    is_collection = resource_type == "collection"
    table = "synced_collections" if is_collection else "synced_products"
    schema_type = "CollectionPage" if is_collection else "Product"
    with db.get_cursor() as cur:
        if ids == "stale":
            cur.execute(
                f"""SELECT t.id FROM {table} t
                    LEFT JOIN schema_markup sm ON sm.store_id = t.store_id
                        AND sm.resource_id = t.shopify_id AND sm.schema_type = %s
                    WHERE t.store_id = %s AND (sm.id IS NULL OR sm.pushed_to_shopify = FALSE)
                    ORDER BY t.id""",
                (schema_type, int(store_id)),
            )
        else:
            cur.execute(f"SELECT id FROM {table} WHERE store_id = %s ORDER BY id", (int(store_id),))
        return [r["id"] for r in cur.fetchall()]


@ai_seo_bp.route("/api/ai-seo/schema/repush", methods=["POST"])
def schema_repush():
    """Regenerate + push schema enrichment for products (and refresh collection
    schema records) as a tracked background batch. No LLM calls involved."""
    _sweep_stale_jobs()
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    rtype = data.get("type", "product")
    ids = data.get("ids", "all")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    if rtype not in ("product", "collection", "all"):
        return jsonify({"error": "type must be product, collection or all"}), 400

    batches = []
    for resource_type in (("product", "collection") if rtype == "all" else (rtype,)):
        resolved = (
            _schema_repush_candidate_ids(store_id, resource_type, ids)
            if ids in ("all", "stale", None, [])
            else [int(i) for i in ids]
        )
        if not resolved:
            continue
        batch_id = _spawn_ai_seo_batch(int(store_id), "schema_repush", resource_type, resolved)
        batches.append({"batch_id": batch_id, "resource_type": resource_type, "total": len(resolved)})

    if not batches:
        return jsonify({"ok": True, "batches": [], "message": "Nothing to repush"}), 200
    return jsonify({"ok": True, "batches": batches}), 201


@ai_seo_bp.route("/api/ai-seo/batches", methods=["GET"])
def list_batches():
    """Recent AI SEO batch jobs (optimize / fact-boost) with derived stats."""
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, kind, resource_type, status, total, completed, failed,
                      current_product_title, created_at, completed_at
               FROM bulk_tasks WHERE store_id = %s AND kind IS NOT NULL
               ORDER BY id DESC LIMIT 25""",
            (int(store_id),),
        )
        batches = [dict(r) for r in cur.fetchall()]
        ids = [b["id"] for b in batches]
        stats = {}
        if ids:
            cur.execute(
                """SELECT bulk_task_id,
                    COUNT(*) FILTER (WHERE (ai_result->>'enriched') = 'true') AS enriched,
                    COUNT(*) FILTER (WHERE ai_result IS NOT NULL AND (ai_result->>'enriched') = 'false') AS skipped,
                    COALESCE(SUM(jsonb_array_length(ai_result->'facts_added'))
                             FILTER (WHERE (ai_result->>'enriched') = 'true'
                                     AND jsonb_typeof(ai_result->'facts_added') = 'array'), 0) AS facts,
                    COUNT(*) FILTER (WHERE jsonb_typeof(ai_result->'dropped_keywords') = 'array'
                                     AND jsonb_array_length(ai_result->'dropped_keywords') > 0) AS dropped
                   FROM bulk_task_items WHERE bulk_task_id = ANY(%s) GROUP BY bulk_task_id""",
                (ids,),
            )
            for r in cur.fetchall():
                stats[r["bulk_task_id"]] = dict(r)
    return jsonify(
        [
            {
                "id": b["id"],
                "kind": b["kind"],
                "resource_type": b["resource_type"],
                "status": b["status"],
                "total": b["total"],
                "completed": b["completed"],
                "failed": b["failed"],
                "current": b["current_product_title"],
                "enriched": int((stats.get(b["id"]) or {}).get("enriched") or 0),
                "skipped": int((stats.get(b["id"]) or {}).get("skipped") or 0),
                "facts_added": int((stats.get(b["id"]) or {}).get("facts") or 0),
                "dropped_kw_items": int((stats.get(b["id"]) or {}).get("dropped") or 0),
                "created_at": b["created_at"].isoformat() if b["created_at"] else None,
                "completed_at": b["completed_at"].isoformat() if b["completed_at"] else None,
            }
            for b in batches
        ]
    )


def _spawn_ai_seo_batch(store_id, kind, resource_type, ids):
    """Create a bulk_task + items for the given resource ids and start the matching
    background runner. Shared by the batch creators' retry path."""
    item_col = "collection_id" if resource_type == "collection" else "product_id"
    ids = [int(i) for i in ids]
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, store_id, kind, task_type, resource_type) "
            "VALUES ('pending', %s, TRUE, %s, %s, %s, %s) RETURNING id",
            (len(ids), int(store_id), kind, kind, resource_type),
        )
        batch_id = cur.fetchone()["id"]
        for rid in ids:
            cur.execute(f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)", (batch_id, rid))
    runner = {
        "fact_boost": _execute_fact_batch,
        "schema_repush": _execute_schema_repush_batch,
    }.get(kind, _execute_ai_seo_batch)
    spawn_task("bulk_tasks", batch_id, runner, batch_id, int(store_id), resource_type, ids)
    return batch_id


@ai_seo_bp.route("/api/ai-seo/batches/<int:batch_id>/retry", methods=["POST"])
def retry_batch(batch_id):
    """Re-run only the failed items of a finished batch as a new batch."""
    _sweep_stale_jobs()
    with db.get_cursor() as cur:
        cur.execute("SELECT store_id, kind, resource_type, status FROM bulk_tasks WHERE id = %s", (batch_id,))
        b = cur.fetchone()
    if not b:
        return jsonify({"error": "Batch not found"}), 404
    if b["status"] in ("pending", "running"):
        return jsonify({"error": "Batch is still running"}), 409
    resource_type = b["resource_type"] or "product"
    item_col = "collection_id" if resource_type == "collection" else "product_id"
    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT {item_col} AS rid FROM bulk_task_items WHERE bulk_task_id = %s AND status = 'failed'",
            (batch_id,),
        )
        ids = [r["rid"] for r in cur.fetchall() if r["rid"] is not None]
    if not ids:
        return jsonify({"error": "No failed items to retry"}), 400
    new_id = _spawn_ai_seo_batch(b["store_id"], b["kind"], resource_type, ids)
    return jsonify({"ok": True, "batch_id": new_id, "total": len(ids)}), 201


@ai_seo_bp.route("/api/ai-seo/batches/clear", methods=["POST"])
def clear_batches():
    """Remove all finished AI SEO batches for a store (keeps running ones)."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM bulk_tasks WHERE store_id = %s AND kind IS NOT NULL "
            "AND status NOT IN ('pending','running')",
            (int(store_id),),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM bulk_task_items WHERE bulk_task_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM bulk_tasks WHERE id = ANY(%s)", (ids,))
    return jsonify({"ok": True, "cleared": len(ids)})


@ai_seo_bp.route("/api/ai-seo/batches/<int:batch_id>", methods=["DELETE"])
def delete_batch(batch_id):
    """Clear a single finished batch from the activity list."""
    with db.get_cursor() as cur:
        cur.execute("SELECT status FROM bulk_tasks WHERE id = %s", (batch_id,))
        b = cur.fetchone()
        if not b:
            return jsonify({"error": "Batch not found"}), 404
        if b["status"] in ("pending", "running"):
            return jsonify({"error": "Can't clear a running job"}), 409
        cur.execute("DELETE FROM bulk_task_items WHERE bulk_task_id = %s", (batch_id,))
        cur.execute("DELETE FROM bulk_tasks WHERE id = %s", (batch_id,))
    return jsonify({"ok": True})


# ── Phase 3: AI visibility tracking ─────────────────────────────────────


@ai_seo_bp.route("/api/ai-seo/prompts", methods=["GET"])
def list_prompts():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, prompt, intent, is_active FROM ai_prompts WHERE store_id = %s ORDER BY id",
            (int(store_id),),
        )
        return jsonify([dict(r) for r in cur.fetchall()])


@ai_seo_bp.route("/api/ai-seo/prompts", methods=["POST"])
def manage_prompts():
    from app.services.ai_visibility_tracker import generate_prompt_set

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    if action == "generate":
        try:
            saved = generate_prompt_set(int(store_id))
        except Exception as e:
            logger.exception("Prompt generation failed: %s", e)
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "prompts": saved})

    if action == "add":
        # One question per line, so a whole set can be pasted in at once.
        lines = [ln.strip() for ln in (data.get("prompt") or "").splitlines() if ln.strip()]
        if not lines:
            return jsonify({"error": "prompt is required"}), 400
        intent = data.get("intent") or "buyer"
        if intent not in ("buyer", "brand"):
            return jsonify({"error": "intent must be 'buyer' or 'brand'"}), 400

        added, skipped = [], []
        with db.get_cursor() as cur:
            for prompt in lines:
                cur.execute(
                    "SELECT 1 FROM ai_prompts WHERE store_id = %s AND LOWER(prompt) = LOWER(%s)",
                    (int(store_id), prompt),
                )
                if cur.fetchone():
                    skipped.append(prompt)
                    continue
                cur.execute(
                    "INSERT INTO ai_prompts (store_id, prompt, intent) VALUES (%s, %s, %s) "
                    "RETURNING id, prompt, intent, is_active",
                    (int(store_id), prompt, intent),
                )
                added.append(dict(cur.fetchone()))
        if not added and skipped:
            return jsonify({"error": "Already in your prompt set", "skipped": skipped}), 400
        return jsonify({"ok": True, "added": added, "skipped": skipped, **(added[0] if added else {})})

    if action == "update":
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return jsonify({"error": "prompt_id is required"}), 400
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt cannot be empty"}), 400
        intent = data.get("intent")
        if intent is not None and intent not in ("buyer", "brand"):
            return jsonify({"error": "intent must be 'buyer' or 'brand'"}), 400
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT 1 FROM ai_prompts WHERE store_id = %s AND LOWER(prompt) = LOWER(%s) AND id <> %s",
                (int(store_id), prompt, int(prompt_id)),
            )
            if cur.fetchone():
                return jsonify({"error": "Another prompt already uses this wording"}), 400
            # Past results keep their own prompt snapshot in ai_mention_results,
            # so history stays true to the question that was actually asked.
            if intent:
                cur.execute(
                    "UPDATE ai_prompts SET prompt = %s, intent = %s WHERE id = %s AND store_id = %s "
                    "RETURNING id, prompt, intent, is_active",
                    (prompt, intent, int(prompt_id), int(store_id)),
                )
            else:
                cur.execute(
                    "UPDATE ai_prompts SET prompt = %s WHERE id = %s AND store_id = %s "
                    "RETURNING id, prompt, intent, is_active",
                    (prompt, int(prompt_id), int(store_id)),
                )
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "Prompt not found"}), 404
        return jsonify({"ok": True, **dict(row)})

    if action == "toggle":
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            return jsonify({"error": "prompt_id is required"}), 400
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_prompts SET is_active = NOT COALESCE(is_active, TRUE) "
                "WHERE id = %s AND store_id = %s RETURNING id, prompt, intent, is_active",
                (int(prompt_id), int(store_id)),
            )
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "Prompt not found"}), 404
        return jsonify({"ok": True, **dict(row)})

    if action == "delete":
        with db.get_cursor() as cur:
            cur.execute(
                "DELETE FROM ai_prompts WHERE id = %s AND store_id = %s",
                (data.get("prompt_id"), int(store_id)),
            )
        return jsonify({"ok": True})

    return jsonify({"error": "Unknown action"}), 400


@ai_seo_bp.route("/api/ai-seo/tracking/run", methods=["POST"])
def tracking_run():
    from app.services.ai_visibility_tracker import start_tracking_run

    _sweep_stale_jobs()
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_tracking_runs WHERE store_id = %s AND status IN ('pending','running')",
            (int(store_id),),
        )
        if cur.fetchone():
            return jsonify({"error": "A visibility tracking run is already in progress for this store"}), 409
    run_id = start_tracking_run(int(store_id), data.get("models"))
    return jsonify({"ok": True, "run_id": run_id}), 201


@ai_seo_bp.route("/api/ai-seo/tracking/runs", methods=["GET"])
def tracking_runs():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT id, status, models, prompt_count, brand_mention_rate, citation_rate,
                   share_of_voice, avg_rank, cost_estimate, summary, error, started_at, completed_at
            FROM ai_tracking_runs WHERE store_id = %s ORDER BY started_at DESC LIMIT 30
            """,
            (int(store_id),),
        )
        rows = cur.fetchall()
    return jsonify(
        [
            {
                **{k: r[k] for k in (
                    "id", "status", "models", "prompt_count", "brand_mention_rate",
                    "citation_rate", "share_of_voice", "avg_rank", "cost_estimate", "summary", "error",
                )},
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ]
    )


@ai_seo_bp.route("/api/ai-seo/tracking/<int:run_id>", methods=["GET"])
def tracking_detail(run_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            return jsonify({"error": "Run not found"}), 404
        cur.execute(
            """
            SELECT m.model, m.prompt, m.prompt_id, m.run_index, m.brand_mentioned,
                   m.domain_cited, m.brand_rank, m.products_mentioned,
                   m.competitors_mentioned, m.recommended_brands, m.grounded,
                   m.citations, m.response_text, m.brand_known, p.intent
            FROM ai_mention_results m
            LEFT JOIN ai_prompts p ON p.id = m.prompt_id
            WHERE m.run_id = %s ORDER BY m.id
            """,
            (run_id,),
        )
        results = [dict(r) for r in cur.fetchall()]
    # Compute live so excluded brands drop out immediately (stored summary is a snapshot).
    from app.services.ai_visibility_tracker import compute_share_of_voice
    agg = compute_share_of_voice(run_id)
    return jsonify(
        {
            "id": run["id"],
            "status": run["status"],
            "share_of_voice": agg["share_of_voice"],
            "brand_mention_rate": agg["brand_mention_rate"],
            "citation_rate": agg["citation_rate"],
            "avg_rank": agg["avg_rank"],
            "brand_known_rate": agg["brand_known_rate"],
            "memory": agg.get("memory"),
            "retrieval": agg.get("retrieval"),
            "models": agg.get("models"),
            "total_answers": agg["total_answers"],
            "brand_hits": agg["brand_hits"],
            "competitors": agg["competitors"],
            "product_brands": agg["product_brands"],
            "sources": agg.get("sources") or [],
            "grounded_answers_with_citations": agg.get("grounded_answers_with_citations") or 0,
            "gap_analysis": run["gap_analysis"],
            "results": results,
        }
    )


@ai_seo_bp.route("/api/ai-seo/tracking/prefs", methods=["GET"])
def tracking_prefs():
    """Tracked / excluded brand preferences for a store."""
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT brand_key, name, domain, status FROM ai_brand_prefs
               WHERE store_id = %s ORDER BY created_at DESC""",
            (int(store_id),),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify(
        {
            "tracked": [r for r in rows if r["status"] == "tracked"],
            "excluded": [r for r in rows if r["status"] == "excluded"],
        }
    )


@ai_seo_bp.route("/api/ai-seo/tracking/prefs", methods=["POST"])
def set_tracking_pref():
    """Upsert a brand pref keyed by the run's canonical brand key.

    status 'tracked'/'excluded' upserts; status null removes the pref
    (un-track / un-exclude)."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    name = (data.get("name") or "").strip()
    domain = (data.get("domain") or "").strip().lower() or None
    status = data.get("status")
    from app.services.ai_visibility_tracker import _canonical_brand_key

    key = (data.get("key") or "").strip() or _canonical_brand_key(domain or name)
    if not store_id or not key:
        return jsonify({"error": "store_id and key/name are required"}), 400
    if status not in ("tracked", "excluded", None):
        return jsonify({"error": "status must be 'tracked', 'excluded' or null"}), 400
    with db.get_cursor() as cur:
        if status is None:
            cur.execute(
                "DELETE FROM ai_brand_prefs WHERE store_id = %s AND brand_key = %s",
                (int(store_id), key),
            )
        else:
            cur.execute(
                """INSERT INTO ai_brand_prefs (store_id, brand_key, name, domain, status)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (store_id, brand_key)
                   DO UPDATE SET status = EXCLUDED.status, name = EXCLUDED.name,
                                 domain = COALESCE(EXCLUDED.domain, ai_brand_prefs.domain)""",
                (int(store_id), key, name or key, domain, status),
            )
    # Tracked brands with a real domain also feed the classic competitor tracker.
    if status == "tracked" and domain and "." in domain:
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT name FROM competitors WHERE store_id = %s AND domain = %s",
                    (int(store_id), domain),
                )
                clash = cur.fetchone()
            if not clash:
                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO competitors (store_id, domain, name, notes)
                           VALUES (%s, %s, %s, 'Added from AI visibility results')
                           ON CONFLICT (store_id, domain) DO NOTHING""",
                        (int(store_id), domain, name or key),
                    )
        except Exception as e:
            logger.warning("Competitor mirror insert failed: %s", e)
    return jsonify({"ok": True, "key": key, "status": status})


@ai_seo_bp.route("/api/ai-seo/tracking/<int:run_id>/reanalyze", methods=["POST"])
def tracking_reanalyze(run_id):
    """Re-extract entities from a run's stored answers with the LLM extractor."""
    from app.services.ai_visibility_tracker import start_reanalyze

    with db.get_cursor() as cur:
        cur.execute("SELECT status FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
    if not run:
        return jsonify({"error": "Run not found"}), 404
    if run["status"] in ("pending", "running", "reanalyzing"):
        return jsonify({"error": "Run is busy"}), 409
    start_reanalyze(run_id)
    return jsonify({"ok": True, "run_id": run_id}), 202


@ai_seo_bp.route("/api/ai-seo/tracking/resolve-domains", methods=["POST"])
def resolve_competitor_domains():
    """Look up official websites for AI-recommended brands missing a domain.

    Returns the refreshed competitor list (with newly resolved domains merged in).
    Resolutions are cached in brand_domains, so this is cheap on repeat calls.
    """
    from app.services.ai_visibility_tracker import compute_share_of_voice, resolve_brand_domains

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    run_id = data.get("run_id")
    if not store_id or not run_id:
        return jsonify({"error": "store_id and run_id are required"}), 400
    try:
        resolve_brand_domains(int(store_id), int(run_id))
        agg = compute_share_of_voice(int(run_id))
    except Exception as e:
        logger.exception("Resolve competitor domains failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"competitors": agg["competitors"]})


@ai_seo_bp.route("/api/ai-seo/tracking/<int:run_id>/gap-analysis", methods=["POST"])
def tracking_gap_analysis(run_id):
    from app.services.ai_visibility_tracker import analyze_competitive_gap

    try:
        analysis = analyze_competitive_gap(run_id)
    except Exception as e:
        logger.exception("Gap analysis failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "gap_analysis": analysis})


@ai_seo_bp.route("/api/ai-seo/gap/send-to-blog", methods=["POST"])
def gap_send_to_blog():
    """Queue a gap-analysis content idea as a blog topic suggestion, entering the
    existing topic-research → schedule pipeline (schedule it from the Blog section)."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    idea = data.get("idea") or {}
    title = (idea.get("title") or "").strip()
    if not store_id or not title:
        return jsonify({"error": "store_id and idea.title are required"}), 400

    target_query = (idea.get("target_query") or "").strip()
    keywords = {"primary": target_query, "informational": []} if target_query else {}
    rationale = (idea.get("why") or "").strip() or "From AI visibility gap analysis"
    fmt = (idea.get("format") or "").strip()
    if fmt:
        rationale = f"[{fmt}] {rationale}"

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM topic_suggestions WHERE store_id = %s AND topic = %s AND status = 'suggested'",
            (int(store_id), title[:500]),
        )
        existing = cur.fetchone()
        if existing:
            return jsonify({"ok": True, "suggestion_id": existing["id"], "already_queued": True})
        cur.execute(
            """INSERT INTO topic_suggestions
                (store_id, topic, h1, keywords, search_intent, opportunity_type, rationale)
            VALUES (%s, %s, %s, %s, 'informational', 'ai_visibility_gap', %s) RETURNING id""",
            (int(store_id), title[:500], title[:500], json.dumps(keywords), rationale),
        )
        suggestion_id = cur.fetchone()["id"]

    # Auto-research the idea so it arrives with the full topic-research shape
    # (meta, keyword groups, overlap guard, link targets). Best-effort — the
    # idea lands either way and a manual Research button covers failures.
    enrich_task_id = None
    try:
        from app.routes.blog_routes import start_topic_enrich
        enrich_task_id = start_topic_enrich(int(store_id), suggestion_id)
    except Exception as e:
        logger.warning("Auto-enrich failed to start for suggestion %s: %s", suggestion_id, e)
    return jsonify({"ok": True, "suggestion_id": suggestion_id, "enrich_task_id": enrich_task_id})


_QUESTION_STOPWORDS = {
    "the", "and", "for", "with", "what", "which", "where", "when", "who", "why", "how",
    "can", "does", "are", "should", "will", "best", "good", "top", "place", "places",
    "buy", "get", "find", "shop", "store", "stores", "online", "website", "websites",
    "site", "sites", "retailer", "retailers", "cheap", "cheapest", "near", "you",
    "your", "recommend", "recommended", "from", "that", "this",
}


@ai_seo_bp.route("/api/ai-seo/tracking/question-targets", methods=["GET"])
def tracking_question_targets():
    """Map a losing buyer question to the store's own pages worth optimizing for it.

    Keyword-matches the question against synced collection/product titles and joins
    each match's AI-readiness score, weakest matches first — the concrete next
    action after seeing a 0-mention question in the breakdown."""
    store_id = request.args.get("store_id")
    q = (request.args.get("q") or "").strip().lower()
    if not store_id or not q:
        return jsonify({"error": "store_id and q are required"}), 400
    tokens = [
        t for t in re.findall(r"[a-z0-9']+", q)
        if len(t) > 2 and t not in _QUESTION_STOPWORDS
    ]
    if not tokens:
        return jsonify({"targets": []})

    out = []
    with db.get_cursor() as cur:
        for rtype, table in (("collection", "synced_collections"), ("product", "synced_products")):
            cur.execute(
                f"""SELECT t.id, t.title, s.score, s.breakdown
                    FROM {table} t
                    LEFT JOIN ai_seo_scores s
                      ON s.store_id = t.store_id AND s.resource_type = %s AND s.resource_id = t.id
                    WHERE t.store_id = %s""",
                (rtype, int(store_id)),
            )
            for r in cur.fetchall():
                title = (r["title"] or "").lower()
                matched = [t for t in tokens if t in title or t.rstrip("s") in title]
                if not matched:
                    continue
                weakest = []
                if r["breakdown"]:
                    ranked = sorted(
                        ((k, d) for k, d in r["breakdown"].items() if k in COMPONENT_LABELS),
                        key=lambda kv: kv[1].get("score", 0),
                    )
                    weakest = [COMPONENT_LABELS[k] for k, _ in ranked[:2]]
                out.append({
                    "type": rtype,
                    "id": r["id"],
                    "title": r["title"],
                    "score": r["score"],
                    "matched": len(matched),
                    "weak_signals": weakest,
                })
    # Collections first (category questions map to category pages), then most
    # tokens matched, then lowest AI score — the biggest optimization headroom.
    out.sort(key=lambda x: (x["type"] != "collection", -x["matched"], x["score"] if x["score"] is not None else 101))
    return jsonify({"targets": out[:6]})


# ── Off-site visibility (third-party corroboration) ─────────────────────


@ai_seo_bp.route("/api/ai-seo/offsite/run", methods=["POST"])
def offsite_run():
    from app.services.offsite_visibility_service import run_offsite_audit

    sweep_stale_jobs()
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM offsite_runs WHERE store_id = %s AND status IN ('pending','running')",
            (int(store_id),),
        )
        if cur.fetchone():
            return jsonify({"error": "An off-site audit is already running for this store"}), 409
        cur.execute(
            "INSERT INTO offsite_runs (store_id, status) VALUES (%s, 'pending') RETURNING id",
            (int(store_id),),
        )
        run_id = cur.fetchone()["id"]
    spawn_task("offsite_runs", run_id, run_offsite_audit, run_id, int(store_id))
    return jsonify({"ok": True, "run_id": run_id}), 201


@ai_seo_bp.route("/api/ai-seo/offsite/runs", methods=["GET"])
def offsite_runs():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, queries_run, scorecard, error, started_at, completed_at
               FROM offsite_runs WHERE store_id = %s ORDER BY id DESC LIMIT 20""",
            (int(store_id),),
        )
        rows = cur.fetchall()
    return jsonify(
        [
            {
                **{k: r[k] for k in ("id", "status", "queries_run", "scorecard", "error")},
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ]
    )


@ai_seo_bp.route("/api/ai-seo/offsite/<int:run_id>", methods=["GET"])
def offsite_detail(run_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM offsite_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            return jsonify({"error": "Run not found"}), 404
        cur.execute(
            """SELECT query, query_kind, position, title, url, domain, classification,
                      brand_present, competitors_present
               FROM offsite_results WHERE run_id = %s ORDER BY query_kind, query, position""",
            (run_id,),
        )
        results = [dict(r) for r in cur.fetchall()]
    grouped = {}
    for r in results:
        grouped.setdefault(r["query_kind"], []).append(r)
    return jsonify(
        {
            "id": run["id"],
            "status": run["status"],
            "queries_run": run["queries_run"],
            "scorecard": run["scorecard"],
            "actions": run["actions"],
            "error": run["error"],
            "results": grouped,
        }
    )


# ── Phase 4: storefront AEO assets ──────────────────────────────────────


def _client_for_store(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
    if not store:
        return None, None
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    return client, store


def _save_aeo_asset(store_id, asset_type, content, status="draft"):
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO aeo_assets (store_id, asset_type, content, status, published_at, updated_at)
            VALUES (%s, %s, %s, %s, CASE WHEN %s = 'published' THEN NOW() ELSE NULL END, NOW())
            ON CONFLICT (store_id, asset_type) DO UPDATE SET
                content = EXCLUDED.content, status = EXCLUDED.status,
                published_at = EXCLUDED.published_at, updated_at = NOW()
            """,
            (store_id, asset_type, content, status, status),
        )


def _crawl_score(num_allowed, total, blocks_sensitive, verified_ratio=None):
    """Mirror ai_seo_analyzer.analyze_store_crawlability scoring for impact estimates."""
    allowed_ratio = (num_allowed / total) if total else 0
    score = round(allowed_ratio * 70)
    if blocks_sensitive:
        score += 15
    ratio = verified_ratio if verified_ratio is not None else allowed_ratio
    score += round(ratio * 15)
    return min(score, 100)


def _current_robots_state(base_url):
    robots = _fetch_text(f"{base_url}/robots.txt") if base_url else None
    if not robots:
        return {"present": False, "detail": "Could not fetch /robots.txt"}
    rules = ai_seo_analyzer._parse_robots_ai_rules(robots)
    allowed = [b for b, ok in rules["bots"].items() if ok]
    blocked = [b for b, ok in rules["bots"].items() if not ok]
    return {
        "present": True,
        "ai_bots_allowed": allowed,
        "ai_bots_blocked": blocked,
        "ai_bots_total": len(rules["bots"]),
        "blocks_sensitive": rules["blocks_sensitive"],
        "raw": robots[:4000],
    }


def _current_llms_state(base_url):
    if not base_url:
        return {"present": False}
    root = _fetch_text(f"{base_url}/llms.txt")
    page = _fetch_text(f"{base_url}/pages/llms")
    if root and "<html" not in root[:200].lower():
        return {"present": True, "location": "root", "url": f"{base_url}/llms.txt", "raw": root[:4000]}
    if page:
        return {"present": True, "location": "page", "url": f"{base_url}/pages/llms"}
    return {"present": False}


@ai_seo_bp.route("/api/ai-seo/aeo/preview", methods=["GET"])
def aeo_preview():
    from app.services import aeo_asset_generator as gen

    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    store_id = int(store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone()
        cur.execute("SELECT asset_type, status, published_at FROM aeo_assets WHERE store_id = %s", (store_id,))
        published = {r["asset_type"]: r["status"] for r in cur.fetchall()}
    if not store:
        return jsonify({"error": "Store not found"}), 404

    base_url = _store_base_url(store)
    org = gen.generate_organization_jsonld(store_id)
    org_configured = bool(settings and (settings.get("organization_name") or settings.get("organization_logo_url") or settings.get("organization_url")))
    site_snippet = gen.generate_site_schema_snippet(store_id)
    site_asset = _site_schema_asset(store_id)
    schema_update_available = bool(
        site_asset["published"]
        and site_asset["snippet_sha"] != hashlib.sha256(site_snippet.encode()).hexdigest()
    )

    robots_state = _current_robots_state(base_url)
    llms_state = _current_llms_state(base_url)
    llms_present = bool(llms_state.get("present"))

    # Crawlability signal: current vs projected (mirrors analyzer scoring).
    total = robots_state.get("ai_bots_total") or len(ai_seo_analyzer.AI_BOTS)
    cur_allowed = len(robots_state.get("ai_bots_allowed") or []) if robots_state.get("present") else total
    cur_sensitive = robots_state.get("blocks_sensitive", True)
    bot_results, bot_checked_at = _cached_bot_check(store_id)
    ok_bots, tested_bots = ai_seo_analyzer.summarize_bot_access(bot_results)
    verified_ratio = (ok_bots / tested_bots) if tested_bots else None
    crawl_current = _crawl_score(cur_allowed, total, cur_sensitive, verified_ratio)
    crawl_after_robots = _crawl_score(total, total, True, verified_ratio)  # proposed: all allowed + sensitive blocked

    # Entity-authority + structured-data averages from scanned items.
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT AVG((breakdown->'entity_authority'->>'score')::numeric) ea,
                      AVG((breakdown->'structured_data'->>'score')::numeric) sd
               FROM ai_seo_scores WHERE store_id = %s""",
            (store_id,),
        )
        row = cur.fetchone()
        ea_avg = round(float(row["ea"])) if row and row["ea"] is not None else None
        sd_avg = round(float(row["sd"])) if row and row["sd"] is not None else None
        # Detect schema already on the live storefront (from a scanned product's crawl).
        cur.execute(
            "SELECT live_crawl FROM ai_seo_scores WHERE store_id = %s AND resource_type='product' AND live_crawl IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (store_id,),
        )
        lc = cur.fetchone()
    live_types = ((lc["live_crawl"] or {}).get("schema_types") if lc and lc["live_crawl"] else []) or []
    s = settings or {}

    return jsonify(
        {
            "base_url": base_url,
            "robots": {
                "proposed": gen.generate_robots_liquid(),
                "current": robots_state,
                "published_status": published.get("robots_txt"),
                "impact": {"signal": "AI crawlability", "current": crawl_current, "projected": crawl_after_robots},
            },
            "llms": {
                "proposed": gen.generate_llms_txt(store_id),
                "current": llms_state,
                "published_status": published.get("llms_txt"),
                "impact": {"signal": "AI crawlability", "current": crawl_current, "projected": crawl_current, "note": "optional"},
            },
            "bot_check": {
                "results": bot_results,
                "checked_at": bot_checked_at.isoformat() if bot_checked_at else None,
                "ok": ok_bots,
                "tested": tested_bots,
            },
            "site_schema": {
                "proposed_org": org,
                "proposed_snippet": site_snippet,
                "update_available": schema_update_available,
                "renders_item_schema": site_asset["renders_item_schema"],
                "org": {
                    "name": s.get("organization_name") or "",
                    "logo_url": s.get("organization_logo_url") or "",
                    "url": s.get("organization_url") or "",
                    "description": s.get("organization_description") or "",
                    "email": s.get("organization_email") or "",
                    "phone": s.get("organization_phone") or "",
                    "same_as": s.get("organization_same_as") or [],
                    "knows_about": s.get("organization_knows_about") or [],
                    "configured": org_configured,
                },
                "published_status": published.get("site_schema"),
                "current_live": {
                    "organization": "Organization" in live_types,
                    "breadcrumb": "BreadcrumbList" in live_types,
                },
                "impact": {"signal": "Structured data", "current_avg": sd_avg, "adds": ["Organization", "BreadcrumbList"]},
                "entity_authority_avg": ea_avg,
            },
            "a11y": {
                "proposed": gen.generate_a11y_snippet(),
                "published_status": published.get("a11y"),
            },
            "indexnow": indexnow_service.status(store_id),
        }
    )


@ai_seo_bp.route("/api/ai-seo/aeo/bot-check", methods=["POST"])
def aeo_bot_check():
    """Force-refresh the live AI-crawler access verification."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
    if not store:
        return jsonify({"error": "Store not found"}), 404
    base_url = _store_base_url(dict(store))
    if not base_url:
        return jsonify({"error": "Store has no domain configured"}), 400
    try:
        results = _run_bot_check(int(store_id), base_url)
    except Exception as e:
        logger.exception("Bot check failed: %s", e)
        return jsonify({"error": str(e)}), 502
    ok, tested = ai_seo_analyzer.summarize_bot_access(results)
    return jsonify({"ok": True, "results": results, "verified": ok, "tested": tested})


@ai_seo_bp.route("/api/ai-seo/aeo/robots/apply", methods=["POST"])
def aeo_robots_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    content = gen.generate_robots_liquid()
    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "No main theme found"}), 502
        client.upsert_theme_asset(theme_id, "templates/robots.txt.liquid", content)
        _save_aeo_asset(int(store_id), "robots_txt", content, "published")
    except Exception as e:
        logger.exception("robots.txt apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "message": "robots.txt.liquid published to theme"})


@ai_seo_bp.route("/api/ai-seo/aeo/llms/apply", methods=["POST"])
def aeo_llms_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    content = gen.generate_llms_txt(int(store_id))
    try:
        existing = client.get_page_by_handle("llms")
        if existing:
            # Update in place — creating again would mint llms-1, llms-2, ...
            page = client.update_page(existing["id"], {
                "title": "llms.txt",
                "body": f"<pre>{content}</pre>",
                "is_published": True,
            })
        else:
            page = client.create_page(
                title="llms.txt",
                body_html=f"<pre>{content}</pre>",
                handle="llms",
                seo_title="llms.txt",
                seo_description="AI crawler content index",
            )
        _save_aeo_asset(int(store_id), "llms_txt", content, "published")
    except Exception as e:
        logger.exception("llms.txt page apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify(
        {
            "ok": True,
            "message": "Published to /pages/llms. Note: Shopify cannot serve /llms.txt at the domain root — "
            "download the file below to host it at the root via your CDN/proxy if desired.",
            "page": page,
        }
    )


_SCHEMA_SNIPPET_KEY = "snippets/ai-seo-schema.liquid"
_SCHEMA_RENDER_TAG = "{% render 'ai-seo-schema' %}"
_SITE_SCHEMA_TYPES = ["Organization", "BreadcrumbList", "WebSite"]
_A11Y_SNIPPET_KEY = "snippets/ai-seo-a11y.liquid"
_A11Y_RENDER_TAG = "{% render 'ai-seo-a11y' %}"


def _inject_render_into_layout(layout, render_tag, anchor="</head>"):
    """Idempotently add the render tag to layout/theme.liquid before an anchor."""
    if not layout or render_tag in layout:
        return layout, False
    if anchor in layout:
        return layout.replace(anchor, f"  {render_tag}\n{anchor}", 1), True
    # Anchor missing (unusual) — prepend so it still renders.
    return render_tag + "\n" + layout, True


def _remove_render_from_layout(layout, render_tag):
    if not layout:
        return layout
    return layout.replace(f"  {render_tag}\n", "").replace(render_tag, "")


@ai_seo_bp.route("/api/ai-seo/aeo/schema/apply", methods=["POST"])
def aeo_schema_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    snippet = gen.generate_site_schema_snippet(int(store_id))
    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "No main theme found"}), 502
        client.upsert_theme_asset(theme_id, _SCHEMA_SNIPPET_KEY, snippet)
        layout = client.get_theme_asset(theme_id, "layout/theme.liquid")
        new_layout, changed = _inject_render_into_layout(layout, _SCHEMA_RENDER_TAG)
        if changed:
            client.upsert_theme_asset(theme_id, "layout/theme.liquid", new_layout)
        _save_aeo_asset(
            int(store_id),
            "site_schema",
            json.dumps({
                "types": _SITE_SCHEMA_TYPES,
                "renders_item_schema": True,
                "snippet_version": gen.SNIPPET_VERSION,
                "snippet_sha": hashlib.sha256(snippet.encode()).hexdigest(),
            }),
            "published",
        )
    except Exception as e:
        logger.exception("Site schema apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify(
        {
            "ok": True,
            "message": "Site-wide Organization + WebSite schema and dynamic BreadcrumbList "
            "published to your theme. Re-scan to reflect it in scores.",
        }
    )


@ai_seo_bp.route("/api/ai-seo/aeo/schema/unpublish", methods=["POST"])
def aeo_schema_unpublish():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    try:
        theme_id = client.get_main_theme_id()
        if theme_id:
            layout = client.get_theme_asset(theme_id, "layout/theme.liquid")
            if layout and _SCHEMA_RENDER_TAG in layout:
                cleaned = layout.replace(f"  {_SCHEMA_RENDER_TAG}\n", "").replace(_SCHEMA_RENDER_TAG, "")
                client.upsert_theme_asset(theme_id, "layout/theme.liquid", cleaned)
            client.delete_theme_asset(theme_id, _SCHEMA_SNIPPET_KEY)
        _save_aeo_asset(int(store_id), "site_schema", "[]", "draft")
    except Exception as e:
        logger.exception("Site schema unpublish failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "message": "Site-wide schema removed from theme and restored."})


@ai_seo_bp.route("/api/ai-seo/aeo/a11y/apply", methods=["POST"])
def aeo_a11y_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    snippet = gen.generate_a11y_snippet()
    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "No main theme found"}), 502
        client.upsert_theme_asset(theme_id, _A11Y_SNIPPET_KEY, snippet)
        layout = client.get_theme_asset(theme_id, "layout/theme.liquid")
        new_layout, changed = _inject_render_into_layout(layout, _A11Y_RENDER_TAG, anchor="</body>")
        if changed:
            client.upsert_theme_asset(theme_id, "layout/theme.liquid", new_layout)
        _save_aeo_asset(int(store_id), "a11y", snippet, "published")
    except Exception as e:
        logger.exception("Accessibility helper apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "message": "Accessibility helper published to your theme — links without discernible text are now labeled."})


@ai_seo_bp.route("/api/ai-seo/aeo/a11y/unpublish", methods=["POST"])
def aeo_a11y_unpublish():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    try:
        theme_id = client.get_main_theme_id()
        if theme_id:
            layout = client.get_theme_asset(theme_id, "layout/theme.liquid")
            if layout and _A11Y_RENDER_TAG in layout:
                client.upsert_theme_asset(theme_id, "layout/theme.liquid", _remove_render_from_layout(layout, _A11Y_RENDER_TAG))
            client.delete_theme_asset(theme_id, _A11Y_SNIPPET_KEY)
        _save_aeo_asset(int(store_id), "a11y", "", "draft")
    except Exception as e:
        logger.exception("Accessibility helper unpublish failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "message": "Accessibility helper removed from theme."})


@ai_seo_bp.route("/api/ai-seo/indexnow/setup", methods=["POST"])
def indexnow_setup():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    try:
        result = indexnow_service.setup_key(int(store_id))
    except Exception as e:
        logger.exception("IndexNow setup failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, **result})


@ai_seo_bp.route("/api/ai-seo/indexnow/submit-all", methods=["POST"])
def indexnow_submit_all():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    try:
        result = indexnow_service.submit_all(int(store_id))
    except Exception as e:
        logger.exception("IndexNow submit-all failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


@ai_seo_bp.route("/api/ai-seo/indexnow/unpublish", methods=["POST"])
def indexnow_unpublish():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    try:
        indexnow_service.remove_key(int(store_id))
    except Exception as e:
        logger.exception("IndexNow unpublish failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "message": "IndexNow key page removed."})


@ai_seo_bp.route("/api/ai-seo/aeo/org-settings", methods=["GET", "POST"])
def aeo_org_settings():
    store_id = request.args.get("store_id") or (request.get_json(silent=True) or {}).get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    store_id = int(store_id)

    if request.method == "GET":
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
            s = cur.fetchone()
        return jsonify(
            {
                "name": (s and s.get("organization_name")) or "",
                "logo_url": (s and s.get("organization_logo_url")) or "",
                "url": (s and s.get("organization_url")) or "",
                "description": (s and s.get("organization_description")) or "",
                "email": (s and s.get("organization_email")) or "",
                "phone": (s and s.get("organization_phone")) or "",
                "same_as": (s and s.get("organization_same_as")) or [],
                "knows_about": (s and s.get("organization_knows_about")) or [],
            }
        )

    data = request.get_json(silent=True) or {}
    same_as = [u.strip() for u in (data.get("same_as") or []) if isinstance(u, str) and u.strip()]
    knows = [k.strip() for k in (data.get("knows_about") or []) if isinstance(k, str) and k.strip()]
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO schema_settings
                (store_id, organization_name, organization_logo_url, organization_url,
                 organization_description, organization_email, organization_phone,
                 organization_same_as, organization_knows_about, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (store_id) DO UPDATE SET
                organization_name = EXCLUDED.organization_name,
                organization_logo_url = EXCLUDED.organization_logo_url,
                organization_url = EXCLUDED.organization_url,
                organization_description = EXCLUDED.organization_description,
                organization_email = EXCLUDED.organization_email,
                organization_phone = EXCLUDED.organization_phone,
                organization_same_as = EXCLUDED.organization_same_as,
                organization_knows_about = EXCLUDED.organization_knows_about,
                updated_at = NOW()
            """,
            (
                store_id, (data.get("name") or "").strip() or None,
                (data.get("logo_url") or "").strip() or None,
                (data.get("url") or "").strip() or None,
                (data.get("description") or "").strip() or None,
                (data.get("email") or "").strip() or None,
                (data.get("phone") or "").strip() or None,
                json.dumps(same_as), json.dumps(knows),
            ),
        )
    return jsonify({"ok": True})
