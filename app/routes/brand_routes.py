import html as html_lib
import logging
import re
import threading
import time

from flask import Blueprint, jsonify, render_template, request

from app.services.apply_pipeline import ApplyRefused, apply_item_changes, refusal_response
from app.auth import decrypt_value
from app.database import db
from app.services.ai_output_guard import guard_shopify_fields
from app.services.task_runner import finalize_failed, is_failed_result, spawn as spawn_task
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

brand_bp = Blueprint("brands", __name__)


@brand_bp.route("/brands")
def brands_page():
    return render_template("brands.html")


@brand_bp.route("/api/brands/vendors", methods=["GET"])
def list_vendors():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT vendor, SUM(GREATEST(jsonb_array_length(variants), 1))::int as product_count
            FROM synced_products
            WHERE store_id = %s AND vendor IS NOT NULL AND vendor != '' AND status = 'ACTIVE'
            GROUP BY vendor ORDER BY vendor""",
            (store_id,),
        )
        vendors = cur.fetchall()

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT tag FROM generated_collections WHERE store_id = %s AND generation_mode = 'brands' AND status IN ('created', 'merged')",
            (store_id,),
        )
        created_tags = {row["tag"] for row in cur.fetchall()}

    result = []
    for v in vendors:
        vendor_name = v["vendor"]
        brand_tag = f"__brand__{vendor_name}"
        result.append({
            "vendor": vendor_name,
            "product_count": v["product_count"],
            "created": brand_tag in created_tags,
        })

    return jsonify(result)


@brand_bp.route("/api/brands/create", methods=["POST"])
def create_brand_collections():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    vendors = data.get("vendors", [])

    if not store_id or not vendors:
        return jsonify({"error": "store_id and vendors are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, TRUE, 'generate_brand_collections') RETURNING id",
            (len(vendors),),
        )
        bulk_task_id = cur.fetchone()["id"]

    spawn_task("bulk_tasks", bulk_task_id, _execute_brand_collections, bulk_task_id, store_id, vendors)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(vendors)}), 201


def _execute_brand_collections(bulk_task_id, store_id, vendors):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'running' WHERE id = %s",
            (bulk_task_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version, shop_name FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        finalize_failed("bulk_tasks", bulk_task_id, "Store no longer exists")
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    store_domain = store.get("shop_name") or store["domain"].replace(".myshopify.com", "")

    for vendor in vendors:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                (vendor, bulk_task_id),
            )

        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, shopify_id, title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) = LOWER(%s)",
                    (store_id, vendor),
                )
                existing = cur.fetchone()

            if existing:
                with db.get_cursor() as cur:
                    brand_tag = f"__brand__{vendor}"
                    cur.execute(
                        """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status, generation_mode)
                        VALUES (%s, %s, %s, %s, 0, 'merged', 'brands')
                        ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                            shopify_collection_id = EXCLUDED.shopify_collection_id, status = 'merged', generation_mode = 'brands'""",
                        (store_id, brand_tag, existing["id"], existing["shopify_id"]),
                    )
                    cur.execute(
                        "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                        (bulk_task_id,),
                    )
                logger.info("Brand collection '%s' already exists, skipping", vendor)
                time.sleep(0.5)
                continue

            seo_title = f"{vendor} | {store_domain}"
            seo_description = f"Shop our full {vendor} collection."

            result = client.create_smart_collection(
                title=vendor,
                vendor=vendor,
                seo_title=seo_title,
                seo_description=seo_description,
            )
            shopify_id = result["shopify_id"]
            handle = result["handle"]

            # Persist the Shopify id BEFORE the publish step — a failure after
            # creation must not lose the id (re-runs would mint duplicates).
            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO synced_collections (store_id, shopify_id, title, handle, collection_type, products_count, synced_at)
                    VALUES (%s, %s, %s, %s, 'smart', 0, NOW())
                    ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, collection_type = 'smart', synced_at = NOW()
                    RETURNING id""",
                    (store_id, shopify_id, vendor, handle),
                )
                synced_collection_id = cur.fetchone()["id"]

                brand_tag = f"__brand__{vendor}"
                cur.execute(
                    """INSERT INTO generated_collections (store_id, tag, collection_id, shopify_collection_id, products_count, status, generation_mode)
                    VALUES (%s, %s, %s, %s, 0, 'created', 'brands')
                    ON CONFLICT (store_id, tag) DO UPDATE SET collection_id = EXCLUDED.collection_id,
                        shopify_collection_id = EXCLUDED.shopify_collection_id, status = 'created', generation_mode = 'brands'""",
                    (store_id, brand_tag, synced_collection_id, shopify_id),
                )

            try:
                client.publish_to_all_channels(shopify_id)
            except Exception:
                logger.exception("Failed to publish brand collection '%s' to sales channels", vendor)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                    (bulk_task_id,),
                )

            logger.info("Created brand collection '%s' (smart, vendor rule)", vendor)

        except Exception:
            logger.exception("Failed to create brand collection for vendor '%s'", vendor)
            with db.get_cursor() as cur:
                brand_tag = f"__brand__{vendor}"
                cur.execute(
                    """INSERT INTO generated_collections (store_id, tag, status, generation_mode)
                    VALUES (%s, %s, 'failed', 'brands')
                    ON CONFLICT (store_id, tag) DO UPDATE SET status = 'failed', generation_mode = 'brands'""",
                    (store_id, brand_tag),
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


@brand_bp.route("/api/brands/collections", methods=["GET"])
def list_brand_collections():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    _repair_broken_brand_links(store_id)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.id, gc.tag, gc.shopify_collection_id, gc.status, gc.created_at,
                sc.title, sc.handle, sc.products_count,
                COALESCE(NULLIF(sc.image_url, ''), gc.image_url) as image_url,
                sc.description, sc.meta_title, sc.meta_description
            FROM generated_collections gc
            LEFT JOIN synced_collections sc ON gc.collection_id = sc.id
            WHERE gc.store_id = %s AND gc.generation_mode = 'brands' AND gc.status IN ('created', 'merged')
            ORDER BY sc.title""",
            (store_id,),
        )
        collections = cur.fetchall()

    from app.services.seo_analyzer import analyze_collection_seo

    result = []
    for c in collections:
        seo = analyze_collection_seo(dict(c))
        result.append({
            "id": c["id"],
            "tag": c["tag"],
            "title": c["title"] or c["tag"].replace("__brand__", ""),
            "handle": c["handle"],
            "shopify_collection_id": c["shopify_collection_id"],
            "products_count": c["products_count"] or 0,
            "status": c["status"],
            "created_at": str(c["created_at"]) if c["created_at"] else None,
            "image_url": c["image_url"] or None,
            "seo_score": seo.get("score", 0),
        })

    return jsonify(result)


def _repair_broken_brand_links(store_id):
    """Auto-repair brand collections that lost their synced_collections link.
    Only re-links using the known shopify_collection_id — no guessing."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.id, gc.tag, gc.shopify_collection_id
            FROM generated_collections gc
            WHERE gc.store_id = %s AND gc.generation_mode = 'brands'
                AND gc.collection_id IS NULL
                AND gc.shopify_collection_id IS NOT NULL
                AND gc.status IN ('created', 'merged')""",
            (store_id,),
        )
        broken = cur.fetchall()

    if not broken:
        return

    logger.info("Repairing %d broken brand collection links for store %s", len(broken), store_id)

    for row in broken:
        gc_id = row["id"]
        shopify_cid = row["shopify_collection_id"]
        vendor = row["tag"].replace("__brand__", "")

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id FROM synced_collections WHERE store_id = %s AND shopify_id = %s",
                (store_id, shopify_cid),
            )
            existing = cur.fetchone()

        if existing:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE generated_collections SET collection_id = %s WHERE id = %s",
                    (existing["id"], gc_id),
                )
            logger.info("Re-linked brand '%s' gc_id=%s to synced_collection=%s", vendor, gc_id, existing["id"])
        else:
            # Sync removes collections deleted in Shopify, so no mirror row
            # means the collection is gone. Record that once (the brand then
            # shows as having no collection) instead of warning on every load.
            with db.get_cursor() as cur:
                cur.execute("UPDATE generated_collections SET status = 'deleted' WHERE id = %s", (gc_id,))
            logger.info("Brand '%s' gc_id=%s: collection %s no longer exists — marked deleted", vendor, gc_id, shopify_cid)


@brand_bp.route("/api/brands/fix-titles", methods=["POST"])
def fix_brand_collection_titles():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.tag, gc.shopify_collection_id, gc.collection_id,
                sc.title, sc.shopify_id,
                s.domain, s.access_token_enc, s.api_version
            FROM generated_collections gc
            JOIN synced_collections sc ON gc.collection_id = sc.id
            JOIN stores s ON s.id = gc.store_id
            WHERE gc.store_id = %s AND gc.generation_mode = 'brands'
              AND gc.status IN ('created', 'merged')""",
            (store_id,),
        )
        rows = cur.fetchall()

    fixed = 0
    errors = 0
    for row in rows:
        tag = row.get("tag") or ""
        if not tag.startswith("__brand__"):
            continue
        original_name = tag.replace("__brand__", "")
        current_title = row.get("title") or ""

        if current_title == original_name:
            continue

        try:
            client = ShopifyClient(
                row["domain"],
                decrypt_value(row["access_token_enc"]),
                row["api_version"],
            )
            apply_item_changes(
                store_id, "collection", row["collection_id"], {"title": original_name},
                source="brands", change_type="brand_title_fix", client=client,
                allow_dropped_primary=True, stamp=False,
            )
            fixed += 1
            logger.info("Fixed brand collection title: '%s' -> '%s'", current_title, original_name)
        except Exception:
            logger.exception("Failed to fix title for brand '%s'", original_name)
            errors += 1

    return jsonify({"ok": True, "fixed": fixed, "errors": errors, "already_correct": len(rows) - fixed - errors})


