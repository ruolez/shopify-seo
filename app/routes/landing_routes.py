import json
import logging
import threading
import uuid

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services.ai_output_guard import clamp_meta_description, clamp_meta_title, guard_shopify_fields
from app.services.landing_page_seo_analyzer import analyze_synced_page, analyze_landing_page, extract_page_content
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

landing_bp = Blueprint("landing", __name__)

MAX_VERSIONS_PER_PAGE = 20


def _create_version(page_id, label):
    """Snapshot the current state of a landing page as a version."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return None
    sections = page.get("sections") or []
    if isinstance(sections, str):
        sections = json.loads(sections)
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO landing_page_versions
            (landing_page_id, label, title, handle, meta_title, meta_description,
             sections, schema_json, last_generated_html)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                page_id, label, page.get("title"), page.get("handle"),
                page.get("meta_title"), page.get("meta_description"),
                json.dumps(sections), json.dumps(page.get("schema_json")),
                page.get("last_generated_html"),
            ),
        )
        version_id = cur.fetchone()["id"]
        # Prune old versions
        cur.execute(
            """DELETE FROM landing_page_versions WHERE id IN (
                SELECT id FROM landing_page_versions
                WHERE landing_page_id = %s
                ORDER BY created_at DESC
                OFFSET %s
            )""",
            (page_id, MAX_VERSIONS_PER_PAGE),
        )
    return version_id


def _get_store_client(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None, None
    access_token = decrypt_value(store["access_token_enc"])
    client = ShopifyClient(store["domain"], access_token, store["api_version"])
    return store, client


# ── Page Render ────────────────────────────────────────────────

@landing_bp.route("/landing-pages")
def landing_pages_page():
    return render_template("landing_pages.html")


# ── Sync ───────────────────────────────────────────────────────

@landing_bp.route("/api/landing-pages/sync", methods=["POST"])
def sync_pages():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    store, client = _get_store_client(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO sync_jobs (store_id, status, started_at)
            VALUES (%s, 'syncing_pages', NOW()) RETURNING id""",
            (store_id,),
        )
        job_id = cur.fetchone()["id"]

    def _sync():
        try:
            pages = client.get_all_pages()

            # Fetch homepage (shop-level SEO + structured theme sections)
            try:
                homepage = client.get_homepage_data()
                homepage_entry = {
                    "shopify_id": homepage["shop_id"] + "/homepage",
                    "title": homepage["shop_name"] + " — Homepage",
                    "handle": "",
                    "body_html": homepage.get("body_html", ""),
                    "seo_title": homepage["seo_title"],
                    "seo_description": homepage["seo_description"],
                    "is_published": True,
                    "published_at": None,
                    "template_suffix": "",
                    "theme_sections": homepage.get("theme_sections"),
                    "menus": homepage.get("menus"),
                }
                pages.insert(0, homepage_entry)
            except Exception as e:
                logger.warning("Could not fetch homepage data: %s", e)

            for page in pages:
                seo_result = analyze_synced_page(page)
                is_homepage = (page.get("handle") or "") in ("", "index", "frontpage") or "/homepage" in (page.get("shopify_id") or "")
                theme_sections = page.get("theme_sections")
                menus = page.get("menus")

                with db.get_cursor() as cur:
                    cur.execute(
                        """INSERT INTO synced_pages
                        (store_id, shopify_id, title, handle, body_html, seo_title, seo_description,
                         is_published, published_at, template_suffix, seo_score, seo_breakdown,
                         is_homepage, theme_sections, menus, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                            title = EXCLUDED.title, handle = EXCLUDED.handle,
                            body_html = EXCLUDED.body_html, seo_title = EXCLUDED.seo_title,
                            seo_description = EXCLUDED.seo_description,
                            is_published = EXCLUDED.is_published, published_at = EXCLUDED.published_at,
                            template_suffix = EXCLUDED.template_suffix,
                            seo_score = EXCLUDED.seo_score, seo_breakdown = EXCLUDED.seo_breakdown,
                            is_homepage = EXCLUDED.is_homepage,
                            theme_sections = EXCLUDED.theme_sections, menus = EXCLUDED.menus,
                            updated_at = NOW()""",
                        (
                            store_id, page["shopify_id"], page["title"], page["handle"],
                            page["body_html"], page["seo_title"], page["seo_description"],
                            page["is_published"], page.get("published_at"),
                            page.get("template_suffix", ""), seo_result["score"],
                            json.dumps(seo_result["breakdown"]),
                            is_homepage,
                            json.dumps(theme_sections) if theme_sections else None,
                            json.dumps(menus) if menus else None,
                        ),
                    )

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET status = 'completed', completed_at = NOW() WHERE id = %s",
                    (job_id,),
                )
            logger.info("Synced %d pages (incl. homepage) for store %s", len(pages), store_id)
        except Exception as e:
            logger.exception("Page sync failed for store %s: %s", store_id, e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE sync_jobs SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), job_id),
                )

    threading.Thread(target=_sync, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "started"})


