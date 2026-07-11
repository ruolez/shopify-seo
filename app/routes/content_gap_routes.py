import logging
import threading

from flask import Blueprint, jsonify, render_template, request, session

from app.services import content_gap_service

logger = logging.getLogger(__name__)
content_gap_bp = Blueprint("content_gaps", __name__)


def _get_store_id():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        store_id = session.get("active_store_id")
    return store_id


@content_gap_bp.route("/content-gaps")
def content_gaps_page():
    return render_template("content_gaps.html")


@content_gap_bp.route("/api/content-gaps/discover", methods=["POST"])
def discover_gaps():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        result = content_gap_service.discover_gaps(store_id)
        return jsonify(result)
    except Exception as e:
        logger.error("Gap discovery failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@content_gap_bp.route("/api/content-gaps")
def list_gaps():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    gap_type = request.args.get("gap_type")
    intent = request.args.get("intent")
    status = request.args.get("status")
    sort_by = request.args.get("sort_by", "opportunity_score")
    sort_dir = request.args.get("sort_dir", "desc")
    limit = request.args.get("limit", 10000, type=int)
    offset = request.args.get("offset", 0, type=int)
    search = request.args.get("search")
    data = content_gap_service.get_gaps(store_id, gap_type, intent, status, sort_by, sort_dir, limit, offset, search)
    return jsonify(data)


@content_gap_bp.route("/api/content-gaps/overview")
def overview():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = content_gap_service.get_overview(store_id)
    return jsonify(data)


@content_gap_bp.route("/api/content-gaps/<int:gap_id>")
def get_gap(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    gap = content_gap_service.get_gap(store_id, gap_id)
    if not gap:
        return jsonify({"error": "Gap not found"}), 404
    return jsonify(gap)


@content_gap_bp.route("/api/content-gaps/<int:gap_id>/brief", methods=["POST"])
def generate_brief(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        brief = content_gap_service.generate_brief(store_id, gap_id)
        return jsonify(brief)
    except Exception as e:
        logger.error("Brief generation failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@content_gap_bp.route("/api/content-gaps/<int:gap_id>/status", methods=["PUT"])
def update_status(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    try:
        content_gap_service.update_gap_status(store_id, gap_id, status)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@content_gap_bp.route("/api/content-gaps/<int:gap_id>", methods=["DELETE"])
def delete_gap(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    content_gap_service.delete_gap(store_id, gap_id)
    return jsonify({"ok": True})


@content_gap_bp.route("/api/content-gaps/<int:gap_id>/generate-content", methods=["POST"])
def generate_content(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = request.get_json(silent=True) or {}
    selected_title = data.get("title", "")
    content_type = data.get("content_type", "collection")
    skip_cannibalization_check = data.get("skip_cannibalization_check", False)

    if not skip_cannibalization_check:
        risk = None
        try:
            gap = content_gap_service.get_gap(store_id, gap_id)
            keyword = (gap or {}).get("keyword", "")
            if keyword:
                risk = content_gap_service.check_cannibalization_risk(store_id, keyword)
        except Exception:
            # Fail closed — a broken check must not silently disable the gate.
            logger.exception("Cannibalization check failed for gap %s", gap_id)
            return jsonify({
                "error": "Cannibalization check failed — retry, or resend with skip_cannibalization_check to override",
            }), 502
        if risk and risk.get("risk_level") == "high":
            return jsonify({
                "cannibalization_warning": True,
                "error": "High cannibalization risk — existing pages already target this keyword",
                "requires_confirmation": True,
                "risk_level": risk["risk_level"],
                "recommendation": risk.get("recommendation"),
                "existing_pages": risk.get("existing_pages", [])[:3],
                "on_site_matches": risk.get("on_site_matches", [])[:3],
            }), 409

    try:
        result = content_gap_service.generate_content(store_id, gap_id, selected_title, content_type)
        return jsonify(result)
    except Exception as e:
        logger.error("Content generation failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@content_gap_bp.route("/api/content-gaps/<int:gap_id>/publish", methods=["POST"])
def publish_content(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = request.get_json(silent=True) or {}
    try:
        result = content_gap_service.publish_content(store_id, gap_id, data)
        return jsonify(result)
    except Exception as e:
        logger.error("Content publish failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@content_gap_bp.route("/api/content-gaps/<int:gap_id>/optimize-existing", methods=["POST"])
def optimize_existing(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    override_keyword = data.get("override_keyword")
    override_volume = data.get("override_volume")
    skip_cannibalization_check = data.get("skip_cannibalization_check", False)

    if not skip_cannibalization_check:
        try:
            gap = content_gap_service.get_gap(store_id, gap_id)
            keyword = override_keyword or (gap.get("keyword", "") if gap else "")
            if keyword:
                risk = content_gap_service.check_cannibalization_risk(store_id, keyword)
                if risk.get("risk_level") == "high":
                    return jsonify({
                        "cannibalization_warning": True,
                "error": "High cannibalization risk — existing pages already target this keyword",
                "requires_confirmation": True,
                        "risk_level": risk["risk_level"],
                        "recommendation": risk["recommendation"],
                        "existing_pages": risk.get("existing_pages", [])[:3],
                        "on_site_matches": risk.get("on_site_matches", [])[:3],
                    }), 409
        except Exception as e:
            logger.warning("Cannibalization check failed, proceeding: %s", e)

    try:
        result = content_gap_service.optimize_existing_page(store_id, gap_id, override_keyword, override_volume)
        return jsonify(result)
    except Exception as e:
        import traceback
        logger.error("Optimize existing failed: %s\n%s", str(e), traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@content_gap_bp.route("/api/content-gaps/<int:gap_id>/apply-optimization", methods=["POST"])
def apply_optimization(gap_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type")
    item_id = data.get("item_id")
    try:
        result = content_gap_service.apply_gap_optimization(store_id, gap_id, item_type, item_id, data)
        return jsonify(result)
    except Exception as e:
        logger.error("Apply optimization failed: %s", str(e))
        return jsonify({"error": str(e)}), 500
