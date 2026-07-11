import json
import logging
import re
import threading
from urllib.parse import urlparse

from flask import Blueprint, jsonify, render_template, request
from openai import OpenAI

from app.auth import decrypt_value
from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.ai_output_guard import validate_injected_html
from app.settings_manager import get_setting
from app.shopify_client import ShopifyClient
from app.services.task_runner import spawn as spawn_task

logger = logging.getLogger(__name__)

interlinking_bp = Blueprint("interlinking", __name__)

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
                 AND created_at > NOW() - INTERVAL '30 minutes'
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

    if not row:
        return jsonify({"analysis": None, "created_at": None})

    return jsonify({
        "analysis": row["analysis_data"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    })


@interlinking_bp.route("/api/interlinking/analyze", methods=["POST"])
def trigger_analysis():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    if _analysis_running_task(store_id):
        return jsonify({"ok": True, "message": "Analysis already running"}), 200

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'interlinking_analysis', 'running', %s) RETURNING id""",
            (store_id, json.dumps({"store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    spawn_task("ai_tasks", task_id, _run_analysis, store_id, task_id)

    return jsonify({"ok": True, "message": "Analysis started"}), 202


@interlinking_bp.route("/api/interlinking/status")
def analysis_status():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    return jsonify({"running": _analysis_running_task(store_id) is not None})


@interlinking_bp.route("/api/interlinking/item/<item_type>/<int:item_id>")
def get_item_links(item_type, item_id):
    if item_type not in ("product", "collection", "article"):
        return jsonify({"error": "Invalid type"}), 400

    from app.services.interlinking_analyzer import extract_links_from_html, build_store_hosts

    blog_handle = None
    if item_type == "article":
        with db.get_cursor() as cur:
            cur.execute(
                """
                SELECT sa.*, sb.handle AS blog_handle
                FROM synced_articles sa
                JOIN synced_blogs sb ON sb.id = sa.blog_id
                WHERE sa.id = %s
                """,
                (item_id,),
            )
            item = cur.fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404
        blog_handle = item["blog_handle"]
    else:
        table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
            item = cur.fetchone()
        if not item:
            return jsonify({"error": "Item not found"}), 404

    with db.get_cursor() as cur:
        cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (item["store_id"],))
        store_row = cur.fetchone()
    store_hosts = build_store_hosts(
        store_row["domain"] if store_row else None,
        store_row["primary_domain"] if store_row else None,
    )

    html_field = "body" if item_type == "article" else "description"
    outgoing = extract_links_from_html(item.get(html_field), store_hosts)

    target_handle = item.get("handle")
    incoming = []

    def _matches_target(link):
        if item_type == "article":
            return (
                link["link_type"] == "article"
                and link["handle"] == target_handle
                and link.get("blog_handle") == blog_handle
            )
        return link["handle"] == target_handle and link["link_type"] == item_type

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, title, handle, description FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
            (item["store_id"],),
        )
        all_products = cur.fetchall()

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, title, handle, description FROM synced_collections WHERE store_id = %s",
            (item["store_id"],),
        )
        all_collections = cur.fetchall()

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sa.id, sa.title, sa.handle, sa.body, sb.handle AS blog_handle
            FROM synced_articles sa
            JOIN synced_blogs sb ON sb.id = sa.blog_id
            WHERE sa.store_id = %s AND sa.is_published = TRUE
            """,
            (item["store_id"],),
        )
        all_articles = cur.fetchall()

    for p in all_products:
        for link in extract_links_from_html(p.get("description"), store_hosts):
            if _matches_target(link):
                incoming.append({
                    "from_type": "product",
                    "from_handle": p["handle"],
                    "from_title": p["title"],
                    "anchor_text": link["anchor_text"],
                })

    for c in all_collections:
        for link in extract_links_from_html(c.get("description"), store_hosts):
            if _matches_target(link):
                incoming.append({
                    "from_type": "collection",
                    "from_handle": c["handle"],
                    "from_title": c["title"],
                    "anchor_text": link["anchor_text"],
                })

    for a in all_articles:
        for link in extract_links_from_html(a.get("body"), store_hosts):
            if _matches_target(link):
                incoming.append({
                    "from_type": "article",
                    "from_handle": a["handle"],
                    "from_title": a["title"],
                    "anchor_text": link["anchor_text"],
                })

    item_response = {
        "id": item["id"],
        "title": item["title"],
        "handle": item.get("handle"),
        "type": item_type,
    }
    if blog_handle:
        item_response["blog_handle"] = blog_handle

    return jsonify({
        "item": item_response,
        "outgoing_links": outgoing,
        "incoming_links": incoming,
    })


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