@brand_bp.route("/api/brands/collections/<int:gc_id>", methods=["DELETE"])
def delete_brand_collection(gc_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT gc.store_id, gc.shopify_collection_id, gc.collection_id FROM generated_collections gc WHERE gc.id = %s AND gc.generation_mode = 'brands'",
            (gc_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"error": "Brand collection not found"}), 404

    store_id = row["store_id"]
    shopify_id = row["shopify_collection_id"]
    collection_id = row["collection_id"]

    if shopify_id:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
                (store_id,),
            )
            store = cur.fetchone()

        if store:
            try:
                client = ShopifyClient(
                    store["domain"],
                    decrypt_value(store["access_token_enc"]),
                    store["api_version"],
                )
                client.delete_collection(shopify_id)
            except Exception as e:
                if not any(t in str(e).lower() for t in ("does not exist", "not found", "invalid id", "no longer exists")):
                    logger.exception("Failed to delete brand collection from Shopify: %s", shopify_id)
                    return jsonify({"error": f"Shopify delete failed: {e} — nothing was removed locally"}), 502
                logger.info("Collection already gone on Shopify: %s", shopify_id)

    with db.get_cursor() as cur:
        cur.execute("DELETE FROM generated_collections WHERE id = %s", (gc_id,))
        if collection_id:
            cur.execute(
                "DELETE FROM technical_seo_analyses WHERE item_type = 'collection' AND item_id = %s",
                (collection_id,),
            )
            cur.execute("DELETE FROM synced_collections WHERE id = %s", (collection_id,))

    return jsonify({"ok": True})


