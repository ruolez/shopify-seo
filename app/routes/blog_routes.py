import json
import logging
import re
import threading
import time
from datetime import datetime, time as dt_time, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services import publish_gate
from app.services.apply_pipeline import ApplyRefused, apply_item_changes, refusal_response
from app.services.ai_output_guard import guard_shopify_fields
from app.services.blog_seo_analyzer import analyze_article_seo
from app.services.blog_import import (
    ARTICLE_KEYS,
    OUTLINE_KEYS,
    apply_locked_fields,
    check_brief,
    dedupe_keywords,
    locked_fields_of,
    parse_blog_briefs,
)
from app.services.title_match import titles_match
from app.services.gsc_page_totals import article_gsc_metrics, attach_article_metrics
from app.services.article_visuals import (
    STYLE_CLASSIC,
    STYLE_VISUAL,
    STYLES,
    get_article_visuals,
    save_store_visuals,
    visuals_with_warnings,
)
from app.services.blog_template import (
    BLOG_SECTION_FILE,
    BLOG_TEMPLATE_FILE,
    BLOG_TEMPLATE_MARKER,
    BLOG_TEMPLATE_MARKER_PREFIX,
    BLOG_TEMPLATE_SUFFIX,
    BLOG_TEMPLATE_VERSION,
    get_article_css,
    get_blog_theme_files,
)
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


def _session_style(session):
    style = (session or {}).get("style") or STYLE_VISUAL
    return style if style in STYLES else STYLE_VISUAL


def _resolve_article_style(store_id, requested=None):
    """Explicit request wins; otherwise the store's default style."""
    if requested in STYLES:
        return requested
    return get_article_visuals(store_id)["default_style"]


def _repin_landing_article(store_id, blog_id, client):
    """Bump the existing landing article's publish date so it stays the newest
    article. The theme's blog index renders the newest article's content as the
    page, so from the moment a new article is published until the landing is
    republished, /blogs/<handle> shows that article instead of the index."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT shopify_page_id FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
            (store_id, blog_id),
        )
        row = cur.fetchone()
    if not row or not row.get("shopify_page_id"):
        return
    from datetime import datetime, timezone

    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.update_article(row["shopify_page_id"], {"is_published": True, "publish_date": now_date})


def _hub_collections(store_id, topics):
    """{handle: title} for the topic collections that exist in the store."""
    handles = [t.get("collection_handle") for t in topics if t.get("collection_handle")]
    if not handles:
        return {}
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle, title FROM synced_collections WHERE store_id = %s AND handle = ANY(%s)",
            (store_id, handles),
        )
        return {r["handle"]: r["title"] for r in cur.fetchall()}


def _hub_news(store_id, blog_id):
    """(newest published news articles in this blog, news hub URL or None)."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT title, handle, published_at, synced_at FROM synced_articles
               WHERE store_id = %s AND blog_id = %s AND is_published = TRUE AND article_type = 'news'
               ORDER BY COALESCE(published_at, synced_at) DESC LIMIT 3""",
            (store_id, blog_id),
        )
        news = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT handle FROM news_hub_pages WHERE store_id = %s AND shopify_page_id IS NOT NULL", (store_id,))
        hub = cur.fetchone()
    return news, (f"/pages/{hub['handle'] or 'news'}" if hub else None)


def _build_and_push_landing(store_id, blog_id, client, store, create_news_blog=False):
    """Build the hub body and write it to the landing article. Returns the
    result dict for ai_tasks, or None when there is nothing to do."""
    from app.prompt_defaults import DEFAULTS
    from app.services.blog_landing_generator import generate_blog_landing_html, get_tags
    from app.services.gsc_page_totals import page_totals_by_path
    from app.settings_manager import get_setting

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_blogs WHERE id = %s AND store_id = %s", (blog_id, store_id))
        blog = cur.fetchone()
    if not blog:
        return None

    with db.get_cursor() as cur:
        cur.execute("SELECT shopify_id FROM synced_blogs WHERE store_id = %s AND handle = 'news'", (store_id,))
        news_row = cur.fetchone()
    news_blog_shopify_id = news_row["shopify_id"] if news_row else None
    if not news_blog_shopify_id:
        if not create_news_blog:
            return None
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
        for fa in client.get_all_articles():
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
        cur.execute("SELECT * FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s", (store_id, blog_id))
        existing = cur.fetchone() or {}

    if not articles:
        if existing.get("shopify_page_id"):
            try:
                client.delete_article(existing["shopify_page_id"])
            except Exception as e:
                if not is_gone_error(e):
                    # Keep the tracking row — dropping it would orphan a
                    # still-live landing article with no local record.
                    logger.warning("Landing delete failed for blog %s, keeping tracking row: %s", blog_id, e)
                    return None
            with db.get_cursor() as cur:
                cur.execute("DELETE FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s", (store_id, blog_id))
        return {"skipped": True, "reason": "no published articles"}

    topics = get_setting("lists.blog_hub_topics", DEFAULTS["lists.blog_hub_topics"]) or []
    clicks, _ = page_totals_by_path(store_id, days=90, path_contains=f"/blogs/{blog['handle']}/")
    news_articles, news_hub_url = _hub_news(store_id, blog_id)
    html_content = generate_blog_landing_html(
        articles, blog["handle"],
        intro_text=existing.get("intro_text"),
        hero_image_url=existing.get("hero_image_url"),
        hero_image_alt=existing.get("hero_image_alt"),
        topics=topics, clicks=clicks,
        collections=_hub_collections(store_id, topics),
        news_articles=news_articles, news_hub_url=news_hub_url,
    )
    clean_title = re.sub(r'\s+', ' ', blog['title']).strip()
    article_title = f"{clean_title} — All Articles"
    all_tags = get_tags(articles[:50])

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
        "meta_description": (existing.get("hub_meta_description") or existing.get("intro_text")
                             or f"Browse all {clean_title} articles, tips, and guides.")[:160],
    }

    shopify_article_id = None
    if existing.get("shopify_page_id"):
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
        shopify_article_id = client.create_article(news_blog_shopify_id, landing_fields)["shopify_id"]

    # The landing article's own URL duplicates /blogs/<handle>: seo.hidden
    # noindexes it and drops it from the sitemap.
    try:
        client.set_article_metafields(shopify_article_id, [("seo", "hidden", "1", "number_integer")])
    except Exception as e:
        logger.warning("Could not hide landing article %s from search: %s", shopify_article_id, e)

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
    logger.info("Regenerated landing page for blog %s", blog["title"])
    return {"shopify_article_id": shopify_article_id, "articles_count": len(articles), "tags_count": len(all_tags)}


def _regen_landing_async(store_id, blog_id, client, store):
    def _run():
        try:
            _repin_landing_article(store_id, blog_id, client)
        except Exception as e:
            logger.warning("Failed to re-pin landing article for blog %s: %s", blog_id, e)

        time.sleep(5)
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                _build_and_push_landing(store_id, blog_id, client, store)
                return
            except Exception as e:
                if attempt < attempts:
                    logger.warning(
                        "Landing regeneration attempt %d/%d failed for blog %s, retrying: %s",
                        attempt, attempts, blog_id, e,
                    )
                    time.sleep(15 * attempt)
                else:
                    logger.exception("Failed to auto-regenerate landing page after %d attempts: %s", attempts, e)

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
                      status, scheduled_for, reviewed_at
               FROM article_wizard_sessions
               WHERE store_id = %s AND scheduled_for >= %s AND scheduled_for < %s
               UNION ALL
               SELECT id, 'news', COALESCE(edited_result->>'headline', analyze_result->>'headline', 'News article'),
                      status, scheduled_for, reviewed_at
               FROM news_sessions
               WHERE store_id = %s AND scheduled_for >= %s AND scheduled_for < %s
               ORDER BY scheduled_for""",
            (store_id, start, end, store_id, start, end),
        )
        rows = cur.fetchall()
    # The schedulers publish only reviewed sessions — the calendar must say
    # so, or a queued-but-unapproved post looks like it will go out.
    entries = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "title": r["title"],
            "status": r["status"],
            "scheduled_for": r["scheduled_for"].replace(tzinfo=timezone.utc).isoformat() if r["scheduled_for"] else None,
            "needs_approval": r["status"] == "scheduled" and r["reviewed_at"] is None,
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
        "created_at": "COALESCE(sa.shopify_created_at, sa.published_at, sa.synced_at)",
    }
    python_sorts = {"gsc_clicks", "gsc_impressions", "seo_score"}
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

    # The Articles tab filters by blog_id only — take the store from the rows.
    metrics_store_id = store_id or (articles[0]["store_id"] if articles else None)
    gsc_by_path, gsc_window = article_gsc_metrics(metrics_store_id) if metrics_store_id else ({}, None)

    result = []
    for a in articles:
        article_dict = dict(a)
        seo = analyze_article_seo(article_dict)
        article_dict["seo_score"] = seo["score"]
        article_dict["seo_breakdown"] = seo["breakdown"]
        article_dict["created_at_display"] = (
            article_dict.get("shopify_created_at") or article_dict.get("published_at") or article_dict.get("synced_at")
        )
        result.append(attach_article_metrics(article_dict, gsc_by_path, gsc_window))

    if sort_by in python_sorts:
        reverse = sort_dir == "desc"
        result.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or 0), reverse=reverse)
        if reverse:
            # Keep "no data" rows at the bottom in both directions.
            result.sort(key=lambda r: r.get(sort_by) is None)

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

    tags = fields.pop("tags", None)
    try:
        applied = apply_item_changes(
            article["store_id"], "article", article_id, fields,
            source=data.get("source") or "blog", change_type=data.get("change_type") or "article_edit",
            client=client, extra_fields={"tags": tags} if tags is not None else None,
            allow_dropped_blocks=bool(data.get("force")),
            # A hand edit may remove an image on purpose; an AI refresh may not.
            restore_media=data.get("change_type") == "article_refresh",
        )
    except ApplyRefused as refused:
        return refusal_response(refused)

    if tags is not None:
        with db.get_cursor() as cur:
            cur.execute("UPDATE synced_articles SET tags = %s WHERE id = %s", (tags, article_id))

    if applied["status"] == "noop" and tags is None:
        return jsonify({"ok": True, "success": True, "message": "Already up to date — nothing changed"})

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


