import json
import logging
import threading

from flask import Blueprint, jsonify, redirect, request

from app.auth import decrypt_value
from app.database import db
from app.services.ai_output_guard import sanitize_html
from app.services.task_runner import spawn as spawn_task
from app.services.technical_seo_analyzer import (
    analyze_collection_technical_seo,
    analyze_product_technical_seo,
)
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

technical_seo_bp = Blueprint("technical_seo", __name__)


def _enforce_meta_limits(shopify_fields):
    meta_desc = shopify_fields.get("meta_description")
    if meta_desc and isinstance(meta_desc, str) and len(meta_desc) > 160:
        truncated = meta_desc[:157].rsplit(" ", 1)[0] + "..."
        shopify_fields["meta_description"] = truncated if len(truncated) <= 160 else meta_desc[:160]
    meta_title = shopify_fields.get("meta_title")
    if meta_title and isinstance(meta_title, str) and len(meta_title) > 60:
        truncated = meta_title[:57].rsplit(" ", 1)[0] + "..."
        shopify_fields["meta_title"] = truncated if len(truncated) <= 60 else meta_title[:60]
    return shopify_fields


def _enrich_collection_data(collection_dict):
    store_id = collection_dict.get("store_id")
    title_lower = (collection_dict.get("title") or "").lower().strip()
    if not title_lower or not store_id:
        return collection_dict

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT vendor, COUNT(*) as cnt FROM synced_products "
            "WHERE store_id = %s AND vendor IS NOT NULL AND vendor != '' "
            "GROUP BY vendor ORDER BY cnt DESC",
            (store_id,),
        )
        vendors = cur.fetchall()

    for v in vendors:
        vendor_lower = v["vendor"].lower()
        if title_lower.startswith(vendor_lower + " ") or title_lower == vendor_lower:
            collection_dict["vendor"] = v["vendor"]
            remainder = title_lower[len(vendor_lower):].strip()
            if remainder:
                remainder = remainder.split(" - ")[0].split(" & ")[0].strip()
                collection_dict["product_type"] = remainder
            break

    return collection_dict


def _resolve_and_analyze(item_type, item_dict, prefetch=None):
    """Resolve demand-grounded keywords, run the technical analysis against the
    resolved primary, and fold the resolution into the analysis payload."""
    from app.services.keyword_resolver import resolve_item_keywords

    item_dict = dict(item_dict)
    if item_type == "collection":
        item_dict = _enrich_collection_data(item_dict)

    store_id = item_dict["store_id"]
    resolution = None
    try:
        resolution = resolve_item_keywords(
            store_id, item_type, item_dict["id"], item=item_dict, prefetch=prefetch
        )
    except Exception:
        logger.warning("Keyword resolution failed for %s %s", item_type, item_dict.get("id"))

    primary = (resolution or {}).get("primary")
    primary_kw = primary["keyword"] if primary else None

    if item_type == "product":
        analysis = analyze_product_technical_seo(item_dict, primary_keyword=primary_kw)
    else:
        analysis = analyze_collection_technical_seo(item_dict, primary_keyword=primary_kw)

    if resolution and resolution.get("candidates"):
        analysis["detected_keywords"] = resolution["candidates"]
    analysis["keyword_resolution"] = resolution
    return analysis, resolution


