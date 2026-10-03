import json
import logging
import threading
import time

from flask import Blueprint, jsonify, request

from app.ai_agent import run_task
from app.auth import decrypt_value
from app.database import db
from app.routes.schema_routes import generate_and_push_product_schema
from app.services.ai_output_guard import (
    clamp_meta_description,
    clamp_meta_title,
    clamp_title,
    sanitize_html,
)
from app.services.apply_pipeline import apply_item_changes, record_side_change
from app.services.task_runner import is_failed_result, spawn as spawn_task, sweep_stale_jobs
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

task_bp = Blueprint("tasks", __name__)


def _execute_task(task_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()

    if not task:
        return

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,)
        )

    try:
        result = run_task(task["task_type"], task["input"], task.get("store_id"))
        status = "failed" if is_failed_result(result) else "completed"
        error = (result.get("error") or "AI returned unparsable output") if status == "failed" else None
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = %s, error = %s, result = %s, completed_at = NOW() WHERE id = %s",
                (status, error, json.dumps(result), task_id),
            )
    except Exception as e:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@task_bp.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.get_json(silent=True) or {}
    task_type = (data.get("task_type") or "").strip()
    store_id = data.get("store_id")
    task_input = data.get("input", {})

    if not task_type:
        return jsonify({"error": "task_type is required"}), 400

    # Ownership guard at the single write boundary: every retarget path
    # (keyword chips, deep links, work sessions) funnels through here, so a
    # keyword another page owns is rejected before any AI work starts.
    target_keyword = (task_input.get("target_keyword") or "").strip() if isinstance(task_input, dict) else ""
    if target_keyword and task_type in ("product_seo", "collection_seo"):
        item_type = "product" if task_type == "product_seo" else "collection"
        item_id = task_input.get("product_id") if item_type == "product" else task_input.get("collection_id")
        guard_store_id = store_id
        if not guard_store_id and item_id:
            table = "synced_products" if item_type == "product" else "synced_collections"
            with db.get_cursor() as cur:
                cur.execute(f"SELECT store_id FROM {table} WHERE id = %s", (item_id,))
                row = cur.fetchone()
                guard_store_id = row["store_id"] if row else None
        if guard_store_id and item_id:
            from app.services.keyword_resolver import keyword_ownership_guard
            try:
                _, conflict = keyword_ownership_guard(guard_store_id, item_type, int(item_id), target_keyword)
            except Exception:
                logger.exception("Keyword safety check failed for %s %s", item_type, item_id)
                return jsonify({"error": "Keyword safety check failed — try again"}), 502
            if conflict:
                return jsonify({"error": conflict}), 409

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, %s, %s) RETURNING id",
            (store_id, task_type, json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

    spawn_task("ai_tasks", task_id, _execute_task, task_id)

    return jsonify({"ok": True, "task_id": task_id}), 201


@task_bp.route("/api/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    sweep_stale_jobs()
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()

    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify(
        {
            "id": task["id"],
            "store_id": task["store_id"],
            "task_type": task["task_type"],
            "status": task["status"],
            "input": task["input"],
            "result": task["result"],
            "error": task["error"],
            "created_at": task["created_at"].isoformat() if task["created_at"] else None,
            "completed_at": task["completed_at"].isoformat() if task["completed_at"] else None,
        }
    )


@task_bp.route("/api/tasks/keyword-analysis", methods=["POST"])
def create_keyword_analysis_task():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")

    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'keyword_analysis', %s) RETURNING id",
            (store_id, json.dumps({"store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    spawn_task("ai_tasks", task_id, _execute_keyword_analysis, task_id, store_id)

    return jsonify({"ok": True, "task_id": task_id}), 201


def _execute_keyword_analysis(task_id, store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,)
        )

    try:
        result = run_task("keyword_analysis", {"store_id": store_id}, store_id)

        status = "failed" if is_failed_result(result) else "completed"
        error = (result.get("error") or "AI returned unparsable output") if status == "failed" else None
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = %s, error = %s, result = %s, completed_at = NOW() WHERE id = %s",
                (status, error, json.dumps(result), task_id),
            )

        if "error" not in result and "raw_response" not in result:
            taxonomy = {
                "categories": result.get("categories", []),
                "global_tags": result.get("global_tags", []),
                "summary": result.get("summary", ""),
            }
            assignments = result.get("product_assignments", [])
            product_count = len(assignments)

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO keyword_taxonomies (store_id, taxonomy, product_assignments, product_count, status, completed_at)
                    VALUES (%s, %s, %s, %s, 'completed', NOW())
                    ON CONFLICT (store_id) DO UPDATE SET
                        taxonomy = EXCLUDED.taxonomy,
                        product_assignments = EXCLUDED.product_assignments,
                        product_count = EXCLUDED.product_count,
                        status = EXCLUDED.status,
                        completed_at = EXCLUDED.completed_at""",
                    (store_id, json.dumps(taxonomy), json.dumps(assignments), product_count),
                )

    except Exception as e:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@task_bp.route("/api/keyword-taxonomy/<int:store_id>", methods=["GET"])
def get_keyword_taxonomy(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM keyword_taxonomies WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "taxonomy": row["taxonomy"],
        "product_assignments": row["product_assignments"],
        "product_count": row["product_count"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    })


@task_bp.route("/api/tasks/bulk", methods=["POST"])
def create_bulk_task():
    data = request.get_json(silent=True) or {}
    product_ids = data.get("product_ids", [])
    auto_apply = data.get("auto_apply", True)
    skip_tags = data.get("skip_tags", True)
    skip_title = data.get("skip_title", False)

    if not product_ids:
        return jsonify({"error": "product_ids is required"}), 400

    with db.get_cursor() as cur:
        # Validate up front — unknown ids would be enqueued but never processed
        # (executor JOIN drops them), leaving completed+failed < total forever.
        cur.execute("SELECT id FROM synced_products WHERE id = ANY(%s)", ([int(i) for i in product_ids],))
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No matching products found"}), 400

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, skip_tags, skip_title) VALUES ('pending', %s, %s, %s, %s) RETURNING id",
            (len(valid_ids), auto_apply, skip_tags, skip_title),
        )
        bulk_task_id = cur.fetchone()["id"]

        for pid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                (bulk_task_id, pid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_task, bulk_task_id)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


@task_bp.route("/api/tasks/bulk/<int:bulk_task_id>", methods=["GET"])
def get_bulk_task(bulk_task_id):
    sweep_stale_jobs()
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM bulk_tasks WHERE id = %s", (bulk_task_id,))
        bulk_task = cur.fetchone()

    if not bulk_task:
        return jsonify({"error": "Bulk task not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT bti.*,
                   COALESCE(sp.title, sc.title) as item_title
            FROM bulk_task_items bti
            LEFT JOIN synced_products sp ON sp.id = bti.product_id
            LEFT JOIN synced_collections sc ON sc.id = bti.collection_id
            WHERE bti.bulk_task_id = %s
            ORDER BY bti.id
            """,
            (bulk_task_id,),
        )
        items = cur.fetchall()

    def _is_skipped(it):
        r = it.get("ai_result")
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except (ValueError, TypeError):
                return False
        return isinstance(r, dict) and bool(r.get("skipped"))

    return jsonify(
        {
            "id": bulk_task["id"],
            "status": bulk_task["status"],
            "total": bulk_task["total"],
            "completed": bulk_task["completed"],
            "failed": bulk_task["failed"],
            "skipped": sum(1 for it in items if _is_skipped(it)),
            "current_product_title": bulk_task["current_product_title"],
            "current_item_title": bulk_task["current_product_title"],
            "auto_apply": bulk_task["auto_apply"],
            "results": [
                {
                    "product_id": it["product_id"],
                    "collection_id": it.get("collection_id"),
                    "item_title": it["item_title"],
                    "status": it["status"],
                    "error": it["error"],
                }
                for it in items
            ],
        }
    )


@task_bp.route("/api/tasks/bulk-collections", methods=["POST"])
def create_bulk_collection_task():
    data = request.get_json(silent=True) or {}
    collection_ids = data.get("collection_ids", [])
    auto_apply = data.get("auto_apply", True)
    skip_title = data.get("skip_title", False)

    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM synced_collections WHERE id = ANY(%s)", ([int(i) for i in collection_ids],))
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No matching collections found"}), 400

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type, skip_title) VALUES ('pending', %s, %s, 'collection', %s) RETURNING id",
            (len(valid_ids), auto_apply, skip_title),
        )
        bulk_task_id = cur.fetchone()["id"]

        for cid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, collection_id) VALUES (%s, %s)",
                (bulk_task_id, cid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_collection_task, bulk_task_id)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _owned_claims_for(ownership, item_type, item_id, cap=150):
    """Keywords owned by other pages (resolved primaries + manual assignments),
    excluding this item's own claim — its primary must stay usable. Capped by
    volume to bound prompt size."""
    claims = [
        (kw, o) for kw, o in ownership.items()
        if (o["item_type"], o["item_id"]) != (item_type, item_id)
    ]
    claims.sort(key=lambda c: c[1].get("volume") or 0, reverse=True)
    # Display form, not the map key — variant_keys are plural-folded and
    # token-sorted, which would feed mangled phrases into the prompt.
    return [o.get("keyword") or kw for kw, o in claims[:cap]]