# ── Refresh (rewrite in place around real searches) ──────────────

@blog_bp.route("/api/blog/articles/<int:article_id>/refresh", methods=["POST"])
def refresh_article(article_id):
    """Start a refresh proposal for one article. Nothing is written to Shopify
    until the person applies the preview."""
    with db.get_cursor() as cur:
        cur.execute("SELECT store_id FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'article_refresh', 'pending', %s) RETURNING id""",
            (article["store_id"], json.dumps({"article_id": article_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        from app.services.article_refresh import run_article_refresh

        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = run_article_refresh(article_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Article refresh failed for %s: %s", article_id, e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/refresh-candidates", methods=["GET"])
def refresh_candidates():
    """Articles Google shows but few click, ranked by the clicks a refresh could add."""
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    from app.services.dashboard_service import get_article_refresh_candidates

    return jsonify({"candidates": get_article_refresh_candidates(store_id, limit=20)})


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
                                    # Reads the live article, restores dropped
                                    # figures, and raises instead of writing when
                                    # the post changed in Shopify or the rewrite
                                    # drops protected content.
                                    applied = apply_item_changes(
                                        item["store_id"], "article", item["article_id"], fields,
                                        source="blog", change_type="bulk_optimize", client=client,
                                    )
                                    apply_result = {"applied_fields": list(applied["pushed"].keys())}
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
            result = _build_and_push_landing(store_id, blog_id, client, store, create_news_blog=True)
            if result is None:
                raise Exception("Blog not found, or the old landing article could not be removed")
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
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
    for key in ("hub_h1", "hub_meta_title", "hub_meta_description"):
        result[key] = result.get(key) or ""
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
            from app.services.image_generator import generate_article_hero_image, generate_blog_image_ai

            session = _find_article_media_session(article)
            hero = dict(((session or {}).get("media_plan") or {}).get("hero") or {})
            refs, handles = _load_hero_reference_images(
                store_id, hero,
                fallback_product_ids=(session or {}).get("target_product_ids"),
                body_html=article.get("body"), title=article["title"],
            )
            if refs or (session and _session_style(session) == STYLE_VISUAL):
                result = generate_article_hero_image(
                    article["title"], subject_prompt=hero.get("prompt"), alt_text=hero.get("alt") or article.get("image_alt"),
                    keywords=keywords, reference_images=refs,
                )
            else:
                result = generate_blog_image_ai(article["title"], keywords=keywords)
            if not result:
                raise Exception("Image generation returned no result")
            image_bytes, filename, alt_text, width, height = result
            if session and refs:
                # Remember which products the hero was built around so the
                # wizard's Visuals step and the next regenerate agree.
                plan = dict(session.get("media_plan") or {})
                plan["hero"] = {**hero, "kind": "product", "product_handles": handles}
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE article_wizard_sessions SET media_plan = %s, updated_at = NOW() WHERE id = %s",
                        (json.dumps(plan), session["id"]),
                    )

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
                    (json.dumps({"image_url": cdn_url, "image_alt": alt_text, "width": width, "height": height, "used_product_refs": len(refs)}), task_id),
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
            "style": _session_style(session),
            "session_id": session["id"],
            "topic": session["topic"],
            "keywords": session["target_keywords"],
            "research_data": session["research_data"],
            "store_id": session["store_id"],
            "locked_fields": session.get("locked_fields"),
        },
        store_id=session["store_id"],
    )
    result = apply_locked_fields(result, session.get("locked_fields"), OUTLINE_KEYS)
    media_plan = result.get("media_plan") if isinstance(result, dict) else None
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET outline_data = %s, media_plan = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(result), json.dumps(media_plan) if media_plan else None, session["id"]),
        )
    return result


def _wizard_step_generate(session):
    from app.ai_agent import run_task as ai_run_task

    result = ai_run_task(
        "article_generate",
        {
            "session_id": session["id"],
            "style": _session_style(session),
            "media_plan": session.get("media_plan"),
            "topic": session["topic"],
            "keywords": session["target_keywords"],
            "outline_data": session["outline_data"],
            "research_data": session["research_data"],
            "store_id": session["store_id"],
            "locked_fields": session.get("locked_fields"),
        },
        store_id=session["store_id"],
    )
    result = apply_locked_fields(result, session.get("locked_fields"), ARTICLE_KEYS)
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET generated_article = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(result), session["id"]),
        )
    return result


def _wizard_step_image(session, client, permanent=False):
    from app.services.image_generator import generate_article_hero_image, generate_blog_image_ai

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

    refs = []
    if _session_style(session) == STYLE_VISUAL:
        hero = ((session.get("media_plan") or {}).get("hero") or {})
        body_html = generated.get("body_html") or ""
        refs, _ = _load_hero_reference_images(
            session["store_id"], hero, fallback_product_ids=target_product_ids, body_html=body_html, title=title
        )
        result = generate_article_hero_image(
            title, subject_prompt=hero.get("prompt"), alt_text=hero.get("alt"), keywords=keywords, reference_images=refs
        )
    else:
        result = generate_blog_image_ai(title, keywords=keywords, product_titles=product_titles)
    if not result:
        raise Exception("Image generation returned no result")
    image_bytes, filename, alt_text, width, height = result

    if permanent:
        # Scheduled sessions publish later — staged-upload urls expire before
        # the delayed publish, so register the file for a permanent CDN url.
        from app.services.article_media import upload_image_to_cdn

        image_url = upload_image_to_cdn(client, image_bytes, filename, alt_text)
    else:
        mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
        staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
        image_url = client.upload_staged_file(staged, image_bytes, content_type=mime)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET featured_image_url = %s, featured_image_alt = %s, updated_at = NOW() WHERE id = %s",
            (image_url, alt_text, session["id"]),
        )
    return {"image_url": image_url, "alt_text": alt_text, "width": width, "height": height, "used_product_refs": len(refs)}


def _resolve_hero_product_handles(store_id, hero_spec, fallback_product_ids=None, body_html=None, title=None):
    """Which real products a hero should be built around, mirroring inline
    'product' images. Explicit planner handles win; a plan that predates hero
    kinds (no 'kind' key) or names none falls back to the session's hand-picked
    products, then to the body's linked products ranked by title relevance.
    A planner that explicitly chose a scene/concept hero gets no products."""
    from app.services.article_media import rank_linked_product_handles, rank_product_handles

    hero_spec = hero_spec or {}
    kind = hero_spec.get("kind")
    if kind and kind != "product":
        return []
    if not get_article_visuals(store_id)["product_composite"]:
        return []
    handles = [h for h in (hero_spec.get("product_handles") or []) if h]
    if not handles and fallback_product_ids:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT handle FROM synced_products WHERE store_id = %s AND id = ANY(%s) ORDER BY title",
                (store_id, list(fallback_product_ids)),
            )
            picked = [r["handle"] for r in cur.fetchall() if r.get("handle")]
        handles = rank_product_handles(picked, f"{title or ''} {hero_spec.get('prompt') or ''}")
    if not handles and body_html:
        handles = rank_linked_product_handles(body_html, f"{title or ''} {hero_spec.get('prompt') or ''}")
    return handles[:3]


def _load_hero_reference_images(store_id, hero_spec, fallback_product_ids=None, body_html=None, title=None):
    handles = _resolve_hero_product_handles(store_id, hero_spec, fallback_product_ids, body_html, title)
    return _load_product_reference_images(store_id, handles), handles


def _load_product_reference_images(store_id, handles, limit=3):
    """First image (bytes) of each named product, for product-composite scenes."""
    handles = [h for h in (handles or []) if h]
    if not handles or not store_id:
        return []
    import requests as http_req

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle, images FROM synced_products WHERE store_id = %s AND handle = ANY(%s)",
            (store_id, handles),
        )
        rows = cur.fetchall()
    out = []
    for row in rows:
        images = row.get("images") or []
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except Exception:
                images = []
        first = images[0] if images else None
        url = first.get("url") or first.get("src") if isinstance(first, dict) else first
        if not url:
            continue
        try:
            resp = http_req.get(url, timeout=15)
            resp.raise_for_status()
            out.append(resp.content)
        except Exception as e:
            logger.warning("Reference image fetch failed for %s: %s", row.get("handle"), e)
        if len(out) >= limit:
            break
    return out


