"""Interlinking analysis, AI recommendations, and apply/fix endpoints.

Apply paths never delete the stored analysis; they patch it in place via
app.services.interlinking_patch and flag it stale, so the page, dashboard,
and recommendations keep working between full re-analyses.
"""

import html as html_lib
import json
import logging
import re

import psycopg2
from flask import Blueprint, jsonify, render_template, request
from openai import OpenAI

from app.auth import decrypt_value
from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.ai_output_guard import sanitize_html, validate_injected_html
from app.services.interlinking_patch import (
    record_broken_fixed,
    record_link_added,
    record_noncanonical_fixed,
)
from app.services.task_runner import (
    is_failed_result,
    spawn as spawn_task,
    sweep_stale_jobs,
)
from app.settings_manager import get_setting
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

interlinking_bp = Blueprint("interlinking", __name__)


def _iso(dt):
    return dt.isoformat() if dt else None


def _analysis_running_task(store_id):
    """Return the id of a live analysis task for this store, if any.

    Tracked in ai_tasks (not a per-process dict) so the status is correct
    across all gunicorn workers.
    """
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id FROM ai_tasks
               WHERE store_id = %s AND task_type = 'interlinking_analysis'
                 AND status IN ('pending', 'running')
               ORDER BY id DESC LIMIT 1""",
            (store_id,),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def _run_analysis(store_id, task_id):
    try:
        from app.services.interlinking_analyzer import (
            analyze_store_interlinking,
            build_store_hosts,
        )

        with db.get_cursor() as cur:
            cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (store_id,))
            row = cur.fetchone()
        store_hosts = build_store_hosts(
            row["domain"] if row else None,
            row["primary_domain"] if row else None,
        )

        try:
            shopify_client = _get_shopify_client(store_id)
        except Exception:
            logger.warning("Could not build Shopify client for store %s; broken links unverified", store_id)
            shopify_client = None

        analyze_store_interlinking(store_id, store_hosts, shopify_client)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (task_id,),
            )
    except Exception as e:
        logger.exception("Interlinking analysis failed for store %s", store_id)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@interlinking_bp.route("/interlinking")
def interlinking_page():
    return render_template("interlinking.html")


# ── Analysis read/trigger/status ──────────────────────────────────


_UI_LIST_KEYS = (
    "orphan_products",
    "orphan_collections",
    "orphan_articles",
    "under_linked",
    "not_in_collection",
    "missing_crosslinks",
    "broken_links",
    "non_canonical_links",
)

_MAX_CROSSLINK_PAIRS = 500


def _trim_for_ui(analysis):
    """The stored blob carries all_links and full per-item details (multi-MB
    on large stores) for AI prompts and the patch layer; the page only needs
    the summary and the issue lists."""
    trimmed = {
        "summary": analysis.get("summary") or {},
        "stale": bool(analysis.get("stale")),
        "verification": analysis.get("verification"),
    }
    for key in _UI_LIST_KEYS:
        trimmed[key] = [
            {k: v for k, v in item.items() if k != "outgoing_links"}
            for item in analysis.get(key) or []
            if isinstance(item, dict)
        ]
    # A big store can produce thousands of crosslink pairs; ship the strongest
    # ones and say so instead of a megabyte of tail.
    pairs = trimmed["missing_crosslinks"]
    if len(pairs) > _MAX_CROSSLINK_PAIRS:
        pairs.sort(
            key=lambda p: (p.get("relatedness") or 0, p.get("shared_products") or 0),
            reverse=True,
        )
        trimmed["missing_crosslinks"] = pairs[:_MAX_CROSSLINK_PAIRS]
        trimmed["missing_crosslinks_truncated"] = len(pairs)
    return trimmed


@interlinking_bp.route("/api/interlinking/analysis")
def get_analysis():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT analysis_data, created_at FROM interlinking_analysis WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()

    if not row or not row["analysis_data"]:
        return jsonify({"analysis": None, "created_at": None})

    return jsonify({
        "analysis": _trim_for_ui(row["analysis_data"]),
        "created_at": _iso(row["created_at"]),
    })


@interlinking_bp.route("/api/interlinking/analyze", methods=["POST"])
def trigger_analysis():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    # Guarded insert; the partial unique index on ai_tasks makes a concurrent
    # duplicate fail atomically instead of double-starting a full analysis.
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO ai_tasks (store_id, task_type, status, input)
                   SELECT %s, 'interlinking_analysis', 'running', %s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM ai_tasks
                       WHERE store_id = %s AND task_type = 'interlinking_analysis'
                         AND status IN ('pending', 'running')
                   )
                   RETURNING id""",
                (store_id, json.dumps({"store_id": store_id}), store_id),
            )
            row = cur.fetchone()
    except psycopg2.IntegrityError:
        row = None

    if not row:
        return jsonify({
            "ok": True,
            "message": "Analysis already running",
            "task_id": _analysis_running_task(store_id),
        }), 200

    task_id = row["id"]
    spawn_task("ai_tasks", task_id, _run_analysis, store_id, task_id)

    return jsonify({"ok": True, "message": "Analysis started", "task_id": task_id}), 202


