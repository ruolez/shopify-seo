"""Duplicates section — cross-type duplicate SEO title / meta description scan.

Scans products, collections, blog articles, news articles, and pages of one store
in a single pool, so collisions across types (e.g. a product and an article sharing
an SEO title) are caught too.
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services.blog_seo_analyzer import analyze_article_seo
from app.services.duplicate_detector import DUPLICATE_FIELDS, find_duplicate_groups
from app.services.landing_page_seo_analyzer import analyze_synced_page
from app.services.seo_analyzer import analyze_collection_seo, analyze_product_seo
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

duplicates_bp = Blueprint("duplicates", __name__)


@duplicates_bp.route("/duplicates")
def duplicates_page():
    return render_template("duplicates.html")


def _classify_article(article):
    # Mirrors the news detection rule in sync_service._sync_blogs_and_articles.
    is_news = article.get("template_suffix") == "news" or "type:news" in (article.get("tags") or "")
    return "news" if is_news else "article"


def _on_page_score(row, item_type):
    try:
        if item_type == "product":
            return analyze_product_seo(row)["score"]
        if item_type == "collection":
            return analyze_collection_seo(row)["score"]
        if item_type in ("article", "news"):
            return analyze_article_seo(row)["score"]
        return analyze_synced_page(row)["score"]
    except Exception:
        return 0


def _build_duplicate_item(row, item_type, store_id, store_name, local_id):
    """Normalize a live Shopify dict of any type into a duplicate-scan item.

    local_id is the synced_* row id used by the apply/AI-fix endpoints; None when
    the item exists live but was never synced. Pages report SEO under
    seo_title/seo_description, so those are folded into meta_title/meta_description.
    """
    meta_title = row.get("meta_title") or row.get("seo_title") or ""
    meta_description = row.get("meta_description") or row.get("seo_description") or ""
    return {
        "id": local_id,
        "type": item_type,
        "store_id": store_id,
        "store_name": store_name,
        "shopify_id": row["shopify_id"],
        "title": row.get("title") or "",
        "handle": row.get("handle") or "",
        "meta_title": meta_title,
        "meta_description": meta_description,
        "vendor": row.get("vendor") or "",
        "product_type": row.get("product_type") or "",
        "blog_title": row.get("blog_title") or "",
        "summary": (row.get("summary") or "")[:200],
        "has_blank_meta_title": not meta_title.strip(),
        "on_page_score": _on_page_score(row, item_type),
    }


@duplicates_bp.route("/api/duplicates", methods=["GET"])
def list_duplicates():
    """Find items of any type that share an SEO title or meta description.

    Fetches SEO fields LIVE from Shopify via GraphQL (not the synced cache) so the scan
    reflects the current store. Scoped to one store; SEO-title grouping uses the effective
    title (meta_title, falling back to the display name).

    Fixing still targets the local synced row, so each live item is mapped back to its
    local DB id by shopify_id; items never synced get id=None (UI disables their fixes).
    """
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT domain, access_token_enc, api_version,
                      COALESCE(shop_name, name) AS store_name
               FROM stores WHERE id = %s""",
            (store_id,),
        )
        store = cur.fetchone()
        if not store:
            return jsonify({"error": "Store not found"}), 404

        local_ids = {}
        for key, table in (
            ("product", "synced_products"),
            ("collection", "synced_collections"),
            ("article", "synced_articles"),
            ("page", "synced_pages"),
        ):
            cur.execute(f"SELECT id, shopify_id FROM {table} WHERE store_id = %s", (store_id,))
            local_ids[key] = {r["shopify_id"]: r["id"] for r in cur.fetchall()}

    try:
        client = ShopifyClient(
            store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"]
        )
        products = client.get_all_products(
            query_filter="status:active AND published_status:published"
        )
        collections = [c for c in client.get_all_collections() if c.get("published")]
        articles = [a for a in client.get_all_articles() if a.get("is_published")]
        pages = [p for p in client.get_all_pages() if p.get("is_published")]
    except Exception as e:
        logger.exception("Live duplicate scan failed for store %s: %s", store_id, e)
        return jsonify({"error": f"Failed to fetch from Shopify: {e}"}), 502

    logger.info(
        "Live duplicate scan store %s: %d products, %d collections, %d articles, %d pages",
        store_id, len(products), len(collections), len(articles), len(pages),
    )

    sid = int(store_id)
    sname = store["store_name"]
    items = []
    for p in products:
        items.append(_build_duplicate_item(
            p, "product", sid, sname, local_ids["product"].get(p["shopify_id"])
        ))
    for c in collections:
        items.append(_build_duplicate_item(
            c, "collection", sid, sname, local_ids["collection"].get(c["shopify_id"])
        ))
    for a in articles:
        items.append(_build_duplicate_item(
            a, _classify_article(a), sid, sname, local_ids["article"].get(a["shopify_id"])
        ))
    for p in pages:
        items.append(_build_duplicate_item(
            p, "page", sid, sname, local_ids["page"].get(p["shopify_id"])
        ))

    result = {
        "counts": {},
        "scanned": {
            "product": len(products),
            "collection": len(collections),
            "article": len(articles),
            "page": len(pages),
        },
    }
    for field in DUPLICATE_FIELDS:
        groups = find_duplicate_groups(items, field)
        result[field] = groups
        result["counts"][field] = sum(g["count"] for g in groups)

    # Near-identical description text — the usual reason Google reads a page
    # and declines to index it. Compared within each page type.
    from app.services.duplicate_detector import find_similar_bodies

    bodies = []
    for rows, item_type, id_key in ((products, "product", "product"), (collections, "collection", "collection"),
                                    (articles, "article", "article")):
        for row in rows:
            bodies.append({
                "id": local_ids[id_key].get(row["shopify_id"]), "type": item_type,
                "title": row.get("title") or "", "handle": row.get("handle") or "",
                "body": row.get("description") or row.get("body") or "",
            })
    try:
        result["similar_bodies"] = find_similar_bodies(bodies)
    except Exception:
        logger.exception("Similar-body scan failed for store %s", store_id)
        result["similar_bodies"] = []
    result["counts"]["similar_bodies"] = sum(g["count"] for g in result["similar_bodies"])

    return jsonify(result)
