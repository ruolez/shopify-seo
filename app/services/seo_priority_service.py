import logging
from datetime import datetime, timedelta, timezone

from app.database import db
from app.services.ctr_benchmarks import estimate_click_gain, get_expected_ctr_curve, is_ctr_underperformer
from app.services.keyword_resolver import get_owned_primary_keywords, variant_key
from app.services.seo_analyzer import (
    _expected_ctr,
    analyze_collection_seo,
    analyze_product_seo,
    analyze_search_performance,
    compute_unified_score,
)

logger = logging.getLogger(__name__)


def _get_gsc_metrics_for_store(store_id):
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
        if not cred or not cred.get("gsc_property"):
            return {}

        # Query-free page totals: the query-level table drops anonymised
        # queries and undercounted these pages' clicks by about half.
        cur.execute(
            """SELECT page,
                      SUM(clicks) as clicks,
                      SUM(impressions) as impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
               FROM gsc_page_daily
               WHERE store_id = %s AND date >= %s AND date <= %s AND NOT is_fresh
               GROUP BY page
               HAVING SUM(impressions) > 0""",
            (store_id, date_from, date_to),
        )
        rows = cur.fetchall()

    from app.services.url_utils import url_path_key

    metrics = {}
    for r in rows:
        path = url_path_key(r["page"])
        m = metrics.setdefault(path, {"clicks": 0.0, "impressions": 0.0, "pos_w": 0.0})
        m["clicks"] += float(r["clicks"] or 0)
        m["impressions"] += float(r["impressions"] or 0)
        m["pos_w"] += float(r["position"] or 0) * float(r["impressions"] or 0)
    return {
        path: {
            "gsc_clicks": m["clicks"],
            "gsc_impressions": m["impressions"],
            "gsc_ctr": round(m["clicks"] / m["impressions"], 4) if m["impressions"] else 0,
            "gsc_position": round(m["pos_w"] / m["impressions"], 1) if m["impressions"] else 0,
        }
        for path, m in metrics.items()
    }


def _get_semrush_metrics_for_store(store_id):
    from app.services.semrush_import_service import get_semrush_metrics_for_store
    return get_semrush_metrics_for_store(store_id)


def _striking_band():
    """[lo, hi] from rules.keyword_resolution.opportunity.striking_band — the
    setting existed but this module hardcoded 4-20 next to it."""
    try:
        from app.prompt_defaults import DEFAULTS
        from app.settings_manager import get_setting
        rules = get_setting("rules.keyword_resolution", DEFAULTS["rules.keyword_resolution"])
        band = (rules.get("opportunity") or {}).get("striking_band") or [4, 20]
        return int(band[0]), int(band[1])
    except Exception:
        return 4, 20


