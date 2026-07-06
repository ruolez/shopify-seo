"""AI SEO section — GEO/AEO/LLMO readiness scoring and (later) optimization.

Phase 1: deterministic AI-visibility scoring of synced products/collections,
enriched by a live storefront crawl (schema, robots.txt, llms.txt) as seen by
AI crawlers. Reuses serp_scraper's page fetcher, the ai_tasks daemon pattern,
and the analyzer in services/ai_seo_analyzer.py.
"""

import asyncio
import json
import logging
import threading

import requests
from itertools import islice
from flask import Blueprint, jsonify, render_template, request

from app.auth import decrypt_value
from app.database import db
from app.services import ai_seo_analyzer
from app.services.serp_scraper import scrape_serp_pages
from app.shopify_client import ShopifyClient

COMPONENT_LABELS = {
    "structured_data": "Structured data",
    "extractability": "Extractability",
    "fact_density": "Fact density",
    "crawlability": "AI crawlability",
    "entity_authority": "Entity authority",
    "freshness": "Freshness",
}

logger = logging.getLogger(__name__)

ai_seo_bp = Blueprint("ai_seo", __name__)

# Crawl in bounded batches so every item is fetched without a single huge,
# heavily-staggered concurrent burst. All items get crawled — no silent cap.
CRAWL_BATCH_SIZE = 100


def _store_base_url(store):
    host = (store.get("primary_domain") or store.get("domain") or "").strip()
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{host}" if host else None


def _item_url(base_url, resource_type, handle):
    if not base_url or not handle:
        return None
    path = "collections" if resource_type == "collection" else "products"
    return f"{base_url}/{path}/{handle}"


def _fetch_text(url, timeout=15):
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def _fetch_store_crawlability(base_url):
    """Return (robots_txt, llms_txt_present) for the store domain."""
    if not base_url:
        return None, False
    robots = _fetch_text(f"{base_url}/robots.txt")
    llms = _fetch_text(f"{base_url}/llms.txt")
    return robots, bool(llms and llms.strip())


def _crawl_batch(urls):
    """Concurrently fetch one bounded batch of pages; return {url: metadata}."""
    urls = [u for u in urls if u]
    if not urls:
        return {}
    try:
        pages = asyncio.run(scrape_serp_pages(urls, max_pages=len(urls)))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            pages = loop.run_until_complete(scrape_serp_pages(urls, max_pages=len(urls)))
        finally:
            loop.close()
    return {p["url"]: p for p in pages if p and p.get("url")}


def _load_items(store_id, resource_type):
    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE store_id = %s", (store_id,))
        return cur.fetchall()


def _pushed_schema(store_id):
    """Map shopify resource_id -> list of pushed schema_markup rows (with JSON for richness)."""
    out = {}
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT resource_id, schema_type, schema_json FROM schema_markup WHERE store_id = %s",
            (store_id,),
        )
        for row in cur.fetchall():
            out.setdefault(str(row["resource_id"]), []).append(row)
    return out


