"""What happened this month, and what the app did about it.

The app recorded changes and scores but never answered the owner's question:
is this working? The report sets 28 days against the 28 before — clicks by
kind of page, organic orders, what the app changed and how those changes
turned out — in one place.
"""

from datetime import timedelta

from app.database import db
from app.services.dashboard_service import gsc_data_end_date

PERIOD_DAYS = 28
_PAGE_KIND_SQL = """CASE WHEN page LIKE '%%/products/%%' THEN 'Products'
                         WHEN page LIKE '%%/collections/%%' THEN 'Collections'
                         WHEN page LIKE '%%/blogs/%%' THEN 'Articles'
                         ELSE 'Home and other' END"""
_VERDICTS = ("improved", "flat", "declined")


def change_pct(current, previous):
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def win_rates(impact_items):
    """Per kind of change: how many have been measured, and how they turned out."""
    by_type = {}
    for item in impact_items:
        if item["status"] not in _VERDICTS:
            continue
        row = by_type.setdefault(
            item["change_type"], {"change_type": item["change_type"], "measured": 0, "improved": 0, "declined": 0})
        row["measured"] += 1
        if item["status"] in ("improved", "declined"):
            row[item["status"]] += 1
    return sorted(by_type.values(), key=lambda r: (-r["measured"], r["change_type"]))


def headline(totals):
    pct = change_pct(totals["clicks"], totals["clicks_prev"])
    if pct is None:
        trend = ""
    elif abs(pct) < 5:
        trend = ", level with the month before"
    else:
        trend = f", {'up' if pct > 0 else 'down'} {abs(pct):.0f}% on the month before"
    text = f"Google sent {totals['clicks']:,} clicks{trend}"
    if totals["orders"]:
        text += f", and {totals['orders']} organic orders worth ${totals['revenue']:,.0f}"
    changes = totals["changes"]
    return text + f". The app made {changes if changes else 'no'} change{'' if changes == 1 else 's'}."


def build_report(store_id):
    from app.services.impact_service import get_optimization_impact

    end = gsc_data_end_date()
    start = end - timedelta(days=PERIOD_DAYS - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=PERIOD_DAYS - 1)

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT {_PAGE_KIND_SQL} AS kind,
                       SUM(clicks) FILTER (WHERE date >= %(s)s) AS clicks,
                       SUM(clicks) FILTER (WHERE date < %(s)s) AS clicks_prev,
                       SUM(impressions) FILTER (WHERE date >= %(s)s) AS impressions,
                       SUM(impressions) FILTER (WHERE date < %(s)s) AS impressions_prev
                FROM gsc_daily_metrics
                WHERE store_id = %(sid)s AND date BETWEEN %(ps)s AND %(e)s
                GROUP BY 1""",
            {"sid": store_id, "s": start, "ps": prev_start, "e": end},
        )
        by_kind = [
            {
                "kind": r["kind"],
                "clicks": int(r["clicks"] or 0), "clicks_prev": int(r["clicks_prev"] or 0),
                "impressions": int(r["impressions"] or 0), "impressions_prev": int(r["impressions_prev"] or 0),
                "clicks_change_pct": change_pct(int(r["clicks"] or 0), int(r["clicks_prev"] or 0)),
            }
            for r in cur.fetchall()
        ]
        cur.execute(
            """SELECT INITCAP(device) AS device, SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date BETWEEN %s AND %s AND device IS NOT NULL
               GROUP BY 1 ORDER BY 2 DESC""",
            (store_id, start, end),
        )
        by_device = [
            {"device": r["device"], "clicks": int(r["clicks"] or 0), "impressions": int(r["impressions"] or 0),
             "position": round(float(r["position"] or 0), 1)}
            for r in cur.fetchall()
        ]
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE created_at >= %(s)s) AS orders,
                      COALESCE(SUM(total_price) FILTER (WHERE created_at >= %(s)s), 0) AS revenue,
                      COUNT(*) FILTER (WHERE created_at < %(s)s) AS orders_prev,
                      COALESCE(SUM(total_price) FILTER (WHERE created_at < %(s)s), 0) AS revenue_prev
               FROM organic_orders
               WHERE store_id = %(sid)s AND attribution = 'organic' AND created_at >= %(ps)s""",
            {"sid": store_id, "s": start, "ps": prev_start},
        )
        orders = cur.fetchone()
        cur.execute(
            """SELECT COALESCE(source, change_type) AS tool, COUNT(*) AS n
               FROM seo_change_events
               WHERE store_id = %s AND created_at >= %s AND change_type <> 'revert' AND reverted_at IS NULL
               GROUP BY 1 ORDER BY 2 DESC""",
            (store_id, start),
        )
        changes = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) AS n FROM synced_articles WHERE store_id = %s AND is_published AND published_at >= %s",
            (store_id, start),
        )
        published = cur.fetchone()["n"]
        cur.execute(
            """SELECT COUNT(*) AS checked, COUNT(*) FILTER (WHERE NOT indexed) AS missing
               FROM index_status WHERE store_id = %s""",
            (store_id,),
        )
        index = cur.fetchone()

    by_kind.sort(key=lambda k: -k["clicks"])
    impact = get_optimization_impact(store_id, days=60, limit=5000)
    totals = {
        "clicks": sum(k["clicks"] for k in by_kind),
        "clicks_prev": sum(k["clicks_prev"] for k in by_kind),
        "orders": int(orders["orders"] or 0), "orders_prev": int(orders["orders_prev"] or 0),
        "revenue": float(orders["revenue"] or 0), "revenue_prev": float(orders["revenue_prev"] or 0),
        "changes": sum(c["n"] for c in changes),
    }
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": PERIOD_DAYS},
        "headline": headline(totals),
        "totals": totals,
        "by_kind": by_kind,
        "by_device": by_device,
        "changes": changes,
        "articles_published": published,
        "outcomes": {k: impact["summary"].get(k, 0) for k in ("improved", "declined", "collecting")},
        "win_rates": win_rates(impact["items"]),
        "indexing": {"checked": index["checked"], "not_indexed": index["missing"]},
    }