def _generate_article_media(store_id, client, media_plan, article_slug, existing=None, only_index=None, allow_product_refs=None):
    """Generate + upload every inline image in a media plan (per-image best-effort).

    Returns the inline_images list: one entry per planned spec with
    status 'ready' (cdn_url set) or 'failed'. Entries already 'ready' in
    `existing` are kept unless `only_index` targets them. Always uploads to a
    permanent CDN url — the body embeds these as <img src>.
    """
    from app.services.article_media import upload_image_to_cdn
    from app.services.image_generator import generate_article_inline_image

    visuals = get_article_visuals(store_id)
    if allow_product_refs is None:
        allow_product_refs = visuals["product_composite"]
    specs = ((media_plan or {}).get("inline") or [])[: visuals["inline_image_count"] if only_index is None else None]
    existing_by_token = {e.get("token"): e for e in (existing or []) if isinstance(e, dict)}
    out = []
    budget = max(0, visuals["max_images_per_article"] - 1)  # hero counts as one
    for i, spec in enumerate(specs, start=1):
        prior = existing_by_token.get(spec.get("token"))
        if only_index is not None and i != only_index:
            if prior:
                out.append(prior)
            continue
        if only_index is None and prior and prior.get("status") == "ready" and prior.get("cdn_url"):
            out.append(prior)
            continue
        entry = {
            "token": spec.get("token"),
            "section": spec.get("section"),
            "kind": spec.get("kind"),
            "prompt": spec.get("prompt"),
            "alt": spec.get("alt"),
            "caption": spec.get("caption"),
            "product_handles": spec.get("product_handles") or [],
            "cdn_url": "",
            "status": "pending",
        }
        if len([e for e in out if e.get("status") == "ready"]) >= budget:
            entry["status"] = "skipped"
            entry["error"] = "image budget reached"
            out.append(entry)
            continue
        try:
            refs = []
            if allow_product_refs and (spec.get("kind") == "product") and spec.get("product_handles"):
                refs = _load_product_reference_images(store_id, spec.get("product_handles"))
            result = generate_article_inline_image(spec, article_slug, i, reference_images=refs)
            if not result:
                raise Exception("no image returned")
            image_bytes, filename, alt_text, width, height = result
            cdn_url = upload_image_to_cdn(client, image_bytes, filename, alt_text)
            entry.update({
                "cdn_url": cdn_url, "alt": alt_text, "width": width, "height": height,
                "status": "ready", "used_product_refs": len(refs),
            })
        except Exception as e:
            logger.warning("Inline image failed for token %s: %s", spec.get("token"), e)
            entry["status"] = "failed"
            entry["error"] = str(e)[:300]
        out.append(entry)
    return out


def _wizard_step_media(session, client, only_index=None):
    """Generate the session's inline images from its media plan and persist them."""
    generated = session.get("generated_article") or {}
    slug = (generated.get("handle") or generated.get("title") or session.get("topic") or "article")
    inline_images = _generate_article_media(
        session["store_id"], client, session.get("media_plan") or {}, slug,
        existing=session.get("inline_images") or [], only_index=only_index,
    )
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET inline_images = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(inline_images), session["id"]),
        )
    return inline_images


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

    is_visual = _session_style(session) == STYLE_VISUAL
    body_html = generated.get("body_html", "")
    if is_visual:
        from app.services.article_media import compose_article_body

        visuals = get_article_visuals(session["store_id"])
        body_html = compose_article_body(body_html, session.get("inline_images") or [], visuals)

    fields = {
        "title": generated.get("title", session["topic"]),
        "body": body_html,
        "summary": generated.get("summary", ""),
        "tags": _scrub_query_echo_tags(
            session["store_id"], generated.get("tags", []), session.get("target_keywords")),
        "meta_title": generated.get("meta_title", ""),
        "meta_description": generated.get("meta_description", ""),
        "is_published": is_published,
    }
    guard_shopify_fields(fields)

    handle = (generated.get("handle") or "").strip()
    if handle:
        fields["handle"] = handle
    # An imported brief's text wins over every clamp and edit above.
    locked = session.get("locked_fields") or {}
    fields = apply_locked_fields(fields, locked, ARTICLE_KEYS)

    if session.get("featured_image_url"):
        fields["image_url"] = session["featured_image_url"]
        fields["image_alt"] = session.get("featured_image_alt") or generated.get("title", session["topic"])

    if is_visual:
        # Classic sessions leave the suffix unset and render on the theme's
        # default article template exactly as before.
        fields["template_suffix"] = BLOG_TEMPLATE_SUFFIX

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
            # A slug from an imported brief is locked: fail instead.
            if "Handle has already been taken" in str(create_err):
                if locked.get("handle"):
                    raise ValueError(
                        f"URL slug '{locked['handle']}' is already taken on Shopify — "
                        "change the slug in the file and import it again"
                    ) from create_err
                fields.pop("handle", None)
                result = client.create_article(blog["shopify_id"], fields)
            else:
                raise

    if is_visual:
        from app.services.article_media import faq_jsonld_string

        faq_json = faq_jsonld_string(fields["body"])
        try:
            if faq_json:
                client.set_article_metafields(result["shopify_id"], [("global", "faq_jsonld", faq_json, "json")])
        except Exception as e:
            logger.warning("FAQ metafield write failed for article %s: %s", result.get("shopify_id"), e)

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
                 published_at, template_suffix, shopify_created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'blog', NOW(),
                    CASE WHEN %s THEN NOW() END, %s, NOW())
            ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                title = EXCLUDED.title, handle = EXCLUDED.handle,
                body = EXCLUDED.body, article_type = 'blog', synced_at = NOW(),
                published_at = COALESCE(synced_articles.published_at, EXCLUDED.published_at),
                template_suffix = EXCLUDED.template_suffix
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
                fields.get("template_suffix", ""),
            ),
        )
        article_row = cur.fetchone()

        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'published', current_step = 7, shopify_article_id = %s, updated_at = NOW() WHERE id = %s",
            (result["shopify_id"], session_id),
        )
        cur.execute(
            "UPDATE topic_suggestions SET status = 'used', updated_at = NOW() "
            "WHERE wizard_session_id = %s AND status = 'scheduled'",
            (session_id,),
        )

    if is_published:
        from app.services.article_keywords import primary_of, record_article_keyword

        record_article_keyword(article_row["id"], primary_of(session.get("target_keywords")))

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


def _scrub_query_echo_tags(store_id, tags, target_keywords=None):
    """Drop tags that would create a /tagged/ archive competing with this
    article's own target queries or with a money page's primary keyword —
    Shopify indexes tag archives, and the wizard once shipped its raw target
    keywords as tags (articles manufacturing their own cannibals). Fails open."""
    if not tags:
        return tags
    try:
        from app.services.keyword_identity import variant_key
        from app.services.keyword_resolver import get_owned_primary_keywords

        kws = target_keywords
        if isinstance(kws, str):
            try:
                kws = json.loads(kws)
            except (ValueError, TypeError):
                kws = [kws]
        blocked = {variant_key(str(k)) for k in (kws or []) if k}
        try:
            # Money pages only: an assignment can name an article or page, and
            # an item-type-blind map would strip the article's own keyword.
            blocked |= set(get_owned_primary_keywords(
                store_id, item_types=("product", "collection")))
        except Exception:
            pass
        kept = [t for t in tags if variant_key(str(t)) not in blocked]
        if len(kept) < len(tags):
            logger.info("Scrubbed %d query-echo tag(s) at publish (store %s)",
                        len(tags) - len(kept), store_id)
        return kept
    except Exception:
        return tags