def _score_items(store_id, resource_type, items, base_url, crawlability, store_domain):
    """Crawl (in bounded batches) + score every item; upsert into ai_seo_scores."""
    schema_map = _pushed_schema(store_id)
    scored = 0
    it_iter = iter(items)
    while True:
        batch = list(islice(it_iter, CRAWL_BATCH_SIZE))
        if not batch:
            break
        urls = {it["id"]: _item_url(base_url, resource_type, it.get("handle")) for it in batch}
        crawled = _crawl_batch(list(urls.values()))
        for it in batch:
            it = dict(it)
            it["resource_type"] = resource_type
            live = crawled.get(urls[it["id"]])
            markup = schema_map.get(str(it.get("shopify_id")), [])
            result = ai_seo_analyzer.analyze_ai_visibility(
                it,
                live=live,
                schema_markup=markup,
                crawlability=crawlability,
                store_domain=store_domain,
            )
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai_seo_scores
                        (store_id, resource_type, resource_id, score, breakdown, live_crawl, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (store_id, resource_type, resource_id) DO UPDATE SET
                        score = EXCLUDED.score,
                        breakdown = EXCLUDED.breakdown,
                        live_crawl = EXCLUDED.live_crawl,
                        created_at = NOW()
                    """,
                    (
                        store_id,
                        resource_type,
                        it["id"],
                        result["score"],
                        json.dumps(result["breakdown"]),
                        json.dumps(live) if live else None,
                    ),
                )
            scored += 1
    return scored


def _execute_scan(task_id, store_id, resource_type):
    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tasks SET status = 'running' WHERE id = %s", (task_id,))
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
            store = cur.fetchone()
        if not store:
            raise ValueError("Store not found")

        base_url = _store_base_url(store)
        robots, llms_present = _fetch_store_crawlability(base_url)
        crawlability = ai_seo_analyzer.analyze_store_crawlability(robots, llms_present)

        types = [resource_type] if resource_type in ("product", "collection") else ["product", "collection"]
        total = 0
        for rtype in types:
            items = _load_items(store_id, rtype)
            _score_items(store_id, rtype, items, base_url, crawlability, base_url)
            total += len(items)

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'completed', result = %s, completed_at = NOW() WHERE id = %s",
                (json.dumps({"scanned": total, "crawlability": crawlability}), task_id),
            )
    except Exception as e:
        logger.exception("AI SEO scan failed: %s", e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tasks SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), task_id),
            )


@ai_seo_bp.route("/ai-seo")
def ai_seo_page():
    return render_template("ai_seo.html")


@ai_seo_bp.route("/api/ai-seo/scan", methods=["POST"])
def start_scan():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "all")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tasks (store_id, task_type, input) VALUES (%s, 'ai_seo_scan', %s) RETURNING id",
            (store_id, json.dumps({"type": resource_type})),
        )
        task_id = cur.fetchone()["id"]

    threading.Thread(
        target=_execute_scan, args=(task_id, int(store_id), resource_type), daemon=True
    ).start()
    return jsonify({"ok": True, "task_id": task_id}), 201


@ai_seo_bp.route("/api/ai-seo/scan/<int:task_id>", methods=["GET"])
def scan_status(task_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tasks WHERE id = %s", (task_id,))
        task = cur.fetchone()
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(
        {
            "id": task["id"],
            "status": task["status"],
            "result": task["result"],
            "error": task["error"],
        }
    )


@ai_seo_bp.route("/api/ai-seo/overview", methods=["GET"])
def overview():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    store_id = int(store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return jsonify({"error": "Store not found"}), 404

    base_url = _store_base_url(store)
    robots, llms_present = _fetch_store_crawlability(base_url)
    crawl_score, crawl_detail = ai_seo_analyzer.analyze_store_crawlability(robots, llms_present)

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT resource_type, COUNT(*) AS n, COALESCE(AVG(score), 0) AS avg_score
            FROM ai_seo_scores WHERE store_id = %s GROUP BY resource_type
            """,
            (store_id,),
        )
        by_type = {r["resource_type"]: {"count": int(r["n"]), "avg_score": round(r["avg_score"])} for r in cur.fetchall()}
        cur.execute(
            "SELECT COUNT(*) AS n, COALESCE(AVG(score), 0) AS avg_score FROM ai_seo_scores WHERE store_id = %s",
            (store_id,),
        )
        totals = cur.fetchone()

    return jsonify(
        {
            "crawlability": {"score": crawl_score, "detail": crawl_detail},
            "llms_txt": llms_present,
            "base_url": base_url,
            "scanned_count": int(totals["n"]),
            "avg_score": round(totals["avg_score"]),
            "by_type": by_type,
        }
    )


