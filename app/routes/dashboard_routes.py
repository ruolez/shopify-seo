import time

from flask import Blueprint, jsonify, render_template, request, session

from app.services import dashboard_service

dashboard_bp = Blueprint("dashboard", __name__)

_badge_cache = {}
_BADGE_TTL = 60


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


@dashboard_bp.route("/api/nav/badges")
def nav_badges():
    store_id = _resolve_store_id()
    if not store_id:
        return jsonify({})
    now = time.time()
    cached = _badge_cache.get(store_id)
    if cached and now - cached[0] < _BADGE_TTL:
        return jsonify(cached[1])
    payload = dashboard_service.get_nav_badges(store_id)
    _badge_cache[store_id] = (now, payload)
    return jsonify(payload)


@dashboard_bp.route("/api/dashboard/action-queue")
def dashboard_action_queue():
    store_id = _resolve_store_id()
    limit = min(request.args.get("limit", 15, type=int), 50)
    if not store_id:
        return jsonify({"items": [], "total_by_source": {}})
    return jsonify(dashboard_service.build_action_queue(store_id, limit))


@dashboard_bp.route("/api/dashboard/action-queue/dismiss", methods=["POST"])
def dashboard_action_dismiss():
    store_id = _resolve_store_id()
    data = request.get_json(silent=True) or {}
    action_key = (data.get("action_key") or "").strip()
    if not store_id or not action_key:
        return jsonify({"error": "store_id and action_key are required"}), 400
    dashboard_service.dismiss_action(store_id, action_key, data.get("snooze_days"))
    return jsonify({"ok": True})


@dashboard_bp.route("/api/dashboard/setup-state")
def dashboard_setup_state():
    store_id = _resolve_store_id()
    if not store_id:
        return jsonify({"steps": [], "complete": True})
    return jsonify(dashboard_service.get_setup_state(store_id))


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


@dashboard_bp.route("/api/dashboard/changes")
def dashboard_changes():
    store_id = _resolve_store_id()
    if not store_id:
        return jsonify({"since": None, "items": [], "counts_by_type": {}})
    return jsonify(dashboard_service.get_changes_feed(store_id))


@dashboard_bp.route("/api/dashboard/changes/seen", methods=["POST"])
def dashboard_changes_seen():
    store_id = _resolve_store_id()
    if store_id:
        dashboard_service.mark_changes_seen(store_id)
    return jsonify({"ok": True})


@dashboard_bp.route("/api/dashboard/revenue")
def dashboard_revenue():
    store_id = _resolve_store_id()
    days = min(request.args.get("days", 28, type=int), 90)
    if not store_id:
        return jsonify({"available": False})
    from app.services.revenue_service import get_revenue
    return jsonify(get_revenue(store_id, days))


@dashboard_bp.route("/api/dashboard/revenue/pull", methods=["POST"])
def dashboard_revenue_pull():
    store_id = _resolve_store_id()
    if not store_id:
        return jsonify({"error": "No store"}), 400
    from app.services.revenue_service import pull_orders
    result = pull_orders(store_id)
    status = 400 if result.get("error") else 200
    return jsonify(result), status


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
