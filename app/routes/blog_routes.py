import json
import logging
import re
import threading
import time
from datetime import datetime, time as dt_time, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services.ai_output_guard import guard_shopify_fields
from app.services.blog_seo_analyzer import analyze_article_seo
from app.shopify_client import ShopifyClient, is_gone_error

logger = logging.getLogger(__name__)

blog_bp = Blueprint("blog", __name__)


def _get_store_client(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None, None
    access_token = decrypt_value(store["access_token_enc"])
    client = ShopifyClient(store["domain"], access_token, store["api_version"])
    return store, client


def _regen_landing_async(store_id, blog_id, client, store):
    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM synced_blogs WHERE id = %s", (blog_id,))
                blog = cur.fetchone()
            if not blog:
                return

            try:
                time.sleep(5)
                fresh_articles = client.get_all_articles()
                for fa in fresh_articles:
                    if fa.get("image_url") and "shopify-staged-uploads" not in fa["image_url"]:
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE synced_articles SET image_url = %s WHERE store_id = %s AND shopify_id = %s AND (image_url IS NULL OR image_url != %s)",
                                (fa["image_url"], store_id, fa["shopify_id"], fa["image_url"]),
                            )
            except Exception as e:
                logger.warning("Failed to refresh article images: %s", e)

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT * FROM synced_articles WHERE store_id = %s AND blog_id = %s AND is_published = TRUE AND article_type = 'blog' AND COALESCE(tags, '') NOT ILIKE '%%landing-page%%' ORDER BY COALESCE(published_at, synced_at) DESC",
                    (store_id, blog_id),
                )
                articles = [dict(a) for a in cur.fetchall()]

            news_blog_shopify_id = None
            with db.get_cursor() as cur:
                cur.execute("SELECT shopify_id FROM synced_blogs WHERE store_id = %s AND handle = 'news'", (store_id,))
                news_row = cur.fetchone()
            if news_row:
                news_blog_shopify_id = news_row["shopify_id"]
            if not news_blog_shopify_id:
                return

            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s", (store_id, blog_id))
                existing = cur.fetchone()

            if not articles:
                if existing and existing.get("shopify_page_id"):
                    try:
                        client.delete_article(existing["shopify_page_id"])
                    except Exception as e:
                        if not is_gone_error(e):
                            # Keep the tracking row — dropping it would orphan a
                            # still-live landing article with no local record.
                            logger.warning("Landing delete failed for blog %s, keeping tracking row: %s", blog_id, e)
                            return
                    with db.get_cursor() as cur:
                        cur.execute("DELETE FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s", (store_id, blog_id))
                return

            from app.services.blog_landing_generator import generate_blog_landing_html, get_tags
            intro_text = existing.get("intro_text") if existing else None
            hero_image_url = existing.get("hero_image_url") if existing else None
            hero_image_alt = existing.get("hero_image_alt") if existing else None
            clean_title = re.sub(r'\s+', ' ', blog['title']).strip()
            html_content = generate_blog_landing_html(articles, blog["handle"], clean_title, intro_text=intro_text, hero_image_url=hero_image_url, hero_image_alt=hero_image_alt)
            all_tags = get_tags(articles[:50])
            article_title = f"{clean_title} — All Articles"
            meta_desc = (intro_text or f"Browse all {clean_title} articles, tips, and guides.")[:160]

            from datetime import datetime, timezone
            now_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            landing_fields = {
                "title": article_title, "body": html_content,
                "tags": ["landing-page"], "is_published": True,
                "publish_date": now_date,
                "summary": "",
                "image_url": "",
                "clear_image": True,
                "meta_title": article_title[:60],
                "meta_description": meta_desc,
            }

            shopify_article_id = None
            if existing and existing.get("shopify_page_id"):
                try:
                    client.update_article(existing["shopify_page_id"], landing_fields)
                    shopify_article_id = existing["shopify_page_id"]
                except Exception as e:
                    # Only recreate when the old article is confirmed gone — a
                    # transient error must not mint a duplicate landing.
                    if is_gone_error(e):
                        logger.info("Landing article %s gone on Shopify — recreating", existing["shopify_page_id"])
                    else:
                        raise

            if not shopify_article_id:
                landing_fields.pop("clear_image", None)
                landing_fields.pop("image_url", None)
                landing_fields["handle"] = f"{blog['handle']}-landing"
                landing_fields["author"] = store.get("shop_name") or store["name"]
                result = client.create_article(news_blog_shopify_id, landing_fields)
                shopify_article_id = result["shopify_id"]

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO blog_landing_pages (store_id, blog_id, shopify_page_id, page_handle, page_title, articles_count, tags_count, last_generated_at)
                    VALUES (%s, %s, %s, 'news', %s, %s, %s, NOW())
                    ON CONFLICT (store_id, blog_id) DO UPDATE SET
                        shopify_page_id = EXCLUDED.shopify_page_id, page_title = EXCLUDED.page_title,
                        articles_count = EXCLUDED.articles_count, tags_count = EXCLUDED.tags_count,
                        last_generated_at = NOW()""",
                    (store_id, blog_id, shopify_article_id, article_title, len(articles), len(all_tags)),
                )
            logger.info("Auto-regenerated landing page for blog %s", blog["title"])
        except Exception as e:
            logger.exception("Failed to auto-regenerate landing page: %s", e)

    threading.Thread(target=_run, daemon=True).start()


@blog_bp.route("/blog")
def blog_page():
    return render_template("blog.html")


@blog_bp.route("/content/calendar")
def content_calendar_page():
    return render_template("calendar.html")


@blog_bp.route("/api/calendar")
def content_calendar_data():
    """Merged month view of the blog + news publishing pipeline."""
    store_id = request.args.get("store_id", type=int)
    month = request.args.get("month") or datetime.now(timezone.utc).strftime("%Y-%m")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    try:
        start = datetime.strptime(month + "-01", "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "month must be YYYY-MM"}), 400
    end = (start + timedelta(days=32)).replace(day=1)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, 'blog' AS kind, COALESCE(generated_article->>'title', topic) AS title,
                      status, scheduled_for
               FROM article_wizard_sessions
               WHERE store_id = %s AND scheduled_for >= %s AND scheduled_for < %s
               UNION ALL
               SELECT id, 'news', COALESCE(edited_result->>'headline', analyze_result->>'headline', 'News article'),
                      status, scheduled_for
               FROM news_sessions
               WHERE store_id = %s AND scheduled_for >= %s AND scheduled_for < %s
               ORDER BY scheduled_for""",
            (store_id, start, end, store_id, start, end),
        )
        rows = cur.fetchall()
    entries = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "title": r["title"],
            "status": r["status"],
            "scheduled_for": r["scheduled_for"].isoformat() if r["scheduled_for"] else None,
        }
        for r in rows
    ]
    return jsonify({"month": month, "entries": entries})


# ── Blog Sync ───────────────────────────────────────────────────

@blog_bp.route("/api/blog/sync", methods=["POST"])
def sync_blogs():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM stores WHERE id = %s", (store_id,))
        if not cur.fetchone():
            return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'blog_sync', 'running', %s) RETURNING id""",
            (store_id, json.dumps({"store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    def run_sync():
        try:
            from app.services.sync_service import sync_store_blogs
            result = sync_store_blogs(store_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Blog sync failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


# ── Blog Management ─────────────────────────────────────────────

@blog_bp.route("/api/blog/blogs", methods=["GET"])
def list_blogs():
    store_id = request.args.get("store_id")
    conditions = []
    params = []
    if store_id:
        conditions.append("sb.store_id = %s")
        params.append(store_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT sb.*, COALESCE(s.shop_name, s.name) as store_name, COALESCE(s.primary_domain, s.domain) as store_domain
            FROM synced_blogs sb
            JOIN stores s ON s.id = sb.store_id
            {where}
            ORDER BY sb.title""",
            params,
        )
        blogs = cur.fetchall()
    return jsonify([dict(b) for b in blogs])


