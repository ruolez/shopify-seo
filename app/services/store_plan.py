"""Store goals and the weekly plan.

Goals: up to MAX_GOALS keyword families (master_keyword_clusters) the store
is trying to win, each with search progress over the last GOAL_WINDOW_DAYS
against the window before: clicks, impressions, impression-weighted average
position, and how many of its keywords sit in the top 10.

Weekly plan: one short list for this week, built from what the app already
knows: the top fixes from the action queue, posts scheduled or waiting for
approval, link suggestions to review, duplicate collections, and how much
was changed since Monday.
"""

from datetime import date, datetime, timedelta, timezone

from app.database import db

MAX_GOALS = 10
GOAL_WINDOW_DAYS = 28
WEEK_FIXES = 5


# ── Goals ───────────────────────────────────────────────────────────────────

def family_progress(rows):
    """Aggregate one family's query rows into {clicks, impressions, position,
    top10}. rows: [{keyword, clicks, impressions, position}] where position
    is that keyword's impression-weighted average for the window. Pure."""
    clicks = sum(r["clicks"] or 0 for r in rows)
    impressions = sum(r["impressions"] or 0 for r in rows)
    weighted = sum((r["position"] or 0) * (r["impressions"] or 0) for r in rows if r["position"])
    top10 = sum(1 for r in rows if r["position"] and r["position"] <= 10 and (r["impressions"] or 0) > 0)
    return {"clicks": round(clicks), "impressions": round(impressions),
            "position": round(weighted / impressions, 1) if impressions else None, "top10": top10}


def goal_change(now, before):
    """Deltas between two family_progress results (position: lower is
    better, so a negative delta is an improvement). Pure."""
    pos = None
    if now["position"] is not None and before["position"] is not None:
        pos = round(now["position"] - before["position"], 1)
    return {"clicks": now["clicks"] - before["clicks"], "impressions": now["impressions"] - before["impressions"],
            "position": pos, "top10": now["top10"] - before["top10"]}