@interlinking_bp.route("/api/interlinking/status")
def analysis_status():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    sweep_stale_jobs()

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, error, created_at, completed_at FROM ai_tasks
               WHERE store_id = %s AND task_type = 'interlinking_analysis'
                 AND created_at > NOW() - INTERVAL '24 hours'
               ORDER BY id DESC LIMIT 1""",
            (store_id,),
        )
        task = cur.fetchone()

    payload = {"running": False, "task": None}
    if task:
        payload["running"] = task["status"] in ("pending", "running")
        payload["task"] = {
            "id": task["id"],
            "status": task["status"],
            "error": task["error"],
            "created_at": _iso(task["created_at"]),
            "completed_at": _iso(task["completed_at"]),
        }
    return jsonify(payload)


@interlinking_bp.route("/api/interlinking/task/<int:task_id>")
def get_interlinking_task(task_id):
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, result, error FROM ai_tasks
               WHERE id = %s AND store_id = %s AND task_type LIKE 'interlinking%%'""",
            (task_id, store_id),
        )
        task = cur.fetchone()

    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify({
        "id": task["id"],
        "status": task["status"],
        "result": task["result"],
        "error": task["error"],
    })


# ── Item link detail ──────────────────────────────────────────────


_DETAIL_LIST_BY_TYPE = {
    "product": "product_details",
    "collection": "collection_details",
    "article": "article_details",
}

_ADMIN_PATH_BY_TYPE = {
    "product": "products",
    "collection": "collections",
    "article": "content/articles",
}


def _links_from_analysis(analysis, item_type, handle, blog_handle):
    outgoing = []
    incoming = []
    for e in analysis.get("all_links") or []:
        if (
            e.get("from_type") == item_type
            and e.get("from_handle") == handle
            and (item_type != "article" or not blog_handle or e.get("from_blog_handle") == blog_handle)
        ):
            out = {
                "link_type": e.get("to_type"),
                "handle": e.get("to_handle"),
                "href": e.get("to_href", ""),
                "anchor_text": e.get("anchor_text", ""),
                "non_canonical": e.get("non_canonical", False),
            }
            if e.get("to_blog_handle"):
                out["blog_handle"] = e["to_blog_handle"]
            outgoing.append(out)
        if (
            e.get("to_type") == item_type
            and e.get("to_handle") == handle
            and (item_type != "article" or not blog_handle or e.get("to_blog_handle") == blog_handle)
        ):
            incoming.append({
                "from_type": e.get("from_type"),
                "from_handle": e.get("from_handle"),
                "from_title": e.get("from_title", ""),
                "anchor_text": e.get("anchor_text", ""),
            })
    return outgoing, incoming


def _detail_from_analysis(analysis, item_type, item_id):
    for d in analysis.get(_DETAIL_LIST_BY_TYPE[item_type]) or []:
        if d.get("id") == item_id:
            return {k: v for k, v in d.items() if k != "outgoing_links"}
    return None


def _item_urls(store_row, item_type, handle, blog_handle, shopify_id):
    urls = {}
    store_row = store_row or {}
    public = store_row.get("primary_domain") or store_row.get("domain")
    if public:
        base = public if public.startswith("http") else f"https://{public}"
        base = base.rstrip("/")
        if item_type == "product":
            urls["storefront"] = f"{base}/products/{handle}"
        elif item_type == "collection":
            urls["storefront"] = f"{base}/collections/{handle}"
        elif item_type == "article" and blog_handle:
            urls["storefront"] = f"{base}/blogs/{blog_handle}/{handle}"

    shop_domain = re.sub(r"^https?://", "", store_row.get("domain") or "")
    numeric = str(shopify_id or "").rsplit("/", 1)[-1]
    if ".myshopify.com" in shop_domain and numeric.isdigit():
        sub = shop_domain.split(".myshopify.com")[0]
        urls["admin"] = f"https://admin.shopify.com/store/{sub}/{_ADMIN_PATH_BY_TYPE[item_type]}/{numeric}"
    return urls