def _near_duplicate(text, seen_token_sets, threshold=0.85):
    """True when text's token set overlaps a previously applied one ≥threshold
    (Jaccard). Catches templated metas that vary by one token across a batch —
    the uniqueness failure Google's scaled-content guidance names — without
    tripping on legitimately similar sibling products."""
    from app.services.keyword_identity import tokens
    t = set(tokens(text))
    if not t:
        return False
    for prev in seen_token_sets:
        union = len(t | prev)
        if union and len(t & prev) / union >= threshold:
            return True
    return False


def _drop_duplicate_metas(fields, applied_meta_tokens, item_label):
    """Remove near-duplicate meta fields from a bulk apply; returns the list
    of dropped field names."""
    dropped = []
    for f in ("meta_title", "meta_description"):
        val = fields.get(f)
        if val and _near_duplicate(val, applied_meta_tokens[f]):
            fields.pop(f)
            dropped.append(f)
    if dropped:
        logger.warning("Bulk optimize: dropped near-duplicate %s for %s — "
                       "boilerplate across a batch reads as scaled content",
                       "+".join(dropped), item_label)
    return dropped


def _record_applied_metas(fields, applied_meta_tokens):
    from app.services.keyword_identity import tokens
    for f in ("meta_title", "meta_description"):
        if fields.get(f):
            applied_meta_tokens[f].append(set(tokens(fields[f])))


def _ownership_for(cache, store_id):
    """Per-store ownership map, built lazily — a mixed-store batch used to
    share the first item's map, guarding every item against the wrong store."""
    if store_id not in cache:
        try:
            from app.services.keyword_resolver import get_owned_primary_keywords
            cache[store_id] = get_owned_primary_keywords(store_id)
        except Exception:
            cache[store_id] = {}
    return cache[store_id]


