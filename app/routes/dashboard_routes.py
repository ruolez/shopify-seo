from flask import Blueprint, jsonify, render_template, request, session

from app.services import dashboard_service

dashboard_bp = Blueprint("dashboard", __name__)


def _resolve_store_id():
    return (
        request.args.get("store_id", type=int)
        or session.get("active_store_id")
        or dashboard_service.resolve_default_store_id()
    )


@dashboard_bp.route("/")
def dashboard_page():
    return render_template("dashboard.html")


@dashboard_bp.route("/api/dashboard/overview")
def dashboard_overview():
    store_id = _resolve_store_id()
    return jsonify(dashboard_service.get_overview(store_id))


@dashboard_bp.route("/api/dashboard/action-queue")
def dashboard_action_queue():
    store_id = _resolve_store_id()
    limit = min(request.args.get("limit", 15, type=int), 50)
    if not store_id:
        return jsonify({"items": [], "total_by_source": {}})
    return jsonify(dashboard_service.build_action_queue(store_id, limit))


@dashboard_bp.route("/api/dashboard/ctr-opportunities")
def dashboard_ctr_opportunities():
    store_id = _resolve_store_id()
    limit = min(request.args.get("limit", 10, type=int), 50)
    if not store_id:
        return jsonify({"items": []})
    return jsonify({"items": dashboard_service.get_ctr_opportunities(store_id, limit)})


@dashboard_bp.route("/api/dashboard/impact")
def dashboard_impact():
    store_id = _resolve_store_id()
    limit = min(request.args.get("limit", 10, type=int), 50)
    if not store_id:
        return jsonify({"items": [], "summary": {}})
    from app.services.impact_service import get_optimization_impact
    return jsonify(get_optimization_impact(store_id, days=60, limit=limit))


@dashboard_bp.route("/api/dashboard/movers")
def dashboard_movers():
    store_id = _resolve_store_id()
    limit = min(request.args.get("limit", 10, type=int), 25)
    if not store_id:
        return jsonify({"up": [], "down": [], "competitor_gains": []})
    return jsonify(dashboard_service.get_rank_movers(store_id, limit))


@dashboard_bp.route("/api/dashboard/ops")
def dashboard_ops():
    store_id = _resolve_store_id()
    if not store_id:
        return jsonify({})
    return jsonify(dashboard_service.get_ops(store_id))


@dashboard_bp.route("/api/dashboard/trends")
def dashboard_trends():
    store_id = _resolve_store_id()
    days = min(request.args.get("days", 90, type=int), 365)
    if not store_id:
        return jsonify({"gsc_daily": [], "score_daily": [], "events": []})
    return jsonify(dashboard_service.get_trends(store_id, days))