@blog_bp.route("/api/blog/blogs", methods=["POST"])
def create_blog():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    title = re.sub(r'\s+', ' ', data.get("title", "")).strip()
    if not store_id or not title:
        return jsonify({"error": "store_id and title are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    result = client.create_blog(title)

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO synced_blogs (store_id, shopify_id, title, handle, synced_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                title = EXCLUDED.title, handle = EXCLUDED.handle, synced_at = NOW()
            RETURNING id""",
            (store_id, result["shopify_id"], result["title"], result["handle"]),
        )
        blog_row = cur.fetchone()

    return jsonify({"id": blog_row["id"], **result})


# ── Article List & CRUD ──────────────────────────────────────────

@blog_bp.route("/api/blog/articles", methods=["GET"])
def list_articles():
    store_id = request.args.get("store_id")
    blog_id = request.args.get("blog_id")
    status = request.args.get("status", "").strip().lower()
    sort_by = request.args.get("sort_by", "title")
    sort_dir = request.args.get("sort_dir", "asc")

    valid_sql_sorts = {
        "title": "sa.title",
        "blog": "sb.title",
        "status": "sa.is_published",
        "last_optimized_at": "sa.last_optimized_at",
    }
    order_clause = "ORDER BY sa.title"
    if sort_by in valid_sql_sorts:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        order_clause = f"ORDER BY {valid_sql_sorts[sort_by]} {direction}"

    conditions = ["sa.article_type = 'blog'"]
    params = []
    if store_id:
        conditions.append("sa.store_id = %s")
        params.append(store_id)
    if blog_id:
        conditions.append("sa.blog_id = %s")
        params.append(blog_id)
    if status == "published":
        conditions.append("sa.is_published = TRUE")
    elif status == "draft":
        conditions.append("sa.is_published = FALSE")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT sa.*, sb.title as blog_title, sb.handle as blog_handle,
                COALESCE(s.shop_name, s.name) as store_name, COALESCE(s.primary_domain, s.domain) as store_domain
            FROM synced_articles sa
            JOIN synced_blogs sb ON sb.id = sa.blog_id
            JOIN stores s ON s.id = sa.store_id
            {where}
            {order_clause}""",
            params,
        )
        articles = cur.fetchall()

    result = []
    for a in articles:
        article_dict = dict(a)
        seo = analyze_article_seo(article_dict)
        article_dict["seo_score"] = seo["score"]
        article_dict["seo_breakdown"] = seo["breakdown"]
        result.append(article_dict)

    return jsonify(result)


@blog_bp.route("/api/blog/articles/<int:article_id>", methods=["GET"])
def get_article(article_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.*, sb.title as blog_title, sb.handle as blog_handle,
                COALESCE(s.shop_name, s.name) as store_name, COALESCE(s.primary_domain, s.domain) as store_domain
            FROM synced_articles sa
            JOIN synced_blogs sb ON sb.id = sa.blog_id
            JOIN stores s ON s.id = sa.store_id
            WHERE sa.id = %s""",
            (article_id,),
        )
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    article_dict = dict(article)
    seo = analyze_article_seo(article_dict)
    article_dict["seo_score"] = seo["score"]
    article_dict["seo_breakdown"] = seo["breakdown"]
    return jsonify(article_dict)


@blog_bp.route("/api/blog/articles/<int:article_id>/apply", methods=["POST"])
def apply_article(article_id):
    data = request.get_json(silent=True) or {}
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.*, s.id as sid
            FROM synced_articles sa
            JOIN stores s ON s.id = sa.store_id
            WHERE sa.id = %s""",
            (article_id,),
        )
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    store, client = _get_store_client(article["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    fields = {}
    for key in ["title", "body", "meta_title", "meta_description", "summary", "tags"]:
        if key in data:
            fields[key] = data[key]

    # An empty apply must not fire a no-op Shopify mutation or bump freshness.
    if not fields:
        return jsonify({"error": "No recognized fields to apply"}), 400

    guard_shopify_fields(fields)
    client.update_article(article["shopify_id"], fields)

    update_parts = []
    update_params = []
    if "title" in fields:
        update_parts.append("title = %s")
        update_params.append(fields["title"])
    if "body" in fields:
        update_parts.append("body = %s")
        update_params.append(fields["body"])
    if "meta_title" in fields:
        update_parts.append("meta_title = %s")
        update_params.append(fields["meta_title"])
    if "meta_description" in fields:
        update_parts.append("meta_description = %s")
        update_params.append(fields["meta_description"])
    if "summary" in fields:
        update_parts.append("summary = %s")
        update_params.append(fields["summary"])
    if "tags" in fields:
        update_parts.append("tags = %s")
        update_params.append(fields["tags"])

    update_parts.append("last_optimized_at = NOW()")
    update_params.append(article_id)

    if update_parts:
        with db.get_cursor() as cur:
            cur.execute(
                f"UPDATE synced_articles SET {', '.join(update_parts)} WHERE id = %s",
                update_params,
            )

    return jsonify({"ok": True, "success": True})


@blog_bp.route("/api/blog/articles/<int:article_id>/move", methods=["POST"])
def move_article(article_id):
    data = request.get_json(silent=True) or {}
    target_blog_id = data.get("target_blog_id")
    if not target_blog_id:
        return jsonify({"error": "target_blog_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    source_blog_id = article["blog_id"]
    if source_blog_id == target_blog_id:
        return jsonify({"error": "Article is already in this blog"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_blogs WHERE id = %s AND store_id = %s", (target_blog_id, article["store_id"]))
        target_blog = cur.fetchone()
    if not target_blog:
        return jsonify({"error": "Target blog not found"}), 404

    store, client = _get_store_client(article["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    try:
        client.update_article(article["shopify_id"], {"blog_id": target_blog["shopify_id"]})
    except Exception as e:
        logger.exception("Failed to move article on Shopify: %s", e)
        return jsonify({"error": f"Shopify error: {e}"}), 500

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE synced_articles SET blog_id = %s, shopify_blog_id = %s WHERE id = %s",
            (target_blog_id, target_blog["shopify_id"], article_id),
        )

    _regen_landing_async(article["store_id"], source_blog_id, client, store)
    _regen_landing_async(article["store_id"], target_blog_id, client, store)

    return jsonify({"ok": True, "success": True, "moved_to": target_blog["title"]})


@blog_bp.route("/api/blog/articles/<int:article_id>", methods=["DELETE"])
def delete_article(article_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    store, client = _get_store_client(article["store_id"])
    if not client:
        # Deleting only the local row would leave the article live on the
        # store but invisible in the app.
        return jsonify({"error": "Store connection unavailable — cannot delete from Shopify"}), 502

    try:
        client.delete_article(article["shopify_id"])
    except Exception as e:
        # Already gone on Shopify is fine to clean up locally; real failures
        # keep the local row so the article doesn't vanish while still live.
        if not is_gone_error(e):
            logger.exception("Shopify delete failed for article %s: %s", article_id, e)
            return jsonify({"error": f"Shopify delete failed: {e}"}), 502

    with db.get_cursor() as cur:
        cur.execute("DELETE FROM synced_articles WHERE id = %s", (article_id,))

    _regen_landing_async(article["store_id"], article["blog_id"], client, store)

    return jsonify({"ok": True, "success": True})


@blog_bp.route("/api/blog/articles/delete", methods=["POST"])
def bulk_delete_articles():
    data = request.get_json(silent=True) or {}
    article_ids = data.get("article_ids", [])
    if not article_ids:
        return jsonify({"error": "No articles specified"}), 400

    placeholders = ",".join(["%s"] * len(article_ids))
    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT sa.*, s.id as sid FROM synced_articles sa JOIN stores s ON s.id = sa.store_id WHERE sa.id IN ({placeholders})",
            article_ids,
        )
        articles = cur.fetchall()

    stores_cache = {}
    deleted = 0
    failed = []
    deletable_ids = []
    for article in articles:
        sid = article["store_id"]
        if sid not in stores_cache:
            stores_cache[sid] = _get_store_client(sid)
        _, client = stores_cache[sid]
        if not client:
            failed.append({"id": article["id"], "title": article.get("title"), "error": "store connection unavailable"})
            continue
        try:
            client.delete_article(article["shopify_id"])
            deletable_ids.append(article["id"])
        except Exception as e:
            # Already-gone articles are safe to remove locally; real failures
            # keep the local row so the article doesn't become invisible while live.
            if is_gone_error(e):
                deletable_ids.append(article["id"])
            else:
                logger.warning("Failed to delete article %s from Shopify: %s", article["id"], e)
                failed.append({"id": article["id"], "title": article.get("title"), "error": str(e)})

    affected_blogs = set((a["store_id"], a["blog_id"]) for a in articles if a["id"] in set(deletable_ids))

    if deletable_ids:
        del_placeholders = ",".join(["%s"] * len(deletable_ids))
        with db.get_cursor() as cur:
            cur.execute(f"DELETE FROM synced_articles WHERE id IN ({del_placeholders})", deletable_ids)
            deleted = cur.rowcount

    for sid, bid in affected_blogs:
        store, client = stores_cache.get(sid, (None, None))
        if client:
            _regen_landing_async(sid, bid, client, store)

    return jsonify({"deleted": deleted, "failed": failed})


# ── AI SEO Analysis ──────────────────────────────────────────────

@blog_bp.route("/api/blog/articles/<int:article_id>/analyze", methods=["POST"])
def analyze_article_ai(article_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'article_seo', 'pending', %s) RETURNING id""",
            (article["store_id"], json.dumps({"article_id": article_id})),
        )
        task_id = cur.fetchone()["id"]

    def run_task():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            from app.ai_agent import run_task as ai_run_task
            result = ai_run_task("article_seo", {"article_id": article_id}, store_id=article["store_id"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Article AI analysis failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


# ── Refresh Links ────────────────────────────────────────────────

@blog_bp.route("/api/blog/articles/<int:article_id>/current-links", methods=["GET"])
def get_article_current_links(article_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT body FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    body = article.get("body") or ""
    links = []
    for match in re.finditer(r'<a\s[^>]*href\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</a>', body, re.IGNORECASE | re.DOTALL):
        url = match.group(1)
        anchor = re.sub(r'<[^>]+>', '', match.group(2)).strip()
        if url.startswith("/products/"):
            link_type = "product"
        elif url.startswith("/collections/"):
            link_type = "collection"
        elif url.startswith("/blogs/"):
            link_type = "article"
        else:
            link_type = "external"
        links.append({"url": url, "anchor_text": anchor, "type": link_type})

    return jsonify({"links": links, "total_count": len(links)})


@blog_bp.route("/api/blog/articles/<int:article_id>/refresh-links", methods=["POST"])
def refresh_article_links(article_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    data = request.get_json(silent=True) or {}
    product_ids = data.get("product_ids", [])
    collection_ids = data.get("collection_ids", [])

    if not product_ids and not collection_ids:
        return jsonify({"error": "Select at least one product or collection"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'article_refresh_links', 'pending', %s) RETURNING id""",
            (article["store_id"], json.dumps({
                "article_id": article_id,
                "product_ids": product_ids,
                "collection_ids": collection_ids,
            })),
        )
        task_id = cur.fetchone()["id"]

    def run_task():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            from app.ai_agent import run_task as ai_run_task
            result = ai_run_task("article_refresh_links", {
                "article_id": article_id,
                "product_ids": product_ids,
                "collection_ids": collection_ids,
            }, store_id=article["store_id"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Article refresh links failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


# ── Bulk Optimization ────────────────────────────────────────────

@blog_bp.route("/api/blog/bulk-optimize", methods=["POST"])
def bulk_optimize_articles():
    data = request.get_json(silent=True) or {}
    article_ids = data.get("article_ids", [])
    auto_apply = data.get("auto_apply", True)
    if not article_ids:
        return jsonify({"error": "No articles specified"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO bulk_tasks (status, total, task_type, auto_apply)
            VALUES ('running', %s, 'article', %s) RETURNING id""",
            (len(article_ids), auto_apply),
        )
        bulk_id = cur.fetchone()["id"]

        for aid in article_ids:
            cur.execute(
                "INSERT INTO bulk_task_items (bulk_task_id, article_id, status) VALUES (%s, %s, 'pending')",
                (bulk_id, aid),
            )

    def run_bulk():
        try:
            from app.ai_agent import run_task as ai_run_task

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT bti.*, sa.store_id, sa.shopify_id as article_shopify_id, sa.title as article_title "
                    "FROM bulk_task_items bti "
                    "JOIN synced_articles sa ON sa.id = bti.article_id "
                    "WHERE bti.bulk_task_id = %s ORDER BY bti.id",
                    (bulk_id,),
                )
                items = cur.fetchall()

            for item in items:
                try:
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s",
                            (item["article_title"], bulk_id),
                        )
                        cur.execute(
                            "UPDATE bulk_task_items SET status = 'running' WHERE id = %s",
                            (item["id"],),
                        )

                    result = ai_run_task(
                        "article_seo",
                        {"article_id": item["article_id"]},
                        store_id=item["store_id"],
                    )

                    apply_result = None
                    if auto_apply and not result.get("error"):
                        try:
                            store, client = _get_store_client(item["store_id"])
                            if client:
                                fields = {}
                                if result.get("optimized_meta_title"):
                                    fields["meta_title"] = result["optimized_meta_title"]
                                if result.get("optimized_meta_description"):
                                    fields["meta_description"] = result["optimized_meta_description"]
                                if result.get("optimized_body"):
                                    fields["body"] = result["optimized_body"]
                                if result.get("optimized_summary"):
                                    fields["summary"] = result["optimized_summary"]
                                if fields:
                                    guard_shopify_fields(fields)
                                    client.update_article(item["article_shopify_id"], fields)
                                    apply_result = {"applied_fields": list(fields.keys())}

                                    update_parts = []
                                    update_params = []
                                    if "meta_title" in fields:
                                        update_parts.append("meta_title = %s")
                                        update_params.append(fields["meta_title"])
                                    if "meta_description" in fields:
                                        update_parts.append("meta_description = %s")
                                        update_params.append(fields["meta_description"])
                                    if "body" in fields:
                                        update_parts.append("body = %s")
                                        update_params.append(fields["body"])
                                    if "summary" in fields:
                                        update_parts.append("summary = %s")
                                        update_params.append(fields["summary"])
                                    update_parts.append("last_optimized_at = NOW()")
                                    update_params.append(item["article_id"])
                                    with db.get_cursor() as cur:
                                        cur.execute(
                                            f"UPDATE synced_articles SET {', '.join(update_parts)} WHERE id = %s",
                                            update_params,
                                        )
                        except Exception as e:
                            logger.warning("Failed to apply article changes: %s", e)
                            apply_result = {"error": str(e)}

                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET status = 'completed', ai_result = %s, apply_result = %s, completed_at = NOW() WHERE id = %s",
                            (json.dumps(result), json.dumps(apply_result) if apply_result else None, item["id"]),
                        )
                        cur.execute(
                            "UPDATE bulk_tasks SET completed = completed + 1 WHERE id = %s",
                            (bulk_id,),
                        )
                except Exception as e:
                    logger.exception("Bulk article item %s failed: %s", item["id"], e)
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                            (str(e), item["id"]),
                        )
                        cur.execute(
                            "UPDATE bulk_tasks SET failed = failed + 1 WHERE id = %s",
                            (bulk_id,),
                        )

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET status = 'completed', current_product_title = NULL, completed_at = NOW() WHERE id = %s",
                    (bulk_id,),
                )
        except Exception as e:
            logger.exception("Bulk article optimization failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE bulk_tasks SET status = 'failed', completed_at = NOW() WHERE id = %s",
                    (bulk_id,),
                )

    thread = threading.Thread(target=run_bulk, daemon=True)
    thread.start()
    return jsonify({"bulk_task_id": bulk_id})


@blog_bp.route("/api/blog/bulk-status/<int:bulk_task_id>", methods=["GET"])
def bulk_status(bulk_task_id):
    from app.services.task_runner import sweep_stale_jobs
    sweep_stale_jobs()
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM bulk_tasks WHERE id = %s", (bulk_task_id,))
        task = cur.fetchone()
    if not task:
        return jsonify({"error": "Bulk task not found"}), 404
    return jsonify(dict(task))


# ── Blog Consolidation ───────────────────────────────────────────

# ── Landing Page Generator ───────────────────────────────────────

@blog_bp.route("/api/blog/generate-landing", methods=["POST"])
def generate_landing_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    intro_text = data.get("intro_text", "").strip() or None
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    if intro_text is not None:
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO blog_landing_pages (store_id, blog_id, intro_text)
                VALUES (%s, %s, %s)
                ON CONFLICT (store_id, blog_id) DO UPDATE SET intro_text = EXCLUDED.intro_text""",
                (store_id, blog_id, intro_text),
            )

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'blog_landing_page', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"store_id": store_id, "blog_id": blog_id})),
        )
        task_id = cur.fetchone()["id"]

    def run_landing():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))

            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM synced_blogs WHERE id = %s AND store_id = %s", (blog_id, store_id))
                blog = cur.fetchone()
            if not blog:
                raise Exception("Blog not found")

            # Find or create the "News" blog to publish into
            news_blog_shopify_id = None
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT shopify_id FROM synced_blogs WHERE store_id = %s AND handle = 'news'",
                    (store_id,),
                )
                news_row = cur.fetchone()
            if news_row:
                news_blog_shopify_id = news_row["shopify_id"]
            else:
                news_result = client.create_blog("News")
                news_blog_shopify_id = news_result["shopify_id"]
                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO synced_blogs (store_id, shopify_id, title, handle, synced_at)
                        VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                            title = EXCLUDED.title, handle = EXCLUDED.handle, synced_at = NOW()""",
                        (store_id, news_result["shopify_id"], news_result["title"], news_result.get("handle", "news")),
                    )

            try:
                fresh_articles = client.get_all_articles()
                for fa in fresh_articles:
                    if fa.get("image_url") and "shopify-staged-uploads" not in fa["image_url"]:
                        with db.get_cursor() as cur:
                            cur.execute(
                                "UPDATE synced_articles SET image_url = %s WHERE store_id = %s AND shopify_id = %s AND (image_url IS NULL OR image_url != %s)",
                                (fa["image_url"], store_id, fa["shopify_id"], fa["image_url"]),
                            )
            except Exception as e:
                logger.warning("Failed to refresh article images for landing page: %s", e)

            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT * FROM synced_articles
                    WHERE store_id = %s AND blog_id = %s AND is_published = TRUE
                    AND article_type = 'blog'
                    AND COALESCE(tags, '') NOT ILIKE '%%landing-page%%'
                    ORDER BY COALESCE(published_at, synced_at) DESC""",
                    (store_id, blog_id),
                )
                articles = [dict(a) for a in cur.fetchall()]

            if not articles:
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT * FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
                        (store_id, blog_id),
                    )
                    existing = cur.fetchone()
                if existing and existing.get("shopify_page_id"):
                    try:
                        client.delete_article(existing["shopify_page_id"])
                        logger.info("Deleted landing page for empty blog %s", blog["title"])
                    except Exception as e:
                        logger.warning("Failed to delete landing page for empty blog: %s", e)
                    with db.get_cursor() as cur:
                        cur.execute(
                            "DELETE FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
                            (store_id, blog_id),
                        )
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                        (json.dumps({"skipped": True, "reason": "no published articles"}), task_id),
                    )
                return

            from app.services.blog_landing_generator import generate_blog_landing_html, get_tags

            landing_intro = None
            hero_image_url = None
            hero_image_alt = None
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT intro_text, hero_image_url, hero_image_alt FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
                    (store_id, blog_id),
                )
                lp_row = cur.fetchone()
                if lp_row:
                    landing_intro = lp_row.get("intro_text")
                    hero_image_url = lp_row.get("hero_image_url")
                    hero_image_alt = lp_row.get("hero_image_alt")

            clean_title = re.sub(r'\s+', ' ', blog['title']).strip()
            html_content = generate_blog_landing_html(articles, blog["handle"], clean_title, intro_text=landing_intro, hero_image_url=hero_image_url, hero_image_alt=hero_image_alt)
            all_tags = get_tags(articles[:50])
            article_title = f"{clean_title} — All Articles"
            article_handle = f"{blog['handle']}-landing"
            meta_desc = (landing_intro or f"Browse all {clean_title} articles, tips, and guides.")[:160]

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT * FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
                    (store_id, blog_id),
                )
                existing = cur.fetchone()

            from datetime import datetime, timezone
            now_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            landing_fields = {
                "title": article_title,
                "body": html_content,
                "tags": ["landing-page"],
                "is_published": True,
                "publish_date": now_date,
                "summary": "",
                "image_url": "",
                "clear_image": True,
                "meta_title": article_title[:60],
                "meta_description": meta_desc,
            }

            shopify_article_id = None

            if existing and existing.get("shopify_page_id"):
                try:
                    client.update_article(existing["shopify_page_id"], landing_fields)
                    shopify_article_id = existing["shopify_page_id"]
                    logger.info("Updated existing landing page article %s", shopify_article_id)
                except Exception as e:
                    # Only recreate when the old article is confirmed gone — a
                    # transient error must not mint a duplicate landing.
                    if is_gone_error(e):
                        logger.info("Landing article %s gone on Shopify — recreating", existing["shopify_page_id"])
                    else:
                        raise

            if not shopify_article_id:
                landing_fields.pop("clear_image", None)
                landing_fields.pop("image_url", None)
                landing_fields["handle"] = article_handle
                landing_fields["author"] = store.get("shop_name") or store["name"]
                result = client.create_article(news_blog_shopify_id, landing_fields)
                shopify_article_id = result["shopify_id"]
                logger.info("Created new landing page article %s", shopify_article_id)

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO blog_landing_pages (store_id, blog_id, shopify_page_id, page_handle, page_title, articles_count, tags_count, last_generated_at)
                    VALUES (%s, %s, %s, 'news', %s, %s, %s, NOW())
                    ON CONFLICT (store_id, blog_id) DO UPDATE SET
                        shopify_page_id = EXCLUDED.shopify_page_id,
                        page_handle = 'news',
                        page_title = EXCLUDED.page_title,
                        articles_count = EXCLUDED.articles_count,
                        tags_count = EXCLUDED.tags_count,
                        last_generated_at = NOW()""",
                    (store_id, blog_id, shopify_article_id, article_title, len(articles), len(all_tags)),
                )

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({
                        "shopify_article_id": shopify_article_id,
                        "articles_count": len(articles),
                        "tags_count": len(all_tags),
                    }), task_id),
                )
        except Exception as e:
            logger.exception("Landing page generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_landing, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/landing-status", methods=["GET"])
def landing_status():
    store_id = request.args.get("store_id")
    blog_id = request.args.get("blog_id")
    if not store_id or not blog_id:
        return jsonify(None)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT blp.*, sb.handle as blog_handle
            FROM blog_landing_pages blp
            JOIN synced_blogs sb ON sb.id = blp.blog_id
            WHERE blp.store_id = %s AND blp.blog_id = %s""",
            (store_id, blog_id),
        )
        row = cur.fetchone()
    if not row:
        return jsonify(None)
    result = dict(row)
    result["intro_text"] = result.get("intro_text") or ""
    result["hero_image_url"] = result.get("hero_image_url") or ""
    result["hero_image_alt"] = result.get("hero_image_alt") or ""
    return jsonify(result)


