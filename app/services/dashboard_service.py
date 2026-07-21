"""Aggregate SQL behind the dashboard endpoints.

Every widget endpoint maps to one function here that batches its own
queries — the routes stay thin and the frontend never has to join data
client-side.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.database import db

logger = logging.getLogger(__name__)

# GSC data lags ~3 days behind real time; every search window in the app
# ends at this date so current and comparison periods stay consistent.
GSC_LAG_DAYS = 3

# Inline (blocking) score snapshots are only acceptable for small catalogs;
# larger stores wait for the nightly SnapshotScheduler instead.
INLINE_SNAPSHOT_MAX_PRODUCTS = 500


def gsc_data_end_date():
    return datetime.now(timezone.utc).date() - timedelta(days=GSC_LAG_DAYS)


def resolve_default_store_id():
    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM stores WHERE is_active = TRUE ORDER BY id LIMIT 1")
        row = cur.fetchone()
    return row["id"] if row else None


def get_overview(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, shop_name, primary_domain, last_synced_at FROM stores WHERE is_active = TRUE ORDER BY id"
        )
        stores = cur.fetchall()

        store = next((s for s in stores if s["id"] == store_id), None)

        counts = {"products": 0, "active_products": 0, "collections": 0, "articles": 0}
        if store:
            cur.execute(
                """SELECT
                       (SELECT COUNT(*) FROM synced_products WHERE store_id = %(sid)s) AS products,
                       (SELECT COUNT(*) FROM synced_products WHERE store_id = %(sid)s AND status = 'ACTIVE') AS active_products,
                       (SELECT COUNT(*) FROM synced_collections WHERE store_id = %(sid)s) AS collections,
                       (SELECT COUNT(*) FROM synced_articles WHERE store_id = %(sid)s) AS articles""",
                {"sid": store_id},
            )
            counts = dict(cur.fetchone())

        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') AS recent, "
            "COUNT(*) FILTER (WHERE status IN ('pending','running')) AS running "
            "FROM ai_tasks" + (" WHERE store_id = %s" if store else ""),
            (store_id,) if store else (),
        )
        ai = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS count FROM bulk_tasks WHERE status IN ('pending','running')"
            + (" AND store_id = %s" if store else ""),
            (store_id,) if store else (),
        )
        running_bulk = cur.fetchone()["count"]

    score = _get_score_summary(store_id) if store else None

    result = {
        "stores": [
            {"id": s["id"], "name": s.get("shop_name") or s["name"]} for s in stores
        ],
        "store": {
            "id": store["id"],
            "name": store.get("shop_name") or store["name"],
            "domain": store.get("primary_domain"),
            "last_synced_at": store["last_synced_at"].isoformat() if store.get("last_synced_at") else None,
        } if store else None,
        "counts": counts,
        "avg_seo_score": score["avg"] if score else 0,
        "score_distribution": score["distribution"] if score else None,
        "score_pending": score is None and store is not None,
        "ai_tasks_30d": int(ai["recent"] or 0),
        "tasks_running": int(ai["running"] or 0) + int(running_bulk or 0),
    }

    if store:
        try:
            result["insights"] = get_store_insights(store_id)
        except Exception as e:
            logger.warning("Dashboard insights failed: %s", e)
        try:
            result["kpis"] = _get_kpis(store_id, score)
        except Exception as e:
            logger.warning("Dashboard KPIs failed: %s", e)

    return result


def _get_kpis(store_id, score_summary):
    kpis = {}
    end = gsc_data_end_date()
    cur_start = end - timedelta(days=27)
    prev_start = end - timedelta(days=55)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT date, SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY date ORDER BY date""",
            (store_id, prev_start.isoformat(), end.isoformat()),
        )
        daily = cur.fetchall()

    if daily:
        by_date = {r["date"]: r for r in daily}
        cur_days = [cur_start + timedelta(days=i) for i in range(28)]
        spark_clicks, spark_pos = [], []
        for d in cur_days:
            r = by_date.get(d)
            spark_clicks.append(int(r["clicks"] or 0) if r else 0)
            spark_pos.append(round(float(r["position"]), 1) if r and r["position"] is not None else None)

        def window(rows):
            clicks = sum(int(r["clicks"] or 0) for r in rows)
            impr = sum(int(r["impressions"] or 0) for r in rows)
            pos_w = sum(float(r["position"]) * int(r["impressions"] or 0) for r in rows if r["position"] is not None)
            return clicks, impr, (pos_w / impr if impr else None)

        cur_rows = [r for r in daily if r["date"] >= cur_start]
        prev_rows = [r for r in daily if r["date"] < cur_start]
        c_clicks, _c_impr, c_pos = window(cur_rows)
        p_clicks, _p_impr, p_pos = window(prev_rows)

        kpis["clicks_28d"] = {
            "value": c_clicks,
            "pct_change": round((c_clicks - p_clicks) / p_clicks * 100, 1) if p_clicks else None,
            "spark": spark_clicks,
        }
        kpis["avg_position_28d"] = {
            "value": round(c_pos, 1) if c_pos is not None else None,
            "change": round(c_pos - p_pos, 1) if c_pos is not None and p_pos is not None else None,
            "spark": spark_pos,
        }

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT recorded_at::date AS date, AVG(score) AS avg_score
               FROM seo_score_history
               WHERE store_id = %s AND item_type = 'product'
                 AND recorded_at >= NOW() - INTERVAL '90 days'
               GROUP BY recorded_at::date ORDER BY date""",
            (store_id,),
        )
        score_days = cur.fetchall()
    if score_summary and score_days:
        baseline = next(
            (r for r in score_days if (score_days[-1]["date"] - r["date"]).days <= 30),
            score_days[0],
        )
        kpis["seo_score"] = {
            "value": score_summary["avg"],
            "change_30d": round(float(score_days[-1]["avg_score"]) - float(baseline["avg_score"]), 1)
            if baseline is not score_days[-1] else None,
            "spark": [round(float(r["avg_score"]), 1) for r in score_days],
        }

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE position <= 10) AS top10,
                      COUNT(*) FILTER (WHERE position BETWEEN 4 AND 20) AS striking,
                      COUNT(*) FILTER (WHERE previous_position BETWEEN 4 AND 20) AS striking_prev
               FROM semrush_keywords
               WHERE store_id = %s AND position IS NOT NULL""",
            (store_id,),
        )
        kw = cur.fetchone()
    if kw and int(kw["total"] or 0) > 0:
        kpis["keywords"] = {
            "total": int(kw["total"]),
            "top10": int(kw["top10"]),
            "striking": int(kw["striking"]),
            "striking_change": int(kw["striking"]) - int(kw["striking_prev"]),
        }

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT brand_mention_rate, share_of_voice, avg_rank, completed_at
               FROM ai_tracking_runs
               WHERE store_id = %s AND status = 'completed'
               ORDER BY id DESC LIMIT 2""",
            (store_id,),
        )
        runs = cur.fetchall()
    if runs:
        latest = runs[0]
        prev = runs[1] if len(runs) > 1 else None
        kpis["ai_visibility"] = {
            "mention_rate": float(latest["brand_mention_rate"] or 0),
            "share_of_voice": float(latest["share_of_voice"] or 0),
            "delta_vs_prev_run": round(
                float(latest["brand_mention_rate"] or 0) - float(prev["brand_mention_rate"] or 0), 3
            ) if prev else None,
            "last_run_at": latest["completed_at"].isoformat() if latest["completed_at"] else None,
        }

    return kpis


def get_trends(store_id, days=90):
    end = gsc_data_end_date()
    start = end - timedelta(days=days - 1)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT date, SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY date ORDER BY date""",
            (store_id, start.isoformat(), end.isoformat()),
        )
        gsc_rows = cur.fetchall()

        cur.execute(
            """SELECT created_at::date AS date, item_type, item_id, change_type
               FROM seo_change_events
               WHERE store_id = %s AND created_at >= %s
               ORDER BY created_at""",
            (store_id, start.isoformat()),
        )
        events = cur.fetchall()

        titles = {}
        for item_type, table in (("product", "synced_products"), ("collection", "synced_collections")):
            ids = [e["item_id"] for e in events if e["item_type"] == item_type]
            if ids:
                cur.execute(f"SELECT id, title FROM {table} WHERE id = ANY(%s)", (ids,))
                titles[item_type] = {r["id"]: r["title"] for r in cur.fetchall()}

    gsc_daily = [
        {
            "date": r["date"].isoformat(),
            "clicks": int(r["clicks"] or 0),
            "impressions": int(r["impressions"] or 0),
            "position": round(float(r["position"]), 1) if r["position"] is not None else None,
        }
        for r in gsc_rows
    ]

    events_by_date = {}
    for e in events:
        d = e["date"].isoformat()
        entry = events_by_date.setdefault(d, {"date": d, "count": 0, "items": []})
        entry["count"] += 1
        title = titles.get(e["item_type"], {}).get(e["item_id"])
        if title and len(entry["items"]) < 8:
            entry["items"].append({"title": title, "change_type": e["change_type"]})

    from app.services.gsc_service import get_score_trends
    score_daily = get_score_trends(store_id, start.isoformat(), (end + timedelta(days=1)).isoformat(), item_type="product")

    return {
        "gsc_daily": gsc_daily,
        "score_daily": [{"date": r["date"], "avg_score": r["avg_score"]} for r in score_daily],
        "events": sorted(events_by_date.values(), key=lambda x: x["date"]),
    }