@interlinking_bp.route("/api/interlinking/item/<item_type>/<int:item_id>")
def get_item_links(item_type, item_id):
    if item_type not in ("product", "collection", "article"):
        return jsonify({"error": "Invalid type"}), 400

    blog_handle = None
    if item_type == "article":
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT sa.id, sa.store_id, sa.shopify_id, sa.title, sa.handle, sa.body,
                          sb.handle AS blog_handle
                   FROM synced_articles sa
                   JOIN synced_blogs sb ON sb.id = sa.blog_id
                   WHERE sa.id = %s""",
                (item_id,),
            )
            item = cur.fetchone()
        if item:
            blog_handle = item["blog_handle"]
    else:
        table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(
                f"SELECT id, store_id, shopify_id, title, handle, description FROM {table} WHERE id = %s",
                (item_id,),
            )
            item = cur.fetchone()

    if not item:
        return jsonify({"error": "Item not found"}), 404

    store_id = item["store_id"]
    handle = item["handle"]

    with db.get_cursor() as cur:
        cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (store_id,))
        store_row = cur.fetchone()

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
            (store_id,),
        )
        arow = cur.fetchone()
    analysis = arow["analysis_data"] if arow else None

    detail = None
    if analysis and isinstance(analysis.get("all_links"), list):
        outgoing, incoming = _links_from_analysis(analysis, item_type, handle, blog_handle)
        detail = _detail_from_analysis(analysis, item_type, item_id)
    else:
        # No stored analysis — extract this item's own links live; incoming
        # links require a full analysis pass.
        from app.services.interlinking_analyzer import build_store_hosts, extract_links_from_html

        store_hosts = build_store_hosts(
            store_row["domain"] if store_row else None,
            store_row["primary_domain"] if store_row else None,
        )
        html_field = "body" if item_type == "article" else "description"
        outgoing = extract_links_from_html(item.get(html_field), store_hosts)
        incoming = []

    item_response = {
        "id": item["id"],
        "title": item["title"],
        "handle": handle,
        "type": item_type,
        "item_type": item_type,
    }
    if blog_handle:
        item_response["blog_handle"] = blog_handle

    return jsonify({
        "item": item_response,
        "detail": detail,
        "outgoing_links": outgoing,
        "incoming_links": incoming,
        "urls": _item_urls(store_row, item_type, handle, blog_handle, item.get("shopify_id")),
    })


# ── AI recommendations ────────────────────────────────────────────


@interlinking_bp.route("/api/interlinking/recommendations", methods=["POST"])
def trigger_recommendations():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()

    if not row or not row["analysis_data"]:
        return jsonify({"error": "Run analysis first"}), 400

    from app.ai_agent import run_task

    max_fixes = data.get("max_fixes", 10)
    max_crosslinks = data.get("max_crosslinks", 5)
    task_input = {
        "store_id": store_id,
        "max_fixes": max_fixes,
        "max_crosslinks": max_crosslinks,
    }

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, %s, %s) RETURNING id",
            (store_id, "interlinking_recommendations", json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

    def _run_recommendations(tid):
        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (tid,))
        try:
            result = run_task("interlinking_recommendations", task_input, store_id)
            if is_failed_result(result):
                err = result.get("error") if isinstance(result, dict) else None
                raise ValueError(err or "AI returned unusable output")
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), tid),
                )
        except Exception as e:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), tid),
                )

    spawn_task("ai_tasks", task_id, _run_recommendations, task_id)

    return jsonify({"ok": True, "task_id": task_id}), 202


@interlinking_bp.route("/api/interlinking/recommendations/latest")
def latest_recommendations():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, result, completed_at FROM ai_tasks
               WHERE store_id = %s AND task_type = 'interlinking_recommendations'
                 AND status = 'completed'
                 AND completed_at > NOW() - INTERVAL '7 days'
               ORDER BY id DESC LIMIT 1""",
            (store_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"task_id": None, "result": None, "completed_at": None})

    return jsonify({
        "task_id": row["id"],
        "result": row["result"],
        "completed_at": _iso(row["completed_at"]),
    })


_ORPHAN_FIX_KEYS = {
    "collection": "orphan_collections",
    "article": "orphan_articles",
    "product": "orphan_products",
}


@interlinking_bp.route("/api/interlinking/orphan-fix", methods=["POST"])
def trigger_orphan_fix():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    scope = data.get("scope") or data.get("item_type") or "collection"
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    if scope != "under_linked" and scope not in _ORPHAN_FIX_KEYS:
        return jsonify({"error": f"Unsupported scope: {scope}"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()
    if not row or not row["analysis_data"]:
        return jsonify({"error": "Run analysis first"}), 400

    from app.ai_agent import run_task

    if scope == "under_linked":
        if not (row["analysis_data"].get("under_linked") or []):
            return jsonify({"error": "No under-linked pages to fix"}), 400
        task_input = {
            "store_id": store_id,
            "scope": "under_linked",
            "limit": data.get("limit", 25),
            "links_per_page": data.get("links_per_page", 3),
        }
        target_count = min(
            int(data.get("limit") or 25),
            len(row["analysis_data"].get("under_linked") or []),
        )
    else:
        handles = data.get("handles") or [
            c.get("handle")
            for c in (row["analysis_data"].get(_ORPHAN_FIX_KEYS[scope]) or [])
            if c.get("handle")
        ]
        if not handles:
            return jsonify({"error": f"No orphan {scope}s to fix"}), 400
        task_input = {
            "store_id": store_id,
            "scope": scope,
            "item_type": scope,
            "handles": handles,
            "links_per_page": data.get("links_per_page", 3),
        }
        target_count = len(handles)

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, %s, %s) RETURNING id",
            (store_id, "interlinking_orphan_fix", json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

    def _run_orphan_fix(tid):
        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (tid,))
        try:
            result = run_task("interlinking_orphan_fix", task_input, store_id)
            if is_failed_result(result):
                err = result.get("error") if isinstance(result, dict) else None
                raise ValueError(err or "AI returned unusable output")
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), tid),
                )
        except Exception as e:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), tid),
                )

    spawn_task("ai_tasks", task_id, _run_orphan_fix, task_id)

    return jsonify({"ok": True, "task_id": task_id, "target_count": target_count}), 202