def _money_page_keyword_warning(store_id, keywords):
    """Advisory money-page cannibalization check for a blog primary keyword.
    Shared by wizard start (approval time) and the publish scheduler (publish
    time — ownership may have changed while the session sat scheduled)."""
    try:
        from app.services.keyword_resolver import get_owned_primary_keywords, variant_key

        primary = variant_key(keywords[0]) if keywords else ""
        if primary:
            # Money pages only — otherwise an article assigned this keyword
            # (Keyword Strategy → Assign) makes the post warn about itself.
            owner = get_owned_primary_keywords(
                store_id, item_types=("product", "collection")).get(primary)
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

    style = _resolve_article_style(store_id, data.get("style"))

    # Money-page keyword guard: a blog post whose primary keyword is a
    # product/collection's resolved primary would cannibalize it.
    keyword_warning = _money_page_keyword_warning(store_id, keywords)

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO article_wizard_sessions
                (store_id, topic, target_keywords, blog_id, target_product_ids, target_collection_ids, status, current_step, style)
            VALUES (%s, %s, %s, %s, %s, %s, 'draft', 1, %s) RETURNING id""",
            (store_id, topic, json.dumps(keywords), blog_id, json.dumps(product_ids), json.dumps(collection_ids), style),
        )
        session_id = cur.fetchone()["id"]

    resp = {"session_id": session_id, "style": style}
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


def _filter_seed_keywords(store_id, seed):
    """Drop seeded keywords a blog post is forbidden to target anyway.

    Rule 8 of the topic_discovery prompt makes money-page keywords
    authoritative and FORBIDDEN as article primaries, so seeding one just
    burns a slot: the model silently substitutes something else and the user
    is left wondering why their keyword vanished. Filtering here means the
    seed list only contains keywords the model is actually allowed to use,
    and the excluded ones come back with their owner so the UI can say why.

    Returns (kept, excluded) where excluded entries carry `owner`.
    """
    from app.ai_agent import _reserved_money_page_keywords
    from app.services.keyword_identity import variant_key

    keywords = (seed or {}).get("keywords") or []
    if not keywords:
        return [], []

    try:
        reserved = {}
        for r in _reserved_money_page_keywords(store_id, cap=1000):
            reserved[variant_key(r["kw"])] = r
    except Exception:
        logger.warning("Could not load reserved keywords for seed filtering", exc_info=True)
        return list(keywords), []

    kept, excluded = [], []
    for k in keywords:
        text = k.get("keyword") if isinstance(k, dict) else k
        if not text:
            continue
        owner = reserved.get(variant_key(str(text)))
        if owner:
            excluded.append({"keyword": text, "owner": owner["kw"], "item_type": owner["item_type"]})
        else:
            kept.append(k)
    return kept, excluded


@blog_bp.route("/api/blog/topics/research", methods=["POST"])
def topic_research_start():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    # Optional focus seed from the Keyword Strategy coverage gaps view:
    # {family, intent, keywords: [{keyword, volume}], count}
    seed = data.get("seed") or None
    excluded_seeds = []
    if seed and seed.get("keywords"):
        kept, excluded_seeds = _filter_seed_keywords(store_id, seed)
        if not kept:
            # Every seeded keyword belongs to a money page. Running unseeded
            # would produce unrelated suggestions and look like the seed was
            # ignored, so say what happened instead.
            owners = ", ".join(f'"{e["keyword"]}" ({e["item_type"]})' for e in excluded_seeds[:5])
            return jsonify({
                "error": "Every keyword here is already the primary keyword of a product or "
                         "collection page, so a blog post can't target them without competing "
                         f"with your own money pages: {owners}. Write supporting content that "
                         "links to those pages, or pick informational keywords instead.",
                "excluded_seeds": excluded_seeds,
            }), 400
        seed = dict(seed, keywords=kept)

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
    return jsonify({"run_id": run_id, "task_id": task_id, "excluded_seeds": excluded_seeds,
                    "seeded_keywords": [k.get("keyword") if isinstance(k, dict) else k
                                        for k in ((seed or {}).get("keywords") or [])]})


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


@blog_bp.route("/api/blog/topics/<int:run_id>", methods=["GET"])
def topic_research_run(run_id):
    """Status of one research run, so a caller that started it can poll and
    report the outcome instead of firing and forgetting."""
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        # Same orphan sweep as the list view: daemon threads don't survive a
        # worker restart, so a run can otherwise poll as 'running' forever.
        cur.execute(
            """UPDATE topic_research_runs r SET status = 'failed',
                error = COALESCE(r.error, 'Interrupted — worker restarted'), completed_at = NOW()
            WHERE r.id = %s AND r.store_id = %s AND r.status IN ('pending', 'running')
              AND (r.created_at < NOW() - INTERVAL '30 minutes'
                   OR EXISTS (SELECT 1 FROM ai_tasks t WHERE t.id = r.task_id AND t.status = 'failed'))""",
            (run_id, store_id),
        )
        cur.execute(
            """SELECT r.id, r.task_id, r.status, r.market_summary, r.rejected_topics, r.error,
                      r.created_at, r.completed_at, t.input AS task_input
               FROM topic_research_runs r
               LEFT JOIN ai_tasks t ON t.id = r.task_id
               WHERE r.id = %s AND r.store_id = %s""",
            (run_id, store_id),
        )
        run = cur.fetchone()
        if not run:
            return jsonify({"error": "Run not found"}), 404
        run = dict(run)
        # The seed the run was started with, so a caller can report which of
        # its keywords the research actually used and which it ruled out.
        task_input = run.pop("task_input", None) or {}
        if isinstance(task_input, str):
            try:
                task_input = json.loads(task_input)
            except ValueError:
                task_input = {}
        seed = task_input.get("seed") or {}
        run["seed_keywords"] = [k.get("keyword") if isinstance(k, dict) else k
                                for k in (seed.get("keywords") or [])]
        run["seed_family"] = seed.get("family")
        cur.execute(
            "SELECT * FROM topic_suggestions WHERE run_id = %s ORDER BY position",
            (run_id,),
        )
        suggestions = [dict(s) for s in cur.fetchall()]

    run["created_at"] = run["created_at"].isoformat() if run.get("created_at") else None
    run["completed_at"] = run["completed_at"].isoformat() if run.get("completed_at") else None
    for s in suggestions:
        s["created_at"] = s["created_at"].isoformat() if s.get("created_at") else None
        s["updated_at"] = s["updated_at"].isoformat() if s.get("updated_at") else None
    run["suggestions"] = suggestions
    return jsonify(run)


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
    return publish_gate.get_cadence(store_id)


def _next_open_slot(store_id):
    """Next publish slot on the store's shared blog + news calendar. Naive UTC."""
    return publish_gate.next_open_slot(store_id)


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
    if session.get("locked_fields") and not session.get("outline_data"):
        _adopt_research_targets(session)
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
        session = _fetch_wizard_session(session_id)
    if (
        _session_style(session) == STYLE_VISUAL
        and (session.get("media_plan") or {}).get("inline")
        and not (session.get("inline_images") or [])
    ):
        _set_status("auto_media")
        try:
            _wizard_step_media(session, client)
        except Exception as e:
            logger.warning("Blog auto pipeline: inline media failed, continuing: %s", e)
    _set_status("scheduled")


def _adopt_research_targets(session):
    """Imported briefs accept the wizard's defaults: the products and
    collections research found become the link targets, exactly as Step 2
    pre-selects them in the manual wizard."""
    if session.get("target_product_ids") or session.get("target_collection_ids"):
        return
    research = session.get("research_data") or {}

    def _ids(items):
        out = []
        for item in items or []:
            try:
                out.append(int(item["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    product_ids = _ids(research.get("related_products"))
    collection_ids = _ids(research.get("related_collections"))
    if not (product_ids or collection_ids):
        return
    with db.get_cursor() as cur:
        # The ids come from the model — keep only rows this store really has.
        cur.execute(
            "SELECT id FROM synced_products WHERE store_id = %s AND id = ANY(%s)",
            (session["store_id"], product_ids),
        )
        known = {r["id"] for r in cur.fetchall()}
        product_ids = [i for i in product_ids if i in known]
        cur.execute(
            "SELECT id FROM synced_collections WHERE store_id = %s AND id = ANY(%s)",
            (session["store_id"], collection_ids),
        )
        known = {r["id"] for r in cur.fetchall()}
        collection_ids = [i for i in collection_ids if i in known]
        cur.execute(
            """UPDATE article_wizard_sessions
               SET target_product_ids = %s, target_collection_ids = %s, updated_at = NOW()
               WHERE id = %s""",
            (json.dumps(product_ids), json.dumps(collection_ids), session["id"]),
        )


def _run_tracked_blog_pipeline(session_id, store, client):
    """Run the auto pipeline under one ai_tasks row (so the Tasks panel shows
    the background writing); a failure marks the session failed, never raises."""
    session = _fetch_wizard_session(session_id)
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, 'scheduled_blog_post', 'running', %s) RETURNING id""",
            (session["store_id"], json.dumps({"session_id": session_id, "topic": session["topic"]})),
        )
        task_id = cur.fetchone()["id"]
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


def _spawn_blog_auto_pipeline(session_id, store, client):
    threading.Thread(
        target=_run_tracked_blog_pipeline, args=(session_id, store, client), daemon=True
    ).start()


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

    style = _resolve_article_style(store_id, data.get("style"))
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO article_wizard_sessions
                (store_id, topic, target_keywords, target_product_ids, target_collection_ids,
                 status, current_step, scheduled_for, style)
            VALUES (%s, %s, %s, %s, %s, 'auto_queued', 1, %s, %s) RETURNING id""",
            (store_id, topic, json.dumps(keywords), json.dumps(product_ids),
             json.dumps(collection_ids), scheduled_for, style),
        )
        session_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE topic_suggestions SET status = 'scheduled', wizard_session_id = %s, updated_at = NOW() WHERE id = %s",
            (session_id, suggestion_id),
        )

    _spawn_blog_auto_pipeline(session_id, store, client)
    return jsonify({"session_id": session_id, "scheduled_for": _iso_utc(scheduled_for)})


# ── Auto mode: import briefs from a file ────────────────────────


def _annotate_brief_conflicts(store_id, briefs):
    """Add store-dependent errors/warnings to checked briefs in place: a URL
    slug already used (live or queued) or repeated in the batch is an error; a
    title that reads like an existing or queued post, or a primary keyword a
    money page owns, is a warning (`keyword_conflict` = that keyword)."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT a.title, a.handle FROM synced_articles a
               JOIN synced_blogs b ON b.id = a.blog_id
               WHERE a.store_id = %s AND b.handle = 'news'""",
            (store_id,),
        )
        live = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT locked_fields->>'handle' AS handle,
                      COALESCE(locked_fields->>'title', generated_article->>'title', topic) AS title
               FROM article_wizard_sessions
               WHERE store_id = %s AND scheduled_for IS NOT NULL AND status != 'published'""",
            (store_id,),
        )
        queued = [dict(r) for r in cur.fetchall()]
    live_handles = {r["handle"]: r["title"] for r in live if r.get("handle")}
    queued_handles = {r["handle"]: r["title"] for r in queued if r.get("handle")}
    seen_handles, seen_titles = set(), []
    for b in briefs:
        handle, title = b.get("handle"), b.get("title")
        if handle:
            if handle in live_handles:
                b["errors"].append(f"URL slug '{handle}' is already used by “{live_handles[handle]}”")
            elif handle in queued_handles:
                b["errors"].append(f"URL slug '{handle}' is already queued for “{queued_handles[handle]}”")
            elif handle in seen_handles:
                b["errors"].append(f"URL slug '{handle}' appears twice in this import")
            seen_handles.add(handle)
        if title:
            match = (
                next((("published", r["title"]) for r in live if titles_match(title, r["title"])), None)
                or next((("queued", r["title"]) for r in queued if r["title"] and titles_match(title, r["title"])), None)
                or next((("in this import", t) for t in seen_titles if titles_match(title, t)), None)
            )
            if match:
                b["warnings"].append(f"Reads like a post already {match[0]}: “{match[1]}”")
            seen_titles.append(title)
        conflict = _money_page_keyword_warning(store_id, b.get("keywords") or [])
        b["keyword_conflict"] = b["keywords"][0] if conflict else None
        if conflict:
            b["warnings"].append(conflict)
    return briefs


def _brief_from_payload(item):
    keywords = item.get("keywords") or []
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    return {
        "topic": str(item.get("topic") or ""),
        "title": str(item.get("title") or "").strip(),
        "meta_title": str(item.get("meta_title") or "").strip(),
        "meta_description": str(item.get("meta_description") or "").strip(),
        "handle": str(item.get("handle") or "").strip(),
        "keywords": dedupe_keywords([str(k).strip() for k in keywords if str(k).strip()]),
    }


@blog_bp.route("/api/blog/import/preview", methods=["POST"])
def import_briefs_preview():
    """Parse brief files and report what each row would import as."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    briefs = []
    for f in data.get("files") or []:
        for brief in parse_blog_briefs(f.get("text") or ""):
            checked = check_brief(brief)
            checked["source_file"] = f.get("name") or ""
            briefs.append(checked)
    if not briefs:
        return jsonify({"error": "No briefs found — each brief needs at least an “H1:” line"}), 400
    _annotate_brief_conflicts(store_id, briefs)
    slots = publish_gate.next_open_slots(store_id, len(briefs))
    return jsonify({
        "briefs": briefs,
        "default_slots": [_iso_utc(s) for s in slots],
        "default_style": _resolve_article_style(store_id),
    })


