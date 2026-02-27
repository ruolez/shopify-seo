from flask import Blueprint, jsonify, render_template, request

from openai import OpenAI

from app.auth import decrypt_value, encrypt_value
from app.database import db

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
        }
    )


@settings_bp.route("/api/settings/ai", methods=["POST"])
def save_ai_config():
    data = request.get_json()
    api_key = (data.get("api_key") or "").strip()
    model = (data.get("model") or "anthropic/claude-sonnet-4").strip()
    daily_budget = data.get("daily_budget", 10.00)

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM ai_config ORDER BY id LIMIT 1")
        existing = cur.fetchone()

        if existing:
            if api_key:
                key_enc = encrypt_value(api_key)
                cur.execute(
                    "UPDATE ai_config SET api_key_enc=%s, model=%s, daily_budget=%s, updated_at=NOW() WHERE id=%s",
                    (key_enc, model, daily_budget, existing["id"]),
                )
            else:
                cur.execute(
                    "UPDATE ai_config SET model=%s, daily_budget=%s, updated_at=NOW() WHERE id=%s",
                    (model, daily_budget, existing["id"]),
                )
        else:
            key_enc = encrypt_value(api_key) if api_key else None
            cur.execute(
                "INSERT INTO ai_config (api_key_enc, model, daily_budget) VALUES (%s, %s, %s)",
                (key_enc, model, daily_budget),
            )

    return jsonify({"ok": True})


@settings_bp.route("/api/settings/brave", methods=["POST"])
def save_brave_config():
    data = request.get_json()
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


@settings_bp.route("/api/settings/ai/models", methods=["GET"])
def get_ai_models():
    """Fetch available models from OpenRouter, filtered to tool-calling models."""
    import requests

    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=15)

        if response.status_code != 200:
            return jsonify({"error": f"OpenRouter API error: {response.status_code}"}), 502

        data = response.json()
        models = []

        for model in data.get("data", []):
            supported_params = model.get("supported_parameters", [])
            if "tools" not in supported_params:
                continue

            pricing = model.get("pricing", {})
            prompt_per_million = float(pricing.get("prompt", "0")) * 1_000_000
            completion_per_million = float(pricing.get("completion", "0")) * 1_000_000

            models.append(
                {
                    "id": model.get("id", ""),
                    "name": model.get("name", model.get("id", "")),
                    "pricing": {
                        "prompt": round(prompt_per_million, 2),
                        "completion": round(completion_per_million, 2),
                    },
                }
            )

        models.sort(key=lambda m: m["name"].lower())
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
