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
        # Prefer snapshot history for the striking-distance delta (a real
        # 7-day comparison); fall back to Semrush previous_position (delta
        # vs whenever the last CSV import happened).
        striking_change = None
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT semrush_striking FROM store_daily_snapshot
                   WHERE store_id = %s AND semrush_striking > 0
                     AND snapshot_date <= CURRENT_DATE - 7
                   ORDER BY snapshot_date DESC LIMIT 1""",
                (store_id,),
            )
            week_ago = cur.fetchone()
        if week_ago:
            striking_change = int(kw["striking"]) - int(week_ago["semrush_striking"])
        else:
            striking_change = int(kw["striking"]) - int(kw["striking_prev"])
        kpis["keywords"] = {
            "total": int(kw["total"]),
            "top10": int(kw["top10"]),
            "striking": int(kw["striking"]),
            "striking_change": striking_change,
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


def _brand_tokens(store):
    """Strings that identify navigational/brand queries. Kept conservative —
    a generic word from the shop name (e.g. 'tobacco') must never be used
    as a brand marker or it would exclude the whole catalog's queries."""
    tokens = set()
    name = (store.get("shop_name") or store.get("name") or "").lower().strip()
    if name:
        tokens.add(name)
        tokens.add(name.replace(" ", ""))
        first = name.split()[0]
        if any(ch.isdigit() for ch in first) or len(first) >= 5:
            tokens.add(first)
    domain = (store.get("primary_domain") or store.get("domain") or "").lower()
    label = domain.split("//")[-1].replace("www.", "").split(".")[0]
    if len(label) >= 4:
        tokens.add(label)
    return tokens


def get_ctr_opportunities(store_id, limit=10, min_impressions=200):
    """Queries ranking in the top 10 whose CTR trails the expected curve for
    their position — the cheapest wins on the site (meta title rewrites)."""
    from app.services.ctr_benchmarks import get_expected_ctr_curve, is_ctr_underperformer
    from app.services.seo_analyzer import _expected_ctr
    from app.services.url_utils import url_path_key

    ctr_curve = get_expected_ctr_curve(store_id)
    end = gsc_data_end_date()
    start = end - timedelta(days=27)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT name, shop_name, domain, primary_domain FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()
        if not store:
            return []
        cur.execute(
            """SELECT query, page, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY query, page
               HAVING SUM(impressions) >= %s
                  AND SUM(position * impressions) / NULLIF(SUM(impressions), 0) <= 10
               ORDER BY SUM(impressions) DESC
               LIMIT 200""",
            (store_id, start.isoformat(), end.isoformat(), min_impressions),
        )
        rows = cur.fetchall()

    brand = _brand_tokens(dict(store))
    candidates = []
    for r in rows:
        query = r["query"] or ""
        if any(t in query.lower() for t in brand):
            continue
        impressions = int(r["impressions"] or 0)
        actual = int(r["clicks"] or 0) / impressions if impressions else 0
        position = float(r["position"])
        expected = _expected_ctr(position, ctr_curve)
        if not is_ctr_underperformer(position, actual, impressions, ctr_curve):
            continue
        candidates.append(
            {
                "query": query,
                "page": r["page"],
                "path_key": url_path_key(r["page"]),
                "position": round(position, 1),
                "impressions": impressions,
                "actual_ctr": round(actual, 4),
                "expected_ctr": round(expected, 4),
                "est_clicks_gain": int((expected - actual) * impressions),
            }
        )

    candidates.sort(key=lambda c: c["est_clicks_gain"], reverse=True)
    candidates = candidates[:limit]

    # Batch-resolve pages to synced items so rows deep-link into the fix UI.
    handles = {"product": {}, "collection": {}}
    for c in candidates:
        parts = c["path_key"].split("/")
        if len(parts) == 2 and parts[0] == "products":
            handles["product"][parts[1]] = None
        elif len(parts) == 2 and parts[0] == "collections":
            handles["collection"][parts[1]] = None
    with db.get_cursor() as cur:
        for item_type, table in (("product", "synced_products"), ("collection", "synced_collections")):
            if handles[item_type]:
                cur.execute(
                    f"SELECT id, handle, title, last_optimized_at FROM {table} WHERE store_id = %s AND handle = ANY(%s)",
                    (store_id, list(handles[item_type].keys())),
                )
                for row in cur.fetchall():
                    handles[item_type][row["handle"]] = {
                        "id": row["id"],
                        "title": row["title"],
                        "last_optimized_at": row["last_optimized_at"],
                    }

    for c in candidates:
        parts = c["path_key"].split("/")
        c["matched_item"] = None
        if len(parts) == 2 and parts[0] in ("products", "collections"):
            item_type = "product" if parts[0] == "products" else "collection"
            match = handles[item_type].get(parts[1])
            if match:
                c["matched_item"] = {"item_type": item_type, **match}
        del c["path_key"]
    return candidates