@interlinking_bp.route("/api/interlinking/orphan-fix/latest")
def latest_orphan_fix():
    store_id = request.args.get("store_id", type=int)
    scope = request.args.get("scope") or request.args.get("item_type") or "collection"
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, result, completed_at FROM ai_tasks
               WHERE store_id = %s AND task_type = 'interlinking_orphan_fix'
                 AND COALESCE(input->>'scope', input->>'item_type') = %s
                 AND status = 'completed'
                 AND completed_at > NOW() - INTERVAL '7 days'
               ORDER BY id DESC LIMIT 1""",
            (store_id, scope),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"task_id": None, "result": None, "completed_at": None})

    return jsonify({
        "task_id": row["id"],
        "result": row["result"],
        "completed_at": _iso(row["completed_at"]),
    })


# ── Apply interlink helpers ────────────────────────────────────────


def _get_ai_config():
    from app.ai_agent import _get_ai_config as _ai_cfg
    return _ai_cfg()


def _get_shopify_client(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()
    if not store:
        raise ValueError(f"Store {store_id} not found")
    return ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )


def _get_store_hosts(store_id):
    from app.services.interlinking_analyzer import build_store_hosts

    with db.get_cursor() as cur:
        cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone()
    return build_store_hosts(
        row["domain"] if row else None,
        row["primary_domain"] if row else None,
    )


def _resolve_item(store_id, item_type, handle, blog_handle=None):
    if item_type == "product":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, shopify_id, title, description FROM synced_products WHERE store_id = %s AND handle = %s",
                (store_id, handle),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "shopify_id": row["shopify_id"],
            "title": row["title"],
            "html": row["description"] or "",
            "item_type": "product",
        }
    elif item_type == "collection":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, shopify_id, title, description FROM synced_collections WHERE store_id = %s AND handle = %s",
                (store_id, handle),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "shopify_id": row["shopify_id"],
            "title": row["title"],
            "html": row["description"] or "",
            "item_type": "collection",
        }
    elif item_type == "article":
        if blog_handle:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sa.id, sa.shopify_id, sa.title, sa.body, sb.handle AS blog_handle
                       FROM synced_articles sa
                       JOIN synced_blogs sb ON sb.id = sa.blog_id
                       WHERE sa.store_id = %s AND sa.handle = %s AND sb.handle = %s""",
                    (store_id, handle, blog_handle),
                )
                rows = cur.fetchall()
        else:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sa.id, sa.shopify_id, sa.title, sa.body, sb.handle AS blog_handle
                       FROM synced_articles sa
                       JOIN synced_blogs sb ON sb.id = sa.blog_id
                       WHERE sa.store_id = %s AND sa.handle = %s
                       ORDER BY sa.id""",
                    (store_id, handle),
                )
                rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            # Same handle in several blogs and no blog specified — refuse to
            # guess which article to edit.
            return {"ambiguous": True, "item_type": "article", "handle": handle}
        row = rows[0]
        return {
            "id": row["id"],
            "shopify_id": row["shopify_id"],
            "title": row["title"],
            "html": row["body"] or "",
            "item_type": "article",
            "blog_handle": row["blog_handle"],
        }
    return None


def _build_target_url(to_type, to_handle, blog_handle=None):
    if to_type == "product":
        return f"/products/{to_handle}"
    if to_type == "collection":
        return f"/collections/{to_handle}"
    if to_type == "article":
        if not blog_handle:
            raise ValueError(f"Article link to '{to_handle}' is missing its blog handle")
        return f"/blogs/{blog_handle}/{to_handle}"
    raise ValueError(f"Unsupported link target type: {to_type!r}")


def _resolve_article_blog_handle(store_id, handle):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sb.handle AS blog_handle
               FROM synced_articles sa
               JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.handle = %s""",
            (store_id, handle),
        )
        rows = cur.fetchall()
    return rows[0]["blog_handle"] if len(rows) == 1 else None


def _link_already_exists(html, to_type, to_handle, blog_handle=None, store_hosts=None):
    """Canonical-identity comparison (locale prefixes, entities, and query
    strings all normalized by the extractor) instead of raw string matching."""
    if not html:
        return False
    from app.services.interlinking_analyzer import extract_links_from_html

    for link in extract_links_from_html(html, store_hosts or set()):
        if link["link_type"] != to_type or link["handle"] != to_handle:
            continue
        if to_type == "article" and blog_handle and link.get("blog_handle") != blog_handle:
            continue
        return True
    return False