@blog_bp.route("/api/blog/generate-hero-image", methods=["POST"])
def generate_hero_image():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'blog_hero_image', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"store_id": store_id, "blog_id": blog_id})),
        )
        task_id = cur.fetchone()["id"]

    def run_hero_gen():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))

            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM synced_blogs WHERE id = %s AND store_id = %s", (blog_id, store_id))
                blog = cur.fetchone()
            if not blog:
                raise Exception("Blog not found")

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT intro_text FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
                    (store_id, blog_id),
                )
                lp_row = cur.fetchone()
            intro_text = lp_row.get("intro_text") if lp_row else None

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title FROM synced_articles WHERE store_id = %s AND blog_id = %s AND is_published = TRUE AND article_type = 'blog' ORDER BY published_at DESC LIMIT 5",
                    (store_id, blog_id),
                )
                article_titles = [r["title"] for r in cur.fetchall()]

            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT tags FROM synced_articles WHERE store_id = %s AND blog_id = %s AND is_published = TRUE AND tags IS NOT NULL AND article_type = 'blog'",
                    (store_id, blog_id),
                )
                from collections import Counter
                tag_counter = Counter()
                for r in cur.fetchall():
                    for tag in (r["tags"] or "").split(","):
                        tag = tag.strip()
                        if tag and tag.lower() != "landing-page":
                            tag_counter[tag] += 1
                popular_tags = [t for t, _ in tag_counter.most_common(6)]

            from app.services.image_generator import generate_blog_hero_image_ai
            result = generate_blog_hero_image_ai(
                blog["title"],
                intro_text=intro_text,
                article_titles=article_titles,
                tags=popular_tags,
            )
            if not result:
                raise Exception("Hero image generation returned no result")

            image_bytes, filename, alt_text, width, height = result

            mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
            staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
            resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime)

            file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
            file_id = file_obj.get("id")
            if not file_id:
                raise Exception("fileCreate did not return a file ID")
            cdn_url = client.file_get_url(file_id)

            with db.get_cursor() as cur:
                cur.execute(
                    """INSERT INTO blog_landing_pages (store_id, blog_id, hero_image_url, hero_image_alt)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (store_id, blog_id) DO UPDATE SET
                        hero_image_url = EXCLUDED.hero_image_url,
                        hero_image_alt = EXCLUDED.hero_image_alt""",
                    (store_id, blog_id, cdn_url, alt_text),
                )

            _regen_landing_async(store_id, blog_id, client, store)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"hero_image_url": cdn_url, "alt_text": alt_text, "width": width, "height": height}), task_id),
                )
        except Exception as e:
            logger.exception("Blog hero image generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=run_hero_gen, daemon=True).start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/articles/<int:article_id>/regenerate-image", methods=["POST"])
