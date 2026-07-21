"""Leader-elected scheduler that publishes scheduled blog sessions when their time arrives.

Sessions are prepared at approval time by /api/blog/schedule-from-suggestion (the wizard
pipeline runs immediately, the session parks in status 'scheduled' with scheduled_for set).
Each tick claims due sessions atomically and publishes them via the wizard publish step.
"""
import logging
import threading

from app.database import db

logger = logging.getLogger(__name__)

_blog_scheduler = None


class BlogPublishScheduler:
    def __init__(self, interval_seconds=60):
        self.interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Blog publish scheduler started (every %ds)", self.interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        self._stop_event.wait(30)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("Blog publish scheduler error: %s", e)
            self._stop_event.wait(self.interval)

    def _tick(self):
        # Lazy imports — the route module imports services at call time too;
        # keeps this service importable before the blueprints are registered.
        from app.routes.blog_routes import (
            _fetch_wizard_session,
            _get_store_client,
            _wizard_step_publish,
        )

        with db.get_cursor() as cur:
            cur.execute(
                """SELECT id, store_id FROM article_wizard_sessions
                   WHERE status = 'scheduled' AND scheduled_for IS NOT NULL
                     AND scheduled_for <= (NOW() AT TIME ZONE 'utc')
                   ORDER BY store_id, scheduled_for"""
            )
            due = [dict(r) for r in cur.fetchall()]
        if not due:
            return

        by_store = {}
        for row in due:
            by_store.setdefault(row["store_id"], []).append(row["id"])

        for store_id, session_ids in by_store.items():
            store, client = _get_store_client(store_id)
            if not client:
                continue  # store deleted mid-tick; CASCADE cleans the rows up
            for sid in session_ids:
                # Atomic claim — identical guard to the publish-now endpoint,
                # so the two can never double-publish the same session.
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE article_wizard_sessions SET status = 'auto_publishing', updated_at = NOW() "
                        "WHERE id = %s AND status = 'scheduled'",
                        (sid,),
                    )
                    if cur.rowcount == 0:
                        continue
                try:
                    _wizard_step_publish(_fetch_wizard_session(sid), store, client, True)
                    logger.info("Scheduled blog session %s published", sid)
                except Exception as e:
                    logger.exception("Scheduled blog publish failed for session %s: %s", sid, e)
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE article_wizard_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                            (str(e), sid),
                        )


def init_scheduler():
    global _blog_scheduler
    if _blog_scheduler is None:
        _blog_scheduler = BlogPublishScheduler(interval_seconds=60)
        _blog_scheduler.start()
