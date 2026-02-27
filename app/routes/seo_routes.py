import json
import logging
import threading
import time

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.routes.schema_routes import generate_and_push_product_schema
from app.services.seo_analyzer import analyze_collection_seo, analyze_product_seo
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

seo_bp = Blueprint("seo", __name__)


@seo_bp.route("/seo")
def seo_page():
    return render_template("seo.html")


@seo_bp.route("/api/seo/products", methods=["GET"])
def list_products_seo():
    store_id = request.args.get("store_id")
    status = request.args.get("status", "").strip().upper()
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {"title": "sp.title", "store": "s.name", "status": "sp.status", "last_optimized_at": "sp.last_optimized_at"}
    order_clause = "ORDER BY sp.title"
    if sort_by in valid_sql_sorts:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {valid_sql_sorts[sort_by]} {direction}"

    valid_statuses = {"ACTIVE", "DRAFT", "ARCHIVED"}
    conditions = []
    params = []

    if store_id:
        conditions.append("sp.store_id = %s")
        params.append(store_id)
    if status in valid_statuses:
        conditions.append("sp.status = %s")
        params.append(status)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT sp.*, s.name as store_name
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            {where_clause}
            {order_clause}
            """,
            tuple(params),
        )
        products = cur.fetchall()

    result = []
    for p in products:
        seo = analyze_product_seo(dict(p))
        result.append(
            {
                "id": p["id"],
                "store_id": p["store_id"],
                "store_name": p["store_name"],
                "shopify_id": p["shopify_id"],
                "title": p["title"],
                "handle": p["handle"],
                "status": p["status"],
                "meta_title": p["meta_title"],
                "meta_description": p["meta_description"],
                "images": p["images"],
                "seo_score": seo["score"],
                "seo_breakdown": seo["breakdown"],
                "last_optimized_at": p["last_optimized_at"].isoformat() if p["last_optimized_at"] else None,
            }
        )

    if sort_by == "seo_score":
        reverse = sort_dir == "desc"
        result.sort(key=lambda x: x["seo_score"], reverse=reverse)

    return jsonify(result)


@seo_bp.route("/api/seo/products/counts", methods=["GET"])
def product_status_counts():
    store_id = request.args.get("store_id")
    with db.get_cursor() as cur:
        if store_id:
            cur.execute(
                "SELECT status, COUNT(*) as cnt FROM synced_products WHERE store_id = %s GROUP BY status",
                (store_id,),
            )
        else:
            cur.execute("SELECT status, COUNT(*) as cnt FROM synced_products GROUP BY status")
        rows = cur.fetchall()

    counts = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    return jsonify({"total": total, **counts})


@seo_bp.route("/api/seo/products/<int:product_id>/apply", methods=["POST"])
def apply_product_changes(product_id):
    data = request.get_json() or {}

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sp.*, s.domain, s.access_token_enc, s.api_version
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            WHERE sp.id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    try:
        client = ShopifyClient(
            product["domain"],
            decrypt_value(product["access_token_enc"]),
            product["api_version"],
        )

        updated_fields = []

        product_fields = {}
        if data.get("title"):
            product_fields["title"] = data["title"]
            updated_fields.append("title")
        if data.get("description_html"):
            product_fields["description_html"] = data["description_html"]
            updated_fields.append("description_html")
        if data.get("meta_title"):
            product_fields["meta_title"] = data["meta_title"]
            updated_fields.append("meta_title")
        if data.get("meta_description"):
            product_fields["meta_description"] = data["meta_description"]
            updated_fields.append("meta_description")
        if "tags" in data and isinstance(data["tags"], list):
            product_fields["tags"] = data["tags"]
            updated_fields.append("tags")

        if product_fields:
            client.update_product(product["shopify_id"], product_fields)

        if data.get("image_alts") and isinstance(data["image_alts"], list):
            client.update_product_media(product["shopify_id"], data["image_alts"])
            updated_fields.append("image_alts")

        with db.get_cursor() as cur:
            if data.get("title"):
                cur.execute("UPDATE synced_products SET title = %s WHERE id = %s", (data["title"], product_id))
            if data.get("description_html"):
                cur.execute("UPDATE synced_products SET description = %s WHERE id = %s", (data["description_html"], product_id))
            if data.get("meta_title"):
                cur.execute("UPDATE synced_products SET meta_title = %s WHERE id = %s", (data["meta_title"], product_id))
            if data.get("meta_description"):
                cur.execute("UPDATE synced_products SET meta_description = %s WHERE id = %s", (data["meta_description"], product_id))
            if "tags" in data and isinstance(data["tags"], list):
                cur.execute("UPDATE synced_products SET tags = %s WHERE id = %s", (", ".join(data["tags"]), product_id))
            if data.get("image_alts"):
                images = product["images"] or []
                if isinstance(images, str):
                    images = json.loads(images)
                alt_map = {a["media_id"]: a["alt"] for a in data["image_alts"] if a.get("media_id")}
                for img in images:
                    if img.get("media_id") in alt_map:
                        img["alt"] = alt_map[img["media_id"]]
                cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), product_id))
            cur.execute("UPDATE synced_products SET last_optimized_at = NOW() WHERE id = %s", (product_id,))

        try:
            generate_and_push_product_schema(product_id, client)
            updated_fields.append("schema")
        except Exception as schema_err:
            logger.warning("Schema push failed for product %s: %s", product_id, schema_err)

        return jsonify({"ok": True, "updated_fields": updated_fields})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/products/<int:product_id>", methods=["GET"])
def get_product_seo(product_id):
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sp.*, s.name as store_name
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            WHERE sp.id = %s
            """,
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    seo = analyze_product_seo(dict(product))

    return jsonify(
        {
            "id": product["id"],
            "store_id": product["store_id"],
            "store_name": product["store_name"],
            "shopify_id": product["shopify_id"],
            "title": product["title"],
            "handle": product["handle"],
            "description": product["description"],
            "vendor": product["vendor"],
            "product_type": product["product_type"],
            "tags": product["tags"],
            "meta_title": product["meta_title"],
            "meta_description": product["meta_description"],
            "images": product["images"],
            "variants": product["variants"],
            "status": product["status"],
            "seo_score": seo["score"],
            "seo_breakdown": seo["breakdown"],
        }
    )


