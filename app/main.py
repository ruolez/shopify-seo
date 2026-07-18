import logging
import sys
from datetime import timedelta

from flask import Flask, jsonify, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

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
from app.routes.technical_seo_routes import technical_seo_bp
from app.routes.image_routes import image_bp
from app.routes.blog_routes import blog_bp
from app.routes.brand_routes import brand_bp
from app.routes.landing_routes import landing_bp
from app.routes.gsc_routes import gsc_bp
from app.routes.semrush_routes import semrush_bp
from app.routes.content_gap_routes import content_gap_bp
from app.routes.competitor_routes import competitor_bp
from app.routes.keyword_strategy_routes import keyword_strategy_bp
from app.routes.news_routes import news_bp
from app.routes.homepage_routes import homepage_bp
from app.routes.ai_seo_routes import ai_seo_bp
from app.routes.duplicates_routes import duplicates_bp

app = Flask(__name__)
app.secret_key = SECRET_KEY
# Applies only to sessions marked permanent ("Keep me signed in" at login);
# refreshed on each request, so it's a rolling 30-day window.
app.permanent_session_lifetime = timedelta(days=30)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(store_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(seo_bp)
app.register_blueprint(task_bp)
app.register_blueprint(schema_bp)
app.register_blueprint(interlinking_bp)
app.register_blueprint(technical_seo_bp)
app.register_blueprint(image_bp)
app.register_blueprint(blog_bp)
app.register_blueprint(brand_bp)
app.register_blueprint(landing_bp)
app.register_blueprint(gsc_bp)
app.register_blueprint(semrush_bp)
app.register_blueprint(content_gap_bp)
app.register_blueprint(competitor_bp)
app.register_blueprint(keyword_strategy_bp)
app.register_blueprint(news_bp)
app.register_blueprint(homepage_bp)
app.register_blueprint(ai_seo_bp)
app.register_blueprint(duplicates_bp)

PUBLIC_PATHS = {"/login", "/api/login", "/health", "/static", "/api/gsc/auth/callback"}


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


@app.errorhandler(500)
def handle_500(e):
    import traceback
    logger = logging.getLogger("app.main")
    logger.error("500 error: %s\n%s", str(e), traceback.format_exc())
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return "Internal Server Error", 500


@app.errorhandler(400)
def handle_400(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(e)}), 400
    return str(e), 400


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


with app.app_context():
    # Leader election: exactly one process runs boot recovery, cache purge,
    # and the singleton schedulers — regardless of gunicorn preload/worker
    # configuration or extra processes pointed at the same database.
    _is_leader = db.try_acquire_leader()
    db.init_tables(run_recovery=_is_leader)
    ensure_default_admin(db)
    if _is_leader:
        try:
            with db.get_cursor() as cur:
                cur.execute("DELETE FROM serp_cache WHERE expires_at < NOW()")
                if cur.rowcount:
                    logging.getLogger("app.main").info("Purged %d expired serp_cache rows", cur.rowcount)
        except Exception:
            logging.getLogger("app.main").exception("serp_cache purge failed (non-fatal)")
        from app.services.gsc_service import init_scheduler
        init_scheduler()
        from app.services.competitor_service import init_scheduler as init_competitor_scheduler
        init_competitor_scheduler()
        # AI visibility tracking is manual-only: the scheduler fired a full
        # multi-model LLM run after every restart (real OpenRouter spend).
        # Trigger runs from the AI SEO page when wanted.
    else:
        logging.getLogger("app.main").info(
            "Another process holds the leader lock — skipping boot recovery and schedulers"
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