def regenerate_article_image(article_id):
    """Generate a fresh featured image for an existing article and push it to Shopify."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404
    store_id = article["store_id"]
    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'article_image', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"article_id": article_id, "title": article["title"]})),
        )
        task_id = cur.fetchone()["id"]

    def run_image_gen():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))

            keywords = [t.strip() for t in (article.get("tags") or "").split(",") if t.strip()]
            from app.services.image_generator import generate_blog_image_ai
            result = generate_blog_image_ai(article["title"], keywords=keywords)
            if not result:
                raise Exception("Image generation returned no result")
            image_bytes, filename, alt_text, width, height = result

            mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
            staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
            resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime)
            file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
            file_id = file_obj.get("id")
            if not file_id:
                raise Exception("fileCreate did not return a file ID")
            cdn_url = client.file_get_url(file_id)

            client.update_article(article["shopify_id"], {"image_url": cdn_url, "image_alt": alt_text})

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE synced_articles SET image_url = %s, image_alt = %s WHERE id = %s",
                    (cdn_url, alt_text, article_id),
                )
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"image_url": cdn_url, "image_alt": alt_text, "width": width, "height": height}), task_id),
                )
        except Exception as e:
            logger.exception("Article image regeneration failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=run_image_gen, daemon=True).start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/remove-hero-image", methods=["POST"])
def remove_hero_image():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE blog_landing_pages SET hero_image_url = NULL, hero_image_alt = NULL WHERE store_id = %s AND blog_id = %s",
            (store_id, blog_id),
        )

    _regen_landing_async(store_id, blog_id, client, store)
    return jsonify({"ok": True, "success": True})


# ── Wizard pipeline steps ───────────────────────────────────────
# Each step persists its artifact and raises on failure; status transitions
# and ai_tasks bookkeeping stay with the callers (manual routes / auto runner).


def _fetch_wizard_session(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        return cur.fetchone()


def _wizard_step_research(session):
    from app.ai_agent import run_task as ai_run_task

    result = ai_run_task(
        "article_research",
        {
            "session_id": session["id"],
            "topic": session["topic"],
            "keywords": session["target_keywords"],
            "store_id": session["store_id"],
        },
        store_id=session["store_id"],
    )
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET research_data = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(result), session["id"]),
        )
    return result


def _wizard_step_outline(session):
    from app.ai_agent import run_task as ai_run_task

    result = ai_run_task(
        "article_outline",
        {
            "session_id": session["id"],
            "topic": session["topic"],
            "keywords": session["target_keywords"],
            "research_data": session["research_data"],
            "store_id": session["store_id"],
        },
        store_id=session["store_id"],
    )
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET outline_data = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(result), session["id"]),
        )
    return result


def _wizard_step_generate(session):
    from app.ai_agent import run_task as ai_run_task

    result = ai_run_task(
        "article_generate",
        {
            "session_id": session["id"],
            "topic": session["topic"],
            "keywords": session["target_keywords"],
            "outline_data": session["outline_data"],
            "research_data": session["research_data"],
            "store_id": session["store_id"],
        },
        store_id=session["store_id"],
    )
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET generated_article = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(result), session["id"]),
        )
    return result


def _wizard_step_image(session, client, permanent=False):
    from app.services.image_generator import generate_blog_image_ai

    generated = session.get("generated_article") or {}
    title = generated.get("title") or session["topic"]
    keywords = session.get("target_keywords") or []

    product_titles = []
    target_product_ids = session.get("target_product_ids") or []
    if target_product_ids:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title FROM synced_products WHERE id = ANY(%s)",
                (target_product_ids,),
            )
            product_titles = [r["title"] for r in cur.fetchall()]

    result = generate_blog_image_ai(title, keywords=keywords, product_titles=product_titles)
    if not result:
        raise Exception("Image generation returned no result")
    image_bytes, filename, alt_text, width, height = result

    if permanent:
        # Scheduled sessions publish later — staged-upload urls expire before
        # the delayed publish, so register the file for a permanent CDN url.
        from app.routes.news_routes import _upload_image_to_cdn

        image_url = _upload_image_to_cdn(client, image_bytes, filename, alt_text)
    else:
        mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
        staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
        image_url = client.upload_staged_file(staged, image_bytes, content_type=mime)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET featured_image_url = %s, updated_at = NOW() WHERE id = %s",
            (image_url, session["id"]),
        )
    return {"image_url": image_url, "alt_text": alt_text, "width": width, "height": height}


def _wizard_step_publish(session, store, client, is_published=True):
    """Create/update the Shopify article for a wizard session, persist it to
    synced_articles, and mark the session published. Raises on Shopify errors."""
    session_id = session["id"]
    generated = session.get("generated_article") or {}

    # Always publish to News blog
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM synced_blogs WHERE store_id = %s AND handle = 'news'",
            (session["store_id"],),
        )
        blog = cur.fetchone()

    if not blog:
        blog_result = client.create_blog("News")
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO synced_blogs (store_id, shopify_id, title, handle, synced_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                    title = EXCLUDED.title, handle = EXCLUDED.handle, synced_at = NOW()
                RETURNING *""",
                (session["store_id"], blog_result["shopify_id"], blog_result["title"], blog_result.get("handle", "news")),
            )
            blog = cur.fetchone()

    blog_id = blog["id"]

    fields = {
        "title": generated.get("title", session["topic"]),
        "body": generated.get("body_html", ""),
        "summary": generated.get("summary", ""),
        "tags": generated.get("tags", []),
        "meta_title": generated.get("meta_title", ""),
        "meta_description": generated.get("meta_description", ""),
        "is_published": is_published,
    }
    guard_shopify_fields(fields)

    handle = (generated.get("handle") or "").strip()
    if handle:
        fields["handle"] = handle

    if session.get("featured_image_url"):
        fields["image_url"] = session["featured_image_url"]
        fields["image_alt"] = generated.get("title", session["topic"])

    existing_article_id = session.get("shopify_article_id")
    if existing_article_id:
        # Re-save: update the article this session already created rather than
        # creating a duplicate (which would collide on handle).
        raw = client.update_article(existing_article_id, fields)
        result = {
            "shopify_id": raw.get("id") or existing_article_id,
            "title": raw.get("title", ""),
            "handle": raw.get("handle", ""),
            "image_url": fields.get("image_url", ""),
            "image_alt": fields.get("image_alt", ""),
        }
    else:
        try:
            result = client.create_article(blog["shopify_id"], fields)
        except Exception as create_err:
            # A pre-existing article already owns this handle — let Shopify
            # auto-generate a unique one instead of failing the whole save.
            if "Handle has already been taken" in str(create_err):
                fields.pop("handle", None)
                result = client.create_article(blog["shopify_id"], fields)
            else:
                raise

    cdn_image_url = result.get("image_url", "")
    if not cdn_image_url or "shopify-staged-uploads" in cdn_image_url:
        cdn_image_url = fields.get("image_url", "")
    if "shopify-staged-uploads" in (cdn_image_url or ""):
        cdn_image_url = ""

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO synced_articles
                (store_id, blog_id, shopify_id, shopify_blog_id, title, handle,
                 body, summary, tags, meta_title, meta_description,
                 image_url, image_alt, is_published, article_type, synced_at,
                 published_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'blog', NOW(),
                    CASE WHEN %s THEN NOW() END)
            ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                title = EXCLUDED.title, handle = EXCLUDED.handle,
                body = EXCLUDED.body, article_type = 'blog', synced_at = NOW(),
                published_at = COALESCE(synced_articles.published_at, EXCLUDED.published_at)
            RETURNING id""",
            (
                session["store_id"],
                blog_id,
                result["shopify_id"],
                blog["shopify_id"],
                fields["title"],
                result.get("handle", ""),
                fields["body"],
                fields["summary"],
                ", ".join(fields["tags"]) if isinstance(fields["tags"], list) else fields["tags"],
                fields["meta_title"],
                fields["meta_description"],
                cdn_image_url,
                result.get("image_alt", "") or fields.get("image_alt", ""),
                is_published,
                is_published,
            ),
        )
        article_row = cur.fetchone()

        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'published', current_step = 6, shopify_article_id = %s, updated_at = NOW() WHERE id = %s",
            (result["shopify_id"], session_id),
        )

    # Auto-regenerate landing page for this blog
    if is_published:
        _regen_landing_async(session["store_id"], blog_id, client, store)
        from app.services.indexnow_service import ping_async

        host = (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/")
        if host and blog.get("handle") and result.get("handle"):
            ping_async(
                session["store_id"],
                [f"https://{host}/blogs/{blog['handle']}/{result['handle']}"],
                "blog_publish",
            )

    return {"article_id": article_row["id"], "shopify_id": result["shopify_id"]}


def _money_page_keyword_warning(store_id, keywords):
    """Advisory money-page cannibalization check for a blog primary keyword.
    Shared by wizard start (approval time) and the publish scheduler (publish
    time — ownership may have changed while the session sat scheduled)."""
    try:
        from app.services.keyword_resolver import get_owned_primary_keywords, variant_key

        primary = variant_key(keywords[0]) if keywords else ""
        if primary:
            owner = get_owned_primary_keywords(store_id).get(primary)
            if owner:
                return (
                    f'"{keywords[0]}" is the primary keyword of a {owner["item_type"]} page in your '
                    f"keyword strategy — a blog post targeting it would compete with your own money "
                    f"page. Consider an informational long-tail variation instead."
                )
    except Exception:
        pass
    return None


@blog_bp.route("/api/blog/wizard/start", methods=["POST"])
def wizard_start():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    topic = data.get("topic", "").strip()
    keywords = data.get("keywords", [])
    blog_id = data.get("blog_id")
    product_ids = data.get("product_ids", [])
    collection_ids = data.get("collection_ids", [])

    if not store_id or not topic:
        return jsonify({"error": "store_id and topic are required"}), 400

    # Money-page keyword guard: a blog post whose primary keyword is a
    # product/collection's resolved primary would cannibalize it.
    keyword_warning = _money_page_keyword_warning(store_id, keywords)

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO article_wizard_sessions
                (store_id, topic, target_keywords, blog_id, target_product_ids, target_collection_ids, status, current_step)
            VALUES (%s, %s, %s, %s, %s, %s, 'draft', 1) RETURNING id""",
            (store_id, topic, json.dumps(keywords), blog_id, json.dumps(product_ids), json.dumps(collection_ids)),
        )
        session_id = cur.fetchone()["id"]

    resp = {"session_id": session_id}
    if keyword_warning:
        resp["keyword_warning"] = keyword_warning
    return jsonify(resp)


