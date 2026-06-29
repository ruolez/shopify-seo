import json
import logging
import threading
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from app.database import db
from app.routes.blog_routes import _get_store_client
from app.services.news_body import insert_inline_images, render_sources_html
from app.services.news_template import (
    NEWS_SECTION_FILE,
    NEWS_TEMPLATE_FILE,
    NEWS_TEMPLATE_MARKER,
    NEWS_TEMPLATE_SUFFIX,
    NEWS_TEMPLATE_VERSION,
    get_news_theme_files,
)

logger = logging.getLogger(__name__)

news_bp = Blueprint("news", __name__)


# ── Helpers ─────────────────────────────────────────────────────

def _get_news_blog(store_id, client):
    """Return the synced_blogs row for the 'news' blog, creating it if missing."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM synced_blogs WHERE store_id = %s AND handle = 'news'",
            (store_id,),
        )
        blog = cur.fetchone()
    if blog:
        return dict(blog)

    blog_result = client.create_blog("News")
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO synced_blogs (store_id, shopify_id, title, handle, synced_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                title = EXCLUDED.title, handle = EXCLUDED.handle, synced_at = NOW()
            RETURNING *""",
            (store_id, blog_result["shopify_id"], blog_result["title"], blog_result.get("handle", "news")),
        )
        blog = cur.fetchone()
    return dict(blog)


def _publisher_identity(store_id, store):
    """Resolve (publisher_name, logo_url) for NewsArticle schema from schema_settings."""
    name = None
    logo = None
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT organization_name, organization_logo_url FROM schema_settings WHERE store_id = %s",
                (store_id,),
            )
            row = cur.fetchone()
        if row:
            name = row.get("organization_name") or None
            logo = row.get("organization_logo_url") or None
    except Exception as e:
        logger.warning("Could not load schema_settings for publisher identity: %s", e)
    name = name or (store.get("shop_name") if store else None) or (store.get("name") if store else None) or "News"
    return name, logo


def _regenerate_news_hub(store_id, client, store):
    """Build and upsert the News hub Shopify Page. Synchronous; returns a result
    dict. Raises on failure (callers decide how to surface it)."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT * FROM synced_articles
               WHERE store_id = %s AND article_type = 'news' AND is_published = TRUE
               ORDER BY published_at DESC NULLS LAST""",
            (store_id,),
        )
        articles = [dict(a) for a in cur.fetchall()]

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_hub_pages WHERE store_id = %s", (store_id,))
        existing = cur.fetchone()
    existing = dict(existing) if existing else None

    from app.services.news_hub_generator import generate_news_hub_html

    intro = existing.get("intro_text") if existing else None
    hero_url = existing.get("hero_image_url") if existing else None
    hero_alt = existing.get("hero_image_alt") if existing else None
    tz = client.get_shop_timezone()
    html_content = generate_news_hub_html(
        articles, intro_text=intro, hero_image_url=hero_url, hero_image_alt=hero_alt, heading="News", tz=tz
    )

    page_title = "News"
    meta_desc = (intro or "The latest news, announcements, and updates.")[:160]

    page_id = existing.get("shopify_page_id") if existing else None
    handle = existing.get("handle") if existing else "news"
    if page_id:
        try:
            client.update_page(page_id, {
                "title": page_title, "body": html_content, "is_published": True,
                "seo_title": page_title[:60], "seo_description": meta_desc,
            })
        except Exception:
            page_id = None
    if not page_id:
        result = client.create_page(
            page_title, html_content, handle="news", is_published=True,
            seo_title=page_title[:60], seo_description=meta_desc,
        )
        page_id = result["shopify_id"]
        handle = result.get("handle", "news")

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO news_hub_pages (store_id, shopify_page_id, handle, last_generated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (store_id) DO UPDATE SET
                shopify_page_id = EXCLUDED.shopify_page_id,
                handle = EXCLUDED.handle, last_generated_at = NOW()""",
            (store_id, page_id, handle),
        )
    logger.info("Regenerated News hub page for store %s (%d articles)", store_id, len(articles))
    return {"articles_count": len(articles), "handle": handle, "url": f"/pages/{handle}"}


def _regen_news_hub_async(store_id, client, store):
    """Fire-and-forget hub regen (used after publish)."""
    def _run():
        try:
            _regenerate_news_hub(store_id, client, store)
        except Exception as e:
            logger.exception("Failed to regenerate News hub: %s", e)

    threading.Thread(target=_run, daemon=True).start()


# ── Pipeline steps (shared by manual routes + auto orchestrator) ─

def _fetch_session(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_sessions WHERE id = %s", (session_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _normalize_sources(data):
    """Coerce request 'source_urls' (URL strings or {url,title} dicts) into [{url,title}]."""
    raw = data.get("source_urls")
    if not isinstance(raw, list):
        single = (data.get("source_url") or "").strip()
        raw = [single] if single else []
    sources = []
    for s in raw:
        if isinstance(s, dict):
            u = (s.get("url") or "").strip()
            t = (s.get("title") or "").strip()
        elif isinstance(s, str):
            u, t = s.strip(), ""
        else:
            continue
        if u:
            sources.append({"url": u, "title": t})
    return sources


def _step_analyze(session_id, store_id):
    """Run the news_analyze AI task and persist its results to the session. Raises on error."""
    from app.ai_agent import run_task as ai_run_task

    session = _fetch_session(session_id) or {}
    result = ai_run_task(
        "news_analyze",
        {"source_text": session.get("source_text") or "", "store_id": store_id},
        store_id=store_id,
    )
    if result.get("error"):
        raise Exception(result["error"])
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE news_sessions SET
                analyze_result = %s, edited_result = %s, status = 'analyzed',
                category = %s, tags = %s, meta_title = %s, meta_description = %s,
                summary = %s, hero_image_alt = %s, hero_image_prompt = %s,
                inline_images = %s, updated_at = NOW()
               WHERE id = %s""",
            (
                json.dumps(result), json.dumps(result),
                result.get("category"), json.dumps(result.get("tags") or []),
                result.get("meta_title"), result.get("meta_description"),
                result.get("summary"), result.get("hero_image_alt"),
                result.get("hero_image_prompt"),
                json.dumps(result.get("inline_images") or []),
                session_id,
            ),
        )
    return result


