import json
import logging
import threading

from flask import Blueprint, jsonify, redirect, request

from app.auth import decrypt_value
from app.database import db
from app.services.ai_output_guard import sanitize_html
from app.services.task_runner import is_failed_result, spawn as spawn_task
from app.services.technical_seo_analyzer import (
    analyze_collection_technical_seo,
    analyze_product_technical_seo,
)
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

technical_seo_bp = Blueprint("technical_seo", __name__)


def _enforce_meta_limits(shopify_fields):
    """One truncation rule everywhere — delegates to the shared clamps
    (a hand-rolled copy here had already drifted from them)."""
    from app.services.ai_output_guard import clamp_meta_description, clamp_meta_title

    meta_desc = shopify_fields.get("meta_description")
    if meta_desc and isinstance(meta_desc, str):
        shopify_fields["meta_description"] = clamp_meta_description(meta_desc)
    meta_title = shopify_fields.get("meta_title")
    if meta_title and isinstance(meta_title, str):
        shopify_fields["meta_title"] = clamp_meta_title(meta_title)
    title = shopify_fields.get("title")
    if title and isinstance(title, str):
        from app.services.ai_output_guard import clamp_title
        shopify_fields["title"] = clamp_title(title)
    return shopify_fields


def _enrich_collection_data(collection_dict):
    store_id = collection_dict.get("store_id")
    title_lower = (collection_dict.get("title") or "").lower().strip()
    if not title_lower or not store_id:
        return collection_dict

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT vendor, COUNT(*) as cnt FROM synced_products "
            "WHERE store_id = %s AND vendor IS NOT NULL AND vendor != '' "
            "GROUP BY vendor ORDER BY cnt DESC",
            (store_id,),
        )
        vendors = cur.fetchall()

    for v in vendors:
        vendor_lower = v["vendor"].lower()
        if title_lower.startswith(vendor_lower + " ") or title_lower == vendor_lower:
            collection_dict["vendor"] = v["vendor"]
            remainder = title_lower[len(vendor_lower):].strip()
            if remainder:
                remainder = remainder.split(" - ")[0].split(" & ")[0].strip()
                collection_dict["product_type"] = remainder
            break

    return collection_dict


def _resolve_and_analyze(item_type, item_dict, prefetch=None):
    """Resolve demand-grounded keywords, run the technical analysis against the
    resolved primary, and fold the resolution into the analysis payload."""
    from app.services.keyword_resolver import resolve_item_keywords

    item_dict = dict(item_dict)
    if item_type == "collection":
        item_dict = _enrich_collection_data(item_dict)

    store_id = item_dict["store_id"]
    resolution = None
    try:
        resolution = resolve_item_keywords(
            store_id, item_type, item_dict["id"], item=item_dict, prefetch=prefetch
        )
        # One keyword, one page: if another page already owns this primary
        # (or a plural/word-order variant of it) with a stronger claim, walk
        # to the next-best unclaimed candidate right away instead of waiting
        # for the post-bulk dedup pass.
        if resolution and resolution.get("primary"):
            from app.services.keyword_resolver import _swap_primary_if_claimed
            _swap_primary_if_claimed(store_id, item_type, item_dict["id"], resolution)
    except Exception:
        logger.warning("Keyword resolution failed for %s %s", item_type, item_dict.get("id"))

    primary = (resolution or {}).get("primary")
    primary_kw = primary["keyword"] if primary else None
    stored_detected = None
    if primary_kw is None:
        # Resolver failed — _upsert_analysis COALESCE-keeps the previous
        # resolution, so score against ITS primary rather than a raw n-gram:
        # otherwise the stored row advertises a primary its checks were never
        # scored against (invisible split-brain). Keep the stored candidate
        # list too, since scoring against a supplied primary skips extraction.
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT keyword_resolution->'primary'->>'keyword' AS kw,
                              detected_keywords
                       FROM technical_seo_analyses
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (store_id, item_type, item_dict["id"]),
                )
                row = cur.fetchone()
            primary_kw = (row or {}).get("kw")
            stored_detected = (row or {}).get("detected_keywords")
        except Exception:
            primary_kw = None

    if item_type == "product":
        analysis = analyze_product_technical_seo(item_dict, primary_keyword=primary_kw)
    else:
        analysis = analyze_collection_technical_seo(item_dict, primary_keyword=primary_kw)

    if resolution and resolution.get("candidates"):
        analysis["detected_keywords"] = resolution["candidates"]
    elif primary_kw and stored_detected and not analysis.get("detected_keywords"):
        analysis["detected_keywords"] = stored_detected
    analysis["keyword_resolution"] = resolution
    return analysis, resolution