def _get_striking_keywords_for_url(store_id, url_path, base_url=""):
    from app.services.url_utils import url_path_key

    target = url_path_key(url_path)
    if not target:
        return []
    handle = target.rsplit("/", 1)[-1]
    lo, hi = _striking_band()
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT keyword, position, search_volume, url
               FROM semrush_keywords
               WHERE store_id = %s AND url ILIKE %s
                 AND position >= %s AND position <= %s
                 AND search_volume IS NOT NULL AND search_volume > 0
               ORDER BY search_volume DESC""",
            (store_id, f"%{handle}%", lo, hi),
        )
        rows = cur.fetchall()
    # ILIKE only narrows the scan; the exact-path compare is what stops
    # products/x from inheriting products/x-large's keywords — the same
    # substring bug already fixed in the cannibalization checker below.
    return [r for r in rows if url_path_key(r["url"]) == target][:10]


def compute_unified_item_score(seo, kw_score, gsc, sr, ctr_curve):
    """Blend a content analysis with GSC/Semrush metrics into the unified
    3-pillar score — single composition shared by the priority queue and
    score-history snapshots."""
    search_perf = analyze_search_performance(
        {
            "gsc_clicks": gsc.get("gsc_clicks"),
            "gsc_impressions": gsc.get("gsc_impressions"),
            "gsc_ctr": gsc.get("gsc_ctr"),
            "gsc_position": gsc.get("gsc_position"),
        },
        {
            "semrush_volume": sr.get("semrush_volume"),
            "semrush_position": sr.get("semrush_position"),
            "semrush_keyword_count": sr.get("semrush_keyword_count"),
        },
        {"ctr_curve": ctr_curve},
    )
    return compute_unified_score(seo, kw_score, search_perf)


def compute_priority_score(item, gsc_data, semrush_data, striking_keywords, ctr_curve=None):
    """Compute 0-100 priority score. Higher = more urgent to optimize."""
    score = 0.0

    impressions = float(gsc_data.get("gsc_impressions") or 0)
    ctr = float(gsc_data.get("gsc_ctr") or 0)
    position = float(gsc_data.get("gsc_position") or 0)

    if impressions > 0 and position > 0:
        expected = _expected_ctr(position, ctr_curve)
        ctr_gap = max(expected - ctr, 0)
        if expected > 0:
            gap_ratio = ctr_gap / expected
            impression_factor = min(impressions / 1000, 1.0)
            score += gap_ratio * impression_factor * 35

    if striking_keywords:
        striking_volume = sum(int(k.get("search_volume") or 0) for k in striking_keywords)
        score += min(striking_volume / 5000, 1.0) * 30

    on_page_score = item.get("on_page_score", item.get("seo_score", 50))
    if on_page_score < 50:
        score += 20
    elif on_page_score < 70:
        score += 10

    if impressions > 0 and not item.get("last_optimized_at"):
        score += 15

    return min(round(score), 100)


def _check_keyword_cannibalization(store_id, keyword, this_path):
    """Check if a keyword is already owned by another page on the store."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()
    kw_lower = keyword.lower()

    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT page, SUM(clicks) as clicks, SUM(impressions) as impressions,
                          SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND LOWER(query) = %s AND date >= %s AND date <= %s
                   GROUP BY page
                   HAVING SUM(impressions) > 0
                   ORDER BY SUM(clicks) DESC""",
                (store_id, kw_lower, date_from, date_to),
            )
            pages = cur.fetchall()

        for p in pages:
            page_path = (p["page"] or "").rstrip("/")
            # Exact-URL compare — substring matching treated products/x-large
            # as "self" when checking products/x, hiding real cannibalization.
            if this_path and page_path != this_path.rstrip("/"):
                pos = round(float(p["position"] or 0), 1)
                if pos <= 10:
                    return {"competing_url": p["page"], "position": pos, "impressions": int(float(p["impressions"] or 0))}
    except Exception:
        pass
    return None


def _within_measuring_window(last_optimized_at, days):
    """True while a recent optimization is still being re-measured by Google —
    GSC/Semrush data predating the change would just re-flag the item."""
    if not last_optimized_at or not days:
        return False
    if last_optimized_at.tzinfo:
        now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - last_optimized_at) < timedelta(days=days)


def get_priority_ranked_items(store_id, item_type="product", limit=50, measuring_days=28,
                              split_measuring=False):
    gsc_metrics = _get_gsc_metrics_for_store(store_id)
    semrush_metrics = _get_semrush_metrics_for_store(store_id)
    ctr_curve = get_expected_ctr_curve(store_id)
    from app.services.impact_service import measured_items
    measured = measured_items(store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
    from app.services.url_utils import gsc_base_url
    base_url = gsc_base_url(cred["gsc_property"] if cred else "").rstrip("/")

    if item_type == "product":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT * FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' ORDER BY title",
                (store_id,),
            )
            items = cur.fetchall()
        analyze_fn = analyze_product_seo
        prefix = "products"
    else:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT * FROM synced_collections WHERE store_id = %s ORDER BY title",
                (store_id,),
            )
            items = cur.fetchall()
        analyze_fn = analyze_collection_seo
        prefix = "collections"

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT item_id, score, keyword_resolution->'primary' AS primary
               FROM technical_seo_analyses WHERE store_id = %s AND item_type = %s""",
            (store_id, item_type),
        )
        analysis_rows = cur.fetchall()
    kw_scores = {r["item_id"]: r["score"] for r in analysis_rows}
    resolved_primaries = {
        r["item_id"]: r["primary"]
        for r in analysis_rows
        if r["primary"] and r["primary"].get("keyword")
    }
    ownership = get_owned_primary_keywords(store_id)

    _owner_titles = {}

    def _owner_title(owner):
        okey = (owner["item_type"], owner["item_id"])
        if okey not in _owner_titles:
            table = "synced_products" if owner["item_type"] == "product" else "synced_collections"
            try:
                with db.get_cursor() as cur:
                    cur.execute(f"SELECT title FROM {table} WHERE id = %s", (owner["item_id"],))
                    row = cur.fetchone()
                _owner_titles[okey] = (row or {}).get("title") or f"{owner['item_type']} #{owner['item_id']}"
            except Exception:
                _owner_titles[okey] = f"{owner['item_type']} #{owner['item_id']}"
        return _owner_titles[okey]

    # Outstanding rule-based issues per item. The measuring window exists
    # because GSC needs ~28 days to re-measure a change — that reasoning only
    # applies to SEARCH-driven work. On-page issues are measurable right now,
    # so an item that still has them must not be silenced (an alt-text-only
    # optimize used to hide a product whose meta description was still missing).
    try:
        from app.services.issue_scanner import fixable_issue_counts
        issue_counts = fixable_issue_counts(store_id)
    except Exception:
        logger.warning("Priority ranking: issue counts unavailable", exc_info=True)
        issue_counts = {}

    results = []
    measuring_results = []
    for item in items:
        # Freshly optimized items are demoted while Google re-measures —
        # re-flagging them off pre-change data is noise. Demoted, not hidden:
        # a catalog-wide bulk optimize must not blank the whole queue.
        # ...unless the impact tracker already has a verdict for the change:
        # then Google has re-measured and the hold has done its job.
        recently_optimized = (
            _within_measuring_window(item.get("last_optimized_at"), measuring_days)
            and (item_type, item["id"]) not in measured
        )
        open_issues = issue_counts.get((item_type, item["id"]), 0)
        # Still broken -> stays actionable, but the recommendation below is
        # rewritten to the on-page work, never a keyword play off stale data.
        in_measuring_window = recently_optimized and not open_issues

        handle = item.get("handle") or ""
        path = f"{prefix}/{handle}"

        gsc = gsc_metrics.get(path, {})
        sr = semrush_metrics.get(path, {})

        seo = analyze_fn(dict(item))
        unified = compute_unified_item_score(seo, kw_scores.get(item["id"]), gsc, sr, ctr_curve)
        composite = {
            "composite_score": unified["unified_score"],
            "on_page_score": unified["content_score"],
            "search_score": unified["search_score"],
        }

        striking = _get_striking_keywords_for_url(store_id, path, base_url)

        priority = compute_priority_score(
            {
                "on_page_score": composite["on_page_score"],
                "last_optimized_at": item.get("last_optimized_at"),
            },
            gsc,
            sr,
            striking,
            ctr_curve=ctr_curve,
        )

        if priority <= 0 and not gsc and not sr:
            continue

        top_opp = ""
        recommended_action = ""
        cannibalization_warning = None
        action_kind = None
        target_keyword = None
        target_keyword_position = None
        target_keyword_volume = None
        primary_review_suggested = False
        keyword_unresolved = False

        resolved = resolved_primaries.get(item["id"])
        # variant_key throughout this branch — the claim identity. Mixing _norm
        # here let a plural/word-order variant of the page's own primary be
        # recommended "against" itself as a supporting opportunity.
        resolved_kw_key = variant_key(resolved["keyword"]) if resolved else None

        if striking:
            own_key = (item_type, item["id"])

            def _foreign_owner(kw):
                o = ownership.get(variant_key(kw))
                return o if o and (o["item_type"], o["item_id"]) != own_key else None

            resolved_striking = next(
                (k for k in striking if resolved_kw_key and variant_key(k["keyword"]) == resolved_kw_key),
                None,
            )
            unowned = [k for k in striking
                       if variant_key(k["keyword"]) != resolved_kw_key and not _foreign_owner(k["keyword"])]

            best = None
            if resolved_striking:
                best = resolved_striking
                pos = float(best.get("position") or 0)
                recommended_action = (f"Optimize for \"{best['keyword']}\" — resolved primary in "
                                      f"striking distance at position {pos:.0f}")
            elif resolved_kw_key:
                # Queue and optimizer must agree: recommend the resolved
                # primary; unowned striking keywords are supporting evidence.
                target_keyword = resolved["keyword"]
                target_keyword_position = resolved.get("gsc_position") or resolved.get("semrush_position")
                target_keyword_volume = resolved.get("volume")
                action_kind = "striking_distance"
                support = unowned[0] if unowned else None
                if support:
                    s_vol = int(support.get("search_volume") or 0)
                    s_pos = float(support.get("position") or 0)
                    top_opp = f"{support['keyword']} (vol: {s_vol:,}, pos: {s_pos:.0f})"
                    recommended_action = (
                        f"Optimize for \"{resolved['keyword']}\" (supporting opportunity: "
                        f"\"{support['keyword']}\" pos {s_pos:.0f}, vol {s_vol:,})"
                    )
                    # Floor of 50: with a zero-volume resolved primary (common
                    # for long-tail product picks) 3×0 made ANY support volume
                    # trigger the flag.
                    if s_vol >= 50 and s_vol > 3 * int(resolved.get("volume") or 0):
                        primary_review_suggested = True
                else:
                    recommended_action = f"Optimize for \"{resolved['keyword']}\" — your resolved primary keyword"
            elif unowned:
                # No stored resolution — this is a raw Semrush suggestion that
                # never went through the resolver's vetting. Say so.
                best = unowned[0]
                pos = float(best.get("position") or 0)
                recommended_action = (
                    f"Optimize for \"{best['keyword']}\" (suggestion — not yet vetted by keyword "
                    f"analysis) — striking distance at position {pos:.0f}"
                )
                keyword_unresolved = True
            else:
                # Every striking keyword belongs to another page — recommending
                # any of them would manufacture cannibalization.
                owner = _foreign_owner(striking[0]["keyword"])
                if owner:
                    cannibalization_warning = (
                        f"Warning: \"{striking[0]['keyword']}\" is the primary keyword of "
                        f"\"{_owner_title(owner)}\". Optimizing this page for it would cause "
                        f"cannibalization. Assign a more specific keyword instead."
                    )

            if best is not None:
                vol = int(best.get("search_volume") or 0)
                pos = float(best.get("position") or 0)
                top_opp = f"{best['keyword']} (vol: {vol:,}, pos: {pos:.0f})"
                action_kind = "striking_distance"
                target_keyword = best["keyword"]
                target_keyword_position = pos
                target_keyword_volume = vol

                # The resolved primary already passed the resolver's own
                # cannibalization checks — don't warn the user off it.
                cannibal = None
                if variant_key(best["keyword"]) != resolved_kw_key:
                    # Full URL — GSC pages are absolute; the exact compare in
                    # the checker needs matching shapes.
                    cannibal = _check_keyword_cannibalization(store_id, best["keyword"], f"{base_url}/{path}" if base_url else path)
                if cannibal:
                    cannibalization_warning = (
                        f"Warning: \"{best['keyword']}\" already ranks on {cannibal['competing_url']} "
                        f"at position {cannibal['position']}. Optimizing this page for the same keyword "
                        f"may cause cannibalization. Consider a long-tail variation instead."
                    )

        if not recommended_action:
            ctr_val = float(gsc.get("gsc_ctr") or 0)
            pos_val = float(gsc.get("gsc_position") or 0)
            impressions_val = float(gsc.get("gsc_impressions") or 0)
            # Shared rule — the home queue must agree with the CTR-opportunities
            # engine on what counts as an underperformer.
            if is_ctr_underperformer(pos_val, ctr_val, impressions_val, ctr_curve):
                expected = _expected_ctr(pos_val, ctr_curve)
                recommended_action = f"Improve CTR — {ctr_val:.1%} vs {expected:.1%} expected at position {pos_val:.1f}"
                action_kind = "improve_ctr"

        if not recommended_action and composite["on_page_score"] < 50:
            recommended_action = "Improve on-page SEO fundamentals (meta title, description, content)"
            action_kind = "onpage_fundamentals"

        if recently_optimized and open_issues:
            # Kept in the queue despite the recent optimize, so say why and
            # point at the work that is actually measurable today. The keyword
            # target is dropped deliberately: its GSC/Semrush position predates
            # the change, so re-optimizing for it would chase stale data.
            recommended_action = (
                f"Fix {open_issues} outstanding on-page issue"
                f"{'s' if open_issues != 1 else ''} — recently optimized, so search "
                "data is still re-measuring"
            )
            action_kind = "onpage_issues"
            target_keyword = None
            target_keyword_position = None
            target_keyword_volume = None

        if not recommended_action:
            # Ranked in on priority score alone: no striking keyword, not a CTR
            # underperformer, on-page score too high for the fundamentals
            # branch. An explicit kind — callers used to see a blank and
            # substitute a label the engine never produced.
            action_kind = "unspecified"

        # Same unit as every other queue row: extra clicks a month if each
        # striking keyword reached its target position, on the store's own
        # CTR curve (was a flat 5% / 2% of search volume).
        potential_clicks = sum(
            estimate_click_gain(int(k.get("search_volume") or 0), float(k.get("position") or 0), ctr_curve)
            for k in (striking or [])[:5]
        )

        result_entry = {
            "id": item["id"],
            "title": item["title"],
            "handle": handle,
            "item_type": item_type,
            "priority_score": priority,
            "seo_score": composite["composite_score"],
            "on_page_score": composite["on_page_score"],
            "search_score": composite["search_score"],
            "top_opportunity": top_opp,
            "recommended_action": recommended_action,
            "action_kind": action_kind,
            "target_keyword": target_keyword,
            "target_keyword_position": target_keyword_position,
            "target_keyword_volume": target_keyword_volume,
            "potential_clicks_gain": potential_clicks,
            "gsc_clicks": gsc.get("gsc_clicks"),
            "gsc_impressions": gsc.get("gsc_impressions"),
            "gsc_ctr": gsc.get("gsc_ctr"),
            "gsc_position": gsc.get("gsc_position"),
            "striking_keywords_count": len(striking),
            "last_optimized_at": item.get("last_optimized_at").isoformat() if item.get("last_optimized_at") else None,
        }
        if keyword_unresolved:
            result_entry["keyword_unresolved"] = True
        if cannibalization_warning:
            result_entry["cannibalization_warning"] = cannibalization_warning
        if resolved:
            result_entry["resolved_primary"] = {
                "keyword": resolved.get("keyword"),
                "manual": bool(resolved.get("manual")),
            }
        if primary_review_suggested:
            result_entry["primary_review_suggested"] = True
        if in_measuring_window:
            result_entry["measuring"] = True
            measuring_results.append(result_entry)
        else:
            results.append(result_entry)

    results.sort(key=lambda x: x["priority_score"], reverse=True)
    measuring_results.sort(key=lambda x: x["priority_score"], reverse=True)
    if split_measuring:
        # Caller renders re-measuring items separately (no action buttons) —
        # a page applied to Shopify is "done" until GSC re-measures it.
        return results[:limit], measuring_results[:limit]
    if len(results) < limit:
        results.extend(measuring_results[: limit - len(results)])
    return results[:limit]