@ai_seo_bp.route("/api/ai-seo/items", methods=["GET"])
def list_items():
    store_id = request.args.get("store_id")
    resource_type = request.args.get("type", "product")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    table = "synced_collections" if resource_type == "collection" else "synced_products"

    with db.get_cursor() as cur:
        cur.execute(
            f"""
            SELECT s.resource_id, s.score, s.breakdown, s.created_at,
                   i.title, i.handle
            FROM ai_seo_scores s
            JOIN {table} i ON i.id = s.resource_id
            WHERE s.store_id = %s AND s.resource_type = %s
            ORDER BY s.score ASC
            """,
            (int(store_id), resource_type),
        )
        rows = cur.fetchall()

    return jsonify(
        [
            {
                "resource_id": r["resource_id"],
                "title": r["title"],
                "handle": r["handle"],
                "score": r["score"],
                "breakdown": r["breakdown"],
                "scanned_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    )


# ── Phase 2: review-first GEO optimization ──────────────────────────────


def _weak_signals(breakdown, n=3):
    """Return the n lowest-scoring components as 'Label: detail' strings."""
    if not breakdown:
        return []
    ranked = sorted(
        ((k, d) for k, d in breakdown.items() if k in COMPONENT_LABELS),
        key=lambda kv: kv[1].get("score", 0),
    )
    return [f"{COMPONENT_LABELS[k]}: {d.get('detail', '')}" for k, d in ranked[:n]]


def _crawlability_from_breakdown(breakdown):
    """Reuse the crawlability sub-score captured during the last scan."""
    crawl = (breakdown or {}).get("crawlability")
    return (crawl["score"], crawl["detail"]) if crawl else None


def _stored_score(store_id, resource_type, resource_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT score, breakdown, live_crawl FROM ai_seo_scores WHERE store_id = %s AND resource_type = %s AND resource_id = %s",
            (store_id, resource_type, resource_id),
        )
        return cur.fetchone()


def _rescore_item(store_id, resource_type, item, live, crawlability, store_domain):
    markup = _pushed_schema(store_id).get(str(item.get("shopify_id")), [])
    result = ai_seo_analyzer.analyze_ai_visibility(
        {**item, "resource_type": resource_type},
        live=live,
        schema_markup=markup,
        crawlability=crawlability,
        store_domain=store_domain,
    )
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_seo_scores (store_id, resource_type, resource_id, score, breakdown, live_crawl, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, resource_type, resource_id) DO UPDATE SET
                score = EXCLUDED.score, breakdown = EXCLUDED.breakdown, created_at = NOW()
            """,
            (
                store_id,
                resource_type,
                item["id"],
                result["score"],
                json.dumps(result["breakdown"]),
                json.dumps(live) if live else None,
            ),
        )
    return result


@ai_seo_bp.route("/api/ai-seo/optimize/preview", methods=["POST"])
def optimize_preview():
    from app.ai_agent import run_task

    data = request.get_json() or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    resource_id = data.get("id")
    if not store_id or not resource_id:
        return jsonify({"error": "store_id and id are required"}), 400

    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (resource_id,))
        item = cur.fetchone()
    if not item:
        return jsonify({"error": "Item not found"}), 404
    item = dict(item)
    store_domain = _store_base_url(dict(store)) if store else None

    stored = _stored_score(int(store_id), resource_type, resource_id)
    before_breakdown = stored["breakdown"] if stored else None
    if before_breakdown is None:
        before = ai_seo_analyzer.analyze_ai_visibility({**item, "resource_type": resource_type}, store_domain=store_domain)
        before_breakdown, before_score = before["breakdown"], before["score"]
    else:
        before_score = stored["score"]

    task_type = "ai_seo_collection" if resource_type == "collection" else "ai_seo_product"
    id_key = "collection_id" if resource_type == "collection" else "product_id"
    geo = run_task(
        task_type,
        {id_key: resource_id, "weak_signals": _weak_signals(before_breakdown)},
        store_id,
    )
    if geo.get("error"):
        return jsonify({"error": geo["error"]}), 502

    # Honest projection: reuse the real crawl schema, add the schema we'd push (FAQPage
    # plus the enriched Product/CollectionPage JSON-LD applied alongside the content).
    live_after = dict((stored or {}).get("live_crawl") or {}) if stored else {}
    schema_after = list(live_after.get("schema_types") or [])
    if geo.get("faqpage_jsonld") and "FAQPage" not in schema_after:
        schema_after.append("FAQPage")
    pushed_type = "CollectionPage" if resource_type == "collection" else "Product"
    if pushed_type not in schema_after:
        schema_after.append(pushed_type)
    live_after["schema_types"] = schema_after
    # Crawlability is store-level (robots.txt/llms.txt) — carry it over unchanged so
    # content optimization never shows a spurious crawlability delta.
    after = ai_seo_analyzer.analyze_ai_visibility(
        {**item, "description": geo["optimized_description_html"], "resource_type": resource_type},
        live=live_after if schema_after else None,
        crawlability=_crawlability_from_breakdown(before_breakdown),
        store_domain=store_domain,
    )

    return jsonify(
        {
            "preview": geo,
            "item": {"id": item["id"], "shopify_id": item.get("shopify_id"), "title": item.get("title")},
            "before_score": before_score,
            "before_breakdown": before_breakdown,
            "after_score": after["score"],
            "after_breakdown": after["breakdown"],
        }
    )


def _push_and_rescore(store, resource_type, item, description_html, meta_title, meta_description, faqpage, client):
    """Push GEO content to Shopify, persist locally, and rescore the item.

    Shared by single-item apply and batch optimize. Raises on Shopify errors.
    """
    store_id = store["id"]
    resource_id = item["id"]
    shopify_id = item["shopify_id"]

    fields = {}
    if description_html:
        fields["description_html"] = description_html
    if meta_title:
        fields["meta_title"] = meta_title
    if meta_description:
        fields["meta_description"] = meta_description

    if fields:
        if resource_type == "collection":
            client.update_collection(shopify_id, fields)
        else:
            client.update_product(shopify_id, fields)
    if faqpage:
        client.set_product_metafield(shopify_id, "seo", "faq_schema_json_ld", json.dumps(faqpage), "json")
        with db.get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, pushed_to_shopify, pushed_at)
                VALUES (%s, %s, %s, 'FAQPage', %s, TRUE, TRUE, NOW())
                ON CONFLICT (store_id, resource_id, schema_type) DO UPDATE
                SET schema_json = EXCLUDED.schema_json, pushed_to_shopify = TRUE, pushed_at = NOW()
                """,
                (store_id, str(shopify_id), resource_type, json.dumps(faqpage)),
            )

    with db.get_cursor() as cur:
        if resource_type == "collection":
            cur.execute(
                "UPDATE synced_collections SET description = %s, meta_title = %s, meta_description = %s WHERE id = %s",
                (description_html or item.get("description"), meta_title or item.get("meta_title"), meta_description or item.get("meta_description"), resource_id),
            )
        else:
            cur.execute(
                "UPDATE synced_products SET description = %s, meta_title = %s, meta_description = %s, last_optimized_at = NOW() WHERE id = %s",
                (description_html or item.get("description"), meta_title or item.get("meta_title"), meta_description or item.get("meta_description"), resource_id),
            )

    stored = _stored_score(store_id, resource_type, resource_id)
    updated_item = {**item, "description": description_html or item.get("description"), "meta_title": meta_title, "meta_description": meta_description}

    # Also generate & push enriched Product / CollectionPage JSON-LD (seo.schema_json_ld) —
    # the structured-data signal AI answer engines reward. Best-effort: a schema failure
    # must not undo the content/meta/FAQ that already applied successfully.
    try:
        base = store["domain"] if store["domain"].endswith(".myshopify.com") else f"{store['domain']}.myshopify.com"
        if resource_type == "collection":
            from app.routes.schema_routes import _get_org_settings
            from app.services.schema_generator import generate_collection_schema
            col_schema = generate_collection_schema(updated_item, base, _get_org_settings(store_id))
            client.set_product_metafield(shopify_id, "seo", "schema_json_ld", json.dumps(col_schema), "json")
            with db.get_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO schema_markup (store_id, resource_id, resource_type, schema_type, schema_json, is_valid, pushed_to_shopify, pushed_at)
                    VALUES (%s, %s, 'collection', 'CollectionPage', %s, TRUE, TRUE, NOW())
                    ON CONFLICT (store_id, resource_id, schema_type) DO UPDATE
                    SET schema_json = EXCLUDED.schema_json, pushed_to_shopify = TRUE, pushed_at = NOW()
                    """,
                    (store_id, str(shopify_id), json.dumps(col_schema)),
                )
        else:
            from app.routes.schema_routes import generate_and_push_product_schema
            generate_and_push_product_schema(resource_id, client)
    except Exception as e:
        logger.warning("Schema JSON-LD push failed for %s %s: %s", resource_type, resource_id, e)

    return _rescore_item(
        store_id,
        resource_type,
        updated_item,
        (stored or {}).get("live_crawl"),
        _crawlability_from_breakdown((stored or {}).get("breakdown")),
        _store_base_url(store),
    )


@ai_seo_bp.route("/api/ai-seo/optimize/apply", methods=["POST"])
def optimize_apply():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    resource_id = data.get("id")
    if not store_id or not resource_id:
        return jsonify({"error": "store_id and id are required"}), 400

    table = "synced_collections" if resource_type == "collection" else "synced_products"
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (resource_id,))
        item = cur.fetchone()
    if not store or not item:
        return jsonify({"error": "Store or item not found"}), 404
    store, item = dict(store), dict(item)

    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    try:
        result = _push_and_rescore(
            store, resource_type, item,
            data.get("description_html", ""), data.get("meta_title", ""),
            data.get("meta_description", ""), data.get("faqpage_jsonld"), client,
        )
    except Exception as e:
        logger.exception("GEO apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "new_score": result["score"], "breakdown": result["breakdown"]})


# ── Phase 2b: batch GEO optimization ────────────────────────────────────


def _execute_ai_seo_batch(batch_id, store_id, resource_type, resource_ids):
    from app.ai_agent import run_task

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'running' WHERE id = %s", (batch_id,))
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET status = 'failed', completed_at = NOW() WHERE id = %s", (batch_id,))
        return
    store = dict(store)
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    table = "synced_collections" if resource_type == "collection" else "synced_products"
    task_type = "ai_seo_collection" if resource_type == "collection" else "ai_seo_product"
    id_key = "collection_id" if resource_type == "collection" else "product_id"
    item_col = "collection_id" if resource_type == "collection" else "product_id"

    completed = failed = 0
    for rid in resource_ids:
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id = %s", (rid,))
            item = cur.fetchone()
        if not item:
            failed += 1
            continue
        item = dict(item)
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET current_product_title = %s WHERE id = %s", (item.get("title"), batch_id))
        try:
            stored = _stored_score(store_id, resource_type, rid)
            geo = run_task(task_type, {id_key: rid, "weak_signals": _weak_signals((stored or {}).get("breakdown"))}, store_id)
            if geo.get("error"):
                raise ValueError(geo["error"])
            _push_and_rescore(
                store, resource_type, item,
                geo.get("optimized_description_html", ""), geo.get("meta_title", ""),
                geo.get("meta_description", ""), geo.get("faqpage_jsonld"), client,
            )
            completed += 1
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'completed', ai_result = %s, completed_at = NOW() WHERE bulk_task_id = %s AND {item_col} = %s",
                    (
                        json.dumps({
                            "geo_edits_applied": geo.get("geo_edits_applied", []),
                            "preserved_keywords": geo.get("preserved_keywords", []),
                            "dropped_keywords": geo.get("dropped_keywords", []),
                        }),
                        batch_id, rid,
                    ),
                )
        except Exception as e:
            failed += 1
            logger.exception("Batch GEO item %s failed: %s", rid, e)
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE bulk_task_items SET status = 'failed', error = %s, completed_at = NOW() WHERE bulk_task_id = %s AND {item_col} = %s",
                    (str(e), batch_id, rid),
                )
        with db.get_cursor() as cur:
            cur.execute("UPDATE bulk_tasks SET completed = %s, failed = %s WHERE id = %s", (completed, failed, batch_id))

    with db.get_cursor() as cur:
        cur.execute("UPDATE bulk_tasks SET status = 'completed', completed_at = NOW(), current_product_title = NULL WHERE id = %s", (batch_id,))


def _sweep_stale_jobs():
    """Fail any background job that's been 'running' far longer than possible —
    catches daemons killed without a clean restart. Runs cheaply on job start/poll."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE bulk_tasks SET status='failed', completed_at=NOW() "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '6 hours'"
            )
            cur.execute(
                "UPDATE ai_tracking_runs SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND started_at < NOW() - INTERVAL '2 hours'"
            )
            cur.execute(
                "UPDATE ai_tasks SET status='failed', error=COALESCE(error,'Timed out'), completed_at=NOW() "
                "WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '2 hours'"
            )
    except Exception:
        pass


@ai_seo_bp.route("/api/ai-seo/optimize/batch", methods=["POST"])
def optimize_batch():
    _sweep_stale_jobs()
    data = request.get_json() or {}
    store_id = data.get("store_id")
    resource_type = data.get("type", "product")
    ids = data.get("ids")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    # "all" → every scanned item of this type for the store.
    if ids in (None, "all", []):
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT resource_id FROM ai_seo_scores WHERE store_id = %s AND resource_type = %s ORDER BY score ASC",
                (int(store_id), resource_type),
            )
            ids = [r["resource_id"] for r in cur.fetchall()]
    ids = [int(i) for i in ids]
    if not ids:
        return jsonify({"error": "No items to optimize"}), 400

    item_col = "collection_id" if resource_type == "collection" else "product_id"
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply) VALUES ('pending', %s, TRUE) RETURNING id",
            (len(ids),),
        )
        batch_id = cur.fetchone()["id"]
        for rid in ids:
            cur.execute(
                f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                (batch_id, rid),
            )

    threading.Thread(
        target=_execute_ai_seo_batch, args=(batch_id, int(store_id), resource_type, ids), daemon=True
    ).start()
    return jsonify({"ok": True, "batch_id": batch_id, "total": len(ids)}), 201