def _claim_keyword(ownership, kw, item_type, item_id):
    """Claim kw in the in-memory ownership map by variant_key, so later items
    in the batch can't take a plural/word-order variant of it (raw-string
    claims let 'dutch master' slip past a 'dutch masters' claim)."""
    try:
        from app.services.keyword_resolver import variant_key
        vk = variant_key(kw)
        if vk and vk not in ownership:
            ownership[vk] = {"item_type": item_type, "item_id": item_id,
                             "score": None, "manual": False, "volume": 0,
                             "keyword": kw}
    except Exception:
        pass


def _claim_applied_primary(ownership, store_id, item_type, item_id):
    """After an apply, claim the item's re-resolved primary — what was actually
    written and scored, not the model's self-reported keyword."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                   FROM technical_seo_analyses
                   WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                (store_id, item_type, item_id),
            )
            row = cur.fetchone()
        if row and row.get("kw"):
            _claim_keyword(ownership, row["kw"], item_type, item_id)
    except Exception:
        logger.warning("Post-apply keyword claim failed for %s %s", item_type, item_id)


def _record_bulk_change_event(store_id, item_type, item_id, change_type, fields):
    """Minimal seo_change_events row for a bulk apply. View Impact keys its
    GSC before/after windows off the event's created_at — without a row,
    bulk-optimized items report 'no optimization history'."""
    try:
        changes = {k: {"new": str(v)[:200]} for k, v in (fields or {}).items() if v}
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO seo_change_events
                   (store_id, item_type, item_id, change_type, changes_applied)
                   VALUES (%s, %s, %s, %s, %s)""",
                (store_id, item_type, item_id, change_type,
                 json.dumps(changes) if changes else None),
            )
    except Exception:
        logger.exception("Bulk change event insert failed for %s %s", item_type, item_id)


def _execute_bulk_collection_task(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )
        cur.execute("SELECT * FROM bulk_tasks WHERE id = %s", (bulk_task_id,))
        bulk_task = cur.fetchone()
        cur.execute(
            """
            SELECT bti.id as item_id, bti.collection_id, sc.shopify_id, sc.title,
                   sc.store_id, s.domain, s.access_token_enc, s.api_version
            FROM bulk_task_items bti
            JOIN synced_collections sc ON sc.id = bti.collection_id
            JOIN stores s ON s.id = sc.store_id
            WHERE bti.bulk_task_id = %s
            ORDER BY bti.id
            """,
            (bulk_task_id,),
        )
        items = cur.fetchall()

    auto_apply = bulk_task["auto_apply"]
    skip_title = bulk_task.get("skip_title", False)
    ownership_cache = {}
    applied_meta_tokens = {"meta_title": [], "meta_description": []}

    for item in items:
        item_id = item["item_id"]
        collection_id = item["collection_id"]

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_task_items SET status = 'running' WHERE id = %s",
                (item_id,),
            )
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (item["title"], bulk_task_id),
            )

        try:
            task_input = {"collection_id": collection_id}
            ownership = _ownership_for(ownership_cache, item["store_id"])
            owned_claims = _owned_claims_for(ownership, "collection", collection_id)
            if owned_claims:
                task_input["claimed_keywords"] = owned_claims
            ai_result = run_task("collection_seo", task_input)

            item_error = None
            if is_failed_result(ai_result):
                item_error = ai_result.get("error") or "AI returned unparsable output"

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), item_id),
                )

            if "error" not in ai_result and "raw_response" not in ai_result:
                target_kw = ai_result.get("target_keywords") or {}
                primary_kw = target_kw.get("primary", "") if isinstance(target_kw, dict) else ""
                if isinstance(primary_kw, str) and primary_kw.strip():
                    # Provisional claim from the model's self-report; superseded
                    # by the re-resolved primary after an apply.
                    _claim_keyword(ownership, primary_kw.split("(")[0].strip(),
                                   "collection", collection_id)

            if auto_apply and "error" not in ai_result and "raw_response" not in ai_result:
                try:
                    client = ShopifyClient(
                        item["domain"],
                        decrypt_value(item["access_token_enc"]),
                        item["api_version"],
                    )

                    collection_fields = {}
                    if not skip_title and ai_result.get("h1_tag"):
                        collection_fields["title"] = clamp_title(ai_result["h1_tag"])
                    if ai_result.get("optimized_description"):
                        # Fresh read — the batch snapshot may be hours old, and
                        # injected hero images must survive the rewrite.
                        from app.services.ai_output_guard import restore_missing_images
                        with db.get_cursor() as cur:
                            cur.execute("SELECT description FROM synced_collections WHERE id = %s", (collection_id,))
                            _cur_desc = ((cur.fetchone() or {}).get("description")) or ""
                        collection_fields["description_html"] = sanitize_html(
                            restore_missing_images(_cur_desc, ai_result["optimized_description"]))
                    if ai_result.get("optimized_meta_title"):
                        collection_fields["meta_title"] = clamp_meta_title(ai_result["optimized_meta_title"])
                    if ai_result.get("optimized_meta_description"):
                        collection_fields["meta_description"] = clamp_meta_description(ai_result["optimized_meta_description"])

                    _drop_duplicate_metas(collection_fields, applied_meta_tokens,
                                          f"collection {collection_id}")

                    # Raises when the live page changed since sync or the
                    # rewrite drops protected content — the item fails with
                    # that reason instead of overwriting.
                    apply_item_changes(
                        item["store_id"], "collection", collection_id,
                        {"title": collection_fields.get("title"), "body": collection_fields.get("description_html"),
                         "meta_title": collection_fields.get("meta_title"),
                         "meta_description": collection_fields.get("meta_description")},
                        source="bulk_optimize", change_type="bulk_optimize", client=client,
                    )
                    _claim_applied_primary(ownership, item["store_id"], "collection", collection_id)
                    _record_applied_metas(collection_fields, applied_meta_tokens)

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": True}), item_id),
                        )

                except Exception as apply_err:
                    item_error = f"Apply to Shopify failed: {apply_err}"
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": False, "error": str(apply_err)}), item_id),
                        )

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (item_error, item_id),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )
                else:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (item_id,),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

        except Exception as e:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), item_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