@interlinking_bp.route("/api/interlinking/recommendations/<int:task_id>")
def get_recommendations(task_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()

    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify({
        "id": task["id"],
        "status": task["status"],
        "result": task["result"],
        "error": task["error"],
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


def _resolve_item(store_id, item_type, handle, blog_handle=None):
    if item_type == "product":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, shopify_id, description FROM synced_products WHERE store_id = %s AND handle = %s",
                (store_id, handle),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "shopify_id": row["shopify_id"],
            "html": row["description"] or "",
            "table": "synced_products",
            "html_field": "description",
            "item_type": "product",
        }
    elif item_type == "collection":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, shopify_id, description FROM synced_collections WHERE store_id = %s AND handle = %s",
                (store_id, handle),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "shopify_id": row["shopify_id"],
            "html": row["description"] or "",
            "table": "synced_collections",
            "html_field": "description",
            "item_type": "collection",
        }
    elif item_type == "article":
        if blog_handle:
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    SELECT sa.id, sa.shopify_id, sa.body
                    FROM synced_articles sa
                    JOIN synced_blogs sb ON sb.id = sa.blog_id
                    WHERE sa.store_id = %s AND sa.handle = %s AND sb.handle = %s
                    """,
                    (store_id, handle, blog_handle),
                )
                row = cur.fetchone()
        else:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, shopify_id, body FROM synced_articles WHERE store_id = %s AND handle = %s",
                    (store_id, handle),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "shopify_id": row["shopify_id"],
            "html": row["body"] or "",
            "table": "synced_articles",
            "html_field": "body",
            "item_type": "article",
        }
    return None


def _build_target_url(to_type, to_handle, blog_handle=None):
    if to_type == "product":
        return f"/products/{to_handle}"
    elif to_type == "collection":
        return f"/collections/{to_handle}"
    elif to_type == "article":
        if blog_handle:
            return f"/blogs/{blog_handle}/{to_handle}"
        return f"/blogs/{to_handle}"
    return f"/{to_handle}"


def _link_already_exists(html, target_url):
    if not html:
        return False
    from app.services.interlinking_analyzer import LINK_RE
    target_clean = target_url.rstrip("/")
    for match in LINK_RE.finditer(html):
        href = match.group(1).strip()
        href_clean = href.split('?')[0].split('#')[0]
        if href_clean == target_url:
            return True
        if target_url.startswith('/'):
            parsed_path = urlparse(href_clean).path.rstrip("/")
            if parsed_path == target_clean:
                return True
    return False


def _inject_link_via_ai(current_html, links_to_add, config):
    if not current_html or not current_html.strip():
        parts = []
        for link in links_to_add:
            parts.append(f'<a href="{link["url"]}">{link["anchor_text"]}</a>')
        return "<p>" + " ".join(parts) + "</p>"

    prompt_template = get_setting("prompts.inject_interlink", DEFAULTS.get("prompts.inject_interlink", ""))
    links_json = json.dumps(links_to_add, indent=2)
    prompt = prompt_template.format(links_json=links_json, current_html=current_html)

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


# ── Apply endpoints ────────────────────────────────────────────────


@interlinking_bp.route("/api/interlinking/apply-fix", methods=["POST"])
def apply_fix():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    from_type = data.get("from_type")
    from_handle = data.get("from_handle")
    from_blog_handle = data.get("from_blog_handle")
    to_type = data.get("to_type")
    to_handle = data.get("to_handle")
    to_blog_handle = data.get("to_blog_handle")
    suggested_anchor = data.get("suggested_anchor", to_handle or "")

    if not all([store_id, from_type, from_handle, to_type, to_handle]):
        return jsonify({"error": "Missing required fields"}), 400

    try:
        source = _resolve_item(store_id, from_type, from_handle, from_blog_handle)
        if not source:
            return jsonify({"error": f"Source {from_type} '{from_handle}' not found"}), 404

        target_url = _build_target_url(to_type, to_handle, to_blog_handle)

        if _link_already_exists(source["html"], target_url):
            return jsonify({"ok": True, "skipped": True, "reason": "Link already exists"})

        config = _get_ai_config()
        links = [{"url": target_url, "anchor_text": suggested_anchor}]
        new_html = _inject_link_via_ai(source["html"], links, config)

        ok, reason = validate_injected_html(source["html"], new_html, expected_hrefs=[target_url])
        if not ok:
            logger.warning("Rejected AI link injection for %s '%s': %s", from_type, from_handle, reason)
            return jsonify({"error": f"AI output rejected: {reason}"}), 422

        shopify_client = _get_shopify_client(store_id)
        _push_html_to_shopify(shopify_client, source["item_type"], source["shopify_id"], new_html)
        _update_local_db(source["item_type"], source["id"], new_html)

        with db.get_cursor() as cur:
            cur.execute("DELETE FROM interlinking_analysis WHERE store_id = %s", (store_id,))

        return jsonify({"ok": True, "from_handle": from_handle, "to_handle": to_handle})

    except Exception as e:
        logger.exception("Failed to apply interlink fix")
        return jsonify({"error": str(e)}), 500


@interlinking_bp.route("/api/interlinking/apply-all", methods=["POST"])
def apply_all_fixes():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    fixes = data.get("fixes", [])

    if not store_id or not fixes:
        return jsonify({"error": "store_id and fixes are required"}), 400

    try:
        config = _get_ai_config()
        shopify_client = _get_shopify_client(store_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    groups = {}
    for fix in fixes:
        ft = fix.get("from_type", "")
        fh = fix.get("from_handle", "")
        fbh = fix.get("from_blog_handle", "")
        key = f"{ft}:{fbh}/{fh}" if fbh else f"{ft}:{fh}"
        groups.setdefault(key, {"from_type": ft, "from_handle": fh, "from_blog_handle": fbh, "links": []})
        groups[key]["links"].append(fix)

    results = []

    for key, group in groups.items():
        source = _resolve_item(store_id, group["from_type"], group["from_handle"], group["from_blog_handle"] or None)
        if not source:
            for fix in group["links"]:
                results.append({
                    "from_handle": fix.get("from_handle"),
                    "to_handle": fix.get("to_handle"),
                    "status": "error",
                    "reason": f"Source {group['from_type']} '{group['from_handle']}' not found",
                })
            continue

        current_html = source["html"]
        links_to_add = []

        for fix in group["links"]:
            target_url = _build_target_url(
                fix.get("to_type"), fix.get("to_handle"), fix.get("to_blog_handle")
            )
            if _link_already_exists(current_html, target_url):
                results.append({
                    "from_handle": fix.get("from_handle"),
                    "to_handle": fix.get("to_handle"),
                    "status": "skipped",
                    "reason": "Link already exists",
                })
            else:
                links_to_add.append({
                    "url": target_url,
                    "anchor_text": fix.get("suggested_anchor", fix.get("to_handle", "")),
                    "_fix": fix,
                })

        if not links_to_add:
            continue

        try:
            ai_links = [{"url": l["url"], "anchor_text": l["anchor_text"]} for l in links_to_add]
            new_html = _inject_link_via_ai(current_html, ai_links, config)
            ok, reason = validate_injected_html(
                current_html, new_html, expected_hrefs=[l["url"] for l in ai_links]
            )
            if not ok:
                raise ValueError(f"AI output rejected: {reason}")
            _push_html_to_shopify(shopify_client, source["item_type"], source["shopify_id"], new_html)
            _update_local_db(source["item_type"], source["id"], new_html)

            for l in links_to_add:
                results.append({
                    "from_handle": l["_fix"].get("from_handle"),
                    "to_handle": l["_fix"].get("to_handle"),
                    "status": "applied",
                })
        except Exception as e:
            logger.exception("Failed to apply interlink fixes for %s", key)
            for l in links_to_add:
                results.append({
                    "from_handle": l["_fix"].get("from_handle"),
                    "to_handle": l["_fix"].get("to_handle"),
                    "status": "error",
                    "reason": str(e),
                })

    applied_count = sum(1 for r in results if r.get("status") == "applied")
    if applied_count > 0:
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM interlinking_analysis WHERE store_id = %s", (store_id,))

    return jsonify({"ok": True, "results": results})


# ── Broken link fix helpers ───────────────────────────────────────


def _get_available_targets(store_id):
    targets = []
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle, title FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
            (store_id,),
        )
        for row in cur.fetchall():
            targets.append({"type": "product", "handle": row["handle"], "title": row["title"]})

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle, title FROM synced_collections WHERE store_id = %s",
            (store_id,),
        )
        for row in cur.fetchall():
            targets.append({"type": "collection", "handle": row["handle"], "title": row["title"]})

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sa.handle, sa.title, sb.handle AS blog_handle
            FROM synced_articles sa
            JOIN synced_blogs sb ON sb.id = sa.blog_id
            WHERE sa.store_id = %s AND sa.is_published = TRUE
            """,
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


def _replace_href_in_html(html, broken_href, new_href):
    from app.services.interlinking_analyzer import LINK_RE

    broken_clean = broken_href.rstrip("/")

    def _replacer(match):
        href = match.group(1).strip()
        href_clean = href.split("?")[0].split("#")[0].rstrip("/")
        parsed_path = urlparse(href_clean).path.rstrip("/")
        if href_clean == broken_clean or parsed_path == broken_clean:
            if new_href:
                return match.group(0).replace(href, new_href)
            else:
                return re.sub(r"<[^>]+>", "", match.group(2)).strip()
        return match.group(0)

    return LINK_RE.sub(_replacer, html)


# ── Broken link fix endpoints ─────────────────────────────────────


@interlinking_bp.route("/api/interlinking/available-targets")
def available_targets():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    return jsonify(_get_available_targets(store_id))


@interlinking_bp.route("/api/interlinking/fix-broken-link", methods=["POST"])
def fix_broken_link():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    to_type = data.get("to_type")
    to_handle = data.get("to_handle")
    to_href = data.get("to_href", "")
    replacement_type = data.get("replacement_type")
    replacement_handle = data.get("replacement_handle")
    replacement_blog_handle = data.get("replacement_blog_handle")
    from_type = data.get("from_type")
    from_handle = data.get("from_handle")
    from_blog_handle = data.get("from_blog_handle")

    if not all([store_id, to_handle, from_type, from_handle]):
        return jsonify({"error": "Missing required fields"}), 400

    broken_href = to_href or _build_target_url(to_type or "product", to_handle)

    if replacement_handle:
        new_href = _build_target_url(
            replacement_type or to_type or "product",
            replacement_handle,
            replacement_blog_handle,
        )
    else:
        new_href = None

    try:
        source = _resolve_item(store_id, from_type, from_handle, from_blog_handle)
        if not source:
            return jsonify({"error": f"Source {from_type} '{from_handle}' not found"}), 404

        new_html = _replace_href_in_html(source["html"], broken_href, new_href)
        if new_html == source["html"]:
            return jsonify({"ok": True, "status": "skipped", "reason": "Broken href not found in HTML"})

        shopify_client = _get_shopify_client(store_id)
        _push_html_to_shopify(shopify_client, source["item_type"], source["shopify_id"], new_html)
        _update_local_db(source["item_type"], source["id"], new_html)

        with db.get_cursor() as cur:
            cur.execute("DELETE FROM interlinking_analysis WHERE store_id = %s", (store_id,))

        return jsonify({"ok": True, "status": "fixed"})

    except Exception as e:
        logger.exception("Failed to fix broken link in %s", from_handle)
        return jsonify({"error": str(e)}), 500
