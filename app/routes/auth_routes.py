from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from app.auth import check_password
from app.database import db

auth_bp = Blueprint("auth", __name__)


def _safe_next(target):
    """Only same-site paths — never an absolute or protocol-relative URL."""
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return None
    return target


@auth_bp.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.dashboard_page"))
    return render_template("login.html")


@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()

    if not user or not check_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid credentials"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    # "Keep me signed in": persistent 30-day rolling cookie; otherwise the
    # session ends when the browser does (previous behavior).
    session.permanent = bool(data.get("remember"))
    return jsonify({"ok": True})


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))