def _execute_bulk_task(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )
        cur.execute("SELECT * FROM bulk_tasks WHERE id = %s", (bulk_task_id,))
        bulk_task = cur.fetchone()
        cur.execute(
            """
            SELECT bti.id as item_id, bti.product_id, sp.shopify_id, sp.title,
                   sp.store_id, sp.images, s.domain, s.access_token_enc, s.api_version
            FROM bulk_task_items bti
            JOIN synced_products sp ON sp.id = bti.product_id
            JOIN stores s ON s.id = sp.store_id
            WHERE bti.bulk_task_id = %s
            ORDER BY bti.id
            """,
            (bulk_task_id,),
        )
        items = cur.fetchall()

    auto_apply = bulk_task["auto_apply"]
    skip_tags = bulk_task.get("skip_tags", True)
    skip_title = bulk_task.get("skip_title", False)
    ownership_cache = {}
    applied_meta_tokens = {"meta_title": [], "meta_description": []}

    for item in items:
        item_id = item["item_id"]
        product_id = item["product_id"]

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_task_items SET status = 'running' WHERE id = %s",
                (item_id,),
            )
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (item["title"], bulk_task_id),
            )

        try:
            task_input = {"product_id": product_id, "skip_tags": skip_tags}
            ownership = _ownership_for(ownership_cache, item["store_id"])
            owned_claims = _owned_claims_for(ownership, "product", product_id)
            if owned_claims:
                task_input["claimed_keywords"] = owned_claims
            ai_result = run_task("product_seo", task_input)

            item_error = None
            if is_failed_result(ai_result):
                item_error = ai_result.get("error") or "AI returned unparsable output"

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), item_id),
                )

            if "error" not in ai_result and "raw_response" not in ai_result:
                target_kw = ai_result.get("target_keywords") or {}
                primary_kw = target_kw.get("primary", "") if isinstance(target_kw, dict) else ""
                if isinstance(primary_kw, str) and primary_kw.strip():
                    # Provisional claim from the model's self-report; superseded
                    # by the re-resolved primary after an apply.
                    _claim_keyword(ownership, primary_kw.split("(")[0].strip(),
                                   "product", product_id)

            if auto_apply and "error" not in ai_result and "raw_response" not in ai_result:
                try:
                    client = ShopifyClient(
                        item["domain"],
                        decrypt_value(item["access_token_enc"]),
                        item["api_version"],
                    )

                    product_fields = {}
                    if not skip_title and ai_result.get("h1_tag"):
                        product_fields["title"] = clamp_title(ai_result["h1_tag"])
                    if ai_result.get("optimized_description"):
                        from app.services.ai_output_guard import restore_missing_images
                        with db.get_cursor() as cur:
                            cur.execute("SELECT description FROM synced_products WHERE id = %s", (product_id,))
                            _cur_desc = ((cur.fetchone() or {}).get("description")) or ""
                        product_fields["description_html"] = sanitize_html(
                            restore_missing_images(_cur_desc, ai_result["optimized_description"]))
                    if ai_result.get("optimized_meta_title"):
                        product_fields["meta_title"] = clamp_meta_title(ai_result["optimized_meta_title"])
                    if ai_result.get("optimized_meta_description"):
                        product_fields["meta_description"] = clamp_meta_description(ai_result["optimized_meta_description"])
                    if not skip_tags and ai_result.get("suggested_tags") and isinstance(ai_result["suggested_tags"], list):
                        product_fields["tags"] = ai_result["suggested_tags"]

                    _drop_duplicate_metas(product_fields, applied_meta_tokens,
                                          f"product {product_id}")

                    _tags = product_fields.get("tags")
                    _applied = apply_item_changes(
                        item["store_id"], "product", product_id,
                        {"title": product_fields.get("title"), "body": product_fields.get("description_html"),
                         "meta_title": product_fields.get("meta_title"),
                         "meta_description": product_fields.get("meta_description")},
                        source="bulk_optimize", change_type="bulk_optimize", client=client,
                        extra_fields={"tags": _tags} if _tags else None,
                    )

                    media_updates = []
                    if ai_result.get("image_alt_suggestions"):
                        media_updates = [
                            {"media_id": s.get("media_id", ""), "alt": s.get("suggested_alt", "")}
                            for s in ai_result["image_alt_suggestions"]
                            if isinstance(s, dict)
                        ]
                        if media_updates:
                            try:
                                from app.services.ai_content_validator import (
                                    clean_keyword,
                                    ensure_applied_alt_balance,
                                )
                                with db.get_cursor() as cur:
                                    cur.execute("SELECT images FROM synced_products WHERE id = %s", (product_id,))
                                    _img_row = cur.fetchone()
                                    cur.execute(
                                        """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                                           FROM technical_seo_analyses
                                           WHERE store_id = %s AND item_type = 'product' AND item_id = %s""",
                                        (item["store_id"], product_id),
                                    )
                                    _kw_row = cur.fetchone()
                                _imgs = (_img_row or {}).get("images") or []
                                if isinstance(_imgs, str):
                                    _imgs = json.loads(_imgs)
                                media_updates = ensure_applied_alt_balance(
                                    _imgs, media_updates, clean_keyword((_kw_row or {}).get("kw"))
                                )
                            except Exception:
                                logger.exception("Alt balance backstop failed (bulk)")
                            logger.info("Applying alt text for %d images on product %s (bulk)", len(media_updates), product_id)
                            media_result = client.update_product_media(item["shopify_id"], media_updates)
                            logger.info("Alt text bulk apply result: %s", media_result)

                    with db.get_cursor() as cur:
                        if product_fields.get("tags"):
                            cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(product_fields["tags"]), product_id))
                        if media_updates:
                            alt_map = {m["media_id"]: m["alt"] for m in media_updates if m.get("media_id")}
                            # Merge into the CURRENT row, not the snapshot read at
                            # task start — hours may have passed, and overwriting
                            # with stale images reverts every change made since.
                            cur.execute("SELECT images FROM synced_products WHERE id = %s", (product_id,))
                            _fresh = cur.fetchone()
                            images = (_fresh or {}).get("images") or []
                            if isinstance(images, str):
                                images = json.loads(images)
                            for img in images:
                                if img.get("media_id") in alt_map:
                                    img["alt"] = alt_map[img["media_id"]]
                            cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), product_id))
                        cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (product_id,))
                    _side = [n for n, changed in (("tags", _tags), ("image_alts", media_updates)) if changed]
                    if _side and not _applied["event_id"]:
                        record_side_change(item["store_id"], "product", product_id,
                                           "bulk_optimize", "bulk_optimize", _side)

                    try:
                        generate_and_push_product_schema(product_id, client)
                    except Exception as schema_err:
                        logger.warning("Schema push failed for product %s: %s", product_id, schema_err)

                    from app.services.analysis_refresh import refresh_item_analysis
                    refresh_item_analysis(item["store_id"], "product", product_id)
                    _claim_applied_primary(ownership, item["store_id"], "product", product_id)
                    _record_applied_metas(product_fields, applied_meta_tokens)

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": True}), item_id),
                        )

                except Exception as apply_err:
                    item_error = f"Apply to Shopify failed: {apply_err}"
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": False, "error": str(apply_err)}), item_id),
                        )

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (item_error, item_id),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )
                else:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (item_id,),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

        except Exception as e:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), item_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