@blog_bp.route("/api/blog/import/schedule", methods=["POST"])
def import_briefs_schedule():
    """Queue imported briefs: one locked session per row, run through the
    auto pipeline one after another, publishing at each row's date."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    items = data.get("items") or []
    if not store_id or not items:
        return jsonify({"error": "store_id and items are required"}), 400
    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    # Validate every row before creating anything (all-or-nothing).
    rows = []
    now = datetime.utcnow()
    for i, item in enumerate(items, 1):
        brief = check_brief(_brief_from_payload(item))
        try:
            scheduled_for = _parse_scheduled_at(item.get("scheduled_at"))
        except (ValueError, TypeError):
            return jsonify({"error": f"Row {i}: a valid publish date is required"}), 400
        if scheduled_for < now:
            return jsonify({"error": f"Row {i}: the publish time is in the past — pick a future date/time"}), 400
        rows.append((brief, scheduled_for, item))
    _annotate_brief_conflicts(store_id, [b for b, _, _ in rows])
    for i, (brief, _, _) in enumerate(rows, 1):
        if brief["errors"]:
            return jsonify({"error": f"Row {i} ({brief['title'] or 'no H1'}): {brief['errors'][0]}"}), 400

    style = _resolve_article_style(store_id, data.get("style"))
    approve = bool(data.get("approve", True))
    session_ids = []
    with db.get_cursor() as cur:
        for brief, scheduled_for, item in rows:
            locked = locked_fields_of(brief)
            locked["source_file"] = str(item.get("source_file") or "")
            if item.get("keyword_conflict_ack") and brief.get("keyword_conflict"):
                locked["keyword_conflict_ack"] = brief["keyword_conflict"]
            cur.execute(
                """INSERT INTO article_wizard_sessions
                    (store_id, topic, target_keywords, target_product_ids, target_collection_ids,
                     status, current_step, scheduled_for, style, locked_fields, reviewed_at)
                VALUES (%s, %s, %s, '[]', '[]', 'auto_queued', 1, %s, %s, %s,
                        CASE WHEN %s THEN NOW() END)
                RETURNING id""",
                (store_id, brief["title"][:500], json.dumps(brief["keywords"]), scheduled_for,
                 style, json.dumps(locked), approve),
            )
            session_ids.append(cur.fetchone()["id"])

    def _run_in_order():
        # One at a time — a batch of posts would otherwise hit the research
        # and image APIs all at once.
        for sid in session_ids:
            _run_tracked_blog_pipeline(sid, store, client)

    threading.Thread(target=_run_in_order, daemon=True).start()
    return jsonify({"sessions": session_ids})


@blog_bp.route("/api/blog/scheduled", methods=["GET"])
def list_scheduled_posts():
    """List scheduled blog sessions (processing, ready, or failed) for a store."""
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, scheduled_for, reviewed_at, error, created_at, updated_at, topic, style,
                      COALESCE(locked_fields->>'title', generated_article->>'title', topic) AS title,
                      (locked_fields IS NOT NULL) AS from_file,
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
    # double-publish the same session. Publishing a ready post by hand is the
    # review; resuming an unfinished one is not.
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE article_wizard_sessions
               SET status = %s, error = NULL, updated_at = NOW(),
                   reviewed_at = CASE WHEN %s THEN NOW() ELSE reviewed_at END
               WHERE id = %s AND status IN ('scheduled', 'failed') RETURNING store_id""",
            ("auto_publishing" if ready else "auto_queued", ready, session_id),
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


@blog_bp.route("/api/blog/scheduled/<int:session_id>/approve", methods=["POST"])
def approve_scheduled_post(session_id):
    """Mark a prepared post as reviewed so the scheduler may publish it."""
    data = request.get_json(silent=True) or {}
    if data.get("approved") is False:
        if not publish_gate.unapprove_session("blog", session_id):
            return jsonify({"error": "Session is not waiting to publish"}), 409
        return jsonify({"success": True, "approved": False})
    slot = publish_gate.approve_session("blog", session_id)
    if not slot:
        return jsonify({"error": "Only a prepared, scheduled post can be approved"}), 409
    return jsonify({"success": True, "approved": True, "scheduled_for": _iso_utc(slot)})


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
        interval_days = max(1, int(data.get("interval_days") or publish_gate.DEFAULT_GAP_DAYS))
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


def _live_body_template(session, previous_images, only_index):
    """Body to recompose a published article from. Prefer the live synced body
    with its figures swapped back to tokens (keeps edits made after publish);
    use the session's template when the target image has no figure live yet."""
    from app.services.article_media import tokenize_figures

    template = (session.get("generated_article") or {}).get("body_html", "")
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT body FROM synced_articles WHERE store_id = %s AND shopify_id = %s",
            (session["store_id"], session["shopify_article_id"]),
        )
        row = cur.fetchone()
    live = (row or {}).get("body") or ""
    if not live:
        return template
    tokenized = tokenize_figures(live, previous_images)
    targets = [e for i, e in enumerate(previous_images, start=1) if only_index is None or i == only_index]
    if all(e.get("token") and e["token"] in tokenized for e in targets if isinstance(e, dict)) and targets:
        return tokenized
    return template


