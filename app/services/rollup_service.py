"""Daily per-store metric snapshots for dashboard trends.

Coverage percentages, issue counts and keyword totals only exist as
current state elsewhere; store_daily_snapshot gives them a history. The
scheduler runs leader-only (main.py); the snapshot upsert and activity
mirror run every tick (both idempotent), while expensive scans are gated
by a row-for-today check, never in-memory state — restarts are safe.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.database import db
from app.services.dashboard_service import (
    get_coverage,
    get_store_insights,
    gsc_data_end_date,
)

logger = logging.getLogger(__name__)


def take_store_snapshot(store_id, snapshot_date=None):
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date()
    coverage = get_coverage(store_id)
    insights = get_store_insights(store_id)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT
                   (SELECT COUNT(*) FROM synced_products WHERE store_id = %(sid)s) AS products_total,
                   (SELECT COUNT(*) FROM synced_products WHERE store_id = %(sid)s AND status = 'ACTIVE') AS products_active,
                   (SELECT COUNT(*) FROM synced_collections WHERE store_id = %(sid)s) AS collections_total,
                   (SELECT COUNT(*) FROM synced_articles WHERE store_id = %(sid)s) AS articles_total""",
            {"sid": store_id},
        )
        counts = cur.fetchone()

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
        score = cur.fetchone()

        cur.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE position <= 10) AS top10,
                      COUNT(*) FILTER (WHERE position BETWEEN 4 AND 20) AS striking
               FROM semrush_keywords WHERE store_id = %s AND position IS NOT NULL""",
            (store_id,),
        )
        sr = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) FILTER (WHERE status = 'addressed') AS addressed FROM content_gaps WHERE store_id = %s",
            (store_id,),
        )
        gaps_addressed = int(cur.fetchone()["addressed"] or 0)

        gsc_end = gsc_data_end_date()
        cur.execute(
            """SELECT COALESCE(SUM(clicks), 0) AS clicks,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s""",
            (store_id, (gsc_end - timedelta(days=6)).isoformat(), gsc_end.isoformat()),
        )
        gsc = cur.fetchone()

        il = insights.get("interlinking") or {}
        tech = insights.get("technical_seo") or {}
        cur.execute(
            """INSERT INTO store_daily_snapshot (
                   store_id, snapshot_date,
                   products_total, products_active, collections_total, articles_total,
                   avg_seo_score, score_good, score_fair, score_poor,
                   meta_title_pct, meta_desc_pct, alt_text_pct, schema_pushed_pct, keyword_mapped_pct,
                   technical_issues, fixable_issues, orphan_pages, broken_links,
                   semrush_keywords, semrush_top10, semrush_striking,
                   content_gaps_new, content_gaps_addressed, aeo_assets_published,
                   gsc_clicks_7d, gsc_position_7d)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (store_id, snapshot_date) DO UPDATE SET
                   products_total = EXCLUDED.products_total,
                   products_active = EXCLUDED.products_active,
                   collections_total = EXCLUDED.collections_total,
                   articles_total = EXCLUDED.articles_total,
                   avg_seo_score = EXCLUDED.avg_seo_score,
                   score_good = EXCLUDED.score_good,
                   score_fair = EXCLUDED.score_fair,
                   score_poor = EXCLUDED.score_poor,
                   meta_title_pct = EXCLUDED.meta_title_pct,
                   meta_desc_pct = EXCLUDED.meta_desc_pct,
                   alt_text_pct = EXCLUDED.alt_text_pct,
                   schema_pushed_pct = EXCLUDED.schema_pushed_pct,
                   keyword_mapped_pct = EXCLUDED.keyword_mapped_pct,
                   technical_issues = EXCLUDED.technical_issues,
                   fixable_issues = EXCLUDED.fixable_issues,
                   orphan_pages = EXCLUDED.orphan_pages,
                   broken_links = EXCLUDED.broken_links,
                   semrush_keywords = EXCLUDED.semrush_keywords,
                   semrush_top10 = EXCLUDED.semrush_top10,
                   semrush_striking = EXCLUDED.semrush_striking,
                   content_gaps_new = EXCLUDED.content_gaps_new,
                   content_gaps_addressed = EXCLUDED.content_gaps_addressed,
                   aeo_assets_published = EXCLUDED.aeo_assets_published,
                   gsc_clicks_7d = EXCLUDED.gsc_clicks_7d,
                   gsc_position_7d = EXCLUDED.gsc_position_7d""",
            (
                store_id, snapshot_date,
                counts["products_total"], counts["products_active"],
                counts["collections_total"], counts["articles_total"],
                round(float(score["avg"]), 1) if score and score["avg"] is not None else None,
                int(score["good"] or 0), int(score["fair"] or 0), int(score["poor"] or 0),
                coverage["meta_title_pct"], coverage["meta_desc_pct"], coverage["alt_text_pct"],
                coverage["schema_pushed_pct"], coverage["keyword_mapped_pct"],
                tech.get("total_issues", 0), tech.get("fixable_issues", 0),
                il.get("orphan_products", 0) + il.get("orphan_collections", 0),
                il.get("broken_links", 0),
                int(sr["total"] or 0), int(sr["top10"] or 0), int(sr["striking"] or 0),
                (insights.get("content_gaps") or {}).get("new", 0), gaps_addressed,
                coverage["aeo_assets"]["published"],
                int(gsc["clicks"] or 0),
                round(float(gsc["position"]), 1) if gsc["position"] is not None else None,
            ),
        )
    logger.info("Store snapshot recorded for store %s (%s)", store_id, snapshot_date)


def backfill_score_history(store_id):
    """Best-effort backfill of score columns from seo_score_history so
    trend widgets have depth on day one. Other columns stay NULL/0 —
    they were never recorded historically."""
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO store_daily_snapshot (store_id, snapshot_date, avg_seo_score, score_good, score_fair, score_poor)
               SELECT store_id, recorded_at::date,
                      ROUND(AVG(score)::numeric, 1),
                      COUNT(*) FILTER (WHERE score >= 80),
                      COUNT(*) FILTER (WHERE score >= 50 AND score < 80),
                      COUNT(*) FILTER (WHERE score < 50)
               FROM seo_score_history
               WHERE store_id = %s AND item_type = 'product'
               GROUP BY store_id, recorded_at::date
               ON CONFLICT (store_id, snapshot_date) DO NOTHING""",
            (store_id,),
        )
        return cur.rowcount