def _step_hero(session, client):
    """Generate + upload the hero image and persist hero_image_url. Returns the URL; raises on failure."""
    from app.services.image_generator import generate_blog_image_ai

    edited = session.get("edited_result") or session.get("analyze_result") or {}
    headline = edited.get("headline") or "News"
    hero_prompt = edited.get("hero_image_prompt") or ""
    tags = edited.get("tags") or []
    keywords = ([hero_prompt] if hero_prompt else []) + list(tags)[:4]
    result = generate_blog_image_ai(headline, keywords=keywords)
    if not result:
        raise Exception("Hero image generation returned no result")
    image_bytes, filename, alt_text, width, height = result
    mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
    staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
    resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime)
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE news_sessions SET hero_image_url = %s, updated_at = NOW() WHERE id = %s",
            (resource_url, session["id"]),
        )
    return resource_url


def _step_inline(session, client):
    """Generate + upload each inline image and persist the inline_images list. Per-image best-effort."""
    from app.services.image_generator import generate_news_inline_image

    edited = session.get("edited_result") or session.get("analyze_result") or {}
    specs = edited.get("inline_images") or []
    out = []
    for spec in specs:
        entry = {
            "token": spec.get("token"),
            "prompt": spec.get("prompt"),
            "alt": spec.get("alt"),
            "caption": spec.get("caption"),
            "cdn_url": "",
            "status": "pending",
        }
        try:
            result = generate_news_inline_image(spec.get("prompt"), spec.get("alt"))
            if not result:
                raise Exception("no image returned")
            image_bytes, filename, alt_text, width, height = result
            mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
            staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
            resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime)
            file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
            cdn_url = client.file_get_url(file_obj.get("id"))
            entry.update({"cdn_url": cdn_url, "width": width, "height": height, "status": "ready"})
        except Exception as e:
            logger.warning("Inline image failed for token %s: %s", spec.get("token"), e)
            entry["status"] = "failed"
        out.append(entry)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE news_sessions SET inline_images = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(out), session["id"]),
        )
    return out


