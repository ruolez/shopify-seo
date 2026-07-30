import logging
from datetime import datetime, timedelta, timezone

from app.database import db
from app.services.ctr_benchmarks import get_expected_ctr_curve
from app.services.keyword_resolver import _norm, get_owned_primary_keywords
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

        cur.execute(
            """SELECT page,
                      SUM(clicks) as clicks,
                      SUM(impressions) as impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY page
               HAVING SUM(impressions) > 0""",
            (store_id, date_from, date_to),
        )
        rows = cur.fetchall()

    base_url = cred["gsc_property"].rstrip("/")
    metrics = {}
    for r in rows:
        path = r["page"].replace(base_url, "").strip("/")
        metrics[path] = {
            "gsc_clicks": float(r["clicks"] or 0),
            "gsc_impressions": float(r["impressions"] or 0),
            "gsc_ctr": round(float(r["ctr"] or 0), 4),
            "gsc_position": round(float(r["position"] or 0), 1),
        }
    return metrics


def _get_semrush_metrics_for_store(store_id):
    from app.services.semrush_import_service import get_semrush_metrics_for_store
    return get_semrush_metrics_for_store(store_id)


def _get_striking_keywords_for_url(store_id, url_path, base_url=""):
    full_url_patterns = [url_path]
    if base_url:
        full_url_patterns.append(f"{base_url}/{url_path}")

    with db.get_cursor() as cur:
        placeholders = " OR ".join(["url ILIKE %s"] * len(full_url_patterns))
        params = [f"%{p}%" for p in full_url_patterns]
        cur.execute(
            f"""SELECT keyword, position, search_volume
               FROM semrush_keywords
               WHERE store_id = %s AND ({placeholders})
                 AND position >= 4 AND position <= 20
                 AND search_volume IS NOT NULL AND search_volume > 0
               ORDER BY search_volume DESC
               LIMIT 10""",
            [store_id] + params,
        )
        return cur.fetchall()


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
            if this_path and this_path not in page_path:
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

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
    base_url = (cred["gsc_property"] or "").rstrip("/") if cred else ""

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

    results = []
    measuring_results = []
    for item in items:
        # Freshly optimized items are demoted while Google re-measures —
        # re-flagging them off pre-change data is noise. Demoted, not hidden:
        # a catalog-wide bulk optimize must not blank the whole queue.
        in_measuring_window = _within_measuring_window(item.get("last_optimized_at"), measuring_days)

        handle = item.get("handle") or ""
        path = f"{prefix}/{handle}"

        gsc = gsc_metrics.get(path, {})
        sr = semrush_metrics.get(path, {})

        seo = analyze_fn(dict(item))
        unified = compute_unified_item_score(seo, kw_scores.get(item["id"]), gsc, sr, ctr_curve)
        composite = {
            "composite_score": unified["seo_score"],
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
        keyword_aligned = False
        primary_review_suggested = False

        resolved = resolved_primaries.get(item["id"])
        resolved_kw_norm = _norm(resolved["keyword"]) if resolved else None

        if striking:
            own_key = (item_type, item["id"])

            def _foreign_owner(kw):
                o = ownership.get(_norm(kw))
                return o if o and (o["item_type"], o["item_id"]) != own_key else None

            resolved_striking = next(
                (k for k in striking if resolved_kw_norm and _norm(k["keyword"]) == resolved_kw_norm),
                None,
            )
            unowned = [k for k in striking
                       if _norm(k["keyword"]) != resolved_kw_norm and not _foreign_owner(k["keyword"])]

            best = None
            if resolved_striking:
                best = resolved_striking
                pos = float(best.get("position") or 0)
                recommended_action = (f"Optimize for \"{best['keyword']}\" — resolved primary in "
                                      f"striking distance at position {pos:.0f}")
                keyword_aligned = True
            elif resolved_kw_norm:
                # Queue and optimizer must agree: recommend the resolved
                # primary; unowned striking keywords are supporting evidence.
                target_keyword = resolved["keyword"]
                target_keyword_position = resolved.get("gsc_position") or resolved.get("semrush_position")
                target_keyword_volume = resolved.get("volume")
                action_kind = "striking_distance"
                keyword_aligned = True
                support = unowned[0] if unowned else None
                if support:
                    s_vol = int(support.get("search_volume") or 0)
                    s_pos = float(support.get("position") or 0)
                    top_opp = f"{support['keyword']} (vol: {s_vol:,}, pos: {s_pos:.0f})"
                    recommended_action = (
                        f"Optimize for \"{resolved['keyword']}\" (supporting opportunity: "
                        f"\"{support['keyword']}\" pos {s_pos:.0f}, vol {s_vol:,})"
                    )
                    if s_vol > 3 * int(resolved.get("volume") or 0):
                        primary_review_suggested = True
                else:
                    recommended_action = f"Optimize for \"{resolved['keyword']}\" — your resolved primary keyword"
            elif unowned:
                best = unowned[0]
                pos = float(best.get("position") or 0)
                recommended_action = f"Optimize for \"{best['keyword']}\" — striking distance at position {pos:.0f}"
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
                if _norm(best["keyword"]) != resolved_kw_norm:
                    cannibal = _check_keyword_cannibalization(store_id, best["keyword"], path)
                if cannibal:
                    cannibalization_warning = (
                        f"Warning: \"{best['keyword']}\" already ranks on {cannibal['competing_url']} "
                        f"at position {cannibal['position']}. Optimizing this page for the same keyword "
                        f"may cause cannibalization. Consider a long-tail variation instead."
                    )

        if not recommended_action and float(gsc.get("gsc_impressions") or 0) > 50:
            ctr_val = float(gsc.get("gsc_ctr") or 0)
            pos_val = float(gsc.get("gsc_position") or 0)
            expected = _expected_ctr(pos_val, ctr_curve)
            if ctr_val < expected * 0.6:
                recommended_action = f"Improve CTR — {ctr_val:.1%} vs {expected:.1%} expected at position {pos_val:.1f}"
                action_kind = "improve_ctr"

        if not recommended_action and composite["on_page_score"] < 50:
            recommended_action = "Improve on-page SEO fundamentals (meta title, description, content)"
            action_kind = "onpage_fundamentals"

        potential_clicks = 0
        if striking:
            for k in striking[:5]:
                vol = int(k.get("search_volume") or 0)
                pos = float(k.get("position") or 0)
                if pos <= 10:
                    potential_clicks += int(vol * 0.05)
                else:
                    potential_clicks += int(vol * 0.02)

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
        if cannibalization_warning:
            result_entry["cannibalization_warning"] = cannibalization_warning
        if resolved:
            result_entry["resolved_primary"] = {
                "keyword": resolved.get("keyword"),
                "manual": bool(resolved.get("manual")),
            }
        if keyword_aligned:
            result_entry["keyword_aligned"] = True
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