@blog_bp.route("/api/blog/wizard/<int:session_id>", methods=["GET"])
def wizard_get(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(dict(session))


@blog_bp.route("/api/blog/wizard/<int:session_id>/research", methods=["POST"])
def wizard_research(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'researching', current_step = 2, updated_at = NOW() WHERE id = %s",
            (session_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'article_research', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({
                "session_id": session_id,
                "topic": session["topic"],
                "keywords": session["target_keywords"],
                "store_id": session["store_id"],
            })),
        )
        task_id = cur.fetchone()["id"]

    def run_research():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _wizard_step_research(session)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'researched', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Article research failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'error', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_research, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


# ── Topic Discovery: AI market research for "what should I write next?" ──


@blog_bp.route("/api/blog/topics/research", methods=["POST"])
def topic_research_start():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    # Optional focus seed from the Keyword Strategy coverage gaps view:
    # {family, intent, keywords: [{keyword, volume}], count}
    seed = data.get("seed") or None

    with db.get_cursor() as cur:
        cur.execute("SELECT topic_suggestion_count FROM ai_config ORDER BY id LIMIT 1")
        cfg = cur.fetchone()
        count = int(data.get("count") or (cfg or {}).get("topic_suggestion_count") or 3)

        cur.execute(
            "INSERT INTO topic_research_runs (store_id, status) VALUES (%s, 'pending') RETURNING id",
            (store_id,),
        )
        run_id = cur.fetchone()["id"]
        task_input = {"run_id": run_id, "store_id": store_id, "count": count}
        if seed:
            task_input["seed"] = seed
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'topic_discovery', 'pending', %s) RETURNING id""",
            (store_id, json.dumps(task_input)),
        )
        task_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE topic_research_runs SET task_id = %s WHERE id = %s",
            (task_id, run_id),
        )

    def run_discovery():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
                cur.execute("UPDATE topic_research_runs SET status = 'running' WHERE id = %s", (run_id,))
            from app.ai_agent import run_task as ai_run_task
            from app.services.task_runner import is_failed_result

            result = ai_run_task(
                "topic_discovery",
                task_input,
                store_id=store_id,
            )
            if is_failed_result(result):
                error = (result or {}).get("error") or "Topic discovery failed"
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE topic_research_runs SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                        (error, run_id),
                    )
                    cur.execute(
                        "UPDATE ai_tasks SET status = 'failed', error = %s, result = %s, completed_at = NOW() WHERE id = %s",
                        (error, json.dumps(result), task_id),
                    )
                return

            with db.get_cursor() as cur:
                for i, s in enumerate(result.get("suggestions", [])):
                    cur.execute(
                        """INSERT INTO topic_suggestions
                            (run_id, store_id, position, topic, h1, meta_title, meta_description,
                             url_slug, keywords, search_intent, opportunity_type, rationale, cannibalization_note,
                             target_products, target_collections, target_articles)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            run_id, store_id, i,
                            s.get("topic"), s.get("h1"), s.get("meta_title"), s.get("meta_description"),
                            s.get("url_slug"), json.dumps(s.get("keywords") or {}),
                            s.get("search_intent"), s.get("opportunity_type") or "quick_win",
                            s.get("rationale"), s.get("cannibalization_note"),
                            json.dumps(s.get("target_products") or []),
                            json.dumps(s.get("target_collections") or []),
                            json.dumps(s.get("target_articles") or []),
                        ),
                    )
                cur.execute(
                    """UPDATE topic_research_runs SET status = 'completed', market_summary = %s,
                        rejected_topics = %s, completed_at = NOW() WHERE id = %s""",
                    (result.get("market_summary"), json.dumps(result.get("rejected_topics") or []), run_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Topic discovery failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE topic_research_runs SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), run_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_discovery, daemon=True)
    thread.start()
    return jsonify({"run_id": run_id, "task_id": task_id})


@blog_bp.route("/api/blog/topics", methods=["GET"])
def topic_research_list():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        # Fail runs orphaned by a worker restart (daemon threads don't survive).
        cur.execute(
            """UPDATE topic_research_runs r SET status = 'failed',
                error = COALESCE(r.error, 'Interrupted — worker restarted'), completed_at = NOW()
            WHERE r.store_id = %s AND r.status IN ('pending', 'running')
              AND (r.created_at < NOW() - INTERVAL '30 minutes'
                   OR EXISTS (SELECT 1 FROM ai_tasks t WHERE t.id = r.task_id AND t.status = 'failed'))""",
            (store_id,),
        )
        cur.execute(
            """SELECT id, task_id, status, market_summary, rejected_topics, error, created_at, completed_at
            FROM topic_research_runs WHERE store_id = %s ORDER BY created_at DESC LIMIT 20""",
            (store_id,),
        )
        runs = [dict(r) for r in cur.fetchall()]
        run_ids = [r["id"] for r in runs]
        suggestions_by_run = {}
        if run_ids:
            cur.execute(
                "SELECT * FROM topic_suggestions WHERE run_id = ANY(%s) ORDER BY run_id, position",
                (run_ids,),
            )
            for s in cur.fetchall():
                suggestions_by_run.setdefault(s["run_id"], []).append(dict(s))
        # Ideas queued from outside topic research (AI Search gap analysis →
        # "Send to blog pipeline") have no run — surface them as their own group.
        cur.execute(
            "SELECT * FROM topic_suggestions WHERE store_id = %s AND run_id IS NULL ORDER BY id DESC LIMIT 30",
            (store_id,),
        )
        pipeline = [dict(s) for s in cur.fetchall()]
        if pipeline:
            cur.execute(
                """SELECT input->>'suggestion_id' AS sid FROM ai_tasks
                   WHERE task_type = 'topic_enrich' AND status IN ('pending','running')""",
            )
            enriching_ids = {int(r["sid"]) for r in cur.fetchall() if r["sid"]}
            for s in pipeline:
                s["enriching"] = s["id"] in enriching_ids

    def _iso_suggestion(s):
        s["created_at"] = s["created_at"].isoformat() if s.get("created_at") else None
        s["updated_at"] = s["updated_at"].isoformat() if s.get("updated_at") else None

    for r in runs:
        r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
        r["completed_at"] = r["completed_at"].isoformat() if r.get("completed_at") else None
        r["suggestions"] = suggestions_by_run.get(r["id"], [])
        for s in r["suggestions"]:
            _iso_suggestion(s)
    for s in pipeline:
        _iso_suggestion(s)

    return jsonify({"runs": runs, "pipeline": pipeline})


def start_topic_enrich(store_id, suggestion_id):
    """Spawn the single-topic research task for a pipeline-sent suggestion.
    Returns the ai_tasks id, or None when one is already pending/running or the
    AI isn't configured (the suggestion still lands — manual Research remains)."""
    from app.services.task_runner import spawn as spawn_task

    with db.get_cursor() as cur:
        cur.execute("SELECT api_key_enc FROM ai_config ORDER BY id LIMIT 1")
        cfg = cur.fetchone()
        if not cfg or not cfg.get("api_key_enc"):
            return None
        cur.execute(
            """SELECT 1 FROM ai_tasks WHERE task_type = 'topic_enrich'
               AND status IN ('pending','running') AND input->>'suggestion_id' = %s""",
            (str(suggestion_id),),
        )
        if cur.fetchone():
            return None
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'topic_enrich', %s) RETURNING id",
            (store_id, json.dumps({"suggestion_id": suggestion_id})),
        )
        task_id = cur.fetchone()["id"]
    spawn_task("ai_tasks", task_id, _execute_topic_enrich, task_id, suggestion_id, store_id)
    return task_id


