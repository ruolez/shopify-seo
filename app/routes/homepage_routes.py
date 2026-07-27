import json
import logging
import threading

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services.task_runner import spawn as spawn_task
from app.services.home_carousel_generator import (
    SECTION_FILENAME,
    inject_section_into_index,
    remove_section_from_index,
    render_carousel_body,
    render_home_carousel_section,
)
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

homepage_bp = Blueprint("homepage", __name__)

INDEX_TEMPLATE = "templates/index.json"


def _get_store_client(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None, None
    access_token = decrypt_value(store["access_token_enc"])
    client = ShopifyClient(store["domain"], access_token, store["api_version"])
    return store, client


def _get_or_create_home_page(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM home_pages WHERE store_id = %s", (store_id,))
        row = cur.fetchone()
        if row:
            return dict(row)
        cur.execute(
            "INSERT INTO home_pages (store_id) VALUES (%s) RETURNING *",
            (store_id,),
        )
        return dict(cur.fetchone())


def _normalize_slides(slides):
    if isinstance(slides, str):
        slides = json.loads(slides)
    return slides or []


# ── Page render ────────────────────────────────────────────────────

@homepage_bp.route("/home-page")
def home_page_page():
    return render_template("homepage.html")


# ── Get / Save ─────────────────────────────────────────────────────

@homepage_bp.route("/api/home-page")
def get_home_page():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    home = _get_or_create_home_page(store_id)
    home["slides"] = _normalize_slides(home.get("slides"))

    # Best-effort: surface the live theme's current home-page sections for context.
    live_sections = []
    try:
        _store, client = _get_store_client(store_id)
        if client:
            live_sections = [
                {"type": s.get("type", ""), "key": s.get("key", "")}
                for s in client.get_homepage_theme_sections()
            ]
    except Exception as e:
        logger.warning("Could not fetch live theme sections: %s", e)

    return jsonify({"home_page": home, "live_sections": live_sections})


@homepage_bp.route("/api/home-page", methods=["PUT"])
def save_home_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    _get_or_create_home_page(store_id)
    slides = _normalize_slides(data.get("slides"))

    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE home_pages
            SET meta_title = %s, meta_description = %s, slides = %s, updated_at = NOW()
            WHERE store_id = %s""",
            (
                data.get("meta_title"),
                data.get("meta_description"),
                json.dumps(slides),
                store_id,
            ),
        )
    return jsonify({"success": True})


# ── AI generate carousel ───────────────────────────────────────────

@homepage_bp.route("/api/home-page/generate", methods=["POST"])
def generate_home_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    topic = (data.get("topic") or "").strip()
    keywords = data.get("keywords") or []
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input, created_at)
            VALUES (%s, 'home_page_generate', 'pending', %s, NOW()) RETURNING id""",
            (store_id, json.dumps({"topic": topic, "keywords": keywords})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        from app.ai_agent import run_task
        try:
            result = run_task("home_page_generate", {"topic": topic, "keywords": keywords, "store_id": store_id}, store_id=store_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("Home page generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    spawn_task("ai_tasks", task_id, _run)
    return jsonify({"task_id": task_id})


# ── Real store images (for product/collection slides) ──────────────

@homepage_bp.route("/api/home-page/images")
def home_page_images():
    store_id = request.args.get("store_id", type=int)
    source = request.args.get("source", "product")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    images = []
    if source == "collection":
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT title, handle, image_url FROM synced_collections
                WHERE store_id = %s AND image_url IS NOT NULL AND image_url != ''
                ORDER BY products_count DESC NULLS LAST LIMIT 40""",
                (store_id,),
            )
            for r in cur.fetchall():
                images.append({
                    "url": r["image_url"],
                    "alt": r["title"] or "",
                    "cta_text": "Shop " + (r["title"] or "Collection"),
                    "cta_url": f"/collections/{r['handle']}" if r["handle"] else "/collections/all",
                })
    else:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT title, handle, images FROM synced_products
                WHERE store_id = %s AND images IS NOT NULL AND status = 'ACTIVE' LIMIT 60""",
                (store_id,),
            )
            for r in cur.fetchall():
                imgs = r["images"]
                if isinstance(imgs, str):
                    imgs = json.loads(imgs)
                if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
                    url = imgs[0].get("url", "")
                    if url:
                        images.append({
                            "url": url,
                            "alt": r["title"] or "",
                            "cta_text": "Shop Now",
                            "cta_url": f"/products/{r['handle']}" if r["handle"] else "/collections/all",
                        })
    return jsonify({"images": images})


# ── AI image generation for one slide ──────────────────────────────

@homepage_bp.route("/api/home-page/generate-image", methods=["POST"])
def generate_slide_image():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    slide_index = data.get("slide_index", 0)
    prompt = (data.get("prompt") or "").strip()
    headline = (data.get("headline") or "").strip()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input, created_at)
            VALUES (%s, 'home_carousel_image', 'pending', %s, NOW()) RETURNING id""",
            (store_id, json.dumps({"slide_index": slide_index})),
        )
        task_id = cur.fetchone()["id"]

    # Gather real store categories + product image references for the prompt.
    store_categories = []
    product_image_urls = []
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title FROM synced_collections WHERE store_id = %s ORDER BY title LIMIT 12",
                (store_id,),
            )
            store_categories = [r["title"] for r in cur.fetchall()]
            cur.execute(
                "SELECT images FROM synced_products WHERE store_id = %s AND images IS NOT NULL ORDER BY RANDOM() LIMIT 5",
                (store_id,),
            )
            for row in cur.fetchall():
                imgs = row["images"]
                if isinstance(imgs, str):
                    imgs = json.loads(imgs)
                if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
                    url = imgs[0].get("url", "")
                    if url:
                        product_image_urls.append(url)
    except Exception:
        pass

    title = headline or (store.get("name") or "Home")
    description = prompt or None

    def _run():
        try:
            from app.services.image_generator import generate_landing_hero_image_ai
            result = generate_landing_hero_image_ai(
                title,
                page_description=description,
                keywords=store_categories or None,
                product_image_urls=product_image_urls or None,
            )
            if not result:
                raise Exception("Image generation returned no result")
            img_bytes, filename, alt_text, _w, _h = result
            mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
            staged = client.staged_upload_create(filename, mime, len(img_bytes), resource="IMAGE")
            resource_url = client.upload_staged_file(staged, img_bytes, content_type=mime)
            file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
            file_id = file_obj.get("id")
            if not file_id:
                raise Exception("fileCreate did not return a file ID")
            cdn_url = client.file_get_url(file_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps({"image_url": cdn_url, "alt_text": alt_text, "slide_index": slide_index}), task_id),
                )
        except Exception as e:
            logger.exception("Home carousel image generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    spawn_task("ai_tasks", task_id, _run)
    return jsonify({"task_id": task_id})


# ── Preview ────────────────────────────────────────────────────────

@homepage_bp.route("/api/home-page/preview")
def preview_home_page():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    store, _client = _get_store_client(store_id)
    if not store:
        return jsonify({"error": "Store not found"}), 404
    home = _get_or_create_home_page(store_id)
    home["slides"] = _normalize_slides(home.get("slides"))
    html = render_carousel_body(dict(home), dict(store))
    return jsonify({"html": html})


# ── Publish / Unpublish (theme injection) ──────────────────────────

@homepage_bp.route("/api/home-page/publish", methods=["POST"])
def publish_home_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    home = _get_or_create_home_page(store_id)
    home["slides"] = _normalize_slides(home.get("slides"))
    if not home["slides"]:
        return jsonify({"error": "Add at least one slide before publishing"}), 400

    section_key = home.get("section_key") or "ai_home_carousel"

    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "Could not find the store's main theme"}), 400

        current_index = client.get_theme_asset(theme_id, INDEX_TEMPLATE) or ""

        # Persist the pristine index.json backup BEFORE any theme write — a
        # partial failure after injection must never poison the next snapshot.
        # Never snapshot an index that already contains our section.
        if not home.get("index_json_backup"):
            if section_key in current_index:
                logger.warning(
                    "index.json already contains section '%s' and no pristine backup exists — skipping snapshot",
                    section_key,
                )
            else:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE home_pages SET index_json_backup = %s WHERE store_id = %s",
                        (current_index, store_id),
                    )

        section_liquid = render_home_carousel_section(dict(home), dict(store))
        client.upsert_theme_asset(theme_id, SECTION_FILENAME, section_liquid)

        new_index = inject_section_into_index(current_index, section_key)
        client.upsert_theme_asset(theme_id, INDEX_TEMPLATE, new_index)

        # Home-page SEO metafields (shop-level title_tag / description_tag).
        if home.get("meta_title") or home.get("meta_description"):
            client.update_homepage_seo(
                seo_title=home.get("meta_title") or None,
                seo_description=home.get("meta_description") or None,
            )

        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE home_pages
                SET is_published = TRUE, last_published_at = NOW()
                WHERE store_id = %s""",
                (store_id,),
            )
        host = (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/")
        if host:
            from app.services.indexnow_service import ping_async

            ping_async(int(store_id), [f"https://{host}/"], "homepage_publish")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Home page publish failed for store %s", store_id)
        return jsonify({"error": str(e)}), 500


@homepage_bp.route("/api/home-page/unpublish", methods=["POST"])
def unpublish_home_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    home = _get_or_create_home_page(store_id)
    section_key = home.get("section_key") or "ai_home_carousel"

    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "Could not find the store's main theme"}), 400

        current_index = client.get_theme_asset(theme_id, INDEX_TEMPLATE) or ""
        new_index = remove_section_from_index(current_index, section_key)
        client.upsert_theme_asset(theme_id, INDEX_TEMPLATE, new_index)
        client.delete_theme_asset(theme_id, SECTION_FILENAME)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE home_pages SET is_published = FALSE WHERE store_id = %s",
                (store_id,),
            )
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Home page unpublish failed for store %s", store_id)
        return jsonify({"error": str(e)}), 500