@brand_bp.route("/api/brands/landing-page", methods=["POST"])
def generate_brand_landing_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version, name, shop_name FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        return jsonify({"error": "Store not found"}), 404

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )
    store_name = store.get("shop_name") or store["name"] or store["domain"].replace(".myshopify.com", "")

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.id, gc.shopify_collection_id, sc.title, sc.handle, sc.products_count,
                COALESCE(NULLIF(sc.image_url, ''), gc.image_url) as image_url
            FROM generated_collections gc
            LEFT JOIN synced_collections sc ON gc.collection_id = sc.id
            WHERE gc.store_id = %s AND gc.generation_mode = 'brands' AND gc.status IN ('created', 'merged')
            ORDER BY sc.title""",
            (store_id,),
        )
        brand_collections = cur.fetchall()

    if not brand_collections:
        return jsonify({"error": "No brand collections found. Create brand collections first."}), 400

    brand_names = [c["title"] for c in brand_collections if c["title"]]
    with db.get_cursor() as cur:
        if brand_names:
            placeholders = ",".join(["%s"] * len(brand_names))
            cur.execute(
                f"""SELECT vendor, product_type, COUNT(*) as cnt
                FROM synced_products
                WHERE store_id = %s AND vendor IN ({placeholders})
                    AND product_type IS NOT NULL AND product_type != ''
                GROUP BY vendor, product_type
                ORDER BY vendor, cnt DESC""",
                [store_id] + brand_names,
            )
            pt_rows = cur.fetchall()
        else:
            pt_rows = []

    vendor_product_types = {}
    for row in pt_rows:
        v = row["vendor"]
        if v not in vendor_product_types:
            vendor_product_types[v] = []
        if len(vendor_product_types[v]) < 3:
            vendor_product_types[v].append(row["product_type"])

    with db.get_cursor() as cur:
        if brand_names:
            placeholders = ",".join(["%s"] * len(brand_names))
            cur.execute(
                f"""SELECT vendor, SUM(GREATEST(jsonb_array_length(variants), 1)) as product_count
                FROM synced_products
                WHERE store_id = %s AND vendor IN ({placeholders}) AND status = 'ACTIVE'
                GROUP BY vendor""",
                [store_id] + brand_names,
            )
            vendor_counts = {row["vendor"]: int(row["product_count"]) for row in cur.fetchall()}
        else:
            vendor_counts = {}

    for c in brand_collections:
        actual_count = vendor_counts.get(c["title"], 0)
        if actual_count > (c["products_count"] or 0):
            c["products_count"] = actual_count

    body_html = _build_brands_landing_html(brand_collections, store_name, vendor_product_types)
    top_brand_names = [c["title"] for c in sorted(brand_collections, key=lambda c: c["products_count"] or 0, reverse=True)[:3] if c["title"]]
    top_str = ", ".join(top_brand_names)

    seo_title = f"Shop by Brand | {store_name}"
    if len(seo_title) > 60:
        seo_title = seo_title[:57] + "..."
    seo_description = f"Browse all {len(brand_collections)} brands at {store_name} including {top_str}. Shop our complete brand collections."
    if len(seo_description) > 160:
        seo_description = seo_description[:157] + "..."

    landing_title = f"Shop by Brand | {store_name}"

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_page_id, page_handle FROM brand_landing_pages WHERE store_id = %s",
            (store_id,),
        )
        existing = cur.fetchone()

    try:
        if existing and existing["shopify_page_id"]:
            client.update_page(existing["shopify_page_id"], {
                "title": landing_title,
                "body": body_html,
                "seo_title": seo_title,
                "seo_description": seo_description,
            })
            shopify_page_id = existing["shopify_page_id"]
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE brand_landing_pages SET page_title = %s, brands_count = %s, last_generated_at = NOW() WHERE id = %s",
                    (landing_title, len(brand_collections), existing["id"]),
                )
            logger.info("Updated existing Brands landing page (Shopify Page)")
        else:
            result = client.create_page(
                title=landing_title,
                body_html=body_html,
                handle="brands",
                seo_title=seo_title,
                seo_description=seo_description,
            )
            shopify_page_id = result["shopify_id"]
            handle = result["handle"]

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO brand_landing_pages (store_id, shopify_page_id, page_handle, page_title, brands_count, last_generated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id) DO UPDATE SET shopify_page_id = EXCLUDED.shopify_page_id,
                        page_handle = EXCLUDED.page_handle, page_title = EXCLUDED.page_title,
                        brands_count = EXCLUDED.brands_count, last_generated_at = NOW()""",
                    (store_id, shopify_page_id, handle, landing_title, len(brand_collections)),
                )
            logger.info("Created Brands landing page (Shopify Page): %s", handle)

        return jsonify({"ok": True, "shopify_page_id": shopify_page_id, "brands_count": len(brand_collections)})
    except Exception as e:
        logger.exception("Failed to create/update Brands landing page")
        return jsonify({"error": str(e)}), 500


@brand_bp.route("/api/brands/landing-page/status", methods=["GET"])
def brand_landing_page_status():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT shopify_page_id, page_handle, page_title, brands_count, last_generated_at FROM brand_landing_pages WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()

    if not row or not row["shopify_page_id"]:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "shopify_page_id": row["shopify_page_id"],
        "handle": row["page_handle"],
        "url": f"/pages/{row['page_handle']}",
        "brands_count": row["brands_count"],
    })


