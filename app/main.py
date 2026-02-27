import logging
import sys

from flask import Flask, jsonify, redirect, request, session, url_for

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from app.auth import ensure_default_admin
from app.config import SECRET_KEY
from app.database import db
from app.routes.auth_routes import auth_bp
from app.routes.dashboard_routes import dashboard_bp
from app.routes.seo_routes import seo_bp
from app.routes.settings_routes import settings_bp
from app.routes.store_routes import store_bp
from app.routes.schema_routes import schema_bp
from app.routes.interlinking_routes import interlinking_bp
from app.routes.task_routes import task_bp

app = Flask(__name__)
app.secret_key = SECRET_KEY

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(store_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(seo_bp)
app.register_blueprint(task_bp)
app.register_blueprint(schema_bp)
app.register_blueprint(interlinking_bp)

PUBLIC_PATHS = {"/login", "/api/login", "/health", "/static"}


@app.before_request
def require_auth():
    path = request.path
    if any(path.startswith(p) for p in PUBLIC_PATHS):
        return None
    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("auth.login_page"))
    return None


@app.after_request
def no_cache(response):
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


with app.app_context():
    db.init_tables()
    ensure_default_admin(db)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