def _execute_topic_enrich(task_id, suggestion_id, store_id):
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
    try:
        result = run_task("topic_enrich", {"suggestion_id": suggestion_id}, store_id)
        if result.get("error"):
            raise RuntimeError(result["error"])
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps(result), task_id),
            )
    except Exception as e:
        logger.exception("Topic enrich failed for suggestion %s: %s", suggestion_id, e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@blog_bp.route("/api/blog/topics/suggestions/<int:suggestion_id>/enrich", methods=["POST"])
def enrich_topic_suggestion(suggestion_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT store_id FROM topic_suggestions WHERE id = %s", (suggestion_id,))
        sug = cur.fetchone()
    if not sug:
        return jsonify({"error": "Suggestion not found"}), 404
    task_id = start_topic_enrich(sug["store_id"], suggestion_id)
    if task_id is None:
        return jsonify({"error": "Already researching (or AI not configured in Settings)"}), 409
    return jsonify({"ok": True, "task_id": task_id}), 201


@blog_bp.route("/api/blog/topics/suggestions/<int:suggestion_id>", methods=["PATCH"])
def topic_suggestion_update(suggestion_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    wizard_session_id = data.get("wizard_session_id")
    if status not in ("suggested", "used", "dismissed", "scheduled"):
        return jsonify({"error": "status must be suggested, used, dismissed, or scheduled"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE topic_suggestions SET status = %s,
                wizard_session_id = COALESCE(%s, wizard_session_id), updated_at = NOW()
            WHERE id = %s""",
            (status, wizard_session_id, suggestion_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Suggestion not found"}), 404
    return jsonify({"ok": True})


# ── Scheduled publishing from approved topic suggestions ────────
# Approving a suggestion runs the wizard pipeline unattended, parks the session
# in 'scheduled', and the blog scheduler publishes it when scheduled_for arrives.


def _parse_scheduled_at(value):
    """ISO-8601 string (with Z or offset) → naive UTC datetime. Raises ValueError."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso_utc(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _get_schedule_settings(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT interval_days, publish_time FROM blog_schedule_settings WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()
    interval_days = int(row["interval_days"]) if row and row["interval_days"] else 7
    publish_time = row["publish_time"] if row and row["publish_time"] else dt_time(9, 0)
    return interval_days, publish_time


def _next_open_slot(store_id):
    """Next publish slot from the store's cadence: last upcoming slot + interval,
    or the first upcoming cadence time if the queue is empty. Naive UTC. Failed
    sessions keep holding their slot so a retry doesn't shift the queue."""
    interval_days, publish_time = _get_schedule_settings(store_id)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT MAX(scheduled_for) AS last_slot FROM article_wizard_sessions
               WHERE store_id = %s AND scheduled_for IS NOT NULL
                 AND scheduled_for >= (NOW() AT TIME ZONE 'utc') AND status != 'published'""",
            (store_id,),
        )
        row = cur.fetchone()
    last_slot = row["last_slot"] if row else None
    if last_slot:
        return datetime.combine((last_slot + timedelta(days=interval_days)).date(), publish_time)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    candidate = datetime.combine(now.date(), publish_time)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _flatten_topic_keywords(kw):
    """Primary + grouped keywords → flat deduped list (port of blog.js flattenTopicKeywords)."""
    kw = kw or {}
    out = []
    if kw.get("primary"):
        out.append(kw["primary"])
    for group in ("informational", "commercial", "transactional", "navigational"):
        for w in kw.get(group) or []:
            if w not in out:
                out.append(w)
    return out


def _resolve_product_targets(store_id, names):
    """Suggestion product names → synced_products ids (the manual wizard resolves
    these through the linking search UI; the auto pipeline resolves them here).
    Exact case-insensitive match first, then substring."""
    ids = []
    with db.get_cursor() as cur:
        for name in names or []:
            name = (name or "").strip() if isinstance(name, str) else ""
            if not name:
                continue
            cur.execute(
                "SELECT id FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' AND title ILIKE %s LIMIT 1",
                (store_id, name),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' AND title ILIKE %s ORDER BY LENGTH(title) LIMIT 1",
                    (store_id, f"%{name}%"),
                )
                row = cur.fetchone()
            if row and row["id"] not in ids:
                ids.append(row["id"])
    return ids


def _resolve_collection_targets(store_id, names):
    """Suggestion collection names → synced_collections ids (only collections
    with a live handle, mirroring the wizard's collection search)."""
    ids = []
    with db.get_cursor() as cur:
        for name in names or []:
            name = (name or "").strip() if isinstance(name, str) else ""
            if not name:
                continue
            cur.execute(
                "SELECT id FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' AND title ILIKE %s LIMIT 1",
                (store_id, name),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' AND title ILIKE %s ORDER BY LENGTH(title) LIMIT 1",
                    (store_id, f"%{name}%"),
                )
                row = cur.fetchone()
            if row and row["id"] not in ids:
                ids.append(row["id"])
    return ids


def _run_blog_auto_pipeline(session_id, store, client):
    """Run the wizard pipeline unattended: research → outline → generate →
    featured image, then park in 'scheduled' for the blog scheduler. Resumable —
    steps whose artifact already exists are skipped, so retrying a failed session
    continues where it stopped. Image is best-effort; the rest are fatal (raise)."""

    def _set_status(status):
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE article_wizard_sessions SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, session_id),
            )

    session = _fetch_wizard_session(session_id)
    if not session.get("research_data"):
        _set_status("auto_researching")
        _wizard_step_research(session)
        session = _fetch_wizard_session(session_id)
    if not session.get("outline_data"):
        _set_status("auto_outlining")
        _wizard_step_outline(session)
        session = _fetch_wizard_session(session_id)
    if not session.get("generated_article"):
        _set_status("auto_generating")
        _wizard_step_generate(session)
        session = _fetch_wizard_session(session_id)
    if not session.get("featured_image_url"):
        _set_status("auto_image")
        try:
            # Permanent CDN url — a staged-upload url would expire before the
            # delayed publish.
            _wizard_step_image(session, client, permanent=True)
        except Exception as e:
            logger.warning("Blog auto pipeline: image failed, continuing: %s", e)
    _set_status("scheduled")


def _spawn_blog_auto_pipeline(session_id, store, client):
    # One ai_tasks row per pipeline run so the Tasks panel shows the
    # background writing; per-step tasks stay a manual-wizard concern.
    session = _fetch_wizard_session(session_id)
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'scheduled_blog_post', 'running', %s) RETURNING id""",
            (session["store_id"], json.dumps({"session_id": session_id, "topic": session["topic"]})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            _run_blog_auto_pipeline(session_id, store, client)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"session_id": session_id, "status": "scheduled"}), task_id),
                )
        except Exception as e:
            logger.exception("Blog auto pipeline: session %s failed: %s", session_id, e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                    (str(e), session_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()


@blog_bp.route("/api/blog/schedule-from-suggestion", methods=["POST"])
def schedule_from_suggestion():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    suggestion_id = data.get("suggestion_id")
    if not store_id or not suggestion_id:
        return jsonify({"error": "store_id and suggestion_id are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM topic_suggestions WHERE id = %s AND store_id = %s",
            (suggestion_id, store_id),
        )
        suggestion = cur.fetchone()
    if not suggestion:
        return jsonify({"error": "Suggestion not found"}), 404
    if suggestion["status"] != "suggested":
        return jsonify({"error": "Suggestion is already used, dismissed, or scheduled"}), 409

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    if data.get("scheduled_at"):
        try:
            scheduled_for = _parse_scheduled_at(data["scheduled_at"])
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid scheduled_at"}), 400
        # A mistyped past date must not silently publish on the next tick.
        if scheduled_for < datetime.utcnow():
            return jsonify({"error": "The publish time is in the past — pick a future date/time"}), 400
    else:
        scheduled_for = _next_open_slot(store_id)

    topic = suggestion.get("topic") or suggestion.get("h1") or ""
    keywords = _flatten_topic_keywords(suggestion.get("keywords"))
    product_ids = _resolve_product_targets(store_id, suggestion.get("target_products"))
    collection_ids = _resolve_collection_targets(store_id, suggestion.get("target_collections"))

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO article_wizard_sessions
                (store_id, topic, target_keywords, target_product_ids, target_collection_ids,
                 status, current_step, scheduled_for)
            VALUES (%s, %s, %s, %s, %s, 'auto_queued', 1, %s) RETURNING id""",
            (store_id, topic, json.dumps(keywords), json.dumps(product_ids),
             json.dumps(collection_ids), scheduled_for),
        )
        session_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE topic_suggestions SET status = 'scheduled', wizard_session_id = %s, updated_at = NOW() WHERE id = %s",
            (session_id, suggestion_id),
        )

    _spawn_blog_auto_pipeline(session_id, store, client)
    return jsonify({"session_id": session_id, "scheduled_for": _iso_utc(scheduled_for)})


@blog_bp.route("/api/blog/scheduled", methods=["GET"])
def list_scheduled_posts():
    """List scheduled blog sessions (processing, ready, or failed) for a store."""
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, scheduled_for, error, created_at, updated_at, topic,
                      COALESCE(generated_article->>'title', topic) AS title,
                      (featured_image_url IS NOT NULL) AS has_image
               FROM article_wizard_sessions
               WHERE store_id = %s AND scheduled_for IS NOT NULL AND status != 'published'
               ORDER BY scheduled_for""",
            (store_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"sessions": rows})


@blog_bp.route("/api/blog/scheduled/<int:session_id>", methods=["PATCH"])
def reschedule_blog_post(session_id):
    """Change a scheduled blog session's publish time."""
    data = request.get_json(silent=True) or {}
    try:
        scheduled_for = _parse_scheduled_at(data.get("scheduled_at"))
    except (ValueError, TypeError):
        return jsonify({"error": "A valid scheduled_at is required"}), 400
    if scheduled_for < datetime.utcnow():
        return jsonify({"error": "The new publish time is in the past — pick a future date/time"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE article_wizard_sessions SET scheduled_for = %s, updated_at = NOW()
               WHERE id = %s AND scheduled_for IS NOT NULL
                 AND status NOT IN ('published', 'auto_publishing')""",
            (scheduled_for, session_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Session is publishing or already published"}), 409
    return jsonify({"success": True})


@blog_bp.route("/api/blog/scheduled/<int:session_id>/publish-now", methods=["POST"])
def publish_blog_now(session_id):
    """Publish a ready session immediately, or resume a failed one from the
    first missing pipeline artifact."""
    session = _fetch_wizard_session(session_id)
    if not session or not session.get("scheduled_for"):
        return jsonify({"error": "Session not found"}), 404

    ready = bool(session.get("generated_article"))
    # Atomic claim — same guard as the scheduler tick, so the two can never
    # double-publish the same session.
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE article_wizard_sessions SET status = %s, error = NULL, updated_at = NOW()
               WHERE id = %s AND status IN ('scheduled', 'failed') RETURNING store_id""",
            ("auto_publishing" if ready else "auto_queued", session_id),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Session is not ready to publish"}), 409

    store, client = _get_store_client(row["store_id"])
    if not client:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE article_wizard_sessions SET status = 'failed', error = 'Store not found', updated_at = NOW() WHERE id = %s",
                (session_id,),
            )
        return jsonify({"error": "Store not found"}), 404

    if ready:
        def _run():
            try:
                _wizard_step_publish(_fetch_wizard_session(session_id), store, client, True)
            except Exception as e:
                logger.exception("publish_blog_now: session %s failed: %s", session_id, e)
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE article_wizard_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                        (str(e), session_id),
                    )

        threading.Thread(target=_run, daemon=True).start()
    else:
        # Failed mid-pipeline: resume; the session parks 'scheduled' and an
        # overdue slot publishes on the next scheduler tick (≤60s later).
        _spawn_blog_auto_pipeline(session_id, store, client)
    return jsonify({"success": True, "resumed": not ready})


@blog_bp.route("/api/blog/scheduled/<int:session_id>", methods=["DELETE"])
def cancel_scheduled_post(session_id):
    """Cancel a scheduled post; the suggestion returns to the idea pool.
    Disallowed mid-processing or once publishing."""
    with db.get_cursor() as cur:
        # Look the suggestion up first — deleting the session would SET NULL
        # its wizard_session_id and lose the link.
        cur.execute("SELECT id FROM topic_suggestions WHERE wizard_session_id = %s", (session_id,))
        sugg = cur.fetchone()
        cur.execute(
            """DELETE FROM article_wizard_sessions
               WHERE id = %s AND scheduled_for IS NOT NULL
                 AND status IN ('scheduled', 'failed', 'auto_queued')""",
            (session_id,),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Session is still processing or already published"}), 409
        if sugg:
            cur.execute(
                "UPDATE topic_suggestions SET status = 'suggested', wizard_session_id = NULL, updated_at = NOW() WHERE id = %s",
                (sugg["id"],),
            )
    return jsonify({"success": True})


@blog_bp.route("/api/blog/schedule-settings", methods=["GET"])
def get_schedule_settings():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    interval_days, publish_time = _get_schedule_settings(store_id)
    return jsonify({
        "interval_days": interval_days,
        "publish_time": publish_time.strftime("%H:%M"),
        "next_slot": _iso_utc(_next_open_slot(store_id)),
    })


@blog_bp.route("/api/blog/schedule-settings", methods=["PUT"])
def save_schedule_settings():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    try:
        interval_days = max(1, int(data.get("interval_days") or 7))
        hours, minutes = str(data.get("publish_time") or "09:00").split(":")
        publish_time = dt_time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return jsonify({"error": "interval_days must be a number and publish_time HH:MM"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO blog_schedule_settings (store_id, interval_days, publish_time, updated_at)
               VALUES (%s, %s, %s, NOW())
               ON CONFLICT (store_id) DO UPDATE SET
                   interval_days = EXCLUDED.interval_days,
                   publish_time = EXCLUDED.publish_time, updated_at = NOW()""",
            (store_id, interval_days, publish_time),
        )
    return jsonify({"success": True, "next_slot": _iso_utc(_next_open_slot(store_id))})


@blog_bp.route("/api/blog/wizard/<int:session_id>/keywords", methods=["PUT"])
def wizard_update_keywords(session_id):
    data = request.json or {}
    keywords = data.get("keywords", [])
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET target_keywords = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(keywords), session_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Wizard session not found"}), 404
    return jsonify({"ok": True})


@blog_bp.route("/api/blog/wizard/search/products", methods=["GET"])
def wizard_search_products():
    store_id = request.args.get("store_id")
    q = request.args.get("q", "").strip()
    if not store_id or not q:
        return jsonify([])
    pattern = f"%{q}%"
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, title, handle, vendor, product_type FROM synced_products
            WHERE store_id = %s AND status = 'ACTIVE'
            AND (title ILIKE %s OR handle ILIKE %s OR vendor ILIKE %s
                 OR product_type ILIKE %s OR tags ILIKE %s OR description ILIKE %s)
            ORDER BY title LIMIT 20""",
            (store_id, pattern, pattern, pattern, pattern, pattern, pattern),
        )
        results = [dict(r) for r in cur.fetchall()]
    return jsonify(results)


@blog_bp.route("/api/blog/wizard/search/collections", methods=["GET"])
def wizard_search_collections():
    store_id = request.args.get("store_id")
    q = request.args.get("q", "").strip()
    if not store_id or not q:
        return jsonify([])
    pattern = f"%{q}%"
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, title, handle, collection_type, products_count FROM synced_collections
            WHERE store_id = %s AND handle IS NOT NULL AND handle != ''
            AND (title ILIKE %s OR handle ILIKE %s OR description ILIKE %s)
            ORDER BY title LIMIT 20""",
            (store_id, pattern, pattern, pattern),
        )
        results = [dict(r) for r in cur.fetchall()]
    return jsonify(results)


@blog_bp.route("/api/blog/wizard/<int:session_id>/linking", methods=["PUT"])
def wizard_update_linking(session_id):
    data = request.get_json(silent=True) or {}
    product_ids = data.get("product_ids", [])
    collection_ids = data.get("collection_ids", [])
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET target_product_ids = %s, target_collection_ids = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(product_ids), json.dumps(collection_ids), session_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Wizard session not found"}), 404
    return jsonify({"ok": True})


@blog_bp.route("/api/blog/wizard/<int:session_id>/generate-image", methods=["POST"])
def wizard_generate_image(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    generated = session.get("generated_article") or {}
    title = generated.get("title") or session["topic"]

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'generating_image', updated_at = NOW() WHERE id = %s",
            (session_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'blog_image', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({"session_id": session_id, "title": title})),
        )
        task_id = cur.fetchone()["id"]

    def run_image_gen():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _wizard_step_image(session, client)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'generated', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Blog image generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'generated', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_image_gen, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/wizard/<int:session_id>/outline", methods=["POST"])
def wizard_outline(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'outlining', current_step = 3, updated_at = NOW() WHERE id = %s",
            (session_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'article_outline', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({
                "session_id": session_id,
                "topic": session["topic"],
                "keywords": session["target_keywords"],
                "research_data": session["research_data"],
                "store_id": session["store_id"],
            })),
        )
        task_id = cur.fetchone()["id"]

    def run_outline():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _wizard_step_outline(session)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'outlined', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Article outline failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'error', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_outline, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/wizard/<int:session_id>/outline", methods=["PUT"])
def wizard_update_outline(session_id):
    data = request.get_json(silent=True) or {}
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET outline_data = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(data.get("outline_data", {})), session_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Wizard session not found"}), 404
    return jsonify({"ok": True, "success": True})


@blog_bp.route("/api/blog/wizard/<int:session_id>/generate", methods=["POST"])
def wizard_generate(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'generating', current_step = 4, updated_at = NOW() WHERE id = %s",
            (session_id,),
        )

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'article_generate', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({
                "session_id": session_id,
                "topic": session["topic"],
                "keywords": session["target_keywords"],
                "outline_data": session["outline_data"],
                "research_data": session["research_data"],
                "store_id": session["store_id"],
            })),
        )
        task_id = cur.fetchone()["id"]

    def run_generate():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _wizard_step_generate(session)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'generated', current_step = 5, updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Article generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = 'error', updated_at = NOW() WHERE id = %s",
                    (session_id,),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_generate, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/wizard/<int:session_id>/review", methods=["PUT"])
def wizard_update_review(session_id):
    data = request.get_json(silent=True) or {}
    with db.get_cursor() as cur:
        updates = ["generated_article = %s", "updated_at = NOW()"]
        params = [json.dumps(data.get("generated_article", {}))]
        if "blog_id" in data and data["blog_id"]:
            updates.append("blog_id = %s")
            params.append(data["blog_id"])
        params.append(session_id)
        cur.execute(
            f"UPDATE article_wizard_sessions SET {', '.join(updates)} WHERE id = %s",
            params,
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Wizard session not found"}), 404
    return jsonify({"ok": True, "success": True})


@blog_bp.route("/api/blog/wizard/<int:session_id>/publish", methods=["POST"])
def wizard_publish(session_id):
    data = request.get_json(silent=True) or {}
    is_published = data.get("is_published", False)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM article_wizard_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    try:
        result = _wizard_step_publish(session, store, client, is_published)
    except Exception as e:
        logger.exception("Wizard publish failed for session %s: %s", session_id, e)
        return jsonify({"error": f"Shopify rejected the article: {e}"}), 502

    return jsonify({
        "success": True,
        "article_id": result["article_id"],
        "shopify_id": result["shopify_id"],
    })


# ── Empty Blog Cleanup ──────────────────────────────────────────

@blog_bp.route("/api/blog/cleanup-empty-blogs", methods=["POST"])
def cleanup_empty_blogs():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sb.id, sb.shopify_id, sb.title, sb.handle,
                (SELECT COUNT(*) FROM synced_articles sa WHERE sa.blog_id = sb.id) as article_count
            FROM synced_blogs sb
            WHERE sb.store_id = %s AND sb.handle != 'news'""",
            (store_id,),
        )
        blogs = [dict(b) for b in cur.fetchall()]

    empty_blogs = [b for b in blogs if b["article_count"] == 0]
    deleted = []
    for blog in empty_blogs:
        try:
            client.delete_blog(blog["shopify_id"])
            with db.get_cursor() as cur:
                cur.execute("DELETE FROM synced_blogs WHERE id = %s", (blog["id"],))
            deleted.append(blog["title"])
            logger.info("Deleted empty blog: %s (%s)", blog["title"], blog["shopify_id"])
        except Exception as e:
            logger.warning("Failed to delete blog %s: %s", blog["title"], e)

    return jsonify({"deleted": deleted, "count": len(deleted)})


@blog_bp.route("/api/blog/empty-blogs", methods=["GET"])
def list_empty_blogs():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify([])

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sb.id, sb.title, sb.handle,
                (SELECT COUNT(*) FROM synced_articles sa WHERE sa.blog_id = sb.id) as article_count
            FROM synced_blogs sb
            WHERE sb.store_id = %s AND sb.handle != 'news'""",
            (store_id,),
        )
        blogs = [dict(b) for b in cur.fetchall()]

    return jsonify([b for b in blogs if b["article_count"] == 0])


# ── AI Suggestions for Blog Title & Intro ───────────────────────

@blog_bp.route("/api/blog/ai-suggestions", methods=["POST"])
def blog_ai_suggestions():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_blogs WHERE id = %s AND store_id = %s", (blog_id, store_id))
        blog = cur.fetchone()
    if not blog:
        return jsonify({"error": "Blog not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT title, tags FROM synced_articles WHERE store_id = %s AND blog_id = %s AND is_published = TRUE AND article_type = 'blog' ORDER BY published_at DESC LIMIT 30",
            (store_id, blog_id),
        )
        articles = cur.fetchall()

    article_titles = [a["title"] for a in articles]
    all_tags = set()
    for a in articles:
        for tag in (a["tags"] or "").split(","):
            tag = tag.strip()
            if tag and tag.lower() != "landing-page":
                all_tags.add(tag)

    current_intro = ""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT intro_text FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
            (store_id, blog_id),
        )
        lp_row = cur.fetchone()
        if lp_row:
            current_intro = lp_row.get("intro_text") or ""

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'blog_seo_suggestions', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"store_id": store_id, "blog_id": blog_id})),
        )
        task_id = cur.fetchone()["id"]

    def run_suggestions():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            from app.ai_agent import run_task as ai_run_task
            result = ai_run_task(
                "blog_seo_suggestions",
                {
                    "blog_title": blog["title"],
                    "blog_handle": blog["handle"],
                    "current_intro": current_intro,
                    "article_titles": article_titles,
                    "article_tags": list(all_tags),
                    "store_id": store_id,
                },
                store_id=store_id,
            )
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Blog AI suggestions failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    thread = threading.Thread(target=run_suggestions, daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/save-intro", methods=["POST"])
def save_intro():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    intro_text = data.get("intro_text", "").strip()
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO blog_landing_pages (store_id, blog_id, intro_text)
            VALUES (%s, %s, %s)
            ON CONFLICT (store_id, blog_id) DO UPDATE SET intro_text = EXCLUDED.intro_text""",
            (store_id, blog_id, intro_text),
        )

    return jsonify({"ok": True, "success": True})


# ── Blog Rename ─────────────────────────────────────────────────

@blog_bp.route("/api/blog/blogs/<int:blog_id>", methods=["PUT"])
def update_blog(blog_id):
    data = request.get_json(silent=True) or {}
    title = re.sub(r'\s+', ' ', data.get("title", "")).strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    if len(title) > 255:
        return jsonify({"error": f"Title is too long ({len(title)} chars, max 255)"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_blogs WHERE id = %s", (blog_id,))
        blog = cur.fetchone()
    if not blog:
        return jsonify({"error": "Blog not found"}), 404

    store, client = _get_store_client(blog["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    result = client.update_blog(blog["shopify_id"], {"title": title})

    meta_title = data.get("meta_title", "").strip() or None
    meta_description = data.get("meta_description", "").strip() or None
    if meta_title or meta_description:
        try:
            client.set_blog_seo(blog["shopify_id"], meta_title, meta_description)
        except Exception as e:
            logger.warning("Failed to set blog SEO metafields: %s", e)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE synced_blogs SET title = %s, synced_at = NOW() WHERE id = %s",
            (result.get("title", title), blog_id),
        )

    return jsonify({"ok": True, "success": True, "title": result.get("title", title)})


# ── Refresh Images ───────────────────────────────────────────────

@blog_bp.route("/api/blog/refresh-images", methods=["POST"])
def refresh_images():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    updated = 0
    try:
        fresh_articles = client.get_all_articles()
        for fa in fresh_articles:
            if fa.get("image_url") and "shopify-staged-uploads" not in fa["image_url"]:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_articles SET image_url = %s WHERE store_id = %s AND shopify_id = %s AND (image_url IS NULL OR image_url != %s)",
                        (fa["image_url"], store_id, fa["shopify_id"], fa["image_url"]),
                    )
                    updated += cur.rowcount
    except Exception as e:
        logger.exception("Failed to refresh article images: %s", e)
        return jsonify({"error": str(e)}), 500

    if updated > 0:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_blogs WHERE id = %s", (blog_id,))
            blog = cur.fetchone()
        if blog:
            _regen_landing_async(store_id, blog_id, client, store)

    return jsonify({"ok": True, "success": True, "updated": updated})


# ── Theme SEO Snippet ───────────────────────────────────────────

_META_DESC_MARKER = "<!-- seo-platform:commented-meta-desc -->"

_META_DESC_PATTERN = re.compile(
    r'(<meta\s+name\s*=\s*["\']description["\'][^>]*>)',
    re.IGNORECASE,
)


def _comment_out_meta_descriptions(content):
    """Comment out <meta name="description"> tags. Returns (modified, count)."""
    count = 0

    def _replace(match):
        nonlocal count
        tag = match.group(1)
        if _META_DESC_MARKER in content[max(0, match.start() - 80):match.start()]:
            return match.group(0)
        count += 1
        return f"{_META_DESC_MARKER}{{% comment %}}{tag}{{% endcomment %}}"

    result = _META_DESC_PATTERN.sub(_replace, content)
    return result, count


def _restore_meta_descriptions(content):
    """Undo _comment_out_meta_descriptions — restore original tags."""
    pattern = re.compile(
        re.escape(_META_DESC_MARKER) + r'\{%\s*comment\s*%\}(.*?)\{%\s*endcomment\s*%\}',
        re.DOTALL,
    )
    return pattern.sub(r'\1', content)


_META_DESC_LIKELY_FILES = [
    "layout/theme.liquid",
    "layout/gift_card.liquid",
    "snippets/head-tag.liquid",
    "snippets/meta-tags.liquid",
    "snippets/seo.liquid",
    "snippets/head.liquid",
    "sections/header.liquid",
    "sections/head.liquid",
]


def _scan_and_fix_meta_descriptions(client, theme_id):
    """Check known theme files for meta description tags and comment them out.
    Returns dict of {asset_key: count_commented}."""
    results = {}
    for asset_key in _META_DESC_LIKELY_FILES:
        try:
            content = client.get_theme_asset(theme_id, asset_key)
            if not content:
                continue
            if 'name="description"' not in content.lower() and "name='description'" not in content.lower():
                continue
            modified, count = _comment_out_meta_descriptions(content)
            if count > 0:
                client.upsert_theme_asset(theme_id, asset_key, modified)
                results[asset_key] = count
                logger.info("Commented out %d meta description(s) in %s", count, asset_key)
        except Exception as e:
            logger.warning("Failed to process %s: %s", asset_key, e)

    return results


@blog_bp.route("/api/blog/push-seo-snippet", methods=["POST"])
def push_seo_snippet():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "No main theme found"}), 404

        from app.services.seo_snippets import generate_seo_snippet
        snippet_content = generate_seo_snippet()

        client.upsert_theme_asset(theme_id, "snippets/seo-fixes.liquid", snippet_content)

        theme_liquid = client.get_theme_asset(theme_id, "layout/theme.liquid")
        if not theme_liquid:
            return jsonify({"error": "Could not read layout/theme.liquid"}), 500

        backup = theme_liquid

        if "{% render 'seo-fixes' %}" not in theme_liquid:
            theme_liquid = theme_liquid.replace("</head>", "  {% render 'seo-fixes' %}\n</head>")
            client.upsert_theme_asset(theme_id, "layout/theme.liquid", theme_liquid)

        desc_results = _scan_and_fix_meta_descriptions(client, theme_id)

        return jsonify({
            "success": True,
            "snippet_pushed": True,
            "theme_liquid_updated": "{% render 'seo-fixes' %}" in theme_liquid,
            "meta_descriptions_commented": desc_results,
            "backup_length": len(backup),
        })
    except Exception as e:
        logger.exception("Failed to push SEO snippet: %s", e)
        return jsonify({"error": str(e)}), 500


@blog_bp.route("/api/blog/revert-seo-snippet", methods=["POST"])
def revert_seo_snippet():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "No main theme found"}), 404

        theme_liquid = client.get_theme_asset(theme_id, "layout/theme.liquid")
        if theme_liquid:
            theme_liquid = theme_liquid.replace("  {% render 'seo-fixes' %}\n", "")
            theme_liquid = theme_liquid.replace("{% render 'seo-fixes' %}\n", "")
            theme_liquid = theme_liquid.replace("{% render 'seo-fixes' %}", "")
            client.upsert_theme_asset(theme_id, "layout/theme.liquid", theme_liquid)

        for asset_key in _META_DESC_LIKELY_FILES:
            try:
                content = client.get_theme_asset(theme_id, asset_key)
                if content and _META_DESC_MARKER in content:
                    restored = _restore_meta_descriptions(content)
                    client.upsert_theme_asset(theme_id, asset_key, restored)
            except Exception:
                pass

        return jsonify({"ok": True, "success": True, "reverted": True})
    except Exception as e:
        logger.exception("Failed to revert SEO snippet: %s", e)
        return jsonify({"error": str(e)}), 500