def _upsert_analysis(store_id, item_type, item_id, analysis, resolution):
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO technical_seo_analyses
            (store_id, item_type, item_id, score, detected_keywords, checks,
             issues_count, fixable_count, keyword_resolution, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, item_type, item_id) DO UPDATE SET
                score = EXCLUDED.score,
                detected_keywords = EXCLUDED.detected_keywords,
                checks = EXCLUDED.checks,
                issues_count = EXCLUDED.issues_count,
                fixable_count = EXCLUDED.fixable_count,
                -- NULL here means "resolver failed / no new info", never
                -- "deliberately cleared" — a transient error must not erase
                -- a previously good resolution.
                keyword_resolution = COALESCE(EXCLUDED.keyword_resolution, technical_seo_analyses.keyword_resolution),
                updated_at = NOW()
            RETURNING id""",
            (
                store_id, item_type, item_id, analysis["score"],
                json.dumps(analysis["detected_keywords"]),
                json.dumps(analysis["checks"]),
                analysis["issues_count"], analysis["fixable_count"],
                json.dumps(resolution) if resolution else None,
            ),
        )
        return cur.fetchone()["id"]


@technical_seo_bp.route("/technical-seo")
def technical_seo_page():
    # The page merged into the SEO section (Issues tab). The /api/technical-seo/*
    # endpoints below stay — they power the merged page's keyword analysis.
    return redirect("/seo?tab=issues", code=301)


# The /api/technical-seo/products and /collections list endpoints were
# removed: no UI called them since the page merged into /seo, and they
# surfaced the raw analyzer issues_count — a different number than the
# scanner-aligned counts every live surface uses.


@technical_seo_bp.route("/api/technical-seo/analyze", methods=["POST"])
def analyze_item():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_id = data.get("item_id")

    if not item_id:
        return jsonify({"error": "item_id is required"}), 400

    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
        else:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    analysis, resolution = _resolve_and_analyze(item_type, item)
    analysis["analysis_id"] = _upsert_analysis(item["store_id"], item_type, item_id, analysis, resolution)

    return jsonify(analysis)


@technical_seo_bp.route("/api/technical-seo/bulk-analyze", methods=["POST"])
def bulk_analyze():
    """Analyze many items as a background bulk task (pollable via /api/tasks/bulk/<id>).

    The previous synchronous loop blocked the request for the whole batch with
    no progress feedback and risked proxy timeouts on large selections.
    """
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_ids = data.get("item_ids", [])
    analyze_all = bool(data.get("all"))
    store_id = data.get("store_id")

    if item_type not in ("product", "collection"):
        return jsonify({"error": f"Invalid item_type: {item_type}"}), 400
    if analyze_all and not store_id:
        return jsonify({"error": "store_id is required with all=true"}), 400
    if not analyze_all and not item_ids:
        return jsonify({"error": "item_ids is required"}), 400

    table = "synced_products" if item_type == "product" else "synced_collections"
    item_col = "product_id" if item_type == "product" else "collection_id"

    with db.get_cursor() as cur:
        if analyze_all:
            cur.execute(f"SELECT id FROM {table} WHERE store_id = %s", (int(store_id),))
        else:
            cur.execute(f"SELECT id FROM {table} WHERE id = ANY(%s)", ([int(i) for i in item_ids],))
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No matching items found"}), 400

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, FALSE, 'tseo_analyze') RETURNING id",
            (len(valid_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]
        for iid in valid_ids:
            cur.execute(
                f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                (bulk_task_id, iid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_analyze, bulk_task_id, item_type)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _execute_bulk_analyze(bulk_task_id, item_type):
    item_col = "product_id" if item_type == "product" else "collection_id"
    table = "synced_products" if item_type == "product" else "synced_collections"

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))
        cur.execute(
            f"SELECT bti.id AS bti_id, bti.{item_col} AS ref_id FROM bulk_task_items bti "
            "WHERE bti.bulk_task_id = %s ORDER BY bti.id",
            (bulk_task_id,),
        )
        items = cur.fetchall()

    # Batch the store-level demand-data queries per store — a mixed-store
    # selection must never analyze store B's items with store A's demand data.
    prefetches = {}
    store_of_ref = {}
    if items:
        try:
            from app.services.keyword_resolver import build_prefetch
            ref_ids = [r["ref_id"] for r in items]
            with db.get_cursor() as cur:
                cur.execute(f"SELECT id, store_id FROM {table} WHERE id = ANY(%s)", (ref_ids,))
                store_of_ref = {r["id"]: r["store_id"] for r in cur.fetchall()}
            by_store = {}
            for rid, sid in store_of_ref.items():
                by_store.setdefault(sid, []).append(rid)
            for sid, ids in by_store.items():
                prefetches[sid] = build_prefetch(sid, item_type, ids)
        except Exception:
            logger.warning("Keyword prefetch failed for bulk task %s", bulk_task_id)

    for row in items:
        bti_id = row["bti_id"]
        ref_id = row["ref_id"]
        try:
            with db.get_cursor() as cur:
                cur.execute(f"SELECT * FROM {table} WHERE id = %s", (ref_id,))
                item = cur.fetchone()

            if not item:
                raise ValueError("Item not found")

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                    (item.get("title") or "", bulk_task_id),
                )

            analysis, resolution = _resolve_and_analyze(
                item_type, item, prefetch=prefetches.get(item["store_id"])
            )
            _upsert_analysis(item["store_id"], item_type, ref_id, analysis, resolution)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                    (bti_id,),
                )
                cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
        except Exception as e:
            logger.exception("Bulk analyze failed for %s %s", item_type, ref_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), bti_id),
                )
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

    # Ownership pass: one keyword, one page — redistribute duplicate
    # auto-primaries now that every item's candidates are fresh. Once per
    # store in the batch, not just the first item's store. Only for
    # substantial batches: the store-wide greedy pass redistributes EVERY
    # item's primary using whatever scores are stored, so a 3-item outdated
    # sweep would reshuffle 1,000 pages on stale data — small batches are
    # already guarded by the per-item _swap_primary_if_claimed above.
    if len(items) < 50:
        store_of_ref = {}
    for dedup_store_id in sorted(set(store_of_ref.values())):
        try:
            from app.services.keyword_resolver import dedupe_primary_ownership
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = 'Deduplicating keyword ownership...' WHERE id = %s",
                    (bulk_task_id,),
                )
            reassigned = dedupe_primary_ownership(dedup_store_id, item_type)
            if reassigned:
                logger.info("Keyword ownership pass reassigned %s %ss", reassigned, item_type)
        except Exception:
            logger.warning("Keyword ownership pass failed for bulk task %s", bulk_task_id)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


_BULK_SPECS = {
    "product": ("synced_products", "product_id", "t.status = 'ACTIVE'"),
    "collection": ("synced_collections", "collection_id", "t.published = TRUE"),
}


def queue_bulk_analyze(store_id, scope="outdated", item_types=("product", "collection")):
    """Create tseo_analyze bulk tasks for items needing (re-)analysis and run
    them in ONE background thread, product pass then collection pass — the two
    passes read each other's primaries when claiming ownership, so concurrent
    runs made final ownership non-deterministic.

    scope:
      'unanalyzed' — no analysis row yet
      'outdated'   — analysis predates the item's own last content change
                     (includes unanalyzed)
      'stale'      — outdated, or older than the resolution TTL
                     (rules.keyword_resolution.resolution_max_age_days)
      'all'        — everything visible
    Returns [{item_type, bulk_task_id, total}]; empty when nothing to do."""
    outdated_clause = """(tsa.id IS NULL
        OR tsa.updated_at < GREATEST(
             COALESCE(t.shopify_updated_at, '-infinity'::timestamp),
             COALESCE(t.last_optimized_at, '-infinity'::timestamp)))"""
    created = []
    pairs = []
    for item_type in item_types:
        table, item_col, visibility = _BULK_SPECS[item_type]
        with db.get_cursor() as cur:
            if scope == "all":
                cur.execute(
                    f"SELECT t.id FROM {table} t WHERE t.store_id = %s AND {visibility} ORDER BY t.id",
                    (store_id,),
                )
            elif scope == "unanalyzed":
                cur.execute(
                    f"""SELECT t.id FROM {table} t
                        LEFT JOIN technical_seo_analyses tsa
                          ON tsa.store_id = t.store_id AND tsa.item_type = %s AND tsa.item_id = t.id
                        WHERE t.store_id = %s AND {visibility} AND tsa.id IS NULL
                        ORDER BY t.id""",
                    (item_type, store_id),
                )
            elif scope == "stale":
                from app.services.keyword_resolver import _rules as _kw_rules
                max_age = int(_kw_rules().get("resolution_max_age_days", 7))
                cur.execute(
                    f"""SELECT t.id FROM {table} t
                        LEFT JOIN technical_seo_analyses tsa
                          ON tsa.store_id = t.store_id AND tsa.item_type = %s AND tsa.item_id = t.id
                        WHERE t.store_id = %s AND {visibility}
                          AND ({outdated_clause}
                               OR tsa.updated_at < NOW() - make_interval(days => %s))
                        ORDER BY t.id""",
                    (item_type, store_id, max_age),
                )
            else:  # outdated
                cur.execute(
                    f"""SELECT t.id FROM {table} t
                        LEFT JOIN technical_seo_analyses tsa
                          ON tsa.store_id = t.store_id AND tsa.item_type = %s AND tsa.item_id = t.id
                        WHERE t.store_id = %s AND {visibility} AND {outdated_clause}
                        ORDER BY t.id""",
                    (item_type, store_id),
                )
            ids = [r["id"] for r in cur.fetchall()]

        if not ids:
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, FALSE, 'tseo_analyze') RETURNING id",
                (len(ids),),
            )
            bulk_task_id = cur.fetchone()["id"]
            for iid in ids:
                cur.execute(
                    f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )
        pairs.append((bulk_task_id, item_type))
        created.append({"item_type": item_type, "bulk_task_id": bulk_task_id, "total": len(ids)})

    if pairs:
        spawn_task("bulk_tasks", pairs[0][0], _run_bulk_analyzes_serial, pairs)
    return created


def _run_bulk_analyzes_serial(pairs):
    from app.services.task_runner import finalize_failed
    for bulk_task_id, item_type in pairs:
        try:
            _execute_bulk_analyze(bulk_task_id, item_type)
        except Exception as e:
            logger.exception("Serial bulk analyze failed for task %s", bulk_task_id)
            finalize_failed("bulk_tasks", bulk_task_id, str(e))


@technical_seo_bp.route("/api/technical-seo/analysis/<item_type>/<int:item_id>", methods=["GET"])
def get_analysis(item_type, item_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM technical_seo_analyses WHERE item_type = %s AND item_id = %s",
            (item_type, item_id),
        )
        analysis = cur.fetchone()

    if not analysis:
        return jsonify({"error": "Analysis not found. Run analysis first."}), 404

    return jsonify({
        "id": analysis["id"],
        "store_id": analysis["store_id"],
        "item_type": analysis["item_type"],
        "item_id": analysis["item_id"],
        "score": analysis["score"],
        "detected_keywords": analysis["detected_keywords"],
        "checks": analysis["checks"],
        "issues_count": analysis["issues_count"],
        "fixable_count": analysis["fixable_count"],
        "ai_fixes": analysis["ai_fixes"],
        "ai_task_id": analysis["ai_task_id"],
        "created_at": analysis["created_at"].isoformat() if analysis["created_at"] else None,
        "updated_at": analysis["updated_at"].isoformat() if analysis["updated_at"] else None,
    })


_ALLOWED_FIX_FIELDS = {"meta_title", "meta_description", "title", "tags", "description", "image_alt"}

# Analyzer check keys a caller may scope a fix to. Derived from the issue
# definitions so the two surfaces can't drift; guard checks are allowed too
# because the prompt builder adds them for whichever fields are in scope.
def _allowed_fix_checks():
    from app.services.issue_scanner import FIX_GUARD_CHECKS, ISSUE_DEFS

    allowed = {c for d in ISSUE_DEFS.values() for c in (d.get("checks") or [])}
    allowed.update(c for cs in FIX_GUARD_CHECKS.values() for c in cs)
    return allowed


_ALLOWED_FIX_CHECKS = _allowed_fix_checks()


def _balanced_media_updates(item, media_updates):
    """Run the alt keyword-balance backstop for `item`'s resolved primary.

    Every path that writes alts needs this; it lives here so the single-item
    apply and the bulk fix-apply share one call shape."""
    try:
        from app.services.ai_content_validator import (
            clean_keyword,
            ensure_applied_alt_balance,
        )

        images = item.get("images") or []
        if isinstance(images, str):
            images = json.loads(images)
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                   FROM technical_seo_analyses
                   WHERE store_id = %s AND item_type = 'product' AND item_id = %s""",
                (item["store_id"], item["id"]),
            )
            row = cur.fetchone()
        return ensure_applied_alt_balance(
            images, media_updates, clean_keyword((row or {}).get("kw"))
        )
    except Exception:
        logger.exception("Alt balance backstop failed — updates passed through")
        return media_updates