@task_bp.route("/api/tasks/bulk-titles", methods=["POST"])
def create_bulk_title_task():
    data = request.get_json(silent=True) or {}
    product_ids = data.get("product_ids", [])

    if not product_ids:
        return jsonify({"error": "product_ids is required"}), 400

    with db.get_cursor() as cur:
        # Validate up front — unknown ids would be enqueued but never processed
        # (executor JOIN drops them), leaving completed+failed < total forever.
        cur.execute("SELECT id FROM synced_products WHERE id = ANY(%s)", ([int(i) for i in product_ids],))
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No matching products found"}), 400

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'title') RETURNING id",
            (len(valid_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for pid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                (bulk_task_id, pid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_title_task, bulk_task_id)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _execute_bulk_title_task(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )
        cur.execute(
            """
            SELECT bti.id as item_id, bti.product_id, sp.shopify_id, sp.title,
                   sp.store_id, s.domain, s.access_token_enc, s.api_version
            FROM bulk_task_items bti
            JOIN synced_products sp ON sp.id = bti.product_id
            JOIN stores s ON s.id = sp.store_id
            WHERE bti.bulk_task_id = %s
            ORDER BY bti.id
            """,
            (bulk_task_id,),
        )
        items = cur.fetchall()

    for item in items:
        item_id = item["item_id"]
        product_id = item["product_id"]

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_task_items SET status = 'running' WHERE id = %s",
                (item_id,),
            )
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (item["title"], bulk_task_id),
            )

        try:
            ai_result = run_task("product_title_seo", {"product_id": product_id})

            item_error = None
            if is_failed_result(ai_result):
                item_error = ai_result.get("error") or "AI returned unparsable output"

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), item_id),
                )

            if "error" not in ai_result and "raw_response" not in ai_result:
                new_title = ai_result.get("optimized_title")
                if new_title:
                    try:
                        client = ShopifyClient(
                            item["domain"],
                            decrypt_value(item["access_token_enc"]),
                            item["api_version"],
                        )
                        apply_item_changes(
                            item["store_id"], "product", product_id, {"title": new_title},
                            source="bulk_title", change_type="bulk_title", client=client,
                        )
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                                (json.dumps({"applied": True}), item_id),
                            )
                    except Exception as apply_err:
                        item_error = f"Apply to Shopify failed: {apply_err}"
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                                (json.dumps({"applied": False, "error": str(apply_err)}), item_id),
                            )

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (item_error, item_id),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )
                else:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (item_id,),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

        except Exception as e:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), item_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


