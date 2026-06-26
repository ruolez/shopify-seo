import logging

from flask import Blueprint, jsonify, render_template, request, session

from app.services import semrush_import_service

logger = logging.getLogger(__name__)

semrush_bp = Blueprint("semrush", __name__)


def _get_store_id():
    store_id = request.args.get("store_id", type=int) or request.form.get("store_id", type=int)
    if not store_id:
        store_id = session.get("active_store_id")
    return store_id


@semrush_bp.route("/semrush")
def semrush_page():
    return render_template("semrush.html")


@semrush_bp.route("/api/semrush/upload/preview", methods=["POST"])
def upload_preview():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Only CSV files are supported"}), 400

    content = file.read()
    if len(content) > 50 * 1024 * 1024:
        return jsonify({"error": "File too large (max 50MB)"}), 400

    try:
        preview = semrush_import_service.parse_csv_preview(content)
        return jsonify(preview)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@semrush_bp.route("/api/semrush/upload", methods=["POST"])
def upload_import():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    content = file.read()
    force_type = request.form.get("type")

    try:
        result = semrush_import_service.process_import(store_id, content, file.filename, force_type)
        return jsonify(result)
    except Exception as e:
        logger.error("Semrush import failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@semrush_bp.route("/api/semrush/imports")
def list_imports():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = semrush_import_service.get_imports(store_id)
    return jsonify(data)


@semrush_bp.route("/api/semrush/imports/<int:import_id>", methods=["DELETE"])
def delete_import(import_id):
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    semrush_import_service.delete_import(import_id, store_id)
    return jsonify({"ok": True})


@semrush_bp.route("/api/semrush/keywords")
def list_keywords():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    source_type = request.args.get("source_type")
    sort_by = request.args.get("sort_by", "search_volume")
    sort_dir = request.args.get("sort_dir", "desc")
    limit = request.args.get("limit", 500, type=int)
    offset = request.args.get("offset", 0, type=int)
    search = request.args.get("search")
    data = semrush_import_service.get_keywords(store_id, source_type, sort_by, sort_dir, limit, offset, search)
    return jsonify(data)


@semrush_bp.route("/api/semrush/keywords/cross-reference")
def keywords_cross_reference():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    limit = request.args.get("limit", 500, type=int)
    search = request.args.get("search")
    data = semrush_import_service.get_keywords_cross_reference(store_id, limit, search)
    return jsonify(data)


@semrush_bp.route("/api/semrush/audit-issues")
def audit_issues():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    severity = request.args.get("severity")
    limit = request.args.get("limit", 500, type=int)
    data = semrush_import_service.get_audit_issues(store_id, severity, limit)
    return jsonify(data)


@semrush_bp.route("/api/semrush/audit-summary")
def audit_summary():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = semrush_import_service.get_audit_summary(store_id)
    return jsonify(data)


@semrush_bp.route("/api/semrush/keywords/striking-distance")
def striking_distance():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    pos_min = request.args.get("pos_min", 4, type=int)
    pos_max = request.args.get("pos_max", 20, type=int)
    limit = request.args.get("limit", 500, type=int)
    search = request.args.get("search")
    data = semrush_import_service.get_striking_distance(store_id, pos_min, pos_max, limit, search)
    return jsonify(data)


@semrush_bp.route("/api/semrush/overview")
def overview():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = semrush_import_service.get_overview(store_id)
    return jsonify(data)
