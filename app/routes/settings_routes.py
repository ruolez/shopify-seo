import json

from flask import Blueprint, jsonify, render_template, request

from openai import OpenAI

from app.auth import decrypt_value, encrypt_value
from app.database import db
from app.prompt_defaults import DEFAULTS
from app.settings_manager import delete_setting, get_all_custom, get_effective, get_setting, set_setting

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings")
def settings_page():
    return render_template("settings.html")


@settings_bp.route("/api/settings/ai", methods=["GET"])
def get_ai_config():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()

    if not config:
        return jsonify({"has_key": False, "model": "anthropic/claude-sonnet-4", "daily_budget": 10.00, "has_brave_key": False})

    return jsonify(
        {
            "has_key": bool(config.get("api_key_enc")),
            "model": config.get("model") or "anthropic/claude-sonnet-4",
            "daily_budget": float(config.get("daily_budget") or 10),
            "has_brave_key": bool(config.get("brave_api_key_enc")),
            "image_model": config.get("image_model") or "google/gemini-2.5-flash-image",
            "has_gsc_client_id": bool(config.get("gsc_client_id_enc")),
            "has_gsc_client_secret": bool(config.get("gsc_client_secret_enc")),
            "topic_research_model": config.get("topic_research_model") or "",
            "topic_suggestion_count": int(config.get("topic_suggestion_count") or 3),
        }
    )


@settings_bp.route("/api/settings/ai", methods=["POST"])
def save_ai_config():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    model = (data.get("model") or "anthropic/claude-sonnet-4").strip()
    daily_budget = data.get("daily_budget", 10.00)
    image_model = (data.get("image_model") or "google/gemini-2.5-flash-image").strip()
    topic_research_model = (data.get("topic_research_model") or "").strip() or None
    try:
        topic_suggestion_count = max(1, min(int(data.get("topic_suggestion_count") or 3), 10))
    except (TypeError, ValueError):
        topic_suggestion_count = 3

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM ai_config ORDER BY id LIMIT 1")
        existing = cur.fetchone()

        if existing:
            if api_key:
                key_enc = encrypt_value(api_key)
                cur.execute(
                    "UPDATE ai_config SET api_key_enc=%s, model=%s, daily_budget=%s, image_model=%s, "
                    "topic_research_model=%s, topic_suggestion_count=%s, updated_at=NOW() WHERE id=%s",
                    (key_enc, model, daily_budget, image_model, topic_research_model, topic_suggestion_count, existing["id"]),
                )
            else:
                cur.execute(
                    "UPDATE ai_config SET model=%s, daily_budget=%s, image_model=%s, "
                    "topic_research_model=%s, topic_suggestion_count=%s, updated_at=NOW() WHERE id=%s",
                    (model, daily_budget, image_model, topic_research_model, topic_suggestion_count, existing["id"]),
                )
        else:
            key_enc = encrypt_value(api_key) if api_key else None
            cur.execute(
                "INSERT INTO ai_config (api_key_enc, model, daily_budget, image_model, topic_research_model, topic_suggestion_count) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (key_enc, model, daily_budget, image_model, topic_research_model, topic_suggestion_count),
            )

    return jsonify({"ok": True})