@task_bp.route("/api/tasks/bulk-collection-titles", methods=["POST"])
def create_bulk_collection_title_task():
    data = request.get_json(silent=True) or {}
    collection_ids = data.get("collection_ids", [])

    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        # Validate up front — unknown ids would be enqueued but never processed
        # (executor JOIN drops them), leaving completed+failed < total forever.
        cur.execute("SELECT id FROM synced_collections WHERE id = ANY(%s)", ([int(i) for i in collection_ids],))
        valid_ids = [r["id"] for r in cur.fetchall()]
        if not valid_ids:
            return jsonify({"error": "No matching collections found"}), 400

        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'collection_title') RETURNING id",
            (len(valid_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for cid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, collection_id) VALUES (%s, %s)",
                (bulk_task_id, cid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_collection_title_task, bulk_task_id)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _execute_bulk_collection_title_task(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )
        cur.execute(
            """
            SELECT bti.id as item_id, bti.collection_id, sc.shopify_id, sc.title,
                   sc.store_id, s.domain, s.access_token_enc, s.api_version
            FROM bulk_task_items bti
            JOIN synced_collections sc ON sc.id = bti.collection_id
            JOIN stores s ON s.id = sc.store_id
            WHERE bti.bulk_task_id = %s
            ORDER BY bti.id
            """,
            (bulk_task_id,),
        )
        items = cur.fetchall()

    for item in items:
        item_id = item["item_id"]
        collection_id = item["collection_id"]

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_task_items SET status = 'running' WHERE id = %s",
                (item_id,),
            )
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (item["title"], bulk_task_id),
            )

        try:
            ai_result = run_task("collection_title_seo", {"collection_id": collection_id})

            item_error = None
            if is_failed_result(ai_result):
                item_error = ai_result.get("error") or "AI returned unparsable output"

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), item_id),
                )

            if "error" not in ai_result and "raw_response" not in ai_result:
                new_title = ai_result.get("optimized_title")
                if new_title:
                    try:
                        client = ShopifyClient(
                            item["domain"],
                            decrypt_value(item["access_token_enc"]),
                            item["api_version"],
                        )
                        apply_item_changes(
                            item["store_id"], "collection", collection_id, {"title": new_title},
                            source="bulk_title", change_type="bulk_title", client=client,
                        )
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                                (json.dumps({"applied": True}), item_id),
                            )
                    except Exception as apply_err:
                        item_error = f"Apply to Shopify failed: {apply_err}"
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                                (json.dumps({"applied": False, "error": str(apply_err)}), item_id),
                            )

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (item_error, item_id),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )
                else:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', completed_at = NOW() WHERE id = %s",
                        (item_id,),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

        except Exception as e:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), item_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


@task_bp.route("/api/tasks", methods=["GET"])
def list_tasks():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks ORDER BY id DESC LIMIT 50")
        tasks = cur.fetchall()

    return jsonify(
        [
            {
                "id": t["id"],
                "store_id": t["store_id"],
                "task_type": t["task_type"],
                "status": t["status"],
                "created_at": t["created_at"].isoformat() if t["created_at"] else None,
                "completed_at": t["completed_at"].isoformat() if t["completed_at"] else None,
            }
            for t in tasks
        ]
    )


# ── Unified active-tasks feed (global tasks panel) ─────────────────
# Read-only aggregation across every task-tracking table. Human labels and
# owning-page links live here so the frontend needs no per-type knowledge.

AI_TASK_META = {
    "cannibalization_bulk_fix": ("Cannibalization Bulk Fix", "/gsc"),
    "ai_seo_scan": ("AI SEO Scan", "/ai-seo"),
    "feed_readiness_scan": ("Feed Readiness Audit", "/ai-seo"),
    "article_seo": ("Article SEO Analysis", "/blog"),
    "article_research": ("Article Research", "/blog"),
    "topic_discovery": ("Topic Discovery", "/blog"),
    "topic_enrich": ("Blog Topic Research (idea)", "/blog"),
    "article_outline": ("Article Outline", "/blog"),
    "article_generate": ("Article Generation", "/blog"),
    "article_refresh_links": ("Article Link Refresh", "/blog"),
    "blog_sync": ("Blog Sync", "/blog"),
    "blog_landing_page": ("Blog Landing Page", "/blog"),
    "blog_hero_image": ("Blog Hero Image", "/blog"),
    "blog_image": ("Blog Image", "/blog"),
    "blog_seo_suggestions": ("Blog SEO Suggestions", "/blog"),
    "scheduled_blog_post": ("Scheduled Blog Post", "/blog"),
    "blog_template_retrofit": ("Blog Template Retrofit", "/blog"),
    "article_media": ("Article Inline Images", "/blog"),
    "article_media_regen": ("Article Inline Image Regeneration", "/blog"),
    "article_restyle": ("Article Restyle", "/blog"),
    "article_intro_link_plan": ("Intro Link Drafts", "/blog?tab=intro-links"),
    "article_intro_link_apply": ("Intro Link Apply", "/blog?tab=intro-links"),
    "brand_seo": ("Brand SEO", "/brands"),
    "home_page_generate": ("Home Page Generation", "/home-page"),
    "home_carousel_image": ("Home Carousel Image", "/home-page"),
    "keyword_analysis": ("Keyword Analysis", "/seo"),
    "landing_page_seo": ("Landing Page SEO", "/landing-pages"),
    "landing_page_generate": ("Landing Page Generation", "/landing-pages"),
    "landing_hero_image": ("Landing Page Hero Image", "/landing-pages"),
    "news_analyze": ("News Analysis", "/news"),
    "news_auto": ("News Auto-Publish", "/news"),
    "news_hero_image": ("News Hero Image", "/news"),
    "news_hub": ("News Hub Generation", "/news"),
    "news_inline_images": ("News Inline Images", "/news"),
    "news_links": ("News Link Building", "/news"),
    "news_regen_hero": ("News Hero Regeneration", "/news"),
    "news_regen_inline": ("News Inline Image Regeneration", "/news"),
    "technical_seo_fix": ("Keyword SEO Fix", "/seo"),
    "interlinking_analysis": ("Interlinking Analysis", "/interlinking"),
    "interlinking_recommendations": ("Interlinking AI Recommendations", "/interlinking?tab=opportunities"),
    "interlinking_apply": ("Interlinking Apply", "/interlinking"),
    "interlinking_collection_fix": ("Orphan Collection Fix Plan", "/interlinking?tab=coverage"),
    "interlinking_orphan_fix": ("Orphan Page Fix Plan", "/interlinking?tab=coverage"),
}

