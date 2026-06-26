import json
import logging

from flask import Blueprint, jsonify, request

from app.auth import decrypt_value
from app.database import db
from app.services.schema_generator import (
    generate_product_schema,
    schema_to_script_tag,
    validate_schema,
)
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

schema_bp = Blueprint("schema", __name__)


def generate_and_push_product_schema(product_id, client=None):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT sp.*, s.domain, s.access_token_enc, s.api_version "
            "FROM synced_products sp JOIN stores s ON s.id = sp.store_id WHERE sp.id = %s",
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return

    product = dict(product)
    store_domain = product["domain"]
    if not store_domain.endswith(".myshopify.com"):
        store_domain = f"{store_domain}.myshopify.com"

    org_settings = _get_org_settings(product["store_id"])
    schema = generate_product_schema(product, store_domain, org_settings)
    is_valid, errors = validate_schema(schema)

    with db.get_cursor() as cur:
        cur.execute("""
            INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, validation_errors)
            VALUES (%s, %s, 'product', 'Product', %s, %s, %s)
            ON CONFLICT (store_id, resource_id, schema_type)
            DO UPDATE SET schema_json = EXCLUDED.schema_json, is_valid = EXCLUDED.is_valid,
                          validation_errors = EXCLUDED.validation_errors, updated_at = NOW(),
                          pushed_to_shopify = FALSE, pushed_at = NULL
            RETURNING id
        """, (
            product["store_id"],
            product["shopify_id"],
            json.dumps(schema),
            is_valid,
            json.dumps(errors) if errors else None,
        ))

    if not is_valid:
        logger.warning("Schema invalid for product %s: %s", product_id, errors)
        return

    if client is None:
        client = ShopifyClient(
            product["domain"],
            decrypt_value(product["access_token_enc"]),
            product["api_version"],
        )

    schema_value = json.dumps(schema)
    client.set_product_metafield(
        product["shopify_id"], "seo", "schema_json_ld", schema_value, "json"
    )

    with db.get_cursor() as cur:
        cur.execute("""
            UPDATE schema_markup SET pushed_to_shopify = TRUE, pushed_at = NOW()
            WHERE store_id = %s AND resource_id = %s AND schema_type = 'Product'
        """, (product["store_id"], product["shopify_id"]))


