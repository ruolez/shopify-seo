import json
import logging
import re

from app.database import db

logger = logging.getLogger(__name__)

PRODUCT_PATH_RE = re.compile(r"/products/([\w\-]+)")
COLLECTION_PATH_RE = re.compile(r"/collections/([\w\-]+)")
LINK_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def extract_links_from_html(html, store_domain=None):
    if not html:
        return []

    links = []
    for match in LINK_RE.finditer(html):
        href = match.group(1).strip()
        anchor_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()

        if store_domain and href.startswith(("http://", "https://")):
            domain_pattern = store_domain.replace(".", r"\.")
            href = re.sub(
                rf"https?://(?:www\.)?{domain_pattern}", "", href
            )

        link_type = None
        handle = None

        product_match = PRODUCT_PATH_RE.search(href)
        collection_match = COLLECTION_PATH_RE.search(href)

        if product_match:
            link_type = "product"
            handle = product_match.group(1)
        elif collection_match:
            link_type = "collection"
            handle = collection_match.group(1)

        if link_type and handle:
            links.append({
                "href": href,
                "handle": handle,
                "anchor_text": anchor_text,
                "link_type": link_type,
            })

    return links


def compute_interlinking_score(incoming_count, outgoing_count, in_collection):
    if incoming_count >= 3:
        return 100
    if incoming_count >= 1:
        return 60
    if in_collection:
        return 30
    return 0


def find_missing_crosslinks(collections, products_by_collection, link_graph):
    suggestions = []
    coll_list = list(collections)

    for i in range(len(coll_list)):
        for j in range(i + 1, len(coll_list)):
            coll_a = coll_list[i]
            coll_b = coll_list[j]

            handle_a = coll_a["handle"]
            handle_b = coll_b["handle"]

            a_links_to_b = handle_b in link_graph.get(f"collection:{handle_a}", set())
            b_links_to_a = handle_a in link_graph.get(f"collection:{handle_b}", set())

            if a_links_to_b and b_links_to_a:
                continue

            tags_a = set(t.strip().lower() for t in (coll_a.get("tags") or "").split(",") if t.strip())
            tags_b = set(t.strip().lower() for t in (coll_b.get("tags") or "").split(",") if t.strip())

            prods_a = products_by_collection.get(coll_a.get("shopify_id"), set())
            prods_b = products_by_collection.get(coll_b.get("shopify_id"), set())

            shared_tags = tags_a & tags_b
            shared_products = prods_a & prods_b

            if len(shared_tags) >= 2 or len(shared_products) >= 3:
                suggestions.append({
                    "collection_a_title": coll_a["title"],
                    "collection_a_handle": handle_a,
                    "collection_b_title": coll_b["title"],
                    "collection_b_handle": handle_b,
                    "shared_tags": len(shared_tags),
                    "shared_products": len(shared_products),
                    "a_links_to_b": a_links_to_b,
                    "b_links_to_a": b_links_to_a,
                })

    return suggestions