def _step_links(session, store_id):
    """Run the news_links AI task and update edited_result.body_html. Raises on error; no-op if no body."""
    from app.ai_agent import run_task as ai_run_task

    edited = session.get("edited_result") or session.get("analyze_result") or {}
    body_html = edited.get("body_html") or ""
    if not body_html.strip():
        return None
    result = ai_run_task(
        "news_links",
        {"body_html": body_html, "headline": edited.get("headline") or "", "store_id": store_id},
        store_id=store_id,
    )
    if result.get("error"):
        raise Exception(result["error"])
    new_edited = dict(edited)
    new_edited["body_html"] = result.get("body_html") or body_html
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE news_sessions SET edited_result = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(new_edited), session["id"]),
        )
    return result


def _step_publish(session, store, client, is_published=True, regen_hub=True):
    """Compose the final body, create the Shopify article, persist, and regen the hub.

    Returns the publish result dict. Raises on a fatal error (nothing to publish / API failure).
    Batch callers pass regen_hub=False and regenerate the hub once after the whole batch.
    """
    edited = session.get("edited_result") or session.get("analyze_result") or {}
    if not edited:
        raise Exception("Nothing to publish — run analysis first")
    store_id = session["store_id"]

    blog = _get_news_blog(store_id, client)
    blog_id = blog["id"]

    # Compose final body: insert generated inline images, then append Sources.
    body_html = insert_inline_images(edited.get("body_html") or "", session.get("inline_images") or [])
    sources_html = render_sources_html(session.get("source_urls") or [])
    if sources_html:
        body_html = f"{body_html}\n{sources_html}"

    tags = edited.get("tags") or []
    if "type:news" not in tags:
        tags = ["type:news"] + tags
    headline = edited.get("headline") or "Untitled News"
    category = edited.get("category") or ""

    now = datetime.now(timezone.utc)
    fields = {
        "title": headline,
        "body": body_html,
        "summary": edited.get("summary", ""),
        "tags": tags,
        "is_published": is_published,
        "publish_date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "template_suffix": NEWS_TEMPLATE_SUFFIX,
        "author": "A2Z",
    }
    if session.get("hero_image_url"):
        fields["image_url"] = session["hero_image_url"]
        fields["image_alt"] = edited.get("hero_image_alt") or headline

    result = client.create_article(blog["shopify_id"], fields)
    shopify_id = result["shopify_id"]

    # SEO meta + news metafields (category + dateModified).
    try:
        client.set_article_seo(shopify_id, edited.get("meta_title") or "", edited.get("meta_description") or "")
    except Exception as e:
        logger.warning("Failed to set news SEO metafields: %s", e)
    try:
        client.set_article_metafields(shopify_id, [
            ("global", "news_category", category or None, "single_line_text_field"),
            ("global", "news_date_modified", now.strftime("%Y-%m-%d"), "date"),
        ])
    except Exception as e:
        logger.warning("Failed to set news category/date metafields: %s", e)

    cdn_image_url = result.get("image_url", "")
    if "shopify-staged-uploads" in (cdn_image_url or ""):
        cdn_image_url = ""

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO synced_articles
                (store_id, blog_id, shopify_id, shopify_blog_id, title, handle,
                 body, summary, tags, author, meta_title, meta_description,
                 image_url, image_alt, is_published, published_at,
                 article_type, template_suffix, category, date_modified, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'A2Z', %s, %s, %s, %s, %s, %s,
                    'news', %s, %s, %s, NOW())
            ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                title = EXCLUDED.title, handle = EXCLUDED.handle, body = EXCLUDED.body,
                summary = EXCLUDED.summary, tags = EXCLUDED.tags,
                meta_title = EXCLUDED.meta_title, meta_description = EXCLUDED.meta_description,
                image_url = EXCLUDED.image_url, image_alt = EXCLUDED.image_alt,
                is_published = EXCLUDED.is_published, category = EXCLUDED.category,
                template_suffix = EXCLUDED.template_suffix, date_modified = EXCLUDED.date_modified,
                synced_at = NOW()
            RETURNING id""",
            (
                store_id, blog_id, shopify_id, blog["shopify_id"],
                headline, result.get("handle", ""),
                body_html, fields["summary"],
                ", ".join(tags),
                edited.get("meta_title", ""), edited.get("meta_description", ""),
                cdn_image_url, fields.get("image_alt", ""),
                is_published, now if is_published else None,
                NEWS_TEMPLATE_SUFFIX, category, now,
            ),
        )
        article_row = cur.fetchone()
        cur.execute(
            "UPDATE news_sessions SET status = 'published', shopify_article_id = %s, article_handle = %s, updated_at = NOW() WHERE id = %s",
            (shopify_id, result.get("handle", ""), session["id"]),
        )

    if is_published and regen_hub:
        _regen_news_hub_async(store_id, client, store)

    return {
        "success": True,
        "article_id": article_row["id"],
        "shopify_id": shopify_id,
        "handle": result.get("handle", ""),
        "url": f"/blogs/news/{result.get('handle', '')}",
    }


def _run_auto_pipeline(session_id, store_id, store, client, regen_hub=True):
    """Run the full auto pipeline for one session: analyze → hero → inline → links → publish.

    Updates the session 'status' for UI progress. Image/link steps are best-effort; analyze and
    publish are fatal (raise). Returns the publish result dict. Batch callers pass regen_hub=False
    and regenerate the hub once after the whole batch.
    """
    def _set_status(status):
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE news_sessions SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, session_id),
            )

    # 1. Analyze & enhance (fatal)
    _set_status("auto_analyzing")
    _step_analyze(session_id, store_id)

    # 2. Hero image (best-effort)
    _set_status("auto_hero")
    try:
        _step_hero(_fetch_session(session_id), client)
    except Exception as e:
        logger.warning("Auto: hero image failed, continuing: %s", e)

    # 3. Inline images (best-effort, only when the analyze step proposed any)
    session = _fetch_session(session_id)
    edited = session.get("edited_result") or session.get("analyze_result") or {}
    if edited.get("inline_images"):
        _set_status("auto_inline")
        try:
            _step_inline(session, client)
        except Exception as e:
            logger.warning("Auto: inline images failed, continuing: %s", e)

    # 4. Internal links (best-effort)
    _set_status("auto_links")
    try:
        _step_links(_fetch_session(session_id), store_id)
    except Exception as e:
        logger.warning("Auto: linking failed, continuing: %s", e)

    # 5. Publish live (fatal). _step_publish sets session status to 'published'.
    _set_status("auto_publishing")
    return _step_publish(_fetch_session(session_id), store, client, True, regen_hub=regen_hub)


# ── Page ────────────────────────────────────────────────────────

@news_bp.route("/news")
def news_page():
    return render_template("news.html")


# ── Article listing ─────────────────────────────────────────────

@news_bp.route("/api/news/articles", methods=["GET"])
def list_news_articles():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"articles": []})
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, shopify_id, title, handle, category, tags, image_url,
                      is_published, published_at, date_modified
               FROM synced_articles
               WHERE store_id = %s AND article_type = 'news'
               ORDER BY published_at DESC NULLS LAST""",
            (store_id,),
        )
        articles = [dict(a) for a in cur.fetchall()]
    return jsonify({"articles": articles})


