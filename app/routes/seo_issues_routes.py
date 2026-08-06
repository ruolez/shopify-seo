"""Store-wide SEO issue endpoints and fix-by-issue-type.

All issue derivation lives in services/issue_scanner.py — the single
definition shared with the sidebar badge and the per-item issue panel.
"""

import logging

from flask import Blueprint, jsonify, request

from app.database import db
from app.services.issue_scanner import (
    ISSUE_DEFS,
    SEVERITY_RANK,
    item_issues,
    scan_store_issues,
)
from app.services.task_runner import spawn as spawn_task

logger = logging.getLogger(__name__)

seo_issues_bp = Blueprint("seo_issues", __name__)


@seo_issues_bp.route("/api/seo/issues", methods=["GET"])
def list_issues():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    items_by_issue, coverage = scan_store_issues(store_id)

    issues = []
    for key, definition in ISSUE_DEFS.items():
        items = items_by_issue.get(key) or []
        if not items:
            continue
        by_type = {}
        for it in items:
            by_type[it["item_type"]] = by_type.get(it["item_type"], 0) + 1
        samples = sorted(items, key=lambda it: it["item_type"] != "product")[:5]
        issues.append({
            "key": key,
            "label": definition["label"],
            "severity": definition["severity"],
            "source": definition["source"],
            "fixable": definition["fixable"],
            "fix_fields": definition["fix_fields"],
            "action_label": definition.get("action_label"),
            "action_href": definition.get("action_href"),
            "count": len(items),
            "by_type": by_type,
            "sample_items": [
                {"item_type": s["item_type"], "id": s["id"], "title": s["title"], "handle": s["handle"]}
                for s in samples
            ],
        })

    issues.sort(key=lambda i: (SEVERITY_RANK[i["severity"]], -i["count"]))

    return jsonify({"store_id": int(store_id), "coverage": coverage, "issues": issues})


@seo_issues_bp.route("/api/seo/issues/item/<item_type>/<int:item_id>", methods=["GET"])
def item_issue_list(item_type, item_id):
    """One item's issues, from the same scan the Issues tab uses — the modal
    must never disagree with the list it was opened from."""
    if item_type not in ("product", "collection"):
        return jsonify({"error": "item_type must be 'product' or 'collection'"}), 400
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    return jsonify({
        "item_type": item_type,
        "id": item_id,
        "issues": item_issues(store_id, item_type, item_id),
    })


def _resolve_issue_items(issue_key, store_id, item_type=None):
    """Affected items for one issue, optionally restricted to an item_type."""
    items_by_issue, _ = scan_store_issues(store_id)
    items = items_by_issue.get(issue_key) or []
    if item_type:
        items = [it for it in items if it["item_type"] == item_type]
    return items


@seo_issues_bp.route("/api/seo/issues/<issue_key>/items", methods=["GET"])
def issue_items(issue_key):
    definition = ISSUE_DEFS.get(issue_key)
    if not definition:
        return jsonify({"error": f"Unknown issue: {issue_key}"}), 404
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    item_type = request.args.get("item_type") or None
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)

    items = _resolve_issue_items(issue_key, store_id, item_type)

    return jsonify({
        "issue": {
            "key": issue_key,
            "label": definition["label"],
            "severity": definition["severity"],
            "fixable": definition["fixable"],
            "fix_fields": definition["fix_fields"],
            "action_label": definition.get("action_label"),
            "action_href": definition.get("action_href"),
        },
        "total": len(items),
        "items": items[offset:offset + limit],
    })


@seo_issues_bp.route("/api/seo/issues/<issue_key>/fix", methods=["POST"])
def fix_issue(issue_key):
    from app.routes.technical_seo_routes import _execute_bulk_fix_apply

    definition = ISSUE_DEFS.get(issue_key)
    if not definition:
        return jsonify({"error": f"Unknown issue: {issue_key}"}), 400
    if not definition["fixable"]:
        return jsonify({"error": f"'{definition['label']}' cannot be fixed automatically"}), 400

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    item_type = data.get("item_type")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    if item_type not in ("product", "collection"):
        return jsonify({"error": "item_type must be 'product' or 'collection'"}), 400

    limit = min(int(data.get("limit", 100)), 500)
    affected = _resolve_issue_items(issue_key, store_id, item_type)

    requested_ids = data.get("item_ids")
    if requested_ids:
        requested = {int(i) for i in requested_ids}
        affected = [it for it in affected if it["id"] in requested]

    affected = affected[:limit]
    if not affected:
        return jsonify({"error": "No affected items to fix"}), 400

    item_col = "product_id" if item_type == "product" else "collection_id"
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, TRUE, 'seo_issue_fix') RETURNING id",
            (len(affected),),
        )
        bulk_task_id = cur.fetchone()["id"]
        for it in affected:
            cur.execute(
                f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                (bulk_task_id, it["id"]),
            )

    spawn_task(
        "bulk_tasks", bulk_task_id, _execute_bulk_fix_apply,
        bulk_task_id, item_type, definition["fix_fields"],
    )

    return jsonify({
        "ok": True,
        "bulk_task_id": bulk_task_id,
        "total": len(affected),
        "issue_key": issue_key,
    }), 201


@seo_issues_bp.route("/api/seo/issues/analyze", methods=["POST"])
def analyze_unanalyzed():
    """Queue keyword-alignment analysis — one bulk task per item type.
    Default: only never-analyzed items. With {"force": true}: ALL visible
    items (used after scoring-threshold changes make stored analyses stale)."""
    from app.routes.technical_seo_routes import _execute_bulk_analyze

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    force = bool(data.get("force"))
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    tasks = []
    specs = [
        ("product", "synced_products", "product_id", "t.status = 'ACTIVE'"),
        ("collection", "synced_collections", "collection_id", "t.published = TRUE"),
    ]
    for item_type, table, item_col, visibility in specs:
        with db.get_cursor() as cur:
            if force:
                cur.execute(
                    f"SELECT t.id FROM {table} t WHERE t.store_id = %s AND {visibility} ORDER BY t.id",
                    (store_id,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT t.id FROM {table} t
                    LEFT JOIN technical_seo_analyses tsa
                      ON tsa.store_id = t.store_id AND tsa.item_type = %s AND tsa.item_id = t.id
                    WHERE t.store_id = %s AND {visibility}
                      AND tsa.id IS NULL
                    ORDER BY t.id
                    """,
                    (item_type, store_id),
                )
            ids = [r["id"] for r in cur.fetchall()]

        if not ids:
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, FALSE, 'tseo_analyze') RETURNING id",
                (len(ids),),
            )
            bulk_task_id = cur.fetchone()["id"]
            for iid in ids:
                cur.execute(
                    f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )

        spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_analyze, bulk_task_id, item_type)
        tasks.append({"item_type": item_type, "bulk_task_id": bulk_task_id, "total": len(ids)})

    return jsonify({"tasks": tasks}), 201 if tasks else 200
