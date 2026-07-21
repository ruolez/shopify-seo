"""Nightly anomaly detection and dashboard event feed.

All detections write dashboard_events rows guarded by (store_id, dedup_key)
so re-runs, restarts and overlapping scans never duplicate an alert. The
changes feed reads this one table; activity from other tables (applied
optimizations, published articles, visibility runs) is mirrored in here for
the same reason.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.dashboard_service import gsc_data_end_date
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)


def _thresholds():
    return get_setting("dashboard.anomaly", DEFAULTS["dashboard.anomaly"])


def emit_event(store_id, event_type, severity, subject, dedup_key, payload=None):
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO dashboard_events (store_id, event_type, severity, subject, dedup_key, payload)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (store_id, dedup_key) DO NOTHING""",
            (store_id, event_type, severity, subject, dedup_key, json.dumps(payload or {})),
        )
        return cur.rowcount > 0


def scan_click_drops(store_id):
    """Pages whose clicks fell >N% week over week (complete GSC weeks).
    One alert per page per ISO week; auto-resolves on recovery."""
    t = _thresholds()
    end = gsc_data_end_date()
    cur_start = end - timedelta(days=6)
    prev_start = end - timedelta(days=13)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT page,
                      SUM(clicks) FILTER (WHERE date >= %s) AS cur_clicks,
                      SUM(clicks) FILTER (WHERE date < %s) AS prev_clicks
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY page
               HAVING SUM(clicks) FILTER (WHERE date < %s) >= %s""",
            (cur_start, cur_start, store_id, prev_start.isoformat(), end.isoformat(),
             cur_start, t["min_weekly_clicks"]),
        )
        rows = cur.fetchall()

    iso_week = end.isocalendar()
    week_tag = f"{iso_week[0]}w{iso_week[1]:02d}"
    emitted = 0
    for r in rows:
        prev = int(r["prev_clicks"] or 0)
        cur_c = int(r["cur_clicks"] or 0)
        drop_pct = (prev - cur_c) / prev * 100 if prev else 0
        if drop_pct > t["clicks_drop_pct"]:
            if emit_event(
                store_id, "anomaly_clicks_drop", "critical",
                r["page"],
                f"clicks_drop:{r['page']}:{week_tag}",
                {"prev_clicks": prev, "cur_clicks": cur_c, "drop_pct": round(drop_pct, 1)},
            ):
                emitted += 1

    # Auto-resolve open drop alerts for pages back within 10% of baseline.
    recovered = {r["page"] for r in rows
                 if int(r["prev_clicks"] or 0) and int(r["cur_clicks"] or 0) >= int(r["prev_clicks"]) * 0.9}
    if recovered:
        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE dashboard_events SET status = 'resolved'
                   WHERE store_id = %s AND event_type = 'anomaly_clicks_drop'
                     AND status = 'open' AND subject = ANY(%s)""",
                (store_id, list(recovered)),
            )
    return emitted


