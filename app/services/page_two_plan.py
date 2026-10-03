"""The page-two plan: where the store is nearly visible.

Most of the store's impressions sit at positions 11 and lower, where almost
nobody clicks. This lists the searches where a page already ranks 4–20 — close
enough that focused work can move it — grouped by page, collections first
(they carry the broad terms), each with the monthly clicks moving up could add.
"""

from datetime import timedelta

from app.database import db
from app.services.ctr_benchmarks import estimate_click_gain, get_expected_ctr_curve, target_position

MIN_POSITION, MAX_POSITION = 4, 20
MIN_IMPRESSIONS = 30
MAX_QUERIES_PER_PAGE = 8
NAVIGATIONAL_PAGE_COUNT = 3
_TYPE_ORDER = {"collection": 0, "product": 1, "article": 2}


def build_plan(rows, pages, curve):
    """rows: [{path, query, impressions, clicks, position}] already limited to
    the striking band. pages: {path: {item_type, id, title}}. Pure."""
    by_page = {}
    for r in rows:
        page = pages.get(r["path"])
        if not page:
            continue
        ctr = r["clicks"] / r["impressions"] if r["impressions"] else 0
        gain = estimate_click_gain(r["impressions"], r["position"], curve, actual_ctr=ctr)
        if gain < 1:
            continue
        entry = by_page.setdefault(r["path"], {**page, "path": r["path"], "est_clicks_gain": 0,
                                               "impressions": 0, "queries": []})
        entry["est_clicks_gain"] += gain
        entry["impressions"] += r["impressions"]
        entry["queries"].append({
            "query": r["query"], "impressions": r["impressions"], "clicks": r["clicks"],
            "position": round(r["position"], 1), "target": target_position(r["position"]),
            "est_clicks_gain": gain,
        })
    plan = list(by_page.values())
    for entry in plan:
        entry["queries"].sort(key=lambda q: -q["est_clicks_gain"])
        entry["queries"] = entry["queries"][:MAX_QUERIES_PER_PAGE]
    plan.sort(key=lambda p: (_TYPE_ORDER.get(p["item_type"], 9), -p["est_clicks_gain"]))
    return plan


def _merge_url_variants(rows):
    """One row per (path, query): Google reports the same page under several
    URL spellings (parameters, trailing slash), which would list a search twice."""
    merged = {}
    for r in rows:
        m = merged.setdefault((r["path"], r["query"]), {**r, "clicks": 0.0, "impressions": 0.0, "_pos": 0.0})
        m["clicks"] += r["clicks"]
        m["impressions"] += r["impressions"]
        m["_pos"] += r["position"] * r["impressions"]
    for m in merged.values():
        m["position"] = m.pop("_pos") / m["impressions"] if m["impressions"] else 0
    return list(merged.values())


def _known_pages(store_id):
    pages = {}
    with db.get_cursor() as cur:
        cur.execute("SELECT id, title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'", (store_id,))
        for r in cur.fetchall():
            pages[f"products/{r['handle']}".lower()] = {"item_type": "product", "id": r["id"], "title": r["title"]}
        cur.execute("SELECT id, title, handle FROM synced_collections WHERE store_id = %s", (store_id,))
        for r in cur.fetchall():
            pages[f"collections/{r['handle']}".lower()] = {"item_type": "collection", "id": r["id"], "title": r["title"]}
        cur.execute(
            """SELECT sa.id, sa.title, sb.handle || '/' || sa.handle AS path
               FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.is_published""",
            (store_id,),
        )
        for r in cur.fetchall():
            pages[f"blogs/{r['path']}".lower()] = {"item_type": "article", "id": r["id"], "title": r["title"]}
    return pages


def get_page_two_plan(store_id):
    from app.services.dashboard_service import gsc_data_end_date
    from app.services.url_utils import url_path_key

    end = gsc_data_end_date()
    start = end - timedelta(days=27)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT page, query, SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date BETWEEN %s AND %s AND query IS NOT NULL AND query <> ''
               GROUP BY page, query
               HAVING SUM(impressions) >= %s
                  AND SUM(position * impressions) / NULLIF(SUM(impressions), 0) BETWEEN %s AND %s""",
            (store_id, start, end, MIN_IMPRESSIONS, MIN_POSITION, MAX_POSITION),
        )
        rows = [
            {"path": url_path_key(r["page"]).lower(), "query": r["query"], "clicks": float(r["clicks"] or 0),
             "impressions": float(r["impressions"] or 0), "position": float(r["position"] or 0)}
            for r in cur.fetchall()
        ]
    rows = _merge_url_variants(rows)

    # A search that shows many of the store's pages at once is someone looking
    # for the store itself (sitelinks) — not a ranking any one page can win.
    pages_per_query = {}
    for r in rows:
        pages_per_query.setdefault(r["query"], set()).add(r["path"])
    rows = [r for r in rows if len(pages_per_query[r["query"]]) < NAVIGATIONAL_PAGE_COUNT]

    plan = build_plan(rows, _known_pages(store_id), get_expected_ctr_curve(store_id))
    for entry in plan:
        entry["impressions"] = round(entry["impressions"])
        for q in entry["queries"]:
            q["impressions"], q["clicks"] = round(q["impressions"]), round(q["clicks"])
    return {
        "pages": plan,
        "total_gain": sum(p["est_clicks_gain"] for p in plan),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
    }