def _normalize_media_updates(item, value):
    """Coerce an image_alt fix value into [{media_id, alt}] for fileUpdate.

    The model is asked for objects carrying the media_id, but bare strings
    still occur — pair those positionally with the item's images that lack
    alt text (the same order images_without_alt was presented in). Entries
    that can't be resolved to a media_id are dropped with a log line rather
    than crashing the apply.
    """
    if not isinstance(value, list):
        return []
    images = item.get("images") or []
    if isinstance(images, str):
        images = json.loads(images)
    missing_alt_ids = [img.get("media_id") for img in images
                       if not img.get("alt") and img.get("media_id")]
    valid_ids = {img.get("media_id") for img in images if img.get("media_id")}

    # Pass 1: entries that already carry a valid media_id claim it directly.
    updates = []
    positional = []
    claimed = set()
    from app.services.ai_content_validator import alt_text_of

    for entry in value:
        if isinstance(entry, dict):
            mid = entry.get("media_id")
            alt = alt_text_of(entry).strip()
            if mid in valid_ids and alt:
                updates.append({"media_id": mid, "alt": alt})
                claimed.add(mid)
                continue
            entry = alt  # unresolvable dict — pair its text positionally
        if isinstance(entry, str) and entry.strip():
            positional.append(entry.strip())

    # Pass 2: pair leftover texts positionally. Default target is the images
    # missing alt text (coverage fixes); when the text count instead matches
    # the full unclaimed image set, it's a whole-set rewrite (keyword-balance
    # fixes rewrite EXISTING alts) — pair against all images in order.
    all_ids = [img.get("media_id") for img in images if img.get("media_id")]
    available = [mid for mid in missing_alt_ids if mid not in claimed]
    all_unclaimed = [mid for mid in all_ids if mid not in claimed]
    if positional and len(positional) != len(available):
        if len(positional) == len(all_unclaimed):
            available = all_unclaimed
        elif not available and len(all_unclaimed) >= 2 and len(positional) == len(all_unclaimed) - 1:
            # Balance rewrite of just the non-hero alts ("remove the keyword
            # from the others") — the hero keeps its keyword-bearing alt.
            available = all_unclaimed[1:]
    for alt, mid in zip(positional, available):
        updates.append({"media_id": mid, "alt": alt})
    if len(positional) > len(available):
        logger.warning(
            "image_alt fix: %d proposed alt(s) had no image to attach to — dropped",
            len(positional) - len(available),
        )
    return updates