def scan_keyword_dropouts(store_id):
    """Keywords that fell out of the top 10 — Semrush transition-keyed
    (alerts once per observed position change) plus GSC week-over-week."""
    t = _thresholds()
    emitted = 0

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT keyword, position, previous_position, search_volume
               FROM semrush_keywords
               WHERE store_id = %s AND previous_position <= 10
                 AND (position > 10 OR position IS NULL)
                 AND COALESCE(search_volume, 0) >= %s""",
            (store_id, t["min_keyword_volume"]),
        )
        sem_rows = cur.fetchall()
    for r in sem_rows:
        pos = r["position"] if r["position"] is not None else 0
        if emit_event(
            store_id, "anomaly_keyword_dropout", "warning",
            r["keyword"],
            f"kw_dropout:sem:{r['keyword']}:{r['previous_position']}:{pos}",
            {"source": "semrush", "previous_position": r["previous_position"],
             "position": r["position"], "volume": r["search_volume"]},
        ):
            emitted += 1

    end = gsc_data_end_date()
    cur_start = end - timedelta(days=6)
    prev_start = end - timedelta(days=13)
    with db.get_cursor() as cur:
        cur.execute(
            """WITH weekly AS (
                   SELECT query,
                          SUM(position * impressions) FILTER (WHERE date >= %(cs)s)
                              / NULLIF(SUM(impressions) FILTER (WHERE date >= %(cs)s), 0) AS cur_pos,
                          SUM(position * impressions) FILTER (WHERE date < %(cs)s)
                              / NULLIF(SUM(impressions) FILTER (WHERE date < %(cs)s), 0) AS prev_pos,
                          SUM(impressions) AS impressions
                   FROM gsc_daily_metrics
                   WHERE store_id = %(sid)s AND date >= %(ps)s AND date <= %(e)s
                   GROUP BY query
               )
               SELECT query, cur_pos, prev_pos, impressions FROM weekly
               WHERE prev_pos <= 10 AND cur_pos > 12 AND impressions >= 100""",
            {"sid": store_id, "cs": cur_start, "ps": prev_start.isoformat(), "e": end.isoformat()},
        )
        gsc_rows = cur.fetchall()

    iso_week = end.isocalendar()
    week_tag = f"{iso_week[0]}w{iso_week[1]:02d}"
    for r in gsc_rows:
        if emit_event(
            store_id, "anomaly_keyword_dropout", "warning",
            r["query"],
            f"kw_dropout:gsc:{r['query']}:{week_tag}",
            {"source": "gsc", "previous_position": round(float(r["prev_pos"]), 1),
             "position": round(float(r["cur_pos"]), 1), "impressions": int(r["impressions"])},
        ):
            emitted += 1
    return emitted


def scan_gap_closures(store_id):
    """Mark content gaps addressed once their keyword actually ranks
    (Semrush <=20, or GSC weighted <=20 with real impressions)."""
    end = gsc_data_end_date()
    week_start = end - timedelta(days=6)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, keyword FROM content_gaps WHERE store_id = %s AND status IN ('new', 'analyzed')",
            (store_id,),
        )
        gaps = cur.fetchall()
        if not gaps:
            return 0
        keywords = [g["keyword"] for g in gaps]

        cur.execute(
            """SELECT keyword, MIN(position) AS position FROM semrush_keywords
               WHERE store_id = %s AND keyword = ANY(%s) AND position <= 20
               GROUP BY keyword""",
            (store_id, keywords),
        )
        sem_ranks = {r["keyword"].lower(): int(r["position"]) for r in cur.fetchall()}

        cur.execute(
            """SELECT query,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position,
                      SUM(impressions) AS impressions
               FROM gsc_daily_metrics
               WHERE store_id = %s AND query = ANY(%s) AND date >= %s AND date <= %s
               GROUP BY query
               HAVING SUM(impressions) >= 50
                  AND SUM(position * impressions) / NULLIF(SUM(impressions), 0) <= 20""",
            (store_id, keywords, week_start.isoformat(), end.isoformat()),
        )
        gsc_ranks = {r["query"].lower(): round(float(r["position"]), 1) for r in cur.fetchall()}

    closed = 0
    for g in gaps:
        k = (g["keyword"] or "").lower()
        position = sem_ranks.get(k) or gsc_ranks.get(k)
        if position is None:
            continue
        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE content_gaps SET status = 'addressed', addressed_at = NOW(),
                          our_position = %s, updated_at = NOW()
                   WHERE id = %s AND status IN ('new', 'analyzed')""",
                (position, g["id"]),
            )
            if cur.rowcount:
                closed += 1
                emit_event(
                    store_id, "gap_closed", "info",
                    g["keyword"],
                    f"gap_closed:{g['id']}",
                    {"position": position},
                )
    return closed