def _build_brands_landing_html(brand_collections, store_name, vendor_product_types=None):
    safe_store = html_lib.escape(store_name)
    total = len(brand_collections)
    vendor_product_types = vendor_product_types or {}
    total_products_exact = sum(c["products_count"] or 0 for c in brand_collections)
    if total_products_exact >= 1000:
        total_products_rounded = (total_products_exact // 100) * 100
        total_products_text = f"over {total_products_rounded:,}"
    else:
        total_products_text = f"{total_products_exact:,}"

    top_brands = sorted(brand_collections, key=lambda c: c["products_count"] or 0, reverse=True)[:5]
    top_names_list = [html_lib.escape(c["title"]) for c in top_brands if c["title"]]

    all_product_types = set()
    for pts in vendor_product_types.values():
        for pt in pts[:2]:
            all_product_types.add(pt)
    top_categories = sorted(all_product_types)[:6]
    categories_text = ", ".join(html_lib.escape(c) for c in top_categories) if top_categories else ""

    groups = {}
    for c in brand_collections:
        title = (c["title"] or "").strip()
        if not title:
            continue
        first = title[0].upper()
        letter = first if first.isalpha() else "#"
        groups.setdefault(letter, []).append(c)

    active_letters = set(groups.keys())

    # Minimal CSS — only hover effects and responsive breakpoint (inline styles handle the rest)
    css = """
.bl-card:hover{transform:translateY(-3px);box-shadow:0 8px 24px rgba(0,0,0,.1);border-color:#ccc}
.bl-alpha:hover{background:#e0e0e0;color:#000}
@media(max-width:600px){
  .bl-grid{grid-template-columns:repeat(2,1fr)!important;gap:12px!important}
  .bl-card-img{height:120px!important}
  .bl-no-img{height:120px!important;font-size:20px!important}
  .bl-alpha{width:28px!important;height:28px!important;font-size:11px!important}
}
"""

    all_letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["#"]
    alpha_links = ""
    for letter in all_letters:
        anchor = "num" if letter == "#" else letter
        if letter in active_letters:
            alpha_links += (
                f'<a href="#brand-{anchor}" class="bl-alpha" aria-label="Jump to brands starting with {letter}" '
                f'style="display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:8px;'
                f'font-size:13px;font-weight:600;text-decoration:none;color:#111;background:#f0f0f0;transition:background .15s">'
                f'{letter}</a>\n'
            )
        else:
            alpha_links += (
                f'<span aria-hidden="true" style="display:inline-flex;align-items:center;justify-content:center;'
                f'width:34px;height:34px;border-radius:8px;font-size:13px;font-weight:600;color:#ccc">{letter}</span>\n'
            )

    palette = ["#2d3748", "#1a365d", "#744210", "#22543d", "#553c9a", "#9b2c2c", "#2c5282", "#285e61", "#6b46c1", "#c05621"]

    sorted_keys = sorted(groups.keys(), key=lambda k: (k == "#", k))
    sections_html = ""
    item_position = 1
    for letter in sorted_keys:
        anchor = "num" if letter == "#" else letter
        display_letter = "0-9" if letter == "#" else letter
        cards = ""
        for c in groups[letter]:
            title = html_lib.escape(c["title"] or "")
            handle = html_lib.escape(c["handle"] or "")
            image_url = c.get("image_url") or ""
            count = c["products_count"] or 0
            count_text = f"{count} product{'s' if count != 1 else ''}" if count > 0 else ""

            product_types = vendor_product_types.get(c["title"], [])
            # Compose from RAW values, truncate, then escape exactly once —
            # double-escaping rendered "H&M" as "H&amp;amp;M", and truncating
            # an already-escaped string could slice an entity in half.
            raw_pt_text = ", ".join(product_types[:2])
            if raw_pt_text:
                raw_alt = f"Shop {c['title'] or ''} {raw_pt_text} - {count_text} at {store_name}"
            else:
                raw_alt = f"Shop {c['title'] or ''} collection - {count_text} at {store_name}"
            alt_text = html_lib.escape(raw_alt[:125])

            card_categories = ""
            if product_types:
                cats = ", ".join(html_lib.escape(pt) for pt in product_types[:3])
                card_categories = f'<p style="font-size:12px;color:#999;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{cats}</p>'

            if image_url:
                safe_url = html_lib.escape(image_url)
                img_html = (
                    f'<img class="bl-card-img" src="{safe_url}" alt="{alt_text}" width="260" height="150" loading="lazy" decoding="async" '
                    f'style="width:100%;height:150px;object-fit:cover;display:block;background:#f5f5f5;border-radius:0">'
                )
            else:
                bg_color = palette[item_position % len(palette)]
                initials = "".join(w[0].upper() for w in (c["title"] or "B").split()[:2])
                img_html = (
                    f'<div class="bl-no-img" role="img" aria-label="{alt_text}" '
                    f'style="width:100%;height:150px;display:flex;align-items:center;justify-content:center;'
                    f'background:{bg_color};font-size:32px;font-weight:700;color:rgba(255,255,255,.85);letter-spacing:3px">'
                    f'{html_lib.escape(initials)}</div>'
                )

            cards += (
                f'<a href="/collections/{handle}" class="bl-card" title="Shop {title} products at {safe_store}" '
                f'itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem" '
                f'style="display:block;border-radius:10px;overflow:hidden;border:1px solid #e5e5e5;background:#fff;'
                f'text-decoration:none;color:inherit;transition:transform .2s,box-shadow .2s">'
                f'<meta itemprop="position" content="{item_position}">'
                f'<meta itemprop="url" content="/collections/{handle}">'
                f'<meta itemprop="name" content="{title}">'
                f'{img_html}'
                f'<div style="padding:14px 16px;text-align:center">'
                f'<h3 style="font-size:15px;font-weight:600;line-height:1.3;color:#111;margin:0 0 4px">{title}</h3>'
                f'<p style="font-size:13px;color:#888;margin:0 0 2px">{count_text}</p>'
                f'{card_categories}'
                f'</div></a>\n'
            )
            item_position += 1

        sections_html += (
            f'<section id="brand-{anchor}" style="margin-bottom:40px;scroll-margin-top:70px">'
            f'<h2 style="font-size:22px;font-weight:700;color:#111;margin:0 0 16px;padding-bottom:8px;border-bottom:2px solid #e5e5e5">{display_letter}</h2>'
            f'<div class="bl-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:18px">'
            f'{cards}</div></section>\n'
        )

    top_names_linked = ""
    for b in top_brands:
        if b["title"] and b["handle"]:
            t = html_lib.escape(b["title"])
            h = html_lib.escape(b["handle"])
            top_names_linked += f'<a href="/collections/{h}" style="color:#1a73e8;text-decoration:none">{t}</a>, '
    top_names_linked = top_names_linked.rstrip(", ")

    intro_p1 = f'<p style="font-size:15px;line-height:1.8;color:#444;margin:0 0 14px">Welcome to the complete brand directory at <strong>{safe_store}</strong>. We proudly carry <strong>{total} brands</strong> with <strong>{total_products_text} products</strong> available to shop. Whether you are looking for a specific brand or exploring something new, use the alphabetical index below to quickly find what you need.</p>'

    intro_p2 = ""
    if top_names_linked:
        intro_p2 = f'<p style="font-size:15px;line-height:1.8;color:#444;margin:0 0 14px">Our most popular brands include {top_names_linked} and many more. Each brand collection is carefully curated so you can browse every product from your favorite manufacturers in one place.</p>'

    intro_p3 = ""
    if categories_text:
        intro_p3 = f'<p style="font-size:15px;line-height:1.8;color:#444;margin:0 0 14px">Our brand collections span product categories including <strong>{categories_text}</strong>. Select any brand below to view their complete product lineup, read detailed descriptions, and find the right products for your needs.</p>'

    faq_q_style = 'style="font-size:16px;font-weight:600;color:#222;margin:0 0 6px"'
    faq_a_style = 'style="font-size:14px;line-height:1.7;color:#555;margin:0"'

    faq_items = f"""<div style="margin-bottom:20px" itemscope itemprop="mainEntity" itemtype="https://schema.org/Question">
  <h3 {faq_q_style} itemprop="name">How many brands does {safe_store} carry?</h3>
  <div itemscope itemprop="acceptedAnswer" itemtype="https://schema.org/Answer">
    <p {faq_a_style} itemprop="text">{safe_store} currently carries {total} brands with {total_products_text} products available. Our brand selection is regularly updated as we add new products and manufacturers.</p>
  </div>
</div>
<div style="margin-bottom:20px" itemscope itemprop="mainEntity" itemtype="https://schema.org/Question">
  <h3 {faq_q_style} itemprop="name">How do I find a specific brand?</h3>
  <div itemscope itemprop="acceptedAnswer" itemtype="https://schema.org/Answer">
    <p {faq_a_style} itemprop="text">Use the A-Z alphabetical navigation at the top of this page to quickly jump to brands starting with a specific letter. You can also scroll through all {total} brands organized alphabetically.</p>
  </div>
</div>"""

    if top_names_list:
        popular_names = ", ".join(top_names_list[:4])
        last_name = top_names_list[4] if len(top_names_list) > 4 else ""
        popular_answer = popular_names
        if last_name:
            popular_answer += f", and {last_name}"
        faq_items += f"""
<div style="margin-bottom:20px" itemscope itemprop="mainEntity" itemtype="https://schema.org/Question">
  <h3 {faq_q_style} itemprop="name">What are the most popular brands at {safe_store}?</h3>
  <div itemscope itemprop="acceptedAnswer" itemtype="https://schema.org/Answer">
    <p {faq_a_style} itemprop="text">Some of our most popular brands by product selection include {popular_answer}. Visit each brand page to see their full product catalog.</p>
  </div>
</div>"""

    if categories_text:
        faq_items += f"""
<div style="margin-bottom:20px" itemscope itemprop="mainEntity" itemtype="https://schema.org/Question">
  <h3 {faq_q_style} itemprop="name">What product categories are available?</h3>
  <div itemscope itemprop="acceptedAnswer" itemtype="https://schema.org/Answer">
    <p {faq_a_style} itemprop="text">Our brands cover a variety of product categories including {categories_text}. Each brand collection page shows all available products from that manufacturer.</p>
  </div>
</div>"""

    from app.services.schema_generator import faq_markup_enabled, strip_faq_microdata

    faq_block = f"""<div style="margin-top:48px;padding-top:32px;border-top:1px solid #e5e5e5" itemscope itemtype="https://schema.org/FAQPage">
    <h2 style="font-size:22px;font-weight:700;color:#111;margin:0 0 20px">Frequently Asked Questions</h2>
    {faq_items}
  </div>"""
    if not faq_markup_enabled():
        faq_block = strip_faq_microdata(faq_block)

    breadcrumb_style = 'style="display:flex;align-items:center;gap:6px;font-size:13px;color:#666;margin:0 0 20px;padding:0;list-style:none"'

    return f"""<style>{css}</style>
<div style="max-width:1200px;margin:0 auto;padding:0" itemscope itemtype="https://schema.org/CollectionPage">
  <meta itemprop="name" content="Shop by Brand - {safe_store}">
  <meta itemprop="description" content="Browse all {total} brands and {total_products_text} products at {safe_store}">
  <meta itemprop="numberOfItems" content="{total}">
  <ol {breadcrumb_style} itemscope itemtype="https://schema.org/BreadcrumbList">
    <li style="display:inline" itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
      <a itemprop="item" href="/" style="color:#1a73e8;text-decoration:none"><span itemprop="name">Home</span></a>
      <meta itemprop="position" content="1">
      <span style="margin-left:6px;color:#ccc">/</span>
    </li>
    <li style="display:inline;color:#333;font-weight:500" itemprop="itemListElement" itemscope itemtype="https://schema.org/ListItem">
      <span itemprop="name">Brands</span>
      <meta itemprop="position" content="2">
    </li>
  </ol>
  <div style="margin-bottom:32px">
    {intro_p1}
    {intro_p2}
    {intro_p3}
  </div>
  <nav style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:32px;padding:14px 0;border-top:1px solid #e5e5e5;border-bottom:1px solid #e5e5e5;position:sticky;top:0;background:#fff;z-index:10" aria-label="Browse brands by letter">
    {alpha_links}
  </nav>
  <div itemscope itemtype="https://schema.org/ItemList">
    <meta itemprop="itemListOrder" content="https://schema.org/ItemListOrderAscending">
    <meta itemprop="numberOfItems" content="{total}">
    {sections_html}
  </div>
  {faq_block}
</div>
<script>
document.querySelectorAll('.bl-alpha').forEach(function(a){{
  a.addEventListener('click',function(e){{
    e.preventDefault();
    var t=document.querySelector(this.getAttribute('href'));
    if(t)t.scrollIntoView({{behavior:'smooth',block:'start'}});
  }});
}});
</script>"""


def _verify_brand_logo(image_bytes, brand_name):
    try:
        import base64
        import requests as http_req
        from app.ai_agent import _get_ai_config
        from app.services.image_generator import _resize_for_ai

        config = _get_ai_config()
        model = config.get("model") or "openai/gpt-4o-mini"

        resized = _resize_for_ai(image_bytes, max_size=256)
        b64 = base64.b64encode(resized).decode("utf-8")

        resp = http_req.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f'Look at this image. I need to verify if this is a logo or branding for "{brand_name}". '
                                f'Read any text visible in the image. Does it match or clearly relate to the brand "{brand_name}"? '
                                f'Reply with ONLY "YES" or "NO". Nothing else.'
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }],
                "max_tokens": 10,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip().upper()
        logger.info("Brand logo verification for '%s': AI answered '%s'", brand_name, answer)
        return answer.startswith("YES")
    except Exception as e:
        logger.warning("Brand logo verification failed for '%s': %s — allowing image", brand_name, e)
        return True


