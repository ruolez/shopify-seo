"""Intro links: put each post's best collection link in its opening paragraph.

Audit (GET) → draft intro rewrites in the background (plan task) → review and
redraft per post → apply the approved drafts (apply task, live-body drift
check per post). Nothing reaches Shopify before the apply step.
"""

import json
import logging

from flask import Blueprint, jsonify, request

from app.database import db
from app.services.intro_link_service import (
    MAX_PLAN_ARTICLES,
    apply_item,
    audit_article,
    build_context,
    draft_fix,
    load_eligible_articles,
    make_ai_client,
    summarize,
)
from app.services.task_runner import spawn as spawn_task

logger = logging.getLogger(__name__)

intro_link_bp = Blueprint("intro_link", __name__)

PLAN_TASK = "article_intro_link_plan"
APPLY_TASK = "article_intro_link_apply"


def _load_result(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return raw


def _progress(items, total):
    return {"total": total, "completed": len(items), "failed": sum(1 for i in items if i.get("error"))}


@intro_link_bp.route("/api/blog/intro-links", methods=["GET"])
def intro_link_audit():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    ctx = build_context(store_id)
    rows = [audit_article(a, ctx) for a in load_eligible_articles(store_id)]
    rows.sort(key=lambda r: (-(r["clicks"] or 0), -(r["words_before_first"] or 0)))
    return jsonify({"summary": summarize(rows), "articles": rows, "gsc_window": ctx["gsc_window"]})


def _run_plan(task_id, store_id, article_ids):
    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
    ctx = build_context(store_id)
    client, model = make_ai_client()
    articles = {a["id"]: a for a in load_eligible_articles(store_id, article_ids)}
    items = []

    def _write(status=None):
        payload = json.dumps({"store_id": store_id, "items": items, "progress": _progress(items, len(article_ids))})
        with db.get_cursor() as cur:
            if status:
                cur.execute(
                    "UPDATE ai_tasks SET status = %s, result = %s, completed_at = NOW() WHERE id = %s",
                    (status, payload, task_id),
                )
            else:
                cur.execute("UPDATE ai_tasks SET result = %s WHERE id = %s", (payload, task_id))

    for article_id in article_ids:
        article = articles.get(article_id)
        if not article:
            items.append({"article_id": article_id, "error": "Not an eligible published post", "valid": False})
        else:
            try:
                items.append(draft_fix(client, model, article, ctx))
            except Exception as e:
                logger.exception("Intro link draft failed for article %s", article_id)
                items.append({"article_id": article_id, "title": article.get("title"), "error": str(e)[:300], "valid": False})
        _write()
    _write("completed")


@intro_link_bp.route("/api/blog/intro-links/plan", methods=["POST"])
def intro_link_plan():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    article_ids = [int(i) for i in (data.get("article_ids") or []) if str(i).isdigit()]
    if not store_id or not article_ids:
        return jsonify({"error": "store_id and article_ids are required"}), 400
    if len(article_ids) > MAX_PLAN_ARTICLES:
        return jsonify({"error": f"Draft at most {MAX_PLAN_ARTICLES} posts per run"}), 400
    article_ids = list(dict.fromkeys(article_ids))
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, %s, 'pending', %s) RETURNING id""",
            (store_id, PLAN_TASK, json.dumps({"store_id": store_id, "article_ids": article_ids})),
        )
        task_id = cur.fetchone()["id"]
    spawn_task("ai_tasks", task_id, _run_plan, task_id, int(store_id), article_ids)
    return jsonify({"task_id": task_id, "total": len(article_ids)}), 202


def _task_payload(task):
    return {
        "id": task["id"],
        "status": task["status"],
        "error": task.get("error"),
        "created_at": task["created_at"].isoformat() if task.get("created_at") else None,
        "result": _load_result(task.get("result")),
    }


@intro_link_bp.route("/api/blog/intro-links/plan/latest", methods=["GET"])
def intro_link_plan_latest():
    store_id = request.args.get("store_id", type=int)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, error, result, created_at FROM ai_tasks
               WHERE task_type = %s AND store_id = %s ORDER BY id DESC LIMIT 1""",
            (PLAN_TASK, store_id),
        )
        task = cur.fetchone()
    return jsonify({"task": _task_payload(task) if task else None})


def _get_plan(cur, task_id, lock=False):
    cur.execute(
        "SELECT id, store_id, status, result FROM ai_tasks WHERE id = %s AND task_type = %s"
        + (" FOR UPDATE" if lock else ""),
        (task_id, PLAN_TASK),
    )
    return cur.fetchone()