@ai_seo_bp.route("/api/ai-seo/optimize/batch/<int:batch_id>", methods=["GET"])
def optimize_batch_status(batch_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT status, total, completed, failed, current_product_title FROM bulk_tasks WHERE id = %s", (batch_id,))
        row = cur.fetchone()
    if not row:
        return jsonify({"error": "Batch not found"}), 404
    return jsonify(
        {
            "status": row["status"],
            "total": row["total"],
            "completed": row["completed"],
            "failed": row["failed"],
            "current": row["current_product_title"],
        }
    )


# ── Phase 3: AI visibility tracking ─────────────────────────────────────


@ai_seo_bp.route("/api/ai-seo/prompts", methods=["GET"])
def list_prompts():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, prompt, intent, is_active FROM ai_prompts WHERE store_id = %s ORDER BY id",
            (int(store_id),),
        )
        return jsonify([dict(r) for r in cur.fetchall()])


@ai_seo_bp.route("/api/ai-seo/prompts", methods=["POST"])
def manage_prompts():
    from app.services.ai_visibility_tracker import generate_prompt_set

    data = request.get_json() or {}
    action = data.get("action")
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400

    if action == "generate":
        try:
            saved = generate_prompt_set(int(store_id))
        except Exception as e:
            logger.exception("Prompt generation failed: %s", e)
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "prompts": saved})

    if action == "add":
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt is required"}), 400
        with db.get_cursor() as cur:
            cur.execute(
                "INSERT INTO ai_prompts (store_id, prompt, intent) VALUES (%s, %s, 'buyer') RETURNING id",
                (int(store_id), prompt),
            )
            pid = cur.fetchone()["id"]
        return jsonify({"ok": True, "id": pid})

    if action == "delete":
        with db.get_cursor() as cur:
            cur.execute(
                "DELETE FROM ai_prompts WHERE id = %s AND store_id = %s",
                (data.get("prompt_id"), int(store_id)),
            )
        return jsonify({"ok": True})

    return jsonify({"error": "Unknown action"}), 400