def _upsert_analysis(store_id, item_type, item_id, analysis, resolution):
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO technical_seo_analyses
            (store_id, item_type, item_id, score, detected_keywords, checks,
             issues_count, fixable_count, keyword_resolution, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, item_type, item_id) DO UPDATE SET
                score = EXCLUDED.score,
                detected_keywords = EXCLUDED.detected_keywords,
                checks = EXCLUDED.checks,
                issues_count = EXCLUDED.issues_count,
                fixable_count = EXCLUDED.fixable_count,
                keyword_resolution = EXCLUDED.keyword_resolution,
                updated_at = NOW()
            RETURNING id""",
            (
                store_id, item_type, item_id, analysis["score"],
                json.dumps(analysis["detected_keywords"]),
                json.dumps(analysis["checks"]),
                analysis["issues_count"], analysis["fixable_count"],
                json.dumps(resolution) if resolution else None,
            ),
        )
        return cur.fetchone()["id"]


@technical_seo_bp.route("/technical-seo")
def technical_seo_page():
    # The page merged into the SEO section (Issues tab). The /api/technical-seo/*
    # endpoints below stay — they power the merged page's keyword analysis.
    return redirect("/seo?tab=issues", code=301)


@technical_seo_bp.route("/api/technical-seo/products", methods=["GET"])
def list_products_technical_seo():
    store_id = request.args.get("store_id")
    status = request.args.get("status", "").strip().upper()
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {
        "title": "sp.title",
        "store": "s.name",
        "status": "sp.status",
    }
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
            SELECT sp.id, sp.store_id, sp.shopify_id, sp.title, sp.handle, sp.status,
                   COALESCE(s.shop_name, s.name) as store_name,
                   tsa.score as tech_seo_score,
                   tsa.issues_count,
                   tsa.updated_at as analyzed_at
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

    result = [
        {
            "id": p["id"],
            "store_id": p["store_id"],
            "store_name": p["store_name"],
            "shopify_id": p["shopify_id"],
            "title": p["title"],
            "handle": p["handle"],
            "status": p["status"],
            "tech_seo_score": p["tech_seo_score"],
            "issues_count": p["issues_count"],
            "analyzed_at": p["analyzed_at"].isoformat() if p["analyzed_at"] else None,
        }
        for p in products
    ]

    if sort_by == "tech_seo_score":
        reverse = sort_dir == "desc"
        result.sort(key=lambda x: x["tech_seo_score"] if x["tech_seo_score"] is not None else -1, reverse=reverse)

    return jsonify(result)


@technical_seo_bp.route("/api/technical-seo/collections", methods=["GET"])
def list_collections_technical_seo():
    store_id = request.args.get("store_id")
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {
        "title": "sc.title",
        "store": "s.name",
        "products_count": "sc.products_count",
    }
    order_clause = "ORDER BY sc.title"
    if sort_by in valid_sql_sorts:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {valid_sql_sorts[sort_by]} {direction}"

    conditions = []
    params = []
    if store_id:
        conditions.append("sc.store_id = %s")
        params.append(store_id)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT sc.id, sc.store_id, sc.shopify_id, sc.title, sc.handle, sc.products_count,
                   COALESCE(s.shop_name, s.name) as store_name,
                   tsa.score as tech_seo_score,
                   tsa.issues_count,
                   tsa.updated_at as analyzed_at
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

    result = [
        {
            "id": c["id"],
            "store_id": c["store_id"],
            "store_name": c["store_name"],
            "shopify_id": c["shopify_id"],
            "title": c["title"],
            "handle": c["handle"],
            "products_count": c["products_count"],
            "tech_seo_score": c["tech_seo_score"],
            "issues_count": c["issues_count"],
            "analyzed_at": c["analyzed_at"].isoformat() if c["analyzed_at"] else None,
        }
        for c in collections
    ]

    if sort_by == "tech_seo_score":
        reverse = sort_dir == "desc"
        result.sort(key=lambda x: x["tech_seo_score"] if x["tech_seo_score"] is not None else -1, reverse=reverse)

    return jsonify(result)


@technical_seo_bp.route("/api/technical-seo/analyze", methods=["POST"])
def analyze_item():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_id = data.get("item_id")

    if not item_id:
        return jsonify({"error": "item_id is required"}), 400

    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
        else:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    analysis, resolution = _resolve_and_analyze(item_type, item)
    analysis["analysis_id"] = _upsert_analysis(item["store_id"], item_type, item_id, analysis, resolution)

    return jsonify(analysis)


@technical_seo_bp.route("/api/technical-seo/bulk-analyze", methods=["POST"])
def bulk_analyze():
    """Analyze many items as a background bulk task (pollable via /api/tasks/bulk/<id>).

    The previous synchronous loop blocked the request for the whole batch with
    no progress feedback and risked proxy timeouts on large selections.
    """
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_ids = data.get("item_ids", [])

    if item_type not in ("product", "collection"):
        return jsonify({"error": f"Invalid item_type: {item_type}"}), 400
    if not item_ids:
        return jsonify({"error": "item_ids is required"}), 400

    table = "synced_products" if item_type == "product" else "synced_collections"
    item_col = "product_id" if item_type == "product" else "collection_id"

    with db.get_cursor() as cur:
        cur.execute(f"SELECT id FROM {table} WHERE id = ANY(%s)", ([int(i) for i in item_ids],))
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No matching items found"}), 400

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, FALSE, 'tseo_analyze') RETURNING id",
            (len(valid_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]
        for iid in valid_ids:
            cur.execute(
                f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                (bulk_task_id, iid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_analyze, bulk_task_id, item_type)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _execute_bulk_analyze(bulk_task_id, item_type):
    item_col = "product_id" if item_type == "product" else "collection_id"
    table = "synced_products" if item_type == "product" else "synced_collections"

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))
        cur.execute(
            f"SELECT bti.id AS bti_id, bti.{item_col} AS ref_id FROM bulk_task_items bti "
            "WHERE bti.bulk_task_id = %s ORDER BY bti.id",
            (bulk_task_id,),
        )
        items = cur.fetchall()

    # Batch the store-level demand-data queries once for the whole run
    prefetch = None
    run_store_id = None
    if items:
        try:
            from app.services.keyword_resolver import build_prefetch
            with db.get_cursor() as cur:
                cur.execute(f"SELECT store_id FROM {table} WHERE id = %s", (items[0]["ref_id"],))
                first = cur.fetchone()
            if first:
                run_store_id = first["store_id"]
                prefetch = build_prefetch(run_store_id, item_type, [r["ref_id"] for r in items])
        except Exception:
            logger.warning("Keyword prefetch failed for bulk task %s", bulk_task_id)

    for row in items:
        bti_id = row["bti_id"]
        ref_id = row["ref_id"]
        try:
            with db.get_cursor() as cur:
                cur.execute(f"SELECT * FROM {table} WHERE id = %s", (ref_id,))
                item = cur.fetchone()

            if not item:
                raise ValueError("Item not found")

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                    (item.get("title") or "", bulk_task_id),
                )

            analysis, resolution = _resolve_and_analyze(item_type, item, prefetch=prefetch)
            _upsert_analysis(item["store_id"], item_type, ref_id, analysis, resolution)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                    (bti_id,),
                )
                cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
        except Exception as e:
            logger.exception("Bulk analyze failed for %s %s", item_type, ref_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), bti_id),
                )
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

    # Ownership pass: one keyword, one page — redistribute duplicate
    # auto-primaries now that every item's candidates are fresh.
    if run_store_id:
        try:
            from app.services.keyword_resolver import dedupe_primary_ownership
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = 'Deduplicating keyword ownership...' WHERE id = %s",
                    (bulk_task_id,),
                )
            reassigned = dedupe_primary_ownership(run_store_id, item_type)
            if reassigned:
                logger.info("Keyword ownership pass reassigned %s %ss", reassigned, item_type)
        except Exception:
            logger.warning("Keyword ownership pass failed for bulk task %s", bulk_task_id)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


@technical_seo_bp.route("/api/technical-seo/analysis/<item_type>/<int:item_id>", methods=["GET"])
def get_analysis(item_type, item_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM technical_seo_analyses WHERE item_type = %s AND item_id = %s",
            (item_type, item_id),
        )
        analysis = cur.fetchone()

    if not analysis:
        return jsonify({"error": "Analysis not found. Run analysis first."}), 404

    return jsonify({
        "id": analysis["id"],
        "store_id": analysis["store_id"],
        "item_type": analysis["item_type"],
        "item_id": analysis["item_id"],
        "score": analysis["score"],
        "detected_keywords": analysis["detected_keywords"],
        "checks": analysis["checks"],
        "issues_count": analysis["issues_count"],
        "fixable_count": analysis["fixable_count"],
        "ai_fixes": analysis["ai_fixes"],
        "ai_task_id": analysis["ai_task_id"],
        "created_at": analysis["created_at"].isoformat() if analysis["created_at"] else None,
        "updated_at": analysis["updated_at"].isoformat() if analysis["updated_at"] else None,
    })


_ALLOWED_FIX_FIELDS = {"meta_title", "meta_description", "title", "tags", "description", "image_alt"}


@technical_seo_bp.route("/api/technical-seo/fix", methods=["POST"])
def create_fix_task():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_id = data.get("item_id")
    only_fields = data.get("only_fields")
    fix_context = data.get("fix_context")

    if not item_id:
        return jsonify({"error": "item_id is required"}), 400
    if only_fields:
        only_fields = [f for f in only_fields if f in _ALLOWED_FIX_FIELDS]

    with db.get_cursor() as cur:
        table = "synced_products" if item_type == "product" else "synced_collections"
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    store_id = item["store_id"]

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM technical_seo_analyses WHERE item_type = %s AND item_id = %s",
            (item_type, item_id),
        )
        analysis = cur.fetchone()

    if not analysis:
        # Seed the analysis inline (same path the bulk fix uses) so a fix can
        # be requested straight from the dashboard queue.
        try:
            analysis_data, resolution = _resolve_and_analyze(item_type, dict(item))
            analysis_pk = _upsert_analysis(store_id, item_type, item_id, analysis_data, resolution)
            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM technical_seo_analyses WHERE id = %s", (analysis_pk,))
                analysis = cur.fetchone()
        except Exception:
            logger.exception("Inline analysis failed for %s %s", item_type, item_id)
    if not analysis:
        return jsonify({"error": "Run analysis first before requesting fixes"}), 400

    task_input = {
        "item_type": item_type,
        "item_id": item_id,
        "analysis_id": analysis["id"],
    }
    if only_fields:
        task_input["only_fields"] = only_fields
    if fix_context:
        task_input["fix_context"] = fix_context

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'technical_seo_fix', %s) RETURNING id",
            (store_id, json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

        cur.execute(
            "UPDATE technical_seo_analyses SET ai_task_id = %s WHERE id = %s",
            (task_id, analysis["id"]),
        )

    spawn_task("ai_tasks", task_id, _execute_fix_task, task_id, analysis["id"])

    return jsonify({"ok": True, "task_id": task_id}), 201


def _execute_fix_task(task_id, analysis_id):
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()

    if not task:
        return

    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))

    try:
        result = run_task(task["task_type"], task["input"], task.get("store_id"))

        _failed = "error" in result or "raw_response" in result
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = %s, error = %s, result = %s, completed_at = NOW() WHERE id = %s",
                ("failed" if _failed else "completed",
                 (result.get("error") or "AI returned unparsable output") if _failed else None,
                 json.dumps(result), task_id),
            )

        if "error" not in result and "raw_response" not in result:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE technical_seo_analyses SET ai_fixes = %s, updated_at = NOW() WHERE id = %s",
                    (json.dumps(result), analysis_id),
                )

    except Exception as e:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


def _record_change_event(item_type, item, fixes, change_type):
    """Best-effort optimization-history event so View Impact has data for
    fixes applied outside the main SEO apply endpoints. Never blocks the fix."""
    try:
        from app.routes.seo_routes import (
            _fetch_keyword_analysis,
            _get_gsc_metrics_for_store,
            _get_semrush_metrics_for_store,
            _get_store_baselines,
        )
        from app.services.seo_analyzer import (
            analyze_collection_seo,
            analyze_product_seo,
            analyze_search_performance,
            compute_unified_score,
        )

        store_id = item["store_id"]
        analyze_fn = analyze_product_seo if item_type == "product" else analyze_collection_seo
        seo_before = analyze_fn(dict(item))
        prefix = "products" if item_type == "product" else "collections"
        path = f"{prefix}/{item.get('handle') or ''}"
        gsc = _get_gsc_metrics_for_store(store_id).get(path, {})
        sr = _get_semrush_metrics_for_store(store_id).get(path, {})
        search_before = analyze_search_performance(
            {k: gsc.get(k) for k in ("gsc_clicks", "gsc_impressions", "gsc_ctr", "gsc_position")},
            {k: sr.get(k) for k in ("semrush_volume", "semrush_position", "semrush_keyword_count")},
            _get_store_baselines(store_id),
        )
        kw = _fetch_keyword_analysis(store_id, item_type, item["id"])
        unified = compute_unified_score(seo_before, kw["score"] if kw else None, search_before)

        changes_map = {}
        for f in fixes:
            field = f.get("field")
            if field:
                changes_map[field] = {"new": str(f.get("value") or f.get("proposed_value") or "")[:200]}

        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO seo_change_events
                   (store_id, item_type, item_id, change_type, changes_applied,
                    seo_score_before, search_score_before, composite_score_before)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (store_id, item_type, item["id"], change_type,
                 json.dumps(changes_map) if changes_map else None,
                 unified["content_score"], unified.get("search_score"), unified["seo_score"]),
            )
    except Exception:
        logger.warning("Failed to record change event for %s %s", item_type, item.get("id"))


@technical_seo_bp.route("/api/technical-seo/apply", methods=["POST"])
def apply_fixes():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_id = data.get("item_id")
    fixes = data.get("fixes", [])

    if not item_id or not fixes:
        return jsonify({"error": "item_id and fixes are required"}), 400

    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute(
                """SELECT sp.*, s.domain, s.access_token_enc, s.api_version
                FROM synced_products sp
                JOIN stores s ON s.id = sp.store_id
                WHERE sp.id = %s""",
                (item_id,),
            )
        else:
            cur.execute(
                """SELECT sc.*, s.domain, s.access_token_enc, s.api_version
                FROM synced_collections sc
                JOIN stores s ON s.id = sc.store_id
                WHERE sc.id = %s""",
                (item_id,),
            )
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    try:
        client = ShopifyClient(
            item["domain"],
            decrypt_value(item["access_token_enc"]),
            item["api_version"],
        )

        updated_fields = []
        shopify_fields = {}
        media_updates = []

        for fix in fixes:
            field = fix.get("field")
            value = fix.get("value")
            if not field or not value:
                continue

            if field == "meta_title":
                shopify_fields["meta_title"] = value
                updated_fields.append("meta_title")
            elif field == "meta_description":
                shopify_fields["meta_description"] = value
                updated_fields.append("meta_description")
            elif field == "title":
                shopify_fields["title"] = value
                updated_fields.append("title")
            elif field == "tags":
                if isinstance(value, list):
                    shopify_fields["tags"] = value
                elif isinstance(value, str):
                    shopify_fields["tags"] = [t.strip() for t in value.split(",")]
                updated_fields.append("tags")
            elif field == "description":
                shopify_fields["description_html"] = value
                updated_fields.append("description")
            elif field == "image_alt":
                if isinstance(value, list):
                    media_updates = value
                    updated_fields.append("image_alt")

        _enforce_meta_limits(shopify_fields)
        if shopify_fields.get("description_html"):
            shopify_fields["description_html"] = sanitize_html(shopify_fields["description_html"])

        # Snapshot before-state so View Impact has history for targeted fixes
        if shopify_fields or media_updates:
            _record_change_event(item_type, item, fixes, "technical_fix")

        if shopify_fields:
            if item_type == "product":
                client.update_product(item["shopify_id"], shopify_fields)
            else:
                client.update_collection(item["shopify_id"], shopify_fields)

        if media_updates and item_type == "product":
            client.update_product_media(item["shopify_id"], media_updates)

        with db.get_cursor() as cur:
            if item_type == "product":
                if shopify_fields.get("title"):
                    cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (shopify_fields["title"], item_id))
                if shopify_fields.get("meta_title"):
                    cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (shopify_fields["meta_title"], item_id))
                if shopify_fields.get("meta_description"):
                    cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (shopify_fields["meta_description"], item_id))
                if shopify_fields.get("description_html"):
                    cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (shopify_fields["description_html"], item_id))
                if shopify_fields.get("tags"):
                    cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(shopify_fields["tags"]), item_id))
                if media_updates:
                    images = item.get("images") or []
                    if isinstance(images, str):
                        images = json.loads(images)
                    alt_map = {m["media_id"]: m["alt"] for m in media_updates if m.get("media_id")}
                    for img in images:
                        if img.get("media_id") in alt_map:
                            img["alt"] = alt_map[img["media_id"]]
                    cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), item_id))
                cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (item_id,))
            else:
                if shopify_fields.get("title"):
                    cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (shopify_fields["title"], item_id))
                if shopify_fields.get("meta_title"):
                    cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (shopify_fields["meta_title"], item_id))
                if shopify_fields.get("meta_description"):
                    cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (shopify_fields["meta_description"], item_id))
                if shopify_fields.get("description_html"):
                    cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (shopify_fields["description_html"], item_id))
                cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (item_id,))

        # Re-analyze after applying
        with db.get_cursor() as cur:
            if item_type == "product":
                cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
            else:
                cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
            updated_item = cur.fetchone()

        if updated_item:
            new_analysis, new_resolution = _resolve_and_analyze(item_type, updated_item)
            _upsert_analysis(updated_item["store_id"], item_type, item_id, new_analysis, new_resolution)
            with db.get_cursor() as cur:
                # Applied proposals are consumed — a stale set must not be
                # re-applicable against the now-changed content.
                cur.execute(
                    """UPDATE technical_seo_analyses SET ai_fixes = NULL
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (updated_item["store_id"], item_type, item_id),
                )

            return jsonify({
                "ok": True,
                "updated_fields": updated_fields,
                "new_score": new_analysis["score"],
                "new_issues_count": new_analysis["issues_count"],
            })

        return jsonify({"ok": True, "updated_fields": updated_fields})

    except Exception as e:
        logger.exception("Failed to apply technical SEO fixes")
        return jsonify({"error": str(e)}), 500