def analyze_store_interlinking(store_id, store_domain=None):
    logger.info("Starting interlinking analysis for store %s", store_id)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_id, title, handle, description, tags FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
            (store_id,),
        )
        products = [dict(r) for r in cur.fetchall()]

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_id, title, handle, description FROM synced_collections WHERE store_id = %s",
            (store_id,),
        )
        collections = [dict(r) for r in cur.fetchall()]

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT collection_shopify_id, product_shopify_id FROM collection_product_map WHERE store_id = %s",
            (store_id,),
        )
        mappings = cur.fetchall()

    products_by_collection = {}
    product_collections = {}
    for m in mappings:
        cid = m["collection_shopify_id"]
        pid = m["product_shopify_id"]
        products_by_collection.setdefault(cid, set()).add(pid)
        product_collections.setdefault(pid, set()).add(cid)

    product_by_handle = {p["handle"]: p for p in products}
    collection_by_handle = {c["handle"]: c for c in collections}

    link_graph = {}
    all_links = []

    incoming_counts = {}
    outgoing_counts = {}

    for p in products:
        key = f"product:{p['handle']}"
        links = extract_links_from_html(p.get("description"), store_domain)
        targets = set()
        for link in links:
            target_key = f"{link['link_type']}:{link['handle']}"
            targets.add(link["handle"])
            incoming_counts[target_key] = incoming_counts.get(target_key, 0) + 1
            all_links.append({
                "from_type": "product",
                "from_handle": p["handle"],
                "from_title": p["title"],
                "to_type": link["link_type"],
                "to_handle": link["handle"],
                "anchor_text": link["anchor_text"],
            })
        link_graph[key] = targets
        outgoing_counts[key] = len(links)

    for c in collections:
        key = f"collection:{c['handle']}"
        links = extract_links_from_html(c.get("description"), store_domain)
        targets = set()
        for link in links:
            target_key = f"{link['link_type']}:{link['handle']}"
            targets.add(link["handle"])
            incoming_counts[target_key] = incoming_counts.get(target_key, 0) + 1
            all_links.append({
                "from_type": "collection",
                "from_handle": c["handle"],
                "from_title": c["title"],
                "to_type": link["link_type"],
                "to_handle": link["handle"],
                "anchor_text": link["anchor_text"],
            })
        link_graph[key] = targets
        outgoing_counts[key] = len(links)

    orphan_products = []
    orphan_collections = []
    under_linked = []
    product_details = []
    collection_details = []

    for p in products:
        key = f"product:{p['handle']}"
        inc = incoming_counts.get(key, 0)
        out = outgoing_counts.get(key, 0)
        in_collection = p["shopify_id"] in product_collections
        score = compute_interlinking_score(inc, out, in_collection)

        detail = {
            "id": p["id"],
            "shopify_id": p["shopify_id"],
            "title": p["title"],
            "handle": p["handle"],
            "incoming_count": inc,
            "outgoing_count": out,
            "in_collection": in_collection,
            "collection_count": len(product_collections.get(p["shopify_id"], set())),
            "score": score,
        }
        product_details.append(detail)

        if inc == 0 and not in_collection:
            orphan_products.append(detail)
        elif inc < 2 and inc > 0:
            under_linked.append(detail)

    for c in collections:
        key = f"collection:{c['handle']}"
        inc = incoming_counts.get(key, 0)
        out = outgoing_counts.get(key, 0)
        score = compute_interlinking_score(inc, out, True)

        detail = {
            "id": c["id"],
            "shopify_id": c["shopify_id"],
            "title": c["title"],
            "handle": c["handle"],
            "incoming_count": inc,
            "outgoing_count": out,
            "score": score,
        }
        collection_details.append(detail)

        if inc == 0:
            orphan_collections.append(detail)
        elif inc < 2:
            under_linked.append(detail)

    missing_crosslinks = find_missing_crosslinks(
        collections, products_by_collection, link_graph
    )

    not_in_collection = [
        d for d in product_details if not d["in_collection"]
    ]

    total_links = len(all_links)
    all_items = product_details + collection_details
    avg_incoming = (
        round(sum(d["incoming_count"] for d in all_items) / len(all_items), 1)
        if all_items else 0
    )
    all_scores = [d["score"] for d in all_items]
    avg_score = round(sum(all_scores) / len(all_scores)) if all_scores else 0

    score_distribution = {
        "excellent": sum(1 for s in all_scores if s >= 80),
        "good": sum(1 for s in all_scores if 50 <= s < 80),
        "poor": sum(1 for s in all_scores if 1 <= s < 50),
        "orphan": sum(1 for s in all_scores if s == 0),
    }

    analysis = {
        "summary": {
            "total_links": total_links,
            "total_products": len(products),
            "total_collections": len(collections),
            "orphan_products_count": len(orphan_products),
            "orphan_collections_count": len(orphan_collections),
            "under_linked_count": len(under_linked),
            "not_in_collection_count": len(not_in_collection),
            "avg_incoming_links": avg_incoming,
            "avg_score": avg_score,
            "score_distribution": score_distribution,
            "missing_crosslinks_count": len(missing_crosslinks),
        },
        "orphan_products": orphan_products,
        "orphan_collections": orphan_collections,
        "under_linked": sorted(under_linked, key=lambda x: x["incoming_count"]),
        "not_in_collection": not_in_collection,
        "missing_crosslinks": missing_crosslinks,
        "all_links": all_links[:200],
        "product_details": product_details,
        "collection_details": collection_details,
    }

    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO interlinking_analysis (store_id, analysis_data, created_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (store_id) DO UPDATE SET
                analysis_data = EXCLUDED.analysis_data,
                created_at = NOW()
            """,
            (store_id, json.dumps(analysis)),
        )

    logger.info(
        "Interlinking analysis complete for store %s: %d links, %d orphan products, %d orphan collections",
        store_id, total_links, len(orphan_products), len(orphan_collections),
    )

    return analysis