def _dismissed_action_keys(store_id):
    """Action keys the user dismissed or snoozed (snooze still in effect)."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT action_key FROM action_dismissals
               WHERE store_id = %s AND (snoozed_until IS NULL OR snoozed_until > NOW())""",
            (store_id,),
        )
        return {r["action_key"] for r in cur.fetchall()}


def dismiss_action(store_id, action_key, snooze_days=None):
    snoozed_until = None
    if snooze_days:
        snoozed_until = datetime.now(timezone.utc) + timedelta(days=int(snooze_days))
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO action_dismissals (store_id, action_key, snoozed_until)
               VALUES (%s, %s, %s)
               ON CONFLICT (store_id, action_key)
               DO UPDATE SET snoozed_until = EXCLUDED.snoozed_until, dismissed_at = NOW()""",
            (store_id, action_key, snoozed_until),
        )


def build_action_queue(store_id, limit=15):
    """One prioritized to-do list merged from every analysis engine,
    ranked by estimated click gain."""
    from urllib.parse import urlencode

    items = []
    counts = {}
    dismissed = _dismissed_action_keys(store_id)

    # Actionable priorities and re-measuring items are strictly separated: a
    # page applied to Shopify leaves the actionable queue immediately and sits
    # in the buttonless re-measuring band until GSC has fresh data for it.
    measuring = []
    try:
        from app.services.seo_priority_service import get_queued_priorities
        priorities, measuring = get_queued_priorities(store_id, dismissed_keys=dismissed)
    except Exception as e:
        logger.warning("Action queue: priorities failed: %s", e)
        priorities = []
    from app.services.seo_priority_service import UNSPECIFIED_ACTION_LABEL
    for p in priorities:
        key = f"priority:{p['item_type']}:{p['id']}"
        severity = "high" if p["priority_score"] >= 80 else "med" if p["priority_score"] >= 60 else "low"
        # Display only. top_opportunity is a keyword, not an action — using it
        # as the label (and the old "Optimize on-page SEO" default) described
        # work the engine never actually recommended.
        action = p.get("recommended_action") or UNSPECIFIED_ACTION_LABEL
        params = {"optimize": p["item_type"], "id": p["id"], "ak": key, "store_id": store_id}
        fix = None
        if p.get("target_keyword"):
            fix = {
                "kind": "striking_distance",
                "keyword": p["target_keyword"],
                "position": p.get("target_keyword_position"),
                "volume": p.get("target_keyword_volume"),
            }
            params["fix"] = "striking_distance"
            params["kw"] = p["target_keyword"]
            if p.get("target_keyword_position") is not None:
                params["pos"] = p["target_keyword_position"]
            if p.get("target_keyword_volume") is not None:
                params["vol"] = p["target_keyword_volume"]
        elif p.get("recommended_action"):
            # Only a real recommendation becomes the optimizer's priority focus.
            # The unspecified label would read as an instruction to the AI.
            params["action"] = p["recommended_action"]
        items.append(
            {
                "source": "priority",
                "severity": severity,
                "title": p["title"],
                "detail": action
                + (" · cannibalization risk" if p.get("cannibalization_warning") else "")
                + (" · a higher-volume keyword is available — review this page's primary"
                   if p.get("primary_review_suggested") else ""),
                "href": "/seo?" + urlencode(params),
                "est_clicks_gain": p.get("potential_clicks_gain"),
                "item_type": p["item_type"],
                "item_id": p["id"],
                "action_key": key,
                "fix": fix,
            }
        )
    counts["priority"] = len(priorities)

    try:
        ctr = get_ctr_opportunities(store_id, limit=15)
    except Exception as e:
        logger.warning("Action queue: CTR scan failed: %s", e)
        ctr = []

    # One meta title serves every query variant of a page — group the
    # underperforming queries by target so one Fix clears them all.
    from app.services.seo_priority_service import _within_measuring_window
    from app.services.url_utils import url_path_key

    ctr_groups = {}
    ctr_order = []
    for c in ctr:
        if f"ctr:{c['query']}" in dismissed:
            continue  # legacy per-query dismissals still honored, per variant
        matched = c.get("matched_item")
        if matched and _within_measuring_window(matched.get("last_optimized_at"), 28):
            continue  # freshly optimized — GSC hasn't re-measured yet
        if matched:
            gkey = f"ctr:{matched['item_type']}:{matched['id']}"
        else:
            gkey = f"ctr:page:{url_path_key(c['page'])}"
        if gkey in dismissed:
            continue
        group = ctr_groups.get(gkey)
        if group is None:
            group = ctr_groups[gkey] = {"matched": matched, "page": c["page"], "variants": []}
            ctr_order.append(gkey)
        group["variants"].append(c)

    for gkey in ctr_order:
        group = ctr_groups[gkey]
        variants = sorted(group["variants"], key=lambda v: v["est_clicks_gain"], reverse=True)
        top = variants[0]
        matched = group["matched"]
        total_gain = sum(v["est_clicks_gain"] for v in variants)
        queries = [v["query"] for v in variants[:5]]
        fix = {
            "kind": "ctr_rewrite",
            "keyword": top["query"],
            "queries": queries,
            "position": top["position"],
            "actual_ctr": top["actual_ctr"],
            "expected_ctr": top["expected_ctr"],
        }
        if matched:
            href = "/seo?" + urlencode(
                {
                    "fix": "ctr",
                    "optimize": matched["item_type"],
                    "id": matched["id"],
                    "q": "|".join(queries),
                    "pos": top["position"],
                    "ctr": top["actual_ctr"],
                    "xctr": top["expected_ctr"],
                    "gain": total_gain,
                    "ak": gkey,
                    "store_id": store_id,
                }
            )
        else:
            href = "/gsc"
        target = matched["title"] if matched else group["page"]
        extra = f" (+{len(variants) - 1} variant{'s' if len(variants) > 2 else ''})" if len(variants) > 1 else ""
        items.append(
            {
                "source": "ctr",
                "severity": "med",
                "title": f'Rewrite meta title for "{top["query"]}"{extra}',
                "detail": f"{target} · pos {top['position']} · CTR {top['actual_ctr'] * 100:.1f}% vs {top['expected_ctr'] * 100:.1f}% expected",
                "href": href,
                "est_clicks_gain": total_gain,
                "item_type": matched["item_type"] if matched else None,
                "item_id": matched["id"] if matched else None,
                "action_key": gkey,
                "fix": fix,
            }
        )
    counts["ctr"] = len(ctr_order)

    insights = get_store_insights(store_id)

    def summary_row(source, severity, title, detail, href):
        key = f"{source}:summary"
        if key in dismissed:
            return
        items.append(
            {
                "source": source, "severity": severity, "title": title, "detail": detail,
                "href": href, "est_clicks_gain": None, "item_type": None, "item_id": None,
                "action_key": key,
            }
        )

    cann = insights.get("cannibalization_count", 0)
    counts["cannibalization"] = cann
    if cann:
        summary_row("cannibalization", "high", f"{cann} keyword cannibalization issue{'s' if cann != 1 else ''}",
                    "Queries ranking on multiple pages", "/gsc")

    # action_key stays "technical:summary" so existing dismissals keep applying
    tech = insights.get("technical_seo") or {}
    counts["technical"] = tech.get("total_issues", 0)
    if tech.get("total_issues"):
        summary_row("technical", "med", f"{tech['total_issues']} SEO issues",
                    f"{tech.get('fixable_issues', 0)} auto-fixable", "/seo?tab=issues")

    gaps = insights.get("content_gaps") or {}
    counts["gaps"] = gaps.get("new", 0)
    if gaps.get("new"):
        summary_row("gaps", "low", f"{gaps['new']} new content gap{'s' if gaps['new'] != 1 else ''}",
                    f"{gaps.get('total', 0)} total opportunities", "/content-gaps")

    alerts = insights.get("unread_competitor_alerts", 0)
    counts["competitor"] = alerts
    if alerts:
        summary_row("competitor", "med", f"{alerts} unread competitor alert{'s' if alerts != 1 else ''}",
                    "Ranking changes on tracked keywords", "/competitors")

    il = insights.get("interlinking") or {}
    orphans = il.get("orphan_products", 0) + il.get("orphan_collections", 0)
    counts["links"] = orphans + il.get("broken_links", 0)
    if counts["links"]:
        il_href = "/interlinking?tab=issues" if il.get("broken_links", 0) else "/interlinking?tab=coverage"
        summary_row("links", "low", f"{orphans} orphan pages · {il.get('broken_links', 0)} broken links",
                    f"{il.get('under_linked', 0)} under-linked", il_href)

    severity_rank = {"high": 0, "med": 1, "low": 2}
    items.sort(key=lambda i: (-(i["est_clicks_gain"] or 0), severity_rank[i["severity"]]))

    now = datetime.now(timezone.utc)
    measuring_rows = []
    for p in measuring[:10]:
        applied_days = None
        if p.get("last_optimized_at"):
            at = datetime.fromisoformat(p["last_optimized_at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            applied_days = max(0, int((now - at).total_seconds() // 86400))
        measuring_rows.append({
            "title": p["title"],
            "item_type": p["item_type"],
            "item_id": p["id"],
            "target_keyword": p.get("target_keyword"),
            "est_clicks_gain": p.get("potential_clicks_gain"),
            "applied_days_ago": applied_days,
            "days_remaining": max(0, 28 - applied_days) if applied_days is not None else None,
            "href": f"/seo?optimize={p['item_type']}&id={p['id']}&store_id={store_id}",
        })

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT
                 (SELECT COUNT(*) FROM synced_products WHERE store_id = %(sid)s) AS products_total,
                 (SELECT COUNT(*) FROM synced_products WHERE store_id = %(sid)s
                    AND last_optimized_at >= NOW() - INTERVAL '28 days') AS products_measuring,
                 (SELECT COUNT(*) FROM synced_collections WHERE store_id = %(sid)s) AS collections_total,
                 (SELECT COUNT(*) FROM synced_collections WHERE store_id = %(sid)s
                    AND last_optimized_at >= NOW() - INTERVAL '28 days') AS collections_measuring,
                 (SELECT MIN(last_optimized_at) FROM (
                     SELECT last_optimized_at FROM synced_products WHERE store_id = %(sid)s
                       AND last_optimized_at >= NOW() - INTERVAL '28 days'
                     UNION ALL
                     SELECT last_optimized_at FROM synced_collections WHERE store_id = %(sid)s
                       AND last_optimized_at >= NOW() - INTERVAL '28 days') w) AS oldest_in_window""",
            {"sid": store_id},
        )
        ms = dict(cur.fetchone())
    oldest = ms.pop("oldest_in_window")
    if oldest:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        ms["next_complete_days"] = max(0, 28 - int((now - oldest).total_seconds() // 86400))
    else:
        ms["next_complete_days"] = None

    return {
        "items": items[:limit],
        # Pre-truncation count. The badge must not report a number the page
        # can't show: it used to be len(items) from a limit=50 build while the
        # dashboard renders limit=15.
        "total_items": len(items),
        "total_by_source": counts,
        "measuring": measuring_rows,
        "measuring_summary": ms,
    }


def get_setup_state(store_id):
    """Cold-start checklist state, inferred entirely from existing tables."""
    with db.get_cursor() as cur:
        cur.execute("SELECT last_synced_at FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        cur.execute("SELECT 1 FROM gsc_credentials WHERE store_id = %s LIMIT 1", (store_id,))
        gsc = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM semrush_imports WHERE store_id = %s LIMIT 1", (store_id,))
        semrush = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM seo_change_events WHERE store_id = %s LIMIT 1", (store_id,))
        optimized = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM blog_schedule_settings WHERE store_id = %s LIMIT 1", (store_id,))
        blog = cur.fetchone() is not None
        cur.execute("SELECT api_key_enc FROM ai_config ORDER BY id LIMIT 1")
        ai_row = cur.fetchone()
    steps = [
        {"key": "sync", "label": "Sync your store from Shopify", "done": bool(store and store["last_synced_at"]), "href": "/stores"},
        {"key": "ai", "label": "Add your OpenRouter API key", "done": bool(ai_row and ai_row.get("api_key_enc")), "href": "/settings"},
        {"key": "gsc", "label": "Connect Google Search Console", "done": gsc, "href": "/gsc"},
        {"key": "semrush", "label": "Import Semrush keyword data", "done": semrush, "href": "/semrush"},
        {"key": "optimize", "label": "Apply your first optimization", "done": optimized, "href": "/seo?tab=priorities"},
        {"key": "blog", "label": "Set a blog publishing cadence", "done": blog, "href": "/blog?tab=scheduled"},
    ]
    return {"steps": steps, "complete": all(s["done"] for s in steps)}


def get_nav_badges(store_id):
    """Pending-work counts for the sidebar. One action-queue build (the caller
    caches this endpoint) plus two cheap counts; zero values are omitted."""
    q = build_action_queue(store_id, limit=50)
    c = q["total_by_source"]
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS n FROM bulk_tasks
               WHERE store_id = %s AND kind IN ('geo_optimize', 'fact_boost')
                 AND (status = 'failed' OR failed > 0)
                 AND created_at > NOW() - INTERVAL '7 days'""",
            (store_id,),
        )
        ai_failed = cur.fetchone()["n"]
        cur.execute(
            """SELECT COUNT(*) AS n FROM article_wizard_sessions
               WHERE store_id = %s AND scheduled_for IS NOT NULL AND status != 'published'
                 AND (status = 'error' OR scheduled_for < NOW() - INTERVAL '1 hour')""",
            (store_id,),
        )
        content_stuck = cur.fetchone()["n"]
    # Counts are "work you'd queue", not raw issue sums — a sidebar of permanent
    # 99+ chips is noise. seo = bounded priority/CTR items plus pages with
    # keyword issues (the merged Issues tab); cannibalization stays off (GSC).
    insights = get_store_insights(store_id)
    tech_pages = (insights.get("technical_seo") or {}).get("items_with_issues", 0)
    badges = {
        "home": q["total_items"],
        "seo": c.get("priority", 0) + c.get("ctr", 0) + tech_pages,
        "interlinking": c.get("links", 0),
        "content-gaps": c.get("gaps", 0),
        "competitors": c.get("competitor", 0),
        "ai-seo": ai_failed,
        "content": content_stuck,
    }
    payload = {k: v for k, v in badges.items() if v}
    # Each badge sums differently-shaped work, so the raw number never matches
    # any single count on the page it points at (SEO counts pages-with-issues,
    # the Issues tab headlines total issues). Ship the arithmetic with it.
    payload["_tooltips"] = {
        "home": f"{q['total_items']} items in the action queue",
        "seo": (
            f"{c.get('priority', 0)} ranking opportunities"
            f" + {c.get('ctr', 0)} CTR fixes"
            f" + {tech_pages} pages with issues"
        ),
        "interlinking": f"{c.get('links', 0)} orphan or broken links",
        "content-gaps": f"{c.get('gaps', 0)} new content gaps",
        "competitors": f"{c.get('competitor', 0)} unread competitor alerts",
        "ai-seo": f"{ai_failed} failed AI runs in the last 7 days",
        "content": f"{content_stuck} scheduled posts stuck or errored",
    }
    return payload


def get_coverage(store_id):
    """Completeness percentages over ACTIVE products. Shared by the ops
    widget and the daily snapshot rollup so the two can never drift.
    Bands come from the same rules.product_seo thresholds the item analyzer
    scores with — a hard-coded band here made the tile disagree with items."""
    from app.prompt_defaults import DEFAULTS
    from app.settings_manager import get_setting

    thresholds = get_setting("rules.product_seo", DEFAULTS["rules.product_seo"])["thresholds"]
    mt_ok = thresholds.get("meta_title", {}).get("ok", [30, 70])
    md_ok = thresholds.get("meta_description", {}).get("ok", [100, 200])

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE char_length(COALESCE(meta_title, '')) BETWEEN %s AND %s) AS meta_title_ok,
                      COUNT(*) FILTER (WHERE char_length(COALESCE(meta_description, '')) BETWEEN %s AND %s) AS meta_desc_ok
               FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'""",
            (mt_ok[0], mt_ok[1], md_ok[0], md_ok[1], store_id),
        )
        p = cur.fetchone()
        total = int(p["total"] or 0)

        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE COALESCE(img->>'alt', '') <> '') AS with_alt, COUNT(*) AS total
               FROM synced_products sp, jsonb_array_elements(sp.images) img
               WHERE sp.store_id = %s AND sp.status = 'ACTIVE' AND jsonb_typeof(sp.images) = 'array'""",
            (store_id,),
        )
        img = cur.fetchone()

        cur.execute(
            """SELECT COUNT(DISTINCT sp.id) AS cnt FROM synced_products sp
               JOIN schema_markup sm ON sm.store_id = sp.store_id
                    AND sm.resource_type = 'product' AND sm.resource_id = sp.shopify_id
                    AND sm.schema_type = 'Product' AND sm.pushed_to_shopify = TRUE
               WHERE sp.store_id = %s AND sp.status = 'ACTIVE'""",
            (store_id,),
        )
        schema_cnt = int(cur.fetchone()["cnt"] or 0)

        cur.execute(
            """SELECT COUNT(DISTINCT sp.id) AS cnt FROM synced_products sp
               JOIN content_keyword_map ckm ON ckm.store_id = sp.store_id
                    AND ckm.item_type = 'product' AND ckm.item_id = sp.id
               WHERE sp.store_id = %s AND sp.status = 'ACTIVE'""",
            (store_id,),
        )
        kw_cnt = int(cur.fetchone()["cnt"] or 0)

        cur.execute(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status = 'published') AS published FROM aeo_assets WHERE store_id = %s",
            (store_id,),
        )
        aeo = cur.fetchone()

    def pct(n, d):
        return round(n / d * 100, 1) if d else None

    return {
        "products_total": total,
        "meta_title_pct": pct(int(p["meta_title_ok"] or 0), total),
        "meta_desc_pct": pct(int(p["meta_desc_ok"] or 0), total),
        "alt_text_pct": pct(int(img["with_alt"] or 0), int(img["total"] or 0)),
        "schema_pushed_pct": pct(schema_cnt, total),
        "keyword_mapped_pct": pct(kw_cnt, total),
        "aeo_assets": {"published": int(aeo["published"] or 0), "total": int(aeo["total"] or 0)},
    }


# Staleness thresholds (hours): source -> (ok_under, stale_under); beyond is "old"
_FRESHNESS_LEVELS = {
    "sync": (24, 72),
    "gsc_pull": (24, 72),
    "semrush_import": (14 * 24, 30 * 24),
    "visibility_run": (8 * 24, 14 * 24),
    "competitor_check": (8 * 24, 14 * 24),
}


def get_ops(store_id):
    now = datetime.now(timezone.utc)

    def freshness_entry(source, at):
        if not at:
            return {"at": None, "age_hours": None, "level": "none"}
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age = (now - at).total_seconds() / 3600
        ok, stale = _FRESHNESS_LEVELS[source]
        level = "ok" if age < ok else "stale" if age < stale else "old"
        return {"at": at.isoformat(), "age_hours": round(age, 1), "level": level}

    with db.get_cursor() as cur:
        cur.execute("SELECT last_synced_at FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone()
        sync_at = row["last_synced_at"] if row else None

        cur.execute(
            "SELECT MAX(completed_at) AS at FROM gsc_data_pulls WHERE store_id = %s AND status = 'completed'",
            (store_id,),
        )
        gsc_at = cur.fetchone()["at"]

        cur.execute("SELECT MAX(created_at) AS at FROM semrush_imports WHERE store_id = %s", (store_id,))
        semrush_at = cur.fetchone()["at"]

        cur.execute(
            "SELECT MAX(completed_at) AS at FROM ai_tracking_runs WHERE store_id = %s AND status = 'completed'",
            (store_id,),
        )
        vis_at = cur.fetchone()["at"]

        cur.execute(
            "SELECT MAX(completed_at) AS at FROM competitor_check_runs WHERE store_id = %s",
            (store_id,),
        )
        comp_at = cur.fetchone()["at"]

        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS tasks_7d,
                      COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days' AND status = 'failed') AS failed_7d,
                      COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days' AND status = 'completed') AS completed_7d,
                      COUNT(*) FILTER (WHERE status IN ('pending','running')) AS running
               FROM ai_tasks WHERE store_id = %s""",
            (store_id,),
        )
        t = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS running FROM bulk_tasks WHERE store_id = %s AND status IN ('pending','running')",
            (store_id,),
        )
        bulk_running = int(cur.fetchone()["running"] or 0)

        cur.execute(
            """SELECT COALESCE(SUM(cost_estimate), 0) AS spend FROM ai_tracking_runs
               WHERE store_id = %s AND completed_at::date = CURRENT_DATE""",
            (store_id,),
        )
        spend_today = float(cur.fetchone()["spend"] or 0)
        cur.execute("SELECT daily_budget FROM ai_config ORDER BY id LIMIT 1")
        budget_row = cur.fetchone()

        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE status = 'scheduled') AS scheduled,
                      COUNT(*) FILTER (WHERE status NOT IN ('scheduled','published') AND COALESCE(status,'') NOT LIKE 'auto%%') AS drafts
               FROM news_sessions WHERE store_id = %s""",
            (store_id,),
        )
        news = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM topic_suggestions WHERE store_id = %s AND status = 'accepted'",
            (store_id,),
        )
        topics_accepted = int(cur.fetchone()["cnt"] or 0)
        cur.execute(
            """SELECT COUNT(*) AS cnt FROM synced_articles
               WHERE store_id = %s AND is_published = TRUE
                 AND published_at >= date_trunc('month', NOW())""",
            (store_id,),
        )
        published_month = int(cur.fetchone()["cnt"] or 0)
        cur.execute(
            """SELECT COUNT(*) AS cnt FROM content_gaps
               WHERE store_id = %s AND status = 'addressed'
                 AND addressed_at >= date_trunc('month', NOW())""",
            (store_id,),
        )
        gaps_closed_month = int(cur.fetchone()["cnt"] or 0)

    completed_7d = int(t["completed_7d"] or 0)
    failed_7d = int(t["failed_7d"] or 0)
    finished = completed_7d + failed_7d

    return {
        "freshness": {
            "sync": freshness_entry("sync", sync_at),
            "gsc_pull": freshness_entry("gsc_pull", gsc_at),
            "semrush_import": freshness_entry("semrush_import", semrush_at),
            "visibility_run": freshness_entry("visibility_run", vis_at),
            "competitor_check": freshness_entry("competitor_check", comp_at),
        },
        "ai_ops": {
            "tasks_7d": int(t["tasks_7d"] or 0),
            "failed_7d": failed_7d,
            "success_rate_7d": round(completed_7d / finished * 100, 1) if finished else None,
            "running": int(t["running"] or 0) + bulk_running,
            "spend_today": round(spend_today, 2),
            "daily_budget": float(budget_row["daily_budget"]) if budget_row and budget_row["daily_budget"] is not None else None,
        },
        "pipeline": {
            "news_scheduled": int(news["scheduled"] or 0),
            "news_drafts": int(news["drafts"] or 0),
            "topics_accepted": topics_accepted,
            "published_this_month": published_month,
            "gaps_closed_this_month": gaps_closed_month,
        },
        "coverage": get_coverage(store_id),
    }


def get_changes_feed(store_id, limit=30):
    """Everything that happened since the user last looked, floor 24h
    (a refresh never wipes the feed mid-read), cap 30 days."""
    from app.settings_manager import get_setting

    now = datetime.now(timezone.utc)
    last_visit = None
    raw = get_setting(f"dashboard_last_visit_{store_id}")
    if raw:
        try:
            last_visit = datetime.fromisoformat(raw)
            if last_visit.tzinfo is None:
                last_visit = last_visit.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    since = last_visit if last_visit else now - timedelta(days=7)
    since = min(since, now - timedelta(hours=24))
    since = max(since, now - timedelta(days=30))

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, event_type, severity, subject, payload, status, created_at
               FROM dashboard_events
               WHERE store_id = %s AND created_at >= %s AND status <> 'resolved'
               ORDER BY created_at DESC LIMIT %s""",
            (store_id, since.replace(tzinfo=None), limit),
        )
        rows = cur.fetchall()

    counts = {}
    items = []
    for r in rows:
        counts[r["event_type"]] = counts.get(r["event_type"], 0) + 1
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        items.append(
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "severity": r["severity"],
                "subject": r["subject"],
                "payload": payload or {},
                "created_at": r["created_at"].isoformat(),
            }
        )
    return {"since": since.isoformat(), "items": items, "counts_by_type": counts}


