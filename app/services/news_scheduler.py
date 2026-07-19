"""Leader-elected scheduler that publishes news sessions when their scheduled time arrives.

Sessions are prepared at upload time by /api/news/schedule-batch (AI pipeline runs
immediately, session parks in status 'scheduled' with scheduled_for set). Each tick
claims due sessions atomically and publishes them via the existing publish step.
"""
import logging
import threading

from app.database import db

logger = logging.getLogger(__name__)

_news_scheduler = None


class NewsPublishScheduler:
    def __init__(self, interval_seconds=60):
        self.interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("News publish scheduler started (every %ds)", self.interval)

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
                logger.error("News publish scheduler error: %s", e)
            self._stop_event.wait(self.interval)

    def _tick(self):
        # Lazy imports — the route module imports services at call time too;
        # keeps this service importable before the blueprints are registered.
        from app.routes.blog_routes import _get_store_client
        from app.routes.news_routes import (
            _fetch_session,
            _regen_blog_landings_async,
            _regen_news_hub_async,
            _step_publish,
        )

        with db.get_cursor() as cur:
            cur.execute(
                """SELECT id, store_id FROM news_sessions
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
            published_any = False
            for sid in session_ids:
                # Atomic claim — identical guard to the publish-now endpoint,
                # so the two can never double-publish the same session.
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE news_sessions SET status = 'auto_publishing', updated_at = NOW() "
                        "WHERE id = %s AND status = 'scheduled'",
                        (sid,),
                    )
                    if cur.rowcount == 0:
                        continue
                try:
                    _step_publish(_fetch_session(sid), store, client, True, regen_hub=False)
                    published_any = True
                    logger.info("Scheduled news session %s published", sid)
                except Exception as e:
                    logger.exception("Scheduled publish failed for session %s: %s", sid, e)
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE news_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                            (str(e), sid),
                        )
            if published_any:
                _regen_news_hub_async(store_id, client, store)
                _regen_blog_landings_async(store_id, client, store)


def init_scheduler():
    global _news_scheduler
    if _news_scheduler is None:
        _news_scheduler = NewsPublishScheduler(interval_seconds=60)
        _news_scheduler.start()