@brand_bp.route("/api/brands/collections/<int:gc_id>/generate-logo", methods=["POST"])
def generate_brand_logo(gc_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.store_id, gc.shopify_collection_id, gc.collection_id,
                sc.title, sc.handle
            FROM generated_collections gc
            LEFT JOIN synced_collections sc ON gc.collection_id = sc.id
            WHERE gc.id = %s AND gc.generation_mode = 'brands'""",
            (gc_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"error": "Brand collection not found"}), 404

    store_id = row["store_id"]
    shopify_collection_id = row["shopify_collection_id"]
    brand_name = row["title"] or ""

    if not brand_name:
        return jsonify({"error": "Brand name not found"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        return jsonify({"error": "Store not found"}), 404

    req_data = request.get_json(silent=True) or {}
    search_keywords = req_data.get("search_keywords", "").strip() or None
    custom_prompt = req_data.get("custom_prompt", "").strip() or None
    result = _find_and_upload_brand_logo(store_id, brand_name, shopify_collection_id, row["collection_id"], store, gc_id=gc_id, search_keywords=search_keywords, custom_prompt=custom_prompt)
    if "error" in result:
        return jsonify({"error": result["error"]}), result.get("status", 500)
    return jsonify(result)


@brand_bp.route("/api/brands/collections/bulk-logo", methods=["POST"])
def bulk_generate_brand_logos():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    gc_ids = data.get("gc_ids", [])

    if not store_id or not gc_ids:
        return jsonify({"error": "store_id and gc_ids are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, TRUE, 'brand_logos') RETURNING id",
            (len(gc_ids),),
        )
        bulk_task_id = cur.fetchone()["id"]

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_brand_logos, bulk_task_id, store_id, gc_ids)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(gc_ids)}), 201


def _execute_bulk_brand_logos(bulk_task_id, store_id, gc_ids):
    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        finalize_failed("bulk_tasks", bulk_task_id, "Store no longer exists")
        return

    for gc_id in gc_ids:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT gc.store_id, gc.shopify_collection_id, gc.collection_id,
                    sc.title, sc.handle
                FROM generated_collections gc
                LEFT JOIN synced_collections sc ON gc.collection_id = sc.id
                WHERE gc.id = %s AND gc.generation_mode = 'brands'
                  AND gc.store_id = %s""",
                (gc_id, store_id),
            )
            row = cur.fetchone()

        if not row or not row["title"]:
            with db.get_cursor() as cur:
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
            continue

        brand_name = row["title"]
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (brand_name, bulk_task_id))

        try:
            result = _find_and_upload_brand_logo(
                store_id, brand_name, row["shopify_collection_id"], row["collection_id"], store, gc_id=gc_id
            )
            if "error" in result:
                logger.warning("Bulk logo failed for '%s': %s", brand_name, result["error"])
                with db.get_cursor() as cur:
                    cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
            else:
                with db.get_cursor() as cur:
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))
        except Exception:
            logger.exception("Bulk logo failed for '%s'", brand_name)
            with db.get_cursor() as cur:
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

        time.sleep(1)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


