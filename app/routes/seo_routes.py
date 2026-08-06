import base64
import io
import json
import logging
import threading
import time

import requests as http_requests
from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.routes.schema_routes import generate_and_push_product_schema
from app.services.ai_output_guard import (
    clamp_meta_description,
    clamp_meta_title,
    clamp_title,
    guard_shopify_fields,
    sanitize_html,
)
from app.services.task_runner import spawn as spawn_task
from app.services.image_generator import (
    build_seo_alt_text,
    generate_collection_image,
    generate_collection_image_ai,
)
from app.services.technical_seo_analyzer import _extract_keywords
from app.services.seo_analyzer import (
    analyze_collection_seo,
    analyze_product_seo,
    analyze_search_performance,
    compute_unified_score,
)
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

seo_bp = Blueprint("seo", __name__)

# Checks the keyword-alignment analyzer shares with the content analyzer —
# excluded when counting keyword-pillar issues so they aren't double counted.
CONTENT_DEDUP_CHECK_KEYS = {
    "meta_title_length",
    "meta_desc_length",
    "description_word_count",
    "image_alt_coverage",
}


def _count_unified_issues(seo_breakdown, tsa_checks):
    """Issues across both pillars: content checks below their good band plus
    keyword-analysis issues, minus the overlap (content path is canonical)."""
    content_issues = sum(1 for c in (seo_breakdown or {}).values() if c["score"] < 100)
    keyword_issues = 0
    if tsa_checks:
        keyword_issues = sum(
            1 for k, c in tsa_checks.items()
            if k not in CONTENT_DEDUP_CHECK_KEYS and c.get("issue")
        )
    return content_issues + keyword_issues


def _fetch_keyword_analysis(store_id, item_type, item_id):
    """Stored keyword-alignment analysis for the detail endpoints, or None."""
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT score, detected_keywords, checks, issues_count, fixable_count,
                   ai_fixes, keyword_resolution, updated_at
            FROM technical_seo_analyses
            WHERE store_id = %s AND item_type = %s AND item_id = %s
            """,
            (store_id, item_type, item_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "score": row["score"],
        "detected_keywords": row["detected_keywords"],
        "checks": row["checks"],
        "issues_count": row["issues_count"],
        "fixable_count": row["fixable_count"],
        "ai_fixes": row["ai_fixes"],
        "keyword_resolution": row["keyword_resolution"],
        "analyzed_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


def _attach_unified_scores(item, seo, search_perf, kw):
    """Set the unified-score response keys on a detail/list item in place."""
    unified = compute_unified_score(seo, kw["score"] if kw else None, search_perf)
    item["seo_score"] = unified["seo_score"]
    item["on_page_score"] = unified["content_score"]
    item["content_score"] = unified["content_score"]
    item["keyword_score"] = unified["keyword_score"]
    item["search_score"] = unified["search_score"]
    item["search_breakdown"] = unified["search_breakdown"]
    item["pillar_weights"] = unified["pillar_weights"]
    item["issues_count"] = _count_unified_issues(seo["breakdown"], kw["checks"] if kw else None)
    item["fixable_count"] = kw["fixable_count"] if kw else None
    item["keyword_analysis"] = kw


def _guard_ai_content_fields(data):
    """Clamp/sanitize AI-generated fields in an apply payload in place."""
    if data.get("title"):
        data["title"] = clamp_title(data["title"])
    if data.get("description_html"):
        data["description_html"] = sanitize_html(data["description_html"])
    if data.get("meta_title"):
        data["meta_title"] = clamp_meta_title(data["meta_title"])
    if data.get("meta_description"):
        data["meta_description"] = clamp_meta_description(data["meta_description"])


def _get_gsc_metrics_for_store(store_id):
    """Fetch aggregated GSC metrics (last 28 days) keyed by URL path suffix (handle)."""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    date_to = (today - timedelta(days=3)).isoformat()
    date_from = (today - timedelta(days=30)).isoformat()

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
        if not cred or not cred.get("gsc_property"):
            return {}

        cur.execute(
            """SELECT page,
                      SUM(clicks) as clicks,
                      SUM(impressions) as impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY page
               HAVING SUM(impressions) > 0""",
            (store_id, date_from, date_to),
        )
        rows = cur.fetchall()

    base_url = cred["gsc_property"].rstrip("/")
    metrics = {}
    for r in rows:
        path = r["page"].replace(base_url, "").strip("/")
        metrics[path] = {
            "gsc_clicks": float(r["clicks"] or 0),
            "gsc_impressions": float(r["impressions"] or 0),
            "gsc_ctr": round(float(r["ctr"] or 0), 4),
            "gsc_position": round(float(r["position"] or 0), 1),
        }
    return metrics


def _attach_gsc_to_item(item, gsc_metrics, item_type):
    """Attach GSC data to a product/collection dict by handle matching."""
    handle = item.get("handle") or ""
    prefix = "products" if item_type == "product" else "collections"
    path = f"{prefix}/{handle}"
    gsc = gsc_metrics.get(path, {})
    item["gsc_clicks"] = gsc.get("gsc_clicks")
    item["gsc_impressions"] = gsc.get("gsc_impressions")
    item["gsc_ctr"] = gsc.get("gsc_ctr")
    item["gsc_position"] = gsc.get("gsc_position")
    return item


def _get_semrush_metrics_for_store(store_id):
    """Fetch aggregated Semrush keyword data keyed by URL path suffix."""
    from app.services.semrush_import_service import get_semrush_metrics_for_store
    return get_semrush_metrics_for_store(store_id)


def _attach_semrush_to_item(item, semrush_metrics, item_type):
    """Attach Semrush data to a product/collection dict by handle matching."""
    handle = item.get("handle") or ""
    prefix = "products" if item_type == "product" else "collections"
    path = f"{prefix}/{handle}"
    sr = semrush_metrics.get(path, {})
    item["semrush_volume"] = sr.get("semrush_volume")
    item["semrush_position"] = sr.get("semrush_position")
    item["semrush_keyword_count"] = sr.get("semrush_keyword_count")
    return item


_store_baselines_cache = {}


def _get_store_baselines(store_id):
    """Get impression percentiles for a store to normalize search visibility scores."""
    import time
    from datetime import datetime, timedelta, timezone

    now = time.time()
    cached = _store_baselines_cache.get(store_id)
    if cached and now - cached["ts"] < 3600:
        return cached["data"]

    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()

    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY impr) as p50,
                          percentile_cont(0.75) WITHIN GROUP (ORDER BY impr) as p75,
                          percentile_cont(0.9) WITHIN GROUP (ORDER BY impr) as p90
                   FROM (
                       SELECT page, SUM(impressions) as impr
                       FROM gsc_daily_metrics
                       WHERE store_id = %s AND date >= %s AND date <= %s
                       GROUP BY page
                       HAVING SUM(impressions) > 0
                   ) sub""",
                (store_id, date_from, date_to),
            )
            row = cur.fetchone()

        if row and row.get("p90"):
            data = {
                "p50": float(row["p50"] or 100),
                "p75": float(row["p75"] or 500),
                "p90": float(row["p90"] or 1000),
            }
        else:
            data = None
    except Exception:
        logger.warning("Failed to compute store baselines for store %s", store_id)
        data = None

    # Store-calibrated expected-CTR curve rides along with the baselines so
    # analyze_search_performance gets it without signature churn.
    from app.services.ctr_benchmarks import get_expected_ctr_curve
    curve = get_expected_ctr_curve(store_id)
    if data is None:
        data = {"ctr_curve": curve}
    else:
        data["ctr_curve"] = curve

    _store_baselines_cache[store_id] = {"ts": now, "data": data}
    return data


@seo_bp.route("/seo")
def seo_page():
    return render_template("seo.html")


