import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request, session

from app.database import db
from app.services.seo_analyzer import analyze_product_seo

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def dashboard_page():
    return render_template("dashboard.html")


@dashboard_bp.route("/api/dashboard/stats")
def dashboard_stats():
    store_id = request.args.get("store_id", type=int) or session.get("active_store_id")

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

    result = {
        "stores": stores_count,
        "products": products_count,
        "collections": collections_count,
        "avg_seo_score": avg_score,
        "ai_tasks": tasks_count,
    }

    if store_id:
        try:
            result["insights"] = _get_store_insights(store_id)
        except Exception as e:
            logger.warning("Dashboard insights failed: %s", e)

    return jsonify(result)


def _get_store_insights(store_id):
    """Aggregate insights from all data sources for the dashboard."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()
    insights = {}

    with db.get_cursor() as cur:
        # GSC summary
        cur.execute(
            """SELECT SUM(clicks) as clicks, SUM(impressions) as impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s""",
            (store_id, date_from, date_to),
        )
        gsc = cur.fetchone()
        if gsc and float(gsc.get("impressions") or 0) > 0:
            insights["gsc"] = {
                "clicks": int(float(gsc["clicks"] or 0)),
                "impressions": int(float(gsc["impressions"] or 0)),
                "ctr": round(float(gsc["ctr"] or 0), 4),
                "avg_position": round(float(gsc["avg_position"] or 0), 1),
            }

        # Semrush summary
        cur.execute(
            """SELECT COUNT(*) as total_keywords,
                      AVG(position) as avg_position,
                      SUM(search_volume) as total_volume,
                      COUNT(*) FILTER (WHERE position >= 4 AND position <= 20) as striking_count
               FROM semrush_keywords
               WHERE store_id = %s AND position IS NOT NULL""",
            (store_id,),
        )
        sr = cur.fetchone()
        if sr and int(sr.get("total_keywords") or 0) > 0:
            insights["semrush"] = {
                "total_keywords": int(sr["total_keywords"]),
                "avg_position": round(float(sr["avg_position"] or 0), 1),
                "total_volume": int(float(sr["total_volume"] or 0)),
                "striking_count": int(sr["striking_count"]),
            }

        # Cannibalization count
        cur.execute(
            """SELECT COUNT(*) as cnt FROM (
                   SELECT query FROM gsc_daily_metrics
                   WHERE store_id = %s AND date >= %s AND date <= %s
                   GROUP BY query
                   HAVING COUNT(DISTINCT page) >= 2 AND SUM(impressions) >= 10
               ) sub""",
            (store_id, date_from, date_to),
        )
        insights["cannibalization_count"] = cur.fetchone()["cnt"]

        # Competitor alerts
        cur.execute(
            "SELECT COUNT(*) as cnt FROM competitor_alerts WHERE store_id = %s AND is_read = FALSE",
            (store_id,),
        )
        insights["unread_competitor_alerts"] = cur.fetchone()["cnt"]

        # Content gaps
        cur.execute(
            "SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE status = 'new') as new_gaps FROM content_gaps WHERE store_id = %s",
            (store_id,),
        )
        gaps = cur.fetchone()
        insights["content_gaps"] = {"total": gaps["total"], "new": gaps["new_gaps"]}

        # Technical SEO issues
        cur.execute(
            """SELECT COUNT(*) as total,
                      SUM(issues_count) as total_issues,
                      SUM(fixable_count) as fixable_issues
               FROM technical_seo_analyses
               WHERE store_id = %s AND issues_count > 0""",
            (store_id,),
        )
        tech = cur.fetchone()
        insights["technical_seo"] = {
            "items_with_issues": int(tech["total"]),
            "total_issues": int(tech["total_issues"] or 0),
            "fixable_issues": int(tech["fixable_issues"] or 0),
        }

        # Interlinking health
        cur.execute(
            "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s ORDER BY created_at DESC LIMIT 1",
            (store_id,),
        )
        il_row = cur.fetchone()
        if il_row and il_row.get("analysis_data"):
            data = il_row["analysis_data"]
            if isinstance(data, str):
                import json
                data = json.loads(data)
            stats = data.get("stats", {})
            insights["interlinking"] = {
                "orphan_products": stats.get("orphan_products", 0),
                "orphan_collections": stats.get("orphan_collections", 0),
                "under_linked": stats.get("under_linked", 0),
                "broken_links": stats.get("broken_links", 0),
            }

        # Recent optimizations impact
        cur.execute(
            """SELECT COUNT(*) as total,
                      COUNT(*) FILTER (WHERE seo_score_before IS NOT NULL AND seo_score_before > 0) as with_before
               FROM seo_change_events
               WHERE store_id = %s AND created_at >= NOW() - INTERVAL '30 days'""",
            (store_id,),
        )
        opt = cur.fetchone()
        insights["recent_optimizations"] = int(opt["total"])

    return insights
