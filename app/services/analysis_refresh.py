"""Refresh an item's stored technical analysis after its content changes.

Apply paths mutate the exact fields the analyzers score; without a refresh,
the Issues tab, dashboard counts, priority queue and keyword coverage keep
reporting problems that were already fixed. The refresh also invalidates
cached AI fix proposals (`technical_seo_analyses.ai_fixes`), which were
generated against the pre-change content and must not be applied afterwards.
"""

import logging

from app.database import db

logger = logging.getLogger(__name__)


def refresh_item_analysis(store_id, item_type, item_id, clear_ai_fixes=True):
    """Re-resolve + re-analyze one product/collection and persist the result.

    Never raises — an apply must not fail because its bookkeeping did.
    Returns the new analysis dict, or None when skipped/failed.
    """
    if item_type not in ("product", "collection") or not item_id:
        return None
    try:
        from app.routes.technical_seo_routes import _resolve_and_analyze, _upsert_analysis

        table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s AND store_id = %s", (item_id, store_id))
            item = cur.fetchone()
        if not item:
            return None
        analysis, resolution = _resolve_and_analyze(item_type, dict(item))
        _upsert_analysis(store_id, item_type, item_id, analysis, resolution)
        if clear_ai_fixes:
            with db.get_cursor() as cur:
                cur.execute(
                    """UPDATE technical_seo_analyses SET ai_fixes = NULL
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (store_id, item_type, item_id),
                )
        # Bust derived caches: the in-process issue scan (badges kept showing
        # just-fixed issues for up to 5 minutes) and the SERP page-crawl cache
        # for this item's URLs (a re-optimize within 24h would benchmark
        # against the pre-change page).
        try:
            from app.services.issue_scanner import invalidate_scan_cache
            invalidate_scan_cache(store_id)
        except Exception:
            pass
        try:
            from app.services.serp_scraper import evict_page_cache
            handle = item.get("handle")
            if handle:
                with db.get_cursor() as cur:
                    cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (store_id,))
                    store = cur.fetchone() or {}
                prefix = "products" if item_type == "product" else "collections"
                urls = []
                for host in {store.get("domain"), store.get("primary_domain")}:
                    if host:
                        h = host.replace("https://", "").replace("http://", "").rstrip("/")
                        urls.append(f"https://{h}/{prefix}/{handle}")
                evict_page_cache(urls)
        except Exception:
            pass
        try:
            from app.routes.ai_seo_routes import rescore_stored_item
            rescore_stored_item(store_id, item_type, item_id)
        except Exception:
            logger.warning("AI readiness rescore failed for %s %s", item_type, item_id)
        return analysis
    except Exception:
        logger.warning("Post-apply analysis refresh failed for %s %s", item_type, item_id)
        return None