@seo_bp.route("/api/seo/products", methods=["GET"])
def list_products_seo():
    store_id = request.args.get("store_id")
    status = request.args.get("status", "").strip().upper()
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {"title": "sp.title", "store": "s.name", "status": "sp.status", "last_optimized_at": "sp.last_optimized_at"}
    order_clause = "ORDER BY sp.title"
    if sort_by in valid_sql_sorts:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {valid_sql_sorts[sort_by]} {direction}"

    valid_statuses = {"ACTIVE", "DRAFT", "ARCHIVED"}
    conditions = []
    params = []

    if store_id:
        conditions.append("sp.store_id = %s")
        params.append(store_id)
    if status in valid_statuses:
        conditions.append("sp.status = %s")
        params.append(status)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT sp.*, COALESCE(s.shop_name, s.name) as store_name,
                   tsa.score AS _kw_score, tsa.checks AS _kw_checks,
                   tsa.updated_at AS _kw_analyzed_at
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            LEFT JOIN technical_seo_analyses tsa
              ON tsa.store_id = sp.store_id AND tsa.item_type = 'product' AND tsa.item_id = sp.id
            {where_clause}
            {order_clause}
            """,
            tuple(params),
        )
        products = cur.fetchall()

    store_ids = set(p["store_id"] for p in products)
    gsc_metrics = {}
    semrush_metrics = {}
    store_baselines = {}
    for sid in store_ids:
        gsc_metrics.update(_get_gsc_metrics_for_store(sid))
        semrush_metrics.update(_get_semrush_metrics_for_store(sid))
        store_baselines[sid] = _get_store_baselines(sid)

    result = []
    for p in products:
        seo = analyze_product_seo(dict(p))
        item = {
            "id": p["id"],
            "store_id": p["store_id"],
            "store_name": p["store_name"],
            "shopify_id": p["shopify_id"],
            "title": p["title"],
            "handle": p["handle"],
            "status": p["status"],
            "meta_title": p["meta_title"],
            "meta_description": p["meta_description"],
            "images": p["images"],
            "seo_breakdown": seo["breakdown"],
            "last_optimized_at": p["last_optimized_at"].isoformat() if p["last_optimized_at"] else None,
        }
        _attach_gsc_to_item(item, gsc_metrics, "product")
        _attach_semrush_to_item(item, semrush_metrics, "product")

        gsc_for_scoring = {
            "gsc_clicks": item.get("gsc_clicks"),
            "gsc_impressions": item.get("gsc_impressions"),
            "gsc_ctr": item.get("gsc_ctr"),
            "gsc_position": item.get("gsc_position"),
        }
        sr_for_scoring = {
            "semrush_volume": item.get("semrush_volume"),
            "semrush_position": item.get("semrush_position"),
            "semrush_keyword_count": item.get("semrush_keyword_count"),
        }
        search_perf = analyze_search_performance(gsc_for_scoring, sr_for_scoring, store_baselines.get(p["store_id"]))
        unified = compute_unified_score(seo, p.get("_kw_score"), search_perf)

        item["seo_score"] = unified["seo_score"]
        item["on_page_score"] = unified["content_score"]
        item["content_score"] = unified["content_score"]
        item["keyword_score"] = unified["keyword_score"]
        item["search_score"] = unified["search_score"]
        item["search_breakdown"] = unified["search_breakdown"]
        item["pillar_weights"] = unified["pillar_weights"]
        item["issues_count"] = _count_unified_issues(seo["breakdown"], p.get("_kw_checks"))
        item["keyword_analyzed_at"] = p["_kw_analyzed_at"].isoformat() if p.get("_kw_analyzed_at") else None

        result.append(item)

    if sort_by in ("seo_score", "on_page_score", "content_score", "keyword_score", "issues_count", "search_score", "gsc_clicks", "gsc_impressions", "gsc_position", "semrush_volume", "semrush_position"):
        reverse = sort_dir == "desc"
        result.sort(key=lambda x: x.get(sort_by) or 0, reverse=reverse)

    return jsonify(result)


@seo_bp.route("/api/seo/priorities", methods=["GET"])
def seo_priorities():
    from app.services.seo_priority_service import get_priority_ranked_items

    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    item_type = request.args.get("type", "product")
    limit = min(request.args.get("limit", 50, type=int) or 50, 200)

    try:
        results = get_priority_ranked_items(int(store_id), item_type=item_type, limit=limit)
        return jsonify(results)
    except Exception as e:
        logger.exception("Priority ranking failed: %s", e)
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/optimized", methods=["GET"])
def recently_optimized():
    """List recently optimized items with before/after scores and GSC impact."""
    from datetime import datetime, timedelta, timezone

    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    store_id = int(store_id)
    limit = min(request.args.get("limit", 30, type=int) or 30, 100)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ce.*, ce.created_at as optimized_at
               FROM seo_change_events ce
               WHERE ce.store_id = %s
               ORDER BY ce.created_at DESC
               LIMIT %s""",
            (store_id, limit),
        )
        events = cur.fetchall()

    if not events:
        return jsonify([])

    today = datetime.now(timezone.utc).date()
    gsc_metrics = _get_gsc_metrics_for_store(store_id)
    sr_metrics = _get_semrush_metrics_for_store(store_id)

    results = []
    seen = set()
    for ev in events:
        key = f"{ev['item_type']}:{ev['item_id']}"
        if key in seen:
            continue
        seen.add(key)

        table = "synced_products" if ev["item_type"] == "product" else "synced_collections"
        prefix = "products" if ev["item_type"] == "product" else "collections"
        with db.get_cursor() as cur:
            cur.execute(f"SELECT id, title, handle, last_optimized_at FROM {table} WHERE id = %s", (ev["item_id"],))
            item = cur.fetchone()
        if not item:
            continue

        handle = item.get("handle") or ""
        path = f"{prefix}/{handle}"
        gsc = gsc_metrics.get(path, {})

        days_since = (today - ev["optimized_at"].date()).days if ev["optimized_at"] else 0

        # Get current scores
        seo_fn = analyze_product_seo if ev["item_type"] == "product" else analyze_collection_seo
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (ev["item_id"],))
            full_item = cur.fetchone()
        current_seo = seo_fn(dict(full_item)) if full_item else {"score": 0}

        sr = sr_metrics.get(path, {})
        search_perf = analyze_search_performance(
            {"gsc_clicks": gsc.get("gsc_clicks"), "gsc_impressions": gsc.get("gsc_impressions"),
             "gsc_ctr": gsc.get("gsc_ctr"), "gsc_position": gsc.get("gsc_position")},
            {"semrush_volume": sr.get("semrush_volume"), "semrush_position": sr.get("semrush_position"),
             "semrush_keyword_count": sr.get("semrush_keyword_count")},
            _get_store_baselines(store_id),
        )
        # Unified blend; historical *_before values were recorded on the old
        # composite blend — deltas normalize as new events accrue.
        kw = _fetch_keyword_analysis(store_id, ev["item_type"], ev["item_id"])
        unified = compute_unified_score(current_seo, kw["score"] if kw else None, search_perf)

        score_before = ev.get("composite_score_before") or ev.get("seo_score_before") or 0
        score_now = unified["seo_score"]
        score_change = score_now - score_before

        results.append({
            "id": item["id"],
            "title": item["title"],
            "handle": handle,
            "item_type": ev["item_type"],
            "optimized_at": ev["optimized_at"].isoformat() if ev["optimized_at"] else None,
            "days_since": days_since,
            "score_before": score_before,
            "score_now": score_now,
            "score_change": score_change,
            "gsc_clicks": gsc.get("gsc_clicks"),
            "gsc_impressions": gsc.get("gsc_impressions"),
            "gsc_ctr": gsc.get("gsc_ctr"),
            "gsc_position": gsc.get("gsc_position"),
            "change_type": ev.get("change_type"),
        })

    return jsonify(results)


@seo_bp.route("/api/seo/actions/preview", methods=["POST"])
def preview_seo_action():
    """Generate a preview of what an action will do. User confirms before apply."""
    data = request.get_json(silent=True) or {}
    action_type = data.get("type")
    store_id = data.get("store_id")

    if not action_type or not store_id:
        return jsonify({"error": "type and store_id required"}), 400

    try:
        if action_type == "internal_linking":
            return _preview_internal_linking(data, int(store_id))
        elif action_type == "image_alt_text":
            return _preview_image_alt_text(data, int(store_id))
        elif action_type == "faq_schema":
            return _preview_faq_schema(data, int(store_id))
        else:
            return jsonify({"error": f"Unknown action type: {action_type}"}), 400
    except Exception as e:
        logger.exception("Action preview failed: %s", e)
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/actions/apply", methods=["POST"])
def apply_seo_action():
    """Apply a previously previewed action with user-confirmed data."""
    data = request.get_json(silent=True) or {}
    action_type = data.get("type")
    store_id = data.get("store_id")

    if not action_type or not store_id:
        return jsonify({"error": "type and store_id required"}), 400

    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
            store = cur.fetchone()
        if not store:
            return jsonify({"error": "Store not found"}), 404

        client = ShopifyClient(
            store["domain"],
            decrypt_value(store["access_token_enc"]),
            store["api_version"],
        )

        if action_type == "internal_linking":
            return _apply_internal_linking(data, int(store_id), client)
        elif action_type == "image_alt_text":
            return _apply_image_alt_text(data, int(store_id), client)
        elif action_type == "faq_schema":
            return _apply_faq_schema(data, int(store_id), client)
        else:
            return jsonify({"error": f"Unknown action type: {action_type}"}), 400
    except Exception as e:
        logger.exception("Action apply failed: %s", e)
        return jsonify({"error": str(e)}), 500