@technical_seo_bp.route("/api/technical-seo/fix", methods=["POST"])
def create_fix_task():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_id = data.get("item_id")
    only_fields = data.get("only_fields")
    only_checks = data.get("only_checks")
    fix_context = data.get("fix_context")

    if not item_id:
        return jsonify({"error": "item_id is required"}), 400
    if only_fields:
        only_fields = [f for f in only_fields if f in _ALLOWED_FIX_FIELDS]
        # An emptied filter must not silently widen a scoped fix into a
        # full-item rewrite (downstream treats falsy as "no restriction").
        if not only_fields:
            return jsonify({"error": "No valid fields in only_fields"}), 400
    if only_checks:
        if not only_fields:
            return jsonify({"error": "only_checks requires only_fields"}), 400
        only_checks = [c for c in only_checks if c in _ALLOWED_FIX_CHECKS]
        if not only_checks:
            return jsonify({"error": "No valid checks in only_checks"}), 400

    with db.get_cursor() as cur:
        table = "synced_products" if item_type == "product" else "synced_collections"
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    store_id = item["store_id"]

    # The fix task reads the stored resolution as-is, so run it through the
    # TTL/manual-supersede rules first — a fix generated from a stale or
    # manually-superseded primary writes a stale keyword to Shopify. This also
    # seeds the analysis when missing (dashboard-queue fixes) and never raises.
    from app.services.keyword_resolver import get_or_refresh_resolution
    get_or_refresh_resolution(store_id, item_type, item_id)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM technical_seo_analyses WHERE item_type = %s AND item_id = %s",
            (item_type, item_id),
        )
        analysis = cur.fetchone()
    if not analysis:
        return jsonify({"error": "Run analysis first before requesting fixes"}), 400

    task_input = {
        "item_type": item_type,
        "item_id": item_id,
        "analysis_id": analysis["id"],
    }
    if only_fields:
        task_input["only_fields"] = only_fields
    if only_checks:
        task_input["only_checks"] = only_checks
    if fix_context:
        task_input["fix_context"] = fix_context

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'technical_seo_fix', %s) RETURNING id",
            (store_id, json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

        cur.execute(
            "UPDATE technical_seo_analyses SET ai_task_id = %s WHERE id = %s",
            (task_id, analysis["id"]),
        )

    spawn_task("ai_tasks", task_id, _execute_fix_task, task_id, analysis["id"])

    return jsonify({"ok": True, "task_id": task_id}), 201


def _execute_fix_task(task_id, analysis_id):
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()

    if not task:
        return

    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))

    try:
        result = run_task(task["task_type"], task["input"], task.get("store_id"))

        _failed = is_failed_result(result)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = %s, error = %s, result = %s, completed_at = NOW() WHERE id = %s",
                ("failed" if _failed else "completed",
                 ((result.get("error") if isinstance(result, dict) else None) or "AI returned unparsable output") if _failed else None,
                 json.dumps(result), task_id),
            )

        if not _failed:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE technical_seo_analyses SET ai_fixes = %s, updated_at = NOW() WHERE id = %s",
                    (json.dumps(result), analysis_id),
                )

    except Exception as e:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