@seo_bp.route("/api/seo/collections", methods=["GET"])
def list_collections_seo():
    store_id = request.args.get("store_id")
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {
        "title": "sc.title",
        "store": "s.name",
        "products_count": "sc.products_count",
        "collection_type": "sc.collection_type",
        "published": "sc.published",
        "last_optimized_at": "sc.last_optimized_at",
    }
    order_clause = "ORDER BY sc.title"
    if sort_by in valid_sql_sorts:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {valid_sql_sorts[sort_by]} {direction}"

    collection_type = request.args.get("collection_type")
    published = request.args.get("published")

    conditions = []
    params = []
    if store_id:
        conditions.append("sc.store_id = %s")
        params.append(store_id)
    if collection_type:
        conditions.append("sc.collection_type = %s")
        params.append(collection_type)
    if published is not None and published != "":
        conditions.append("sc.published = %s")
        params.append(published.lower() == "true")

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT sc.*, s.name as store_name
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            {where_clause}
            {order_clause}
            """,
            tuple(params),
        )
        collections = cur.fetchall()

    result = []
    for c in collections:
        seo = analyze_collection_seo(dict(c))
        result.append(
            {
                "id": c["id"],
                "store_id": c["store_id"],
                "store_name": c["store_name"],
                "shopify_id": c["shopify_id"],
                "title": c["title"],
                "handle": c["handle"],
                "meta_title": c["meta_title"],
                "meta_description": c["meta_description"],
                "products_count": c["products_count"],
                "collection_type": c["collection_type"],
                "published": c["published"],
                "seo_score": seo["score"],
                "seo_breakdown": seo["breakdown"],
                "last_optimized_at": c["last_optimized_at"].isoformat() if c["last_optimized_at"] else None,
            }
        )

    if sort_by == "seo_score":
        reverse = sort_dir == "desc"
        result.sort(key=lambda x: x["seo_score"], reverse=reverse)

    return jsonify(result)


@seo_bp.route("/api/seo/collections/<int:collection_id>/apply", methods=["POST"])
def apply_collection_changes(collection_id):
    data = request.get_json() or {}

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sc.*, s.domain, s.access_token_enc, s.api_version
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id = %s
            """,
            (collection_id,),
        )
        collection = cur.fetchone()

    if not collection:
        return jsonify({"error": "Collection not found"}), 404

    try:
        client = ShopifyClient(
            collection["domain"],
            decrypt_value(collection["access_token_enc"]),
            collection["api_version"],
        )

        updated_fields = []

        fields = {}
        if data.get("title"):
            fields["title"] = data["title"]
            updated_fields.append("title")
        if data.get("description_html"):
            fields["description_html"] = data["description_html"]
            updated_fields.append("description_html")
        if data.get("meta_title"):
            fields["meta_title"] = data["meta_title"]
            updated_fields.append("meta_title")
        if data.get("meta_description"):
            fields["meta_description"] = data["meta_description"]
            updated_fields.append("meta_description")

        if fields:
            client.update_collection(collection["shopify_id"], fields)

        with db.get_cursor() as cur:
            if data.get("title"):
                cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (data["title"], collection_id))
            if data.get("description_html"):
                cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (data["description_html"], collection_id))
            if data.get("meta_title"):
                cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (data["meta_title"], collection_id))
            if data.get("meta_description"):
                cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (data["meta_description"], collection_id))
            cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (collection_id,))

        return jsonify({"ok": True, "updated_fields": updated_fields})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/collections/<int:collection_id>", methods=["GET"])
