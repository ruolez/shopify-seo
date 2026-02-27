from flask import Blueprint, jsonify, render_template

from app.database import db
from app.services.seo_analyzer import analyze_product_seo

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def dashboard_page():
    return render_template("dashboard.html")


@dashboard_bp.route("/api/dashboard/stats")
def dashboard_stats():
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as count FROM stores WHERE is_active = TRUE")
        stores_count = cur.fetchone()["count"]

        cur.execute("SELECT COUNT(*) as count FROM synced_products")
        products_count = cur.fetchone()["count"]

        cur.execute("SELECT COUNT(*) as count FROM synced_collections")
        collections_count = cur.fetchone()["count"]

        cur.execute("SELECT COUNT(*) as count FROM ai_tasks")
        tasks_count = cur.fetchone()["count"]

        cur.execute("SELECT * FROM synced_products LIMIT 200")
        products = cur.fetchall()

    scores = [analyze_product_seo(dict(p))["score"] for p in products]
    avg_score = round(sum(scores) / len(scores)) if scores else 0

    return jsonify(
        {
            "stores": stores_count,
            "products": products_count,
            "collections": collections_count,
            "avg_seo_score": avg_score,
            "ai_tasks": tasks_count,
        }
    )
