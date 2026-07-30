import logging
import threading

from flask import Blueprint, jsonify, render_template, request, session

from app.services import keyword_strategy_service as kws

logger = logging.getLogger(__name__)
keyword_strategy_bp = Blueprint("keyword_strategy", __name__)


def _get_store_id():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        store_id = session.get("active_store_id")
    return store_id


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/keyword-strategy")
def keyword_strategy_page():
    return render_template("keyword_strategy.html")


@keyword_strategy_bp.route("/api/keyword-strategy/freshness")
def keyword_freshness():
    """Last-run time and staleness level for every data layer feeding the
    Explorer, so the UI can show what is current and what needs a re-run.
    GSC refreshes automatically (6h scheduler); the rest are user-triggered."""
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    from datetime import datetime, timezone

    from app.database import db

    # hours: (fresh under, stale under) — beyond is "old"
    thresholds = {
        "gsc": (24, 72),
        "consolidate": (7 * 24, 30 * 24),
        "ai_classify": (14 * 24, 60 * 24),
        "audit": (7 * 24, 30 * 24),
    }
    now = datetime.now(timezone.utc)

    def entry(source, at):
        if not at:
            return {"at": None, "age_hours": None, "level": "none"}
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age = (now - at).total_seconds() / 3600
        ok, stale = thresholds[source]
        level = "ok" if age < ok else "stale" if age < stale else "old"
        return {"at": at.isoformat(), "age_hours": round(age, 1), "level": level}

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT MAX(completed_at) AS at FROM gsc_data_pulls "
            "WHERE store_id = %s AND status = 'completed'",
            (store_id,),
        )
        gsc_at = cur.fetchone()["at"]
        cur.execute(
            """SELECT run_type, MAX(completed_at) AS at FROM keyword_strategy_runs
               WHERE store_id = %s AND status = 'completed' GROUP BY run_type""",
            (store_id,),
        )
        runs = {r["run_type"]: r["at"] for r in cur.fetchall()}
        cur.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE updated_at < NOW() - INTERVAL '7 days') AS stale_count,
                      MAX(updated_at) AS newest
               FROM technical_seo_analyses WHERE store_id = %s""",
            (store_id,),
        )
        arow = cur.fetchone()

    total, stale_count = arow["total"], arow["stale_count"]
    if not total:
        analyses_level = "none"
    elif stale_count / total <= 0.05:
        analyses_level = "ok"
    elif stale_count / total <= 0.25:
        analyses_level = "stale"
    else:
        analyses_level = "old"
    newest = arow["newest"]
    if newest and newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)

    return jsonify({
        "gsc": entry("gsc", gsc_at),
        "consolidate": entry("consolidate", runs.get("consolidate")),
        "ai_classify": entry("ai_classify", runs.get("ai_classify")),
        "audit": entry("audit", runs.get("audit")),
        "analyses": {
            "at": newest.isoformat() if newest else None,
            "age_hours": round((now - newest).total_seconds() / 3600, 1) if newest else None,
            "level": analyses_level,
            "total": total,
            "stale_count": stale_count,
        },
    })


# ---------------------------------------------------------------------------
# Master Keyword List
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/consolidate", methods=["POST"])
def consolidate_keywords():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        def _run():
            try:
                kws.consolidate_master_keywords(store_id)
            except Exception as e:
                logger.error("Consolidation thread failed: %s", e)
                return
            try:
                # New keywords arrive with rules-based intents and no family —
                # classify them against the existing taxonomy. Must not break
                # consolidation when no AI key is configured.
                from app.services.keyword_ai_classifier import run_ai_classification

                run_ai_classification(store_id, mode="incremental")
            except Exception as e:
                logger.warning("Post-consolidation classification skipped: %s", e)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return jsonify({"status": "started"})
    except Exception as e:
        logger.error("Consolidation failed: %s", e)
        return jsonify({"error": str(e)}), 500


@keyword_strategy_bp.route("/api/keyword-strategy/keywords")
def list_keywords():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_master_keywords(
        store_id,
        search=request.args.get("search"),
        intent=request.args.get("intent"),
        source=request.args.get("source"),
        cluster_id=request.args.get("cluster_id", type=int),
        status=request.args.get("status"),
        sort_by=request.args.get("sort_by", "search_volume"),
        sort_dir=request.args.get("sort_dir", "desc"),
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/overview")
def keywords_overview():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_master_keyword_overview(store_id)
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/keywords", methods=["POST"])
def add_keyword():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    try:
        kw_id = kws.add_manual_keyword(
            store_id,
            body.get("keyword", ""),
            search_volume=body.get("search_volume"),
            intent=body.get("intent"),
        )
        return jsonify({"id": kw_id})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/<int:keyword_id>", methods=["DELETE"])
def remove_keyword(keyword_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    kws.delete_keyword(store_id, keyword_id)
    return jsonify({"deleted": True})


@keyword_strategy_bp.route("/api/keyword-strategy/ai/classify", methods=["POST"])
def ai_classify():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "full")
    if mode not in ("full", "incremental"):
        return jsonify({"error": "mode must be 'full' or 'incremental'"}), 400
    from app.services.keyword_ai_classifier import run_ai_classification

    result = run_ai_classification(store_id, mode=mode)
    if result.get("error"):
        return jsonify(result), 409
    return jsonify(result)


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/<int:keyword_id>/intent", methods=["PUT"])
def update_kw_intent(keyword_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    try:
        kws.update_keyword_intent(store_id, keyword_id, body.get("intent", ""))
        return jsonify({"updated": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/<int:keyword_id>/status", methods=["PUT"])
def update_kw_status(keyword_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    try:
        kws.update_keyword_status(store_id, keyword_id, body.get("status", "active"))
        return jsonify({"updated": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Coverage / ownership
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/coverage/matrix")
def coverage_matrix():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    from app.services import keyword_coverage as kc

    data = kc.get_coverage_matrix(
        store_id,
        min_volume=request.args.get("min_volume", 0, type=int),
        search=request.args.get("search"),
        only_gaps=request.args.get("only_gaps") == "1",
        sort_by=request.args.get("sort_by", "gap_score"),
        limit=request.args.get("limit", 50, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/families/<int:cluster_id>")
def family_coverage(cluster_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    from app.services import keyword_coverage as kc

    data = kc.get_family_coverage(store_id, cluster_id or None)
    if data is None:
        return jsonify({"error": "Family not found"}), 404
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/<int:keyword_id>/detail")
def keyword_detail(keyword_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    from app.services import keyword_coverage as kc

    data = kc.get_keyword_detail(store_id, keyword_id)
    if data is None:
        return jsonify({"error": "Keyword not found"}), 404
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/<int:keyword_id>/assign", methods=["POST"])
def assign_keyword(keyword_id):
    """Manually assign a keyword to a page. Products and collections should go
    through POST /api/seo/items/<type>/<id>/primary-keyword (full resolver
    pipeline); this lightweight path covers articles and pages, which have no
    resolver — the accepted assignment row alone makes them the owner."""
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    item_type = body.get("item_type")
    item_id = body.get("item_id")
    if item_type not in ("product", "collection", "article", "page") or not item_id:
        return jsonify({"error": "item_type and item_id required"}), 400

    from app.database import db

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM master_keywords WHERE id = %s AND store_id = %s",
            (keyword_id, store_id),
        )
        if not cur.fetchone():
            return jsonify({"error": "Keyword not found"}), 404
        cur.execute(
            """INSERT INTO keyword_assignments
               (store_id, master_keyword_id, assigned_item_type, assigned_item_id,
                assignment_reason, status)
               VALUES (%s, %s, %s, %s, 'manual_assign', 'accepted')
               ON CONFLICT (store_id, master_keyword_id) DO UPDATE SET
                 assigned_item_type = EXCLUDED.assigned_item_type,
                 assigned_item_id = EXCLUDED.assigned_item_id,
                 assignment_reason = 'manual_assign',
                 status = 'accepted',
                 updated_at = NOW()""",
            (store_id, keyword_id, item_type, int(item_id)),
        )
    return jsonify({"assigned": True})


@keyword_strategy_bp.route("/api/keyword-strategy/keywords/<int:keyword_id>/mentions")
def keyword_mentions(keyword_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    from app.services import keyword_coverage as kc

    data = kc.get_keyword_mentions(
        store_id,
        keyword_id,
        limit=request.args.get("limit", 25, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/clusters")
def list_clusters():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_clusters(store_id)
    return jsonify({"clusters": data})


@keyword_strategy_bp.route("/api/keyword-strategy/clusters/generate", methods=["POST"])
def generate_clusters():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        result = kws.cluster_keywords(store_id)
        return jsonify(result)
    except Exception as e:
        logger.error("Clustering failed: %s", e)
        return jsonify({"error": str(e)}), 500


@keyword_strategy_bp.route("/api/keyword-strategy/clusters/<int:cluster_id>", methods=["PUT"])
def rename_cluster_route(cluster_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    kws.rename_cluster(store_id, cluster_id, body.get("name", ""))
    return jsonify({"updated": True})


@keyword_strategy_bp.route("/api/keyword-strategy/clusters/merge", methods=["POST"])
def merge_clusters_route():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    try:
        kws.merge_clusters(store_id, body.get("cluster_ids", []), body.get("name"))
        return jsonify({"merged": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Content Audit
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/audit", methods=["POST"])
def run_audit():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    result = kws.run_content_audit(store_id)
    return jsonify(result)


@keyword_strategy_bp.route("/api/keyword-strategy/audit/status")
def audit_status():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    run = kws.get_audit_status(store_id)
    return jsonify(run or {})


@keyword_strategy_bp.route("/api/keyword-strategy/audit/results")
def audit_results():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_audit_results(
        store_id,
        item_type=request.args.get("item_type"),
        search=request.args.get("search"),
        sort_by=request.args.get("sort_by", "keyword_count"),
        sort_dir=request.args.get("sort_dir", "desc"),
        limit=request.args.get("limit", 50, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/audit/item/<item_type>/<int:item_id>")
def audit_item(item_type, item_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_item_audit(store_id, item_type, item_id)
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/audit/cannibalization")
def cannibalization():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_cannibalization_report(
        store_id,
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/audit/uncovered")
def uncovered():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_uncovered_keywords(
        store_id,
        min_volume=request.args.get("min_volume", 0, type=int),
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


# ---------------------------------------------------------------------------
# Assignment Plan
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/plan/generate", methods=["POST"])
def generate_plan():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    result = kws.generate_assignment_plan(store_id)
    return jsonify(result)


@keyword_strategy_bp.route("/api/keyword-strategy/plan")
def get_plan():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_assignment_plan(
        store_id,
        status=request.args.get("status"),
        item_type=request.args.get("item_type"),
        priority=request.args.get("priority"),
        search=request.args.get("search"),
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/plan/<int:assignment_id>/accept", methods=["PUT"])
def accept(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    kws.accept_assignment(store_id, assignment_id)
    return jsonify({"accepted": True})


@keyword_strategy_bp.route("/api/keyword-strategy/plan/<int:assignment_id>/reject", methods=["PUT"])
def reject(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    kws.reject_assignment(store_id, assignment_id)
    return jsonify({"rejected": True})


@keyword_strategy_bp.route("/api/keyword-strategy/plan/bulk-accept", methods=["POST"])
def bulk_accept():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    result = kws.bulk_accept_assignments(store_id, body.get("ids", []))
    return jsonify(result)


# ---------------------------------------------------------------------------
# Fix Application
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/fix/<int:assignment_id>/generate", methods=["POST"])
def generate_fix(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        result = kws.generate_fixes_for_assignment(store_id, assignment_id)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@keyword_strategy_bp.route("/api/keyword-strategy/plan/<int:assignment_id>/generate-steps", methods=["POST"])
def generate_steps(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        result = kws.generate_implementation_steps(store_id, assignment_id)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Generate steps failed: %s", e)
        return jsonify({"error": str(e)}), 500


@keyword_strategy_bp.route("/api/keyword-strategy/plan/<int:assignment_id>/apply-step", methods=["POST"])
def apply_step(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    step_index = body.get("step_index")
    if step_index is None:
        return jsonify({"error": "step_index required"}), 400
    try:
        result = kws.apply_single_step(store_id, assignment_id, int(step_index))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Apply step failed: %s", e)
        return jsonify({"error": str(e)}), 500


@keyword_strategy_bp.route("/api/keyword-strategy/fix/<int:assignment_id>/preview")
def preview(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.preview_fix(store_id, assignment_id)
    if not data:
        return jsonify({"error": "Assignment not found"}), 404
    return jsonify(data)


@keyword_strategy_bp.route("/api/keyword-strategy/fix/<int:assignment_id>/apply", methods=["POST"])
def apply_fix_route(assignment_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        result = kws.apply_fix(store_id, assignment_id)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Apply fix failed: %s", e)
        return jsonify({"error": str(e)}), 500


@keyword_strategy_bp.route("/api/keyword-strategy/fix/bulk-apply", methods=["POST"])
def bulk_apply():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    body = request.get_json(force=True)
    result = kws.bulk_apply_fixes(store_id, body.get("ids", []))
    return jsonify(result)


@keyword_strategy_bp.route("/api/keyword-strategy/fix/<int:change_id>/revert", methods=["POST"])
def revert(change_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        result = kws.revert_fix(store_id, change_id)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Revert failed: %s", e)
        return jsonify({"error": str(e)}), 500


@keyword_strategy_bp.route("/api/keyword-strategy/changes")
def list_changes():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_changes(
        store_id,
        limit=request.args.get("limit", 100, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(data)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@keyword_strategy_bp.route("/api/keyword-strategy/runs")
def list_runs():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = kws.get_runs(store_id)
    return jsonify({"runs": data})


@keyword_strategy_bp.route("/api/keyword-strategy/runs/<int:run_id>")
def get_run_status(run_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    run = kws.get_run(store_id, run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(run)
