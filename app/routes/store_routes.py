from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value, encrypt_value
from app.database import db
from app.services.sync_service import sync_store_async
from app.shopify_client import ShopifyClient

store_bp = Blueprint("stores", __name__)


@store_bp.route("/stores")
def stores_page():
    return render_template("stores.html")


@store_bp.route("/api/stores", methods=["GET"])
def list_stores():
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, domain, api_version, is_active, last_synced_at, created_at FROM stores ORDER BY id"
        )
        stores = cur.fetchall()
    result = []
    for s in stores:
        result.append(
            {
                "id": s["id"],
                "name": s["name"],
                "domain": s["domain"],
                "api_version": s["api_version"],
                "is_active": s["is_active"],
                "last_synced_at": s["last_synced_at"].isoformat() if s["last_synced_at"] else None,
                "created_at": s["created_at"].isoformat() if s["created_at"] else None,
            }
        )
    return jsonify(result)


@store_bp.route("/api/stores", methods=["POST"])
def create_store():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    domain = (data.get("domain") or "").strip()
    access_token = (data.get("access_token") or "").strip()
    api_version = (data.get("api_version") or "2024-10").strip()

    if not name or not domain or not access_token:
        return jsonify({"error": "Name, domain, and access token are required"}), 400

    token_enc = encrypt_value(access_token)

    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO stores (name, domain, access_token_enc, api_version)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (name, domain, token_enc, api_version),
        )
        store_id = cur.fetchone()["id"]

    return jsonify({"ok": True, "id": store_id}), 201


@store_bp.route("/api/stores/<int:store_id>", methods=["PUT"])
def update_store(store_id):
    data = request.get_json()
    name = (data.get("name") or "").strip()
    domain = (data.get("domain") or "").strip()
    access_token = (data.get("access_token") or "").strip()
    api_version = (data.get("api_version") or "2024-10").strip()

    if not name or not domain:
        return jsonify({"error": "Name and domain are required"}), 400

    with db.get_cursor() as cur:
        if access_token:
            token_enc = encrypt_value(access_token)
            cur.execute(
                "UPDATE stores SET name=%s, domain=%s, access_token_enc=%s, api_version=%s WHERE id=%s",
                (name, domain, token_enc, api_version, store_id),
            )
        else:
            cur.execute(
                "UPDATE stores SET name=%s, domain=%s, api_version=%s WHERE id=%s",
                (name, domain, api_version, store_id),
            )

    return jsonify({"ok": True})


@store_bp.route("/api/stores/<int:store_id>", methods=["DELETE"])
def delete_store(store_id):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM stores WHERE id = %s", (store_id,))
    return jsonify({"ok": True})


@store_bp.route("/api/stores/<int:store_id>/test", methods=["POST"])
def test_store_connection(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()

    if not store:
        return jsonify({"error": "Store not found"}), 404

    try:
        access_token = decrypt_value(store["access_token_enc"])
        client = ShopifyClient(store["domain"], access_token, store["api_version"])
        result = client.test_connection()
        return jsonify({"ok": True, "shop": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@store_bp.route("/api/stores/<int:store_id>/sync", methods=["POST"])
def sync_store_route(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM stores WHERE id = %s", (store_id,))
        if not cur.fetchone():
            return jsonify({"error": "Store not found"}), 404

    sync_store_async(store_id)
    return jsonify({"ok": True, "message": "Sync started"})


@store_bp.route("/api/stores/<int:store_id>/sync-status", methods=["GET"])
def sync_status(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM sync_jobs WHERE store_id = %s ORDER BY id DESC LIMIT 1",
            (store_id,),
        )
        job = cur.fetchone()

    if not job:
        return jsonify({"status": "none"})

    return jsonify(
        {
            "status": job["status"],
            "products_synced": job["products_synced"],
            "collections_synced": job["collections_synced"],
            "error": job["error"],
            "started_at": job["started_at"].isoformat() if job["started_at"] else None,
            "completed_at": job["completed_at"].isoformat() if job["completed_at"] else None,
            "current_phase": job.get("current_phase"),
            "total_products": job.get("total_products", 0),
            "total_collections": job.get("total_collections", 0),
            "collections_mapped": job.get("collections_mapped", 0),
        }
    )
