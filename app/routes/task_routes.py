import json
import logging
import threading
import time

from flask import Blueprint, jsonify, request

from app.ai_agent import run_task
from app.auth import decrypt_value
from app.database import db
from app.routes.schema_routes import generate_and_push_product_schema
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
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps(result), task_id),
            )
    except Exception as e:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@task_bp.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.get_json()
    task_type = (data.get("task_type") or "").strip()
    store_id = data.get("store_id")
    task_input = data.get("input", {})

    if not task_type:
        return jsonify({"error": "task_type is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, %s, %s) RETURNING id",
            (store_id, task_type, json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

    thread = threading.Thread(target=_execute_task, args=(task_id,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "task_id": task_id}), 201


@task_bp.route("/api/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
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
    data = request.get_json() or {}
    store_id = data.get("store_id")

    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'keyword_analysis', %s) RETURNING id",
            (store_id, json.dumps({"store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    thread = threading.Thread(
        target=_execute_keyword_analysis, args=(task_id, store_id), daemon=True
    )
    thread.start()

    return jsonify({"ok": True, "task_id": task_id}), 201


def _execute_keyword_analysis(task_id, store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,)
        )

    try:
        result = run_task("keyword_analysis", {"store_id": store_id}, store_id)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps(result), task_id),
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
    data = request.get_json() or {}
    product_ids = data.get("product_ids", [])
    auto_apply = data.get("auto_apply", True)
    skip_tags = data.get("skip_tags", True)

    if not product_ids:
        return jsonify({"error": "product_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, skip_tags) VALUES ('pending', %s, %s, %s) RETURNING id",
            (len(product_ids), auto_apply, skip_tags),
        )
        bulk_task_id = cur.fetchone()["id"]

        for pid in product_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                (bulk_task_id, pid),
            )

    thread = threading.Thread(
        target=_execute_bulk_task, args=(bulk_task_id,), daemon=True
    )
    thread.start()

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(product_ids)}), 201


@task_bp.route("/api/tasks/bulk/<int:bulk_task_id>", methods=["GET"])
def get_bulk_task(bulk_task_id):
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

    return jsonify(
        {
            "id": bulk_task["id"],
            "status": bulk_task["status"],
            "total": bulk_task["total"],
            "completed": bulk_task["completed"],
            "failed": bulk_task["failed"],
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
    data = request.get_json() or {}
    collection_ids = data.get("collection_ids", [])
    auto_apply = data.get("auto_apply", True)

    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, %s, 'collection') RETURNING id",
            (len(collection_ids), auto_apply),
        )
        bulk_task_id = cur.fetchone()["id"]

        for cid in collection_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, collection_id) VALUES (%s, %s)",
                (bulk_task_id, cid),
            )

    thread = threading.Thread(
        target=_execute_bulk_collection_task, args=(bulk_task_id,), daemon=True
    )
    thread.start()

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(collection_ids)}), 201


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
            ai_result = run_task("collection_seo", {"collection_id": collection_id})

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), item_id),
                )

            if auto_apply and "error" not in ai_result and "raw_response" not in ai_result:
                try:
                    client = ShopifyClient(
                        item["domain"],
                        decrypt_value(item["access_token_enc"]),
                        item["api_version"],
                    )

                    collection_fields = {}
                    if ai_result.get("h1_tag"):
                        collection_fields["title"] = ai_result["h1_tag"]
                    if ai_result.get("optimized_description"):
                        collection_fields["description_html"] = ai_result["optimized_description"]
                    if ai_result.get("optimized_meta_title"):
                        collection_fields["meta_title"] = ai_result["optimized_meta_title"]
                    if ai_result.get("optimized_meta_description"):
                        collection_fields["meta_description"] = ai_result["optimized_meta_description"]

                    if collection_fields:
                        client.update_collection(item["shopify_id"], collection_fields)

                    with db.get_cursor() as cur:
                        if collection_fields.get("title"):
                            cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (collection_fields["title"], collection_id))
                        if collection_fields.get("description_html"):
                            cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (collection_fields["description_html"], collection_id))
                        if collection_fields.get("meta_title"):
                            cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (collection_fields["meta_title"], collection_id))
                        if collection_fields.get("meta_description"):
                            cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (collection_fields["meta_description"], collection_id))
                        cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (collection_id,))

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": True}), item_id),
                        )

                except Exception as apply_err:
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": False, "error": str(apply_err)}), item_id),
                        )

            with db.get_cursor() as cur:
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
            ai_result = run_task("product_seo", {"product_id": product_id, "skip_tags": skip_tags})

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET ai_result = %s WHERE id = %s",
                    (json.dumps(ai_result), item_id),
                )

            if auto_apply and "error" not in ai_result and "raw_response" not in ai_result:
                try:
                    client = ShopifyClient(
                        item["domain"],
                        decrypt_value(item["access_token_enc"]),
                        item["api_version"],
                    )

                    product_fields = {}
                    if ai_result.get("h1_tag"):
                        product_fields["title"] = ai_result["h1_tag"]
                    if ai_result.get("optimized_description"):
                        product_fields["description_html"] = ai_result["optimized_description"]
                    if ai_result.get("optimized_meta_title"):
                        product_fields["meta_title"] = ai_result["optimized_meta_title"]
                    if ai_result.get("optimized_meta_description"):
                        product_fields["meta_description"] = ai_result["optimized_meta_description"]
                    if not skip_tags and ai_result.get("suggested_tags") and isinstance(ai_result["suggested_tags"], list):
                        product_fields["tags"] = ai_result["suggested_tags"]

                    if product_fields:
                        client.update_product(item["shopify_id"], product_fields)

                    media_updates = []
                    if ai_result.get("image_alt_suggestions"):
                        media_updates = [
                            {"media_id": s.get("media_id", ""), "alt": s.get("suggested_alt", "")}
                            for s in ai_result["image_alt_suggestions"]
                            if isinstance(s, dict)
                        ]
                        if media_updates:
                            client.update_product_media(item["shopify_id"], media_updates)

                    with db.get_cursor() as cur:
                        if product_fields.get("title"):
                            cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (product_fields["title"], product_id))
                        if product_fields.get("description_html"):
                            cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (product_fields["description_html"], product_id))
                        if product_fields.get("meta_title"):
                            cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (product_fields["meta_title"], product_id))
                        if product_fields.get("meta_description"):
                            cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (product_fields["meta_description"], product_id))
                        if product_fields.get("tags"):
                            cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(product_fields["tags"]), product_id))
                        if media_updates:
                            alt_map = {m["media_id"]: m["alt"] for m in media_updates if m.get("media_id")}
                            images = item["images"] or []
                            if isinstance(images, str):
                                images = json.loads(images)
                            for img in images:
                                if img.get("media_id") in alt_map:
                                    img["alt"] = alt_map[img["media_id"]]
                            cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), product_id))
                        cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (product_id,))

                    try:
                        generate_and_push_product_schema(product_id, client)
                    except Exception as schema_err:
                        logger.warning("Schema push failed for product %s: %s", product_id, schema_err)

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": True}), item_id),
                        )

                except Exception as apply_err:
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET apply_result = %s WHERE id = %s",
                            (json.dumps({"applied": False, "error": str(apply_err)}), item_id),
                        )

            with db.get_cursor() as cur:
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
