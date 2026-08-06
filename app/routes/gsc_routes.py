import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from app.services import gsc_service
from app.services.task_runner import is_failed_result

logger = logging.getLogger(__name__)

gsc_bp = Blueprint("gsc", __name__)


def _default_date_range():
    today = datetime.now(timezone.utc).date()
    date_to = (today - timedelta(days=3)).isoformat()
    date_from = (today - timedelta(days=30)).isoformat()
    return date_from, date_to


def _parse_date_range(args):
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    if not date_from or not date_to:
        return _default_date_range()
    return date_from, date_to


def _get_store_id():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        store_id = session.get("active_store_id")
    return store_id


def _get_filters():
    device = request.args.get("device") or None
    country = request.args.get("country") or None
    return {"device": device, "country": country}


# ── HTML Page ──

@gsc_bp.route("/gsc")
def gsc_page():
    return render_template("gsc.html")


# ── OAuth Flow ──

@gsc_bp.route("/api/gsc/auth/start")
def auth_start():
    store_id = request.args.get("store_id", type=int)
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    session["gsc_auth_store_id"] = store_id
    redirect_uri = request.host_url.rstrip("/") + "/api/gsc/auth/callback"

    try:
        auth_url, state = gsc_service.start_oauth_flow(redirect_uri)
        session["gsc_oauth_state"] = state
        return jsonify({"auth_url": auth_url})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@gsc_bp.route("/api/gsc/auth/callback")
def auth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return redirect("/gsc?error=" + error)

    if not code:
        return redirect("/gsc?error=no_code")

    # CSRF protection: the callback must carry the state minted in auth_start.
    expected_state = session.pop("gsc_oauth_state", None)
    if not state or state != expected_state:
        logger.warning("GSC OAuth callback with invalid state (expected=%s)", bool(expected_state))
        return redirect("/gsc?error=invalid_state")

    store_id = session.get("gsc_auth_store_id")
    if not store_id:
        return redirect("/gsc?error=no_store")

    redirect_uri = request.host_url.rstrip("/") + "/api/gsc/auth/callback"

    try:
        token_data = gsc_service.complete_oauth_flow(code, state, redirect_uri)
        gsc_service.save_credentials(store_id, token_data)
        return redirect("/gsc?connected=true&store_id=" + str(store_id))
    except Exception as e:
        return redirect("/gsc?error=" + str(e))


# ── Property Selection ──

@gsc_bp.route("/api/gsc/properties")
def list_properties():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    try:
        props = gsc_service.list_properties(store_id)
        return jsonify(props)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@gsc_bp.route("/api/gsc/properties/select", methods=["POST"])
def select_property():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    data = request.get_json(silent=True) or {}
    site_url = (data.get("site_url") or "").strip()
    if not site_url:
        return jsonify({"error": "site_url required"}), 400

    gsc_service.select_property(store_id, site_url)

    def _backfill():
        try:
            gsc_service.pull_data(store_id, pull_type="backfill")
        except Exception:
            logger.exception("GSC backfill failed for store %s", store_id)

    threading.Thread(target=_backfill, daemon=True).start()

    return jsonify({"ok": True, "property": site_url})


# ── Connection Management ──

@gsc_bp.route("/api/gsc/status")
def connection_status():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    status = gsc_service.get_connection_status(store_id)
    return jsonify(status)


@gsc_bp.route("/api/gsc/auth", methods=["DELETE"])
def disconnect():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    gsc_service.disconnect(store_id)
    return jsonify({"ok": True})


# ── Data Pull ──

@gsc_bp.route("/api/gsc/pull", methods=["POST"])
def trigger_pull():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    data = request.get_json(silent=True) or {}
    pull_type = data.get("type", "incremental")

    def _pull():
        try:
            gsc_service.pull_data(store_id, pull_type=pull_type)
        except Exception:
            logger.exception("GSC pull failed for store %s", store_id)

    threading.Thread(target=_pull, daemon=True).start()
    return jsonify({"ok": True, "message": "Pull started in background"})


@gsc_bp.route("/api/gsc/pull-sync", methods=["POST"])
def trigger_pull_sync():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    data = request.get_json(silent=True) or {}
    pull_type = data.get("type", "incremental")

    try:
        result = gsc_service.pull_data(store_id, pull_type=pull_type)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Dashboard Data Endpoints ──

