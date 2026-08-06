"""Shared plumbing for background task threads.

Guarantees every DB-tracked job reaches a terminal status even when its
executor crashes before its own error handling (or the worker is restarted),
and provides the stale-job sweeper used by polling endpoints.
"""

import logging
import threading
import time

from app.database import db

logger = logging.getLogger(__name__)

_TERMINAL = ("completed", "failed", "completed_with_errors")

# bulk_tasks has no error column; item-level errors live on bulk_task_items.
_HAS_ERROR_COLUMN = {
    "ai_tasks": True,
    "bulk_tasks": False,
    "ai_tracking_runs": True,
    "keyword_strategy_runs": True,
    "offsite_runs": True,
}


def is_failed_result(result):
    """run_task signals failure by returning {'error': ...} or a bare
    {'raw_response': ...} (unparsable LLM output) instead of raising."""
    if not isinstance(result, dict):
        return True
    if result.get("error"):
        return True
    if "raw_response" in result and len(result) == 1:
        return True
    return False


def finalize_failed(table, task_id, message):
    """Mark a task row failed (public: executors bailing early — e.g. store
    deleted mid-flight — must not leave the row completed or running)."""
    return _finalize_failed(table, task_id, message)


def _finalize_failed(table, task_id, message):
    if table not in _HAS_ERROR_COLUMN:
        raise ValueError(f"Unknown task table: {table}")
    try:
        with db.get_cursor() as cur:
            if _HAS_ERROR_COLUMN[table]:
                cur.execute(
                    f"UPDATE {table} SET status='failed', error=%s, completed_at=NOW() "
                    f"WHERE id=%s AND status NOT IN %s",
                    ((message or "")[:2000], task_id, _TERMINAL),
                )
            else:
                cur.execute(
                    f"UPDATE {table} SET status='failed', completed_at=NOW() "
                    f"WHERE id=%s AND status NOT IN %s",
                    (task_id, _TERMINAL),
                )
    except Exception:
        logger.exception("Could not finalize failed %s #%s", table, task_id)


def spawn(table, task_id, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) in a daemon thread. If it raises anywhere the
    executor's own handling doesn't cover, the task row is marked failed
    instead of sitting in 'running' forever."""
    if table not in _HAS_ERROR_COLUMN:
        raise ValueError(f"Unknown task table: {table}")

    def _wrapped():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.exception("Background executor for %s #%s crashed", table, task_id)
            _finalize_failed(table, task_id, str(e))

    thread = threading.Thread(target=_wrapped, daemon=True)
    thread.start()
    return thread


_last_sweep = 0.0
_sweep_lock = threading.Lock()


def sweep_stale_jobs(min_interval_seconds=60):
    """Fail any background job that's been 'running' far longer than possible —
    catches daemon threads killed by a worker restart. Cheap; called from
    polling endpoints, self-throttled to once per min_interval_seconds."""
    global _last_sweep
    with _sweep_lock:
        now = time.monotonic()
        if now - _last_sweep < min_interval_seconds:
            return
        _last_sweep = now
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status='failed', completed_at=NOW() "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '6 hours'"
            )
            cur.execute(
                "UPDATE ai_tracking_runs SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE ai_tasks SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE keyword_strategy_runs SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE offsite_runs SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE gsc_data_pulls SET status='failed', error_message=COALESCE(error_message,'Interrupted'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE sync_jobs SET status='failed', error=COALESCE(error,'Interrupted'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '3 hours'"
            )
            cur.execute(
                "UPDATE semrush_imports SET status='failed', error_message=COALESCE(error_message,'Interrupted') "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '1 hour'"
            )
    except Exception:
        logger.exception("Stale-job sweep failed")