def _find_and_upload_brand_logo(store_id, brand_name, shopify_collection_id, collection_id, store, gc_id=None, search_keywords=None, custom_prompt=None):
    import requests as http_req
    from app.services.serp_scraper import brave_image_search
    from app.services.image_generator import generate_brand_logo_image

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT product_type, COUNT(*) as cnt FROM synced_products
            WHERE store_id = %s AND vendor = %s AND product_type IS NOT NULL AND product_type != ''
            GROUP BY product_type ORDER BY cnt DESC LIMIT 1""",
            (store_id, brand_name),
        )
        pt_row = cur.fetchone()
    product_type = pt_row["product_type"] if pt_row else ""

    if product_type:
        search_queries = [
            f"{brand_name} {product_type} brand logo",
            f"{brand_name} logo {product_type}",
            f"{brand_name} brand logo",
        ]
    else:
        search_queries = [
            f"{brand_name} brand logo",
            f"{brand_name} logo official",
            f"{brand_name} logo png",
        ]

    if search_keywords:
        search_queries.insert(0, f"{brand_name} {search_keywords} logo")

    logo_image_bytes = None
    for query in search_queries:
        results = brave_image_search(query, count=5)
        if not results:
            continue

        for img_result in results:
            img_url = img_result.get("url") or img_result.get("thumbnail")
            if not img_url:
                continue
            try:
                resp = http_req.get(img_url, timeout=15, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                resp.raise_for_status()
                if len(resp.content) < 1000:
                    continue

                if _verify_brand_logo(resp.content, brand_name):
                    logo_image_bytes = resp.content
                    logger.info("Verified brand logo for '%s' from: %s (%d bytes)", brand_name, img_url, len(resp.content))
                    break
                else:
                    logger.info("Logo from %s rejected — does not match brand '%s'", img_url, brand_name)
            except Exception:
                logger.warning("Failed to download logo image from: %s", img_url)
                continue

        if logo_image_bytes:
            break

    if not logo_image_bytes:
        return {"error": f"Could not find a verified logo image for '{brand_name}' via search", "status": 404}

    ai_result = generate_brand_logo_image(brand_name, logo_image_bytes, custom_prompt=custom_prompt)
    if not ai_result:
        return {"error": "AI image generation failed", "status": 500}

    image_bytes, filename, alt_text, width, height = ai_result

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    try:
        mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
        staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
        resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime, filename=filename)
        result = client.update_collection_image(shopify_collection_id, resource_url, alt_text)

        final_url = resource_url
        if result and result.get("image"):
            final_url = result["image"].get("url") or resource_url

        if collection_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_collections SET image_url = %s WHERE id = %s",
                    (final_url, collection_id),
                )

        if gc_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE generated_collections SET image_url = %s WHERE id = %s",
                    (final_url, gc_id),
                )

        return {"ok": True, "image_url": final_url}
    except Exception as e:
        logger.exception("Failed to upload brand logo image for '%s'", brand_name)
        return {"error": str(e), "status": 500}


# ==================== Brand SEO Optimization ====================


@brand_bp.route("/api/brands/search-collections", methods=["GET"])
def search_collections_for_interlink():
    store_id = request.args.get("store_id")
    q = request.args.get("q", "").strip()
    exclude_id = request.args.get("exclude_id", type=int)
    if not store_id or not q:
        return jsonify([])
    pattern = f"%{q}%"
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, title, handle, products_count FROM synced_collections
            WHERE store_id = %s AND handle IS NOT NULL AND handle != ''
            AND (title ILIKE %s OR handle ILIKE %s)
            AND (%s IS NULL OR id != %s)
            ORDER BY title LIMIT 15""",
            (store_id, pattern, pattern, exclude_id, exclude_id),
        )
        results = [dict(r) for r in cur.fetchall()]
    return jsonify(results)