# ── Analyze (paste -> AI enhance) ───────────────────────────────

@news_bp.route("/api/news/analyze", methods=["POST"])
def news_analyze():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    source_text = (data.get("source_text") or "").strip()
    sources = _normalize_sources(data)
    if not store_id or not source_text:
        return jsonify({"error": "store_id and source_text are required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO news_sessions (store_id, status, source_text, source_url, source_urls)
               VALUES (%s, 'analyzing', %s, %s, %s) RETURNING id""",
            (store_id, source_text, sources[0]["url"] if sources else "", json.dumps(sources)),
        )
        session_id = cur.fetchone()["id"]

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'news_analyze', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"session_id": session_id, "store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _step_analyze(session_id, store_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("news_analyze failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE news_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                    (str(e), session_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"session_id": session_id, "task_id": task_id})


@news_bp.route("/api/news/session/<int:session_id>", methods=["GET"])
def get_news_session(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(dict(session))


@news_bp.route("/api/news/session/<int:session_id>", methods=["PATCH"])
def patch_news_session(session_id):
    data = request.get_json() or {}
    edited = data.get("edited_result")
    if edited is None:
        return jsonify({"error": "edited_result required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE news_sessions SET
                edited_result = %s, category = %s, tags = %s,
                meta_title = %s, meta_description = %s, summary = %s,
                hero_image_alt = %s, status = 'review', updated_at = NOW()
               WHERE id = %s""",
            (
                json.dumps(edited),
                edited.get("category"),
                json.dumps(edited.get("tags") or []),
                edited.get("meta_title"),
                edited.get("meta_description"),
                edited.get("summary"),
                edited.get("hero_image_alt"),
                session_id,
            ),
        )
    return jsonify({"ok": True})


# ── Hero image generation ───────────────────────────────────────

@news_bp.route("/api/news/session/<int:session_id>/generate-hero", methods=["POST"])
def generate_hero(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404
    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'news_hero_image', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({"session_id": session_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            resource_url = _step_hero(dict(session), client)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"image_url": resource_url}), task_id),
                )
        except Exception as e:
            logger.exception("News hero image generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


# ── Inline image generation (resolves to CDN URLs) ──────────────

@news_bp.route("/api/news/session/<int:session_id>/generate-inline", methods=["POST"])
def generate_inline(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404
    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    edited = session.get("edited_result") or session.get("analyze_result") or {}
    specs = edited.get("inline_images") or []
    if not specs:
        return jsonify({"error": "No inline images to generate"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'news_inline_images', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({"session_id": session_id, "count": len(specs)})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            out = _step_inline(dict(session), client)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"inline_images": out}), task_id),
                )
        except Exception as e:
            logger.exception("News inline image generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


# ── Internal linking pass ───────────────────────────────────────

@news_bp.route("/api/news/session/<int:session_id>/generate-links", methods=["POST"])
def generate_links(session_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    edited = session.get("edited_result") or session.get("analyze_result") or {}
    body_html = edited.get("body_html") or ""
    if not body_html.strip():
        return jsonify({"error": "No article body to link yet"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'news_links', 'pending', %s) RETURNING id""",
            (session["store_id"], json.dumps({"session_id": session_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _step_links(dict(session), session["store_id"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result or {}), task_id),
                )
        except Exception as e:
            logger.exception("news_links pass failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


# ── Publish ─────────────────────────────────────────────────────

@news_bp.route("/api/news/session/<int:session_id>/publish", methods=["POST"])
def publish_news(session_id):
    data = request.get_json() or {}
    is_published = data.get("is_published", True)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM news_sessions WHERE id = %s", (session_id,))
        session = cur.fetchone()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    store, client = _get_store_client(session["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    edited = session.get("edited_result") or session.get("analyze_result") or {}
    if not edited:
        return jsonify({"error": "Nothing to publish — run analysis first"}), 400

    try:
        result = _step_publish(dict(session), store, client, is_published)
    except Exception as e:
        logger.exception("news publish failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


# ── Auto mode (upload → publish, fully automated) ───────────────

@news_bp.route("/api/news/auto", methods=["POST"])
def news_auto():
    """Run the full pipeline (analyze → hero → inline → links → publish) in one
    background thread. Image/link steps are best-effort; analyze and publish are
    fatal. The session 'status' column reports the current stage for the UI."""
    data = request.get_json() or {}
    store_id = data.get("store_id")
    source_text = (data.get("source_text") or "").strip()
    sources = _normalize_sources(data)
    if not store_id or not source_text:
        return jsonify({"error": "store_id and source_text are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO news_sessions (store_id, status, source_text, source_url, source_urls)
               VALUES (%s, 'auto_queued', %s, %s, %s) RETURNING id""",
            (store_id, source_text, sources[0]["url"] if sources else "", json.dumps(sources)),
        )
        session_id = cur.fetchone()["id"]

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'news_auto', 'pending', %s) RETURNING id""",
            (store_id, json.dumps({"session_id": session_id, "store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _run_auto_pipeline(session_id, store_id, store, client)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("news_auto pipeline failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE news_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                    (str(e), session_id),
                )
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"session_id": session_id, "task_id": task_id})


@news_bp.route("/api/news/auto-batch", methods=["POST"])
def news_auto_batch():
    """Auto-publish multiple articles. Creates one session per article and processes them
    sequentially in a single background thread (rate-limit friendly, survives browser close).
    A failed article is isolated; the rest continue. The hub is regenerated once at the end."""
    data = request.get_json() or {}
    store_id = data.get("store_id")
    articles = data.get("articles")
    if not store_id or not isinstance(articles, list) or not articles:
        return jsonify({"error": "store_id and a non-empty articles list are required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    session_ids = []
    for art in articles:
        source_text = (art.get("source_text") or "").strip() if isinstance(art, dict) else ""
        if not source_text:
            continue
        sources = _normalize_sources(art)
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO news_sessions (store_id, status, source_text, source_url, source_urls)
                   VALUES (%s, 'auto_queued', %s, %s, %s) RETURNING id""",
                (store_id, source_text, sources[0]["url"] if sources else "", json.dumps(sources)),
            )
            session_ids.append(cur.fetchone()["id"])

    if not session_ids:
        return jsonify({"error": "No articles with source_text"}), 400

    def _run():
        published_any = False
        for sid in session_ids:
            try:
                _run_auto_pipeline(sid, store_id, store, client, regen_hub=False)
                published_any = True
            except Exception as e:
                logger.exception("news_auto_batch: session %s failed: %s", sid, e)
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE news_sessions SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                        (str(e), sid),
                    )
        if published_any:
            _regen_news_hub_async(store_id, client, store)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"sessions": session_ids})