@technical_seo_bp.route("/api/seo/items/<item_type>/<int:item_id>/primary-keyword", methods=["POST"])
def set_primary_keyword(item_type, item_id):
    """Manually pin an item's primary keyword. Recorded as an accepted
    keyword_assignments row (the durable manual signal the resolver honors),
    then the item is re-resolved and re-analyzed."""
    from app.services.keyword_strategy_service import add_manual_keyword

    if item_type not in ("product", "collection"):
        return jsonify({"error": "item_type must be 'product' or 'collection'"}), 400

    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    table = "synced_products" if item_type == "product" else "synced_collections"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()
    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    store_id = item["store_id"]
    mk_id = add_manual_keyword(store_id, keyword)

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO keyword_assignments
               (store_id, master_keyword_id, assigned_item_type, assigned_item_id,
                assignment_reason, priority, status)
               VALUES (%s, %s, %s, %s, 'manual_primary', 'high', 'accepted')
               ON CONFLICT (store_id, master_keyword_id) DO UPDATE SET
                   assigned_item_type = EXCLUDED.assigned_item_type,
                   assigned_item_id = EXCLUDED.assigned_item_id,
                   assignment_reason = 'manual_primary',
                   status = 'accepted',
                   updated_at = NOW()""",
            (store_id, mk_id, item_type, item_id),
        )
        # One manual primary per item — demote previous picks
        cur.execute(
            """UPDATE keyword_assignments SET status = 'superseded', updated_at = NOW()
               WHERE store_id = %s AND assigned_item_type = %s AND assigned_item_id = %s
                 AND assignment_reason = 'manual_primary' AND master_keyword_id != %s
                 AND status = 'accepted'""",
            (store_id, item_type, item_id, mk_id),
        )
        # Best-effort mirror into content_keyword_map for the strategy page UI
        # (rebuilt by the next content audit anyway)
        cur.execute(
            """UPDATE content_keyword_map SET is_primary = FALSE
               WHERE store_id = %s AND item_type = %s AND item_id = %s AND is_primary = TRUE""",
            (store_id, item_type, item_id),
        )
        cur.execute(
            """INSERT INTO content_keyword_map
               (store_id, master_keyword_id, item_type, item_id, found_in, is_primary, relevance_score)
               VALUES (%s, %s, %s, %s, %s, TRUE, 100)
               ON CONFLICT (store_id, master_keyword_id, item_type, item_id) DO UPDATE SET
                   is_primary = TRUE, relevance_score = 100""",
            (store_id, mk_id, item_type, item_id, json.dumps(["manual"])),
        )

    analysis, resolution = _resolve_and_analyze(item_type, item)
    analysis["analysis_id"] = _upsert_analysis(store_id, item_type, item_id, analysis, resolution)

    return jsonify(analysis)


@technical_seo_bp.route("/api/technical-seo/bulk-fix-apply", methods=["POST"])
def bulk_fix_apply():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_ids = data.get("item_ids", [])

    if not item_ids:
        return jsonify({"error": "item_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'technical_seo') RETURNING id",
            (len(item_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for iid in item_ids:
            if item_type == "product":
                cur.execute(
                    "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )
            else:
                cur.execute(
                    "INSERT INTO bulk_task_items (bulk_task_id, collection_id) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_fix_apply, bulk_task_id, item_type)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(item_ids)}), 201


def _execute_bulk_fix_apply(bulk_task_id, item_type, only_fields=None):
    """Analyze → AI fix → apply for each item. When only_fields is set (issue-type
    bulk fixes), the AI and the apply step are scoped to those fix fields."""
    import time
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))

        if item_type == "product":
            cur.execute(
                """SELECT bti.id as item_id, bti.product_id as ref_id,
                          sp.shopify_id, sp.title, sp.store_id,
                          s.domain, s.access_token_enc, s.api_version
                FROM bulk_task_items bti
                JOIN synced_products sp ON sp.id = bti.product_id
                JOIN stores s ON s.id = sp.store_id
                WHERE bti.bulk_task_id = %s
                ORDER BY bti.id""",
                (bulk_task_id,),
            )
        else:
            cur.execute(
                """SELECT bti.id as item_id, bti.collection_id as ref_id,
                          sc.shopify_id, sc.title, sc.store_id,
                          s.domain, s.access_token_enc, s.api_version
                FROM bulk_task_items bti
                JOIN synced_collections sc ON sc.id = bti.collection_id
                JOIN stores s ON s.id = sc.store_id
                WHERE bti.bulk_task_id = %s
                ORDER BY bti.id""",
                (bulk_task_id,),
            )
        items = cur.fetchall()

    for item in items:
        bti_id = item["item_id"]
        ref_id = item["ref_id"]

        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_task_items SET status = 'running' WHERE id = %s", (bti_id,))
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (item["title"], bulk_task_id))

        try:
            # Step 1: Analyze
            with db.get_cursor() as cur:
                if item_type == "product":
                    cur.execute("SELECT * FROM synced_products WHERE id = %s", (ref_id,))
                else:
                    cur.execute("SELECT * FROM synced_collections WHERE id = %s", (ref_id,))
                full_item = cur.fetchone()

            if not full_item:
                raise ValueError(f"{item_type} {ref_id} not found")

            analysis, resolution = _resolve_and_analyze(item_type, full_item)
            store_id = full_item["store_id"]
            analysis_id = _upsert_analysis(store_id, item_type, ref_id, analysis, resolution)

            if only_fields:
                relevant_fixable = sum(
                    1 for c in analysis["checks"].values()
                    if c.get("issue") and c.get("fix_type") in only_fields
                )
            else:
                relevant_fixable = analysis["fixable_count"]
            if relevant_fixable == 0:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE id = %s",
                        (json.dumps({"skipped": True, "reason": "no fixable issues"}), bti_id),
                    )
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
                continue

            # Step 2: AI fix
            task_input = {"item_type": item_type, "item_id": ref_id, "analysis_id": analysis_id}
            if only_fields:
                task_input["only_fields"] = list(only_fields)
            ai_result = run_task("technical_seo_fix", task_input, store_id)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), bti_id),
                )

            if "error" in ai_result or "raw_response" in ai_result:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (ai_result.get("error") or "AI returned unparsable output", bti_id),
                    )
                    cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
                continue

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE technical_seo_analyses SET ai_fixes = %s, updated_at = NOW() WHERE id = %s",
                    (json.dumps(ai_result), analysis_id),
                )

            # Step 3: Apply (defense in depth: never apply outside only_fields)
            item_error = None
            ai_fixes = ai_result.get("fixes", [])
            if only_fields:
                ai_fixes = [f for f in ai_fixes if f.get("field") in only_fields]
            if ai_fixes:
                try:
                    client = ShopifyClient(
                        item["domain"],
                        decrypt_value(item["access_token_enc"]),
                        item["api_version"],
                    )

                    shopify_fields = {}
                    media_updates = []
                    for fix in ai_fixes:
                        field = fix.get("field")
                        value = fix.get("proposed_value")
                        if not field or not value:
                            continue
                        if field in ("meta_title", "meta_description", "title"):
                            shopify_fields[field] = value
                        elif field == "description":
                            shopify_fields["description_html"] = value
                        elif field == "tags" and isinstance(value, list):
                            shopify_fields["tags"] = value
                        elif field == "image_alt" and isinstance(value, list):
                            media_updates = value

                    _enforce_meta_limits(shopify_fields)
                    if shopify_fields.get("description_html"):
                        shopify_fields["description_html"] = sanitize_html(shopify_fields["description_html"])

                    # Snapshot before-state so View Impact has history
                    if shopify_fields or media_updates:
                        _record_change_event(
                            item_type, full_item, ai_fixes,
                            "issue_fix" if only_fields else "technical_fix",
                        )

                    if shopify_fields:
                        if item_type == "product":
                            client.update_product(item["shopify_id"], shopify_fields)
                        else:
                            client.update_collection(item["shopify_id"], shopify_fields)

                    if media_updates and item_type == "product":
                        client.update_product_media(item["shopify_id"], media_updates)

                    with db.get_cursor() as cur:
                        if item_type == "product":
                            for f, v in shopify_fields.items():
                                if f == "tags" and isinstance(v, list):
                                    cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(v), ref_id))
                                elif f == "title":
                                    cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (v, ref_id))
                                elif f == "meta_title":
                                    cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (v, ref_id))
                                elif f == "meta_description":
                                    cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (v, ref_id))
                                elif f == "description_html":
                                    cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (v, ref_id))
                            if media_updates:
                                images = full_item.get("images") or []
                                if isinstance(images, str):
                                    images = json.loads(images)
                                alt_map = {m["media_id"]: m["alt"] for m in media_updates if m.get("media_id")}
                                for img in images:
                                    if img.get("media_id") in alt_map:
                                        img["alt"] = alt_map[img["media_id"]]
                                cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), ref_id))
                            cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (ref_id,))
                        else:
                            for f, v in shopify_fields.items():
                                if f == "title":
                                    cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (v, ref_id))
                                elif f == "meta_title":
                                    cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (v, ref_id))
                                elif f == "meta_description":
                                    cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (v, ref_id))
                                elif f == "description_html":
                                    cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (v, ref_id))
                            cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (ref_id,))

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": True}), bti_id),
                        )

                    with db.get_cursor() as cur:
                        if item_type == "product":
                            cur.execute("SELECT * FROM synced_products WHERE id = %s", (ref_id,))
                        else:
                            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (ref_id,))
                        updated_item = cur.fetchone()

                    if updated_item:
                        new_analysis, new_resolution = _resolve_and_analyze(item_type, updated_item)
                        _upsert_analysis(store_id, item_type, ref_id, new_analysis, new_resolution)
                        with db.get_cursor() as cur:
                            cur.execute(
                                """UPDATE technical_seo_analyses SET ai_fixes = NULL
                                   WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                                (store_id, item_type, ref_id),
                            )

                except Exception as apply_err:
                    item_error = f"Apply to Shopify failed: {apply_err}"
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": False, "error": str(apply_err)}), bti_id),
                        )

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (item_error, bti_id),
                    )
                    cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
                else:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (bti_id,),
                    )
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))

        except Exception as e:
            logger.exception("Bulk fix-apply failed for %s %s", item_type, ref_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), bti_id),
                )
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


def _get_stored_analysis(store_id, item_type, item_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM technical_seo_analyses WHERE store_id = %s AND item_type = %s AND item_id = %s",
            (store_id, item_type, item_id),
        )
        return cur.fetchone()
