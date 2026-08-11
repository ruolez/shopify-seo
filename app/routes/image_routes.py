import base64
import json
import logging
import threading
import time

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services.task_runner import spawn as spawn_task, sweep_stale_jobs
from app.services.image_generator import generate_placeholder_image
from app.services.sync_service import _upsert_product_batch
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

image_bp = Blueprint("images", __name__)


@image_bp.route("/images")
def image_manager_page():
    return render_template("image_manager.html")


_LIVE_MEDIA_QUERY = """
query ($id: ID!) {
  product(id: $id) {
    media(first: 50) {
      edges { node { ... on MediaImage { id alt preview { image { url } } } } }
    }
  }
}
"""


def _refresh_images_from_shopify(store_id, limit=30):
    """Re-pull live media for products whose mirror says no images (or that
    carry a placeholder) so a re-scan reflects Shopify reality — images added
    in the Shopify admin are otherwise invisible until the next full sync.
    Returns the number of products whose mirror changed."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sp.id, sp.shopify_id, sp.images
               FROM synced_products sp
               LEFT JOIN placeholder_images pi ON pi.product_id = sp.id
               WHERE sp.store_id = %s AND sp.status = 'ACTIVE'
                 AND (sp.images IS NULL OR sp.images::text IN ('[]', 'null') OR pi.id IS NOT NULL)
               LIMIT %s""",
            (store_id, limit),
        )
        candidates = cur.fetchall()
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()
    if not candidates or not store:
        return 0

    client = ShopifyClient(
        store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"]
    )
    changed = 0
    for c in candidates:
        try:
            data = client._graphql(_LIVE_MEDIA_QUERY, {"id": c["shopify_id"]})
            edges = (((data or {}).get("product") or {}).get("media") or {}).get("edges") or []
            live_images = [
                {
                    "media_id": m["node"].get("id") or "",
                    "url": ((m["node"].get("preview") or {}).get("image") or {}).get("url", ""),
                    "alt": m["node"].get("alt") or "",
                }
                for m in edges
                if m.get("node", {}).get("id")
            ]
            mirror = c["images"] or []
            if isinstance(mirror, str):
                mirror = json.loads(mirror)
            if {i.get("media_id") for i in live_images} != {i.get("media_id") for i in mirror}:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_products SET images = %s WHERE id = %s",
                        (json.dumps(live_images), c["id"]),
                    )
                from app.services.analysis_refresh import refresh_item_analysis
                refresh_item_analysis(store_id, "product", c["id"])
                changed += 1
        except Exception:
            logger.warning("Live image refresh failed for product %s", c["id"])
    return changed


