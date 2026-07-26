"""In-place updates to a stored interlinking analysis after apply actions.

Applying a fix used to delete the whole analysis row, which blanked the page,
the dashboard card, and AI recommendations until a multi-minute re-analysis.
These helpers instead patch the JSONB blob under a row lock — approximately
but honestly — and mark it stale so the UI can offer a re-analysis for exact
counts.
"""

import json
import logging

from app.database import db
from app.services.interlinking_analyzer import (
    compute_interlinking_score,
    normalize_href_path,
)

logger = logging.getLogger(__name__)

_TRACKED_TYPES = ("product", "collection", "article")

_DETAIL_LIST_BY_TYPE = {
    "product": "product_details",
    "collection": "collection_details",
    "article": "article_details",
}

_ORPHAN_LIST_BY_TYPE = {
    "product": "orphan_products",
    "collection": "orphan_collections",
    "article": "orphan_articles",
}


def _recount_summary(analysis):
    s = analysis.get("summary") or {}
    s["orphan_products_count"] = len(analysis.get("orphan_products") or [])
    s["orphan_collections_count"] = len(analysis.get("orphan_collections") or [])
    s["orphan_articles_count"] = len(analysis.get("orphan_articles") or [])
    s["under_linked_count"] = len(analysis.get("under_linked") or [])
    s["not_in_collection_count"] = len(analysis.get("not_in_collection") or [])
    s["missing_crosslinks_count"] = len(analysis.get("missing_crosslinks") or [])
    s["broken_links_count"] = len(analysis.get("broken_links") or [])
    s["non_canonical_count"] = len(analysis.get("non_canonical_links") or [])
    analysis["summary"] = s
    return s