def _record_change_event(item_type, item, fixes, change_type):
    """Best-effort optimization-history event so View Impact has data for
    fixes applied outside the main SEO apply endpoints. Never blocks the fix."""
    try:
        from app.routes.seo_routes import (
            _fetch_keyword_analysis,
            _get_gsc_metrics_for_store,
            _get_semrush_metrics_for_store,
            _get_store_baselines,
        )
        from app.services.seo_analyzer import (
            analyze_collection_seo,
            analyze_product_seo,
            analyze_search_performance,
            compute_unified_score,
        )

        store_id = item["store_id"]
        analyze_fn = analyze_product_seo if item_type == "product" else analyze_collection_seo
        seo_before = analyze_fn(dict(item))
        prefix = "products" if item_type == "product" else "collections"
        path = f"{prefix}/{item.get('handle') or ''}"
        gsc = _get_gsc_metrics_for_store(store_id).get(path, {})
        sr = _get_semrush_metrics_for_store(store_id).get(path, {})
        search_before = analyze_search_performance(
            {k: gsc.get(k) for k in ("gsc_clicks", "gsc_impressions", "gsc_ctr", "gsc_position")},
            {k: sr.get(k) for k in ("semrush_volume", "semrush_position", "semrush_keyword_count")},
            _get_store_baselines(store_id),
        )
        kw = _fetch_keyword_analysis(store_id, item_type, item["id"])
        unified = compute_unified_score(seo_before, kw["score"] if kw else None, search_before)

        changes_map = {}
        for f in fixes:
            field = f.get("field")
            if field:
                changes_map[field] = {"new": str(f.get("value") or f.get("proposed_value") or "")[:200]}

        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO seo_change_events
                   (store_id, item_type, item_id, change_type, changes_applied,
                    seo_score_before, search_score_before, composite_score_before)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (store_id, item_type, item["id"], change_type,
                 json.dumps(changes_map) if changes_map else None,
                 unified["content_score"], unified.get("search_score"), unified["seo_score"]),
            )
    except Exception:
        logger.warning("Failed to record change event for %s %s", item_type, item.get("id"))