def _preview_internal_linking(data, store_id):
    """AI selects blog articles for linking — returns preview for user confirmation."""
    import re
    from openai import OpenAI
    from app.ai_agent import _get_ai_config
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS

    anchor_text = data.get("anchor_text", "")
    target_handle = data.get("target_handle", "")
    target_type = data.get("target_type", "collection")

    if not target_handle:
        return jsonify({"error": "target_handle required"}), 400

    prefix = "products" if target_type == "product" else "collections"
    target_url = f"/{prefix}/{target_handle}"

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.id, sa.shopify_id, sa.title, sa.body, sb.handle as blog_handle
               FROM synced_articles sa
               JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.body IS NOT NULL AND sa.body != ''
               ORDER BY sa.title LIMIT 100""",
            (store_id,),
        )
        articles = cur.fetchall()

    if not articles:
        return jsonify({"error": "No blog articles found"}), 404

    # Only exclude articles that have an actual <a href> link to the target URL
    candidates = []
    already_linked = []
    for art in articles:
        body = art["body"] or ""
        if re.search(r'href=["\'][^"\']*' + re.escape(target_url) + r'["\']', body):
            already_linked.append(art["title"])
        else:
            candidates.append(art)

    if not candidates:
        return jsonify({"preview": [], "all_candidates": [], "already_linked": already_linked,
                         "message": "All articles already have a link to this page"})

    config = _get_ai_config()
    ai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=config["api_key"], timeout=60.0)

    summaries = []
    for art in candidates[:30]:
        body_text = re.sub(r"<[^>]+>", "", art["body"] or "")[:300]
        summaries.append({"id": art["id"], "title": art["title"], "snippet": body_text})

    _tpl = get_setting("prompts.action_select_articles", DEFAULTS["prompts.action_select_articles"])
    try:
        prompt = _tpl.format(target_url=target_url, anchor_text=anchor_text or target_handle, articles_json=json.dumps(summaries, indent=1))
    except (KeyError, IndexError):
        prompt = DEFAULTS["prompts.action_select_articles"].format(target_url=target_url, anchor_text=anchor_text or target_handle, articles_json=json.dumps(summaries, indent=1))

    resp = ai_client.chat.completions.create(model=config["model"], messages=[{"role": "user", "content": prompt}], temperature=0.2)
    content = resp.choices[0].message.content or ""
    s, e = content.find("{"), content.rfind("}") + 1
    selected_ids = set()
    if s >= 0 and e > s:
        try:
            selected_ids = set(json.loads(content[s:e]).get("selected_ids", []))
        except (json.JSONDecodeError, ValueError):
            pass

    preview = []
    remaining = []
    for art in candidates:
        entry = {
            "article_id": art["id"],
            "article_shopify_id": art["shopify_id"],
            "article_title": art["title"],
            "blog_handle": art["blog_handle"],
            "anchor_text": anchor_text or target_handle,
            "target_url": target_url,
        }
        if art["id"] in selected_ids:
            preview.append(entry)
        else:
            body_text = re.sub(r"<[^>]+>", "", art["body"] or "")[:200]
            entry["snippet"] = body_text
            remaining.append(entry)

    return jsonify({
        "preview": preview,
        "remaining": remaining[:20],
        "already_linked": already_linked,
        "target_url": target_url,
        "anchor_text": anchor_text or target_handle,
    })


def _apply_internal_linking(data, store_id, client):
    """Apply user-confirmed internal links."""
    from app.ai_agent import _get_ai_config
    from app.routes.interlinking_routes import _inject_link_via_ai, _push_html_to_shopify, _update_local_db

    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items array required"}), 400

    config = _get_ai_config()
    results = []
    for item in items:
        article_id = item.get("article_id")
        article_shopify_id = item.get("article_shopify_id")
        target_url = item.get("target_url")
        anchor_text = item.get("anchor_text")

        with db.get_cursor() as cur:
            cur.execute("SELECT id, body FROM synced_articles WHERE id = %s", (article_id,))
            art = cur.fetchone()
        if not art:
            results.append({"article_id": article_id, "status": "error", "reason": "Article not found"})
            continue

        try:
            from app.services.ai_output_guard import validate_injected_html
            new_html = _inject_link_via_ai(art["body"], [{"url": target_url, "anchor_text": anchor_text}], config)
            ok, reason = validate_injected_html(art["body"], new_html, expected_hrefs=[target_url])
            if not ok:
                results.append({"article_id": article_id, "status": "error", "reason": f"AI output rejected: {reason}"})
                continue
            new_html = sanitize_html(new_html)
            if new_html != art["body"]:
                _push_html_to_shopify(client, "article", article_shopify_id, new_html)
                _update_local_db("article", art["id"], new_html)
                results.append({"article_id": article_id, "status": "applied"})
            else:
                results.append({"article_id": article_id, "status": "skipped", "reason": "No change"})
        except Exception as e:
            results.append({"article_id": article_id, "status": "error", "reason": str(e)})

    applied = sum(1 for r in results if r["status"] == "applied")

    if applied:
        target_url = (items[0].get("target_url") or "").strip("/")
        parts = target_url.split("/")
        if len(parts) == 2 and parts[0] in ("products", "collections"):
            t_type = "product" if parts[0] == "products" else "collection"
            table = "synced_products" if t_type == "product" else "synced_collections"
            with db.get_cursor() as cur:
                cur.execute(f"SELECT * FROM {table} WHERE store_id = %s AND handle = %s", (store_id, parts[1]))
                target = cur.fetchone()
            if target:
                from app.routes.technical_seo_routes import _record_change_event
                _record_change_event(
                    t_type, target,
                    [{"field": "internal_links", "value": f"{applied} inbound link(s) added"}],
                    "internal_linking",
                )
                from app.services.analysis_refresh import refresh_item_analysis
                refresh_item_analysis(store_id, t_type, target["id"])

    return jsonify({"ok": True, "links_added": applied, "results": results, "message": f"Added links in {applied} article(s)"})


def _preview_image_alt_text(data, store_id):
    """Generate alt text suggestions — returns preview for user confirmation."""
    from app.ai_agent import _get_ai_config
    from openai import OpenAI
    from app.settings_manager import get_setting as _gs
    from app.prompt_defaults import DEFAULTS as _defs
    from app.services.ai_content_validator import clean_keyword as clean_alt_keyword
    from app.services.ai_content_validator import strip_keyword_phrase
    from app.services.technical_seo_analyzer import _keyword_in_text as _kw_in_text

    item_id = data.get("item_id")
    scope = data.get("scope", "single")

    if scope == "collection_products" and item_id:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT sp.id, sp.shopify_id, sp.title, sp.images
                   FROM synced_products sp
                   JOIN collection_product_map cpm ON cpm.product_shopify_id = sp.shopify_id AND cpm.store_id = sp.store_id
                   JOIN synced_collections sc ON sc.shopify_id = cpm.collection_shopify_id AND sc.store_id = cpm.store_id
                   WHERE sc.id = %s AND sp.store_id = %s AND sp.status = 'ACTIVE'""",
                (item_id, store_id),
            )
            products = cur.fetchall()
    elif item_id:
        with db.get_cursor() as cur:
            cur.execute("SELECT id, shopify_id, title, images FROM synced_products WHERE id = %s AND store_id = %s", (item_id, store_id))
            row = cur.fetchone()
        products = [row] if row else []
    else:
        return jsonify({"error": "item_id required"}), 400

    if not products:
        return jsonify({"error": "No products found"}), 404

    config = _get_ai_config()
    ai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=config["api_key"], timeout=60.0)

    recommendation_text = data.get("text", "")

    preview = []
    for product in products:
        images = product.get("images") or []
        if isinstance(images, str):
            images = json.loads(images)

        actionable_images = [img for img in images if img.get("media_id")]
        if not actionable_images:
            continue

        existing_alts = []
        for img in actionable_images:
            existing_alts.append({"position": images.index(img) + 1, "current_alt": img.get("alt") or ""})

        existing_context = "\n".join(
            f"- Image {a['position']}: \"{a['current_alt']}\"" if a["current_alt"]
            else f"- Image {a['position']}: (no alt text)"
            for a in existing_alts
        )
        kw_analysis = _fetch_keyword_analysis(store_id, "product", product["id"])
        primary_kw = clean_alt_keyword(
            (((kw_analysis or {}).get("keyword_resolution") or {}).get("primary") or {}).get("keyword")
        )
        kw_line = (
            f"Primary keyword: \"{primary_kw}\". Include it naturally in ONLY the first (hero) "
            f"image alt. Every other alt must NOT contain it — keyword in every alt reads as "
            f"spam to search engines. "
            if primary_kw else ""
        )
        prompt = (
            f"Generate optimized SEO alt text for ALL images of this product.\n"
            f"Product: {product['title']}\n"
            f"Total images: {len(actionable_images)}\n\n"
            f"Current alt text:\n{existing_context}\n\n"
            f"Recommendation context: {recommendation_text}\n\n"
            f"Return JSON with key 'alts' — an array of strings, one per image in order. "
            f"{kw_line}"
            f"For images with no alt text, write new descriptions; describe what each image "
            f"shows (angle, detail, packaging, lifestyle). Keep each under 125 characters. "
            f"Vary descriptions (front view, side view, detail shot, lifestyle, etc.)."
        )

        try:
            resp = ai_client.chat.completions.create(model=config["model"], messages=[{"role": "user", "content": prompt}], temperature=0.3)
            content = resp.choices[0].message.content or ""
            s, e = content.find("{"), content.rfind("}") + 1
            alts = []
            if s >= 0 and e > s:
                alts = json.loads(content[s:e]).get("alts", [])
            # Deterministic balance backstop: keyword stays in the hero alt only.
            if primary_kw and len(alts) >= 2 and all(
                a and _kw_in_text(primary_kw, a) for a in alts
            ):
                alts = [alts[0]] + [strip_keyword_phrase(a, primary_kw) for a in alts[1:]]

            suggestions = []
            for i, img in enumerate(actionable_images):
                suggestions.append({
                    "media_id": img["media_id"],
                    "image_url": img.get("url", ""),
                    "current_alt": img.get("alt") or "",
                    "suggested_alt": alts[i] if i < len(alts) else f"{product['title']} — image {i+1}",
                })
            preview.append({
                "product_id": product["id"],
                "product_shopify_id": product["shopify_id"],
                "product_title": product["title"],
                "suggestions": suggestions,
            })
        except Exception as e:
            logger.warning("Alt preview failed for product %s: %s", product["title"], e)

    return jsonify({"preview": preview})


def _apply_image_alt_text(data, store_id, client):
    """Apply user-confirmed alt text changes."""
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items array required"}), 400

    results = []
    for item in items:
        product_id = item.get("product_id")
        product_shopify_id = item.get("product_shopify_id")
        updates = item.get("updates", [])

        if not updates:
            continue

        try:
            media_updates = [{"media_id": u["media_id"], "alt": u["alt"]} for u in updates]
            client.update_product_media(product_shopify_id, media_updates)

            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
                row = cur.fetchone()
            if row:
                from app.routes.technical_seo_routes import _record_change_event
                _record_change_event(
                    "product", row,
                    [{"field": "image_alt", "value": f"{len(updates)} image(s) updated"}],
                    "image_alt_text",
                )
                images = row.get("images") or []
                if isinstance(images, str):
                    images = json.loads(images)
                alt_map = {u["media_id"]: u["alt"] for u in updates}
                for img in images:
                    if img.get("media_id") in alt_map:
                        img["alt"] = alt_map[img["media_id"]]
                with db.get_cursor() as cur:
                    cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), product_id))
                # Re-analyze so Issues/Checks stop flagging the alts just fixed
                from app.services.analysis_refresh import refresh_item_analysis
                refresh_item_analysis(store_id, "product", product_id)

            results.append({"product_id": product_id, "status": "applied", "count": len(updates)})
        except Exception as e:
            results.append({"product_id": product_id, "status": "error", "reason": str(e)})

    applied = sum(1 for r in results if r["status"] == "applied")
    total_imgs = sum(r.get("count", 0) for r in results if r["status"] == "applied")
    return jsonify({"ok": True, "products_updated": applied, "images_updated": total_imgs, "results": results,
                     "message": f"Updated {total_imgs} image(s) across {applied} product(s)"})


def _preview_faq_schema(data, store_id):
    """Generate FAQ Q&A pairs — returns preview for user confirmation."""
    from app.ai_agent import _get_ai_config
    from openai import OpenAI
    from app.settings_manager import get_setting as _gs
    from app.prompt_defaults import DEFAULTS as _defs

    questions = data.get("questions", [])
    item_type = data.get("item_type", "collection")
    item_id = data.get("item_id")

    if not item_id:
        return jsonify({"error": "item_id required"}), 400

    table = "synced_products" if item_type == "product" else "synced_collections"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT shopify_id, title FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404

    if not questions:
        questions = [f"What is {item['title']}?", f"Where to buy {item['title']}?"]

    config = _get_ai_config()
    ai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=config["api_key"], timeout=60.0)

    questions_text = "\n".join(f"- {q}" for q in questions)
    _tpl = _gs("prompts.action_generate_faq", _defs["prompts.action_generate_faq"])
    try:
        prompt = _tpl.format(item_title=item["title"], questions_text=questions_text)
    except (KeyError, IndexError):
        prompt = _defs["prompts.action_generate_faq"].format(item_title=item["title"], questions_text=questions_text)

    resp = ai_client.chat.completions.create(model=config["model"], messages=[{"role": "user", "content": prompt}], temperature=0.3)
    content = resp.choices[0].message.content or ""
    s, e = content.find("{"), content.rfind("}") + 1
    faqs = []
    if s >= 0 and e > s:
        faqs = json.loads(content[s:e]).get("faqs", [])

    return jsonify({"preview": faqs, "item_title": item["title"], "item_shopify_id": item["shopify_id"], "item_type": item_type})