def _run_session_media_task(session, client, task_type, only_index=None, resume_status="generated", include_hero=False):
    """Background inline-image generation for a wizard session with an ai_tasks row.
    With `include_hero`, a missing hero is generated first (best-effort: a hero
    failure is recorded on the task but never blocks the inline images)."""
    session_id = session["id"]
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET status = 'generating_media', updated_at = NOW() WHERE id = %s",
            (session_id,),
        )
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
            VALUES (%s, %s, 'pending', %s) RETURNING id""",
            (session["store_id"], task_type, json.dumps({"session_id": session_id, "index": only_index})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            fresh = _fetch_wizard_session(session_id)
            hero_error = None
            if include_hero and not fresh.get("featured_image_url"):
                try:
                    _wizard_step_image(fresh, client, permanent=bool(fresh.get("scheduled_for")))
                except Exception as e:
                    logger.exception("Hero generation failed during generate-all: %s", e)
                    hero_error = str(e)
            previous = fresh.get("inline_images") or []
            images = _wizard_step_media(fresh, client, only_index=only_index) if (fresh.get("media_plan") or {}).get("inline") else previous
            # Already-published article (restyle / regenerate after publish): push the recomposed body.
            if fresh.get("shopify_article_id") and fresh.get("generated_article"):
                from app.services.article_media import compose_article_body

                base = _live_body_template(fresh, previous, only_index)
                body = compose_article_body(base, images, get_article_visuals(fresh["store_id"]))
                client.update_article(fresh["shopify_article_id"], {"body": body})
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE synced_articles SET body = %s WHERE store_id = %s AND shopify_id = %s",
                        (body, fresh["store_id"], fresh["shopify_article_id"]),
                    )
            ready = len([i for i in images if i.get("status") == "ready"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = %s, updated_at = NOW() WHERE id = %s",
                    (resume_status, session_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"ready": ready, "total": len(images), "inline_images": images, "hero_error": hero_error}), task_id),
                )
        except Exception as e:
            logger.exception("Article media generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE article_wizard_sessions SET status = %s, updated_at = NOW() WHERE id = %s",
                    (resume_status, session_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return task_id


@blog_bp.route("/api/blog/wizard/<int:session_id>/generate-media", methods=["POST"])
def wizard_generate_media(session_id):
    session = _fetch_wizard_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if _session_style(session) != STYLE_VISUAL:
        return jsonify({"error": "Classic articles have no inline images"}), 400
    needs_hero = not session.get("featured_image_url")
    if not (session.get("media_plan") or {}).get("inline") and not needs_hero:
        return jsonify({"error": "No inline images planned for this article"}), 400
    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404
    resume = "published" if session.get("status") == "published" else "generated"
    task_id = _run_session_media_task(session, client, "article_media", resume_status=resume, include_hero=needs_hero)
    return jsonify({"task_id": task_id, "includes_hero": needs_hero})


@blog_bp.route("/api/blog/wizard/<int:session_id>/media/<int:index>/regenerate", methods=["POST"])
def wizard_regenerate_media(session_id, index):
    data = request.get_json(silent=True) or {}
    session = _fetch_wizard_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    plan = session.get("media_plan") or {}
    inline = plan.get("inline") or []
    if index < 1 or index > len(inline):
        return jsonify({"error": "No such inline image"}), 404
    # Optional prompt/alt/caption edits before regenerating.
    spec = inline[index - 1]
    changed = False
    for key in ("prompt", "alt", "caption", "kind"):
        if data.get(key) is not None:
            spec[key] = str(data[key]).strip()
            changed = True
    if changed:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE article_wizard_sessions SET media_plan = %s, updated_at = NOW() WHERE id = %s",
                (json.dumps(plan), session_id),
            )
        session["media_plan"] = plan
    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404
    resume = "published" if session.get("status") == "published" else "generated"
    task_id = _run_session_media_task(session, client, "article_media_regen", only_index=index, resume_status=resume)
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/wizard/<int:session_id>/media-plan", methods=["PUT"])
def wizard_update_media_plan(session_id):
    data = request.get_json(silent=True) or {}
    session = _fetch_wizard_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    from app.services.article_media import normalize_media_plan

    visuals = get_article_visuals(session["store_id"])
    plan = normalize_media_plan(data.get("media_plan"), visuals["inline_image_count"])
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE article_wizard_sessions SET media_plan = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(plan), session_id),
        )
    return jsonify({"success": True, "media_plan": plan})


@blog_bp.route("/api/blog/wizard/<int:session_id>/compose-preview", methods=["POST"])
def wizard_compose_preview(session_id):
    """Render the body exactly as publish would (figures, TOC, no stray tokens)."""
    data = request.get_json(silent=True) or {}
    session = _fetch_wizard_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    from app.services.ai_output_guard import sanitize_html
    from app.services.article_media import compose_article_body, count_figures

    body = data.get("body_html")
    if body is None:
        body = (session.get("generated_article") or {}).get("body_html", "")
    if _session_style(session) == STYLE_VISUAL:
        body = compose_article_body(body, session.get("inline_images") or [], get_article_visuals(session["store_id"]))
    body = sanitize_html(body)
    return jsonify({"body_html": body, "figures": count_figures(body)})


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
            if isinstance(result, dict) and not result.get("error"):
                result["conflicts"] = hub_conflicts_for(store_id, {
                    "hub_h1": result.get("suggested_hub_h1"),
                    "hub_meta_title": result.get("suggested_meta_title"),
                })
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


_HUB_FIELDS = ("intro_text", "hub_h1", "hub_meta_title", "hub_meta_description")


def _hub_owned_keywords(store_id):
    """variant_key -> owner for every page's primary keyword: resolved and
    assigned primaries plus each published article's own primary."""
    from app.services.keyword_identity import variant_key
    from app.services.keyword_resolver import get_owned_primary_keywords

    owned = get_owned_primary_keywords(store_id)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, primary_keyword FROM synced_articles
               WHERE store_id = %s AND is_published = TRUE AND COALESCE(primary_keyword, '') <> ''
                 AND COALESCE(tags, '') NOT ILIKE '%%landing-page%%'""",
            (store_id,),
        )
        for r in cur.fetchall():
            kw = r["primary_keyword"].split(",")[0].strip()
            key = variant_key(kw)
            if key and key not in owned:
                owned[key] = {"keyword": kw, "item_type": "article", "item_id": r["id"]}
    return owned


def hub_conflicts_for(store_id, fields):
    """Owned keywords the hub H1 or meta title would compete for."""
    from app.services.blog_landing_generator import hub_keyword_conflicts

    texts = [fields[k] for k in ("hub_h1", "hub_meta_title") if fields.get(k)]
    if not texts:
        return []
    owned = _hub_owned_keywords(store_id)
    seen, conflicts = set(), []
    for text in texts:
        for c in hub_keyword_conflicts(text, owned):
            if c["keyword"] not in seen:
                seen.add(c["keyword"])
                conflicts.append(c)
    return conflicts


@blog_bp.route("/api/blog/hub-settings", methods=["POST"])
@blog_bp.route("/api/blog/save-intro", methods=["POST"])
def save_hub_settings():
    """Save any of the hub's intro, H1, meta title and meta description, push
    the H1/meta to the blog's metafields, and rebuild the hub. An H1 or meta
    title that names another page's keyword is refused unless force is set."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    blog_id = data.get("blog_id")
    if not store_id or not blog_id:
        return jsonify({"error": "store_id and blog_id are required"}), 400
    fields = {k: re.sub(r"[ \t]+", " ", str(data.get(k) or "")).strip() for k in _HUB_FIELDS if k in data}
    if not fields:
        return jsonify({"error": "Nothing to save"}), 400

    if not data.get("force"):
        conflicts = hub_conflicts_for(store_id, fields)
        if conflicts:
            named = "; ".join(f'"{c["phrase"]}" is the {c["item_type"]} keyword "{c["keyword"]}"' for c in conflicts)
            return jsonify({
                "error": f"The hub would compete with pages that already target these keywords: {named}.",
                "code": "hub_keyword_conflict",
                "conflicts": conflicts,
                "can_force": True,
                "confirm_title": "This name competes with another page",
            }), 409

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    with db.get_cursor() as cur:
        cur.execute("SELECT shopify_id FROM synced_blogs WHERE id = %s AND store_id = %s", (blog_id, store_id))
        blog = cur.fetchone()
    if not blog:
        return jsonify({"error": "Blog not found"}), 404

    if "hub_h1" in fields:
        if fields["hub_h1"]:
            client.set_blog_metafields(blog["shopify_id"], [("global", "hub_h1", fields["hub_h1"], "single_line_text_field")])
        else:
            client.delete_metafields([(blog["shopify_id"], "global", "hub_h1")])
    if fields.get("hub_meta_title") or fields.get("hub_meta_description"):
        client.set_blog_seo(blog["shopify_id"], fields.get("hub_meta_title") or None,
                            fields.get("hub_meta_description") or None)

    cols = list(fields)
    with db.get_cursor() as cur:
        cur.execute(
            f"""INSERT INTO blog_landing_pages (store_id, blog_id, {", ".join(cols)})
                VALUES (%s, %s, {", ".join(["%s"] * len(cols))})
                ON CONFLICT (store_id, blog_id) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)}""",
            (store_id, blog_id, *[fields[c] or None for c in cols]),
        )
        cur.execute("SELECT shopify_page_id FROM blog_landing_pages WHERE store_id = %s AND blog_id = %s",
                    (store_id, blog_id))
        landing = cur.fetchone()

    # Only rebuild a hub that exists; the first one is made with Landing Page.
    if landing and landing.get("shopify_page_id"):
        _regen_landing_async(store_id, blog_id, client, store)
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


# ── Visual settings + blog article theme template ─────────────────

@blog_bp.route("/api/blog/visual-settings", methods=["GET"])
def get_visual_settings():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    from app.services.article_visuals import get_store_overrides

    effective = get_article_visuals(store_id)
    return jsonify({
        "effective": visuals_with_warnings(effective),
        "overrides": get_store_overrides(store_id),
        "styles": list(STYLES),
    })


@blog_bp.route("/api/blog/visual-settings", methods=["POST"])
def post_visual_settings():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    effective = save_store_visuals(store_id, data)
    return jsonify({"success": True, "effective": visuals_with_warnings(effective)})


@blog_bp.route("/api/blog/article-css", methods=["GET"])
def article_css():
    """The article stylesheet for the store's brand color — used by the wizard
    preview so the review pane matches the storefront."""
    store_id = request.args.get("store_id")
    visuals = get_article_visuals(store_id) if store_id else get_article_visuals(None)
    from flask import Response

    return Response(get_article_css(visuals["brand_color"]), mimetype="text/css")


def _blog_template_state(client):
    """Return (theme_id, installed, outdated) for the blog article template."""
    theme_id = client.get_main_theme_id()
    if not theme_id:
        return None, False, False
    template = client.get_theme_asset(theme_id, BLOG_TEMPLATE_FILE)
    installed = template is not None and BLOG_TEMPLATE_MARKER_PREFIX in (template or "")
    outdated = installed and BLOG_TEMPLATE_MARKER not in (template or "")
    return theme_id, installed, outdated