@technical_seo_bp.route("/api/technical-seo/apply", methods=["POST"])
def apply_fixes():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_id = data.get("item_id")
    fixes = data.get("fixes", [])

    if not item_id or not fixes:
        return jsonify({"error": "item_id and fixes are required"}), 400

    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute(
                """SELECT sp.*, s.domain, s.access_token_enc, s.api_version
                FROM synced_products sp
                JOIN stores s ON s.id = sp.store_id
                WHERE sp.id = %s""",
                (item_id,),
            )
        else:
            cur.execute(
                """SELECT sc.*, s.domain, s.access_token_enc, s.api_version
                FROM synced_collections sc
                JOIN stores s ON s.id = sc.store_id
                WHERE sc.id = %s""",
                (item_id,),
            )
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    if data.get("from_ai_fixes"):
        # AI-proposed fixes are single-use: applying them (or re-analyzing the
        # item) clears technical_seo_analyses.ai_fixes, so a stale browser tab
        # must not re-apply a superseded proposal against changed content.
        # Manual quick-edits don't send this flag and are unaffected.
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT ai_fixes FROM technical_seo_analyses WHERE item_type = %s AND item_id = %s",
                (item_type, item_id),
            )
            row = cur.fetchone()
        if not row or not row.get("ai_fixes"):
            return jsonify({
                "error": "These AI fixes are no longer current — the item changed "
                         "or they were already applied. Generate fixes again."
            }), 409

    try:
        client = ShopifyClient(
            item["domain"],
            decrypt_value(item["access_token_enc"]),
            item["api_version"],
        )

        updated_fields = []
        shopify_fields = {}
        media_updates = []

        for fix in fixes:
            field = fix.get("field")
            value = fix.get("value")
            # Absent means skip; explicit ""/[] means "clear this field" and
            # must go through — removing bad content is a legitimate fix.
            if not field or value is None:
                continue

            if field == "meta_title":
                shopify_fields["meta_title"] = value
                updated_fields.append("meta_title")
            elif field == "meta_description":
                shopify_fields["meta_description"] = value
                updated_fields.append("meta_description")
            elif field == "title":
                shopify_fields["title"] = value
                updated_fields.append("title")
            elif field == "tags":
                if isinstance(value, list):
                    shopify_fields["tags"] = value
                elif isinstance(value, str):
                    shopify_fields["tags"] = [t.strip() for t in value.split(",") if t.strip()]
                updated_fields.append("tags")
            elif field == "description":
                shopify_fields["description_html"] = value
                updated_fields.append("description")
            elif field == "image_alt":
                if isinstance(value, list):
                    media_updates = _normalize_media_updates(item, value)
                    if media_updates:
                        updated_fields.append("image_alt")

        # Nothing resolvable means no Shopify write — succeeding here would
        # falsely bump last_optimized_at and clear the item from queues.
        if not shopify_fields and not media_updates:
            return jsonify({"error": "No applicable fixes — nothing was resolvable to a Shopify update"}), 400

        _enforce_meta_limits(shopify_fields)
        if shopify_fields.get("description_html"):
            from app.services.ai_output_guard import restore_missing_images
            shopify_fields["description_html"] = sanitize_html(restore_missing_images(
                item.get("description"), shopify_fields["description_html"]))

        # Snapshot before-state so View Impact has history for targeted fixes
        _record_change_event(item_type, item, fixes, "technical_fix")

        if shopify_fields:
            if item_type == "product":
                client.update_product(item["shopify_id"], shopify_fields)
            else:
                client.update_collection(item["shopify_id"], shopify_fields)

        if media_updates and item_type == "product":
            # Write-time balance guard. The generation gate already ran, but
            # these alts are editable in the modal before applying, so the
            # only reliable place to check is right before the write.
            media_updates = _balanced_media_updates(item, media_updates)
            client.update_product_media(item["shopify_id"], media_updates)

        with db.get_cursor() as cur:
            if item_type == "product":
                # Presence checks, not truthiness — a blanked field pushed to
                # Shopify must also blank the local mirror.
                if "title" in shopify_fields:
                    cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (shopify_fields["title"], item_id))
                if "meta_title" in shopify_fields:
                    cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (shopify_fields["meta_title"], item_id))
                if "meta_description" in shopify_fields:
                    cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (shopify_fields["meta_description"], item_id))
                if "description_html" in shopify_fields:
                    cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (shopify_fields["description_html"], item_id))
                if "tags" in shopify_fields:
                    cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(shopify_fields["tags"]), item_id))
                if media_updates:
                    images = item.get("images") or []
                    if isinstance(images, str):
                        images = json.loads(images)
                    alt_map = {m["media_id"]: m["alt"] for m in media_updates if m.get("media_id")}
                    for img in images:
                        if img.get("media_id") in alt_map:
                            img["alt"] = alt_map[img["media_id"]]
                    cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), item_id))
                cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (item_id,))
            else:
                if "title" in shopify_fields:
                    cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (shopify_fields["title"], item_id))
                if "meta_title" in shopify_fields:
                    cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (shopify_fields["meta_title"], item_id))
                if "meta_description" in shopify_fields:
                    cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (shopify_fields["meta_description"], item_id))
                if "description_html" in shopify_fields:
                    cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (shopify_fields["description_html"], item_id))
                cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (item_id,))

        # Re-analyze after applying
        with db.get_cursor() as cur:
            if item_type == "product":
                cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
            else:
                cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
            updated_item = cur.fetchone()

        if updated_item:
            new_analysis, new_resolution = _resolve_and_analyze(item_type, updated_item)
            _upsert_analysis(updated_item["store_id"], item_type, item_id, new_analysis, new_resolution)
            with db.get_cursor() as cur:
                # Applied proposals are consumed — a stale set must not be
                # re-applicable against the now-changed content.
                cur.execute(
                    """UPDATE technical_seo_analyses SET ai_fixes = NULL
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (updated_item["store_id"], item_type, item_id),
                )

            return jsonify({
                "ok": True,
                "updated_fields": updated_fields,
                "new_score": new_analysis["score"],
                "new_issues_count": new_analysis["issues_count"],
            })

        return jsonify({"ok": True, "updated_fields": updated_fields})

    except Exception as e:
        logger.exception("Failed to apply technical SEO fixes")
        return jsonify({"error": str(e)}), 500


@technical_seo_bp.route("/api/seo/items/<item_type>/<int:item_id>/primary-keyword", methods=["POST"])
def set_primary_keyword(item_type, item_id):
    """Manually pin an item's primary keyword. Recorded as an accepted
    keyword_assignments row (the durable manual signal the resolver honors),
    then the item is re-resolved and re-analyzed."""
    from app.services.keyword_strategy_service import (
        add_manual_keyword,
        assign_keyword_to_content,
    )

    if item_type not in ("product", "collection"):
        return jsonify({"error": "item_type must be 'product' or 'collection'"}), 400

    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    keyword_id = data.get("keyword_id")
    if not keyword and not keyword_id:
        return jsonify({"error": "keyword is required"}), 400

    table = "synced_products" if item_type == "product" else "synced_collections"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()
    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    store_id = item["store_id"]
    # Callers holding a master_keywords id pass it straight through; deriving
    # the id from display text can mint a duplicate master row when the stored
    # keyword carries punctuation.
    mk_id = keyword_id or add_manual_keyword(store_id, keyword)

    # Ledger writes live in one place (the strategy service), shared with the
    # Keyword Strategy assign flow, which also serves articles and pages.
    ledger = assign_keyword_to_content(store_id, mk_id, item_type, item_id)

    analysis, resolution = _resolve_and_analyze(item_type, item)
    analysis["analysis_id"] = _upsert_analysis(store_id, item_type, item_id, analysis, resolution)

    # Dispossess previous owners: any other page whose stored primary matches
    # this keyword (by variant_key) keeps advertising it until re-resolved —
    # re-resolve them now so the manual claim walks them onto their next-best
    # candidate instead of leaving a silent shared primary.
    try:
        from app.services.keyword_resolver import variant_key
        # ledger["keyword"] is the stored master form — `keyword` is empty when
        # the caller passed keyword_id, which would silently skip dispossession.
        vk = variant_key(ledger["keyword"])
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT item_type, item_id,
                          keyword_resolution->'primary'->>'keyword' AS kw
                   FROM technical_seo_analyses
                   WHERE store_id = %s
                     AND keyword_resolution->'primary'->>'keyword' IS NOT NULL
                     AND NOT (item_type = %s AND item_id = %s)""",
                (store_id, item_type, item_id),
            )
            losers = [r for r in cur.fetchall() if variant_key(r["kw"]) == vk]
        for r in losers:
            loser_table = "synced_products" if r["item_type"] == "product" else "synced_collections"
            with db.get_cursor() as cur:
                cur.execute(f"SELECT * FROM {loser_table} WHERE id = %s", (r["item_id"],))
                loser = cur.fetchone()
            if loser:
                loser_analysis, loser_resolution = _resolve_and_analyze(r["item_type"], dict(loser))
                _upsert_analysis(store_id, r["item_type"], r["item_id"], loser_analysis, loser_resolution)
    except Exception:
        logger.warning("Dispossession re-resolve failed after manual pin for %s %s", item_type, item_id)

    # What this item stopped owning — the UI reports the actual change rather
    # than promising a coverage cell turns green (zero-volume keywords never
    # flip one).
    analysis["assigned_keyword"] = ledger["keyword"]
    analysis["superseded"] = ledger["superseded"]
    return jsonify(analysis)


