"""Leader-elected sweeper enforcing the keyword-resolution TTL.

The TTL (rules.keyword_resolution.resolution_max_age_days, default 7) used to
be enforced only lazily when an AI task happened to read a resolution — pages
never touched by an AI flow kept arbitrarily old resolutions, while the
priority queue, coverage, and score snapshots all read stored rows with no
refresh. This sweeper re-analyzes stale/outdated analyses per store on a
schedule, so stored rows are never older than the TTL by construction (which
is also what makes those stored-read consumers honest).

Runs through queue_bulk_analyze: visible in Activity as ordinary bulk tasks,
product pass before collection pass (ownership claiming reads across types —
the passes must never run concurrently).
"""

import logging
import threading

from app.database import db

logger = logging.getLogger(__name__)

_sweeper = None


class ResolutionSweepScheduler:
    def __init__(self, interval_hours=6):
        self.interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Resolution TTL sweep scheduler started (every %d hours)", self.interval // 3600)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        # Let boot settle before the first pass.
        self._stop_event.wait(120)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("Resolution sweep error: %s", e)
            self._stop_event.wait(self.interval)

    def _tick(self):
        from app.routes.technical_seo_routes import queue_bulk_analyze

        # Never overlap another keyword-analysis run (user-triggered, post-
        # sync, or a previous tick) — concurrent passes race the ownership
        # ledger. Next tick picks the work up.
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT 1 FROM bulk_tasks WHERE task_type = 'tseo_analyze' "
                "AND status IN ('pending', 'running') LIMIT 1"
            )
            if cur.fetchone():
                logger.info("Resolution sweep skipped — a keyword analysis task is already active")
                return
            cur.execute("SELECT id FROM stores ORDER BY id")
            store_ids = [r["id"] for r in cur.fetchall()]

        for sid in store_ids:
            try:
                queued = queue_bulk_analyze(sid, scope="stale")
                if queued:
                    logger.info("Resolution TTL sweep queued for store %s: %s", sid, queued)
            except Exception:
                logger.exception("Resolution sweep failed for store %s", sid)


def init_scheduler():
    global _sweeper
    if _sweeper is None:
        _sweeper = ResolutionSweepScheduler(interval_hours=6)
        _sweeper.start()
