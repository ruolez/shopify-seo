import json
import logging
import threading
import time

import psycopg2.extras

from app.auth import decrypt_value
from app.database import db
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

PRODUCT_BATCH_SIZE = 100

_sync_locks: dict = {}
_sync_locks_mutex = threading.Lock()


def _get_sync_lock(store_id):
    with _sync_locks_mutex:
        if store_id not in _sync_locks:
            _sync_locks[store_id] = threading.Lock()
        return _sync_locks[store_id]


def _upsert_product_batch(store_id, batch):
    # De-dupe by shopify_id (keep the newest occurrence) — ON CONFLICT raises
    # "cannot affect row a second time" if a product appears twice in one batch.
    deduped = {p["shopify_id"]: p for p in batch}
    batch = list(deduped.values())
    with db.get_conn() as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO synced_products
                (store_id, shopify_id, title, handle, description, vendor,
                 product_type, tags, meta_title, meta_description, images,
                 variants, status, shopify_updated_at, synced_at)
            VALUES %s
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
                shopify_updated_at = EXCLUDED.shopify_updated_at,
                synced_at = NOW()
            """,
            [
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
                    p.get("shopify_updated_at"),
                )
                for p in batch
            ],
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        )
        cur.close()


def _sync_blogs_and_articles(client, store_id, progress_cb=None):
    """Fetch all blogs + their articles and upsert them locally.

    Shared by the full store sync and the blogs-only sync so the upsert
    semantics (article_type promotion, staged-upload image filtering, stale
    cleanup) can't drift apart. Returns (blogs_synced, articles_synced, errors).
    """
    blogs = client.get_all_blogs()
    logger.info("Fetched %d blogs for store %s", len(blogs), store_id)

    blogs_synced = 0
    articles_synced = 0
    blog_errors = []

    for b in blogs:
        try:
            with db.get_conn() as conn:
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(
                    """
                    INSERT INTO synced_blogs
                        (store_id, shopify_id, title, handle, comment_policy,
                         articles_count, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        handle = EXCLUDED.handle,
                        comment_policy = EXCLUDED.comment_policy,
                        articles_count = EXCLUDED.articles_count,
                        synced_at = NOW()
                    RETURNING id
                    """,
                    (
                        store_id,
                        b["shopify_id"],
                        b["title"],
                        b["handle"],
                        b["comment_policy"],
                        b["articles_count"],
                    ),
                )
                blog_db_id = cur.fetchone()["id"]

                blog_articles = client.get_all_articles(blog_id=b["shopify_id"])
                for a in blog_articles:
                    cur.execute(
                        """
                        INSERT INTO synced_articles
                            (store_id, blog_id, shopify_id, shopify_blog_id, title, handle,
                             body, summary, tags, author, meta_title, meta_description,
                             image_url, image_alt, is_published, published_at, article_type, synced_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                            blog_id = EXCLUDED.blog_id,
                            shopify_blog_id = EXCLUDED.shopify_blog_id,
                            title = EXCLUDED.title,
                            handle = EXCLUDED.handle,
                            body = EXCLUDED.body,
                            summary = EXCLUDED.summary,
                            tags = EXCLUDED.tags,
                            author = EXCLUDED.author,
                            meta_title = EXCLUDED.meta_title,
                            meta_description = EXCLUDED.meta_description,
                            image_url = CASE WHEN EXCLUDED.image_url LIKE '%%shopify-staged-uploads%%' THEN synced_articles.image_url ELSE EXCLUDED.image_url END,
                            image_alt = EXCLUDED.image_alt,
                            is_published = EXCLUDED.is_published,
                            published_at = EXCLUDED.published_at,
                            article_type = CASE WHEN EXCLUDED.article_type = 'news' THEN 'news' ELSE synced_articles.article_type END,
                            synced_at = NOW()
                        """,
                        (
                            store_id,
                            blog_db_id,
                            a["shopify_id"],
                            a["blog_shopify_id"],
                            a["title"],
                            a["handle"],
                            a["body"],
                            a["summary"],
                            a["tags"],
                            a["author"],
                            a["meta_title"],
                            a["meta_description"],
                            a["image_url"] if "shopify-staged-uploads" not in (a["image_url"] or "") else "",
                            a["image_alt"],
                            a["is_published"],
                            a["published_at"],
                            ("news" if (a.get("template_suffix") == "news" or "type:news" in (a.get("tags") or "")) else "blog"),
                        ),
                    )
                    articles_synced += 1

                # Stale-article cleanup runs even when the blog is now empty —
                # the fetch succeeded, so an empty list means all were deleted.
                shopify_article_ids = [a["shopify_id"] for a in blog_articles]
                if shopify_article_ids:
                    placeholders = ",".join(["%s"] * len(shopify_article_ids))
                    cur.execute(
                        f"DELETE FROM synced_articles WHERE blog_id = %s AND shopify_id NOT IN ({placeholders})",
                        (blog_db_id, *shopify_article_ids),
                    )
                else:
                    cur.execute("DELETE FROM synced_articles WHERE blog_id = %s", (blog_db_id,))
                stale_article_count = cur.rowcount
                if stale_article_count:
                    logger.info("Removed %d stale articles for blog %s", stale_article_count, blog_db_id)

                cur.close()

            blogs_synced += 1
            time.sleep(0.5)
        except Exception as e:
            logger.warning("Failed to sync blog %s: %s", b["shopify_id"], e)
            blog_errors.append(f"{b.get('handle') or b['shopify_id']}: {e}")

        if progress_cb:
            progress_cb(blogs_synced, articles_synced)

    # Stale-blog cleanup (articles cascade via FK). Unconditional: the fetch
    # succeeded, so zero blogs means the store legitimately has none.
    shopify_blog_ids = [b["shopify_id"] for b in blogs]
    with db.get_cursor() as cur:
        if shopify_blog_ids:
            placeholders = ",".join(["%s"] * len(shopify_blog_ids))
            cur.execute(
                f"DELETE FROM synced_blogs WHERE store_id = %s AND shopify_id NOT IN ({placeholders})",
                (store_id, *shopify_blog_ids),
            )
        else:
            cur.execute("DELETE FROM synced_blogs WHERE store_id = %s", (store_id,))
        stale_blog_count = cur.rowcount
    if stale_blog_count:
        logger.info("Removed %d stale blogs for store %s", stale_blog_count, store_id)

    return blogs_synced, articles_synced, blog_errors


def sync_store(store_id):
    lock = _get_sync_lock(store_id)
    if not lock.acquire(blocking=False):
        logger.warning("Sync already running for store %s, skipping", store_id)
        return None
    try:
        return _sync_store_inner(store_id)
    finally:
        lock.release()


def _sync_store_inner(store_id):
    logger.info("Starting sync for store %s", store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()

    if not store:
        logger.error("Store %s not found", store_id)
        raise ValueError(f"Store {store_id} not found")

    access_token = decrypt_value(store["access_token_enc"])
    client = ShopifyClient(store["domain"], access_token, store["api_version"])

    try:
        shop_info = client.test_connection()
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE stores SET shop_name = %s, primary_domain = %s WHERE id = %s",
                (shop_info.get("name"), shop_info.get("primary_domain"), store_id),
            )
    except Exception:
        logger.warning("Failed to refresh shop info for store %s (non-fatal)", store_id)

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

        # Fetch and upsert products in batches (Fix #3 + #4)
        logger.info("Fetching products for store %s...", store_id)
        products = []
        products_synced = 0
        batch = []
        for page in client.iter_products(query_filter="status:active AND published_status:published"):
            products.extend(page)
            batch.extend(page)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET current_phase = 'saving_products', total_products = %s WHERE id = %s",
                    (len(products), job_id),
                )
            while len(batch) >= PRODUCT_BATCH_SIZE:
                chunk, batch = batch[:PRODUCT_BATCH_SIZE], batch[PRODUCT_BATCH_SIZE:]
                _upsert_product_batch(store_id, chunk)
                products_synced += len(chunk)
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE sync_jobs SET products_synced = %s WHERE id = %s",
                        (products_synced, job_id),
                    )
        if batch:
            _upsert_product_batch(store_id, batch)
            products_synced += len(batch)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET products_synced = %s WHERE id = %s",
                    (products_synced, job_id),
                )
        logger.info("Fetched and saved %d products", len(products))

        # Clean up stale products — unconditional: the fetch succeeded, so an
        # empty list means the store legitimately has no matching products.
        shopify_product_ids = [p["shopify_id"] for p in products]
        with db.get_cursor() as cur:
            if shopify_product_ids:
                placeholders = ",".join(["%s"] * len(shopify_product_ids))
                cur.execute(
                    f"DELETE FROM synced_products WHERE store_id = %s AND shopify_id NOT IN ({placeholders})",
                    (store_id, *shopify_product_ids),
                )
            else:
                cur.execute("DELETE FROM synced_products WHERE store_id = %s", (store_id,))
            stale_count = cur.rowcount
        if stale_count:
            logger.info("Removed %d stale products for store %s", stale_count, store_id)

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
                         collection_type, published, shopify_updated_at,
                         synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        handle = EXCLUDED.handle,
                        description = EXCLUDED.description,
                        meta_title = EXCLUDED.meta_title,
                        meta_description = EXCLUDED.meta_description,
                        products_count = EXCLUDED.products_count,
                        image_url = CASE
                            WHEN EXCLUDED.image_url IS NOT NULL AND EXCLUDED.image_url <> ''
                            THEN EXCLUDED.image_url
                            ELSE synced_collections.image_url
                        END,
                        collection_type = EXCLUDED.collection_type,
                        published = EXCLUDED.published,
                        shopify_updated_at = EXCLUDED.shopify_updated_at,
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
                        c.get("shopify_updated_at"),
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

        # Atomic collection-product mapping (Fix #1)
        logger.info("Syncing collection-product memberships for store %s (%d collections)...", store_id, len(collections))
        all_mapping_rows = []
        collections_mapped = 0
        for c in collections:
            try:
                coll_products = client.get_all_collection_products(c["shopify_id"])
                for cp in coll_products:
                    all_mapping_rows.append((store_id, c["shopify_id"], cp["shopify_id"]))
                logger.info("Fetched %d products for collection '%s'", len(coll_products), c["title"])
                time.sleep(0.5)
            except Exception as e:
                logger.warning("Failed to fetch products for collection %s: %s", c["shopify_id"], e)
            collections_mapped += 1
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET collections_mapped = %s WHERE id = %s",
                    (collections_mapped, job_id),
                )

        # Write all mappings atomically
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM collection_product_map WHERE store_id = %s", (store_id,))
            if all_mapping_rows:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO collection_product_map
                        (store_id, collection_shopify_id, product_shopify_id, synced_at)
                    VALUES %s
                    ON CONFLICT (store_id, collection_shopify_id, product_shopify_id) DO NOTHING
                    """,
                    all_mapping_rows,
                    template="(%s, %s, %s, NOW())",
                )
            cur.close()
        logger.info("Collection-product mapping sync complete for store %s (%d mappings)", store_id, len(all_mapping_rows))

        # --- Blog sync (Fix #5: blog + articles in single transaction) ---
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'fetching_blogs' WHERE id = %s",
                (job_id,),
            )

        logger.info("Fetching blogs for store %s...", store_id)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE sync_jobs SET current_phase = 'saving_blogs' WHERE id = %s",
                (job_id,),
            )

        def _blog_progress(n_blogs, n_articles):
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET blogs_synced = %s, articles_synced = %s WHERE id = %s",
                    (n_blogs, n_articles, job_id),
                )

        blogs_synced, articles_synced, blog_errors = _sync_blogs_and_articles(
            client, store_id, progress_cb=_blog_progress
        )

        if blog_errors:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET error = %s WHERE id = %s",
                    (f"{len(blog_errors)} blog(s) failed to sync: " + "; ".join(blog_errors)[:1500], job_id),
                )

        logger.info("Blog sync complete: %d blogs, %d articles (%d failed)", blogs_synced, articles_synced, len(blog_errors))

        shopify_collection_ids = [c["shopify_id"] for c in collections]
        with db.get_cursor() as cur:
            if shopify_collection_ids:
                placeholders = ",".join(["%s"] * len(shopify_collection_ids))
                cur.execute(
                    f"DELETE FROM synced_collections WHERE store_id = %s AND shopify_id NOT IN ({placeholders})",
                    (store_id, *shopify_collection_ids),
                )
            else:
                cur.execute("DELETE FROM synced_collections WHERE store_id = %s", (store_id,))
            stale_count = cur.rowcount
        if stale_count:
            logger.info("Removed %d stale collections for store %s", stale_count, store_id)

        # Clear stale interlinking analysis and re-run
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM interlinking_analysis WHERE store_id = %s", (store_id,))

        try:
            from app.services.interlinking_analyzer import (
                analyze_store_interlinking,
                build_store_hosts,
            )
            with db.get_cursor() as cur:
                cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (store_id,))
                store_row = cur.fetchone()
            store_hosts = build_store_hosts(
                store_row["domain"] if store_row else None,
                store_row["primary_domain"] if store_row else None,
            )
            analyze_store_interlinking(store_id, store_hosts, client)
            logger.info("Interlinking analysis refreshed for store %s", store_id)
        except Exception:
            logger.exception("Interlinking re-analysis failed for store %s (non-fatal)", store_id)

        with db.get_cursor() as cur:
            cur.execute(
                """
                UPDATE sync_jobs
                SET status = 'completed', products_synced = %s,
                    collections_synced = %s, blogs_synced = %s,
                    articles_synced = %s, current_phase = NULL, completed_at = NOW()
                WHERE id = %s
                """,
                (products_synced, collections_synced, blogs_synced, articles_synced, job_id),
            )
            cur.execute(
                "UPDATE stores SET last_synced_at = NOW() WHERE id = %s", (store_id,)
            )

        logger.info(
            "Sync complete for store %s: %d products, %d collections, %d blogs, %d articles",
            store_id, products_synced, collections_synced, blogs_synced, articles_synced,
        )
        return {
            "job_id": job_id,
            "products_synced": products_synced,
            "collections_synced": collections_synced,
            "blogs_synced": blogs_synced,
            "articles_synced": articles_synced,
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


def sync_store_blogs(store_id):
    logger.info("Starting blog sync for store %s", store_id)

    lock = _get_sync_lock(store_id)
    if not lock.acquire(blocking=False):
        raise RuntimeError("A sync is already running for this store — try again when it finishes")
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
            store = cur.fetchone()

        if not store:
            raise ValueError(f"Store {store_id} not found")

        access_token = decrypt_value(store["access_token_enc"])
        client = ShopifyClient(store["domain"], access_token, store["api_version"])

        blogs_synced, articles_synced, blog_errors = _sync_blogs_and_articles(client, store_id)

        logger.info(
            "Blog sync complete for store %s: %d blogs, %d articles (%d failed)",
            store_id, blogs_synced, articles_synced, len(blog_errors),
        )
        return {
            "blogs_synced": blogs_synced,
            "articles_synced": articles_synced,
            "blogs_failed": len(blog_errors),
            "errors": blog_errors,
        }
    finally:
        lock.release()