@brand_bp.route("/api/brands/collections/<int:gc_id>/seo", methods=["GET"])
def get_brand_collection_seo(gc_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.id as gc_id, gc.store_id, gc.shopify_collection_id, gc.collection_id, gc.tag,
                sc.id, sc.title, sc.handle, sc.description, sc.meta_title, sc.meta_description,
                sc.products_count, sc.image_url, sc.shopify_id,
                COALESCE(s.shop_name, s.name) as store_name
            FROM generated_collections gc
            JOIN synced_collections sc ON gc.collection_id = sc.id
            JOIN stores s ON s.id = gc.store_id
            WHERE gc.id = %s AND gc.generation_mode = 'brands'""",
            (gc_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"error": "Brand collection not found"}), 404

    from app.services.seo_analyzer import analyze_collection_seo
    seo = analyze_collection_seo(dict(row))

    collection_id = row["collection_id"]
    store_id = row["store_id"]
    tag = row.get("tag") or ""
    vendor_name = tag.replace("__brand__", "") if tag.startswith("__brand__") else (row["title"] or "")

    suggested_keywords = _get_brand_keyword_suggestions(store_id, vendor_name)
    related_collections = _get_interlink_suggestions(store_id, collection_id, vendor_name)

    return jsonify({
        "gc_id": gc_id,
        "collection_id": collection_id,
        "store_id": store_id,
        "store_name": row["store_name"],
        "shopify_id": row["shopify_id"],
        "title": row["title"],
        "handle": row["handle"],
        "description": row["description"],
        "meta_title": row["meta_title"],
        "meta_description": row["meta_description"],
        "products_count": row["products_count"] or 0,
        "image_url": row["image_url"],
        "seo_score": seo.get("score", 0),
        "seo_breakdown": seo.get("breakdown", {}),
        "suggested_keywords": suggested_keywords,
        "related_collections": related_collections,
    })


@brand_bp.route("/api/brands/collections/<int:gc_id>/optimize", methods=["POST"])
def optimize_brand_seo(gc_id):
    data = request.get_json(silent=True) or {}
    keywords = data.get("keywords", [])
    interlink_collections = data.get("interlink_collections", [])

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.collection_id, gc.store_id
            FROM generated_collections gc
            WHERE gc.id = %s AND gc.generation_mode = 'brands'""",
            (gc_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"error": "Brand collection not found"}), 404

    collection_id = row["collection_id"]
    store_id = row["store_id"]

    import json as json_lib
    task_input = {
        "collection_id": collection_id,
        "keywords": keywords,
        "interlink_collections": interlink_collections,
    }

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'brand_seo', %s) RETURNING id",
            (store_id, json_lib.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]

    def _run_brand_task(tid):
        import json as j
        from app.ai_agent import run_task
        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (tid,))
        try:
            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (tid,))
                task = cur.fetchone()
            result = run_task(task["task_type"], task["input"], task.get("store_id"))
            from app.services.task_runner import finish_task
            finish_task(tid, result)
        except Exception as e:
            logger.exception("Brand SEO task failed")
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), tid),
                )

    spawn_task("ai_tasks", task_id, _run_brand_task, task_id)

    return jsonify({"ok": True, "task_id": task_id}), 201


@brand_bp.route("/api/brands/collections/<int:gc_id>/apply", methods=["POST"])
def apply_brand_seo(gc_id):
    data = request.get_json(silent=True) or {}

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT gc.collection_id, gc.shopify_collection_id, gc.store_id,
                sc.shopify_id, s.domain, s.access_token_enc, s.api_version
            FROM generated_collections gc
            JOIN synced_collections sc ON gc.collection_id = sc.id
            JOIN stores s ON s.id = gc.store_id
            WHERE gc.id = %s AND gc.generation_mode = 'brands'""",
            (gc_id,),
        )
        row = cur.fetchone()

    if not row:
        return jsonify({"error": "Brand collection not found"}), 404

    try:
        client = ShopifyClient(
            row["domain"],
            decrypt_value(row["access_token_enc"]),
            row["api_version"],
        )

        updated_fields = []
        fields = {}
        if data.get("description_html"):
            fields["description_html"] = data["description_html"]
            updated_fields.append("description_html")
        if data.get("meta_title"):
            fields["meta_title"] = data["meta_title"]
            updated_fields.append("meta_title")
        if data.get("meta_description"):
            fields["meta_description"] = data["meta_description"]
            updated_fields.append("meta_description")

        guard_shopify_fields(fields)

        collection_id = row["collection_id"]
        try:
            applied = apply_item_changes(
                row["store_id"], "collection", collection_id,
                {"body": fields.get("description_html"), "meta_title": fields.get("meta_title"),
             "meta_description": fields.get("meta_description")},
                source="brands", change_type="brand_optimize", client=client,
                allow_dropped_blocks=bool(data.get("force")), allow_dropped_primary=bool(data.get("force")),
            )
        except ApplyRefused as refused:
            return refusal_response(refused)

        names = {"body": "description_html"}
        return jsonify({"ok": True, "updated_fields": [names.get(k, k) for k in applied["pushed"]]})

    except Exception as e:
        logger.exception("Failed to apply brand SEO changes")
        return jsonify({"error": str(e)}), 500


@brand_bp.route("/api/brands/bulk-optimize", methods=["POST"])
def bulk_optimize_brand_seo():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    gc_ids = data.get("gc_ids", [])
    auto_apply = data.get("auto_apply", True)

    if not store_id or not gc_ids:
        return jsonify({"error": "store_id and gc_ids are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, %s, 'brand_seo') RETURNING id",
            (len(gc_ids), auto_apply),
        )
        bulk_task_id = cur.fetchone()["id"]

    spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_brand_optimize, bulk_task_id, store_id, gc_ids, auto_apply)

    return jsonify({"ok": True, "bulk_task_id": bulk_task_id, "total": len(gc_ids)}), 201


def _execute_bulk_brand_optimize(bulk_task_id, store_id, gc_ids, auto_apply):
    import json as json_lib
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (bulk_task_id,))

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s",
            (store_id,),
        )
        store = cur.fetchone()

    if not store:
        finalize_failed("bulk_tasks", bulk_task_id, "Store no longer exists")
        return

    client = ShopifyClient(
        store["domain"],
        decrypt_value(store["access_token_enc"]),
        store["api_version"],
    )

    for gc_id in gc_ids:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT gc.collection_id, gc.tag, sc.title, sc.handle, sc.shopify_id
                FROM generated_collections gc
                JOIN synced_collections sc ON gc.collection_id = sc.id
                WHERE gc.id = %s AND gc.generation_mode = 'brands'
                  AND gc.store_id = %s""",
                (gc_id, store_id),
            )
            row = cur.fetchone()

        if not row:
            with db.get_cursor() as cur:
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
            continue

        tag = row.get("tag") or ""
        vendor_name = tag.replace("__brand__", "") if tag.startswith("__brand__") else (row["title"] or "")
        collection_id = row["collection_id"]

        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (vendor_name, bulk_task_id))

        try:
            keywords = _get_brand_keyword_suggestions(store_id, vendor_name)
            keyword_list = [k["keyword"] if isinstance(k, dict) else k for k in keywords[:5]]

            interlinks = _get_interlink_suggestions(store_id, collection_id, vendor_name)
            interlink_list = interlinks[:3]

            task_input = {
                "collection_id": collection_id,
                "keywords": keyword_list,
                "interlink_collections": interlink_list,
            }

            result = run_task("brand_seo", task_input, store_id)

            item_error = None
            if is_failed_result(result):
                item_error = ((result.get("error") if isinstance(result, dict) else None)
                              or "AI returned unparsable output")

            if auto_apply and not item_error:
                fields = {}
                if result.get("optimized_description"):
                    fields["description_html"] = result["optimized_description"]
                if result.get("optimized_meta_title"):
                    fields["meta_title"] = result["optimized_meta_title"]
                if result.get("optimized_meta_description"):
                    fields["meta_description"] = result["optimized_meta_description"]

                guard_shopify_fields(fields)

                if fields:
                    try:
                        apply_item_changes(
                            store_id, "collection", collection_id,
                            {"body": fields.get("description_html"), "meta_title": fields.get("meta_title"),
                             "meta_description": fields.get("meta_description")},
                            source="brands", change_type="brand_optimize", client=client,
                        )
                    except Exception as apply_err:
                        item_error = f"Apply to Shopify failed: {apply_err}"
                        logger.exception("Failed to auto-apply brand SEO for '%s'", vendor_name)

            with db.get_cursor() as cur:
                if item_error:
                    cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))
                else:
                    cur.execute("UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s", (bulk_task_id,))

        except Exception:
            logger.exception("Failed to optimize brand SEO for '%s'", vendor_name)
            with db.get_cursor() as cur:
                cur.execute("UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s", (bulk_task_id,))

        time.sleep(0.5)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
            (bulk_task_id,),
        )