@ai_seo_bp.route("/api/ai-seo/tracking/run", methods=["POST"])
def tracking_run():
    from app.services.ai_visibility_tracker import start_tracking_run

    _sweep_stale_jobs()
    data = request.get_json() or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    run_id = start_tracking_run(int(store_id), data.get("models"))
    return jsonify({"ok": True, "run_id": run_id}), 201


@ai_seo_bp.route("/api/ai-seo/tracking/runs", methods=["GET"])
def tracking_runs():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT id, status, models, prompt_count, brand_mention_rate, citation_rate,
                   share_of_voice, avg_rank, cost_estimate, summary, error, started_at, completed_at
            FROM ai_tracking_runs WHERE store_id = %s ORDER BY started_at DESC LIMIT 30
            """,
            (int(store_id),),
        )
        rows = cur.fetchall()
    return jsonify(
        [
            {
                **{k: r[k] for k in (
                    "id", "status", "models", "prompt_count", "brand_mention_rate",
                    "citation_rate", "share_of_voice", "avg_rank", "cost_estimate", "summary", "error",
                )},
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ]
    )


@ai_seo_bp.route("/api/ai-seo/tracking/<int:run_id>", methods=["GET"])
def tracking_detail(run_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            return jsonify({"error": "Run not found"}), 404
        cur.execute(
            """
            SELECT model, prompt, brand_mentioned, domain_cited, brand_rank,
                   products_mentioned, competitors_mentioned
            FROM ai_mention_results WHERE run_id = %s ORDER BY id
            """,
            (run_id,),
        )
        results = [dict(r) for r in cur.fetchall()]
    # Compute live so excluded brands drop out immediately (stored summary is a snapshot).
    from app.services.ai_visibility_tracker import compute_share_of_voice
    agg = compute_share_of_voice(run_id)
    return jsonify(
        {
            "id": run["id"],
            "status": run["status"],
            "share_of_voice": agg["share_of_voice"],
            "brand_mention_rate": agg["brand_mention_rate"],
            "citation_rate": agg["citation_rate"],
            "avg_rank": agg["avg_rank"],
            "brand_known_rate": agg["brand_known_rate"],
            "competitors": agg["competitors"],
            "gap_analysis": run["gap_analysis"],
            "results": results,
        }
    )


@ai_seo_bp.route("/api/ai-seo/tracking/competitor", methods=["POST"])
def add_competitor_from_results():
    """Add an AI-recommended brand to the store's competitor list."""
    import re as _re

    data = request.get_json() or {}
    store_id = data.get("store_id")
    name = (data.get("name") or "").strip()
    domain_in = (data.get("domain") or "").strip().lower()
    if not store_id or not name:
        return jsonify({"error": "store_id and name are required"}), 400
    # Prefer the real domain when we have one; else use the name if it's a domain, else slugify.
    if domain_in:
        domain, display = domain_in, name
    elif _re.match(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", name, _re.I):
        domain, display = name.lower(), name
    else:
        domain = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or name.lower()
        display = name
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO competitors (store_id, domain, name, notes)
                   VALUES (%s, %s, %s, 'Added from AI visibility results')
                   ON CONFLICT (store_id, domain) DO NOTHING""",
                (int(store_id), domain, display),
            )
    except Exception as e:
        logger.exception("Add competitor failed: %s", e)
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "domain": domain, "name": display})


@ai_seo_bp.route("/api/ai-seo/tracking/exclude", methods=["POST"])
def exclude_brand_from_results():
    """Exclude an AI-recommended brand (by canonical key) from future results."""
    from app.services.ai_visibility_tracker import _canonical_brand_key

    data = request.get_json() or {}
    store_id = data.get("store_id")
    name = (data.get("name") or "").strip()
    key = (data.get("key") or "").strip() or _canonical_brand_key(data.get("domain") or name)
    if not store_id or not key:
        return jsonify({"error": "store_id and key/name are required"}), 400
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO ai_excluded_brands (store_id, brand_key, name)
               VALUES (%s, %s, %s) ON CONFLICT (store_id, brand_key) DO NOTHING""",
            (int(store_id), key, name or key),
        )
    return jsonify({"ok": True, "key": key})