def patch_analysis(store_id, mutate):
    """Apply mutate(analysis) to the stored blob under a row lock, recompute
    summary counts, and flag the analysis stale. Returns
    {"summary": ..., "stale": True} or None when no analysis exists or the
    patch failed (callers treat None as "nothing to report")."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s FOR UPDATE",
                (store_id,),
            )
            row = cur.fetchone()
            if not row or not row["analysis_data"]:
                return None
            analysis = row["analysis_data"]
            mutate(analysis)
            summary = _recount_summary(analysis)
            analysis["stale"] = True
            cur.execute(
                "UPDATE interlinking_analysis SET analysis_data = %s WHERE store_id = %s",
                (json.dumps(analysis), store_id),
            )
    except Exception:
        logger.exception("Interlinking analysis patch failed for store %s", store_id)
        return None
    return {"summary": summary, "stale": True}


def _matches_ref(entry, item_type, handle, blog_handle, prefix):
    if entry.get(f"{prefix}_type") != item_type or entry.get(f"{prefix}_handle") != handle:
        return False
    if item_type == "article" and blog_handle:
        entry_blog = entry.get(f"{prefix}_blog_handle")
        if entry_blog and entry_blog != blog_handle:
            return False
    return True


def _detail_matches(detail, item_type, handle, blog_handle):
    if detail.get("handle") != handle:
        return False
    if item_type == "article" and blog_handle and detail.get("blog_handle") != blog_handle:
        return False
    return True


def record_link_added(store_id, from_ref, to_ref, anchor_text=""):
    """Register a newly injected link. from_ref/to_ref are dicts with keys
    type, handle, and optionally blog_handle/title. Bumps the target's
    incoming count everywhere it appears, rescores it, drops it from the
    orphan/under-linked buckets it no longer belongs to, and updates
    missing-crosslink pair flags."""
    f_type, f_handle = from_ref["type"], from_ref["handle"]
    t_type, t_handle = to_ref["type"], to_ref["handle"]
    f_blog = from_ref.get("blog_handle")
    t_blog = to_ref.get("blog_handle")

    def mutate(analysis):
        entry = {
            "from_type": f_type,
            "from_handle": f_handle,
            "from_title": from_ref.get("title") or f_handle,
            "to_type": t_type,
            "to_handle": t_handle,
            "to_href": to_ref.get("href", ""),
            "anchor_text": anchor_text,
            "non_canonical": False,
        }
        if f_blog:
            entry["from_blog_handle"] = f_blog
        if t_blog:
            entry["to_blog_handle"] = t_blog
        if isinstance(analysis.get("all_links"), list):
            analysis["all_links"].append(entry)

        if t_type in _TRACKED_TYPES:
            new_incoming = None
            for list_key in (
                _DETAIL_LIST_BY_TYPE[t_type],
                _ORPHAN_LIST_BY_TYPE[t_type],
                "under_linked",
                "not_in_collection",
            ):
                if list_key == "not_in_collection" and t_type != "product":
                    continue
                for detail in analysis.get(list_key) or []:
                    if list_key == "under_linked" and detail.get("item_type", "product") != t_type:
                        continue
                    if not _detail_matches(detail, t_type, t_handle, t_blog):
                        continue
                    detail["incoming_count"] = (detail.get("incoming_count") or 0) + 1
                    if new_incoming is None:
                        new_incoming = detail["incoming_count"]
                    in_collection = (
                        detail.get("in_collection", False) if t_type == "product" else t_type == "collection"
                    )
                    detail["score"] = compute_interlinking_score(detail["incoming_count"], in_collection)
            if new_incoming is None:
                new_incoming = 1

            orphan_key = _ORPHAN_LIST_BY_TYPE[t_type]
            analysis[orphan_key] = [
                d for d in analysis.get(orphan_key) or []
                if not _detail_matches(d, t_type, t_handle, t_blog)
            ]
            if new_incoming >= 2:
                analysis["under_linked"] = [
                    d for d in analysis.get("under_linked") or []
                    if d.get("item_type", "product") != t_type
                    or not _detail_matches(d, t_type, t_handle, t_blog)
                ]

        if f_type in _TRACKED_TYPES:
            for list_key in (_DETAIL_LIST_BY_TYPE[f_type], _ORPHAN_LIST_BY_TYPE[f_type], "under_linked"):
                for detail in analysis.get(list_key) or []:
                    if list_key == "under_linked" and detail.get("item_type", "product") != f_type:
                        continue
                    if _detail_matches(detail, f_type, f_handle, f_blog):
                        detail["outgoing_count"] = (detail.get("outgoing_count") or 0) + 1

        if f_type == "collection" and t_type == "collection":
            kept = []
            for pair in analysis.get("missing_crosslinks") or []:
                if pair.get("collection_a_handle") == f_handle and pair.get("collection_b_handle") == t_handle:
                    pair["a_links_to_b"] = True
                elif pair.get("collection_a_handle") == t_handle and pair.get("collection_b_handle") == f_handle:
                    pair["b_links_to_a"] = True
                if not (pair.get("a_links_to_b") and pair.get("b_links_to_a")):
                    kept.append(pair)
            analysis["missing_crosslinks"] = kept

    return patch_analysis(store_id, mutate)


def _remove_href_entries(store_id, list_key, broken_href, from_type, from_handle, from_blog_handle):
    target = normalize_href_path(broken_href)

    def mutate(analysis):
        def keep(entry):
            if normalize_href_path(entry.get("to_href") or "") != target:
                return True
            return not _matches_ref(entry, from_type, from_handle, from_blog_handle, "from")

        analysis[list_key] = [e for e in analysis.get(list_key) or [] if keep(e)]

    return patch_analysis(store_id, mutate)


def record_broken_fixed(store_id, broken_href, from_type, from_handle, from_blog_handle=None):
    """Remove broken-link entries for one source page after a replace/remove."""
    return _remove_href_entries(
        store_id, "broken_links", broken_href, from_type, from_handle, from_blog_handle
    )


def record_noncanonical_fixed(store_id, href, from_type, from_handle, from_blog_handle=None):
    """Remove non-canonical entries for one source page after canonicalizing."""
    return _remove_href_entries(
        store_id, "non_canonical_links", href, from_type, from_handle, from_blog_handle
    )