def _get_brand_keyword_suggestions(store_id, vendor_name):
    if not vendor_name:
        return []

    keywords = []

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT product_type, COUNT(*) as cnt FROM synced_products
            WHERE store_id = %s AND vendor = %s AND product_type IS NOT NULL AND product_type != ''
            GROUP BY product_type ORDER BY cnt DESC LIMIT 5""",
            (store_id, vendor_name),
        )
        product_types = [r["product_type"] for r in cur.fetchall()]

    keywords.append({"keyword": vendor_name, "source": "brand_name"})
    for pt in product_types:
        keywords.append({"keyword": f"{vendor_name} {pt}", "source": "brand_product_type"})

    try:
        from app.services.serp_scraper import brave_search
        search_queries = []
        if product_types:
            search_queries.append(f"{vendor_name} {product_types[0]}")
        search_queries.append(f"{vendor_name} products")

        for query in search_queries[:2]:
            results = brave_search(query, count=5)
            if not results:
                continue

            word_freq = {}
            stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
                          "for", "of", "with", "by", "is", "it", "from", "as", "are",
                          "was", "be", "this", "that", "not", "your", "our", "you", "we",
                          "all", "can", "will", "has", "have", "more", "about", "how",
                          "what", "which", "best", "top", "buy", "shop", "get", "new"}
            for r in results:
                text = f"{r.get('title', '')} {r.get('description', '')}".lower()
                words = re.findall(r'[a-z]+(?:\s+[a-z]+)?', text)
                for w in words:
                    w = w.strip()
                    if len(w) > 2 and w not in stop_words and vendor_name.lower() not in w:
                        word_freq[w] = word_freq.get(w, 0) + 1

            sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
            for word, freq in sorted_words[:5]:
                if freq >= 2:
                    kw = f"{vendor_name} {word}"
                    if not any(k.get("keyword", "").lower() == kw.lower() for k in keywords):
                        keywords.append({"keyword": kw, "source": "serp_research"})

    except Exception:
        logger.exception("Brave search keyword suggestions failed for brand '%s'", vendor_name)

    seen = set()
    deduped = []
    for k in keywords:
        kw_lower = k["keyword"].lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            deduped.append(k)

    return deduped[:15]


def _get_interlink_suggestions(store_id, collection_id, vendor_name=None):
    suggestions = []

    try:
        with db.get_cursor() as cur:
            if not vendor_name:
                cur.execute(
                    "SELECT tag FROM generated_collections WHERE collection_id = %s AND generation_mode = 'brands' LIMIT 1",
                    (collection_id,),
                )
                tag_row = cur.fetchone()
                if tag_row and tag_row["tag"] and tag_row["tag"].startswith("__brand__"):
                    vendor_name = tag_row["tag"].replace("__brand__", "")
                else:
                    cur.execute("SELECT title FROM synced_collections WHERE id = %s", (collection_id,))
                    title_row = cur.fetchone()
                    vendor_name = title_row["title"] if title_row else ""

            cur.execute(
                """SELECT product_type FROM synced_products
                WHERE store_id = %s AND vendor = %s AND product_type IS NOT NULL AND product_type != ''
                GROUP BY product_type ORDER BY COUNT(*) DESC LIMIT 5""",
                (store_id, vendor_name),
            )
            brand_product_types = {r["product_type"].lower() for r in cur.fetchall()}

            cur.execute(
                """SELECT sc.id, sc.title, sc.handle, sc.products_count
                FROM synced_collections sc
                WHERE sc.store_id = %s AND sc.id != %s
                  AND sc.handle IS NOT NULL AND sc.handle != ''
                ORDER BY sc.products_count DESC NULLS LAST""",
                (store_id, collection_id),
            )
            other_collections = cur.fetchall()

        for c in other_collections:
            coll_title_lower = (c["title"] or "").lower()
            reason = None

            for pt in brand_product_types:
                if pt in coll_title_lower:
                    reason = f"Shared product type: {pt}"
                    break

            # vendor_name can be empty ("" in x is always True) — never emit
            # a blank "Related to brand:" suggestion.
            if not reason and vendor_name and vendor_name.lower() in coll_title_lower:
                reason = f"Related to brand: {vendor_name}"

            if reason:
                suggestions.append({
                    "id": c["id"],
                    "title": c["title"],
                    "handle": c["handle"],
                    "relevance_reason": reason,
                })

            if len(suggestions) >= 10:
                break

        if len(suggestions) < 5:
            for c in other_collections:
                if len(suggestions) >= 10:
                    break
                if any(s["id"] == c["id"] for s in suggestions):
                    continue
                suggestions.append({
                    "id": c["id"],
                    "title": c["title"],
                    "handle": c["handle"],
                    "relevance_reason": "Other store collection",
                })

    except Exception:
        logger.exception("Failed to get interlink suggestions for brand collection")

    return suggestions[:10]