@blog_bp.route("/api/blog/template-status", methods=["GET"])
def blog_template_status():
    store_id = request.args.get("store_id")
    store, client = _get_store_client(store_id) if store_id else (None, None)
    if not client:
        return jsonify({"installed": False})
    try:
        theme_id, installed, outdated = _blog_template_state(client)
    except Exception as e:
        logger.warning("Blog template status failed: %s", e)
        return jsonify({"installed": False})
    if not theme_id:
        return jsonify({"installed": False, "error": "No main theme found"})

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE COALESCE(template_suffix, '') <> %s) AS pending,
                 COUNT(*) FILTER (WHERE COALESCE(template_suffix, '') = %s) AS applied
               FROM synced_articles
               WHERE store_id = %s AND article_type = 'blog'
                 AND COALESCE(tags, '') NOT ILIKE '%%landing-page%%'""",
            (BLOG_TEMPLATE_SUFFIX, BLOG_TEMPLATE_SUFFIX, store_id),
        )
        counts = cur.fetchone() or {}
    return jsonify({
        "installed": installed,
        "outdated": outdated,
        "version": BLOG_TEMPLATE_VERSION,
        "pending_retrofit": int(counts.get("pending") or 0),
        "applied": int(counts.get("applied") or 0),
    })


@blog_bp.route("/api/blog/push-template", methods=["POST"])
def push_blog_template():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    store, client = _get_store_client(store_id) if store_id else (None, None)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    theme_id = client.get_main_theme_id()
    if not theme_id:
        return jsonify({"error": "No main theme found"}), 404

    visuals = visuals_with_warnings(get_article_visuals(store_id))
    files = get_blog_theme_files(visuals["brand_color"], visuals["related_limit"])
    try:
        # Section first, then the template that references it.
        client.upsert_theme_asset(theme_id, BLOG_SECTION_FILE, files[BLOG_SECTION_FILE])
        client.upsert_theme_asset(theme_id, BLOG_TEMPLATE_FILE, files[BLOG_TEMPLATE_FILE])
    except Exception as e:
        logger.exception("Failed to push blog template: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "success": True,
        "installed": True,
        "version": BLOG_TEMPLATE_VERSION,
        "warnings": visuals["link_color_warnings"],
    })


@blog_bp.route("/api/blog/revert-template", methods=["POST"])
def revert_blog_template():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    store, client = _get_store_client(store_id) if store_id else (None, None)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    theme_id = client.get_main_theme_id()
    if not theme_id:
        return jsonify({"error": "No main theme found"}), 404

    # Clear templateSuffix on every article using it first, so a missing
    # template file can never break a live article.
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT shopify_id FROM synced_articles WHERE store_id = %s AND COALESCE(template_suffix, '') = %s",
            (store_id, BLOG_TEMPLATE_SUFFIX),
        )
        ids = [r["shopify_id"] for r in cur.fetchall()]
    cleared_ids = []
    for sid in ids:
        try:
            client.update_article(sid, {"template_suffix": ""})
            cleared_ids.append(sid)
        except Exception as e:
            logger.warning("Failed to clear template_suffix on %s: %s", sid, e)
    if cleared_ids:
        with db.get_cursor() as cur:
            placeholders = ",".join(["%s"] * len(cleared_ids))
            cur.execute(
                f"UPDATE synced_articles SET template_suffix = '' WHERE store_id = %s AND shopify_id IN ({placeholders})",
                (store_id, *cleared_ids),
            )
    if len(cleared_ids) < len(ids):
        return jsonify({
            "error": f"Cleared template on {len(cleared_ids)}/{len(ids)} articles — fix the failures and retry before removing the template files",
        }), 502

    try:
        client.delete_theme_asset(theme_id, BLOG_TEMPLATE_FILE)
        client.delete_theme_asset(theme_id, BLOG_SECTION_FILE)
    except Exception as e:
        logger.exception("Failed to delete blog template files: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "cleared": len(cleared_ids)})


def _apply_template_to_article(client, article_row):
    """Set templateSuffix='blog' on one Shopify article and mirror it locally."""
    client.update_article(article_row["shopify_id"], {"template_suffix": BLOG_TEMPLATE_SUFFIX})
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE synced_articles SET template_suffix = %s WHERE id = %s",
            (BLOG_TEMPLATE_SUFFIX, article_row["id"]),
        )


@blog_bp.route("/api/blog/articles/<int:article_id>/apply-template", methods=["POST"])
def apply_template_to_article(article_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT id, store_id, shopify_id FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404
    store, client = _get_store_client(article["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404
    try:
        _, installed, _ = _blog_template_state(client)
    except Exception as e:
        return jsonify({"error": f"Could not read theme: {e}"}), 502
    if not installed:
        return jsonify({"error": "Install the blog article template first"}), 409
    try:
        _apply_template_to_article(client, article)
    except Exception as e:
        logger.exception("Apply template failed for article %s: %s", article_id, e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"success": True, "template_suffix": BLOG_TEMPLATE_SUFFIX})


@blog_bp.route("/api/blog/retrofit-template", methods=["POST"])
def retrofit_blog_template():
    """Apply templateSuffix='blog' to every blog article not yet using it.
    Content is untouched — this only switches the storefront layout."""
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    store, client = _get_store_client(store_id) if store_id else (None, None)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    try:
        _, installed, _ = _blog_template_state(client)
    except Exception as e:
        return jsonify({"error": f"Could not read theme: {e}"}), 502
    if not installed:
        return jsonify({"error": "Install the blog article template first"}), 409

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, shopify_id, title FROM synced_articles
               WHERE store_id = %s AND article_type = 'blog'
                 AND COALESCE(template_suffix, '') <> %s
                 AND COALESCE(tags, '') NOT ILIKE '%%landing-page%%'
               ORDER BY id""",
            (store_id, BLOG_TEMPLATE_SUFFIX),
        )
        articles = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'blog_template_retrofit', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"total": len(articles)})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
        updated, failed = 0, []
        for art in articles:
            try:
                _apply_template_to_article(client, art)
                updated += 1
            except Exception as e:
                logger.warning("Retrofit failed for article %s: %s", art["id"], e)
                failed.append({"id": art["id"], "title": art.get("title"), "error": str(e)})
        result = {"total": len(articles), "updated": updated, "failed": failed}
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps(result), task_id),
            )

    from app.services.task_runner import spawn as spawn_task

    spawn_task("ai_tasks", task_id, _run)
    return jsonify({"task_id": task_id, "total": len(articles)})


# ── Restyle an existing article into the visual format ──────────

@blog_bp.route("/api/blog/articles/<int:article_id>/restyle", methods=["POST"])
def restyle_article(article_id):
    """Background: rewrite structure (blocks/FAQ/tokens), generate inline images,
    compose. Nothing is written to Shopify until /restyle/apply."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404
    store, client = _get_store_client(article["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'article_restyle', 'pending', %s) RETURNING id""",
            (article["store_id"], json.dumps({"article_id": article_id, "title": article.get("title")})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        from app.ai_agent import run_task as ai_run_task
        from app.services.ai_output_guard import restore_missing_figures, restore_missing_images, sanitize_html
        from app.services.article_media import compose_article_body, count_figures, faq_jsonld_string

        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
        result = ai_run_task("article_restyle", {"article_id": article_id}, store_id=article["store_id"])
        if not isinstance(result, dict) or result.get("error") or not result.get("body_html"):
            raise Exception((result or {}).get("error") or "Restyle returned no body")

        visuals = get_article_visuals(article["store_id"])
        media_plan = result.get("media_plan") or {}
        inline_images = []
        if media_plan.get("inline"):
            try:
                inline_images = _generate_article_media(
                    article["store_id"], client, media_plan, article.get("handle") or article.get("title") or "article"
                )
            except Exception as e:
                logger.warning("Restyle media generation failed for article %s: %s", article_id, e)

        hero = None
        if not article.get("image_url") and (media_plan.get("hero") or {}).get("prompt"):
            try:
                from app.services.article_media import upload_image_to_cdn
                from app.services.image_generator import generate_article_hero_image

                hero_spec = media_plan["hero"]
                refs, _ = _load_hero_reference_images(
                    article["store_id"], hero_spec, body_html=article.get("body"), title=article.get("title")
                )
                gen = generate_article_hero_image(
                    article.get("title") or "", hero_spec.get("prompt"), hero_spec.get("alt"), reference_images=refs
                )
                if gen:
                    image_bytes, filename, alt_text, width, height = gen
                    hero = {
                        "image_url": upload_image_to_cdn(client, image_bytes, filename, alt_text),
                        "image_alt": alt_text,
                        "used_product_refs": len(refs),
                    }
            except Exception as e:
                logger.warning("Restyle hero generation failed for article %s: %s", article_id, e)

        original = article.get("body") or ""
        # Tokenized template (figures from the original restored, tokens kept)
        # is what later per-image regeneration recomposes from.
        body_template = sanitize_html(restore_missing_figures(original, result["body_html"]))
        body = compose_article_body(body_template, inline_images, visuals)
        body = restore_missing_images(original, body)
        body = sanitize_html(body)
        payload = {
            "article_id": article_id,
            "body_html": body,
            "body_template": body_template,
            "inline_images": inline_images,
            "media_plan": media_plan,
            "faq_jsonld": faq_jsonld_string(body),
            "hero": hero,
            "stats": {
                "before_words": len(re.sub(r"<[^>]+>", " ", original).split()),
                "after_words": len(re.sub(r"<[^>]+>", " ", body).split()),
                "before_figures": count_figures(original),
                "after_figures": count_figures(body),
                "ready_images": len([i for i in inline_images if i.get("status") == "ready"]),
            },
            "_qa": result.get("_qa"),
        }
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps(payload), task_id),
            )

    from app.services.task_runner import spawn as spawn_task

    spawn_task("ai_tasks", task_id, _run)
    return jsonify({"task_id": task_id})


@blog_bp.route("/api/blog/articles/<int:article_id>/restyle/latest", methods=["GET"])
def latest_restyle(article_id):
    """Most recent restyle task for this article (any status) that hasn't been applied."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, status, error, result, created_at, completed_at FROM ai_tasks
               WHERE task_type = 'article_restyle' AND (input->>'article_id')::int = %s
               ORDER BY id DESC LIMIT 1""",
            (article_id,),
        )
        task = cur.fetchone()
    if not task:
        return jsonify({"task": None})
    result = task.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = None
    if result and result.get("applied"):
        return jsonify({"task": None})
    return jsonify({"task": {
        "id": task["id"], "status": task["status"], "error": task.get("error"),
        "created_at": task["created_at"].isoformat() if task.get("created_at") else None,
        "result": result if task["status"] == "completed" else None,
    }})