def _apply_faq_schema(data, store_id, client):
    """Apply user-confirmed FAQ schema."""
    faqs = data.get("faqs", [])
    item_shopify_id = data.get("item_shopify_id")
    item_type = data.get("item_type", "collection")
    item_id = data.get("item_id")

    if not faqs or not item_shopify_id:
        return jsonify({"error": "faqs and item_shopify_id required"}), 400

    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f["question"], "acceptedAnswer": {"@type": "Answer", "text": f["answer"]}}
            for f in faqs
        ],
    }

    client.set_product_metafield(item_shopify_id, "seo", "faq_schema_json_ld", json.dumps(faq_schema), "json")

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, pushed_to_shopify, pushed_at)
               VALUES (%s, %s, %s, 'FAQPage', %s, TRUE, TRUE, NOW())
               ON CONFLICT (store_id, resource_id, schema_type) DO UPDATE
               SET schema_json = EXCLUDED.schema_json, pushed_to_shopify = TRUE, pushed_at = NOW()""",
            (store_id, str(item_shopify_id), item_type, json.dumps(faq_schema)),
        )

    if item_id:
        table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
            target = cur.fetchone()
        if target:
            from app.routes.technical_seo_routes import _record_change_event
            _record_change_event(
                item_type, target,
                [{"field": "faq_schema", "value": f"{len(faqs)} FAQ(s) pushed"}],
                "faq_schema",
            )
            from app.services.analysis_refresh import refresh_item_analysis
            refresh_item_analysis(store_id, item_type, item_id)

    return jsonify({"ok": True, "faqs_count": len(faqs), "message": f"Pushed {len(faqs)} FAQ(s) as schema markup"})


@seo_bp.route("/api/seo/impact", methods=["GET"])
def seo_impact():
    from datetime import datetime, timedelta, timezone

    store_id = request.args.get("store_id")
    item_id = request.args.get("item_id")
    item_type = request.args.get("item_type", "product")

    if not store_id or not item_id:
        return jsonify({"error": "store_id and item_id required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT * FROM seo_change_events
               WHERE store_id = %s AND item_type = %s AND item_id = %s
               ORDER BY created_at DESC LIMIT 1""",
            (store_id, item_type, item_id),
        )
        event = cur.fetchone()

    if not event:
        # 200 with a status the modal renders — a 404 here made the button
        # toast "Request failed" for items optimized before event tracking.
        return jsonify({"status": "no_history"})

    change_date = event["created_at"]
    days_since = (datetime.now(timezone.utc) - change_date.replace(tzinfo=timezone.utc)).days

    before_start = (change_date - timedelta(days=28)).date().isoformat()
    before_end = change_date.date().isoformat()
    after_start = change_date.date().isoformat()
    after_end = (change_date + timedelta(days=28)).date().isoformat()

    table = "synced_products" if item_type == "product" else "synced_collections"
    prefix = "products" if item_type == "product" else "collections"

    with db.get_cursor() as cur:
        cur.execute(f"SELECT handle, title FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()

    if not cred or not cred.get("gsc_property"):
        return jsonify({
            "item_title": item["title"],
            "optimization_date": change_date.isoformat(),
            "days_since": days_since,
            "status": "no_gsc_data",
            "scores_before": {
                "on_page": event["seo_score_before"],
                "search": event["search_score_before"],
                "composite": event["composite_score_before"],
            },
        })

    base_url = cred["gsc_property"].rstrip("/")
    page_url = f"{base_url}/{prefix}/{item['handle']}"

    def _get_period_metrics(d_from, d_to):
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT SUM(clicks) as clicks, SUM(impressions) as impressions,
                          CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                          SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND page = %s AND date >= %s AND date <= %s""",
                (store_id, page_url, d_from, d_to),
            )
            return cur.fetchone()

    before = _get_period_metrics(before_start, before_end)
    after = _get_period_metrics(after_start, after_end)

    def _safe_float(val):
        return float(val) if val else 0

    before_data = {
        "clicks": int(_safe_float(before["clicks"])) if before else 0,
        "impressions": int(_safe_float(before["impressions"])) if before else 0,
        "ctr": round(_safe_float(before["ctr"]), 4) if before else 0,
        "position": round(_safe_float(before["position"]), 1) if before else 0,
    }
    after_data = {
        "clicks": int(_safe_float(after["clicks"])) if after else 0,
        "impressions": int(_safe_float(after["impressions"])) if after else 0,
        "ctr": round(_safe_float(after["ctr"]), 4) if after else 0,
        "position": round(_safe_float(after["position"]), 1) if after else 0,
    }

    click_delta = after_data["clicks"] - before_data["clicks"]
    impr_delta = after_data["impressions"] - before_data["impressions"]
    pos_delta = before_data["position"] - after_data["position"]

    if days_since < 14:
        status = "too_early"
    elif click_delta > 0 and pos_delta > 0:
        status = "improving"
    elif click_delta < 0 and pos_delta < -1:
        status = "declining"
    else:
        status = "stable"

    return jsonify({
        "item_title": item["title"],
        "optimization_date": change_date.isoformat(),
        "days_since": days_since,
        "before": before_data,
        "after": after_data,
        "delta": {
            "clicks": click_delta,
            "impressions": impr_delta,
            "ctr": round(after_data["ctr"] - before_data["ctr"], 4),
            "position": round(pos_delta, 1),
        },
        "scores_before": {
            "on_page": event["seo_score_before"],
            "search": event["search_score_before"],
            "composite": event["composite_score_before"],
        },
        "status": status,
    })


@seo_bp.route("/api/seo/products/counts", methods=["GET"])
def product_status_counts():
    store_id = request.args.get("store_id")
    with db.get_cursor() as cur:
        if store_id:
            cur.execute(
                "SELECT status, COUNT(*) as cnt FROM synced_products WHERE store_id = %s GROUP BY status",
                (store_id,),
            )
        else:
            cur.execute("SELECT status, COUNT(*) as cnt FROM synced_products GROUP BY status")
        rows = cur.fetchall()

    counts = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return jsonify({"total": total, **counts})


@seo_bp.route("/api/seo/products/<int:product_id>/apply", methods=["POST"])
def apply_product_changes(product_id):
    data = request.get_json(silent=True) or {}
    _guard_ai_content_fields(data)

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sp.*, s.domain, s.access_token_enc, s.api_version
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            WHERE sp.id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    try:
        try:
            seo_before = analyze_product_seo(dict(product))
            gsc_m = _get_gsc_metrics_for_store(product["store_id"])
            sr_m = _get_semrush_metrics_for_store(product["store_id"])
            handle = product.get("handle") or ""
            path = f"products/{handle}"
            gsc_item = gsc_m.get(path, {})
            sr_item = sr_m.get(path, {})
            search_before = analyze_search_performance(
                {"gsc_clicks": gsc_item.get("gsc_clicks"), "gsc_impressions": gsc_item.get("gsc_impressions"),
                 "gsc_ctr": gsc_item.get("gsc_ctr"), "gsc_position": gsc_item.get("gsc_position")},
                {"semrush_volume": sr_item.get("semrush_volume"), "semrush_position": sr_item.get("semrush_position"),
                 "semrush_keyword_count": sr_item.get("semrush_keyword_count")},
                _get_store_baselines(product["store_id"]),
            )
            kw_before = _fetch_keyword_analysis(product["store_id"], "product", product_id)
            unified_before = compute_unified_score(seo_before, kw_before["score"] if kw_before else None, search_before)

            changes_map = {}
            for field in ("title", "description_html", "meta_title", "meta_description"):
                if data.get(field):
                    old_key = "description" if field == "description_html" else field
                    changes_map[field] = {"old": (product.get(old_key) or "")[:200], "new": data[field][:200]}

            # Recorded only after the Shopify push succeeds — an event for a
            # failed push shows up as a phantom "optimization" in impact views.
            _event_params = (
                product["store_id"], product_id,
                data.get("change_type", "ai_optimization"),
                json.dumps(changes_map) if changes_map else None,
                unified_before["content_score"],
                unified_before.get("search_score"),
                unified_before["seo_score"],
            )
        except Exception:
            _event_params = None
            logger.warning("Failed to compute SEO change event for product %s", product_id)

        client = ShopifyClient(
            product["domain"],
            decrypt_value(product["access_token_enc"]),
            product["api_version"],
        )

        updated_fields = []

        product_fields = {}
        if data.get("title"):
            product_fields["title"] = data["title"]
            updated_fields.append("title")
        if data.get("description_html"):
            product_fields["description_html"] = data["description_html"]
            updated_fields.append("description_html")
        if data.get("meta_title"):
            product_fields["meta_title"] = data["meta_title"]
            updated_fields.append("meta_title")
        if data.get("meta_description"):
            product_fields["meta_description"] = data["meta_description"]
            updated_fields.append("meta_description")
        if "tags" in data and isinstance(data["tags"], list):
            product_fields["tags"] = data["tags"]
            updated_fields.append("tags")

        if product_fields:
            client.update_product(product["shopify_id"], product_fields)

        if data.get("image_alts") and isinstance(data["image_alts"], list):
            try:
                from app.services.ai_content_validator import (
                    clean_keyword as _ck,
                    ensure_applied_alt_balance,
                )
                _kw_analysis = _fetch_keyword_analysis(product["store_id"], "product", product_id)
                _pk = _ck((((_kw_analysis or {}).get("keyword_resolution") or {}).get("primary") or {}).get("keyword"))
                _imgs = product.get("images") or []
                if isinstance(_imgs, str):
                    _imgs = json.loads(_imgs)
                data["image_alts"] = ensure_applied_alt_balance(_imgs, data["image_alts"], _pk)
            except Exception:
                logger.exception("Alt balance backstop failed (apply)")
            logger.info("Applying alt text for %d images on product %s", len(data["image_alts"]), product_id)
            result = client.update_product_media(product["shopify_id"], data["image_alts"])
            logger.info("Alt text apply result: %s", result)
            updated_fields.append("image_alts")

        if _event_params:
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO seo_change_events
                       (store_id, item_type, item_id, change_type, changes_applied,
                        seo_score_before, search_score_before, composite_score_before)
                       VALUES (%s, 'product', %s, %s, %s, %s, %s, %s)""",
                    _event_params,
                )

        with db.get_cursor() as cur:
            if data.get("title"):
                cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (data["title"], product_id))
            if data.get("description_html"):
                cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (data["description_html"], product_id))
            if data.get("meta_title"):
                cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (data["meta_title"], product_id))
            if data.get("meta_description"):
                cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (data["meta_description"], product_id))
            if "tags" in data and isinstance(data["tags"], list):
                cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(data["tags"]), product_id))
            if data.get("image_alts"):
                images = product["images"] or []
                if isinstance(images, str):
                    images = json.loads(images)
                alt_map = {a["media_id"]: a["alt"] for a in data["image_alts"] if a.get("media_id")}
                for img in images:
                    if img.get("media_id") in alt_map:
                        img["alt"] = alt_map[img["media_id"]]
                cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), product_id))
            cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (product_id,))

        try:
            generate_and_push_product_schema(product_id, client)
            updated_fields.append("schema")
        except Exception as schema_err:
            logger.warning("Schema push failed for product %s: %s", product_id, schema_err)

        from app.services.analysis_refresh import refresh_item_analysis
        refresh_item_analysis(product["store_id"], "product", product_id)

        return jsonify({"ok": True, "updated_fields": updated_fields})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/products/<int:product_id>", methods=["GET"])