BULK_TASK_META = {
    None: ("Product SEO Optimization", "/seo"),
    "product": ("Product SEO Optimization", "/seo"),
    "collection": ("Collection SEO Optimization", "/seo"),
    "title": ("Product Title Optimization", "/seo"),
    "collection_title": ("Collection Title Optimization", "/seo"),
    "apply_tags": ("Apply Product Tags", "/seo"),
    "article": ("Bulk Article SEO", "/blog"),
    "brand_logos": ("Brand Logo Generation", "/brands"),
    "brand_seo": ("Bulk Brand SEO", "/brands"),
    "generate_brand_collections": ("Brand Collection Generation", "/brands"),
    "collection_images": ("Collection Image Generation", "/seo"),
    "fact_boost": ("Fact Boost", "/ai-seo"),
    "geo_optimize": ("GEO Optimization", "/ai-seo"),
    "schema_repush": ("Schema Regenerate + Push", "/ai-seo"),
    "generate_collections": ("Collection Generation", "/seo"),
    "generate_taxonomy_collections": ("Taxonomy Collection Generation", "/seo"),
    "placeholder_image": ("Placeholder Image Generation", "/images"),
    "replace_placeholder_image": ("Placeholder Image Replacement", "/images"),
    "technical_seo": ("Keyword SEO Bulk Fix", "/seo?tab=issues"),
    "tseo_analyze": ("Keyword Analysis", "/seo?tab=issues"),
    "seo_issue_fix": ("SEO Issue Fix", "/seo?tab=issues"),
    "collection_publish": ("Publish Collections", "/seo"),
    "collection_delete": ("Delete Collections", "/seo"),
}

KEYWORD_RUN_META = {
    "consolidate": "Keyword Consolidation",
    "audit": "Keyword Audit",
    "plan": "Keyword Planning",
    "apply": "Keyword Strategy Apply",
}

_ACTIVE_STATUSES = ("pending", "running")


def _fallback_label(key):
    return (str(key or "task")).replace("_", " ").title()


def _iso(dt):
    return dt.isoformat() if dt else None


