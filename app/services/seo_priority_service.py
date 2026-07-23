import logging
from datetime import datetime, timedelta, timezone

from app.database import db
from app.services.ctr_benchmarks import get_expected_ctr_curve
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


def get_priority_ranked_items(store_id, item_type="product", limit=50):
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
            "SELECT item_id, score FROM technical_seo_analyses WHERE store_id = %s AND item_type = %s",
            (store_id, item_type),
        )
        kw_scores = {r["item_id"]: r["score"] for r in cur.fetchall()}

    results = []
    for item in items:
        handle = item.get("handle") or ""
        path = f"{prefix}/{handle}"

        gsc = gsc_metrics.get(path, {})
        sr = semrush_metrics.get(path, {})

        seo = analyze_fn(dict(item))

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
        unified = compute_unified_score(seo, kw_scores.get(item["id"]), search_perf)
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
        if striking:
            best = striking[0]
            vol = int(best.get("search_volume") or 0)
            pos = float(best.get("position") or 0)
            top_opp = f"{best['keyword']} (vol: {vol:,}, pos: {pos:.0f})"
            recommended_action = f"Optimize for \"{best['keyword']}\" — striking distance at position {pos:.0f}"

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

        if not recommended_action and composite["on_page_score"] < 50:
            recommended_action = "Improve on-page SEO fundamentals (meta title, description, content)"

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
        results.append(result_entry)

    results.sort(key=lambda x: x["priority_score"], reverse=True)
    return results[:limit]