def _inject_link_via_ai(current_html, links_to_add, config):
    if not current_html or not current_html.strip():
        parts = []
        for link in links_to_add:
            parts.append(
                '<a href="{}">{}</a>'.format(
                    html_lib.escape(link["url"], quote=True),
                    html_lib.escape(link["anchor_text"]),
                )
            )
        return "<p>" + " ".join(parts) + "</p>"

    prompt_template = get_setting("prompts.inject_interlink", DEFAULTS.get("prompts.inject_interlink", ""))
    links_json = json.dumps(links_to_add, indent=2)
    # .replace, not .format — a user-edited prompt containing a stray { must
    # not crash every apply.
    prompt = prompt_template.replace("{links_json}", links_json).replace("{current_html}", current_html)

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config["api_key"],
    )

    response = client.chat.completions.create(
        model=config["model"],
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )

    result = response.choices[0].message.content or ""
    result = re.sub(r"^```(?:html)?\s*\n?", "", result)
    result = re.sub(r"\n?```\s*$", "", result)
    return result.strip()


def _push_html_to_shopify(client, item_type, shopify_id, new_html):
    if item_type == "product":
        client.update_product(shopify_id, {"description_html": new_html})
    elif item_type == "collection":
        client.update_collection(shopify_id, {"description_html": new_html})
    elif item_type == "article":
        client.update_article(shopify_id, {"body": new_html})


def _update_local_db(item_type, item_id, new_html):
    table_map = {
        "product": ("synced_products", "description"),
        "collection": ("synced_collections", "description"),
        "article": ("synced_articles", "body"),
    }
    table, col = table_map[item_type]
    with db.get_cursor() as cur:
        cur.execute(f"UPDATE {table} SET {col} = %s WHERE id = %s", (new_html, item_id))