def _get_score_summary(store_id):
    """Product score summary from the latest seo_score_history snapshot day.

    Scoring every product per page load (the old approach) took seconds on
    large catalogs; the nightly SnapshotScheduler already persists the same
    rule-based scores.
    """
    summary = _query_score_summary(store_id)
    if summary:
        return summary

    # Fresh install: no snapshot yet. Take one inline only if the catalog is
    # small enough to score within a request.
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM synced_products WHERE store_id = %s", (store_id,))
        product_count = cur.fetchone()["count"]
    if product_count == 0 or product_count > INLINE_SNAPSHOT_MAX_PRODUCTS:
        return None
    try:
        from app.services.gsc_service import take_seo_score_snapshot
        take_seo_score_snapshot(store_id)
    except Exception as e:
        logger.warning("Inline score snapshot failed: %s", e)
        return None
    return _query_score_summary(store_id)


def _query_score_summary(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT AVG(score) AS avg,
                      COUNT(*) FILTER (WHERE score >= 80) AS good,
                      COUNT(*) FILTER (WHERE score >= 50 AND score < 80) AS fair,
                      COUNT(*) FILTER (WHERE score < 50) AS poor
               FROM seo_score_history
               WHERE store_id = %s AND item_type = 'product'
                 AND recorded_at >= (SELECT MAX(recorded_at)::date FROM seo_score_history
                                     WHERE store_id = %s AND item_type = 'product')""",
            (store_id, store_id),
        )
        row = cur.fetchone()
    if not row or row["avg"] is None:
        return None
    return {
        "avg": round(float(row["avg"])),
        "distribution": {"good": row["good"], "fair": row["fair"], "poor": row["poor"]},
    }


def get_store_insights(store_id):
    """Aggregate insights from all data sources for the dashboard."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = gsc_data_end_date().isoformat()
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
            """SELECT COUNT(*) as total
               FROM seo_change_events
               WHERE store_id = %s AND created_at >= NOW() - INTERVAL '30 days'""",
            (store_id,),
        )
        insights["recent_optimizations"] = int(cur.fetchone()["total"])

        # Keyword strategy corpus
        cur.execute(
            """SELECT COUNT(*) as total,
                      COUNT(*) FILTER (WHERE source IN ('manual','both')) as manual_count
               FROM master_keywords WHERE store_id = %s""",
            (store_id,),
        )
        kw = cur.fetchone()
        if int(kw["total"] or 0) > 0:
            insights["keywords"] = {"total": int(kw["total"]), "manual": int(kw["manual_count"] or 0)}

        # Latest completed AI visibility run
        cur.execute(
            """SELECT brand_mention_rate, share_of_voice, avg_rank, completed_at
               FROM ai_tracking_runs
               WHERE store_id = %s AND status = 'completed'
               ORDER BY id DESC LIMIT 1""",
            (store_id,),
        )
        vis = cur.fetchone()
        if vis:
            insights["ai_visibility"] = {
                "brand_mention_rate": float(vis["brand_mention_rate"] or 0),
                "share_of_voice": float(vis["share_of_voice"] or 0),
                "avg_rank": float(vis["avg_rank"]) if vis["avg_rank"] is not None else None,
                "completed_at": vis["completed_at"].isoformat() if vis["completed_at"] else None,
            }

    return insights