def _discover_products_since_last_sync(store_id, max_pages=40):
    """Upsert products Shopify has that the mirror is missing or has stale.

    The mirror only gains rows during a full store sync, so a product
    published afterwards is invisible here — including a brand-new product
    with no image at all, which is exactly what this scan exists to find.
    Pull everything Shopify touched at/after the mirror's newest timestamp
    and upsert it. Returns the local ids of products the mirror had never
    seen."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT MAX(shopify_updated_at) AS cutoff FROM synced_products WHERE store_id = %s",
            (store_id,),
        )
        cutoff = cur.fetchone()["cutoff"]
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()
    # No cutoff means the mirror is empty for this store — a full sync, not an
    # incremental catch-up, is the right way to populate it.
    if not store or not cutoff:
        return []

    client = ShopifyClient(
        store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"]
    )
    query_filter = (
        "status:active AND published_status:published AND "
        f"updated_at:>='{cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    )
    fetched = []
    try:
        for page_no, page in enumerate(client.iter_products(query_filter=query_filter), start=1):
            fetched.extend(page)
            if page_no >= max_pages:
                logger.warning(
                    "Product discovery for store %s hit the %d-page cap — run a full sync",
                    store_id, max_pages,
                )
                break
    except Exception:
        logger.warning("Product discovery failed for store %s", store_id)
        return []
    if not fetched:
        return []

    shopify_ids = [p["shopify_id"] for p in fetched]
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT shopify_id FROM synced_products WHERE store_id = %s AND shopify_id = ANY(%s)",
            (store_id, shopify_ids),
        )
        known = {r["shopify_id"] for r in cur.fetchall()}

    _upsert_product_batch(int(store_id), fetched)

    new_shopify_ids = [sid for sid in shopify_ids if sid not in known]
    if not new_shopify_ids:
        return []
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM synced_products WHERE store_id = %s AND shopify_id = ANY(%s)",
            (store_id, new_shopify_ids),
        )
        new_ids = [r["id"] for r in cur.fetchall()]

    from app.services.analysis_refresh import refresh_item_analysis
    for pid in new_ids:
        refresh_item_analysis(store_id, "product", pid)
    logger.info("Discovered %d product(s) missing from the mirror for store %s", len(new_ids), store_id)
    return new_ids


@image_bp.route("/api/images/scan", methods=["GET"])
def scan_missing_images():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    refreshed = 0
    discovered = 0
    if request.args.get("refresh"):
        discovered = len(_discover_products_since_last_sync(store_id))
        refreshed = _refresh_images_from_shopify(store_id)

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sp.id, sp.shopify_id, sp.title, sp.handle, sp.status,
                   sp.images, sp.variants,
                   COALESCE(s.shop_name, s.name) as store_name,
                   pi.id as placeholder_id,
                   pi.uploaded_at as placeholder_uploaded_at,
                   pi.filename as placeholder_filename,
                   pi.seo_optimized
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            LEFT JOIN placeholder_images pi ON pi.product_id = sp.id
            WHERE sp.store_id = %s AND sp.status = 'ACTIVE'
            ORDER BY sp.title
            """,
            (store_id,),
        )
        products = cur.fetchall()

    results = []
    total_with_images = 0
    needs_seo_fix = 0
    for p in products:
        images = p["images"] or []
        if isinstance(images, str):
            images = json.loads(images)

        variants = p["variants"] or []
        if isinstance(variants, str):
            variants = json.loads(variants)

        has_placeholder = p["placeholder_id"] is not None
        variant_count = max(len(variants), 1)
        has_product_image = len(images) > 0
        seo_optimized = bool(p.get("seo_optimized"))

        if has_placeholder and not seo_optimized:
            needs_seo_fix += 1

        if has_product_image and not has_placeholder:
            total_with_images += 1
            continue

        results.append({
            "id": p["id"],
            "shopify_id": p["shopify_id"],
            "title": p["title"],
            "handle": p["handle"],
            "status": p["status"],
            "store_name": p["store_name"],
            "image_count": len(images),
            "variant_count": variant_count,
            "has_placeholder": has_placeholder,
            "placeholder_uploaded_at": (
                p["placeholder_uploaded_at"].isoformat()
                if p["placeholder_uploaded_at"]
                else None
            ),
            "placeholder_filename": p.get("placeholder_filename"),
            "seo_optimized": seo_optimized,
        })

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) as total, MAX(synced_at) as last_synced_at
               FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'""",
            (store_id,),
        )
        mirror = cur.fetchone()
        total_active = mirror["total"]
        last_synced_at = mirror["last_synced_at"]

    missing_count = sum(1 for r in results if not r["has_placeholder"] and r["image_count"] == 0)
    placeholder_count = sum(1 for r in results if r["has_placeholder"])

    return jsonify({
        "products": results,
        "refreshed_from_shopify": refreshed,
        "discovered_from_shopify": discovered,
        "mirror_synced_at": last_synced_at.isoformat() if last_synced_at else None,
        "stats": {
            "total_active": total_active,
            "missing_images": missing_count,
            "has_placeholder": placeholder_count,
            "with_images": total_with_images,
            "needs_seo_fix": needs_seo_fix,
        },
    })


