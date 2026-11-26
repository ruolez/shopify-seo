"""Whether a page's clicks changed after an edit, beyond day-to-day noise.

Clicks are counts, so their natural noise is Poisson: a page with 3 clicks a
fortnight can easily show 1 or 6 with nothing changed. A change counts only
when the per-day rate moved by more than both FLAT_THRESHOLD_PCT of the
baseline and Z standard errors of the difference."""
import math

FLAT_THRESHOLD_PCT = 10
Z = 2.0


def impact_verdict(pre_clicks, pre_days, post_clicks, post_days):
    """Returns (status, change_pct): status is improved | declined | flat;
    change_pct is the per-day change against the baseline, None when the
    baseline had no clicks."""
    if pre_days <= 0 or post_days <= 0:
        raise ValueError("windows must have at least one day")
    pre_rate = pre_clicks / pre_days
    post_rate = post_clicks / post_days
    diff = post_rate - pre_rate
    noise = Z * math.sqrt(pre_clicks / pre_days ** 2 + post_clicks / post_days ** 2)
    change_pct = round(diff / pre_rate * 100, 1) if pre_rate else None
    material = abs(diff) > noise and (change_pct is None or abs(change_pct) > FLAT_THRESHOLD_PCT)
    if not material:
        return "flat", change_pct
    return ("improved" if diff > 0 else "declined"), change_pct
