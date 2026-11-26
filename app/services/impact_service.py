"""Before/after search impact of applied optimizations.

Joins seo_change_events to gsc_page_daily (query-free page totals: the
query-level table drops anonymised queries, about half an article's
clicks). The pre window is the 14 days ending the day before the change; the
post window starts 2 days after it (Google recrawl lag) and is clipped to
the GSC data lag, so fresh changes report as "collecting" instead of a fake
verdict. impact_verdict decides whether the change beats the noise.
"""

import logging
from datetime import timedelta

from app.database import db
from app.services.dashboard_service import gsc_data_end_date
from app.services.impact_verdict import impact_verdict
from app.services.url_utils import url_path_key

logger = logging.getLogger(__name__)

PRE_DAYS = 14
POST_DAYS = 14
RECRAWL_LAG_DAYS = 2
MIN_PRE_IMPRESSIONS = 20
MIN_POST_DAYS = 7

_PATH_PREFIX = {"product": "products", "collection": "collections", "article": "blogs"}


def get_optimization_impact(store_id, days=60, limit=10, only=None):
    """only=(item_type, item_id) limits the analysis to that item's latest change."""
    end = gsc_data_end_date()

    with db.get_cursor() as cur:
        only_sql = " AND item_type = %s AND item_id = %s" if only else ""
        cur.execute(
            f"""SELECT id, item_type, item_id, change_type, created_at
               FROM seo_change_events
               WHERE store_id = %s AND created_at >= NOW() - make_interval(days => %s)
                 AND change_type <> 'revert' AND reverted_at IS NULL{only_sql}
               ORDER BY created_at DESC""",
            (store_id, days, *(only or ())),
        )
        events = cur.fetchall()

        meta = {}
        for item_type, table in (("product", "synced_products"), ("collection", "synced_collections")):
            ids = [e["item_id"] for e in events if e["item_type"] == item_type]
            if ids:
                cur.execute(
                    f"SELECT id, title, handle FROM {table} WHERE store_id = %s AND id = ANY(%s)",
                    (store_id, ids),
                )
                meta[item_type] = {r["id"]: r for r in cur.fetchall()}
        article_ids = [e["item_id"] for e in events if e["item_type"] == "article"]
        if article_ids:
            # An article's path is /blogs/<blog>/<handle>; fold the blog handle
            # into "handle" so the shared path join below needs no special case.
            cur.execute(
                """SELECT sa.id, sa.title, sb.handle || '/' || sa.handle AS handle
                   FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                   WHERE sa.store_id = %s AND sa.id = ANY(%s)""",
                (store_id, article_ids),
            )
            meta["article"] = {r["id"]: r for r in cur.fetchall()}

    summary = {"events_30d": sum(1 for e in events if (end - e["created_at"].date()).days <= 30),
               "improved": 0, "declined": 0, "collecting": 0}

    # Newest event per item wins; older ones within the analysis span are
    # superseded (their post window would overlap the newer change).
    deduped, superseded, seen = [], 0, set()
    for e in events:
        key = (e["item_type"], e["item_id"])
        if key in seen:
            superseded += 1
            continue
        seen.add(key)
        deduped.append(e)

    items = []
    path_keys = {}
    for e in deduped:
        m = meta.get(e["item_type"], {}).get(e["item_id"])
        if not m or not m.get("handle"):
            continue
        pk = f"{_PATH_PREFIX[e['item_type']]}/{m['handle']}"
        path_keys.setdefault(pk, []).append((e, m))

    if not path_keys:
        return {"items": [], "summary": {**summary, "superseded": superseded}}

    earliest = min(e["created_at"].date() for e in deduped) - timedelta(days=PRE_DAYS + 1)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT page, date, clicks, impressions, position
               FROM gsc_page_daily
               WHERE store_id = %s AND date >= %s AND date <= %s AND NOT is_fresh""",
            (store_id, earliest.isoformat(), end.isoformat()),
        )
        gsc_rows = cur.fetchall()

    daily_by_path = {}
    for r in gsc_rows:
        pk = url_path_key(r["page"])
        if pk in path_keys:
            daily_by_path.setdefault(pk, {}).setdefault(r["date"], {"clicks": 0, "impressions": 0, "pos_w": 0.0})
            d = daily_by_path[pk][r["date"]]
            d["clicks"] += int(r["clicks"] or 0)
            d["impressions"] += int(r["impressions"] or 0)
            if r["position"] is not None:
                d["pos_w"] += float(r["position"]) * int(r["impressions"] or 0)

    def window_stats(daily, start, stop):
        clicks = impressions = 0
        pos_w = 0.0
        d = start
        while d <= stop:
            row = daily.get(d)
            if row:
                clicks += row["clicks"]
                impressions += row["impressions"]
                pos_w += row["pos_w"]
            d += timedelta(days=1)
        n_days = (stop - start).days + 1
        return {
            "clicks": clicks,
            "clicks_per_day": round(clicks / n_days, 2) if n_days else 0,
            "impressions": impressions,
            "position": round(pos_w / impressions, 1) if impressions else None,
            "days": n_days,
        }

    for pk, entries in path_keys.items():
        daily = daily_by_path.get(pk, {})
        for e, m in entries:
            changed = e["created_at"].date()
            pre = window_stats(daily, changed - timedelta(days=PRE_DAYS), changed - timedelta(days=1))
            post_start = changed + timedelta(days=RECRAWL_LAG_DAYS)
            post_stop = min(post_start + timedelta(days=POST_DAYS - 1), end)
            post_days_available = (post_stop - post_start).days + 1

            if pre["impressions"] < MIN_PRE_IMPRESSIONS:
                status, post, clicks_change_pct, position_change = "insufficient", None, None, None
            elif post_days_available < MIN_POST_DAYS:
                status, post, clicks_change_pct, position_change = "collecting", None, None, None
                summary["collecting"] += 1
            else:
                post = window_stats(daily, post_start, post_stop)
                status, clicks_change_pct = impact_verdict(
                    pre["clicks"], pre["days"], post["clicks"], post["days"])
                position_change = (
                    round(post["position"] - pre["position"], 1)
                    if post["position"] is not None and pre["position"] is not None else None
                )
                if status == "improved":
                    summary["improved"] += 1
                elif status == "declined":
                    summary["declined"] += 1

            items.append(
                {
                    "event_id": e["id"],
                    "item_type": e["item_type"],
                    "item_id": e["item_id"],
                    "title": m["title"],
                    "change_type": e["change_type"],
                    "changed_at": e["created_at"].isoformat(),
                    "pre": pre,
                    "post": post,
                    "post_days_available": post_days_available if status == "collecting" else None,
                    "clicks_change_pct": clicks_change_pct,
                    "position_change": position_change,
                    "status": status,
                }
            )

    order = {"improved": 0, "declined": 1, "flat": 2, "collecting": 3, "insufficient": 4}
    items.sort(key=lambda i: (order[i["status"]], i["changed_at"]), reverse=False)
    summary["superseded"] = superseded
    return {"items": items[:limit], "summary": summary}


_VERDICTS = ("improved", "flat", "declined")


def item_impact(store_id, item_type, item_id):
    """The impact row for one item's latest change, or None without one."""
    items = get_optimization_impact(store_id, days=365, limit=1, only=(item_type, int(item_id)))["items"]
    return items[0] if items else None


def measured_items(store_id):
    """{(item_type, item_id)} whose latest change already has a verdict. Google
    has re-measured these, so they need not wait out the full hold period."""
    try:
        items = get_optimization_impact(store_id, days=45, limit=5000)["items"]
    except Exception:
        return set()
    return {(i["item_type"], i["item_id"]) for i in items if i["status"] in _VERDICTS}