class NightlyScheduler:
    """Rolls up daily snapshots for every active store. Ticks every few
    hours; every tick refreshes the snapshot upsert and activity mirror,
    while the row-for-today check keeps backfill, anomaly/gap scans and
    paid runs to once per day (restart-safe)."""

    def __init__(self, interval_hours=6):
        self.interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Nightly rollup scheduler started (tick every %d hours)", self.interval // 3600)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        self._stop_event.wait(120)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("Nightly rollup error: %s", str(e))
            self._stop_event.wait(self.interval)

    def _tick(self):
        today = datetime.now(timezone.utc).date()
        with db.get_cursor() as cur:
            cur.execute("SELECT id FROM stores WHERE is_active = TRUE")
            store_ids = [r["id"] for r in cur.fetchall()]
            cur.execute(
                "SELECT store_id FROM store_daily_snapshot WHERE snapshot_date = %s",
                (today,),
            )
            snapshotted_today = {r["store_id"] for r in cur.fetchall()}

        from app.services.anomaly_service import (
            maybe_run_weekly_visibility,
            mirror_activity_events,
            run_nightly_scans,
        )
        for store_id in store_ids:
            first_tick_today = store_id not in snapshotted_today
            try:
                if first_tick_today and backfill_score_history(store_id):
                    logger.info("Backfilled snapshot score history for store %s", store_id)
                # Upsert is idempotent (ON CONFLICT DO UPDATE) — refresh every
                # tick so post-apply changes show up the same day.
                take_store_snapshot(store_id, today)
            except Exception as e:
                logger.error("Snapshot failed for store %s: %s", store_id, str(e))
                continue
            try:
                mirror_activity_events(store_id)
            except Exception as e:
                logger.error("Activity mirror failed for store %s: %s", store_id, str(e))
            if not first_tick_today:
                continue
            # Once per day, alongside the first snapshot: anomaly + gap scans
            # and the opt-in weekly paid runs.
            try:
                run_nightly_scans(store_id)
            except Exception as e:
                logger.error("Nightly scans failed for store %s: %s", store_id, str(e))
            try:
                maybe_run_weekly_visibility(store_id)
            except Exception as e:
                logger.error("Weekly visibility check failed for store %s: %s", store_id, str(e))
            # Order pull for revenue attribution — a missing read_orders
            # scope logs one warning per day and the widget shows setup help.
            try:
                from app.services.revenue_service import pull_orders
                pull_orders(store_id)
            except Exception as e:
                logger.error("Order pull failed for store %s: %s", store_id, str(e))


_scheduler = None


def init_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = NightlyScheduler(interval_hours=6)
        _scheduler.start()
