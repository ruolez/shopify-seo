import json
import logging
import threading
import time

from app.auth import decrypt_value
from app.database import db
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)


def sync_store(store_id):
    logger.info("Starting sync for store %s", store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()

    if not store:
        logger.error("Store %s not found", store_id)
        raise ValueError(f"Store {store_id} not found")

    access_token = decrypt_value(store["access_token_enc"])
    client = ShopifyClient(store["domain"], access_token, store["api_version"])

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO sync_jobs (store_id, status) VALUES (%s, 'running') RETURNING id",
            (store_id,),
        )
        job_id = cur.fetchone()["id"]

    try:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'fetching_products' WHERE id = %s",
                (job_id,),
            )

        logger.info("Fetching products for store %s...", store_id)
        products = client.get_all_products()
        logger.info("Fetched %d products", len(products))

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'saving_products', total_products = %s WHERE id = %s",
                (len(products), job_id),
            )

        products_synced = 0
        for p in products:
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO synced_products
                        (store_id, shopify_id, title, handle, description, vendor,
                         product_type, tags, meta_title, meta_description, images,
                         variants, status, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        handle = EXCLUDED.handle,
                        description = EXCLUDED.description,
                        vendor = EXCLUDED.vendor,
                        product_type = EXCLUDED.product_type,
                        tags = EXCLUDED.tags,
                        meta_title = EXCLUDED.meta_title,
                        meta_description = EXCLUDED.meta_description,
                        images = EXCLUDED.images,
                        variants = EXCLUDED.variants,
                        status = EXCLUDED.status,
                        synced_at = NOW()
                    """,
                    (
                        store_id,
                        p["shopify_id"],
                        p["title"],
                        p["handle"],
                        p["description"],
                        p["vendor"],
                        p["product_type"],
                        p["tags"],
                        p["meta_title"],
                        p["meta_description"],
                        json.dumps(p["images"]),
                        json.dumps(p["variants"]),
                        p["status"],
                    ),
                )
                products_synced += 1
                cur.execute(
                    "UPDATE sync_jobs SET products_synced = %s WHERE id = %s",
                    (products_synced, job_id),
                )

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'fetching_collections' WHERE id = %s",
                (job_id,),
            )

        logger.info("Fetching collections for store %s...", store_id)
        collections = client.get_all_collections()
        logger.info("Fetched %d collections", len(collections))

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'saving_collections', total_collections = %s WHERE id = %s",
                (len(collections), job_id),
            )

        collections_synced = 0
        for c in collections:
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO synced_collections
                        (store_id, shopify_id, title, handle, description,
                         meta_title, meta_description, products_count, image_url,
                         collection_type, published,
                         synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        handle = EXCLUDED.handle,
                        description = EXCLUDED.description,
                        meta_title = EXCLUDED.meta_title,
                        meta_description = EXCLUDED.meta_description,
                        products_count = EXCLUDED.products_count,
                        image_url = EXCLUDED.image_url,
                        collection_type = EXCLUDED.collection_type,
                        published = EXCLUDED.published,
                        synced_at = NOW()
                    """,
                    (
                        store_id,
                        c["shopify_id"],
                        c["title"],
                        c["handle"],
                        c["description"],
                        c["meta_title"],
                        c["meta_description"],
                        c["products_count"],
                        c["image_url"],
                        c["collection_type"],
                        c["published"],
                    ),
                )
                collections_synced += 1
                cur.execute(
                    "UPDATE sync_jobs SET collections_synced = %s WHERE id = %s",
                    (collections_synced, job_id),
                )

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'mapping_products', collections_mapped = 0 WHERE id = %s",
                (job_id,),
            )

        logger.info("Syncing collection-product memberships for store %s...", store_id)
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM collection_product_map WHERE store_id = %s", (store_id,))
        collections_mapped = 0
        for c in collections:
            try:
                coll_products = client.get_all_collection_products(c["shopify_id"])
                for cp in coll_products:
                    with db.get_cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO collection_product_map
                                (store_id, collection_shopify_id, product_shopify_id, synced_at)
                            VALUES (%s, %s, %s, NOW())
                            ON CONFLICT (store_id, collection_shopify_id, product_shopify_id) DO NOTHING
                            """,
                            (store_id, c["shopify_id"], cp["shopify_id"]),
                        )
                time.sleep(0.5)
            except Exception as e:
                logger.warning("Failed to sync products for collection %s: %s", c["shopify_id"], e)
            collections_mapped += 1
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET collections_mapped = %s WHERE id = %s",
                    (collections_mapped, job_id),
                )
        logger.info("Collection-product mapping sync complete for store %s", store_id)

        shopify_collection_ids = [c["shopify_id"] for c in collections]
        if shopify_collection_ids:
            placeholders = ",".join(["%s"] * len(shopify_collection_ids))
            with db.get_cursor() as cur:
                cur.execute(
                    f"DELETE FROM synced_collections WHERE store_id = %s AND shopify_id NOT IN ({placeholders})",
                    (store_id, *shopify_collection_ids),
                )
                stale_count = cur.rowcount
            if stale_count:
                logger.info("Removed %d stale collections for store %s", stale_count, store_id)

        with db.get_cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET status = 'completed', products_synced = %s,
                    collections_synced = %s, current_phase = NULL, completed_at = NOW()
                WHERE id = %s
                """,
                (products_synced, collections_synced, job_id),
            )
            cur.execute(
                "UPDATE stores SET last_synced_at = NOW() WHERE id = %s", (store_id,)
            )

        logger.info(
            "Sync complete for store %s: %d products, %d collections",
            store_id, products_synced, collections_synced,
        )
        return {
            "job_id": job_id,
            "products_synced": products_synced,
            "collections_synced": collections_synced,
        }

    except Exception as e:
        logger.exception("Sync failed for store %s: %s", store_id, e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET status = 'failed', error = %s, current_phase = NULL, completed_at = NOW() WHERE id = %s",
                (str(e), job_id),
            )
        raise


def sync_store_async(store_id):
    thread = threading.Thread(target=sync_store, args=(store_id,), daemon=True)
    thread.start()
    return thread