# The sidebar SEO badge, the home action queue and the Priorities tab all
# answer "what SEO work is queued?" — they must answer it identically, so the
# selection lives here once. Everything else derives from it.
QUEUED_PRIORITY_LIMIT = 10

# Shown wherever an item ranked in without a diagnosis. Never sent to the AI as
# a priority focus — it describes the absence of an instruction, not one.
UNSPECIFIED_ACTION_LABEL = "Queued on score — no specific opportunity identified"


def priority_action_key(item):
    """Stable key for dismiss/snooze, matching the home queue's action_key."""
    return f"priority:{item['item_type']}:{item['id']}"


def get_queued_priorities(store_id, dismissed_keys=None, limit=QUEUED_PRIORITY_LIMIT):
    """(queued, measuring) — the actionable priority work for a store.

    Top `limit` by priority score across BOTH item types, with re-measuring
    items split off (a page applied to Shopify is done until GSC re-measures
    it) and dismissed/snoozed keys removed. Dismissals are applied BEFORE the
    truncation so the count always equals the number of items returned."""
    actionable, measuring = [], []
    for item_type in ("product", "collection"):
        act, meas = get_priority_ranked_items(
            store_id, item_type=item_type, limit=limit, split_measuring=True
        )
        actionable.extend(act)
        measuring.extend(meas)

    if dismissed_keys is None:
        # Local import: dashboard_service imports this module.
        from app.services.dashboard_service import _dismissed_action_keys

        dismissed_keys = _dismissed_action_keys(store_id)
    actionable = [p for p in actionable if priority_action_key(p) not in dismissed_keys]

    actionable.sort(key=lambda p: p["priority_score"], reverse=True)
    measuring.sort(key=lambda p: p["priority_score"], reverse=True)
    return actionable[:limit], measuring