def _patch_plan_item(task_id, article_id, patch=None, replace=None):
    """Update one item of a plan task's result under a row lock."""
    with db.get_cursor() as cur:
        task = _get_plan(cur, task_id, lock=True)
        result = _load_result(task["result"]) if task else None
        if not result:
            return
        items = result.get("items") or []
        for idx, item in enumerate(items):
            if item.get("article_id") == article_id:
                items[idx] = replace if replace is not None else {**item, **(patch or {})}
        cur.execute("UPDATE ai_tasks SET result = %s WHERE id = %s", (json.dumps(result), task_id))


@intro_link_bp.route("/api/blog/intro-links/plan/<int:task_id>/redraft", methods=["POST"])
def intro_link_redraft(task_id):
    data = request.get_json(silent=True) or {}
    article_id = data.get("article_id")
    with db.get_cursor() as cur:
        task = _get_plan(cur, task_id)
    if not task:
        return jsonify({"error": "Draft run not found"}), 404
    if task["status"] != "completed":
        return jsonify({"error": "Wait for the draft run to finish"}), 409
    store_id = task["store_id"]
    articles = load_eligible_articles(store_id, [article_id])
    if not articles:
        return jsonify({"error": "Not an eligible published post"}), 404
    try:
        client, model = make_ai_client()
        item = draft_fix(client, model, articles[0], build_context(store_id), target_handle=data.get("target_handle"))
    except Exception as e:
        logger.exception("Intro link redraft failed for article %s", article_id)
        return jsonify({"error": str(e)}), 502
    _patch_plan_item(task_id, article_id, replace=item)
    return jsonify({"item": item})


def _run_apply(task_id, plan_task_id, store_id, article_ids):
    from app.routes.blog_routes import _get_store_client
    from app.services.intro_link_service import load_store_hosts

    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
        task = _get_plan(cur, plan_task_id)
    plan = _load_result(task["result"]) if task else {}
    by_id = {i.get("article_id"): i for i in (plan or {}).get("items") or []}
    _, shopify_client = _get_store_client(store_id)
    hosts = load_store_hosts(store_id)
    articles = {a["id"]: a for a in load_eligible_articles(store_id, article_ids)}
    results = []

    def _write(status=None):
        failed = sum(1 for r in results if r["status"] != "applied")
        payload = json.dumps({
            "plan_task_id": plan_task_id,
            "results": results,
            "progress": {"total": len(article_ids), "completed": len(results), "failed": failed},
        })
        with db.get_cursor() as cur:
            if status:
                cur.execute(
                    "UPDATE ai_tasks SET status = %s, result = %s, completed_at = NOW() WHERE id = %s",
                    (status, payload, task_id),
                )
            else:
                cur.execute("UPDATE ai_tasks SET result = %s WHERE id = %s", (payload, task_id))

    for article_id in article_ids:
        item, article = by_id.get(article_id), articles.get(article_id)
        if not item or not item.get("valid") or not item.get("after_intro"):
            res = {"status": "failed", "error": "No valid draft for this post"}
        elif item.get("applied"):
            res = {"status": "skipped", "error": "Already applied"}
        elif not article or not shopify_client:
            res = {"status": "failed", "error": "Post or store not found"}
        else:
            try:
                res = apply_item(shopify_client, article, item, hosts)
            except Exception as e:
                logger.exception("Intro link apply failed for article %s", article_id)
                res = {"status": "failed", "error": str(e)[:300]}
        results.append({"article_id": article_id, "title": (item or {}).get("title"), **res})
        if res["status"] == "applied":
            _patch_plan_item(plan_task_id, article_id, patch={"applied": True})
        elif res["status"] in ("stale", "failed"):
            _patch_plan_item(plan_task_id, article_id, patch={"apply_error": res.get("error")})
        _write()
    failed = sum(1 for r in results if r["status"] != "applied")
    _write("completed_with_errors" if failed else "completed")


@intro_link_bp.route("/api/blog/intro-links/apply", methods=["POST"])
def intro_link_apply():
    data = request.get_json(silent=True) or {}
    plan_task_id = data.get("task_id")
    article_ids = [int(i) for i in (data.get("article_ids") or []) if str(i).isdigit()]
    if not plan_task_id or not article_ids:
        return jsonify({"error": "task_id and article_ids are required"}), 400
    with db.get_cursor() as cur:
        task = _get_plan(cur, plan_task_id)
    if not task or task["status"] != "completed":
        return jsonify({"error": "Draft run not found or not finished"}), 409
    store_id = task["store_id"]
    article_ids = list(dict.fromkeys(article_ids))
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, %s, 'pending', %s) RETURNING id""",
            (store_id, APPLY_TASK, json.dumps({"plan_task_id": plan_task_id, "article_ids": article_ids})),
        )
        task_id = cur.fetchone()["id"]
    spawn_task("ai_tasks", task_id, _run_apply, task_id, plan_task_id, store_id, article_ids)
    return jsonify({"task_id": task_id, "total": len(article_ids)}), 202