def get_product_seo(product_id):
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sp.*, COALESCE(s.shop_name, s.name) as store_name
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            WHERE sp.id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    seo = analyze_product_seo(dict(product))
    gsc_metrics = _get_gsc_metrics_for_store(product["store_id"])
    semrush_metrics = _get_semrush_metrics_for_store(product["store_id"])

    item = {
        "id": product["id"],
        "store_id": product["store_id"],
        "store_name": product["store_name"],
        "shopify_id": product["shopify_id"],
        "title": product["title"],
        "handle": product["handle"],
        "description": product["description"],
        "vendor": product["vendor"],
        "product_type": product["product_type"],
        "tags": product["tags"],
        "meta_title": product["meta_title"],
        "meta_description": product["meta_description"],
        "images": product["images"],
        "variants": product["variants"],
        "status": product["status"],
        "seo_breakdown": seo["breakdown"],
        "last_optimized_at": product["last_optimized_at"].isoformat() if product.get("last_optimized_at") else None,
    }
    _attach_gsc_to_item(item, gsc_metrics, "product")
    _attach_semrush_to_item(item, semrush_metrics, "product")

    baselines = _get_store_baselines(product["store_id"])
    gsc_for_scoring = {
        "gsc_clicks": item.get("gsc_clicks"),
        "gsc_impressions": item.get("gsc_impressions"),
        "gsc_ctr": item.get("gsc_ctr"),
        "gsc_position": item.get("gsc_position"),
    }
    sr_for_scoring = {
        "semrush_volume": item.get("semrush_volume"),
        "semrush_position": item.get("semrush_position"),
        "semrush_keyword_count": item.get("semrush_keyword_count"),
    }
    search_perf = analyze_search_performance(gsc_for_scoring, sr_for_scoring, baselines)
    kw = _fetch_keyword_analysis(product["store_id"], "product", product_id)
    _attach_unified_scores(item, seo, search_perf, kw)

    return jsonify(item)


@seo_bp.route("/api/seo/collections", methods=["GET"])
def list_collections_seo():
    store_id = request.args.get("store_id")
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {
        "title": "sc.title",
        "store": "s.name",
        "products_count": "sc.products_count",
        "collection_type": "sc.collection_type",
        "published": "sc.published",
        "image_url": "CASE WHEN sc.image_url IS NOT NULL AND sc.image_url != '' THEN 0 ELSE 1 END",
        "last_optimized_at": "sc.last_optimized_at",
    }
    order_clause = "ORDER BY sc.title"
    if sort_by in valid_sql_sorts:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {valid_sql_sorts[sort_by]} {direction}"

    collection_type = request.args.get("collection_type")
    published = request.args.get("published")

    conditions = []
    params = []
    if store_id:
        conditions.append("sc.store_id = %s")
        params.append(store_id)
    if collection_type:
        conditions.append("sc.collection_type = %s")
        params.append(collection_type)
    if published is not None and published != "":
        conditions.append("sc.published = %s")
        params.append(published.lower() == "true")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT sc.*, COALESCE(s.shop_name, s.name) as store_name,
                   tsa.score AS _kw_score, tsa.checks AS _kw_checks,
                   tsa.updated_at AS _kw_analyzed_at
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            LEFT JOIN technical_seo_analyses tsa
              ON tsa.store_id = sc.store_id AND tsa.item_type = 'collection' AND tsa.item_id = sc.id
            {where_clause}
            {order_clause}
            """,
            tuple(params),
        )
        collections = cur.fetchall()

    store_ids = set(c["store_id"] for c in collections)
    gsc_metrics = {}
    semrush_metrics = {}
    store_baselines = {}
    for sid in store_ids:
        gsc_metrics.update(_get_gsc_metrics_for_store(sid))
        semrush_metrics.update(_get_semrush_metrics_for_store(sid))
        store_baselines[sid] = _get_store_baselines(sid)

    result = []
    for c in collections:
        seo = analyze_collection_seo(dict(c))
        item = {
            "id": c["id"],
            "store_id": c["store_id"],
            "store_name": c["store_name"],
            "shopify_id": c["shopify_id"],
            "title": c["title"],
            "handle": c["handle"],
            "meta_title": c["meta_title"],
            "meta_description": c["meta_description"],
            "products_count": c["products_count"],
            "collection_type": c["collection_type"],
            "published": c["published"],
            "image_url": c["image_url"],
            "seo_breakdown": seo["breakdown"],
            "last_optimized_at": c["last_optimized_at"].isoformat() if c["last_optimized_at"] else None,
        }
        _attach_gsc_to_item(item, gsc_metrics, "collection")
        _attach_semrush_to_item(item, semrush_metrics, "collection")

        gsc_for_scoring = {
            "gsc_clicks": item.get("gsc_clicks"),
            "gsc_impressions": item.get("gsc_impressions"),
            "gsc_ctr": item.get("gsc_ctr"),
            "gsc_position": item.get("gsc_position"),
        }
        sr_for_scoring = {
            "semrush_volume": item.get("semrush_volume"),
            "semrush_position": item.get("semrush_position"),
            "semrush_keyword_count": item.get("semrush_keyword_count"),
        }
        search_perf = analyze_search_performance(gsc_for_scoring, sr_for_scoring, store_baselines.get(c["store_id"]))
        unified = compute_unified_score(seo, c.get("_kw_score"), search_perf)

        item["seo_score"] = unified["seo_score"]
        item["on_page_score"] = unified["content_score"]
        item["content_score"] = unified["content_score"]
        item["keyword_score"] = unified["keyword_score"]
        item["search_score"] = unified["search_score"]
        item["search_breakdown"] = unified["search_breakdown"]
        item["pillar_weights"] = unified["pillar_weights"]
        item["issues_count"] = _count_unified_issues(seo["breakdown"], c.get("_kw_checks"))
        item["keyword_analyzed_at"] = c["_kw_analyzed_at"].isoformat() if c.get("_kw_analyzed_at") else None

        result.append(item)

    if sort_by in ("seo_score", "on_page_score", "content_score", "keyword_score", "issues_count", "search_score", "gsc_clicks", "gsc_impressions", "gsc_position", "semrush_volume", "semrush_position"):
        reverse = sort_dir == "desc"
        result.sort(key=lambda x: x.get(sort_by) or 0, reverse=reverse)

    return jsonify(result)


@seo_bp.route("/api/seo/collections/<int:collection_id>/apply", methods=["POST"])
def apply_collection_changes(collection_id):
    data = request.get_json(silent=True) or {}
    _guard_ai_content_fields(data)

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sc.*, s.domain, s.access_token_enc, s.api_version
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id = %s
            """,
            (collection_id,),
        )
        collection = cur.fetchone()

    if not collection:
        return jsonify({"error": "Collection not found"}), 404

    try:
        try:
            from app.services.seo_analyzer import analyze_collection_seo as _analyze_coll
            seo_before = _analyze_coll(dict(collection))
            gsc_m = _get_gsc_metrics_for_store(collection["store_id"])
            sr_m = _get_semrush_metrics_for_store(collection["store_id"])
            handle = collection.get("handle") or ""
            path = f"collections/{handle}"
            gsc_item = gsc_m.get(path, {})
            sr_item = sr_m.get(path, {})
            search_before = analyze_search_performance(
                {"gsc_clicks": gsc_item.get("gsc_clicks"), "gsc_impressions": gsc_item.get("gsc_impressions"),
                 "gsc_ctr": gsc_item.get("gsc_ctr"), "gsc_position": gsc_item.get("gsc_position")},
                {"semrush_volume": sr_item.get("semrush_volume"), "semrush_position": sr_item.get("semrush_position"),
                 "semrush_keyword_count": sr_item.get("semrush_keyword_count")},
                _get_store_baselines(collection["store_id"]),
            )
            kw_before = _fetch_keyword_analysis(collection["store_id"], "collection", collection_id)
            unified_before = compute_unified_score(seo_before, kw_before["score"] if kw_before else None, search_before)

            changes_map = {}
            for field in ("title", "description_html", "meta_title", "meta_description"):
                if data.get(field):
                    old_key = "description" if field == "description_html" else field
                    changes_map[field] = {"old": (collection.get(old_key) or "")[:200], "new": data[field][:200]}

            _event_params = (
                collection["store_id"], collection_id,
                data.get("change_type", "ai_optimization"),
                json.dumps(changes_map) if changes_map else None,
                unified_before["content_score"],
                unified_before.get("search_score"),
                unified_before["seo_score"],
            )
        except Exception:
            _event_params = None
            logger.warning("Failed to compute SEO change event for collection %s", collection_id)

        client = ShopifyClient(
            collection["domain"],
            decrypt_value(collection["access_token_enc"]),
            collection["api_version"],
        )

        updated_fields = []

        fields = {}
        if data.get("title"):
            fields["title"] = data["title"]
            updated_fields.append("title")
        if data.get("description_html"):
            fields["description_html"] = data["description_html"]
            updated_fields.append("description_html")
        if data.get("meta_title"):
            fields["meta_title"] = data["meta_title"]
            updated_fields.append("meta_title")
        if data.get("meta_description"):
            fields["meta_description"] = data["meta_description"]
            updated_fields.append("meta_description")

        if fields:
            client.update_collection(collection["shopify_id"], fields)

        if _event_params:
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO seo_change_events
                       (store_id, item_type, item_id, change_type, changes_applied,
                        seo_score_before, search_score_before, composite_score_before)
                       VALUES (%s, 'collection', %s, %s, %s, %s, %s, %s)""",
                    _event_params,
                )

        with db.get_cursor() as cur:
            if data.get("title"):
                cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (data["title"], collection_id))
            if data.get("description_html"):
                cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (data["description_html"], collection_id))
            if data.get("meta_title"):
                cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (data["meta_title"], collection_id))
            if data.get("meta_description"):
                cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (data["meta_description"], collection_id))
            cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (collection_id,))

        from app.services.analysis_refresh import refresh_item_analysis
        refresh_item_analysis(collection["store_id"], "collection", collection_id)

        return jsonify({"ok": True, "updated_fields": updated_fields})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/collections/<int:collection_id>", methods=["GET"])
def get_collection_seo(collection_id):
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sc.*, COALESCE(s.shop_name, s.name) as store_name
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id = %s
            """,
            (collection_id,),
        )
        collection = cur.fetchone()

    if not collection:
        return jsonify({"error": "Collection not found"}), 404

    seo = analyze_collection_seo(dict(collection))
    gsc_metrics = _get_gsc_metrics_for_store(collection["store_id"])
    semrush_metrics = _get_semrush_metrics_for_store(collection["store_id"])

    item = {
        "id": collection["id"],
        "store_id": collection["store_id"],
        "store_name": collection["store_name"],
        "shopify_id": collection["shopify_id"],
        "title": collection["title"],
        "handle": collection["handle"],
        "description": collection["description"],
        "meta_title": collection["meta_title"],
        "meta_description": collection["meta_description"],
        "products_count": collection["products_count"],
        "collection_type": collection["collection_type"],
        "published": collection["published"],
        "image_url": collection["image_url"],
        "seo_breakdown": seo["breakdown"],
        "last_optimized_at": collection["last_optimized_at"].isoformat() if collection.get("last_optimized_at") else None,
    }
    _attach_gsc_to_item(item, gsc_metrics, "collection")
    _attach_semrush_to_item(item, semrush_metrics, "collection")

    baselines = _get_store_baselines(collection["store_id"])
    gsc_for_scoring = {
        "gsc_clicks": item.get("gsc_clicks"),
        "gsc_impressions": item.get("gsc_impressions"),
        "gsc_ctr": item.get("gsc_ctr"),
        "gsc_position": item.get("gsc_position"),
    }
    sr_for_scoring = {
        "semrush_volume": item.get("semrush_volume"),
        "semrush_position": item.get("semrush_position"),
        "semrush_keyword_count": item.get("semrush_keyword_count"),
    }
    search_perf = analyze_search_performance(gsc_for_scoring, sr_for_scoring, baselines)
    kw = _fetch_keyword_analysis(collection["store_id"], "collection", collection_id)
    _attach_unified_scores(item, seo, search_perf, kw)

    return jsonify(item)