@task_bp.route("/api/tasks/active", methods=["GET"])
def active_tasks():
    sweep_stale_jobs()
    hours = min(max(request.args.get("hours", 24, type=int), 1), 24 * 14)

    if request.args.get("count_only"):
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT
                     (SELECT COUNT(*) FROM ai_tasks WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM bulk_tasks WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM sync_jobs WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM keyword_strategy_runs WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM ai_tracking_runs WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM offsite_runs WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM gsc_data_pulls WHERE status IN ('pending','running'))
                   + (SELECT COUNT(*) FROM semrush_imports WHERE status IN ('pending','running'))
                   AS active_count"""
            )
            n = cur.fetchone()["active_count"]
        return jsonify({"active_count": int(n or 0)})

    tasks = []
    with db.get_cursor() as cur:
        # 1. ai_tasks
        cur.execute(
            f"""SELECT id, store_id, task_type, status, error, created_at, completed_at,
                      COALESCE(input->>'title', input->>'topic', input->>'handle') AS detail,
                      result->'progress' AS progress
               FROM ai_tasks
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed','completed_with_errors') AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 50"""
        )
        for r in cur.fetchall():
            label, page = AI_TASK_META.get(r["task_type"], (_fallback_label(r["task_type"]), None))
            tasks.append({
                "source": "ai_tasks", "id": r["id"], "label": label, "page": page,
                "detail": r["detail"], "status": r["status"], "progress": r["progress"],
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["created_at"]), "completed_at": _iso(r["completed_at"]),
                "error": r["error"],
            })

        # 2. bulk_tasks (discriminator spans two columns: kind holds geo_optimize/fact_boost)
        cur.execute(
            f"""SELECT id, store_id, kind, task_type, status, total, completed, failed,
                      current_product_title, created_at, completed_at
               FROM bulk_tasks
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed','completed_with_errors') AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 50"""
        )
        for r in cur.fetchall():
            # kind first: AI-SEO batch creators set kind while task_type falls
            # to its 'product' column default — task_type-first mislabeled
            # every GEO/fact-boost/schema-repush batch as product SEO.
            key = r["kind"] or r["task_type"]
            label, page = BULK_TASK_META.get(key, (_fallback_label(key), None))
            tasks.append({
                "source": "bulk", "id": r["id"], "label": label, "page": page,
                "detail": None, "status": r["status"], "kind": r["kind"],
                "progress": {"completed": r["completed"] or 0, "failed": r["failed"] or 0, "total": r["total"] or 0},
                "current_item": r["current_product_title"], "store_id": r["store_id"],
                "started_at": _iso(r["created_at"]), "completed_at": _iso(r["completed_at"]),
                "error": None,
            })

        # 3. sync_jobs
        cur.execute(
            f"""SELECT id, store_id, status, current_phase, products_synced, collections_synced,
                      blogs_synced, articles_synced, total_products, error, started_at, completed_at
               FROM sync_jobs
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed','completed_with_errors')
                      AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 20"""
        )
        for r in cur.fetchall():
            if r["status"] in _ACTIVE_STATUSES:
                detail = (r["current_phase"] or "starting").replace("_", " ")
            else:
                detail = f"{r['products_synced'] or 0} products, {r['collections_synced'] or 0} collections"
            progress = None
            if r["total_products"]:
                progress = {"completed": r["products_synced"] or 0, "failed": 0, "total": r["total_products"]}
            tasks.append({
                "source": "sync", "id": r["id"], "label": "Store Sync", "page": "/stores",
                "detail": detail, "status": r["status"], "progress": progress,
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["started_at"]), "completed_at": _iso(r["completed_at"]),
                "error": r["error"],
            })

        # 4. keyword_strategy_runs
        cur.execute(
            f"""SELECT id, store_id, run_type, status, total_items, completed_items, failed_items,
                      error, started_at, completed_at
               FROM keyword_strategy_runs
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed') AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 20"""
        )
        for r in cur.fetchall():
            label = KEYWORD_RUN_META.get(r["run_type"], _fallback_label(r["run_type"]))
            progress = None
            if r["total_items"]:
                progress = {"completed": r["completed_items"] or 0, "failed": r["failed_items"] or 0, "total": r["total_items"]}
            tasks.append({
                "source": "keyword_strategy", "id": r["id"], "label": label, "page": "/keyword-strategy",
                "detail": None, "status": r["status"], "progress": progress,
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["started_at"]), "completed_at": _iso(r["completed_at"]),
                "error": r["error"],
            })

        # 5. ai_tracking_runs
        cur.execute(
            f"""SELECT id, store_id, status, error, started_at, completed_at
               FROM ai_tracking_runs
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed') AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 20"""
        )
        for r in cur.fetchall():
            tasks.append({
                "source": "ai_tracking", "id": r["id"], "label": "AI Visibility Tracking", "page": "/ai-seo",
                "detail": None, "status": r["status"], "progress": None,
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["started_at"]), "completed_at": _iso(r["completed_at"]),
                "error": r["error"],
            })

        # 5b. offsite_runs (off-site visibility audits)
        cur.execute(
            f"""SELECT id, store_id, status, queries_run, error, started_at, completed_at
               FROM offsite_runs
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed') AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 20"""
        )
        for r in cur.fetchall():
            detail = f"{r['queries_run']} queries" if r["queries_run"] else None
            tasks.append({
                "source": "offsite", "id": r["id"], "label": "Off-site Visibility Audit", "page": "/ai-seo",
                "detail": detail, "status": r["status"], "progress": None,
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["started_at"]), "completed_at": _iso(r["completed_at"]),
                "error": r["error"],
            })

        # 6. gsc_data_pulls
        cur.execute(
            f"""SELECT id, store_id, pull_type, rows_pulled, status, error_message, started_at, completed_at
               FROM gsc_data_pulls
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed') AND completed_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 20"""
        )
        for r in cur.fetchall():
            tasks.append({
                "source": "gsc_pull", "id": r["id"],
                "label": f"GSC Data Pull ({r['pull_type'] or 'incremental'})", "page": "/gsc",
                "detail": f"{r['rows_pulled']} rows" if r["rows_pulled"] else None,
                "status": r["status"], "progress": None,
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["started_at"]), "completed_at": _iso(r["completed_at"]),
                "error": r["error_message"],
            })

        # 7. semrush_imports (no completed_at column — window on created_at)
        cur.execute(
            f"""SELECT id, store_id, import_type, filename, row_count, status, error_message, created_at
               FROM semrush_imports
               WHERE status IN ('pending','running')
                  OR (status IN ('completed','failed') AND created_at > NOW() - INTERVAL '{hours} hours')
               ORDER BY id DESC LIMIT 20"""
        )
        for r in cur.fetchall():
            tasks.append({
                "source": "semrush_import", "id": r["id"],
                "label": f"Semrush Import ({r['import_type'] or 'csv'})", "page": "/semrush",
                "detail": r["filename"], "status": r["status"], "progress": None,
                "current_item": None, "store_id": r["store_id"],
                "started_at": _iso(r["created_at"]), "completed_at": None,
                "error": r["error_message"],
            })

    # Optional filters for the Activity Center (backward compatible: absent = all)
    source = request.args.get("source")
    page_filter = request.args.get("page")
    store_filter = request.args.get("store_id", type=int)
    if source:
        tasks = [t for t in tasks if t["source"] == source]
    if page_filter:
        tasks = [t for t in tasks if t.get("page") == page_filter]
    if store_filter:
        tasks = [t for t in tasks if t.get("store_id") in (store_filter, None)]

    active = [t for t in tasks if t["status"] in _ACTIVE_STATUSES]
    finished = [t for t in tasks if t["status"] not in _ACTIVE_STATUSES]

    # running first, then pending — newest first within each
    active.sort(key=lambda t: (t["status"] != "running", -(t["id"] or 0)))
    finished.sort(key=lambda t: (t["completed_at"] or t["started_at"] or ""), reverse=True)
    finished = finished[: 100 if hours > 24 else 20]

    return jsonify({"tasks": active + finished, "active_count": len(active)})