def _current_summary(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()
    if not row or not row["analysis_data"]:
        return None
    a = row["analysis_data"]
    return {"summary": a.get("summary") or {}, "stale": bool(a.get("stale"))}


def _get_target_index(store_id):
    from app.services.interlinking_analyzer import _RESERVED_COLLECTION_HANDLES

    idx = {}
    for t in _get_available_targets(store_id):
        idx[(t["type"], t["handle"])] = t
    for h in _RESERVED_COLLECTION_HANDLES:
        idx.setdefault(("collection", h), {"type": "collection", "handle": h, "title": h})
    return idx


def _apply_link_fixes_to_source(store_id, shopify_client, config, source_ref, fixes, target_index, store_hosts):
    """Inject one or more links into a single source page. Returns per-fix
    result dicts: {from_handle, to_handle, status, reason?, warning?}."""
    from_type = source_ref["from_type"]
    from_handle = source_ref["from_handle"]
    from_blog = source_ref.get("from_blog_handle") or None

    def _all_errored(reason):
        return [
            {
                "from_handle": f.get("from_handle") or from_handle,
                "to_handle": f.get("to_handle"),
                "status": "error",
                "reason": reason,
            }
            for f in fixes
        ]

    source = _resolve_item(store_id, from_type, from_handle, from_blog)
    if not source:
        return _all_errored(f"Source {from_type} '{from_handle}' not found")
    if source.get("ambiguous"):
        return _all_errored(
            f"Article handle '{from_handle}' exists in multiple blogs — specify from_blog_handle"
        )

    results = []
    links_to_add = []
    for fix in fixes:
        to_type = fix.get("to_type")
        to_handle = fix.get("to_handle")
        base = {"from_handle": from_handle, "to_handle": to_handle}
        target = target_index.get((to_type, to_handle))
        if not target:
            # AI-suggested handles are only trusted after this check — a
            # hallucinated target would otherwise become a new broken link.
            results.append({**base, "status": "error",
                            "reason": f"Target {to_type} '{to_handle}' does not exist in the catalog"})
            continue
        to_blog = fix.get("to_blog_handle") or target.get("blog_handle")
        try:
            target_url = _build_target_url(to_type, to_handle, to_blog)
        except ValueError as e:
            results.append({**base, "status": "error", "reason": str(e)})
            continue
        if _link_already_exists(source["html"], to_type, to_handle, to_blog, store_hosts):
            results.append({**base, "status": "skipped", "reason": "Link already exists"})
            continue
        links_to_add.append({
            "url": target_url,
            # Anchor equity should flow to the target's resolved primary
            # keyword when the AI didn't propose an anchor — title is the
            # last resort, not the default.
            "anchor_text": (fix.get("suggested_anchor") or target.get("primary_keyword")
                            or target.get("title") or to_handle),
            "_fix": fix,
            "_target": target,
            "_to_blog": to_blog,
        })

    if not links_to_add:
        return results

    pushed = False
    recorded_handles = set()
    try:
        ai_links = [{"url": l["url"], "anchor_text": l["anchor_text"]} for l in links_to_add]
        new_html = sanitize_html(_inject_link_via_ai(source["html"], ai_links, config))
        ok, reason = validate_injected_html(
            source["html"], new_html, expected_hrefs=[l["url"] for l in ai_links]
        )
        if not ok:
            raise ValueError(f"AI output rejected: {reason}")

        _push_html_to_shopify(shopify_client, source["item_type"], source["shopify_id"], new_html)
        pushed = True
        warning = None
        try:
            _update_local_db(source["item_type"], source["id"], new_html)
        except Exception:
            logger.exception("Local cache update failed for %s '%s'", from_type, from_handle)
            warning = "Applied to Shopify, but the local cache update failed — re-sync recommended"

        for l in links_to_add:
            fix = l["_fix"]
            record_link_added(
                store_id,
                {
                    "type": source["item_type"],
                    "handle": from_handle,
                    "blog_handle": source.get("blog_handle") or from_blog,
                    "title": source.get("title"),
                },
                {
                    "type": fix.get("to_type"),
                    "handle": fix.get("to_handle"),
                    "blog_handle": l["_to_blog"],
                    "href": l["url"],
                    "title": l["_target"].get("title"),
                },
                anchor_text=l["anchor_text"],
            )
            recorded_handles.add(fix.get("to_handle"))
            r = {"from_handle": from_handle, "to_handle": fix.get("to_handle"), "status": "applied"}
            if warning:
                r["warning"] = warning
            results.append(r)
    except Exception as e:
        logger.exception("Failed to apply interlink fixes for %s '%s'", from_type, from_handle)
        # The Shopify push is one atomic call. Links already recorded keep
        # their "applied" rows; the rest are "applied with warning" if the
        # push happened (they ARE live) or "error" if it never did.
        for l in links_to_add:
            to_handle = l["_fix"].get("to_handle")
            if to_handle in recorded_handles:
                continue
            if pushed:
                results.append({
                    "from_handle": from_handle,
                    "to_handle": to_handle,
                    "status": "applied",
                    "warning": f"Link is live on Shopify but post-apply bookkeeping failed: {e}",
                })
            else:
                results.append({
                    "from_handle": from_handle,
                    "to_handle": to_handle,
                    "status": "error",
                    "reason": str(e),
                })
    return results


# ── Apply endpoints ────────────────────────────────────────────────


@interlinking_bp.route("/api/interlinking/apply-fix", methods=["POST"])
def apply_fix():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not all([store_id, data.get("from_type"), data.get("from_handle"),
                data.get("to_type"), data.get("to_handle")]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        config = _get_ai_config()
        shopify_client = _get_shopify_client(store_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    source_ref = {
        "from_type": data["from_type"],
        "from_handle": data["from_handle"],
        "from_blog_handle": data.get("from_blog_handle"),
    }
    results = _apply_link_fixes_to_source(
        store_id, shopify_client, config, source_ref, [data],
        _get_target_index(store_id), _get_store_hosts(store_id),
    )
    result = results[0] if results else {"status": "error", "reason": "Nothing to apply"}

    if result.get("status") == "error":
        reason = result.get("reason") or "Apply failed"
        if "not found" in reason:
            code = 404
        elif "does not exist" in reason or "rejected" in reason or "multiple blogs" in reason:
            code = 422
        else:
            code = 500
        return jsonify({"error": reason}), code

    payload = {
        "ok": True,
        "status": result["status"],
        "from_handle": data["from_handle"],
        "to_handle": data["to_handle"],
    }
    if result.get("reason"):
        payload["reason"] = result["reason"]
    if result.get("warning"):
        payload["warning"] = result["warning"]
    summary = _current_summary(store_id)
    if summary:
        payload.update(summary)
    return jsonify(payload)


_BATCH_KINDS = ("ai_links", "broken", "noncanonical")


def _run_apply_batch(store_id, kind, items, task_id):
    results = []
    total = len(items)

    def _write_progress(status=None):
        failed = sum(1 for r in results if r.get("status") == "error")
        payload = {
            "kind": kind,
            "progress": {"total": total, "completed": len(results), "failed": failed},
            "results": results,
        }
        summary = _current_summary(store_id)
        if summary:
            payload.update(summary)
        with db.get_cursor() as cur:
            if status:
                cur.execute(
                    "UPDATE ai_tasks SET status = %s, result = %s, completed_at = NOW() WHERE id = %s",
                    (status, json.dumps(payload), task_id),
                )
            else:
                cur.execute(
                    "UPDATE ai_tasks SET result = %s WHERE id = %s",
                    (json.dumps(payload), task_id),
                )

    try:
        shopify_client = _get_shopify_client(store_id)
        if kind == "ai_links":
            config = _get_ai_config()
            target_index = _get_target_index(store_id)
            store_hosts = _get_store_hosts(store_id)
            groups = {}
            for fix in items:
                key = (fix.get("from_type", ""), fix.get("from_handle", ""), fix.get("from_blog_handle") or "")
                groups.setdefault(key, []).append(fix)
            for (from_type, from_handle, from_blog), fixes in groups.items():
                source_ref = {
                    "from_type": from_type,
                    "from_handle": from_handle,
                    "from_blog_handle": from_blog or None,
                }
                results.extend(
                    _apply_link_fixes_to_source(
                        store_id, shopify_client, config, source_ref, fixes, target_index, store_hosts
                    )
                )
                _write_progress()
        else:
            for item in items:
                res = _apply_broken_fix(
                    store_id, shopify_client, item, noncanonical=(kind == "noncanonical")
                )
                results.append({
                    "from_handle": item.get("from_handle"),
                    "to_handle": item.get("to_handle"),
                    "to_href": item.get("to_href"),
                    **res,
                })
                _write_progress()

        failed = sum(1 for r in results if r.get("status") == "error")
        _write_progress("completed_with_errors" if failed else "completed")
    except Exception as e:
        logger.exception("Interlinking apply batch %s failed", task_id)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@interlinking_bp.route("/api/interlinking/apply-batch", methods=["POST"])
def apply_batch():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    kind = data.get("kind")
    items = data.get("items") or []
    if not store_id or kind not in _BATCH_KINDS or not items:
        return jsonify({"error": "store_id, a valid kind, and items are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'interlinking_apply', 'running', %s) RETURNING id""",
            (store_id, json.dumps({"kind": kind, "count": len(items)})),
        )
        task_id = cur.fetchone()["id"]

    spawn_task("ai_tasks", task_id, _run_apply_batch, store_id, kind, items, task_id)
    return jsonify({"ok": True, "task_id": task_id}), 202


# ── Broken link fix helpers ───────────────────────────────────────


def _get_available_targets(store_id):
    targets = []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sp.handle, sp.title,
                      tsa.keyword_resolution->'primary'->>'keyword' AS primary_keyword
               FROM synced_products sp
               LEFT JOIN technical_seo_analyses tsa
                 ON tsa.store_id = sp.store_id AND tsa.item_type = 'product' AND tsa.item_id = sp.id
               WHERE sp.store_id = %s AND sp.status = 'ACTIVE'""",
            (store_id,),
        )
        for row in cur.fetchall():
            targets.append({"type": "product", "handle": row["handle"], "title": row["title"],
                            "primary_keyword": row["primary_keyword"]})

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sc.handle, sc.title,
                      tsa.keyword_resolution->'primary'->>'keyword' AS primary_keyword
               FROM synced_collections sc
               LEFT JOIN technical_seo_analyses tsa
                 ON tsa.store_id = sc.store_id AND tsa.item_type = 'collection' AND tsa.item_id = sc.id
               WHERE sc.store_id = %s""",
            (store_id,),
        )
        for row in cur.fetchall():
            targets.append({"type": "collection", "handle": row["handle"], "title": row["title"],
                            "primary_keyword": row["primary_keyword"]})

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.handle, sa.title, sb.handle AS blog_handle
               FROM synced_articles sa
               JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.is_published = TRUE""",
            (store_id,),
        )
        for row in cur.fetchall():
            targets.append({
                "type": "article",
                "handle": row["handle"],
                "title": row["title"],
                "blog_handle": row["blog_handle"],
            })

    return targets


def _handle_similarity(broken_handle, candidate_handle):
    broken_parts = set(broken_handle.split("-"))
    candidate_parts = set(candidate_handle.split("-"))
    if not broken_parts or not candidate_parts:
        return 0.0
    intersection = broken_parts & candidate_parts
    union = broken_parts | candidate_parts
    return len(intersection) / len(union)


def _find_replacement(broken_handle, broken_type, targets):
    best = None
    best_score = 0.0
    for t in targets:
        score = _handle_similarity(broken_handle, t["handle"])
        if t["type"] == broken_type:
            score += 0.1
        if score > best_score:
            best_score = score
            best = t
    if best_score >= 0.3:
        return best
    return None


_ANCHOR_RE = re.compile(
    r'(<a\b[^>]*\bhref\s*=\s*)(["\'])(.*?)\2([^>]*>)(.*?)(</a\s*>)',
    re.IGNORECASE | re.DOTALL,
)


def _replace_href_in_html(html, broken_href, new_href):
    """Swap (or, when new_href is falsy, unwrap) every anchor whose href
    resolves to broken_href. Only the href attribute value is rewritten —
    anchor text and other attributes are never touched — and both sides are
    compared in normalized form so entities, locale prefixes, and query
    strings can't cause silent mismatches."""
    from app.services.interlinking_analyzer import normalize_href_path

    target = normalize_href_path(broken_href)
    if not target:
        return html

    def _replacer(match):
        if normalize_href_path(match.group(3)) != target:
            return match.group(0)
        if new_href:
            escaped = html_lib.escape(new_href, quote=True)
            return (
                match.group(1) + match.group(2) + escaped + match.group(2)
                + match.group(4) + match.group(5) + match.group(6)
            )
        return re.sub(r"<[^>]+>", "", match.group(5)).strip()

    return _ANCHOR_RE.sub(_replacer, html)


def _apply_broken_fix(store_id, shopify_client, item, noncanonical=False):
    """Replace or remove one (source page, target href) pair. Returns
    {status: fixed|skipped|error, reason?, warning?}."""
    to_handle = item.get("to_handle")
    from_type = item.get("from_type")
    from_handle = item.get("from_handle")
    from_blog = item.get("from_blog_handle") or None

    replacement_handle = item.get("replacement_handle")
    mode = "replace" if replacement_handle else item.get("mode")
    if mode not in ("replace", "remove"):
        return {"status": "error", "reason": "Specify a replacement or set mode='remove'"}

    try:
        broken_href = item.get("to_href") or _build_target_url(
            item.get("to_type") or "product", to_handle
        )

        new_href = None
        if mode == "replace":
            rep_type = item.get("replacement_type") or item.get("to_type") or "product"
            rep_blog = item.get("replacement_blog_handle")
            if rep_type == "article" and not rep_blog:
                rep_blog = _resolve_article_blog_handle(store_id, replacement_handle)
            new_href = _build_target_url(rep_type, replacement_handle, rep_blog)

        source = _resolve_item(store_id, from_type, from_handle, from_blog)
        if not source:
            return {"status": "error", "reason": f"Source {from_type} '{from_handle}' not found"}
        if source.get("ambiguous"):
            return {
                "status": "error",
                "reason": f"Article handle '{from_handle}' exists in multiple blogs — specify from_blog_handle",
            }

        new_html = _replace_href_in_html(source["html"], broken_href, new_href)
        if new_html == source["html"]:
            return {"status": "skipped", "reason": "Link not found in the page content"}

        _push_html_to_shopify(shopify_client, source["item_type"], source["shopify_id"], new_html)
        warning = None
        try:
            _update_local_db(source["item_type"], source["id"], new_html)
        except Exception:
            logger.exception("Local cache update failed for %s '%s'", from_type, from_handle)
            warning = "Applied to Shopify, but the local cache update failed — re-sync recommended"

        recorder = record_noncanonical_fixed if noncanonical else record_broken_fixed
        recorder(store_id, broken_href, source["item_type"], from_handle, source.get("blog_handle") or from_blog)
        if new_href:
            record_link_added(
                store_id,
                {
                    "type": source["item_type"],
                    "handle": from_handle,
                    "blog_handle": source.get("blog_handle") or from_blog,
                    "title": source.get("title"),
                },
                {
                    "type": item.get("replacement_type") or item.get("to_type") or "product",
                    "handle": replacement_handle,
                    "href": new_href,
                },
            )

        result = {"status": "fixed"}
        if warning:
            result["warning"] = warning
        return result
    except Exception as e:
        logger.exception("Failed to fix broken link in %s", from_handle)
        return {"status": "error", "reason": str(e)}


# ── Broken link fix endpoints ─────────────────────────────────────


@interlinking_bp.route("/api/interlinking/available-targets")
def available_targets():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    return jsonify(_get_available_targets(store_id))


@interlinking_bp.route("/api/interlinking/suggest-replacement")
def suggest_replacement():
    store_id = request.args.get("store_id", type=int)
    handle = request.args.get("handle", "")
    link_type = request.args.get("type", "product")
    if not store_id or not handle:
        return jsonify({"error": "store_id and handle are required"}), 400
    return jsonify({
        "suggestion": _find_replacement(handle, link_type, _get_available_targets(store_id)),
    })


@interlinking_bp.route("/api/interlinking/fix-broken-link", methods=["POST"])
def fix_broken_link():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not all([store_id, data.get("to_handle") or data.get("to_href"),
                data.get("from_type"), data.get("from_handle")]):
        return jsonify({"error": "Missing required fields"}), 400
    if not data.get("replacement_handle") and data.get("mode") != "remove":
        # Explicit intent required — a missing replacement must never silently
        # mean "delete the link from live content".
        return jsonify({"error": "Specify a replacement or set mode='remove'"}), 400

    try:
        shopify_client = _get_shopify_client(store_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = _apply_broken_fix(
        store_id, shopify_client, data, noncanonical=bool(data.get("noncanonical"))
    )
    if result["status"] == "error":
        return jsonify({"error": result.get("reason") or "Fix failed"}), 500

    payload = {"ok": True, "status": result["status"]}
    if result.get("reason"):
        payload["reason"] = result["reason"]
    if result.get("warning"):
        payload["warning"] = result["warning"]
    summary = _current_summary(store_id)
    if summary:
        payload.update(summary)
    return jsonify(payload)