@seo_bp.route("/api/seo/collections/delete", methods=["POST"])
def delete_collections():
    return _start_bulk_collection_action("delete")


@seo_bp.route("/api/seo/collections/publish", methods=["POST"])
def publish_collections():
    return _start_bulk_collection_action("publish")


def _start_bulk_collection_action(action):
    """Run publish/delete over many collections as a pollable background task —
    the synchronous loops gave no progress feedback and risked proxy timeouts."""
    data = request.get_json(silent=True) or {}
    collection_ids = data.get("collection_ids", [])

    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM synced_collections WHERE id = ANY(%s)",
            ([int(i) for i in collection_ids],),
        )
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No collections found"}), 404

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, FALSE, %s) RETURNING id",
            (len(valid_ids), f"collection_{action}"),
        )
        bulk_task_id = cur.fetchone()["id"]
        for cid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, collection_id) VALUES (%s, %s)",
                (bulk_task_id, cid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_collection_action, bulk_task_id, action)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _execute_bulk_collection_action(bulk_task_id, action):
    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))
        # Fetch everything up front — deleting a collection cascades away its
        # bulk_task_items row (FK ON DELETE CASCADE), so don't rely on re-reads.
        cur.execute(
            """SELECT bti.id AS bti_id, sc.id AS cid, sc.shopify_id, sc.title, sc.store_id,
                      s.domain, s.access_token_enc, s.api_version
               FROM bulk_task_items bti
               JOIN synced_collections sc ON sc.id = bti.collection_id
               JOIN stores s ON s.id = sc.store_id
               WHERE bti.bulk_task_id = %s ORDER BY bti.id""",
            (bulk_task_id,),
        )
        items = cur.fetchall()

    clients = {}
    for c in items:
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                    (c["title"] or "", bulk_task_id),
                )

            store_id = c["store_id"]
            if store_id not in clients:
                clients[store_id] = ShopifyClient(
                    c["domain"],
                    decrypt_value(c["access_token_enc"]),
                    c["api_version"],
                )
            client = clients[store_id]

            if action == "publish":
                client.publish_to_all_channels(c["shopify_id"])
                with db.get_cursor() as cur:
                    cur.execute("UPDATE synced_collections SET published = TRUE WHERE id = %s", (c["cid"],))
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (c["bti_id"],),
                    )
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
            else:
                try:
                    client.delete_collection(c["shopify_id"])
                except Exception as e:
                    # Already gone on Shopify is fine — still clean up locally.
                    if not any(t in str(e).lower() for t in ("does not exist", "not found", "no longer exists")):
                        raise
                with db.get_cursor() as cur:
                    # Mark progress first — deleting the row cascades this item away.
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (c["bti_id"],),
                    )
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
                    cur.execute("DELETE FROM generated_collections WHERE collection_id = %s", (c["cid"],))
                    cur.execute(
                        "DELETE FROM technical_seo_analyses WHERE item_type = 'collection' AND item_id = %s",
                        (c["cid"],),
                    )
                    cur.execute("DELETE FROM synced_collections WHERE id = %s", (c["cid"],))

            logger.info("Bulk %s: collection '%s' (shopify_id=%s)", action, c["title"], c["shopify_id"])
        except Exception as e:
            logger.exception("Bulk %s failed for collection '%s'", action, c["title"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), c["bti_id"]),
                )
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


@seo_bp.route("/api/seo/bulk-apply-tags/<int:store_id>", methods=["POST"])
def bulk_apply_tags(store_id):
    data = request.get_json(silent=True) or {}
    assignments = data.get("product_assignments")

    if not assignments:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT product_assignments FROM keyword_taxonomies WHERE store_id = %s",
                (store_id,),
            )
            row = cur.fetchone()
        if not row or not row["product_assignments"]:
            return jsonify({"error": "No product assignments found. Run keyword analysis first."}), 400
        assignments = row["product_assignments"]
        if isinstance(assignments, str):
            assignments = json.loads(assignments)

    if not assignments:
        return jsonify({"error": "No product assignments to apply"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, TRUE, 'apply_tags') RETURNING id",
            (len(assignments),),
        )
        bulk_task_id = cur.fetchone()["id"]

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_apply_tags, bulk_task_id, store_id, assignments)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(assignments)}), 201


def _execute_bulk_apply_tags(bulk_task_id, store_id, assignments):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (bulk_task_id,),
            )
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    for assignment in assignments:
        product_id = assignment.get("product_id")
        assigned_tags = assignment.get("assigned_tags", [])

        if not product_id or not assigned_tags:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT shopify_id, title FROM synced_products WHERE id = %s AND store_id = %s",
                (product_id, store_id),
            )
            product = cur.fetchone()

        if not product:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (product["title"], bulk_task_id),
            )

        try:
            last_err = None
            for attempt in range(3):
                try:
                    client.update_product(product["shopify_id"], {"tags": assigned_tags})
                    last_err = None
                    break
                except Exception as retry_err:
                    last_err = retry_err
                    logger.warning("Retry %d/3 for product %s: %s", attempt + 1, product_id, retry_err)
                    time.sleep(2 ** attempt)

            if last_err:
                raise last_err

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_products SET tags = %s WHERE id = %s",
                    (", ".join(assigned_tags), product_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        except Exception as e:
            logger.exception("Failed to apply tags to product %s", product_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


@seo_bp.route("/api/seo/collections/generate/tags", methods=["GET"])
def extract_tags():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, title, handle, tags FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' AND tags IS NOT NULL AND tags != ''",
            (store_id,),
        )
        products = cur.fetchall()

        cur.execute(
            "SELECT shopify_id, title, handle FROM synced_collections WHERE store_id = %s",
            (store_id,),
        )
        existing_map = {}
        for r in cur.fetchall():
            existing_map[r["title"].lower()] = {
                "shopify_id": r["shopify_id"],
                "title": r["title"],
                "handle": r["handle"],
            }

    tag_map = {}
    for p in products:
        raw_tags = p["tags"] or ""
        for tag in raw_tags.split(","):
            tag = tag.strip()
            if not tag:
                continue
            key = tag.lower()
            if key not in tag_map:
                tag_map[key] = {"tag": tag, "product_count": 0, "sample_products": []}
            tag_map[key]["product_count"] += 1
            if len(tag_map[key]["sample_products"]) < 5:
                tag_map[key]["sample_products"].append({"title": p["title"], "handle": p["handle"]})

    result = []
    for key, data in tag_map.items():
        if key in existing_map:
            data["existing_collection"] = existing_map[key]
        result.append(data)

    result.sort(key=lambda x: x["product_count"], reverse=True)
    return jsonify(result)


@seo_bp.route("/api/seo/collections/generate/filter-tags", methods=["POST"])
def filter_tags_ai():
    data = request.get_json(silent=True) or {}
    tags = data.get("tags", [])
    if not tags:
        return jsonify({"error": "tags list is required"}), 400

    try:
        from app.ai_agent import _get_ai_config
        from openai import OpenAI

        config = _get_ai_config()
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=config["api_key"],
        )

        tag_list_str = "\n".join(
            f"- \"{t['tag']}\" ({t.get('product_count', 0)} products)" for t in tags
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an SEO expert for Shopify stores. Analyze product tags and determine "
                        "which ones would make good, meaningful collections.\n\n"
                        "Return ONLY valid JSON with this exact structure:\n"
                        '{"valid_tags": [{"tag": "original_tag", "collection_title": "Clean SEO Title"}], '
                        '"rejected_tags": [{"tag": "original_tag", "reason": "why rejected"}]}\n\n'
                        "Filter out tags that are: numeric-only, internal codes, meaningless abbreviations, "
                        "single characters, or too generic (like 'new' or 'sale' unless highly relevant). "
                        "For valid tags, provide a cleaned-up, SEO-friendly collection title with proper "
                        "capitalization and expanded abbreviations."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Analyze these product tags and identify which would make good Shopify collections:\n\n{tag_list_str}",
                },
            ],
            temperature=0.2,
        )

        content = response.choices[0].message.content or ""
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(content[start:end])
            return jsonify(parsed)

        return jsonify({"error": "AI returned unparseable response", "raw": content}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/collections/generate", methods=["POST"])
def generate_collections():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    tags = data.get("tags", [])
    auto_optimize = data.get("auto_optimize", False)

    if not store_id or not tags:
        return jsonify({"error": "store_id and tags are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, %s, 'generate_collections') RETURNING id",
            (len(tags), auto_optimize),
        )
        bulk_task_id = cur.fetchone()["id"]

    spawn_task("bulk_tasks", bulk_task_id, _execute_generate_collections, bulk_task_id, store_id, tags, auto_optimize)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(tags)}), 201


