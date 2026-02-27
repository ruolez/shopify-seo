import json
import logging
import threading

from flask import Blueprint, jsonify, render_template, request

from app.database import db

logger = logging.getLogger(__name__)

interlinking_bp = Blueprint("interlinking", __name__)

_analysis_running = {}


def _run_analysis(store_id):
    try:
        from app.services.interlinking_analyzer import analyze_store_interlinking

        with db.get_cursor() as cur:
            cur.execute("SELECT domain FROM stores WHERE id = %s", (store_id,))
            row = cur.fetchone()
        store_domain = row["domain"] if row else None

        analyze_store_interlinking(store_id, store_domain)
    except Exception:
        logger.exception("Interlinking analysis failed for store %s", store_id)
    finally:
        _analysis_running.pop(store_id, None)


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
    data = request.get_json() or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    if store_id in _analysis_running:
        return jsonify({"ok": True, "message": "Analysis already running"}), 200

    _analysis_running[store_id] = True
    thread = threading.Thread(target=_run_analysis, args=(store_id,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": "Analysis started"}), 202


@interlinking_bp.route("/api/interlinking/status")
def analysis_status():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    running = store_id in _analysis_running
    return jsonify({"running": running})


@interlinking_bp.route("/api/interlinking/item/<item_type>/<int:item_id>")
def get_item_links(item_type, item_id):
    if item_type not in ("product", "collection"):
        return jsonify({"error": "Invalid type"}), 400

    table = "synced_products" if item_type == "product" else "synced_collections"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()

    if not item:
        return jsonify({"error": "Item not found"}), 404

    from app.services.interlinking_analyzer import extract_links_from_html

    with db.get_cursor() as cur:
        cur.execute("SELECT domain FROM stores WHERE id = %s", (item["store_id"],))
        store_row = cur.fetchone()
    store_domain = store_row["domain"] if store_row else None

    outgoing = extract_links_from_html(item.get("description"), store_domain)

    target_handle = item.get("handle")
    incoming = []

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

    for p in all_products:
        for link in extract_links_from_html(p.get("description"), store_domain):
            if link["handle"] == target_handle and link["link_type"] == item_type:
                incoming.append({
                    "from_type": "product",
                    "from_handle": p["handle"],
                    "from_title": p["title"],
                    "anchor_text": link["anchor_text"],
                })

    for c in all_collections:
        for link in extract_links_from_html(c.get("description"), store_domain):
            if link["handle"] == target_handle and link["link_type"] == item_type:
                incoming.append({
                    "from_type": "collection",
                    "from_handle": c["handle"],
                    "from_title": c["title"],
                    "anchor_text": link["anchor_text"],
                })

    return jsonify({
        "item": {
            "id": item["id"],
            "title": item["title"],
            "handle": item.get("handle"),
            "type": item_type,
        },
        "outgoing_links": outgoing,
        "incoming_links": incoming,
    })


@interlinking_bp.route("/api/interlinking/recommendations", methods=["POST"])
def trigger_recommendations():
    data = request.get_json() or {}
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

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, %s, %s) RETURNING id",
            (store_id, "interlinking_recommendations", json.dumps({"store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run_recommendations(tid):
        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (tid,))
        try:
            result = run_task("interlinking_recommendations", {"store_id": store_id}, store_id)
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

    thread = threading.Thread(target=_run_recommendations, args=(task_id,), daemon=True)
    thread.start()

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
