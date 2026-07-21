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