# ── Synced Pages ───────────────────────────────────────────────

@landing_bp.route("/api/landing-pages/synced")
def list_synced_pages():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, store_id, shopify_id, title, handle, seo_title, seo_description,
                      is_published, published_at, template_suffix, seo_score, seo_breakdown,
                      is_homepage, created_at, updated_at
            FROM synced_pages WHERE store_id = %s
            ORDER BY is_homepage DESC, title""",
            (store_id,),
        )
        pages = cur.fetchall()
    return jsonify([dict(p) for p in pages])


@landing_bp.route("/api/landing-pages/synced/<int:page_id>/seo")
def synced_page_seo(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404
    result = analyze_synced_page(dict(page))
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE synced_pages SET seo_score = %s, seo_breakdown = %s WHERE id = %s",
            (result["score"], json.dumps(result["breakdown"]), page_id),
        )
    return jsonify(result)


@landing_bp.route("/api/landing-pages/synced/<int:page_id>/ai-optimize", methods=["POST"])
def ai_optimize_synced(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input, created_at)
            VALUES (%s, 'landing_page_seo', 'pending', %s, NOW()) RETURNING id""",
            (page["store_id"], json.dumps({"synced_page_id": page_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            from app.ai_agent import run_task
            result = run_task("landing_page_seo", {"synced_page_id": page_id}, store_id=page["store_id"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("AI optimize synced page failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@landing_bp.route("/api/landing-pages/synced/<int:page_id>/ai-apply", methods=["POST"])
def ai_apply_synced(page_id):
    data = request.get_json(silent=True) or {}
    guard_shopify_fields(data)
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    store, client = _get_store_client(page["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    try:
        is_homepage = page.get("is_homepage", False)

        if is_homepage:
            # Homepage SEO is stored on the Shop object, not a Page
            client.update_homepage_seo(
                seo_title=data.get("meta_title"),
                seo_description=data.get("meta_description"),
            )
        else:
            fields = {}
            if data.get("meta_title"):
                fields["seo_title"] = data["meta_title"]
            if data.get("meta_description"):
                fields["seo_description"] = data["meta_description"]
            if data.get("body_html"):
                fields["body"] = data["body_html"]
            if data.get("title"):
                fields["title"] = data["title"]
            update_id = page.get("shopify_page_id") or page["shopify_id"]
            if not update_id.startswith("gid://shopify/Page/"):
                return jsonify({"error": "Cannot update this page type on Shopify (homepage content is theme-controlled)"}), 400
            from app.services.apply_pipeline import ApplyRefused, apply_item_changes, refusal_response

            try:
                apply_item_changes(
                    page["store_id"], "page", page_id,
                    {"title": data.get("title") or None, "body": data.get("body_html") or None,
                     "meta_title": data.get("meta_title") or None,
                     "meta_description": data.get("meta_description") or None},
                    source="landing_pages", change_type=data.get("change_type") or "page_optimize",
                    client=client, allow_dropped_blocks=bool(data.get("force")),
                )
            except ApplyRefused as refused:
                return refusal_response(refused)

        with db.get_cursor() as cur:
            if data.get("meta_title"):
                cur.execute("UPDATE synced_pages SET seo_title = %s WHERE id = %s", (data["meta_title"], page_id))
            if data.get("meta_description"):
                cur.execute("UPDATE synced_pages SET seo_description = %s WHERE id = %s", (data["meta_description"], page_id))
            if data.get("body_html") and not is_homepage:
                cur.execute("UPDATE synced_pages SET body_html = %s WHERE id = %s", (data["body_html"], page_id))
            if data.get("title") and not is_homepage:
                cur.execute("UPDATE synced_pages SET title = %s WHERE id = %s", (data["title"], page_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Builder Pages (CRUD) ──────────────────────────────────────

@landing_bp.route("/api/landing-pages")
def list_builder_pages():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, store_id, synced_page_id, shopify_page_id, title, handle,
                      meta_title, meta_description, is_published, seo_score, seo_breakdown,
                      last_published_at, created_at, updated_at
            FROM landing_pages WHERE store_id = %s ORDER BY updated_at DESC""",
            (store_id,),
        )
        pages = cur.fetchall()
    return jsonify([dict(p) for p in pages])


@landing_bp.route("/api/landing-pages", methods=["POST"])
def create_builder_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    title = data.get("title")
    if not store_id or not title:
        return jsonify({"error": "store_id and title required"}), 400

    handle = data.get("handle") or title.lower().replace(" ", "-")
    handle = "".join(c for c in handle if c.isalnum() or c == "-").strip("-")
    if not handle:
        return jsonify({"error": "Title must contain letters or numbers (empty URL handle)"}), 400

    import psycopg2 as _pg
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO landing_pages (store_id, title, handle, meta_title, meta_description, sections)
                VALUES (%s, %s, %s, %s, %s, '[]') RETURNING id""",
                (store_id, title, handle, data.get("meta_title", ""), data.get("meta_description", "")),
            )
            page_id = cur.fetchone()["id"]
    except _pg.errors.UniqueViolation:
        return jsonify({"error": f"A landing page with the handle '{handle}' already exists — pick another title"}), 400
    return jsonify({"id": page_id, "title": title, "handle": handle})


@landing_bp.route("/api/landing-pages/<int:page_id>")
def get_builder_page(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404
    result = dict(page)
    if isinstance(result.get("sections"), str):
        result["sections"] = json.loads(result["sections"])
    return jsonify(result)


@landing_bp.route("/api/landing-pages/<int:page_id>", methods=["PUT"])
def update_builder_page(page_id):
    data = request.get_json(silent=True) or {}
    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM landing_pages WHERE id = %s", (page_id,))
        if not cur.fetchone():
            return jsonify({"error": "Page not found"}), 404

    sets = ["updated_at = NOW()"]
    params = []
    for field in ("title", "handle", "meta_title", "meta_description"):
        if field in data:
            sets.append(f"{field} = %s")
            params.append(data[field])
    if "sections" in data:
        sets.append("sections = %s")
        params.append(json.dumps(data["sections"]))
    if "is_published" in data:
        sets.append("is_published = %s")
        params.append(data["is_published"])

    params.append(page_id)
    with db.get_cursor() as cur:
        cur.execute(f"UPDATE landing_pages SET {', '.join(sets)} WHERE id = %s", params)
    return jsonify({"success": True})


@landing_bp.route("/api/landing-pages/<int:page_id>", methods=["DELETE"])
def delete_builder_page(page_id):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM landing_pages WHERE id = %s RETURNING id", (page_id,))
        deleted = cur.fetchone()
    if not deleted:
        return jsonify({"error": "Page not found"}), 404
    return jsonify({"success": True})


# ── Section Management ─────────────────────────────────────────

@landing_bp.route("/api/landing-pages/<int:page_id>/sections", methods=["POST"])
def add_section(page_id):
    data = request.get_json(silent=True) or {}
    section_type = data.get("type")
    if not section_type:
        return jsonify({"error": "type required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT sections FROM landing_pages WHERE id = %s", (page_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Page not found"}), 404

    sections = row["sections"] or []
    if isinstance(sections, str):
        sections = json.loads(sections)

    new_section = {
        "id": str(uuid.uuid4()),
        "type": section_type,
        "order": len(sections),
        "data": data.get("data", {}),
    }
    sections.append(new_section)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET sections = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(sections), page_id),
        )
    return jsonify(new_section)


@landing_bp.route("/api/landing-pages/<int:page_id>/sections/<section_id>", methods=["PUT"])
def update_section(page_id, section_id):
    data = request.get_json(silent=True) or {}
    with db.get_cursor() as cur:
        cur.execute("SELECT sections FROM landing_pages WHERE id = %s", (page_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Page not found"}), 404

    sections = row["sections"] or []
    if isinstance(sections, str):
        sections = json.loads(sections)

    for section in sections:
        if section.get("id") == section_id:
            section["data"] = data.get("data", section.get("data", {}))
            break
    else:
        return jsonify({"error": "Section not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET sections = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(sections), page_id),
        )
    return jsonify({"success": True})


@landing_bp.route("/api/landing-pages/<int:page_id>/sections/<section_id>", methods=["DELETE"])
def delete_section(page_id, section_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT sections FROM landing_pages WHERE id = %s", (page_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Page not found"}), 404

    sections = row["sections"] or []
    if isinstance(sections, str):
        sections = json.loads(sections)

    sections = [s for s in sections if s.get("id") != section_id]
    for i, s in enumerate(sections):
        s["order"] = i

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET sections = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(sections), page_id),
        )
    return jsonify({"success": True})


@landing_bp.route("/api/landing-pages/<int:page_id>/sections/reorder", methods=["POST"])
def reorder_sections(page_id):
    data = request.get_json(silent=True) or {}
    order = data.get("order", [])

    with db.get_cursor() as cur:
        cur.execute("SELECT sections FROM landing_pages WHERE id = %s", (page_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Page not found"}), 404

    sections = row["sections"] or []
    if isinstance(sections, str):
        sections = json.loads(sections)

    section_map = {s["id"]: s for s in sections}
    reordered = []
    for i, sid in enumerate(order):
        if sid in section_map:
            section_map[sid]["order"] = i
            reordered.append(section_map[sid])

    # Sections missing from the submitted order (e.g. added by a concurrent
    # autosave) are appended, not silently deleted.
    ordered_ids = set(order)
    for s in sections:
        if s["id"] not in ordered_ids:
            s["order"] = len(reordered)
            reordered.append(s)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET sections = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(reordered), page_id),
        )
    return jsonify({"success": True})


# ── SEO + Preview + Publish ────────────────────────────────────

@landing_bp.route("/api/landing-pages/<int:page_id>/seo")
def builder_page_seo(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    page_data = dict(page)
    if not page_data.get("last_generated_html"):
        from app.services.landing_page_generator import generate_landing_page_html
        page_data["last_generated_html"] = generate_landing_page_html(page_data)

    result = analyze_landing_page(page_data)
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET seo_score = %s, seo_breakdown = %s WHERE id = %s",
            (result["score"], json.dumps(result["breakdown"]), page_id),
        )
    return jsonify(result)


@landing_bp.route("/api/landing-pages/<int:page_id>/preview")
def preview_page(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    from app.services.landing_page_generator import generate_landing_page_html
    page_data = dict(page)
    html_content = generate_landing_page_html(page_data)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET last_generated_html = %s WHERE id = %s",
            (html_content, page_id),
        )
    return jsonify({"html": html_content})


@landing_bp.route("/api/landing-pages/<int:page_id>/generate-schema", methods=["POST"])
def generate_schema(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT lp.*, s.primary_domain, s.domain FROM landing_pages lp JOIN stores s ON s.id = lp.store_id WHERE lp.id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    from app.services.landing_page_generator import generate_schema_json
    store_domain = page.get("primary_domain") or page.get("domain") or ""
    schemas = generate_schema_json(dict(page), store_domain)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET schema_json = %s WHERE id = %s",
            (json.dumps(schemas), page_id),
        )
    return jsonify({"schemas": schemas})


@landing_bp.route("/api/landing-pages/<int:page_id>/publish", methods=["POST"])
def publish_page(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    _create_version(page_id, "Before publish")

    store, client = _get_store_client(page["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    from app.services.landing_page_generator import generate_landing_page_html, generate_schema_json
    page_data = dict(page)
    html_content = generate_landing_page_html(page_data)

    store_domain = store.get("primary_domain") or store.get("domain") or ""
    schemas = generate_schema_json(page_data, store_domain)
    schema_script = ""
    if schemas:
        schema_script = f'\n<script type="application/ld+json">{json.dumps(schemas)}</script>'
    full_html = html_content + schema_script

    try:
        shopify_page_id = page.get("shopify_page_id") or ""
        is_valid_page_gid = shopify_page_id.startswith("gid://shopify/Page/")

        # Clamp before the live push — nothing upstream guarantees these.
        seo_title = clamp_meta_title(page.get("meta_title") or "")
        seo_description = clamp_meta_description(page.get("meta_description") or "")

        if is_valid_page_gid:
            client.update_page(shopify_page_id, {
                "title": page["title"],
                "body": full_html,
                "is_published": True,
                "seo_title": seo_title,
                "seo_description": seo_description,
            })
        else:
            result = client.create_page(
                title=page["title"],
                body_html=full_html,
                handle=page.get("handle"),
                is_published=True,
                seo_title=seo_title,
                seo_description=seo_description,
            )
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE landing_pages SET shopify_page_id = %s WHERE id = %s",
                    (result["shopify_id"], page_id),
                )

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE landing_pages SET is_published = TRUE, last_published_at = NOW(), last_generated_html = %s, schema_json = %s WHERE id = %s",
                (full_html, json.dumps(schemas), page_id),
            )
        if store_domain and page.get("handle"):
            from app.services.indexnow_service import ping_async

            host = store_domain.replace("https://", "").replace("http://", "").strip("/")
            ping_async(page["store_id"], [f"https://{host}/pages/{page['handle']}"], "landing_publish")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Publish failed for page %s", page_id)
        return jsonify({"error": str(e)}), 500


@landing_bp.route("/api/landing-pages/<int:page_id>/unpublish", methods=["POST"])
def unpublish_page(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    shopify_page_id = page.get("shopify_page_id") or ""
    if shopify_page_id.startswith("gid://shopify/Page/"):
        store, client = _get_store_client(page["store_id"])
        if not client:
            return jsonify({"error": "Store connection unavailable — page NOT unpublished"}), 502
        try:
            client.update_page(shopify_page_id, {"is_published": False})
        except Exception as e:
            if not any(t in str(e).lower() for t in ("does not exist", "not found", "invalid id", "no longer exists")):
                logger.warning("Failed to unpublish on Shopify: %s", e)
                return jsonify({"error": f"Shopify unpublish failed: {e} — the page is still live"}), 502
            logger.info("Page already gone on Shopify: %s", shopify_page_id)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE landing_pages SET is_published = FALSE WHERE id = %s",
            (page_id,),
        )
    return jsonify({"success": True})


# ── Version History ───────────────────────────────────────────

@landing_bp.route("/api/landing-pages/<int:page_id>/versions")
def list_versions(page_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, label, created_at FROM landing_page_versions
            WHERE landing_page_id = %s ORDER BY created_at DESC""",
            (page_id,),
        )
        versions = cur.fetchall()
    return jsonify([dict(v) for v in versions])


@landing_bp.route("/api/landing-pages/<int:page_id>/versions", methods=["POST"])
def save_version(page_id):
    data = request.get_json(silent=True) or {}
    label = data.get("label", "Manual save")
    vid = _create_version(page_id, label)
    if not vid:
        return jsonify({"error": "Page not found"}), 404
    return jsonify({"id": vid, "label": label})


@landing_bp.route("/api/landing-pages/<int:page_id>/versions/<int:version_id>/restore", methods=["POST"])
def restore_version(page_id, version_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM landing_page_versions WHERE id = %s AND landing_page_id = %s",
            (version_id, page_id),
        )
        version = cur.fetchone()
    if not version:
        return jsonify({"error": "Version not found"}), 404

    _create_version(page_id, "Before restore")

    sections = version.get("sections") or []
    if isinstance(sections, str):
        sections = json.loads(sections)

    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE landing_pages SET
                title = %s, handle = %s, meta_title = %s, meta_description = %s,
                sections = %s, schema_json = %s, last_generated_html = %s,
                updated_at = NOW()
            WHERE id = %s""",
            (
                version["title"], version["handle"],
                version["meta_title"], version["meta_description"],
                json.dumps(sections), json.dumps(version.get("schema_json")),
                version.get("last_generated_html"),
                page_id,
            ),
        )
    return jsonify({"success": True})


@landing_bp.route("/api/landing-pages/<int:page_id>/versions/<int:version_id>", methods=["DELETE"])
def delete_version(page_id, version_id):
    with db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM landing_page_versions WHERE id = %s AND landing_page_id = %s RETURNING id",
            (version_id, page_id),
        )
        deleted = cur.fetchone()
    if not deleted:
        return jsonify({"error": "Version not found"}), 404
    return jsonify({"success": True})


# ── AI Endpoints ───────────────────────────────────────────────

@landing_bp.route("/api/landing-pages/<int:page_id>/ai-optimize", methods=["POST"])
def ai_optimize_builder(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    _create_version(page_id, "Before AI optimize")

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input, created_at)
            VALUES (%s, 'landing_page_seo', 'pending', %s, NOW()) RETURNING id""",
            (page["store_id"], json.dumps({"landing_page_id": page_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            from app.ai_agent import run_task
            result = run_task("landing_page_seo", {"landing_page_id": page_id}, store_id=page["store_id"])
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("AI optimize builder page failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@landing_bp.route("/api/landing-pages/ai-generate", methods=["POST"])
def ai_generate_page():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    topic = data.get("topic")
    keywords = data.get("keywords", [])
    if not store_id or not topic:
        return jsonify({"error": "store_id and topic required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input, created_at)
            VALUES (%s, 'landing_page_generate', 'pending', %s, NOW()) RETURNING id""",
            (store_id, json.dumps({"topic": topic, "keywords": keywords, "store_id": store_id})),
        )
        task_id = cur.fetchone()["id"]

    def _run():
        try:
            from app.ai_agent import run_task
            result = run_task("landing_page_generate", {"topic": topic, "keywords": keywords, "store_id": store_id}, store_id=store_id)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                    (json.dumps(result), task_id),
                )
        except Exception as e:
            logger.exception("AI generate landing page failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


# ── Homepage Section Mapping ───────────────────────────────────

def _map_homepage_sections(theme_sections, page):
    """Map Shopify theme sections from index.json to builder section types."""
    import re as _re
    import html as _html

    sections = []
    order = 0

    for ts in theme_sections:
        stype = ts.get("type", "")
        settings = ts.get("settings", {})
        blocks = ts.get("blocks", {})
        block_order = ts.get("block_order", list(blocks.keys()))
        mapped = None

        if stype == "custom-liquid":
            liquid = settings.get("custom_liquid", "")
            h1_match = _re.search(r'<h1[^>]*>(.*?)</h1>', liquid, _re.IGNORECASE | _re.DOTALL)
            if h1_match:
                headline = _re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
                remaining = liquid[h1_match.end():]
                subheadline = _re.sub(r'<[^>]+>', '', remaining).strip()
                mapped = {
                    "type": "hero",
                    "data": {
                        "slides": [{
                            "headline": headline,
                            "subheadline": subheadline,
                            "cta_text": "Shop Now",
                            "cta_url": "/collections/all",
                        }],
                        "categories": _extract_categories_from_menus(page),
                    },
                }
            else:
                text = _re.sub(r'<[^>]+>', '', liquid).strip()
                if text:
                    mapped = {
                        "type": "rich_text",
                        "data": {"heading": "", "content": liquid},
                    }

        elif stype == "featured-collection":
            title = settings.get("title", "")
            coll_handle = settings.get("collection", "")
            products = _get_collection_products(page.get("store_id"), coll_handle)
            mapped = {
                "type": "product_showcase",
                "data": {
                    "heading": title,
                    "products": products,
                },
            }

        elif stype == "featured-collections":
            heading = settings.get("heading", "")
            products = []
            for bkey in block_order:
                block = blocks.get(bkey, {})
                if block.get("type") == "collection":
                    coll_handle = block.get("settings", {}).get("collection", "")
                    if coll_handle:
                        products.append({
                            "title": coll_handle.replace("-", " ").title(),
                            "handle": coll_handle,
                            "image_url": "",
                        })
            mapped = {
                "type": "product_showcase",
                "data": {
                    "heading": heading,
                    "products": products,
                },
            }

        elif stype == "logo-list":
            title = settings.get("title", "")
            items = []
            for bkey in block_order:
                block = blocks.get(bkey, {})
                bs = block.get("settings", {})
                if bs.get("title"):
                    items.append({
                        "icon": "",
                        "text": bs["title"],
                    })
            mapped = {
                "type": "trust_badges",
                "data": {"heading": title, "items": items},
            }

        elif stype == "text-with-icons":
            items = []
            for bkey in block_order:
                block = blocks.get(bkey, {})
                bs = block.get("settings", {})
                if bs.get("title"):
                    items.append({
                        "icon": bs.get("icon", ""),
                        "text": bs["title"],
                    })
            mapped = {
                "type": "trust_badges",
                "data": {"heading": "", "items": items},
            }

        elif stype == "newsletter":
            heading = settings.get("heading", "")
            subtext = _re.sub(r'<[^>]+>', '', settings.get("subtext", "")).strip()
            mapped = {
                "type": "cta_banner",
                "data": {
                    "heading": heading,
                    "subtext": subtext,
                    "cta_text": "Subscribe",
                    "cta_url": "#",
                },
            }

        elif stype == "image-banner":
            heading = settings.get("heading", "")
            subheading = settings.get("subheading", "")
            mapped = {
                "type": "hero",
                "data": {
                    "slides": [{
                        "headline": heading,
                        "subheadline": subheading,
                        "cta_text": settings.get("button_label", "Shop Now"),
                        "cta_url": settings.get("button_link", "/collections/all"),
                        "image_url": settings.get("image", ""),
                    }],
                    "categories": _extract_categories_from_menus(page),
                },
            }

        elif stype == "slideshow":
            slides = []
            for bkey in block_order:
                block = blocks.get(bkey, {})
                bs = block.get("settings", {})
                if block.get("type") == "slide":
                    slides.append({
                        "headline": bs.get("heading", ""),
                        "subheadline": bs.get("subheading", ""),
                        "cta_text": bs.get("button_label", ""),
                        "cta_url": bs.get("button_link", ""),
                        "image_url": bs.get("image", ""),
                    })
            if not slides:
                slides = [{"headline": settings.get("heading", ""), "subheadline": "", "cta_text": "Shop Now", "cta_url": "/collections/all"}]
            mapped = {
                "type": "hero",
                "data": {
                    "slides": slides,
                    "categories": _extract_categories_from_menus(page),
                },
            }

        elif stype in ("rich-text", "rich_text"):
            heading = settings.get("heading", "")
            content_parts = []
            for bkey in block_order:
                block = blocks.get(bkey, {})
                bs = block.get("settings", {})
                if bs.get("text"):
                    content_parts.append(bs["text"])
            mapped = {
                "type": "rich_text",
                "data": {
                    "heading": heading,
                    "content": "\n".join(content_parts),
                },
            }

        if mapped:
            mapped["id"] = str(uuid.uuid4())
            mapped["order"] = order
            sections.append(mapped)
            order += 1

    if not sections:
        sections = [{
            "id": str(uuid.uuid4()),
            "type": "hero",
            "order": 0,
            "data": {
                "slides": [{
                    "headline": page.get("seo_title") or page.get("title", ""),
                    "subheadline": page.get("seo_description") or "",
                    "cta_text": "Shop Now",
                    "cta_url": "/collections/all",
                }],
                "categories": _extract_categories_from_menus(page),
            },
        }]

    return sections


def _extract_categories_from_menus(page):
    """Extract top-level menu items as category badges from synced menus data."""
    menus = page.get("menus") or {}
    if isinstance(menus, str):
        try:
            menus = json.loads(menus)
        except (json.JSONDecodeError, TypeError):
            menus = {}
    if not isinstance(menus, dict):
        return []
    # Prefer main-menu, fall back to any menu
    main = menus.get("main-menu") or menus.get("main_menu") or {}
    if not main:
        for v in menus.values():
            if isinstance(v, dict) and v.get("items"):
                main = v
                break
    categories = []
    for item in (main.get("items") or []):
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        url = item.get("url", "")
        if title and "/collections/" in url:
            categories.append({"name": title, "url": url})
    return categories[:6]


def _get_collection_products(store_id, collection_handle):
    """Look up products for a collection handle from already-synced data."""
    if not store_id or not collection_handle:
        return []
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT sp.title, sp.handle, sp.images
                FROM synced_products sp
                JOIN collection_product_map cpm ON cpm.product_shopify_id = sp.shopify_id AND cpm.store_id = sp.store_id
                JOIN synced_collections sc ON sc.shopify_id = cpm.collection_shopify_id AND sc.store_id = cpm.store_id
                WHERE sc.handle = %s AND sc.store_id = %s
                LIMIT 8""",
                (collection_handle, store_id),
            )
            rows = cur.fetchall()
        products = []
        for r in rows:
            images = r.get("images") or []
            if isinstance(images, str):
                images = json.loads(images)
            image_url = images[0].get("url", "") if images else ""
            products.append({
                "title": r["title"],
                "handle": r["handle"],
                "image_url": image_url,
            })
        return products
    except Exception:
        return []


# ── Import Synced → Builder ────────────────────────────────────

@landing_bp.route("/api/landing-pages/import", methods=["POST"])
def import_to_builder():
    data = request.get_json(silent=True) or {}
    synced_page_id = data.get("synced_page_id")
    if not synced_page_id:
        return jsonify({"error": "synced_page_id required"}), 400

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM synced_pages WHERE id = %s", (synced_page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Synced page not found"}), 404

    body_html = page["body_html"] or ""
    is_homepage = page.get("is_homepage", False) or (page.get("handle") or "") in ("", "index", "frontpage")

    sections = []
    if is_homepage:
        theme_sections = page.get("theme_sections")
        if isinstance(theme_sections, str):
            theme_sections = json.loads(theme_sections)

        if theme_sections:
            sections = _map_homepage_sections(theme_sections, page)
        else:
            sections = [{
                "id": str(uuid.uuid4()),
                "type": "hero",
                "order": 0,
                "data": {
                    "headline": page.get("seo_title") or page["title"],
                    "subheadline": page.get("seo_description") or "",
                    "cta_text": "Shop Now",
                    "cta_url": "/collections/all",
                },
            }, {
                "id": str(uuid.uuid4()),
                "type": "rich_text",
                "order": 1,
                "data": {"heading": "", "content": ""},
            }]
    else:
        sections = [{
            "id": str(uuid.uuid4()),
            "type": "rich_text",
            "order": 0,
            "data": {
                "heading": page["title"],
                "content": body_html,
            },
        }]

    handle = page["handle"] or page["title"].lower().replace(" ", "-")
    handle = "".join(c for c in handle if c.isalnum() or c == "-")

    # Back up existing builder page before overwrite
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM landing_pages WHERE store_id = %s AND handle = %s",
            (page["store_id"], handle),
        )
        existing = cur.fetchone()
    if existing:
        _create_version(existing["id"], "Before re-import")

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO landing_pages
            (store_id, synced_page_id, shopify_page_id, title, handle,
             meta_title, meta_description, sections, is_published)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (store_id, handle) DO UPDATE SET
                synced_page_id = EXCLUDED.synced_page_id,
                shopify_page_id = EXCLUDED.shopify_page_id,
                title = EXCLUDED.title,
                meta_title = EXCLUDED.meta_title,
                meta_description = EXCLUDED.meta_description,
                sections = EXCLUDED.sections,
                updated_at = NOW()
            RETURNING id""",
            (
                page["store_id"], synced_page_id, page["shopify_id"],
                page["title"], handle,
                page.get("seo_title") or "", page.get("seo_description") or "",
                json.dumps(sections), page["is_published"],
            ),
        )
        new_id = cur.fetchone()["id"]
    return jsonify({"id": new_id, "title": page["title"]})


# ── Hero Image Generation ─────────────────────────────────────

@landing_bp.route("/api/landing-pages/<int:page_id>/generate-hero", methods=["POST"])
def generate_hero_image(page_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
        page = cur.fetchone()
    if not page:
        return jsonify({"error": "Page not found"}), 404

    store, client = _get_store_client(page["store_id"])
    if not client:
        return jsonify({"error": "Store not found"}), 404

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_tasks (store_id, task_type, status, input, created_at)
            VALUES (%s, 'landing_hero_image', 'pending', %s, NOW()) RETURNING id""",
            (page["store_id"], json.dumps({"landing_page_id": page_id})),
        )
        task_id = cur.fetchone()["id"]

    # Gather real store categories for the image prompt
    store_categories = []
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title FROM synced_collections WHERE store_id = %s ORDER BY title LIMIT 12",
                (page["store_id"],),
            )
            store_categories = [r["title"] for r in cur.fetchall()]
    except Exception:
        pass
    # Also try menus from synced homepage
    if not store_categories:
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT menus FROM synced_pages WHERE store_id = %s AND is_homepage = true LIMIT 1",
                    (page["store_id"],),
                )
                row = cur.fetchone()
                if row and row.get("menus"):
                    menus = row["menus"]
                    if isinstance(menus, str):
                        menus = json.loads(menus)
                    if isinstance(menus, dict):
                        main = menus.get("main-menu") or menus.get("main_menu") or {}
                        for item in (main.get("items") or []):
                            if isinstance(item, dict) and item.get("title"):
                                store_categories.append(item["title"])
        except Exception:
            pass

    # Gather real product image URLs — one per collection for diversity
    product_image_urls = []
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ON (sc.title) sp.images
                FROM synced_products sp
                JOIN collection_product_map cpm ON cpm.product_shopify_id = sp.shopify_id AND cpm.store_id = sp.store_id
                JOIN synced_collections sc ON sc.shopify_id = cpm.collection_shopify_id AND sc.store_id = cpm.store_id
                WHERE sp.store_id = %s AND sp.images IS NOT NULL
                ORDER BY sc.title, RANDOM()
                LIMIT 10""",
                (page["store_id"],),
            )
            for row in cur.fetchall():
                imgs = row.get("images") or []
                if isinstance(imgs, str):
                    imgs = json.loads(imgs)
                if imgs and isinstance(imgs, list):
                    url = imgs[0].get("url", "") if isinstance(imgs[0], dict) else ""
                    if url:
                        product_image_urls.append(url)
                if len(product_image_urls) >= 5:
                    break
    except Exception:
        pass
    # Fallback to random products if no collection mapping
    if not product_image_urls:
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT images FROM synced_products WHERE store_id = %s AND images IS NOT NULL ORDER BY RANDOM() LIMIT 5",
                    (page["store_id"],),
                )
                for row in cur.fetchall():
                    imgs = row.get("images") or []
                    if isinstance(imgs, str):
                        imgs = json.loads(imgs)
                    if imgs and isinstance(imgs, list):
                        url = imgs[0].get("url", "") if isinstance(imgs[0], dict) else ""
                        if url:
                            product_image_urls.append(url)
        except Exception:
            pass

    def _run():
        try:
            from app.services.image_generator import generate_landing_hero_image_ai
            result = generate_landing_hero_image_ai(
                page["title"],
                page_description=page.get("meta_description"),
                keywords=store_categories or None,
                product_image_urls=product_image_urls or None,
            )
            if result:
                img_bytes, filename, alt_text, width, height = result
                mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
                staged = client.staged_upload_create(filename, mime, len(img_bytes), resource="IMAGE")
                resource_url = client.upload_staged_file(staged, img_bytes, content_type=mime, filename=filename)
                file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
                file_id = file_obj.get("id")
                if not file_id:
                    raise Exception("fileCreate did not return a file ID")
                cdn_url = client.file_get_url(file_id)
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                        (json.dumps({"image_url": cdn_url, "alt_text": alt_text, "filename": filename}), task_id),
                    )
            else:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE ai_tasks SET status = 'failed', error = 'Image generation returned no result', completed_at = NOW() WHERE id = %s",
                        (task_id,),
                    )
        except Exception as e:
            logger.exception("Hero image generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (str(e), task_id),
                )

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})