def _balance_placeholder_alt(item, product_id, alt_text):
    """Keyword-balance backstop for placeholder alts. The alt is the product
    title, which usually carries the resolved primary keyword — if an existing
    image alt already carries it, strip the phrase from the placeholder's alt
    so the set can't tip into keyword-in-every-alt (scored as stuffing).
    Fails open."""
    try:
        from app.services.ai_content_validator import clean_keyword, strip_keyword_phrase
        from app.services.technical_seo_analyzer import _keyword_in_text
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                   FROM technical_seo_analyses
                   WHERE store_id = %s AND item_type = 'product' AND item_id = %s""",
                (item["store_id"], product_id),
            )
            row = cur.fetchone()
        kw = clean_keyword((row or {}).get("kw"))
        if not kw or not _keyword_in_text(kw, alt_text):
            return alt_text
        images = item.get("images") or []
        if isinstance(images, str):
            images = json.loads(images)
        if any(_keyword_in_text(kw, img.get("alt") or "") for img in images):
            return strip_keyword_phrase(alt_text, kw) or alt_text
    except Exception:
        logger.warning("Placeholder alt balance failed for product %s", product_id)
    return alt_text


@image_bp.route("/api/images/generate-preview", methods=["POST"])
def generate_preview():
    data = request.get_json(silent=True) or {}
    product_title = data.get("title", "Sample Product")
    handle = data.get("handle")

    image_bytes, filename, alt_text = generate_placeholder_image(product_title, handle=handle)
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    return jsonify({
        "image_base64": b64,
        "filename": filename,
        "alt_text": alt_text,
    })


@image_bp.route("/api/images/bulk-upload", methods=["POST"])
def bulk_upload():
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
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'placeholder_image') RETURNING id",
            (len(valid_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for pid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                (bulk_task_id, pid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_upload, bulk_task_id)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


@image_bp.route("/api/images/bulk-status/<int:bulk_task_id>", methods=["GET"])
def bulk_status(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM bulk_tasks WHERE id = %s", (bulk_task_id,))
        task = cur.fetchone()

    if not task:
        return jsonify({"error": "Task not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT bti.*, sp.title as product_title
            FROM bulk_task_items bti
            LEFT JOIN synced_products sp ON sp.id = bti.product_id
            WHERE bti.bulk_task_id = %s
            ORDER BY bti.id
            """,
            (bulk_task_id,),
        )
        items = cur.fetchall()

    return jsonify({
        "id": task["id"],
        "status": task["status"],
        "total": task["total"],
        "completed": task["completed"],
        "failed": task["failed"],
        "current_product_title": task["current_product_title"],
        "results": [
            {
                "product_id": it["product_id"],
                "product_title": it["product_title"],
                "status": it["status"],
                "error": it["error"],
            }
            for it in items
        ],
    })


def _execute_bulk_upload(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )
        cur.execute(
            """
            SELECT bti.id as item_id, bti.product_id, sp.shopify_id, sp.title, sp.handle,
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
            # Check if already has a placeholder
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id FROM placeholder_images WHERE product_id = %s",
                    (product_id,),
                )
                existing = cur.fetchone()

            if existing:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE id = %s",
                        (json.dumps({"skipped": True, "reason": "already has placeholder"}), item_id),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )
                continue

            # Generate placeholder image
            image_bytes, filename, alt_text = generate_placeholder_image(
                item["title"], handle=item.get("handle")
            )
            alt_text = _balance_placeholder_alt(item, product_id, alt_text)

            # Create Shopify client
            client = ShopifyClient(
                item["domain"],
                decrypt_value(item["access_token_enc"]),
                item["api_version"],
            )

            # Staged upload
            staged = client.staged_upload_create(filename, "image/png", len(image_bytes))

            # Upload file to staged URL
            resource_url = client.upload_staged_file(staged, image_bytes, filename=filename)

            # Attach media to product
            media_result = client.create_product_media(
                item["shopify_id"], resource_url, alt_text
            )

            media_id = media_result.get("id", "")
            media_url = (
                ((media_result.get("preview") or {}).get("image") or {}).get("url", "")
            )

            # Track in placeholder_images table
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO placeholder_images (product_id, store_id, shopify_media_id, shopify_media_url, alt_text, filename, seo_optimized)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (product_id) DO UPDATE SET
                        shopify_media_id = EXCLUDED.shopify_media_id,
                        shopify_media_url = EXCLUDED.shopify_media_url,
                        alt_text = EXCLUDED.alt_text,
                        filename = EXCLUDED.filename,
                        seo_optimized = TRUE,
                        uploaded_at = NOW()""",
                    (product_id, item["store_id"], media_id, media_url, alt_text, filename),
                )

            # Update synced_products.images JSONB
            images = item["images"] or []
            if isinstance(images, str):
                images = json.loads(images)
            images.append({
                "media_id": media_id,
                "url": media_url,
                "alt": alt_text,
            })
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_products SET images = %s WHERE id = %s",
                    (json.dumps(images), product_id),
                )

            # The live schema picks the image up automatically (v2 renders
            # from product media) — only the stored analysis needs refreshing
            # so the "schema missing product image" issue clears.
            from app.services.analysis_refresh import refresh_item_analysis
            refresh_item_analysis(item["store_id"], "product", product_id)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"uploaded": True, "media_id": media_id}), item_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        except Exception as e:
            logger.exception("Placeholder upload failed for product %s", product_id)
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