def _get_org_settings(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        row = cur.fetchone()
        if row:
            return dict(row)
    return None


def _get_store_domain(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT COALESCE(primary_domain, domain) as domain FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone()
        if row:
            domain = row["domain"]
            if domain.endswith(".myshopify.com"):
                return domain
            return f"{domain}.myshopify.com"
    return None


@schema_bp.route("/api/schema/products", methods=["GET"])
def list_products_schema():
    with db.get_cursor() as cur:
        cur.execute("""
            SELECT sp.id, sp.store_id, sp.shopify_id, sp.title, sp.handle, sp.status,
                   COALESCE(s.shop_name, s.name) as store_name,
                   sm.id as schema_id, sm.is_valid, sm.pushed_to_shopify, sm.pushed_at
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            LEFT JOIN schema_markup sm ON sm.resource_id = sp.shopify_id
                AND sm.store_id = sp.store_id AND sm.schema_type = 'Product'
            ORDER BY sp.title
        """)
        products = cur.fetchall()

    result = []
    for p in products:
        result.append({
            "id": p["id"],
            "store_id": p["store_id"],
            "shopify_id": p["shopify_id"],
            "title": p["title"],
            "handle": p["handle"],
            "status": p["status"],
            "store_name": p["store_name"],
            "schema_generated": p["schema_id"] is not None,
            "is_valid": p["is_valid"] if p["schema_id"] else None,
            "pushed_to_shopify": p["pushed_to_shopify"] if p["schema_id"] else False,
            "pushed_at": p["pushed_at"].isoformat() if p.get("pushed_at") else None,
        })

    return jsonify(result)


@schema_bp.route("/api/schema/products/<int:product_id>/generate", methods=["POST"])
def generate_schema(product_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT sp.*, s.domain FROM synced_products sp JOIN stores s ON s.id = sp.store_id WHERE sp.id = %s",
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    product = dict(product)
    store_domain = product["domain"]
    if not store_domain.endswith(".myshopify.com"):
        store_domain = f"{store_domain}.myshopify.com"

    org_settings = _get_org_settings(product["store_id"])
    schema = generate_product_schema(product, store_domain, org_settings)
    is_valid, errors = validate_schema(schema)

    with db.get_cursor() as cur:
        cur.execute("""
            INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, validation_errors)
            VALUES (%s, %s, 'product', 'Product', %s, %s, %s)
            ON CONFLICT (store_id, resource_id, schema_type)
            DO UPDATE SET schema_json = EXCLUDED.schema_json, is_valid = EXCLUDED.is_valid,
                          validation_errors = EXCLUDED.validation_errors, updated_at = NOW()
            RETURNING id
        """, (
            product["store_id"],
            product["shopify_id"],
            json.dumps(schema),
            is_valid,
            json.dumps(errors) if errors else None,
        ))
        row = cur.fetchone()

    return jsonify({
        "ok": True,
        "schema_id": row["id"],
        "schema": schema,
        "is_valid": is_valid,
        "validation_errors": errors,
        "script_tag": schema_to_script_tag(schema),
    })


@schema_bp.route("/api/schema/products/<int:product_id>/view", methods=["GET"])
def view_schema(product_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT shopify_id, store_id FROM synced_products WHERE id = %s",
            (product_id,),
        )
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    with db.get_cursor() as cur:
        cur.execute("""
            SELECT * FROM schema_markup
            WHERE store_id = %s AND resource_id = %s AND schema_type = 'Product'
        """, (product["store_id"], product["shopify_id"]))
        schema_row = cur.fetchone()

    if not schema_row:
        return jsonify({"error": "Schema not generated yet"}), 404

    schema_json = schema_row["schema_json"]
    if isinstance(schema_json, str):
        schema_json = json.loads(schema_json)

    return jsonify({
        "schema": schema_json,
        "is_valid": schema_row["is_valid"],
        "validation_errors": schema_row["validation_errors"] or [],
        "pushed_to_shopify": schema_row["pushed_to_shopify"],
        "pushed_at": schema_row["pushed_at"].isoformat() if schema_row.get("pushed_at") else None,
        "script_tag": schema_to_script_tag(schema_json),
        "updated_at": schema_row["updated_at"].isoformat() if schema_row.get("updated_at") else None,
    })


@schema_bp.route("/api/schema/products/<int:product_id>/push", methods=["POST"])
def push_schema(product_id):
    with db.get_cursor() as cur:
        cur.execute("""
            SELECT sp.shopify_id, sp.store_id, s.domain, s.access_token_enc, s.api_version
            FROM synced_products sp
            JOIN stores s ON s.id = sp.store_id
            WHERE sp.id = %s
        """, (product_id,))
        product = cur.fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    with db.get_cursor() as cur:
        cur.execute("""
            SELECT schema_json FROM schema_markup
            WHERE store_id = %s AND resource_id = %s AND schema_type = 'Product'
        """, (product["store_id"], product["shopify_id"]))
        schema_row = cur.fetchone()

    if not schema_row:
        return jsonify({"error": "Schema not generated yet. Generate first."}), 400

    schema_json = schema_row["schema_json"]
    if isinstance(schema_json, dict):
        schema_value = json.dumps(schema_json)
    else:
        schema_value = schema_json

    try:
        client = ShopifyClient(
            product["domain"],
            decrypt_value(product["access_token_enc"]),
            product["api_version"],
        )
        client.set_product_metafield(
            product["shopify_id"],
            "seo",
            "schema_json_ld",
            schema_value,
            "json",
        )

        with db.get_cursor() as cur:
            cur.execute("""
                UPDATE schema_markup SET pushed_to_shopify = TRUE, pushed_at = NOW()
                WHERE store_id = %s AND resource_id = %s AND schema_type = 'Product'
            """, (product["store_id"], product["shopify_id"]))

        return jsonify({"ok": True})

    except Exception as e:
        logger.exception("Push schema failed for product %s", product_id)
        return jsonify({"error": str(e)}), 500


@schema_bp.route("/api/schema/batch/generate", methods=["POST"])
def batch_generate():
    data = request.get_json() or {}
    product_ids = data.get("product_ids", [])

    if not product_ids:
        with db.get_cursor() as cur:
            cur.execute("SELECT id FROM synced_products ORDER BY id")
            product_ids = [r["id"] for r in cur.fetchall()]

    generated = 0
    errors_list = []

    for pid in product_ids:
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT sp.*, s.domain FROM synced_products sp JOIN stores s ON s.id = sp.store_id WHERE sp.id = %s",
                    (pid,),
                )
                product = cur.fetchone()

            if not product:
                continue

            product = dict(product)
            store_domain = product["domain"]
            if not store_domain.endswith(".myshopify.com"):
                store_domain = f"{store_domain}.myshopify.com"

            org_settings = _get_org_settings(product["store_id"])
            schema = generate_product_schema(product, store_domain, org_settings)
            is_valid, validation_errors = validate_schema(schema)

            with db.get_cursor() as cur:
                cur.execute("""
                    INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, validation_errors)
                    VALUES (%s, %s, 'product', 'Product', %s, %s, %s)
                    ON CONFLICT (store_id, resource_id, schema_type)
                    DO UPDATE SET schema_json = EXCLUDED.schema_json, is_valid = EXCLUDED.is_valid,
                                  validation_errors = EXCLUDED.validation_errors, updated_at = NOW()
                """, (
                    product["store_id"],
                    product["shopify_id"],
                    json.dumps(schema),
                    is_valid,
                    json.dumps(validation_errors) if validation_errors else None,
                ))

            generated += 1
        except Exception as e:
            errors_list.append({"product_id": pid, "error": str(e)})

    return jsonify({
        "ok": True,
        "generated": generated,
        "total": len(product_ids),
        "errors": errors_list,
    })


@schema_bp.route("/api/schema/batch/push", methods=["POST"])
def batch_push():
    with db.get_cursor() as cur:
        cur.execute("""
            SELECT sm.*, sp.id as product_db_id, s.domain, s.access_token_enc, s.api_version
            FROM schema_markup sm
            JOIN synced_products sp ON sp.shopify_id = sm.resource_id AND sp.store_id = sm.store_id
            JOIN stores s ON s.id = sm.store_id
            WHERE sm.schema_type = 'Product' AND sm.is_valid = TRUE AND sm.pushed_to_shopify = FALSE
        """)
        rows = cur.fetchall()

    pushed = 0
    errors_list = []

    for row in rows:
        try:
            schema_json = row["schema_json"]
            if isinstance(schema_json, dict):
                schema_value = json.dumps(schema_json)
            else:
                schema_value = schema_json

            client = ShopifyClient(
                row["domain"],
                decrypt_value(row["access_token_enc"]),
                row["api_version"],
            )
            client.set_product_metafield(
                row["resource_id"],
                "seo",
                "schema_json_ld",
                schema_value,
                "json",
            )

            with db.get_cursor() as cur:
                cur.execute("""
                    UPDATE schema_markup SET pushed_to_shopify = TRUE, pushed_at = NOW()
                    WHERE id = %s
                """, (row["id"],))

            pushed += 1
        except Exception as e:
            errors_list.append({"resource_id": row["resource_id"], "error": str(e)})

    return jsonify({
        "ok": True,
        "pushed": pushed,
        "total": len(rows),
        "errors": errors_list,
    })


@schema_bp.route("/api/schema/stats", methods=["GET"])
def schema_stats():
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as total FROM synced_products")
        total = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) as cnt FROM schema_markup WHERE schema_type = 'Product'")
        generated = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM schema_markup WHERE schema_type = 'Product' AND is_valid = TRUE")
        valid = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM schema_markup WHERE schema_type = 'Product' AND pushed_to_shopify = TRUE")
        pushed = cur.fetchone()["cnt"]

    return jsonify({
        "total_products": total,
        "schemas_generated": generated,
        "schemas_valid": valid,
        "schemas_pushed": pushed,
    })


@schema_bp.route("/api/schema/settings", methods=["GET", "POST"])
def schema_settings():
    if request.method == "GET":
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM schema_settings ORDER BY id LIMIT 1")
            row = cur.fetchone()

        if not row:
            return jsonify({
                "organization_name": "",
                "organization_logo_url": "",
                "organization_url": "",
                "default_currency": "USD",
            })

        return jsonify({
            "organization_name": row["organization_name"] or "",
            "organization_logo_url": row["organization_logo_url"] or "",
            "organization_url": row["organization_url"] or "",
            "default_currency": row["default_currency"] or "USD",
        })

    data = request.get_json() or {}

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM stores ORDER BY id LIMIT 1")
        store_row = cur.fetchone()

    if not store_row:
        return jsonify({"error": "No store configured"}), 400

    store_id = store_row["id"]

    with db.get_cursor() as cur:
        cur.execute("""
            INSERT INTO schema_settings (store_id, organization_name, organization_logo_url, organization_url, default_currency)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (store_id)
            DO UPDATE SET organization_name = EXCLUDED.organization_name,
                          organization_logo_url = EXCLUDED.organization_logo_url,
                          organization_url = EXCLUDED.organization_url,
                          default_currency = EXCLUDED.default_currency,
                          updated_at = NOW()
        """, (
            store_id,
            data.get("organization_name", ""),
            data.get("organization_logo_url", ""),
            data.get("organization_url", ""),
            data.get("default_currency", "USD"),
        ))

    return jsonify({"ok": True})
