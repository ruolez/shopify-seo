"""Expected organic CTR by position — calibrated per store from its own GSC
data, falling back to post-AI-Overviews-era industry defaults.

A static CTR table can't survive the modern SERP: AI Overviews, shopping
units and ad load vary expected CTR wildly by niche. Wherever the store has
enough impressions in a position bucket, the store's own clicks/impressions
ratio IS the expectation; sparse buckets fall back to the defaults below.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from app.database import db

logger = logging.getLogger(__name__)

# Fallback curve. Legacy 28-32% position-1 studies predate AI Overviews;
# recent ecommerce data puts organic pos-1 CTR far lower on affected queries.
# These defaults only matter for buckets the store lacks data for.
DEFAULT_CTR_CURVE = {1: 0.20, 2: 0.10, 3: 0.07, 5: 0.045, 10: 0.02, 20: 0.008, 999: 0.004}

MIN_BUCKET_IMPRESSIONS = 200

_curve_cache = {}  # store_id -> {"ts": time, "data": curve}
_CACHE_TTL = 3600


def expected_ctr_for_position(position, curve=None):
    """Expected CTR for a search position; the 999 bucket covers 21+."""
    if position is None or position <= 0:
        return 0.01
    curve = curve or DEFAULT_CTR_CURVE
    for threshold in sorted(curve.keys()):
        if position <= threshold:
            return curve[threshold]
    return DEFAULT_CTR_CURVE[999]


PAGE_ONE_TARGET = 8
TOP_THREE_TARGET = 3


def target_position(position):
    """Where a page can realistically move next: page-one pages to the top
    three, everything else onto page one."""
    return TOP_THREE_TARGET if position and 0 < position <= 10 else PAGE_ONE_TARGET


def estimate_click_gain(monthly_impressions, position, curve=None, actual_ctr=None):
    """Extra clicks a month if the page reached its target position — the one
    unit every recommendation is ranked in. Uses the page's actual CTR when
    known, else the expected CTR for where it ranks now."""
    if not monthly_impressions:
        return 0
    current = actual_ctr if actual_ctr is not None else expected_ctr_for_position(position, curve)
    gain = expected_ctr_for_position(target_position(position), curve) - current
    return max(0, round(monthly_impressions * gain))


# The single app-wide rule for "this page's CTR underperforms its position".
# Top-10 only: past page one, CTR is dominated by position, not snippet quality.
CTR_UNDERPERFORMER_MIN_IMPRESSIONS = 200
CTR_UNDERPERFORMER_MAX_POSITION = 10
CTR_UNDERPERFORMER_RATIO = 0.8


def is_ctr_underperformer(position, ctr, impressions, curve=None):
    """One definition of a CTR opportunity, shared by every engine that feeds
    the home queue — a page is either an opportunity everywhere or nowhere."""
    if not impressions or impressions < CTR_UNDERPERFORMER_MIN_IMPRESSIONS:
        return False
    if not position or position <= 0 or position > CTR_UNDERPERFORMER_MAX_POSITION:
        return False
    return (ctr or 0) < expected_ctr_for_position(position, curve) * CTR_UNDERPERFORMER_RATIO


def _bucket_for_position(pos):
    if pos <= 1.5:
        return 1
    if pos <= 2.5:
        return 2
    if pos <= 3.5:
        return 3
    if pos <= 5.5:
        return 5
    if pos <= 10.5:
        return 10
    if pos <= 20.5:
        return 20
    return 999


def get_expected_ctr_curve(store_id):
    """Store-calibrated expected-CTR curve from 28d of GSC query data.

    Per bucket: total clicks / total impressions (impressions-weighted by
    construction). Searches for the store's own name are left out: people
    looking for the store click it at rates no product page can reach, which
    made every non-brand page look like an underperformer. Buckets under
    MIN_BUCKET_IMPRESSIONS fall back to the default; the final curve is
    forced monotonically non-increasing so a sparse bucket can't invert the
    shape.
    """
    now = time.time()
    cached = _curve_cache.get(store_id)
    if cached and now - cached["ts"] < _CACHE_TTL:
        return cached["data"]

    curve = dict(DEFAULT_CTR_CURVE)
    try:
        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=30)).isoformat()
        date_to = (today - timedelta(days=3)).isoformat()
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT query, SUM(clicks) AS clicks, SUM(impressions) AS impr,
                          SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS pos
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND date >= %s AND date <= %s AND query IS NOT NULL
                   GROUP BY query
                   HAVING SUM(impressions) > 0""",
                (store_id, date_from, date_to),
            )
            rows = cur.fetchall()
            cur.execute("SELECT shop_name, primary_domain, domain FROM stores WHERE id = %s", (store_id,))
            store = cur.fetchone() or {}

        from app.services.gsc_extras import brand_labels
        from app.services.gsc_queries import brand_terms, is_branded_query
        terms = brand_terms(store.get("shop_name"), store.get("primary_domain") or store.get("domain"))
        labels = brand_labels(store_id)

        buckets = {}
        for r in rows:
            if is_branded_query(r["query"], terms, labels):
                continue
            pos = float(r["pos"] or 0)
            if pos <= 0:
                continue
            b = _bucket_for_position(pos)
            agg = buckets.setdefault(b, {"clicks": 0.0, "impr": 0.0})
            agg["clicks"] += float(r["clicks"] or 0)
            agg["impr"] += float(r["impr"] or 0)

        for b, agg in buckets.items():
            if agg["impr"] >= MIN_BUCKET_IMPRESSIONS:
                curve[b] = agg["clicks"] / agg["impr"]

        # Monotonic non-increasing walk keeps the curve shaped like a curve.
        previous = None
        for threshold in sorted(curve.keys()):
            if previous is not None and curve[threshold] > previous:
                curve[threshold] = previous
            previous = curve[threshold]
    except Exception:
        logger.warning("Failed to calibrate CTR curve for store %s", store_id)
        curve = dict(DEFAULT_CTR_CURVE)

    _curve_cache[store_id] = {"ts": now, "data": curve}
    return curve