def _execute_generate_collections(bulk_task_id, store_id, tags, auto_optimize):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s",
            (bulk_task_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (bulk_task_id,),
            )
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    created_collection_ids = []

    for tag_info in tags:
        tag = tag_info.get("tag", "")
        collection_title = tag_info.get("collection_title") or tag.title()

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (collection_title, bulk_task_id),
            )

        try:
            with db.get_cursor() as cur:
                # Exact tag membership — substring ILIKE matched unrelated tags
                # ("red" matched "bordered") and treated %/_ as wildcards.
                cur.execute(
                    """SELECT shopify_id, title, handle FROM synced_products
                       WHERE store_id = %s AND status = 'ACTIVE'
                         AND EXISTS (
                            SELECT 1 FROM unnest(string_to_array(tags, ',')) t
                            WHERE lower(trim(t)) = lower(%s)
                         )""",
                    (store_id, tag),
                )
                matching_products = cur.fetchall()

            product_shopify_ids = [p["shopify_id"] for p in matching_products]

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                    (store_id, collection_title),
                )
                existing = cur.fetchone()

            if existing:
                shopify_id = existing["shopify_id"]
                synced_collection_id = existing["id"]
                handle = existing["handle"]

                try:
                    current_products = client.get_all_collection_products(shopify_id)
                    current_product_ids = {p["shopify_id"] for p in current_products}
                except Exception:
                    logger.exception("Failed to fetch products for existing collection '%s'", collection_title)
                    current_product_ids = set()

                missing_ids = [pid for pid in product_shopify_ids if pid not in current_product_ids]

                if missing_ids:
                    try:
                        client.add_products_to_collection(shopify_id, missing_ids)
                    except Exception:
                        logger.exception("Failed to add products to existing collection '%s'", collection_title)
                    logger.info("Added %d missing products to existing collection '%s'", len(missing_ids), collection_title)
                else:
                    logger.info("Collection '%s' already has all tagged products, skipping", collection_title)

                total_products = len(current_product_ids) + len(missing_ids)

                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_collections SET products_count = %s, synced_at = NOW() WHERE id = %s",
                        (total_products, synced_collection_id),
                    )
                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status)
                        VALUES (%s, %s, %s, %s, %s, 'merged')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id,
                            products_count = EXCLUDED.products_count, status = EXCLUDED.status""",
                        (store_id, tag, synced_collection_id, shopify_id, total_products),
                    )

                created_collection_ids.append(synced_collection_id)
            else:
                result = client.create_collection(collection_title)
                shopify_id = result["shopify_id"]
                handle = result["handle"]

                try:
                    client.publish_to_all_channels(shopify_id)
                except Exception:
                    logger.exception("Failed to publish collection '%s' to sales channels", collection_title)

                if product_shopify_ids:
                    try:
                        client.add_products_to_collection(shopify_id, product_shopify_ids)
                    except Exception:
                        logger.exception("Failed to add products to collection '%s'", collection_title)

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                        VALUES (%s, %s, %s, %s, 'manual', %s, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, products_count = EXCLUDED.products_count, synced_at = NOW()
                        RETURNING id""",
                        (store_id, shopify_id, collection_title, handle, len(product_shopify_ids)),
                    )
                    synced_collection_id = cur.fetchone()["id"]

                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status)
                        VALUES (%s, %s, %s, %s, %s, 'created')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id,
                            products_count = EXCLUDED.products_count, status = EXCLUDED.status""",
                        (store_id, tag, synced_collection_id, shopify_id, len(product_shopify_ids)),
                    )

                created_collection_ids.append(synced_collection_id)

                logger.info(
                    "Created collection '%s' with %d products",
                    collection_title, len(product_shopify_ids),
                )

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        except Exception as e:
            logger.exception("Failed to process collection for tag '%s'", tag)
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO generated_collections (store_id, tag, status)
                    VALUES (%s, %s, 'failed')
                    ON CONFLICT (store_id, tag) DO UPDATE SET status = 'failed'""",
                    (store_id, tag),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )

    if auto_optimize and created_collection_ids:
        _auto_optimize_generated_collections(store_id, created_collection_ids)


@seo_bp.route("/api/seo/collections/generate/taxonomy", methods=["POST"])
def generate_taxonomy_collections():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    categories = data.get("categories", [])
    auto_optimize = data.get("auto_optimize", False)

    if not store_id or not categories:
        return jsonify({"error": "store_id and categories are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT taxonomy, product_assignments FROM keyword_taxonomies WHERE store_id = %s AND status = 'completed'",
            (store_id,),
        )
        tax_row = cur.fetchone()

    if not tax_row:
        return jsonify({"error": "No completed taxonomy found. Run keyword analysis first."}), 400

    total_work = len(categories)
    for cat in categories:
        total_work += len(cat.get("tags", []))

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, %s, 'generate_taxonomy_collections') RETURNING id",
            (total_work, auto_optimize),
        )
        bulk_task_id = cur.fetchone()["id"]

    product_assignments = tax_row["product_assignments"]
    if isinstance(product_assignments, str):
        product_assignments = json.loads(product_assignments)

    spawn_task("bulk_tasks", bulk_task_id, _execute_generate_taxonomy_collections, bulk_task_id, store_id, categories, product_assignments, auto_optimize)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": total_work}), 201


def _execute_generate_taxonomy_collections(bulk_task_id, store_id, categories, product_assignments, auto_optimize):
    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (bulk_task_id,),
            )
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    tag_to_product_ids = {}
    for assignment in product_assignments:
        pid = assignment.get("product_id")
        for tag in assignment.get("assigned_tags", []):
            tag_to_product_ids.setdefault(tag, set()).add(pid)

    created_collection_ids = []
    hierarchy = {}

    for cat in categories:
        cat_name = cat.get("name", "")
        cat_tags = cat.get("tags", [])

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (f"Category: {cat_name}", bulk_task_id),
            )

        try:
            all_cat_product_ids = set()
            for tag in cat_tags:
                all_cat_product_ids.update(tag_to_product_ids.get(tag, set()))

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                    (store_id, cat_name),
                )
                existing_parent = cur.fetchone()

            if existing_parent:
                parent_shopify_id = existing_parent["shopify_id"]
                parent_synced_id = existing_parent["id"]
                parent_handle = existing_parent["handle"]
            else:
                result = client.create_collection(cat_name)
                parent_shopify_id = result["shopify_id"]
                parent_handle = result["handle"]

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                        VALUES (%s, %s, %s, %s, 'manual', 0, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, synced_at = NOW()
                        RETURNING id""",
                        (store_id, parent_shopify_id, cat_name, parent_handle),
                    )
                    parent_synced_id = cur.fetchone()["id"]

            if all_cat_product_ids:
                with db.get_cursor() as cur:
                    placeholders = ", ".join(["%s"] * len(all_cat_product_ids))
                    cur.execute(
                        f"SELECT shopify_id FROM synced_products WHERE id IN ({placeholders}) AND store_id = %s",
                        list(all_cat_product_ids) + [store_id],
                    )
                    cat_shopify_ids = [r["shopify_id"] for r in cur.fetchall()]

                if cat_shopify_ids:
                    try:
                        client.add_products_to_collection(parent_shopify_id, cat_shopify_ids)
                    except Exception:
                        logger.exception("Failed to add products to parent collection '%s'", cat_name)

                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_collections SET products_count = %s, synced_at = NOW() WHERE id = %s",
                        (len(cat_shopify_ids), parent_synced_id),
                    )

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status, parent_collection_id, taxonomy_category, generation_mode)
                    VALUES (%s, %s, %s, %s, %s, 'created', NULL, %s, 'taxonomy')
                    ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                        shopify_collection_id = EXCLUDED.shopify_collection_id,
                        products_count = EXCLUDED.products_count, status = EXCLUDED.status,
                        parent_collection_id = EXCLUDED.parent_collection_id,
                        taxonomy_category = EXCLUDED.taxonomy_category,
                        generation_mode = EXCLUDED.generation_mode""",
                    (store_id, f"__category__{cat_name}", parent_synced_id, parent_shopify_id, len(all_cat_product_ids), cat_name),
                )

            created_collection_ids.append(parent_synced_id)
            hierarchy[cat_name] = {
                "parent_id": parent_synced_id,
                "parent_handle": parent_handle,
                "parent_title": cat_name,
                "children": [],
            }

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

            logger.info("Created parent category collection '%s' with %d products", cat_name, len(all_cat_product_ids))

        except Exception:
            logger.exception("Failed to create parent collection for category '%s'", cat_name)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

        for tag in cat_tags:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                    (tag, bulk_task_id),
                )

            try:
                tag_product_ids = tag_to_product_ids.get(tag, set())

                product_shopify_ids = []
                if tag_product_ids:
                    with db.get_cursor() as cur:
                        placeholders = ", ".join(["%s"] * len(tag_product_ids))
                        cur.execute(
                            f"SELECT shopify_id FROM synced_products WHERE id IN ({placeholders}) AND store_id = %s",
                            list(tag_product_ids) + [store_id],
                        )
                        product_shopify_ids = [r["shopify_id"] for r in cur.fetchall()]

                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                        (store_id, tag),
                    )
                    existing_child = cur.fetchone()

                if existing_child:
                    child_shopify_id = existing_child["shopify_id"]
                    child_synced_id = existing_child["id"]
                    child_handle = existing_child["handle"]

                    if product_shopify_ids:
                        try:
                            current_products = client.get_all_collection_products(child_shopify_id)
                            current_ids = {p["shopify_id"] for p in current_products}
                            missing = [pid for pid in product_shopify_ids if pid not in current_ids]
                            if missing:
                                client.add_products_to_collection(child_shopify_id, missing)
                        except Exception:
                            logger.exception("Failed to update products for existing child collection '%s'", tag)

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE synced_collections SET products_count = %s, synced_at = NOW() WHERE id = %s",
                            (len(product_shopify_ids), child_synced_id),
                        )
                else:
                    result = client.create_collection(tag)
                    child_shopify_id = result["shopify_id"]
                    child_handle = result["handle"]

                    if product_shopify_ids:
                        try:
                            client.add_products_to_collection(child_shopify_id, product_shopify_ids)
                        except Exception:
                            logger.exception("Failed to add products to child collection '%s'", tag)

                    with db.get_cursor() as cur:
                        cur.execute(
                            """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                            VALUES (%s, %s, %s, %s, 'manual', %s, NOW())
                            ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, products_count = EXCLUDED.products_count, synced_at = NOW()
                            RETURNING id""",
                            (store_id, child_shopify_id, tag, child_handle, len(product_shopify_ids)),
                        )
                        child_synced_id = cur.fetchone()["id"]

                parent_synced_id = hierarchy.get(cat_name, {}).get("parent_id")

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status, parent_collection_id, taxonomy_category, generation_mode)
                        VALUES (%s, %s, %s, %s, %s, 'created', %s, %s, 'taxonomy')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id,
                            products_count = EXCLUDED.products_count, status = EXCLUDED.status,
                            parent_collection_id = EXCLUDED.parent_collection_id,
                            taxonomy_category = EXCLUDED.taxonomy_category,
                            generation_mode = EXCLUDED.generation_mode""",
                        (store_id, tag, child_synced_id, child_shopify_id, len(product_shopify_ids), parent_synced_id, cat_name),
                    )

                created_collection_ids.append(child_synced_id)

                if cat_name in hierarchy:
                    hierarchy[cat_name]["children"].append({
                        "id": child_synced_id,
                        "title": tag,
                        "handle": child_handle,
                    })

                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

                logger.info("Created child collection '%s' under '%s' with %d products", tag, cat_name, len(product_shopify_ids))

            except Exception:
                logger.exception("Failed to create child collection for tag '%s'", tag)
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

            time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )

    if auto_optimize and created_collection_ids:
        _auto_optimize_generated_collections(store_id, created_collection_ids, hierarchy)



def _auto_optimize_generated_collections(store_id, collection_ids, hierarchy=None):
    from app.ai_agent import run_task as ai_run_task

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        return

    hierarchy_context_map = {}
    if hierarchy:
        for cat_name, cat_data in hierarchy.items():
            parent_id = cat_data.get("parent_id")
            children = cat_data.get("children", [])
            child_titles = [c["title"] for c in children]
            child_handles = [c.get("handle", "") for c in children]

            if parent_id:
                hierarchy_context_map[parent_id] = (
                    f"This is a PARENT category collection for '{cat_name}'. "
                    f"It has {len(children)} sub-collections: {', '.join(child_titles)}. "
                    "Include internal links to these sub-collections using /collections/{{handle}} URLs: "
                    + ", ".join(f"{t} (/collections/{h})" for t, h in zip(child_titles, child_handles) if h)
                )

            for child in children:
                cid = child.get("id")
                if cid:
                    siblings = [c["title"] for c in children if c["id"] != cid]
                    sibling_handles = [c.get("handle", "") for c in children if c["id"] != cid]
                    hierarchy_context_map[cid] = (
                        f"This is a sub-collection under the parent category '{cat_name}' "
                        f"(/collections/{cat_data.get('parent_handle', '')}). "
                        f"Sibling collections: {', '.join(siblings)}. "
                        "Include a link back to the parent collection and links to 2-3 sibling collections: "
                        + ", ".join(f"{t} (/collections/{h})" for t, h in zip(siblings, sibling_handles) if h)
                    )

    for cid in collection_ids:
        try:
            task_input = {"collection_id": cid}
            if cid in hierarchy_context_map:
                task_input["hierarchy_context"] = hierarchy_context_map[cid]

            ai_result = ai_run_task("collection_seo", task_input)

            if "error" not in ai_result and "raw_response" not in ai_result:
                client = ShopifyClient(
                    store["domain"],
                    decrypt_value(store["access_token_enc"]),
                    store["api_version"],
                )

                with db.get_cursor() as cur:
                    cur.execute("SELECT shopify_id FROM synced_collections WHERE id = %s", (cid,))
                    row = cur.fetchone()

                if not row:
                    continue

                fields = {}
                if ai_result.get("h1_tag"):
                    fields["title"] = ai_result["h1_tag"]
                if ai_result.get("optimized_description"):
                    fields["description_html"] = ai_result["optimized_description"]
                if ai_result.get("optimized_meta_title"):
                    fields["meta_title"] = ai_result["optimized_meta_title"]
                if ai_result.get("optimized_meta_description"):
                    fields["meta_description"] = ai_result["optimized_meta_description"]

                guard_shopify_fields(fields)

                if fields:
                    client.update_collection(row["shopify_id"], fields)

                with db.get_cursor() as cur:
                    if fields.get("title"):
                        cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (fields["title"], cid))
                    if fields.get("description_html"):
                        cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (fields["description_html"], cid))
                    if fields.get("meta_title"):
                        cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (fields["meta_title"], cid))
                    if fields.get("meta_description"):
                        cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (fields["meta_description"], cid))
                    cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (cid,))

                    cur.execute(
                        "UPDATE generated_collections SET status = 'optimized' WHERE collection_id = %s",
                        (cid,),
                    )

                logger.info("Auto-optimized generated collection %s", cid)

        except Exception:
            logger.exception("Auto-optimize failed for collection %s", cid)

        time.sleep(1)


def _get_collection_with_store(collection_id):
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sc.*, s.domain, s.access_token_enc, s.api_version
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id = %s
            """,
            (collection_id,),
        )
        return cur.fetchone()