@news_bp.route("/api/news/session-status", methods=["POST"])
def news_session_status():
    """Return minimal status for a list of session ids (for batch progress polling)."""
    data = request.get_json() or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"sessions": []})
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return jsonify({"sessions": []})
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, status, article_handle, error FROM news_sessions WHERE id = ANY(%s)",
            (ids,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"sessions": rows})


# ── Theme template install / status / revert ────────────────────

@news_bp.route("/api/news/push-template", methods=["POST"])
def push_template():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    theme_id = client.get_main_theme_id()
    if not theme_id:
        return jsonify({"error": "No main theme found"}), 404

    publisher_name, logo_url = _publisher_identity(store_id, store)
    files = get_news_theme_files(publisher_name, logo_url=logo_url)

    try:
        # Section first, then the template that references it.
        client.upsert_theme_asset(theme_id, NEWS_SECTION_FILE, files[NEWS_SECTION_FILE])
        client.upsert_theme_asset(theme_id, NEWS_TEMPLATE_FILE, files[NEWS_TEMPLATE_FILE])
    except Exception as e:
        logger.exception("Failed to push news template: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True, "installed": True})


@news_bp.route("/api/news/template-status", methods=["GET"])
def template_status():
    store_id = request.args.get("store_id")
    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"installed": False})
    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"installed": False})
        template = client.get_theme_asset(theme_id, NEWS_TEMPLATE_FILE)
        installed = template is not None and "A2Z News Template" in (template or "")
        if not installed:
            return jsonify({"installed": False})
        outdated = NEWS_TEMPLATE_MARKER not in (template or "")
        return jsonify({"installed": True, "outdated": outdated, "version": NEWS_TEMPLATE_VERSION})
    except Exception:
        return jsonify({"installed": False})