@blog_bp.route("/api/blog/seo-snippet-status", methods=["GET"])
def seo_snippet_status():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"installed": False})

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"installed": False})

    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"installed": False})

        theme_liquid = client.get_theme_asset(theme_id, "layout/theme.liquid")
        installed = theme_liquid is not None and "{% render 'seo-fixes' %}" in theme_liquid
        if not installed:
            return jsonify({"installed": False})

        snippet_content = client.get_theme_asset(theme_id, "snippets/seo-fixes.liquid")
        outdated = snippet_content is not None and not any(v in snippet_content for v in ("v2", "v3", "v4"))
        return jsonify({"installed": True, "outdated": outdated})
    except Exception:
        return jsonify({"installed": False})


# ── URL Redirects ──────────────────────────────────────────────

_KNOWN_OLD_HANDLES = ["blog", "black-and-mild", "zyn", "loose-leaf-wraps", "dutch-masters", "cigarillos"]


@blog_bp.route("/api/blog/detect-old-blogs", methods=["GET"])
def detect_old_blogs():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    paths = []
    for handle in _KNOWN_OLD_HANDLES:
        paths.append({"path": f"/blogs/{handle}", "target": "/blogs/news"})

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle FROM synced_blogs WHERE store_id = %s AND handle != 'news'",
            (store_id,),
        )
        for row in cur.fetchall():
            handle = row["handle"]
            if handle not in _KNOWN_OLD_HANDLES:
                paths.append({"path": f"/blogs/{handle}", "target": "/blogs/news"})

    return jsonify({"redirects": paths})


@blog_bp.route("/api/blog/create-redirects", methods=["POST"])
def create_redirects():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    redirects = data.get("redirects", [])
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    if not redirects:
        return jsonify({"error": "No redirects provided"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    results = []
    for r in redirects:
        path = r.get("path", "")
        target = r.get("target", "/blogs/news")
        try:
            client.create_url_redirect(path, target)
            results.append({"path": path, "target": target, "status": "created"})
        except Exception as e:
            results.append({"path": path, "target": target, "status": "error", "error": str(e)})

    created = sum(1 for r in results if r["status"] == "created")
    failed = sum(1 for r in results if r["status"] == "error")
    return jsonify({"results": results, "created": created, "failed": failed})