def mark_changes_seen(store_id):
    from app.settings_manager import set_setting
    set_setting(f"dashboard_last_visit_{store_id}", datetime.now(timezone.utc).isoformat())


def get_rank_movers(store_id, limit=10):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT keyword, position, previous_position,
                      (previous_position - position) AS change,
                      search_volume, url
               FROM semrush_keywords
               WHERE store_id = %s AND position IS NOT NULL AND previous_position IS NOT NULL
                 AND position <> previous_position
               ORDER BY ABS(previous_position - position) * GREATEST(COALESCE(search_volume, 1), 1) DESC
               LIMIT 80""",
            (store_id,),
        )
        rows = cur.fetchall()

        cur.execute(
            """SELECT keyword, competitor_domain, competitor_position, our_position, alert_type, created_at
               FROM competitor_alerts
               WHERE store_id = %s AND is_read = FALSE
               ORDER BY created_at DESC LIMIT %s""",
            (store_id, limit),
        )
        alerts = cur.fetchall()

    def fmt(r):
        return {
            "keyword": r["keyword"],
            "position": r["position"],
            "previous_position": r["previous_position"],
            "change": r["change"],
            "volume": r["search_volume"],
            "url": r["url"],
        }

    up = [fmt(r) for r in rows if r["change"] > 0][:limit]
    down = [fmt(r) for r in rows if r["change"] < 0][:limit]
    competitor_gains = [
        {
            "keyword": a["keyword"],
            "competitor": a["competitor_domain"],
            "their_position": a["competitor_position"],
            "our_position": a["our_position"],
            "alert_type": a["alert_type"],
        }
        for a in alerts
    ]
    return {"up": up, "down": down, "competitor_gains": competitor_gains}


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

        # Technical SEO issues — same scan as the Issues tab (issue_scanner is
        # the single definition), so the badge and the tab always agree.
        from app.services.issue_scanner import count_store_issues
        try:
            # 60s, not the 300s default: the badge endpoint caches its own
            # result for 60s, and the two windows used to compound into a
            # number that could lag the Issues tab by ~6 minutes.
            insights["technical_seo"] = count_store_issues(store_id, max_age=60)
        except Exception:
            logger.exception("Issue count failed for store %s", store_id)
            insights["technical_seo"] = {"items_with_issues": 0, "total_issues": 0, "fixable_issues": 0}

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
