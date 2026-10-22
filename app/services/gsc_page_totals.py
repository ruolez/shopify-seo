"""Per-page daily totals from Google Search Console.

`gsc_daily_metrics` is grouped by query, and Google omits anonymized
(low-volume / private) queries from any query-grouped result, so summing
those rows per page under-reports clicks and impressions — by roughly
half on this store's articles. The page dimension on its own has no such
gap, so page-level figures shown to the user come from `gsc_page_daily`,
which `pull_data` fills with a second, query-free request.
"""
import logging
from datetime import timedelta
from urllib.parse import urlparse

import psycopg2.extras

from app.database import db

logger = logging.getLogger(__name__)

MAX_ROWS_PER_REQUEST = 25000


def aggregate_by_path(rows):
    """{path: {clicks, impressions, position}} from page rows, merging URL
    variants (trailing slash, query string, case) of one page."""
    out = {}
    for r in rows:
        path = urlparse(r.get("page") or "").path.rstrip("/").lower()
        if not path:
            continue
        entry = out.setdefault(path, {"clicks": 0.0, "impressions": 0.0, "_pos_w": 0.0})
        clicks = float(r.get("clicks") or 0)
        imps = float(r.get("impressions") or 0)
        entry["clicks"] += clicks
        entry["impressions"] += imps
        if r.get("position") and imps:
            entry["_pos_w"] += float(r["position"]) * imps
    for entry in out.values():
        entry["position"] = round(entry["_pos_w"] / entry["impressions"], 1) if entry["impressions"] else None
        entry["clicks"] = int(round(entry["clicks"]))
        entry["impressions"] = int(round(entry["impressions"]))
        entry.pop("_pos_w", None)
    return out


def totals_window(latest, days):
    """The `days`-day window ending on `latest` (the newest day GSC has
    reported), as {'from', 'to', 'days'}; None when there is no data."""
    if not latest:
        return None
    return {"from": (latest - timedelta(days=days - 1)).isoformat(), "to": latest.isoformat(), "days": days}


def fetch_page_rows(service, site_url, date_from, date_to, data_state="final"):
    """Every (date, page) row GSC reports for the range, paginated."""
    rows = []
    start = 0
    while True:
        body = {
            "startDate": date_from,
            "endDate": date_to,
            "dimensions": ["date", "page"],
            "rowLimit": MAX_ROWS_PER_REQUEST,
            "startRow": start,
            "dataState": data_state,
        }
        batch = service.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])
        rows.extend(batch)
        if len(batch) < MAX_ROWS_PER_REQUEST:
            return rows
        start += MAX_ROWS_PER_REQUEST


def store_page_rows(store_id, date_from, date_to, rows, is_fresh):
    """Replace the store's page totals for the range with `rows`. Replacing
    (not upserting) drops pages that stop appearing once a fresh day is
    finalized."""
    values = []
    for r in rows:
        keys = r.get("keys") or []
        if len(keys) < 2:
            continue
        values.append((store_id, keys[0], keys[1], r.get("clicks", 0), r.get("impressions", 0),
                       r.get("ctr", 0), r.get("position", 0), is_fresh))
    with db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM gsc_page_daily WHERE store_id = %s AND date BETWEEN %s AND %s",
            (store_id, date_from, date_to),
        )
        if values:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO gsc_page_daily
                   (store_id, date, page, clicks, impressions, ctr, position, is_fresh)
                   VALUES %s""",
                values,
            )
    return len(values)


def page_totals_by_path(store_id, days=30, path_contains=None):
    """({path: {clicks, impressions, position}}, window) over the last `days`
    days GSC has reported — fresh days included, so the window ends today or
    yesterday rather than three days back. ({}, None) when GSC isn't
    connected or hasn't been pulled yet."""
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT 1 FROM gsc_credentials WHERE store_id = %s AND gsc_property IS NOT NULL", (store_id,))
            if not cur.fetchone():
                return {}, None
            cur.execute("SELECT MAX(date) AS latest FROM gsc_page_daily WHERE store_id = %s", (store_id,))
            latest = (cur.fetchone() or {}).get("latest")
            window = totals_window(latest, days)
            if not window:
                return {}, None
            params = [store_id, window["from"], window["to"]]
            path_clause = ""
            if path_contains:
                path_clause = " AND page LIKE %s"
                params.append(f"%{path_contains}%")
            cur.execute(
                f"""SELECT page, SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                           CASE WHEN SUM(impressions) > 0 THEN SUM(position * impressions) / SUM(impressions) END AS position
                    FROM gsc_page_daily
                    WHERE store_id = %s AND date BETWEEN %s AND %s{path_clause}
                    GROUP BY page""",
                params,
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("GSC page totals unavailable for store %s: %s", store_id, e)
        return {}, None
    return aggregate_by_path(rows), window


def article_gsc_metrics(store_id, days=30):
    """Blog and News lists: per-article clicks / impressions / position over
    the last `days` reported days, keyed by /blogs/<blog>/<handle>."""
    return page_totals_by_path(store_id, days=days, path_contains="/blogs/")


def article_path(blog_handle, handle):
    return f"/blogs/{blog_handle or ''}/{handle or ''}".lower()


def attach_article_metrics(article, by_path, window):
    """Set gsc_clicks / gsc_impressions / gsc_position / gsc_window on an
    article dict from its /blogs/<blog>/<handle> path."""
    metrics = by_path.get(article_path(article.get("blog_handle"), article.get("handle"))) or {}
    article["gsc_clicks"] = metrics.get("clicks")
    article["gsc_impressions"] = metrics.get("impressions")
    article["gsc_position"] = metrics.get("position")
    article["gsc_window"] = window
    return article


def backfill_days_if_empty(store_id, today, default_from, backfill_days):
    """First pull after this table was introduced: reach back `backfill_days`
    so the 30-day views are populated immediately instead of after a month."""
    with db.get_cursor() as cur:
        cur.execute("SELECT 1 FROM gsc_page_daily WHERE store_id = %s LIMIT 1", (store_id,))
        if cur.fetchone():
            return default_from
    return (today - timedelta(days=backfill_days)).isoformat()