def mirror_activity_events(store_id, days=7):
    """Mirror recent activity from other tables into dashboard_events so
    the changes feed is a single-table read."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT e.id, e.item_type, e.item_id, e.change_type, e.created_at
               FROM seo_change_events e
               WHERE e.store_id = %s AND e.created_at >= NOW() - make_interval(days => %s)""",
            (store_id, days),
        )
        changes = cur.fetchall()
        titles = {}
        for item_type, table in (("product", "synced_products"), ("collection", "synced_collections")):
            ids = [c["item_id"] for c in changes if c["item_type"] == item_type]
            if ids:
                cur.execute(f"SELECT id, title FROM {table} WHERE id = ANY(%s)", (ids,))
                titles[item_type] = {r["id"]: r["title"] for r in cur.fetchall()}

        cur.execute(
            """SELECT shopify_id, title, published_at FROM synced_articles
               WHERE store_id = %s AND is_published = TRUE
                 AND published_at >= NOW() - make_interval(days => %s)""",
            (store_id, days),
        )
        articles = cur.fetchall()

        cur.execute(
            """SELECT id, brand_mention_rate, share_of_voice, completed_at FROM ai_tracking_runs
               WHERE store_id = %s AND status = 'completed'
                 AND completed_at >= NOW() - make_interval(days => %s)""",
            (store_id, days),
        )
        runs = cur.fetchall()

        cur.execute(
            """SELECT id, alerts_generated, completed_at FROM competitor_check_runs
               WHERE store_id = %s AND alerts_generated > 0
                 AND completed_at >= NOW() - make_interval(days => %s)""",
            (store_id, days),
        )
        checks = cur.fetchall()

    for c in changes:
        title = titles.get(c["item_type"], {}).get(c["item_id"]) or f"{c['item_type']} #{c['item_id']}"
        emit_event(store_id, "seo_applied", "info", title, f"seo_applied:{c['id']}",
                   {"change_type": c["change_type"], "item_type": c["item_type"], "item_id": c["item_id"]})
    for a in articles:
        emit_event(store_id, "article_published", "info", a["title"],
                   f"article_published:{a['shopify_id']}", {})
    for r in runs:
        emit_event(store_id, "visibility_run", "info",
                   f"AI visibility run: {round(float(r['brand_mention_rate'] or 0) * 100)}% mentions",
                   f"visibility_run:{r['id']}",
                   {"mention_rate": float(r["brand_mention_rate"] or 0),
                    "share_of_voice": float(r["share_of_voice"] or 0)})
    for c in checks:
        emit_event(store_id, "competitor_check", "warning",
                   f"Competitor check found {c['alerts_generated']} alerts",
                   f"competitor_check:{c['id']}", {"alerts": c["alerts_generated"]})


def run_nightly_scans(store_id):
    results = {}
    for name, fn in (
        ("click_drops", scan_click_drops),
        ("keyword_dropouts", scan_keyword_dropouts),
        ("gap_closures", scan_gap_closures),
        ("activity_mirror", lambda sid: mirror_activity_events(sid)),
    ):
        try:
            results[name] = fn(store_id)
        except Exception as e:
            logger.error("Anomaly scan %s failed for store %s: %s", name, store_id, e)
    logger.info("Nightly scans for store %s: %s", store_id, results)
    return results


def maybe_run_weekly_visibility(store_id):
    """Opt-in weekly AI visibility run. OFF by default — these runs spend
    real OpenRouter credits (a restart-triggered scheduler once burned
    budget, see main.py). DB-based last-run check makes restarts safe."""
    if not get_setting("dashboard.weekly_visibility_enabled", False):
        return False
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT MAX(completed_at) AS last_run,
                      COALESCE(SUM(cost_estimate) FILTER (WHERE completed_at::date = CURRENT_DATE), 0) AS spend_today
               FROM ai_tracking_runs WHERE store_id = %s AND status = 'completed'""",
            (store_id,),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS running FROM ai_tracking_runs WHERE store_id = %s AND status IN ('pending','running')",
            (store_id,),
        )
        running = int(cur.fetchone()["running"] or 0)
        cur.execute("SELECT daily_budget FROM ai_config ORDER BY id LIMIT 1")
        budget_row = cur.fetchone()

    if running:
        return False
    last_run = row["last_run"]
    if last_run and last_run.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) - timedelta(days=7):
        return False
    budget = float(budget_row["daily_budget"]) if budget_row and budget_row["daily_budget"] is not None else None
    if budget is not None and float(row["spend_today"] or 0) >= budget:
        logger.info("Weekly visibility run skipped for store %s: daily budget reached", store_id)
        return False

    from app.services.ai_visibility_tracker import start_tracking_run
    start_tracking_run(store_id)
    logger.info("Weekly AI visibility run started for store %s", store_id)
    return True
