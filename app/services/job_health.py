"""Last outcome of every unattended job, so a failure shows up in the app
instead of only in the container log."""
import logging

from app.database import db

logger = logging.getLogger(__name__)


def record_job(job, store_id=None, ok=True, error=None, detail=None):
    """Upsert the latest run of one job. Never raises: health bookkeeping
    must not break the job it describes."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO job_health (job, store_id, last_status, last_error, last_detail, last_run_at, last_ok_at)
                   VALUES (%s, %s, %s, %s, %s, NOW(), CASE WHEN %s THEN NOW() END)
                   ON CONFLICT (job, store_id) DO UPDATE SET
                       last_status = EXCLUDED.last_status,
                       last_error = EXCLUDED.last_error,
                       last_detail = EXCLUDED.last_detail,
                       last_run_at = NOW(),
                       last_ok_at = COALESCE(EXCLUDED.last_ok_at, job_health.last_ok_at)""",
                (job, store_id or 0, "ok" if ok else "failed",
                 None if ok else str(error)[:1000], (str(detail)[:500] if detail else None), ok),
            )
    except Exception:
        logger.exception("Could not record health for job %s", job)


def run_recorded(job, store_id, fn, *args, **kwargs):
    """Call fn, record the outcome, and return (ok, result)."""
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        logger.exception("Job %s failed for store %s", job, store_id)
        record_job(job, store_id, ok=False, error=e)
        return False, None
    record_job(job, store_id, ok=True, detail=result if isinstance(result, (str, int)) else None)
    return True, result


# job -> (label, hours without a success before it counts as overdue; None = never)
JOB_LABELS = {
    "gsc_pull": ("Search Console data pull", 48),
    "gsc_extras": ("Search Console brand split and Lens data", None),
    "unindexed_links": ("Links to unindexed pages", 72),
    "keyword_consolidation": ("Keyword list refresh", 9 * 24),
    "cannibalization_verify": ("Cannibalization fix check", None),
    "daily_snapshot": ("Daily score snapshot", 30),
    "activity_mirror": ("Activity feed", None),
    "backfill_score_history": ("Score history", None),
    "nightly_scans": ("Nightly drop and gap scans", 30),
    "weekly_visibility_check": ("Weekly AI visibility run", None),
    "index_coverage": ("Google indexing check", 72),
    "sitemap_check": ("Sitemap in Search Console", None),
    "order_pull": ("Order import (revenue)", None),
    "blog_scheduler": ("Blog publishing schedule", None),
    "news_scheduler": ("News publishing schedule", None),
    "store_sync": ("Store sync", 30),
    "price_text": ("Prices in product text", None),
    "pages_sync": ("Pages sync", None),
    "indexnow": ("IndexNow (Bing) notifications", None),
    "competitor_check": ("Competitor check", None),
}


def job_problems(rows, now):
    """Jobs whose last run failed, or that have not succeeded for longer
    than their limit. Pure. rows: dicts with job, last_status, last_error,
    last_run_at, last_ok_at (naive UTC datetimes)."""
    problems = []
    for r in rows:
        label, overdue_hours = JOB_LABELS.get(r["job"], (r["job"].replace("_", " ").capitalize(), None))
        if r["last_status"] in ("failed", "warning"):
            problems.append({"job": r["job"], "label": label, "kind": r["last_status"],
                             "at": r["last_run_at"], "error": r.get("last_error") or ""})
            continue
        last_ok = r.get("last_ok_at")
        if overdue_hours and last_ok and (now - last_ok).total_seconds() > overdue_hours * 3600:
            problems.append({"job": r["job"], "label": label, "kind": "overdue",
                             "at": last_ok, "error": ""})
    rank = {"failed": 0, "warning": 1, "overdue": 2}
    return sorted(problems, key=lambda p: (rank[p["kind"]], p["label"]))


def store_job_problems(store_id):
    """job_problems for one store, from job_health plus the store sync table
    (its progress overlay already reads sync_jobs)."""
    from datetime import datetime, timezone

    rows = []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT job, last_status, last_error, last_run_at, last_ok_at FROM job_health
               WHERE store_id IN (%s, 0)""",
            (store_id,),
        )
        rows.extend(dict(r) for r in cur.fetchall())
        cur.execute(
            """SELECT status, error, COALESCE(completed_at, started_at) AS at FROM sync_jobs
               WHERE store_id = %s AND kind = 'store' ORDER BY started_at DESC LIMIT 1""",
            (store_id,),
        )
        sync = cur.fetchone()
        nightly = next((r for r in rows if r["job"] == "store_sync"), None)
        newer = sync and (not nightly or not nightly.get("last_run_at") or sync["at"] > nightly["last_run_at"])
        if newer and (sync["status"] == "failed" or sync["error"]):
            rows = [r for r in rows if r["job"] != "store_sync"]
            rows.append({"job": "store_sync", "last_status": "failed" if sync["status"] == "failed" else "warning",
                         "last_run_at": sync["at"],
                         "last_error": sync["error"] or "Sync failed", "last_ok_at": None})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return job_problems(rows, now)