@settings_bp.route("/api/settings/gsc", methods=["POST"])
def save_gsc_config():
    data = request.get_json(silent=True) or {}
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()

    if not client_id or not client_secret:
        return jsonify({"error": "Both client_id and client_secret are required"}), 400

    id_enc = encrypt_value(client_id)
    secret_enc = encrypt_value(client_secret)

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM ai_config ORDER BY id LIMIT 1")
        existing = cur.fetchone()

        if existing:
            cur.execute(
                "UPDATE ai_config SET gsc_client_id_enc = %s, gsc_client_secret_enc = %s, updated_at = NOW() WHERE id = %s",
                (id_enc, secret_enc, existing["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO ai_config (gsc_client_id_enc, gsc_client_secret_enc) VALUES (%s, %s)",
                (id_enc, secret_enc),
            )

    return jsonify({"ok": True})


@settings_bp.route("/api/settings/brave", methods=["POST"])
def save_brave_config():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()

    if not api_key:
        return jsonify({"error": "api_key is required"}), 400

    key_enc = encrypt_value(api_key)

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM ai_config ORDER BY id LIMIT 1")
        existing = cur.fetchone()

        if existing:
            cur.execute(
                "UPDATE ai_config SET brave_api_key_enc = %s, updated_at = NOW() WHERE id = %s",
                (key_enc, existing["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO ai_config (brave_api_key_enc) VALUES (%s)",
                (key_enc,),
            )

    return jsonify({"ok": True})


@settings_bp.route("/api/settings/brave/test", methods=["POST"])
def test_brave_connection():
    import requests as req

    with db.get_cursor() as cur:
        cur.execute("SELECT brave_api_key_enc FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()

    if not config or not config.get("brave_api_key_enc"):
        return jsonify({"error": "No Brave API key configured"}), 400

    try:
        api_key = decrypt_value(config["brave_api_key_enc"])
        resp = req.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
            params={"q": "test", "count": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            return jsonify({"ok": True})
        elif resp.status_code == 401:
            return jsonify({"error": "Invalid API key"}), 400
        else:
            return jsonify({"error": f"HTTP {resp.status_code}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


NEW_MODEL_DAYS = 30


def _fetch_openrouter_models(force=False):
    """OpenRouter's public model catalog. Its CDN caches for 5 minutes; a
    forced refresh busts that with a nonce so a just-released model shows up."""
    import time

    import requests

    params = {"_": int(time.time())} if force else None
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"} if force else None
    response = requests.get("https://openrouter.ai/api/v1/models", params=params, headers=headers, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter API error: {response.status_code}")
    return response.json().get("data", [])


def _model_age_fields(model):
    import time

    created = model.get("created") or 0
    days = max(0, int((time.time() - created) // 86400)) if created else None
    return {"added_days_ago": days, "is_new": days is not None and days <= NEW_MODEL_DAYS,
            "context_length": model.get("context_length")}


def _catalog_response(models):
    from datetime import datetime, timezone

    return jsonify({
        "models": models,
        "total": len(models),
        "new_count": sum(1 for m in models if m.get("is_new")),
        "new_days": NEW_MODEL_DAYS,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@settings_bp.route("/api/settings/ai/models", methods=["GET"])
def get_ai_models():
    """Text models from OpenRouter, filtered to tool-calling ones. ?refresh=1 bypasses caches."""
    try:
        models = []
        for model in _fetch_openrouter_models(force=request.args.get("refresh") == "1"):
            if "tools" not in model.get("supported_parameters", []):
                continue
            pricing = model.get("pricing", {})
            models.append({
                "id": model.get("id", ""),
                "name": model.get("name", model.get("id", "")),
                "pricing": {
                    "prompt": round(float(pricing.get("prompt", "0")) * 1_000_000, 2),
                    "completion": round(float(pricing.get("completion", "0")) * 1_000_000, 2),
                },
                **_model_age_fields(model),
            })
        models.sort(key=lambda m: m["name"].lower())
        return _catalog_response(models)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@settings_bp.route("/api/settings/ai/image-models", methods=["GET"])
def get_image_models():
    """Image-output models from OpenRouter. ?refresh=1 bypasses caches."""
    try:
        models = []
        for model in _fetch_openrouter_models(force=request.args.get("refresh") == "1"):
            if "image" not in (model.get("architecture", {}).get("output_modalities") or []):
                continue
            if model.get("id") == "openrouter/auto":
                continue
            pricing = model.get("pricing", {})
            models.append({
                "id": model.get("id", ""),
                "name": model.get("name", model.get("id", "")),
                "pricing": {
                    "prompt": round(float(pricing.get("prompt", "0")) * 1_000_000, 2),
                    "image": round(float(pricing.get("image", pricing.get("completion", "0"))) * 1_000_000, 2),
                },
                **_model_age_fields(model),
            })
        models.sort(key=lambda m: m["pricing"]["prompt"])
        if models:
            return _catalog_response(models)
    except Exception:
        pass
    return _catalog_response(_fallback_image_models())


def _fallback_image_models():
    return [
        {"id": "google/gemini-2.5-flash-image", "name": "Google: Gemini 2.5 Flash Image", "pricing": {"prompt": 0, "image": 0}},
        {"id": "google/gemini-3-pro-image-preview", "name": "Google: Gemini 3 Pro Image Preview", "pricing": {"prompt": 0, "image": 0}},
        {"id": "openai/gpt-5-image-mini", "name": "OpenAI: GPT-5 Image Mini", "pricing": {"prompt": 0, "image": 0}},
        {"id": "openai/gpt-5-image", "name": "OpenAI: GPT-5 Image", "pricing": {"prompt": 0, "image": 0}},
    ]


@settings_bp.route("/api/settings/ai/test", methods=["POST"])
def test_ai_connection():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()

    if not config or not config.get("api_key_enc"):
        return jsonify({"error": "No API key configured"}), 400

    try:
        api_key = decrypt_value(config["api_key_enc"])
        model = config.get("model") or "anthropic/claude-sonnet-4"

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            timeout=120.0, max_retries=1,
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say 'connected' in one word."}],
            max_tokens=10,
        )
        reply = response.choices[0].message.content
        return jsonify({"ok": True, "response": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@settings_bp.route("/api/settings/custom", methods=["GET"])
def list_custom_settings():
    prefix = request.args.get("prefix", "")
    effective = get_effective(prefix if prefix else None)
    overrides = get_all_custom(prefix if prefix else None)
    result = {}
    for key, value in effective.items():
        result[key] = {
            "value": value,
            "is_custom": key in overrides,
        }
    return jsonify(result)


@settings_bp.route("/api/settings/custom/<path:key>", methods=["GET"])
def get_custom_setting(key):
    overrides = get_all_custom()
    value = get_setting(key)
    if value is None:
        return jsonify({"error": "Unknown setting key"}), 404
    return jsonify({
        "key": key,
        "value": value,
        "is_custom": key in overrides,
    })


@settings_bp.route("/api/settings/custom/<path:key>", methods=["POST"])
def save_custom_setting(key):
    if key not in DEFAULTS:
        return jsonify({"error": f"Unknown setting key: {key}"}), 400

    data = request.get_json(silent=True) or {}
    value = data.get("value")
    if value is None:
        return jsonify({"error": "value is required"}), 400

    default = DEFAULTS[key]
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return jsonify({"error": "Value must be a JSON object"}), 400
        if "weights" in default and "weights" in value:
            total = sum(value["weights"].values())
            if abs(total - 1.0) > 0.01:
                return jsonify({"error": f"Weights must sum to 1.0 (currently {total:.2f})"}), 400

    set_setting(key, value)
    return jsonify({"ok": True})


@settings_bp.route("/api/settings/custom/<path:key>", methods=["DELETE"])
def reset_custom_setting(key):
    delete_setting(key)
    default_value = DEFAULTS.get(key)
    return jsonify({"ok": True, "default_value": default_value})


@settings_bp.route("/api/settings/defaults/<path:key>", methods=["GET"])
def get_default_setting(key):
    if key not in DEFAULTS:
        return jsonify({"error": "Unknown setting key"}), 404
    return jsonify({"key": key, "value": DEFAULTS[key]})