def list_goals(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT g.cluster_id, c.name, c.keyword_count, c.total_volume, g.created_at
               FROM store_goals g JOIN master_keyword_clusters c ON c.id = g.cluster_id
               WHERE g.store_id = %s ORDER BY g.created_at, c.name""",
            (store_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def set_goals(store_id, cluster_ids):
    ids = list(dict.fromkeys(int(i) for i in cluster_ids))[:MAX_GOALS]
    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM master_keyword_clusters WHERE store_id = %s AND id = ANY(%s)", (store_id, ids))
        valid = [r["id"] for r in cur.fetchall()]
        cur.execute("DELETE FROM store_goals WHERE store_id = %s AND NOT (cluster_id = ANY(%s))", (store_id, valid))
        for cid in valid:
            cur.execute(
                "INSERT INTO store_goals (store_id, cluster_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (store_id, cid),
            )
    return list_goals(store_id)


def family_options(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT c.id, c.name, c.keyword_count, c.total_volume,
                      EXISTS (SELECT 1 FROM store_goals g WHERE g.store_id = c.store_id AND g.cluster_id = c.id) AS chosen
               FROM master_keyword_clusters c WHERE c.store_id = %s ORDER BY c.total_volume DESC NULLS LAST, c.name""",
            (store_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def _window_rows(store_id, cluster_ids, date_from, date_to):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT k.cluster_id, k.keyword, SUM(m.clicks) AS clicks, SUM(m.impressions) AS impressions,
                      SUM(m.position * m.impressions) / NULLIF(SUM(m.impressions), 0) AS position
               FROM master_keywords k
               JOIN gsc_daily_metrics m ON m.store_id = k.store_id AND LOWER(m.query) = k.keyword
               WHERE k.store_id = %s AND k.cluster_id = ANY(%s) AND m.date BETWEEN %s AND %s
               GROUP BY k.cluster_id, k.keyword""",
            (store_id, cluster_ids, date_from, date_to),
        )
        out = {}
        for r in cur.fetchall():
            out.setdefault(r["cluster_id"], []).append(dict(r))
        return out


def goals_progress(store_id):
    goals = list_goals(store_id)
    if not goals:
        return {"goals": [], "window_days": GOAL_WINDOW_DAYS, "max": MAX_GOALS}
    from app.services.dashboard_service import gsc_data_end_date

    end = gsc_data_end_date()
    start = end - timedelta(days=GOAL_WINDOW_DAYS - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=GOAL_WINDOW_DAYS - 1)
    ids = [g["cluster_id"] for g in goals]
    now_rows = _window_rows(store_id, ids, start, end)
    before_rows = _window_rows(store_id, ids, prev_start, prev_end)
    out = []
    for g in goals:
        now = family_progress(now_rows.get(g["cluster_id"], []))
        before = family_progress(before_rows.get(g["cluster_id"], []))
        out.append({**g, "created_at": g["created_at"].isoformat() if g.get("created_at") else None,
                    "now": now, "change": goal_change(now, before)})
    return {"goals": out, "window_days": GOAL_WINDOW_DAYS, "max": MAX_GOALS,
            "window": {"from": start.isoformat(), "to": end.isoformat()}}


# ── Weekly plan ─────────────────────────────────────────────────────────────

def week_bounds(today):
    """Monday .. Sunday of today's week. Pure."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def plan_lines(fixes, posts, links_pending, links_auto, duplicate_pairs, changed_this_week):
    """The week's to-do lines, most valuable first. Pure.
    Each line: {kind, text, href, count}."""
    lines = []
    for f in fixes:
        lines.append({"kind": "fix", "text": f["label"], "detail": f.get("detail"), "href": f.get("href"),
                      "count": f.get("gain")})
    if posts.get("waiting_approval"):
        n = posts["waiting_approval"]
        lines.append({"kind": "approve", "text": f"Approve {n} scheduled post{'s' if n != 1 else ''} so {'they' if n != 1 else 'it'} can go live",
                      "href": "/content/calendar", "count": n})
    if posts.get("this_week"):
        n = posts["this_week"]
        lines.append({"kind": "publish", "text": f"{n} post{'s' if n != 1 else ''} publish this week",
                      "href": "/content/calendar", "count": n})
    if links_pending and not links_auto:
        lines.append({"kind": "links", "text": f"Review {links_pending} suggested link{'s' if links_pending != 1 else ''} "
                      "to pages Google hasn't indexed", "href": "/gsc?tab=indexing", "count": links_pending})
    if duplicate_pairs:
        lines.append({"kind": "duplicates", "text": f"Merge or keep {duplicate_pairs} pair{'s' if duplicate_pairs != 1 else ''} "
                      "of duplicate collections", "href": "/duplicates", "count": duplicate_pairs})
    return {"lines": lines, "changed_this_week": changed_this_week}


def weekly_plan(store_id, today=None, include_fixes=True):
    """include_fixes=False skips the action queue (slow) for callers that
    already hold it and add the top fixes themselves."""
    from app.services.collection_duplicates import find_duplicate_collections
    from app.services.dashboard_service import build_action_queue
    from app.services.publish_gate import SESSION_TABLES
    from app.services.unindexed_links import auto_settings

    today = today or datetime.now(timezone.utc).date()
    monday, sunday = week_bounds(today)

    queue = build_action_queue(store_id, WEEK_FIXES) if include_fixes else {}
    fixes = [{"label": it.get("title") or "Fix", "detail": it.get("detail"),
              "href": it.get("href"), "gain": it.get("est_clicks_gain")}
             for it in (queue.get("items") or [])[:WEEK_FIXES]]

    posts = {"this_week": 0, "waiting_approval": 0}
    with db.get_cursor() as cur:
        for table in SESSION_TABLES.values():
            cur.execute(
                f"""SELECT COUNT(*) FILTER (WHERE reviewed_at IS NOT NULL) AS approved,
                           COUNT(*) FILTER (WHERE reviewed_at IS NULL) AS waiting
                    FROM {table} WHERE store_id = %s AND status = 'scheduled'
                      AND scheduled_for::date BETWEEN %s AND %s""",
                (store_id, monday, sunday),
            )
            r = cur.fetchone()
            posts["this_week"] += r["approved"] or 0
            posts["waiting_approval"] += r["waiting"] or 0
        cur.execute("SELECT COUNT(*) AS n FROM link_suggestions WHERE store_id = %s AND status = 'pending'", (store_id,))
        links_pending = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM seo_change_events WHERE store_id = %s AND created_at::date >= %s AND reverted_at IS NULL",
            (store_id, monday),
        )
        changed = cur.fetchone()["n"]
    try:
        duplicate_pairs = len(find_duplicate_collections(store_id))
    except Exception:
        duplicate_pairs = 0
    plan = plan_lines(fixes, posts, links_pending, auto_settings()["auto"], duplicate_pairs, changed)
    plan.update({"week": {"from": monday.isoformat(), "to": sunday.isoformat()},
                 "queue_failed": queue.get("failed_sources") or []})
    return plan