@gsc_bp.route("/api/gsc/overview")
def overview():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    f = _get_filters()
    data = gsc_service.get_overview(store_id, date_from, date_to, **f)
    return jsonify(data)


@gsc_bp.route("/api/gsc/trends")
def trends():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    f = _get_filters()
    data = gsc_service.get_trends(store_id, date_from, date_to, **f)
    return jsonify(data)


@gsc_bp.route("/api/gsc/top-pages")
def top_pages():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    limit = request.args.get("limit", 500, type=int)
    offset = request.args.get("offset", 0, type=int)
    sort_by = request.args.get("sort_by", "clicks")
    f = _get_filters()
    data = gsc_service.get_top_pages(store_id, date_from, date_to, limit, offset, sort_by, **f)
    return jsonify(data)


@gsc_bp.route("/api/gsc/top-queries")
def top_queries():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    limit = request.args.get("limit", 500, type=int)
    offset = request.args.get("offset", 0, type=int)
    sort_by = request.args.get("sort_by", "clicks")
    f = _get_filters()
    data = gsc_service.get_top_queries(store_id, date_from, date_to, limit, offset, sort_by, **f)
    return jsonify(data)


@gsc_bp.route("/api/gsc/page-detail")
def page_detail():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    page_url = request.args.get("page")
    if not page_url:
        return jsonify({"error": "page parameter required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    f = _get_filters()
    data = gsc_service.get_page_detail(store_id, page_url, date_from, date_to, device=f["device"], country=f["country"])
    return jsonify(data)


@gsc_bp.route("/api/gsc/query-detail")
def query_detail():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    query_text = request.args.get("query")
    if not query_text:
        return jsonify({"error": "query parameter required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    f = _get_filters()
    data = gsc_service.get_query_detail(store_id, query_text, date_from, date_to, device=f["device"], country=f["country"])
    return jsonify(data)


@gsc_bp.route("/api/gsc/opportunities")
def opportunities():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    limit = request.args.get("limit", 200, type=int)
    f = _get_filters()
    data = gsc_service.get_opportunities(store_id, date_from, date_to, limit, **f)
    return jsonify(data)


@gsc_bp.route("/api/gsc/cannibalization")
def cannibalization():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    min_impressions = request.args.get("min_impressions", 3, type=int)
    f = _get_filters()
    data = gsc_service.get_cannibalization(store_id, date_from, date_to, min_impressions, **f)
    return jsonify(data)


@gsc_bp.route("/api/gsc/countries")
def available_countries():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    data = gsc_service.get_available_countries(store_id)
    return jsonify(data)


@gsc_bp.route("/api/gsc/page-matches")
def page_matches():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    f = _get_filters()
    data = gsc_service.get_page_product_matches(store_id, date_from, date_to, **f)
    return jsonify(data)


# ── SEO Score Snapshots & Progress ──

@gsc_bp.route("/api/dashboard/snapshot", methods=["POST"])
def take_snapshot():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    def _snap():
        try:
            gsc_service.take_seo_score_snapshot(store_id)
        except Exception:
            logger.exception("GSC score snapshot failed for store %s", store_id)

    threading.Thread(target=_snap, daemon=True).start()
    return jsonify({"ok": True, "message": "Snapshot started"})


@gsc_bp.route("/api/dashboard/score-trends")
def score_trends():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    date_from, date_to = _parse_date_range(request.args)
    item_type = request.args.get("item_type")
    data = gsc_service.get_score_trends(store_id, date_from, date_to, item_type)
    return jsonify(data)


@gsc_bp.route("/api/dashboard/improvements")
def improvements():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    limit = request.args.get("limit", 20, type=int)
    data = gsc_service.get_score_improvements(store_id, limit)
    return jsonify(data)


# ── Cannibalization AI Analysis & Fix ──

def _get_ai_client():
    from app.auth import decrypt_value
    from app.database import db
    from openai import OpenAI

    with db.get_cursor() as cur:
        cur.execute("SELECT api_key_enc, model FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()

    if not config or not config.get("api_key_enc"):
        return None, None, "AI not configured. Set your OpenRouter API key in Settings."

    api_key = decrypt_value(config["api_key_enc"])
    model = config.get("model") or "anthropic/claude-sonnet-4"
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    return client, model, None


def _ai_json_call(client, model, prompt, max_tokens=1500):
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    content = response.choices[0].message.content or ""
    start = content.find("{")
    end = content.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(content[start:end])
    return {"raw_response": content}


@gsc_bp.route("/api/gsc/cannibalization/analyze", methods=["POST"])
def analyze_cannibalization():
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS

    data = request.get_json(silent=True) or {}
    query = data.get("query")
    pages = data.get("pages", [])
    manual_winner = data.get("manual_winner")
    semrush_volume = data.get("semrush_volume")
    if not query or not pages:
        return jsonify({"error": "query and pages required"}), 400

    client, model, err = _get_ai_client()
    if err:
        return jsonify({"error": err}), 400

    pages_desc = "\n".join([
        f"  - {p['page']} | Clicks: {p['clicks']} | Impressions: {p['impressions']} | Avg Position: {p['position']} | Matched: {p.get('match', {}).get('title', 'Unknown')}"
        for p in pages
    ])

    extra_context = ""
    if semrush_volume:
        extra_context += f"\n\nSEMRUSH DATA: This keyword has {semrush_volume:,} monthly searches. Prioritize your recommendations based on the traffic value at stake."
    if manual_winner:
        extra_context += f"\n\nIMPORTANT: The user has manually designated {manual_winner} as the WINNER page. Do NOT change this decision. Provide recommendations only for the OTHER pages."

    prompt_template = get_setting("prompts.cannibalization_analysis", DEFAULTS["prompts.cannibalization_analysis"])
    prompt = prompt_template.format(query=query, pages_description=pages_desc) + extra_context

    try:
        result = _ai_json_call(client, model, prompt)
        if is_failed_result(result):
            return jsonify({"error": "AI returned an unusable response — try again"}), 502
        return jsonify(result)
    except Exception as e:
        logger.error("Cannibalization AI analysis failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


def _cannibalization_keyword_guard(store_id, item_type, item_id, retarget_kw, ownership=None):
    """Resolver guard for cannibalization retargeting. Returns
    (resolved_primary, conflict_error): conflict_error is set when the
    retarget keyword is already another page's primary — rewriting this page
    onto it would just move the cannibalization, not fix it. Pass a prebuilt
    `ownership` map when calling in a loop."""
    resolved_primary = ""
    try:
        from app.services.keyword_resolver import _norm, get_or_refresh_resolution, get_owned_primary_keywords

        resolution = get_or_refresh_resolution(store_id, item_type, item_id)
        rp = (resolution or {}).get("primary")
        if rp and rp.get("keyword"):
            resolved_primary = rp["keyword"]
        tk = _norm(retarget_kw or "")
        if tk and tk != _norm(resolved_primary):
            if ownership is None:
                ownership = get_owned_primary_keywords(store_id, exclude=(item_type, item_id))
            owner = ownership.get(tk)
            if owner and (owner["item_type"], owner["item_id"]) != (item_type, item_id):
                return resolved_primary, (
                    f'Target keyword "{retarget_kw}" is already the primary keyword of another page '
                    f'({owner["item_type"]} #{owner["item_id"]}'
                    + (", manually assigned" if owner.get("manual") else "")
                    + "). Retargeting this page onto it would create new cannibalization — pick a different keyword."
                )
    except Exception:
        logger.warning("Cannibalization keyword guard failed for %s %s", item_type, item_id)
    return resolved_primary, None


def _resolved_primary_suffix(resolved_primary):
    if not resolved_primary:
        return ""
    return (
        f"\n\nThis page's resolved primary keyword (from the store's keyword strategy) is "
        f"\"{resolved_primary}\". The rewrite must keep the page targeted on it — differentiate "
        f"away from the competing query WITHOUT dropping this primary from the meta title."
    )


@gsc_bp.route("/api/gsc/cannibalization/generate-fix", methods=["POST"])
def generate_cannibalization_fix():
    from app.database import db
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS

    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type")
    item_id = data.get("item_id")
    competing_query = data.get("competing_query", "")
    action = data.get("action", "differentiate")
    reason = data.get("reason", "")
    target_keyword = data.get("target_keyword", "")

    if not item_type or not item_id:
        return jsonify({"error": "item_type and item_id required"}), 400

    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute("SELECT id, store_id, title, handle, meta_title, meta_description, images FROM synced_products WHERE id = %s", (item_id,))
        elif item_type == "collection":
            cur.execute("SELECT id, store_id, title, handle, meta_title, meta_description FROM synced_collections WHERE id = %s", (item_id,))
        else:
            return jsonify({"error": "item_type must be product or collection"}), 400
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    resolved_primary, conflict = _cannibalization_keyword_guard(
        item["store_id"], item_type, item_id, target_keyword or competing_query
    )
    if conflict:
        return jsonify({"error": conflict}), 409

    images = []
    images_context = ""
    if item_type == "product":
        raw_images = item.get("images") or []
        if isinstance(raw_images, str):
            import json as _json
            raw_images = _json.loads(raw_images)
        images = [{"media_id": img.get("media_id", ""), "alt": img.get("alt", ""), "src": img.get("url", "")} for img in raw_images if img.get("media_id")]
        if images:
            alt_lines = "\n".join([f"  - Image {i+1}: alt=\"{img['alt']}\"" for i, img in enumerate(images)])
            images_context = f"\n\nCURRENT IMAGE ALT TEXTS ({len(images)} images):\n{alt_lines}"

    client, model, err = _get_ai_client()
    if err:
        return jsonify({"error": err}), 400

    prompt_template = get_setting("prompts.cannibalization_fix", DEFAULTS["prompts.cannibalization_fix"])
    prompt = prompt_template.format(
        item_type=item_type,
        current_title=item.get("title") or "",
        current_meta_title=item.get("meta_title") or "",
        current_meta_description=item.get("meta_description") or "",
        handle=item.get("handle") or "",
        competing_query=competing_query,
        action=action,
        reason=reason,
        target_keyword=target_keyword or competing_query,
        images_context=images_context,
    )
    prompt += _resolved_primary_suffix(resolved_primary)

    try:
        result = _ai_json_call(client, model, prompt, max_tokens=1200)
        # Must run before the enrichment below — added keys would defeat
        # is_failed_result's bare-raw_response test.
        if is_failed_result(result):
            return jsonify({"error": "AI returned an unusable response — try again"}), 502
        result["current"] = {
            "title": item.get("title") or "",
            "meta_title": item.get("meta_title") or "",
            "meta_description": item.get("meta_description") or "",
            "images": images,
        }
        result["item_type"] = item_type
        result["item_id"] = item_id
        return jsonify(result)
    except Exception as e:
        logger.error("Cannibalization fix generation failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@gsc_bp.route("/api/gsc/cannibalization/apply-fix", methods=["POST"])
def apply_cannibalization_fix():
    from app.auth import decrypt_value
    from app.database import db
    from app.services.ai_output_guard import guard_shopify_fields
    from app.shopify_client import ShopifyClient

    data = request.get_json(silent=True) or {}
    item_type = data.get("item_type")
    item_id = data.get("item_id")
    new_title = data.get("new_title")
    new_meta_title = data.get("new_meta_title")
    new_meta_description = data.get("new_meta_description")
    image_alts = data.get("image_alts", [])
    cannibal_query = data.get("cannibal_query", "")
    cannibal_page = data.get("cannibal_page", "")
    cannibal_action = data.get("cannibal_action", "")
    cannibal_target = data.get("cannibal_target", "")

    if not item_type or not item_id:
        return jsonify({"error": "item_type and item_id required"}), 400

    if item_type not in ("product", "collection"):
        return jsonify({"error": f"Invalid item_type: {item_type}"}), 400
    table = "synced_products" if item_type == "product" else "synced_collections"

    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT t.*, s.domain, s.access_token_enc, s.api_version FROM {table} t JOIN stores s ON s.id = t.store_id WHERE t.id = %s",
            (item_id,),
        )
        item = cur.fetchone()

    if not item:
        return jsonify({"error": f"{item_type} not found"}), 404

    try:
        client = ShopifyClient(
            item["domain"],
            decrypt_value(item["access_token_enc"]),
            item["api_version"],
        )

        fields = {}
        updated_fields = []
        if new_title:
            fields["title"] = new_title
            updated_fields.append("title")
        if new_meta_title:
            fields["meta_title"] = new_meta_title
            updated_fields.append("meta_title")
        if new_meta_description:
            fields["meta_description"] = new_meta_description
            updated_fields.append("meta_description")

        guard_shopify_fields(fields)
        new_title = fields.get("title", new_title)
        new_meta_title = fields.get("meta_title", new_meta_title)
        new_meta_description = fields.get("meta_description", new_meta_description)

        if fields:
            if item_type == "product":
                client.update_product(item["shopify_id"], fields)
            else:
                client.update_collection(item["shopify_id"], fields)

        if image_alts and item_type == "product":
            valid_alts = [a for a in image_alts if a.get("media_id") and a.get("alt")]
            if valid_alts:
                client.update_product_media(item["shopify_id"], valid_alts)
                updated_fields.append("image_alts")

        if not updated_fields:
            return jsonify({"error": "No changes to apply"}), 400

        with db.get_cursor() as cur:
            if new_title:
                cur.execute(f"UPDATE {table} SET title = %s WHERE id = %s", (new_title, item_id))
            if new_meta_title:
                cur.execute(f"UPDATE {table} SET meta_title = %s WHERE id = %s", (new_meta_title, item_id))
            if new_meta_description:
                cur.execute(f"UPDATE {table} SET meta_description = %s WHERE id = %s", (new_meta_description, item_id))
            if image_alts and item_type == "product":
                images = item.get("images") or []
                if isinstance(images, str):
                    images = json.loads(images)
                alt_map = {a["media_id"]: a["alt"] for a in image_alts if a.get("media_id")}
                for img in images:
                    if img.get("media_id") in alt_map:
                        img["alt"] = alt_map[img["media_id"]]
                cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(images), item_id))
            cur.execute(f"UPDATE {table} SET last_optimized_at = NOW() WHERE id = %s", (item_id,))

        from app.services.analysis_refresh import refresh_item_analysis
        refresh_item_analysis(item.get("store_id"), item_type, item_id)

        with db.get_cursor() as cur:
            if cannibal_query and cannibal_page:
                cur.execute(
                    """INSERT INTO cannibalization_fixes
                       (store_id, query, page_url, item_type, item_id, action, target_keyword,
                        old_meta_title, new_meta_title, old_meta_description, new_meta_description,
                        old_title, new_title, image_alts_changed, status, applied_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW())
                       ON CONFLICT (store_id, query, page_url) DO UPDATE
                       SET new_meta_title = EXCLUDED.new_meta_title,
                           new_meta_description = EXCLUDED.new_meta_description,
                           new_title = EXCLUDED.new_title,
                           image_alts_changed = EXCLUDED.image_alts_changed,
                           status = 'pending', applied_at = NOW(), verified_at = NULL""",
                    (item.get("store_id"), cannibal_query, cannibal_page, item_type, item_id,
                     cannibal_action, cannibal_target,
                     item.get("meta_title") or "", new_meta_title or "",
                     item.get("meta_description") or "", new_meta_description or "",
                     item.get("title") or "", new_title or "",
                     len(image_alts)),
                )

        return jsonify({"ok": True, "updated_fields": updated_fields})
    except Exception as e:
        logger.error("Cannibalization fix apply failed: %s", str(e))
        return jsonify({"error": str(e)}), 500


@gsc_bp.route("/api/gsc/cannibalization/fixes")
def list_cannibalization_fixes():
    from app.database import db
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT query, page_url, item_type, action, target_keyword, status, applied_at, verified_at
               FROM cannibalization_fixes WHERE store_id = %s ORDER BY applied_at DESC""",
            (store_id,),
        )
        fixes = cur.fetchall()

    result = {}
    for f in fixes:
        key = f"{f['query']}|{f['page_url']}"
        result[key] = {
            "query": f["query"],
            "page_url": f["page_url"],
            "item_type": f["item_type"],
            "action": f["action"],
            "target_keyword": f["target_keyword"],
            "status": f["status"],
            "applied_at": f["applied_at"].isoformat() if f["applied_at"] else None,
            "verified_at": f["verified_at"].isoformat() if f["verified_at"] else None,
        }
    return jsonify(result)


@gsc_bp.route("/api/gsc/cannibalization/verify", methods=["POST"])
def verify_fixes():
    store_id = _get_store_id()
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    result = gsc_service.verify_cannibalization_fixes(store_id)
    return jsonify(result)


@gsc_bp.route("/api/gsc/cannibalization/bulk-fix", methods=["POST"])
def bulk_cannibalization_fix():
    from app.auth import decrypt_value
    from app.database import db
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS
    from app.services.ai_output_guard import guard_shopify_fields
    from app.shopify_client import ShopifyClient

    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items array required"}), 400

    client_ai, model, err = _get_ai_client()
    if err:
        return jsonify({"error": err}), 400

    ownership_by_store = {}
    results = []
    for item_data in items:
        item_type = item_data.get("item_type")
        item_id = item_data.get("item_id")
        competing_query = item_data.get("competing_query", "")
        action = item_data.get("action", "differentiate")
        reason = item_data.get("reason", "")
        target_keyword = item_data.get("target_keyword", "")
        page_url = item_data.get("page_url", "")

        try:
            if item_type not in ("product", "collection"):
                results.append({"item_id": item_id, "status": "error", "error": f"Invalid item_type: {item_type}"})
                continue
            table = "synced_products" if item_type == "product" else "synced_collections"
            with db.get_cursor() as cur:
                if item_type == "product":
                    cur.execute(f"SELECT t.*, s.domain, s.access_token_enc, s.api_version FROM synced_products t JOIN stores s ON s.id = t.store_id WHERE t.id = %s", (item_id,))
                else:
                    cur.execute(f"SELECT t.*, s.domain, s.access_token_enc, s.api_version FROM synced_collections t JOIN stores s ON s.id = t.store_id WHERE t.id = %s", (item_id,))
                item = cur.fetchone()

            if not item:
                results.append({"item_id": item_id, "status": "error", "error": "Not found"})
                continue

            if item["store_id"] not in ownership_by_store:
                from app.services.keyword_resolver import get_owned_primary_keywords
                ownership_by_store[item["store_id"]] = get_owned_primary_keywords(item["store_id"])
            resolved_primary, conflict = _cannibalization_keyword_guard(
                item["store_id"], item_type, item_id, target_keyword or competing_query,
                ownership=ownership_by_store[item["store_id"]],
            )
            if conflict:
                results.append({"item_id": item_id, "status": "skipped", "error": conflict})
                continue

            images = []
            images_context = ""
            if item_type == "product":
                raw_images = item.get("images") or []
                if isinstance(raw_images, str):
                    raw_images = json.loads(raw_images)
                images = [{"media_id": img.get("media_id", ""), "alt": img.get("alt", "")} for img in raw_images if img.get("media_id")]
                if images:
                    alt_lines = "\n".join([f"  - Image {i+1}: alt=\"{img['alt']}\"" for i, img in enumerate(images)])
                    images_context = f"\n\nCURRENT IMAGE ALT TEXTS ({len(images)} images):\n{alt_lines}"

            prompt_template = get_setting("prompts.cannibalization_fix", DEFAULTS["prompts.cannibalization_fix"])
            prompt = prompt_template.format(
                item_type=item_type,
                current_title=item.get("title") or "",
                current_meta_title=item.get("meta_title") or "",
                current_meta_description=item.get("meta_description") or "",
                handle=item.get("handle") or "",
                competing_query=competing_query,
                action=action,
                reason=reason,
                target_keyword=target_keyword or competing_query,
                images_context=images_context,
            )
            prompt += _resolved_primary_suffix(resolved_primary)

            fix = _ai_json_call(client_ai, model, prompt, max_tokens=1200)
            if is_failed_result(fix):
                results.append({"item_id": item_id, "status": "error", "error": "AI returned an unusable response"})
                continue

            shopify_client = ShopifyClient(
                item["domain"],
                decrypt_value(item["access_token_enc"]),
                item["api_version"],
            )

            fields = {}
            updated = []
            if fix.get("new_title"):
                fields["title"] = fix["new_title"]
                updated.append("title")
            if fix.get("new_meta_title"):
                fields["meta_title"] = fix["new_meta_title"]
                updated.append("meta_title")
            if fix.get("new_meta_description"):
                fields["meta_description"] = fix["new_meta_description"]
                updated.append("meta_description")

            guard_shopify_fields(fields)
            if "title" in fields:
                fix["new_title"] = fields["title"]
            if "meta_title" in fields:
                fix["new_meta_title"] = fields["meta_title"]
            if "meta_description" in fields:
                fix["new_meta_description"] = fields["meta_description"]

            has_alt_suggestions = bool(item_type == "product" and fix.get("image_alt_suggestions"))
            if not fields and not has_alt_suggestions:
                results.append({"item_id": item_id, "status": "skipped", "updated": [], "explanation": fix.get("explanation", "AI proposed no changes")})
                continue

            if fields:
                if item_type == "product":
                    shopify_client.update_product(item["shopify_id"], fields)
                else:
                    shopify_client.update_collection(item["shopify_id"], fields)

            image_alts_changed = 0
            alt_by_media = {}
            if item_type == "product" and fix.get("image_alt_suggestions"):
                alts_to_push = []
                for sug in fix["image_alt_suggestions"]:
                    idx = sug.get("image_index", 0)
                    if idx < len(images) and images[idx].get("media_id"):
                        alts_to_push.append({"media_id": images[idx]["media_id"], "alt": sug["new_alt"]})
                        alt_by_media[images[idx]["media_id"]] = sug["new_alt"]
                if alts_to_push:
                    shopify_client.update_product_media(item["shopify_id"], alts_to_push)
                    image_alts_changed = len(alts_to_push)
                    updated.append("image_alts")

            if not updated:
                # Nothing to apply — don't stamp last_optimized_at or record a fix row.
                results.append({"item_id": item_id, "status": "skipped", "updated": [], "explanation": fix.get("explanation", "AI proposed no changes")})
                continue

            with db.get_cursor() as cur:
                if fix.get("new_title"):
                    cur.execute(f"UPDATE {table} SET title = %s WHERE id = %s", (fix["new_title"], item_id))
                if fix.get("new_meta_title"):
                    cur.execute(f"UPDATE {table} SET meta_title = %s WHERE id = %s", (fix["new_meta_title"], item_id))
                if fix.get("new_meta_description"):
                    cur.execute(f"UPDATE {table} SET meta_description = %s WHERE id = %s", (fix["new_meta_description"], item_id))
                if image_alts_changed and item_type == "product":
                    # Key by media_id — positional indexing against the raw list
                    # (which includes images without media_id) hit the wrong images.
                    db_images = item.get("images") or []
                    if isinstance(db_images, str):
                        db_images = json.loads(db_images)
                    for img in db_images:
                        if img.get("media_id") in alt_by_media:
                            img["alt"] = alt_by_media[img["media_id"]]
                    cur.execute("UPDATE synced_products SET images = %s WHERE id = %s", (json.dumps(db_images), item_id))
                cur.execute(f"UPDATE {table} SET last_optimized_at = NOW() WHERE id = %s", (item_id,))

            from app.services.analysis_refresh import refresh_item_analysis
            refresh_item_analysis(item.get("store_id"), item_type, item_id)

            with db.get_cursor() as cur:
                if competing_query and page_url:
                    cur.execute(
                        """INSERT INTO cannibalization_fixes
                           (store_id, query, page_url, item_type, item_id, action, target_keyword,
                            old_meta_title, new_meta_title, old_meta_description, new_meta_description,
                            old_title, new_title, image_alts_changed, status, applied_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', NOW())
                           ON CONFLICT (store_id, query, page_url) DO UPDATE
                           SET new_meta_title = EXCLUDED.new_meta_title,
                               new_meta_description = EXCLUDED.new_meta_description,
                               new_title = EXCLUDED.new_title,
                               image_alts_changed = EXCLUDED.image_alts_changed,
                               status = 'pending', applied_at = NOW(), verified_at = NULL""",
                        (item.get("store_id"), competing_query, page_url, item_type, item_id,
                         action, target_keyword,
                         item.get("meta_title") or "", fix.get("new_meta_title") or "",
                         item.get("meta_description") or "", fix.get("new_meta_description") or "",
                         item.get("title") or "", fix.get("new_title") or "",
                         image_alts_changed),
                    )

            results.append({"item_id": item_id, "status": "ok", "updated": updated, "explanation": fix.get("explanation", "")})
        except Exception as e:
            logger.error("Bulk fix failed for %s %s: %s", item_type, item_id, str(e))
            results.append({"item_id": item_id, "status": "error", "error": str(e)})

    success = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    return jsonify({"ok": True, "results": results, "success": success, "failed": failed})
