"""One publishing cadence and one review gate for every article the app writes.

Blog posts and Product News used to queue independently — blog on its own
interval, news on whatever dates the browser suggested, and the news "auto"
paths straight to live. Both now draw slots from one store-wide calendar, and
the schedulers publish only sessions a person has approved.
"""

from datetime import datetime, time as dt_time, timedelta, timezone

from app.database import db

DEFAULT_GAP_DAYS = 2
DEFAULT_PUBLISH_TIME = dt_time(9, 0)

SESSION_TABLES = {"blog": "article_wizard_sessions", "news": "news_sessions"}


def pick_slot(now, publish_time, gap_days, taken):
    """First publish_time slot at or after now that sits at least gap_days
    from every taken date. Naive UTC throughout."""
    taken_dates = sorted({t.date() for t in taken})
    day = now.date() if datetime.combine(now.date(), publish_time) > now else now.date() + timedelta(days=1)
    while True:
        clash = [d for d in taken_dates if abs((d - day).days) < gap_days]
        if not clash:
            return datetime.combine(day, publish_time)
        day = max(clash) + timedelta(days=gap_days)


def pick_slots(now, publish_time, gap_days, taken, count):
    taken = list(taken)
    slots = []
    for _ in range(count):
        slot = pick_slot(now, publish_time, gap_days, taken)
        slots.append(slot)
        taken.append(slot)
    return slots


def get_cadence(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT interval_days, publish_time FROM blog_schedule_settings WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()
    gap_days = int(row["interval_days"]) if row and row["interval_days"] else DEFAULT_GAP_DAYS
    publish_time = row["publish_time"] if row and row["publish_time"] else DEFAULT_PUBLISH_TIME
    return gap_days, publish_time


def _taken_slots(store_id, exclude=None):
    """Every date the calendar must keep clear: queued sessions of both kinds
    (failed ones keep their slot so a retry doesn't shift the queue) and the
    most recent publish. exclude = (kind, session_id) being re-slotted."""
    taken = []
    with db.get_cursor() as cur:
        for kind, table in SESSION_TABLES.items():
            cur.execute(
                f"""SELECT id, scheduled_for FROM {table}
                    WHERE store_id = %s AND scheduled_for IS NOT NULL AND status != 'published'
                      AND scheduled_for >= (NOW() AT TIME ZONE 'utc')""",
                (store_id,),
            )
            taken.extend(
                r["scheduled_for"] for r in cur.fetchall() if (kind, r["id"]) != exclude
            )
        cur.execute(
            "SELECT MAX(published_at) AS last FROM synced_articles WHERE store_id = %s AND is_published",
            (store_id,),
        )
        row = cur.fetchone()
    if row and row["last"]:
        taken.append(row["last"])
    return taken


def next_open_slots(store_id, count=1, exclude=None):
    gap_days, publish_time = get_cadence(store_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return pick_slots(now, publish_time, gap_days, _taken_slots(store_id, exclude), count)


def next_open_slot(store_id, exclude=None):
    return next_open_slots(store_id, 1, exclude)[0]


def approve_session(kind, session_id):
    """Mark a prepared session as reviewed by a person. An overdue session is
    moved to the next open slot so a backlog approved at once still goes out
    at the cadence. Returns the session's slot, or None if it can't be approved."""
    table = SESSION_TABLES[kind]
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT store_id, scheduled_for FROM {table}
                WHERE id = %s AND scheduled_for IS NOT NULL AND status = 'scheduled'""",
            (session_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    slot = row["scheduled_for"]
    if slot <= datetime.now(timezone.utc).replace(tzinfo=None):
        slot = next_open_slot(row["store_id"], exclude=(kind, session_id))
    with db.get_cursor() as cur:
        cur.execute(
            f"""UPDATE {table} SET reviewed_at = NOW(), scheduled_for = %s, error = NULL, updated_at = NOW()
                WHERE id = %s AND status = 'scheduled'""",
            (slot, session_id),
        )
        if cur.rowcount == 0:
            return None
    return slot


def unapprove_session(kind, session_id):
    table = SESSION_TABLES[kind]
    with db.get_cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET reviewed_at = NULL, updated_at = NOW() WHERE id = %s AND status = 'scheduled'",
            (session_id,),
        )
        return cur.rowcount > 0