@technical_seo_bp.route("/api/technical-seo/bulk-fix-apply", methods=["POST"])
def bulk_fix_apply():
    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type", "product")
    item_ids = data.get("item_ids", [])

    if not item_ids:
        return jsonify({"error": "item_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'technical_seo') RETURNING id",
            (len(item_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for iid in item_ids:
            if item_type == "product":
                cur.execute(
                    "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )
            else:
                cur.execute(
                    "INSERT INTO bulk_task_items (bulk_task_id, collection_id) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_fix_apply, bulk_task_id, item_type)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(item_ids)}), 201


def _execute_bulk_fix_apply(bulk_task_id, item_type, only_fields=None, only_checks=None):
    """Analyze → AI fix → apply for each item.

    only_fields scopes the WRITE (which Shopify fields may change); only_checks
    scopes the READ (which failing checks the AI is asked to address). Issue-type
    bulk fixes set both, so "Fix all with AI" on one issue rewrites the field for
    that issue only. Both unset = full-item fix (the Optimize path)."""
    import time
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))

        if item_type == "product":
            cur.execute(
                """SELECT bti.id as item_id, bti.product_id as ref_id,
                          sp.shopify_id, sp.title, sp.store_id,
                          s.domain, s.access_token_enc, s.api_version
                FROM bulk_task_items bti
                JOIN synced_products sp ON sp.id = bti.product_id
                JOIN stores s ON s.id = sp.store_id
                WHERE bti.bulk_task_id = %s
                ORDER BY bti.id""",
                (bulk_task_id,),
            )
        else:
            cur.execute(
                """SELECT bti.id as item_id, bti.collection_id as ref_id,
                          sc.shopify_id, sc.title, sc.store_id,
                          s.domain, s.access_token_enc, s.api_version
                FROM bulk_task_items bti
                JOIN synced_collections sc ON sc.id = bti.collection_id
                JOIN stores s ON s.id = sc.store_id
                WHERE bti.bulk_task_id = %s
                ORDER BY bti.id""",
                (bulk_task_id,),
            )
        items = cur.fetchall()

    for item in items:
        bti_id = item["item_id"]
        ref_id = item["ref_id"]

        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_task_items SET status = 'running' WHERE id = %s", (bti_id,))
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (item["title"], bulk_task_id))

        try:
            # Step 1: Analyze
            with db.get_cursor() as cur:
                if item_type == "product":
                    cur.execute("SELECT * FROM synced_products WHERE id = %s", (ref_id,))
                else:
                    cur.execute("SELECT * FROM synced_collections WHERE id = %s", (ref_id,))
                full_item = cur.fetchone()

            if not full_item:
                raise ValueError(f"{item_type} {ref_id} not found")

            analysis, resolution = _resolve_and_analyze(item_type, full_item)
            store_id = full_item["store_id"]
            analysis_id = _upsert_analysis(store_id, item_type, ref_id, analysis, resolution)

            if only_fields:
                # Re-analysis happens after the scan that queued this item, so
                # the check may already be clean — skip rather than rewrite the
                # field for some *other* issue the user didn't ask about.
                relevant_fixable = sum(
                    1 for key, c in analysis["checks"].items()
                    if c.get("issue") and c.get("fix_type") in only_fields
                    and (not only_checks or key in only_checks)
                )
            else:
                relevant_fixable = analysis["fixable_count"]
            if relevant_fixable == 0:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE id = %s",
                        (json.dumps({"skipped": True, "reason": "no fixable issues"}), bti_id),
                    )
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
                continue

            # Step 2: AI fix
            task_input = {"item_type": item_type, "item_id": ref_id, "analysis_id": analysis_id}
            if only_fields:
                task_input["only_fields"] = list(only_fields)
            if only_checks:
                task_input["only_checks"] = list(only_checks)
            ai_result = run_task("technical_seo_fix", task_input, store_id)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), bti_id),
                )

            if is_failed_result(ai_result):
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (((ai_result.get("error") if isinstance(ai_result, dict) else None) or "AI returned unparsable output"), bti_id),
                    )
                    cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
                continue

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE technical_seo_analyses SET ai_fixes = %s, updated_at = NOW() WHERE id = %s",
                    (json.dumps(ai_result), analysis_id),
                )

            # Step 3: Apply (defense in depth: never apply outside only_fields)
            item_error = None
            ai_fixes = ai_result.get("fixes", [])
            if only_fields:
                ai_fixes = [f for f in ai_fixes if f.get("field") in only_fields]
            if ai_fixes:
                try:
                    client = ShopifyClient(
                        item["domain"],
                        decrypt_value(item["access_token_enc"]),
                        item["api_version"],
                    )

                    shopify_fields = {}
                    media_updates = []
                    for fix in ai_fixes:
                        field = fix.get("field")
                        value = fix.get("proposed_value")
                        # Absent means skip; explicit "" means "clear this field".
                        if not field or value is None:
                            continue
                        if field in ("meta_title", "meta_description", "title"):
                            shopify_fields[field] = value
                        elif field == "description":
                            shopify_fields["description_html"] = value
                        elif field == "tags" and isinstance(value, list):
                            shopify_fields["tags"] = value
                        elif field == "image_alt" and isinstance(value, list):
                            media_updates = _normalize_media_updates(full_item, value)

                    # Every fix entry dropped means no Shopify write — recording
                    # success would fake an optimization and bump freshness.
                    if not shopify_fields and not media_updates:
                        item_error = "No applicable fixes — nothing was resolvable to a Shopify update"
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                                (json.dumps({"applied": False, "error": item_error}), bti_id),
                            )
                    else:
                        _enforce_meta_limits(shopify_fields)
                        if shopify_fields.get("description_html"):
                            from app.services.ai_output_guard import restore_missing_images
                            shopify_fields["description_html"] = sanitize_html(restore_missing_images(
                                full_item.get("description"), shopify_fields["description_html"]))

                        # Snapshot before-state so View Impact has history
                        _record_change_event(
                            item_type, full_item, ai_fixes,
                            "issue_fix" if only_fields else "technical_fix",
                        )

                        if shopify_fields:
                            if item_type == "product":
                                client.update_product(item["shopify_id"], shopify_fields)
                            else:
                                client.update_collection(item["shopify_id"], shopify_fields)

                        if media_updates and item_type == "product":
                            media_updates = _balanced_media_updates(full_item, media_updates)
                            client.update_product_media(item["shopify_id"], media_updates)

                        with db.get_cursor() as cur:
                            if item_type == "product":
                                for f, v in shopify_fields.items():
                                    if f == "tags" and isinstance(v, list):
                                        cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(v), ref_id))
                                    elif f == "title":
                                        cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (v, ref_id))
                                    elif f == "meta_title":
                                        cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (v, ref_id))
                                    elif f == "meta_description":
                                        cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (v, ref_id))
                                    elif f == "description_html":
                                        cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (v, ref_id))
                                if media_updates:
                                    images = full_item.get("images") or []
                                    if isinstance(images, str):
                                        images = json.loads(images)
                                    alt_map = {m["media_id"]: m["alt"] for m in media_updates if m.get("media_id")}
                                    for img in images:
                                        if img.get("media_id") in alt_map:
                                            img["alt"] = alt_map[img["media_id"]]
                                    cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), ref_id))
                                cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (ref_id,))
                            else:
                                for f, v in shopify_fields.items():
                                    if f == "title":
                                        cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (v, ref_id))
                                    elif f == "meta_title":
                                        cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (v, ref_id))
                                    elif f == "meta_description":
                                        cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (v, ref_id))
                                    elif f == "description_html":
                                        cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (v, ref_id))
                                cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (ref_id,))

                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                                (json.dumps({"applied": True}), bti_id),
                            )

                        with db.get_cursor() as cur:
                            if item_type == "product":
                                cur.execute("SELECT * FROM synced_products WHERE id = %s", (ref_id,))
                            else:
                                cur.execute("SELECT * FROM synced_collections WHERE id = %s", (ref_id,))
                            updated_item = cur.fetchone()

                        if updated_item:
                            new_analysis, new_resolution = _resolve_and_analyze(item_type, updated_item)
                            _upsert_analysis(store_id, item_type, ref_id, new_analysis, new_resolution)
                            with db.get_cursor() as cur:
                                cur.execute(
                                    """UPDATE technical_seo_analyses SET ai_fixes = NULL
                                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                                    (store_id, item_type, ref_id),
                                )

                except Exception as apply_err:
                    item_error = f"Apply to Shopify failed: {apply_err}"
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": False, "error": str(apply_err)}), bti_id),
                        )

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (item_error, bti_id),
                    )
                    cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
                else:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (bti_id,),
                    )
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))

        except Exception as e:
            logger.exception("Bulk fix-apply failed for %s %s", item_type, ref_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), bti_id),
                )
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

        time.sleep(0.5)

    # Ownership pass — bulk AI fixes rewrite titles/metas, which shifts
    # resolver scores; without this two pages could leave the batch sharing a
    # primary until the next full analyze (the analyze executor has had this
    # pass all along). Same size gate: small batches are covered by the
    # per-item swap inside each refresh.
    try:
        _item_col = "product_id" if item_type == "product" else "collection_id"
        _ref_table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(
                f"""SELECT DISTINCT t.store_id FROM bulk_task_items bti
                    JOIN {_ref_table} t ON t.id = bti.{_item_col}
                    WHERE bti.bulk_task_id = %s""",
                (bulk_task_id,),
            )
            _store_ids = [r["store_id"] for r in cur.fetchall()]
            cur.execute("SELECT total FROM bulk_tasks WHERE id = %s", (bulk_task_id,))
            _total = (cur.fetchone() or {}).get("total") or 0
        if _total >= 50:
            from app.services.keyword_resolver import dedupe_primary_ownership
            for _sid in _store_ids:
                reassigned = dedupe_primary_ownership(_sid, item_type)
                if reassigned:
                    logger.info("Post-fix ownership pass reassigned %s %ss", reassigned, item_type)
    except Exception:
        logger.warning("Post-fix ownership pass failed for bulk task %s", bulk_task_id)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


def _get_stored_analysis(store_id, item_type, item_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM technical_seo_analyses WHERE store_id = %s AND item_type = %s AND item_id = %s",
            (store_id, item_type, item_id),
        )
        return cur.fetchone()