@ai_seo_bp.route("/api/ai-seo/tracking/<int:run_id>/gap-analysis", methods=["POST"])
def tracking_gap_analysis(run_id):
    from app.services.ai_visibility_tracker import analyze_competitive_gap

    try:
        analysis = analyze_competitive_gap(run_id)
    except Exception as e:
        logger.exception("Gap analysis failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "gap_analysis": analysis})


# ── Phase 4: storefront AEO assets ──────────────────────────────────────


def _client_for_store(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (int(store_id),))
        store = cur.fetchone()
    if not store:
        return None, None
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    return client, store


def _save_aeo_asset(store_id, asset_type, content, status="draft"):
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO aeo_assets (store_id, asset_type, content, status, published_at, updated_at)
            VALUES (%s, %s, %s, %s, CASE WHEN %s = 'published' THEN NOW() ELSE NULL END, NOW())
            ON CONFLICT (store_id, asset_type) DO UPDATE SET
                content = EXCLUDED.content, status = EXCLUDED.status,
                published_at = EXCLUDED.published_at, updated_at = NOW()
            """,
            (store_id, asset_type, content, status, status),
        )


def _crawl_score(num_allowed, total, blocks_sensitive, llms_present):
    """Mirror ai_seo_analyzer.analyze_store_crawlability scoring for impact estimates."""
    score = round(num_allowed / total * 70) if total else 0
    if llms_present:
        score += 15
    if blocks_sensitive:
        score += 15
    return min(score, 100)


def _current_robots_state(base_url):
    robots = _fetch_text(f"{base_url}/robots.txt") if base_url else None
    if not robots:
        return {"present": False, "detail": "Could not fetch /robots.txt"}
    rules = ai_seo_analyzer._parse_robots_ai_rules(robots)
    allowed = [b for b, ok in rules["bots"].items() if ok]
    blocked = [b for b, ok in rules["bots"].items() if not ok]
    return {
        "present": True,
        "ai_bots_allowed": allowed,
        "ai_bots_blocked": blocked,
        "ai_bots_total": len(rules["bots"]),
        "blocks_sensitive": rules["blocks_sensitive"],
        "raw": robots[:4000],
    }


def _current_llms_state(base_url):
    if not base_url:
        return {"present": False}
    root = _fetch_text(f"{base_url}/llms.txt")
    page = _fetch_text(f"{base_url}/pages/llms")
    if root and "<html" not in root[:200].lower():
        return {"present": True, "location": "root", "url": f"{base_url}/llms.txt", "raw": root[:4000]}
    if page:
        return {"present": True, "location": "page", "url": f"{base_url}/pages/llms"}
    return {"present": False}


@ai_seo_bp.route("/api/ai-seo/aeo/preview", methods=["GET"])
def aeo_preview():
    from app.services import aeo_asset_generator as gen

    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    store_id = int(store_id)

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone()
        cur.execute("SELECT asset_type, status, published_at FROM aeo_assets WHERE store_id = %s", (store_id,))
        published = {r["asset_type"]: r["status"] for r in cur.fetchall()}
    if not store:
        return jsonify({"error": "Store not found"}), 404

    base_url = _store_base_url(store)
    org = gen.generate_organization_jsonld(store_id)
    org_configured = bool(settings and (settings.get("organization_name") or settings.get("organization_logo_url") or settings.get("organization_url")))

    robots_state = _current_robots_state(base_url)
    llms_state = _current_llms_state(base_url)
    llms_present = bool(llms_state.get("present"))

    # Crawlability signal: current vs projected (mirrors analyzer scoring).
    total = robots_state.get("ai_bots_total") or len(ai_seo_analyzer.AI_BOTS)
    cur_allowed = len(robots_state.get("ai_bots_allowed") or []) if robots_state.get("present") else total
    cur_sensitive = robots_state.get("blocks_sensitive", True)
    crawl_current = _crawl_score(cur_allowed, total, cur_sensitive, llms_present)
    crawl_after_robots = _crawl_score(total, total, True, llms_present)  # proposed: all allowed + sensitive blocked
    crawl_after_llms = _crawl_score(cur_allowed, total, cur_sensitive, True)  # proposed: llms.txt present

    # Entity-authority signal: current store average from scanned items.
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT AVG((breakdown->'entity_authority'->>'score')::numeric) a FROM ai_seo_scores WHERE store_id = %s",
            (store_id,),
        )
        row = cur.fetchone()
    ea_avg = round(float(row["a"])) if row and row["a"] is not None else None

    return jsonify(
        {
            "base_url": base_url,
            "robots": {
                "proposed": gen.generate_robots_liquid(),
                "current": robots_state,
                "published_status": published.get("robots_txt"),
                "impact": {"signal": "AI crawlability", "current": crawl_current, "projected": crawl_after_robots},
            },
            "llms": {
                "proposed": gen.generate_llms_txt(store_id),
                "current": llms_state,
                "published_status": published.get("llms_txt"),
                "impact": {"signal": "AI crawlability", "current": crawl_current, "projected": crawl_after_llms},
            },
            "organization": {
                "proposed": org,
                "current": {
                    "name": (settings and settings.get("organization_name")) or "",
                    "url": (settings and settings.get("organization_url")) or "",
                    "logo": (settings and settings.get("organization_logo_url")) or "",
                    "configured": org_configured,
                },
                "published_status": published.get("org_schema"),
                "impact": {"signal": "Entity authority", "current_avg": ea_avg, "uplift": 30},
            },
        }
    )


@ai_seo_bp.route("/api/ai-seo/aeo/robots/apply", methods=["POST"])
def aeo_robots_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json() or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    content = gen.generate_robots_liquid()
    try:
        theme_id = client.get_main_theme_id()
        if not theme_id:
            return jsonify({"error": "No main theme found"}), 502
        client.upsert_theme_asset(theme_id, "templates/robots.txt.liquid", content)
        _save_aeo_asset(int(store_id), "robots_txt", content, "published")
    except Exception as e:
        logger.exception("robots.txt apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "message": "robots.txt.liquid published to theme"})


@ai_seo_bp.route("/api/ai-seo/aeo/llms/apply", methods=["POST"])
def aeo_llms_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json() or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    client, store = _client_for_store(store_id)
    if not client:
        return jsonify({"error": "Store not found"}), 404
    content = gen.generate_llms_txt(int(store_id))
    try:
        page = client.create_page(
            title="llms.txt",
            body_html=f"<pre>{content}</pre>",
            handle="llms",
            seo_title="llms.txt",
            seo_description="AI crawler content index",
        )
        _save_aeo_asset(int(store_id), "llms_txt", content, "published")
    except Exception as e:
        logger.exception("llms.txt page apply failed: %s", e)
        return jsonify({"error": str(e)}), 502
    return jsonify(
        {
            "ok": True,
            "message": "Published to /pages/llms. Note: Shopify cannot serve /llms.txt at the domain root — "
            "download the file below to host it at the root via your CDN/proxy if desired.",
            "page": page,
        }
    )


@ai_seo_bp.route("/api/ai-seo/aeo/schema/apply", methods=["POST"])
def aeo_schema_apply():
    from app.services import aeo_asset_generator as gen

    data = request.get_json() or {}
    store_id = data.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id is required"}), 400
    org = gen.generate_organization_jsonld(int(store_id))
    if not org:
        return jsonify({"error": "Configure Organization details in Schema settings first"}), 400
    _save_aeo_asset(int(store_id), "org_schema", json.dumps(org), "draft")
    return jsonify(
        {
            "ok": True,
            "organization_jsonld": org,
            "message": "Organization JSON-LD generated. Add it site-wide via a theme snippet "
            "(layout/theme.liquid) — copy the snippet below into your theme.",
        }
    )