@blog_bp.route("/api/blog/articles/<int:article_id>/restyle/apply", methods=["POST"])
def apply_restyle(article_id):
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
        cur.execute("SELECT result, status FROM ai_tasks WHERE id = %s AND task_type = 'article_restyle'", (task_id,))
        task = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404
    if not task or task.get("status") != "completed" or not task.get("result"):
        return jsonify({"error": "Restyle result not ready"}), 409
    payload = task["result"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if payload.get("article_id") != article_id:
        return jsonify({"error": "Restyle result belongs to a different article"}), 400
    store, client = _get_store_client(article["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    body = data.get("body_html") or payload.get("body_html") or ""
    fields = {"body": body, "template_suffix": BLOG_TEMPLATE_SUFFIX}
    hero = payload.get("hero") or {}
    if hero.get("image_url") and not article.get("image_url"):
        fields["image_url"] = hero["image_url"]
        fields["image_alt"] = hero.get("image_alt") or article.get("title") or ""
    extras = {k: v for k, v in fields.items() if k != "body"}
    try:
        # Media is settled when the restyle is generated; putting "missing"
        # figures back here would undo deliberate layout changes.
        applied = apply_item_changes(
            article["store_id"], "article", article_id, {"body": body},
            source="blog", change_type="restyle", client=client, extra_fields=extras,
            allow_dropped_blocks=bool(data.get("force")), restore_media=False,
        )
        fields["body"] = applied["pushed"].get("body", article.get("body") or body)
        faq_json = payload.get("faq_jsonld")
        if faq_json:
            try:
                client.set_article_metafields(article["shopify_id"], [("global", "faq_jsonld", faq_json, "json")])
            except Exception as e:
                logger.warning("FAQ metafield write failed for article %s: %s", article_id, e)
    except ApplyRefused as refused:
        return refusal_response(refused)
    except Exception as e:
        logger.exception("Restyle apply failed for article %s: %s", article_id, e)
        return jsonify({"error": str(e)}), 502

    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE synced_articles SET body = %s, template_suffix = %s,
                   image_url = COALESCE(NULLIF(%s, ''), image_url),
                   image_alt = CASE WHEN %s <> '' THEN %s ELSE image_alt END,
                   last_optimized_at = NOW()
               WHERE id = %s""",
            (fields["body"], BLOG_TEMPLATE_SUFFIX, fields.get("image_url", ""), fields.get("image_alt", ""), fields.get("image_alt", ""), article_id),
        )
    payload["applied"] = True
    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET result = %s WHERE id = %s", (json.dumps(payload), task_id))
    try:
        _link_article_media_session(article, payload, body_template=payload.get("body_template"))
    except Exception as e:
        logger.warning("Media session link failed for article %s: %s", article_id, e)
    return jsonify({"success": True, "template_suffix": BLOG_TEMPLATE_SUFFIX, "figures": payload.get("stats", {}).get("after_figures")})


# ── Per-image regeneration for published articles ────────────────

def _find_article_media_session(article):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT * FROM article_wizard_sessions
               WHERE store_id = %s AND shopify_article_id = %s
               ORDER BY updated_at DESC LIMIT 1""",
            (article["store_id"], article["shopify_id"]),
        )
        return cur.fetchone()


def _link_article_media_session(article, payload, body_template=None):
    """Upsert the wizard session that owns a published article's inline images.

    Restyle has no wizard session, so one is created (status published, never
    scheduled — the calendar and queues filter on scheduled_for). A wizard
    article that gets restyled keeps its session; its body/plan are replaced.
    `body_template` is the tokenized body; without one the live body is
    tokenized from the ready images (failed images then have no placeholder)."""
    from app.services.article_media import tokenize_figures

    inline_images = payload.get("inline_images") or []
    template = body_template or tokenize_figures(article.get("body") or "", inline_images)
    session = _find_article_media_session(article)
    generated = dict((session or {}).get("generated_article") or {})
    generated.update({"title": article.get("title") or generated.get("title"), "body_html": template,
                      "handle": article.get("handle") or generated.get("handle")})
    media_plan = payload.get("media_plan") or {}
    with db.get_cursor() as cur:
        if session:
            cur.execute(
                """UPDATE article_wizard_sessions
                   SET generated_article = %s, media_plan = %s, inline_images = %s,
                       status = 'published', style = %s, current_step = 7, updated_at = NOW()
                   WHERE id = %s""",
                (json.dumps(generated), json.dumps(media_plan), json.dumps(inline_images), STYLE_VISUAL, session["id"]),
            )
            return session["id"]
        cur.execute(
            """INSERT INTO article_wizard_sessions
               (store_id, status, current_step, topic, blog_id, generated_article, featured_image_url,
                shopify_article_id, style, media_plan, inline_images)
               VALUES (%s, 'published', 7, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (article["store_id"], (article.get("title") or "")[:500], article.get("blog_id"), json.dumps(generated),
             article.get("image_url"), article["shopify_id"], STYLE_VISUAL, json.dumps(media_plan), json.dumps(inline_images)),
        )
        return cur.fetchone()["id"]


def _backfill_article_media_session(article):
    """Articles restyled before sessions were linked: rebuild one from the
    latest applied restyle task."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT result FROM ai_tasks
               WHERE task_type = 'article_restyle' AND status = 'completed'
                 AND (input->>'article_id')::int = %s AND result->>'applied' = 'true'
               ORDER BY id DESC LIMIT 1""",
            (article["id"],),
        )
        row = cur.fetchone()
    if not row:
        return None
    payload = row["result"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not (payload.get("media_plan") or {}).get("inline"):
        return None
    _link_article_media_session(article, payload, body_template=payload.get("body_template"))
    return _find_article_media_session(article)


@blog_bp.route("/api/blog/articles/<int:article_id>/media", methods=["GET"])
def article_media(article_id):
    """Inline-image plan + generation state for a published article, so single
    images can be regenerated from the article modal."""
    from app.services.article_media import TOKEN_RE

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
        article = cur.fetchone()
    if not article:
        return jsonify({"error": "Article not found"}), 404
    session = _find_article_media_session(article)
    if not session or not ((session.get("media_plan") or {}).get("inline")):
        # Restyled before sessions were linked, or a pre-visual wizard session
        # that a later restyle replaced: rebuild from the applied restyle task.
        session = _backfill_article_media_session(article)
    if not session or not ((session.get("media_plan") or {}).get("inline")):
        return jsonify({"session_id": None, "inline": []})

    template = (session.get("generated_article") or {}).get("body_html") or ""
    tokens_in_template = {f"[[INLINE_IMAGE_{n}]]" for n in TOKEN_RE.findall(template)}
    by_token = {e.get("token"): e for e in (session.get("inline_images") or []) if isinstance(e, dict)}
    inline = []
    for spec in session["media_plan"]["inline"]:
        entry = by_token.get(spec.get("token")) or {}
        inline.append({
            **spec,
            "cdn_url": entry.get("cdn_url") or "",
            "status": entry.get("status") or "pending",
            "error": entry.get("error"),
            "placeable": bool(entry.get("cdn_url")) or spec.get("token") in tokens_in_template,
        })
    task_id = None
    if session.get("status") == "generating_media":
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT id FROM ai_tasks
                   WHERE task_type = 'article_media_regen' AND status IN ('pending', 'running')
                     AND (input->>'session_id')::int = %s
                   ORDER BY id DESC LIMIT 1""",
                (session["id"],),
            )
            row = cur.fetchone()
            task_id = row["id"] if row else None
    return jsonify({"session_id": session["id"], "status": session.get("status"), "task_id": task_id, "inline": inline})


def _blog_section_key(client, theme_id):
    """Asset key of the section that prints the blog index H1, or None."""
    template = client.get_theme_asset(theme_id, "templates/blog.json")
    if not template:
        return None
    try:
        sections = json.loads(re.sub(r"/\*.*?\*/", "", template, flags=re.S)).get("sections") or {}
    except ValueError:
        return None
    for section in sections.values():
        key = f"sections/{section.get('type')}.liquid"
        content = client.get_theme_asset(theme_id, key) if section.get("type") else None
        if content and "blog.title" in content and "<h1" in content.lower():
            return key
    return None


def _blog_h1_patch_state(client, theme_id):
    from app.services.seo_snippets import blog_section_patch_state

    try:
        key = _blog_section_key(client, theme_id)
        return blog_section_patch_state(client.get_theme_asset(theme_id, key) if key else None)
    except Exception as e:
        logger.warning("Blog section patch state unavailable: %s", e)
        return "unknown"


def _patch_blog_h1(client, theme_id):
    """Patch the blog section so the hub prints its own H1. Returns the parts changed."""
    from app.services.seo_snippets import patch_blog_section

    key = _blog_section_key(client, theme_id)
    if not key:
        return []
    patched, parts = patch_blog_section(client.get_theme_asset(theme_id, key))
    if parts:
        client.upsert_theme_asset(theme_id, key, patched)
        logger.info("Patched %s for the blog hub: %s", key, ", ".join(parts))
    return parts


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

        try:
            hub_parts = _patch_blog_h1(client, theme_id)
        except Exception as e:
            logger.warning("Blog section patch failed: %s", e)
            hub_parts = []

        return jsonify({
            "success": True,
            "snippet_pushed": True,
            "theme_liquid_updated": "{% render 'seo-fixes' %}" in theme_liquid,
            "meta_descriptions_commented": desc_results,
            "blog_section_patched": hub_parts,
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

        try:
            from app.services.seo_snippets import unpatch_blog_section

            key = _blog_section_key(client, theme_id)
            content = client.get_theme_asset(theme_id, key) if key else None
            if content and unpatch_blog_section(content) != content:
                client.upsert_theme_asset(theme_id, key, unpatch_blog_section(content))
        except Exception as e:
            logger.warning("Blog section unpatch failed: %s", e)

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

        from app.services.seo_snippets import SNIPPET_VERSION

        snippet_content = client.get_theme_asset(theme_id, "snippets/seo-fixes.liquid")
        outdated = snippet_content is not None and f"SEO Fixes Snippet {SNIPPET_VERSION} " not in snippet_content
        h1_patch = _blog_h1_patch_state(client, theme_id)
        return jsonify({"installed": True, "outdated": outdated or h1_patch == "missing", "hub_h1_patch": h1_patch})
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
