import logging
import threading

from flask import Blueprint, jsonify, render_template, request, session

from app.services import competitor_service

logger = logging.getLogger(__name__)
competitor_bp = Blueprint("competitors", __name__)


def _get_store_id():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        store_id = session.get("active_store_id")
    return store_id


@competitor_bp.route("/competitors")
def competitors_page():
    return render_template("competitors.html")


@competitor_bp.route("/api/competitors", methods=["GET"])
def list_competitors():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    return jsonify(competitor_service.get_competitors(store_id))


@competitor_bp.route("/api/competitors", methods=["POST"])
def add_competitor():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = request.get_json(silent=True) or {}
    domain = (data.get("domain") or "").strip()
    name = (data.get("name") or "").strip()
    if not domain:
        return jsonify({"error": "domain required"}), 400
    try:
        comp_id = competitor_service.add_competitor(store_id, domain, name)
        return jsonify({"ok": True, "id": comp_id}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@competitor_bp.route("/api/competitors/<int:competitor_id>", methods=["DELETE"])
def remove_competitor(competitor_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    competitor_service.remove_competitor(store_id, competitor_id)
    return jsonify({"ok": True})


@competitor_bp.route("/api/competitors/check", methods=["POST"])
def trigger_check():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    keyword_limit = request.args.get("keyword_limit", 100, type=int)
    keyword_limit = min(max(keyword_limit, 10), 500)
    try:
        result = competitor_service.run_competitor_check(store_id, keyword_limit=keyword_limit)
        if isinstance(result, dict) and result.get("error"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@competitor_bp.route("/api/competitors/rankings")
def rankings():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    keyword = request.args.get("keyword")
    limit = request.args.get("limit", 500, type=int)
    include_unchallenged = request.args.get("include_unchallenged", "false").lower() == "true"
    data = competitor_service.get_rankings(store_id, keyword, limit, include_unchallenged)
    # Client-side filter for gaps_only
    return jsonify(data)


@competitor_bp.route("/api/competitors/alerts")
def alerts():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    is_read = request.args.get("is_read")
    if is_read is not None:
        is_read = is_read.lower() == "true"
    severity = request.args.get("severity")
    data = competitor_service.get_alerts(store_id, is_read, severity)
    return jsonify(data)


@competitor_bp.route("/api/competitors/alerts/<int:alert_id>/read", methods=["PUT"])
def mark_read(alert_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    competitor_service.mark_alert_read(store_id, alert_id)
    return jsonify({"ok": True})


@competitor_bp.route("/api/competitors/discover", methods=["POST"])
def discover_competitors():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = request.get_json(silent=True) or {}
    keyword_limit = min(max(data.get("keyword_limit", 20), 5), 50)
    try:
        result = competitor_service.discover_competitors(store_id, keyword_limit=keyword_limit)
        return jsonify(result)
    except Exception as e:
        logger.error("Discover competitors failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@competitor_bp.route("/api/competitors/overview")
def overview():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    return jsonify(competitor_service.get_overview(store_id))


@competitor_bp.route("/api/competitors/history")
def history():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    return jsonify(competitor_service.get_check_history(store_id))