def get_collection_seo(collection_id):
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sc.*, s.name as store_name
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id = %s
            """,
            (collection_id,),
        )
        collection = cur.fetchone()

    if not collection:
        return jsonify({"error": "Collection not found"}), 404

    seo = analyze_collection_seo(dict(collection))

    return jsonify(
        {
            "id": collection["id"],
            "store_id": collection["store_id"],
            "store_name": collection["store_name"],
            "shopify_id": collection["shopify_id"],
            "title": collection["title"],
            "handle": collection["handle"],
            "description": collection["description"],
            "meta_title": collection["meta_title"],
            "meta_description": collection["meta_description"],
            "products_count": collection["products_count"],
            "collection_type": collection["collection_type"],
            "published": collection["published"],
            "image_url": collection["image_url"],
            "seo_score": seo["score"],
            "seo_breakdown": seo["breakdown"],
        }
    )


@seo_bp.route("/api/seo/collections/delete", methods=["POST"])
def delete_collections():
    data = request.get_json() or {}
    collection_ids = data.get("collection_ids", [])

    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        placeholders = ", ".join(["%s"] * len(collection_ids))
        cur.execute(
            f"""
            SELECT sc.id, sc.shopify_id, sc.title, sc.store_id,
                   s.domain, s.access_token_enc, s.api_version
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id IN ({placeholders})
            """,
            tuple(collection_ids),
        )
        collections = cur.fetchall()

    if not collections:
        return jsonify({"error": "No collections found"}), 404

    deleted = []
    failed = []

    clients = {}
    for c in collections:
        store_id = c["store_id"]
        if store_id not in clients:
            clients[store_id] = ShopifyClient(
                c["domain"],
                decrypt_value(c["access_token_enc"]),
                c["api_version"],
            )

        try:
            clients[store_id].delete_collection(c["shopify_id"])

            with db.get_cursor() as cur:
                cur.execute("DELETE FROM generated_collections WHERE collection_id = %s", (c["id"],))
                cur.execute("DELETE FROM synced_collections WHERE id = %s", (c["id"],))

            deleted.append({"id": c["id"], "title": c["title"]})
            logger.info("Deleted collection '%s' (shopify_id=%s)", c["title"], c["shopify_id"])
        except Exception as e:
            logger.exception("Failed to delete collection '%s'", c["title"])
            failed.append({"id": c["id"], "title": c["title"], "error": str(e)})

    return jsonify({"ok": True, "deleted": deleted, "failed": failed})


@seo_bp.route("/api/seo/collections/publish", methods=["POST"])
def publish_collections():
    data = request.get_json() or {}
    collection_ids = data.get("collection_ids", [])

    if not collection_ids:
        return jsonify({"error": "collection_ids is required"}), 400

    with db.get_cursor() as cur:
        placeholders = ", ".join(["%s"] * len(collection_ids))
        cur.execute(
            f"""
            SELECT sc.id, sc.shopify_id, sc.title, sc.store_id,
                   s.domain, s.access_token_enc, s.api_version
            FROM synced_collections sc
            JOIN stores s ON s.id = sc.store_id
            WHERE sc.id IN ({placeholders})
            """,
            tuple(collection_ids),
        )
        collections = cur.fetchall()

    if not collections:
        return jsonify({"error": "No collections found"}), 404

    published = []
    failed = []

    clients = {}
    for c in collections:
        store_id = c["store_id"]
        if store_id not in clients:
            clients[store_id] = ShopifyClient(
                c["domain"],
                decrypt_value(c["access_token_enc"]),
                c["api_version"],
            )

        try:
            clients[store_id].publish_to_all_channels(c["shopify_id"])

            with db.get_cursor() as cur:
                cur.execute("UPDATE synced_collections SET published = TRUE WHERE id = %s", (c["id"],))

            published.append({"id": c["id"], "title": c["title"]})
            logger.info("Published collection '%s' (shopify_id=%s)", c["title"], c["shopify_id"])
        except Exception as e:
            logger.exception("Failed to publish collection '%s'", c["title"])
            failed.append({"id": c["id"], "title": c["title"], "error": str(e)})

    return jsonify({"ok": True, "published": published, "failed": failed})


@seo_bp.route("/api/seo/bulk-apply-tags/<int:store_id>", methods=["POST"])
def bulk_apply_tags(store_id):
    data = request.get_json() or {}
    assignments = data.get("product_assignments")

    if not assignments:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT product_assignments FROM keyword_taxonomies WHERE store_id = %s",
                (store_id,),
            )
            row = cur.fetchone()
        if not row or not row["product_assignments"]:
            return jsonify({"error": "No product assignments found. Run keyword analysis first."}), 400
        assignments = row["product_assignments"]
        if isinstance(assignments, str):
            assignments = json.loads(assignments)

    if not assignments:
        return jsonify({"error": "No product assignments to apply"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, TRUE, 'apply_tags') RETURNING id",
            (len(assignments),),
        )
        bulk_task_id = cur.fetchone()["id"]

    thread = threading.Thread(
        target=_execute_bulk_apply_tags,
        args=(bulk_task_id, store_id, assignments),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(assignments)}), 201


def _execute_bulk_apply_tags(bulk_task_id, store_id, assignments):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (bulk_task_id,),
            )
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    for assignment in assignments:
        product_id = assignment.get("product_id")
        assigned_tags = assignment.get("assigned_tags", [])

        if not product_id or not assigned_tags:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT shopify_id, title FROM synced_products WHERE id = %s AND store_id = %s",
                (product_id, store_id),
            )
            product = cur.fetchone()

        if not product:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (product["title"], bulk_task_id),
            )

        try:
            last_err = None
            for attempt in range(3):
                try:
                    client.update_product(product["shopify_id"], {"tags": assigned_tags})
                    last_err = None
                    break
                except Exception as retry_err:
                    last_err = retry_err
                    logger.warning("Retry %d/3 for product %s: %s", attempt + 1, product_id, retry_err)
                    time.sleep(2 ** attempt)

            if last_err:
                raise last_err

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_products SET tags = %s WHERE id = %s",
                    (", ".join(assigned_tags), product_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        except Exception as e:
            logger.exception("Failed to apply tags to product %s", product_id)
            with db.get_cursor() as cur:
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


@seo_bp.route("/api/seo/collections/generate/tags", methods=["GET"])
def extract_tags():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, title, handle, tags FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' AND tags IS NOT NULL AND tags != ''",
            (store_id,),
        )
        products = cur.fetchall()

        cur.execute(
            "SELECT shopify_id, title, handle FROM synced_collections WHERE store_id = %s",
            (store_id,),
        )
        existing_map = {}
        for r in cur.fetchall():
            existing_map[r["title"].lower()] = {
                "shopify_id": r["shopify_id"],
                "title": r["title"],
                "handle": r["handle"],
            }

    tag_map = {}
    for p in products:
        raw_tags = p["tags"] or ""
        for tag in raw_tags.split(","):
            tag = tag.strip()
            if not tag:
                continue
            key = tag.lower()
            if key not in tag_map:
                tag_map[key] = {"tag": tag, "product_count": 0, "sample_products": []}
            tag_map[key]["product_count"] += 1
            if len(tag_map[key]["sample_products"]) < 5:
                tag_map[key]["sample_products"].append({"title": p["title"], "handle": p["handle"]})

    result = []
    for key, data in tag_map.items():
        if key in existing_map:
            data["existing_collection"] = existing_map[key]
        result.append(data)

    result.sort(key=lambda x: x["product_count"], reverse=True)
    return jsonify(result)


@seo_bp.route("/api/seo/collections/generate/filter-tags", methods=["POST"])
def filter_tags_ai():
    data = request.get_json() or {}
    tags = data.get("tags", [])
    if not tags:
        return jsonify({"error": "tags list is required"}), 400

    try:
        from app.ai_agent import _get_ai_config
        from openai import OpenAI

        config = _get_ai_config()
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=config["api_key"],
        )

        tag_list_str = "\n".join(
            f"- \"{t['tag']}\" ({t.get('product_count', 0)} products)" for t in tags
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an SEO expert for Shopify stores. Analyze product tags and determine "
                        "which ones would make good, meaningful collections.\n\n"
                        "Return ONLY valid JSON with this exact structure:\n"
                        '{"valid_tags": [{"tag": "original_tag", "collection_title": "Clean SEO Title"}], '
                        '"rejected_tags": [{"tag": "original_tag", "reason": "why rejected"}]}\n\n'
                        "Filter out tags that are: numeric-only, internal codes, meaningless abbreviations, "
                        "single characters, or too generic (like 'new' or 'sale' unless highly relevant). "
                        "For valid tags, provide a cleaned-up, SEO-friendly collection title with proper "
                        "capitalization and expanded abbreviations."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Analyze these product tags and identify which would make good Shopify collections:\n\n{tag_list_str}",
                },
            ],
            temperature=0.2,
        )

        content = response.choices[0].message.content or ""
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(content[start:end])
            return jsonify(parsed)

        return jsonify({"error": "AI returned unparseable response", "raw": content}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@seo_bp.route("/api/seo/collections/generate", methods=["POST"])
def generate_collections():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    tags = data.get("tags", [])
    auto_optimize = data.get("auto_optimize", False)

    if not store_id or not tags:
        return jsonify({"error": "store_id and tags are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, %s, 'generate_collections') RETURNING id",
            (len(tags), auto_optimize),
        )
        bulk_task_id = cur.fetchone()["id"]

    thread = threading.Thread(
        target=_execute_generate_collections,
        args=(bulk_task_id, store_id, tags, auto_optimize),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(tags)}), 201


def _execute_generate_collections(bulk_task_id, store_id, tags, auto_optimize):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s",
            (bulk_task_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (bulk_task_id,),
            )
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    created_collection_ids = []

    for tag_info in tags:
        tag = tag_info.get("tag", "")
        collection_title = tag_info.get("collection_title") or tag.title()

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (collection_title, bulk_task_id),
            )

        try:
            with db.get_cursor() as cur:
                tag_pattern = f"%{tag}%"
                cur.execute(
                    "SELECT shopify_id, title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' AND tags ILIKE %s",
                    (store_id, tag_pattern),
                )
                matching_products = cur.fetchall()

            product_shopify_ids = [p["shopify_id"] for p in matching_products]

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                    (store_id, collection_title),
                )
                existing = cur.fetchone()

            if existing:
                shopify_id = existing["shopify_id"]
                synced_collection_id = existing["id"]
                handle = existing["handle"]

                try:
                    current_products = client.get_all_collection_products(shopify_id)
                    current_product_ids = {p["shopify_id"] for p in current_products}
                except Exception:
                    logger.exception("Failed to fetch products for existing collection '%s'", collection_title)
                    current_product_ids = set()

                missing_ids = [pid for pid in product_shopify_ids if pid not in current_product_ids]

                if missing_ids:
                    try:
                        client.add_products_to_collection(shopify_id, missing_ids)
                    except Exception:
                        logger.exception("Failed to add products to existing collection '%s'", collection_title)
                    logger.info("Added %d missing products to existing collection '%s'", len(missing_ids), collection_title)
                else:
                    logger.info("Collection '%s' already has all tagged products, skipping", collection_title)

                total_products = len(current_product_ids) + len(missing_ids)

                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_collections SET products_count = %s, synced_at = NOW() WHERE id = %s",
                        (total_products, synced_collection_id),
                    )
                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status)
                        VALUES (%s, %s, %s, %s, %s, 'merged')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id,
                            products_count = EXCLUDED.products_count, status = EXCLUDED.status""",
                        (store_id, tag, synced_collection_id, shopify_id, total_products),
                    )

                created_collection_ids.append(synced_collection_id)
            else:
                result = client.create_collection(collection_title)
                shopify_id = result["shopify_id"]
                handle = result["handle"]

                try:
                    client.publish_to_all_channels(shopify_id)
                except Exception:
                    logger.exception("Failed to publish collection '%s' to sales channels", collection_title)

                if product_shopify_ids:
                    try:
                        client.add_products_to_collection(shopify_id, product_shopify_ids)
                    except Exception:
                        logger.exception("Failed to add products to collection '%s'", collection_title)

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                        VALUES (%s, %s, %s, %s, 'manual', %s, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, products_count = EXCLUDED.products_count, synced_at = NOW()
                        RETURNING id""",
                        (store_id, shopify_id, collection_title, handle, len(product_shopify_ids)),
                    )
                    synced_collection_id = cur.fetchone()["id"]

                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status)
                        VALUES (%s, %s, %s, %s, %s, 'created')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id,
                            products_count = EXCLUDED.products_count, status = EXCLUDED.status""",
                        (store_id, tag, synced_collection_id, shopify_id, len(product_shopify_ids)),
                    )

                created_collection_ids.append(synced_collection_id)

                logger.info(
                    "Created collection '%s' with %d products",
                    collection_title, len(product_shopify_ids),
                )

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        except Exception as e:
            logger.exception("Failed to process collection for tag '%s'", tag)
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO generated_collections (store_id, tag, status)
                    VALUES (%s, %s, 'failed')
                    ON CONFLICT (store_id, tag) DO UPDATE SET status = 'failed'""",
                    (store_id, tag),
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

    if auto_optimize and created_collection_ids:
        _auto_optimize_generated_collections(store_id, created_collection_ids)


@seo_bp.route("/api/seo/collections/generate/taxonomy", methods=["POST"])
def generate_taxonomy_collections():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    categories = data.get("categories", [])
    auto_optimize = data.get("auto_optimize", False)

    if not store_id or not categories:
        return jsonify({"error": "store_id and categories are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT taxonomy, product_assignments FROM keyword_taxonomies WHERE store_id = %s AND status = 'completed'",
            (store_id,),
        )
        tax_row = cur.fetchone()

    if not tax_row:
        return jsonify({"error": "No completed taxonomy found. Run keyword analysis first."}), 400

    total_work = len(categories)
    for cat in categories:
        total_work += len(cat.get("tags", []))

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, %s, 'generate_taxonomy_collections') RETURNING id",
            (total_work, auto_optimize),
        )
        bulk_task_id = cur.fetchone()["id"]

    product_assignments = tax_row["product_assignments"]
    if isinstance(product_assignments, str):
        product_assignments = json.loads(product_assignments)

    thread = threading.Thread(
        target=_execute_generate_taxonomy_collections,
        args=(bulk_task_id, store_id, categories, product_assignments, auto_optimize),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": total_work}), 201


def _execute_generate_taxonomy_collections(bulk_task_id, store_id, categories, product_assignments, auto_optimize):
    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status = 'completed', completed_at = NOW() WHERE id = %s",
                (bulk_task_id,),
            )
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    tag_to_product_ids = {}
    for assignment in product_assignments:
        pid = assignment.get("product_id")
        for tag in assignment.get("assigned_tags", []):
            tag_to_product_ids.setdefault(tag, set()).add(pid)

    created_collection_ids = []
    hierarchy = {}

    for cat in categories:
        cat_name = cat.get("name", "")
        cat_tags = cat.get("tags", [])

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (f"Category: {cat_name}", bulk_task_id),
            )

        try:
            all_cat_product_ids = set()
            for tag in cat_tags:
                all_cat_product_ids.update(tag_to_product_ids.get(tag, set()))

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                    (store_id, cat_name),
                )
                existing_parent = cur.fetchone()

            if existing_parent:
                parent_shopify_id = existing_parent["shopify_id"]
                parent_synced_id = existing_parent["id"]
                parent_handle = existing_parent["handle"]
            else:
                result = client.create_collection(cat_name)
                parent_shopify_id = result["shopify_id"]
                parent_handle = result["handle"]

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                        VALUES (%s, %s, %s, %s, 'manual', 0, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, synced_at = NOW()
                        RETURNING id""",
                        (store_id, parent_shopify_id, cat_name, parent_handle),
                    )
                    parent_synced_id = cur.fetchone()["id"]

            if all_cat_product_ids:
                with db.get_cursor() as cur:
                    placeholders = ", ".join(["%s"] * len(all_cat_product_ids))
                    cur.execute(
                        f"SELECT shopify_id FROM synced_products WHERE id IN ({placeholders}) AND store_id = %s",
                        list(all_cat_product_ids) + [store_id],
                    )
                    cat_shopify_ids = [r["shopify_id"] for r in cur.fetchall()]

                if cat_shopify_ids:
                    try:
                        client.add_products_to_collection(parent_shopify_id, cat_shopify_ids)
                    except Exception:
                        logger.exception("Failed to add products to parent collection '%s'", cat_name)

                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_collections SET products_count = %s, synced_at = NOW() WHERE id = %s",
                        (len(cat_shopify_ids), parent_synced_id),
                    )

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status, parent_collection_id, taxonomy_category, generation_mode)
                    VALUES (%s, %s, %s, %s, %s, 'created', NULL, %s, 'taxonomy')
                    ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                        shopify_collection_id = EXCLUDED.shopify_collection_id,
                        products_count = EXCLUDED.products_count, status = EXCLUDED.status,
                        parent_collection_id = EXCLUDED.parent_collection_id,
                        taxonomy_category = EXCLUDED.taxonomy_category,
                        generation_mode = EXCLUDED.generation_mode""",
                    (store_id, f"__category__{cat_name}", parent_synced_id, parent_shopify_id, len(all_cat_product_ids), cat_name),
                )

            created_collection_ids.append(parent_synced_id)
            hierarchy[cat_name] = {
                "parent_id": parent_synced_id,
                "parent_handle": parent_handle,
                "parent_title": cat_name,
                "children": [],
            }

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

            logger.info("Created parent category collection '%s' with %d products", cat_name, len(all_cat_product_ids))

        except Exception:
            logger.exception("Failed to create parent collection for category '%s'", cat_name)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        time.sleep(0.5)

        for tag in cat_tags:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                    (tag, bulk_task_id),
                )

            try:
                tag_product_ids = tag_to_product_ids.get(tag, set())

                product_shopify_ids = []
                if tag_product_ids:
                    with db.get_cursor() as cur:
                        placeholders = ", ".join(["%s"] * len(tag_product_ids))
                        cur.execute(
                            f"SELECT shopify_id FROM synced_products WHERE id IN ({placeholders}) AND store_id = %s",
                            list(tag_product_ids) + [store_id],
                        )
                        product_shopify_ids = [r["shopify_id"] for r in cur.fetchall()]

                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                        (store_id, tag),
                    )
                    existing_child = cur.fetchone()

                if existing_child:
                    child_shopify_id = existing_child["shopify_id"]
                    child_synced_id = existing_child["id"]
                    child_handle = existing_child["handle"]

                    if product_shopify_ids:
                        try:
                            current_products = client.get_all_collection_products(child_shopify_id)
                            current_ids = {p["shopify_id"] for p in current_products}
                            missing = [pid for pid in product_shopify_ids if pid not in current_ids]
                            if missing:
                                client.add_products_to_collection(child_shopify_id, missing)
                        except Exception:
                            logger.exception("Failed to update products for existing child collection '%s'", tag)

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE synced_collections SET products_count = %s, synced_at = NOW() WHERE id = %s",
                            (len(product_shopify_ids), child_synced_id),
                        )
                else:
                    result = client.create_collection(tag)
                    child_shopify_id = result["shopify_id"]
                    child_handle = result["handle"]

                    if product_shopify_ids:
                        try:
                            client.add_products_to_collection(child_shopify_id, product_shopify_ids)
                        except Exception:
                            logger.exception("Failed to add products to child collection '%s'", tag)

                    with db.get_cursor() as cur:
                        cur.execute(
                            """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                            VALUES (%s, %s, %s, %s, 'manual', %s, NOW())
                            ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, products_count = EXCLUDED.products_count, synced_at = NOW()
                            RETURNING id""",
                            (store_id, child_shopify_id, tag, child_handle, len(product_shopify_ids)),
                        )
                        child_synced_id = cur.fetchone()["id"]

                parent_synced_id = hierarchy.get(cat_name, {}).get("parent_id")

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status, parent_collection_id, taxonomy_category, generation_mode)
                        VALUES (%s, %s, %s, %s, %s, 'created', %s, %s, 'taxonomy')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id,
                            products_count = EXCLUDED.products_count, status = EXCLUDED.status,
                            parent_collection_id = EXCLUDED.parent_collection_id,
                            taxonomy_category = EXCLUDED.taxonomy_category,
                            generation_mode = EXCLUDED.generation_mode""",
                        (store_id, tag, child_synced_id, child_shopify_id, len(product_shopify_ids), parent_synced_id, cat_name),
                    )

                created_collection_ids.append(child_synced_id)

                if cat_name in hierarchy:
                    hierarchy[cat_name]["children"].append({
                        "id": child_synced_id,
                        "title": tag,
                        "handle": child_handle,
                    })

                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )

                logger.info("Created child collection '%s' under '%s' with %d products", tag, cat_name, len(product_shopify_ids))

            except Exception:
                logger.exception("Failed to create child collection for tag '%s'", tag)
                with db.get_cursor() as cur:
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

    if auto_optimize and created_collection_ids:
        _auto_optimize_generated_collections(store_id, created_collection_ids, hierarchy)


@seo_bp.route("/api/seo/collections/hierarchy/<int:store_id>", methods=["GET"])
def get_collection_hierarchy(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.*, sc.shopify_id, sc.title as collection_title, sc.handle as collection_handle, sc.products_count as actual_products_count
            FROM generated_collections gc
            LEFT JOIN synced_collections sc ON sc.id = gc.collection_id
            WHERE gc.store_id = %s AND gc.generation_mode = 'taxonomy'
            ORDER BY gc.taxonomy_category, gc.parent_collection_id NULLS FIRST, gc.tag""",
            (store_id,),
        )
        rows = cur.fetchall()

    categories_map = {}
    for r in rows:
        cat_name = r["taxonomy_category"] or "Uncategorized"

        if cat_name not in categories_map:
            categories_map[cat_name] = {
                "name": cat_name,
                "parent_collection": None,
                "children": [],
            }

        if r["parent_collection_id"] is None and r["tag"].startswith("__category__"):
            categories_map[cat_name]["parent_collection"] = {
                "id": r["collection_id"],
                "shopify_id": r["shopify_id"],
                "title": r["collection_title"],
                "handle": r["collection_handle"],
                "products_count": r["actual_products_count"] or 0,
            }
        else:
            categories_map[cat_name]["children"].append({
                "tag": r["tag"],
                "collection": {
                    "id": r["collection_id"],
                    "shopify_id": r["shopify_id"],
                    "title": r["collection_title"],
                    "handle": r["collection_handle"],
                },
                "products_count": r["actual_products_count"] or r["products_count"] or 0,
            })

    return jsonify({"categories": list(categories_map.values())})


def _auto_optimize_generated_collections(store_id, collection_ids, hierarchy=None):
    from app.ai_agent import run_task as ai_run_task

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        return

    hierarchy_context_map = {}
    if hierarchy:
        for cat_name, cat_data in hierarchy.items():
            parent_id = cat_data.get("parent_id")
            children = cat_data.get("children", [])
            child_titles = [c["title"] for c in children]
            child_handles = [c.get("handle", "") for c in children]

            if parent_id:
                hierarchy_context_map[parent_id] = (
                    f"This is a PARENT category collection for '{cat_name}'. "
                    f"It has {len(children)} sub-collections: {', '.join(child_titles)}. "
                    "Include internal links to these sub-collections using /collections/{{handle}} URLs: "
                    + ", ".join(f"{t} (/collections/{h})" for t, h in zip(child_titles, child_handles) if h)
                )

            for child in children:
                cid = child.get("id")
                if cid:
                    siblings = [c["title"] for c in children if c["id"] != cid]
                    sibling_handles = [c.get("handle", "") for c in children if c["id"] != cid]
                    hierarchy_context_map[cid] = (
                        f"This is a sub-collection under the parent category '{cat_name}' "
                        f"(/collections/{cat_data.get('parent_handle', '')}). "
                        f"Sibling collections: {', '.join(siblings)}. "
                        "Include a link back to the parent collection and links to 2-3 sibling collections: "
                        + ", ".join(f"{t} (/collections/{h})" for t, h in zip(siblings, sibling_handles) if h)
                    )

    for cid in collection_ids:
        try:
            task_input = {"collection_id": cid}
            if cid in hierarchy_context_map:
                task_input["hierarchy_context"] = hierarchy_context_map[cid]

            ai_result = ai_run_task("collection_seo", task_input)

            if "error" not in ai_result and "raw_response" not in ai_result:
                client = ShopifyClient(
                    store["domain"],
                    decrypt_value(store["access_token_enc"]),
                    store["api_version"],
                )

                with db.get_cursor() as cur:
                    cur.execute("SELECT shopify_id FROM synced_collections WHERE id = %s", (cid,))
                    row = cur.fetchone()

                if not row:
                    continue

                fields = {}
                if ai_result.get("h1_tag"):
                    fields["title"] = ai_result["h1_tag"]
                if ai_result.get("optimized_description"):
                    fields["description_html"] = ai_result["optimized_description"]
                if ai_result.get("optimized_meta_title"):
                    fields["meta_title"] = ai_result["optimized_meta_title"]
                if ai_result.get("optimized_meta_description"):
                    fields["meta_description"] = ai_result["optimized_meta_description"]

                if fields:
                    client.update_collection(row["shopify_id"], fields)

                with db.get_cursor() as cur:
                    if fields.get("title"):
                        cur.execute("UPDATE synced_collections SET title = %s WHERE id = %s", (fields["title"], cid))
                    if fields.get("description_html"):
                        cur.execute("UPDATE synced_collections SET description = %s WHERE id = %s", (fields["description_html"], cid))
                    if fields.get("meta_title"):
                        cur.execute("UPDATE synced_collections SET meta_title = %s WHERE id = %s", (fields["meta_title"], cid))
                    if fields.get("meta_description"):
                        cur.execute("UPDATE synced_collections SET meta_description = %s WHERE id = %s", (fields["meta_description"], cid))
                    cur.execute("UPDATE synced_collections SET last_optimized_at = NOW() WHERE id = %s", (cid,))

                    cur.execute(
                        "UPDATE generated_collections SET status = 'optimized' WHERE collection_id = %s",
                        (cid,),
                    )

                logger.info("Auto-optimized generated collection %s", cid)

        except Exception:
            logger.exception("Auto-optimize failed for collection %s", cid)

        time.sleep(1)