@news_bp.route("/api/news/revert-template", methods=["POST"])
def revert_template():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    theme_id = client.get_main_theme_id()
    if not theme_id:
        return jsonify({"error": "No main theme found"}), 404

    # Clear templateSuffix on all news articles first, so a missing template
    # file can never break a live article (Shopify falls back to article.liquid).
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT shopify_id FROM synced_articles WHERE store_id = %s AND article_type = 'news'",
            (store_id,),
        )
        news_ids = [r["shopify_id"] for r in cur.fetchall()]
    cleared = 0
    for sid in news_ids:
        try:
            client.update_article(sid, {"template_suffix": ""})
            cleared += 1
        except Exception as e:
            logger.warning("Failed to clear template_suffix on %s: %s", sid, e)
    if news_ids:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE synced_articles SET template_suffix = '' WHERE store_id = %s AND article_type = 'news'",
                (store_id,),
            )

    # Delete template then section.
    try:
        client.delete_theme_asset(theme_id, NEWS_TEMPLATE_FILE)
        client.delete_theme_asset(theme_id, NEWS_SECTION_FILE)
    except Exception as e:
        logger.exception("Failed to delete news theme files: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True, "reverted": True, "articles_cleared": cleared})


# ── Hub regeneration ────────────────────────────────────────────

@news_bp.route("/api/news/regenerate-hub", methods=["POST"])
def regenerate_hub():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input)
               VALUES (%s, 'news_hub', 'pending', '{}') RETURNING id""",
            (store_id,),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
            result = _regenerate_news_hub(store_id, client, store)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("News hub regen failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})