@image_bp.route("/api/images/bulk-replace", methods=["POST"])
def bulk_replace():
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
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, true, 'replace_placeholder_image') RETURNING id",
            (len(valid_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

        for pid in valid_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, product_id) VALUES (%s, %s)",
                (bulk_task_id, pid),
            )

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_replace, bulk_task_id)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(valid_ids)}), 201


def _execute_bulk_replace(bulk_task_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,)
        )
        cur.execute(
            """
            SELECT bti.id as item_id, bti.product_id, sp.shopify_id, sp.title, sp.handle,
                   sp.store_id, sp.images, s.domain, s.access_token_enc, s.api_version,
                   pi.shopify_media_id as old_media_id
            FROM bulk_task_items bti
            JOIN synced_products sp ON sp.id = bti.product_id
            JOIN stores s ON s.id = sp.store_id
            LEFT JOIN placeholder_images pi ON pi.product_id = sp.id
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
            client = ShopifyClient(
                item["domain"],
                decrypt_value(item["access_token_enc"]),
                item["api_version"],
            )

            # Delete old image from Shopify if exists
            old_media_id = item.get("old_media_id")
            if old_media_id:
                try:
                    client.delete_files([old_media_id])
                except Exception as del_err:
                    logger.warning("Failed to delete old media %s: %s", old_media_id, del_err)

            # Generate new image with SEO-friendly naming
            image_bytes, filename, alt_text = generate_placeholder_image(
                item["title"], handle=item.get("handle")
            )
            alt_text = _balance_placeholder_alt(item, product_id, alt_text)

            # Upload new image
            staged = client.staged_upload_create(filename, "image/png", len(image_bytes))
            resource_url = client.upload_staged_file(staged, image_bytes, filename=filename)
            media_result = client.create_product_media(
                item["shopify_id"], resource_url, alt_text
            )

            media_id = media_result.get("id", "")
            media_url = (
                ((media_result.get("preview") or {}).get("image") or {}).get("url", "")
            )

            # Update placeholder_images row
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO placeholder_images (product_id, store_id, shopify_media_id, shopify_media_url, alt_text, filename, seo_optimized)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (product_id) DO UPDATE SET
                        shopify_media_id = EXCLUDED.shopify_media_id,
                        shopify_media_url = EXCLUDED.shopify_media_url,
                        alt_text = EXCLUDED.alt_text,
                        filename = EXCLUDED.filename,
                        seo_optimized = TRUE,
                        uploaded_at = NOW()""",
                    (product_id, item["store_id"], media_id, media_url, alt_text, filename),
                )

            # Update synced_products.images JSONB — remove old, add new
            images = item["images"] or []
            if isinstance(images, str):
                images = json.loads(images)
            if old_media_id:
                images = [img for img in images if img.get("media_id") != old_media_id]
            images.append({
                "media_id": media_id,
                "url": media_url,
                "alt": alt_text,
            })
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_products SET images = %s WHERE id = %s",
                    (json.dumps(images), product_id),
                )

            from app.services.analysis_refresh import refresh_item_analysis
            refresh_item_analysis(item["store_id"], "product", product_id)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"replaced": True, "media_id": media_id}), item_id),
                )
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

        except Exception as e:
            logger.exception("Placeholder replace failed for product %s", product_id)
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