def _build_client(row):
    return ShopifyClient(
        row["domain"],
        decrypt_value(row["access_token_enc"]),
        row["api_version"],
    )


def _generate_image_for_collection(client, collection):
    keywords = _extract_keywords(collection)
    products = client.get_collection_products_with_images(collection["shopify_id"], limit=4)
    product_image_bytes = []
    product_titles = []
    for p in products:
        try:
            resp = http_requests.get(p["image_url"], timeout=15)
            resp.raise_for_status()
            product_image_bytes.append(resp.content)
            product_titles.append(p.get("title", ""))
        except Exception:
            logger.warning("Failed to download product image: %s", p.get("image_url"))

    if product_image_bytes:
        ai_result = generate_collection_image_ai(
            collection["title"], product_image_bytes, product_titles, keywords=keywords
        )
        if ai_result:
            return *ai_result, len(products)

    image_bytes, filename, alt_text, width, height = generate_collection_image(
        collection["title"], product_image_bytes
    )
    alt_text = build_seo_alt_text(collection["title"], keywords)
    return image_bytes, filename, alt_text, width, height, len(products)


@seo_bp.route("/api/seo/collections/<int:collection_id>/generate-image", methods=["POST"])
def generate_collection_image_preview(collection_id):
    collection = _get_collection_with_store(collection_id)
    if not collection:
        return jsonify({"error": "Collection not found"}), 404

    try:
        client = _build_client(collection)
        image_bytes, filename, alt_text, width, height, product_count = _generate_image_for_collection(
            client, collection
        )
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        return jsonify({
            "image_base64": image_b64,
            "filename": filename,
            "alt_text": alt_text,
            "width": width,
            "height": height,
            "product_count": product_count,
        })
    except Exception as e:
        logger.exception("Failed to generate collection image for %s", collection_id)
        return jsonify({"error": str(e)}), 500


import re as _re


def _inject_image_into_description(client, collection, image_url, alt_text, width=1200, height=628):
    description = collection.get("description") or ""

    description = _re.sub(
        r'<img[^>]+class="collection-banner-img"[^>]*/?>\s*(<br\s*/?>)?\s*',
        "",
        description,
    )

    img_tag = (
        f'<img src="{image_url}" alt="{alt_text}" class="collection-banner-img"'
        f' width="{width}" height="{height}"'
        f' loading="eager" fetchpriority="high"'
        f' style="width:100%;max-width:100%;height:auto;border-radius:8px;margin-bottom:16px;" />'
    )
    new_description = img_tag + "\n" + description.lstrip()

    client.update_collection(collection["shopify_id"], {"description_html": new_description})

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE synced_collections SET description = %s WHERE id = %s",
            (new_description, collection["id"]),
        )
    logger.info("Injected banner image into description for collection %s", collection["id"])


@seo_bp.route("/api/seo/collections/<int:collection_id>/apply-image", methods=["POST"])
def apply_collection_image(collection_id):
    data = request.get_json(silent=True) or {}
    collection = _get_collection_with_store(collection_id)
    if not collection:
        return jsonify({"error": "Collection not found"}), 404

    try:
        client = _build_client(collection)

        image_b64 = data.get("image_base64")
        filename = data.get("filename", "collection-image.jpg")
        alt_text = data.get("alt_text", f"{collection['title']} collection image")
        width = 1200
        height = 628

        if image_b64:
            image_bytes = base64.b64decode(image_b64)
            try:
                from PIL import Image as PILImage
                img = PILImage.open(io.BytesIO(image_bytes))
                width, height = img.size
            except Exception:
                pass
        else:
            image_bytes, filename, alt_text, width, height, _ = _generate_image_for_collection(
                client, collection
            )

        mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
        staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
        resource_url = client.upload_staged_file(staged, image_bytes)
        logger.info("Staged upload complete for collection %s: %s", collection_id, resource_url)
        result = client.update_collection_image(collection["shopify_id"], resource_url, alt_text)
        logger.info("collectionUpdate result for %s: %s", collection_id, result)

        final_url = resource_url
        if result and result.get("image"):
            final_url = result["image"].get("url") or resource_url

        _inject_image_into_description(client, collection, final_url, alt_text, width, height)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE synced_collections SET image_url = %s WHERE id = %s",
                (final_url, collection_id),
            )

        return jsonify({"ok": True, "image_url": final_url})
    except Exception as e:
        logger.exception("Failed to apply collection image for %s", collection_id)
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/collections/bulk-generate-images", methods=["POST"])
def bulk_generate_collection_images():
    data = request.get_json(silent=True) or {}
    collection_ids = data.get("collection_ids", [])
    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'collection_images') RETURNING id",
            (len(collection_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for cid in collection_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, collection_id, status) VALUES (%s, %s, 'pending')",
                (bulk_task_id, cid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _run_bulk_collection_images, bulk_task_id, collection_ids)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(collection_ids)}), 201


def _run_bulk_collection_images(bulk_task_id, collection_ids):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )

    clients = {}

    for cid in collection_ids:
        collection = _get_collection_with_store(cid)
        if not collection:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = 'Collection not found' WHERE bulk_task_id = %s AND collection_id = %s",
                    (bulk_task_id, cid),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (collection["title"], bulk_task_id),
            )

        try:
            store_id = collection["store_id"]
            if store_id not in clients:
                clients[store_id] = _build_client(collection)
            client = clients[store_id]

            image_bytes, filename, alt_text, width, height, _ = _generate_image_for_collection(
                client, collection
            )

            mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
            staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
            resource_url = client.upload_staged_file(staged, image_bytes)
            result = client.update_collection_image(collection["shopify_id"], resource_url, alt_text)

            final_url = resource_url
            if result and result.get("image"):
                final_url = result["image"].get("url") or resource_url

            _inject_image_into_description(client, collection, final_url, alt_text, width, height)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_collections SET image_url = %s WHERE id = %s",
                    (final_url, cid),
                )
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'completed' WHERE bulk_task_id = %s AND collection_id = %s",
                    (bulk_task_id, cid),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

            logger.info("Generated image for collection %s '%s'", cid, collection["title"])

        except Exception as e:
            logger.exception("Failed to generate image for collection %s", cid)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s WHERE bulk_task_id = %s AND collection_id = %s",
                    (str(e), bulk_task_id, cid),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )
