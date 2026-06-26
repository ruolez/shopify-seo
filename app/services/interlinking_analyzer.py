import json
import logging
import re

from app.database import db
from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS

logger = logging.getLogger(__name__)

PRODUCT_PATH_RE = re.compile(r"/products/([\w\-]+)")
COLLECTION_PATH_RE = re.compile(r"/collections/([\w\-]+)")
ARTICLE_PATH_RE = re.compile(r"/blogs/([\w\-]+)/([\w\-]+)")
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
        article_match = ARTICLE_PATH_RE.search(href)

        if product_match:
            link_type = "product"
            handle = product_match.group(1)
        elif article_match:
            link_type = "article"
            handle = article_match.group(2)
        elif collection_match:
            link_type = "collection"
            handle = collection_match.group(1)

        if link_type and handle:
            link_data = {
                "href": href,
                "handle": handle,
                "anchor_text": anchor_text,
                "link_type": link_type,
            }
            if link_type == "article":
                link_data["blog_handle"] = article_match.group(1)
            links.append(link_data)

    return links


def compute_interlinking_score(incoming_count, outgoing_count, in_collection):
    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    t = rules.get("score_thresholds", {})
    if incoming_count >= t.get("incoming_good", 3):
        return 100
    if incoming_count >= t.get("incoming_ok", 1):
        return 60
    if in_collection:
        return t.get("in_collection_score", 30)
    return 0


def find_missing_crosslinks(collections, products_by_collection, link_graph):
    suggestions = []

    # Build inverted index: product -> set of collection shopify_ids (Fix #13)
    coll_by_sid = {}
    product_to_collections = {}
    for c in collections:
        sid = c.get("shopify_id")
        coll_by_sid[sid] = c
        for pid in products_by_collection.get(sid, set()):
            product_to_collections.setdefault(pid, set()).add(sid)

    # Find candidate pairs sharing at least one product
    candidate_pairs = set()
    for pid, coll_sids in product_to_collections.items():
        coll_list = sorted(coll_sids)
        for i in range(len(coll_list)):
            for j in range(i + 1, len(coll_list)):
                candidate_pairs.add((coll_list[i], coll_list[j]))

    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    ct = rules.get("crosslink_thresholds", {})
    min_shared = ct.get("shared_products", 3)

    for sid_a, sid_b in candidate_pairs:
        coll_a = coll_by_sid.get(sid_a)
        coll_b = coll_by_sid.get(sid_b)
        if not coll_a or not coll_b:
            continue

        handle_a = coll_a["handle"]
        handle_b = coll_b["handle"]

        a_links_to_b = f"collection:{handle_b}" in link_graph.get(f"collection:{handle_a}", set())
        b_links_to_a = f"collection:{handle_a}" in link_graph.get(f"collection:{handle_b}", set())

        if a_links_to_b and b_links_to_a:
            continue

        prods_a = products_by_collection.get(sid_a, set())
        prods_b = products_by_collection.get(sid_b, set())
        shared_products = prods_a & prods_b

        if len(shared_products) >= min_shared:
            suggestions.append({
                "collection_a_title": coll_a["title"],
                "collection_a_handle": handle_a,
                "collection_b_title": coll_b["title"],
                "collection_b_handle": handle_b,
                "shared_tags": 0,
                "shared_products": len(shared_products),
                "a_links_to_b": a_links_to_b,
                "b_links_to_a": b_links_to_a,
            })

    return suggestions


def analyze_store_interlinking(store_id, store_domain=None):
    logger.info("Starting interlinking analysis for store %s", store_id)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_id, title, handle, description, tags, vendor, product_type FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
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

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sa.id, sa.shopify_id, sa.title, sa.handle, sa.body, sa.tags,
                   sb.handle AS blog_handle
            FROM synced_articles sa
            JOIN synced_blogs sb ON sb.id = sa.blog_id
            WHERE sa.store_id = %s AND sa.is_published = TRUE
            """,
            (store_id,),
        )
        articles = [dict(r) for r in cur.fetchall()]

    product_by_handle = {p["handle"]: p for p in products}
    collection_by_handle = {c["handle"]: c for c in collections}

    # Build set of valid entity keys for broken link detection (Fix #10, #12)
    valid_keys = set()
    for p in products:
        valid_keys.add(f"product:{p['handle']}")
    for c in collections:
        valid_keys.add(f"collection:{c['handle']}")
    for a in articles:
        valid_keys.add(f"article:{a['blog_handle']}/{a['handle']}")

    link_graph = {}
    all_links = []
    broken_links = []

    incoming_counts = {}
    outgoing_counts = {}

    def _target_key_for_link(link):
        if link["link_type"] == "article":
            return f"article:{link.get('blog_handle', '')}/{link['handle']}"
        return f"{link['link_type']}:{link['handle']}"

    def _process_links(entity_key, entity_type, entity_handle, entity_title, links, entity_blog_handle=None):
        targets = set()
        for link in links:
            target_key = _target_key_for_link(link)
            targets.add(target_key)
            incoming_counts[target_key] = incoming_counts.get(target_key, 0) + 1
            link_entry = {
                "from_type": entity_type,
                "from_handle": entity_handle,
                "from_title": entity_title,
                "to_type": link["link_type"],
                "to_handle": link["handle"],
                "to_href": link.get("href", ""),
                "anchor_text": link["anchor_text"],
            }
            if entity_type == "article" and entity_blog_handle:
                link_entry["from_blog_handle"] = entity_blog_handle
            if link["link_type"] == "article":
                link_entry["to_blog_handle"] = link.get("blog_handle", "")
            all_links.append(link_entry)
            if target_key not in valid_keys:
                broken_links.append(link_entry)
        link_graph[entity_key] = targets
        outgoing_counts[entity_key] = len(links)

    for p in products:
        key = f"product:{p['handle']}"
        links = extract_links_from_html(p.get("description"), store_domain)
        _process_links(key, "product", p["handle"], p["title"], links)

    for c in collections:
        key = f"collection:{c['handle']}"
        links = extract_links_from_html(c.get("description"), store_domain)
        _process_links(key, "collection", c["handle"], c["title"], links)

    for a in articles:
        key = f"article:{a['blog_handle']}/{a['handle']}"
        links = extract_links_from_html(a.get("body"), store_domain)
        _process_links(key, "article", a["handle"], a["title"], links, entity_blog_handle=a["blog_handle"])

    orphan_products = []
    orphan_collections = []
    orphan_articles = []
    under_linked = []
    product_details = []
    collection_details = []
    article_details = []

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
            "vendor": p.get("vendor") or "",
            "product_type": p.get("product_type") or "",
            "tags": p.get("tags") or "",
            "incoming_count": inc,
            "outgoing_count": out,
            "in_collection": in_collection,
            "collection_count": len(product_collections.get(p["shopify_id"], set())),
            "score": score,
            "outgoing_links": list(link_graph.get(key, set())),
        }
        product_details.append(detail)

        # Fix #11: products with 0 incoming but in a collection are under-linked
        if inc == 0 and not in_collection:
            orphan_products.append(detail)
        elif inc == 0 and in_collection:
            under_linked.append({**detail, "reason": "in_collection_but_no_incoming_links"})
        elif inc < 2 and inc > 0:
            under_linked.append(detail)

    product_vendor_by_sid = {p["shopify_id"]: p.get("vendor") or "" for p in products}
    product_type_by_sid = {p["shopify_id"]: p.get("product_type") or "" for p in products}

    for c in collections:
        key = f"collection:{c['handle']}"
        inc = incoming_counts.get(key, 0)
        out = outgoing_counts.get(key, 0)
        score = compute_interlinking_score(inc, out, True)

        coll_product_sids = products_by_collection.get(c.get("shopify_id"), set())
        vendor_counts = {}
        type_counts = {}
        for sid in coll_product_sids:
            v = product_vendor_by_sid.get(sid, "")
            t = product_type_by_sid.get(sid, "")
            if v:
                vendor_counts[v] = vendor_counts.get(v, 0) + 1
            if t:
                type_counts[t] = type_counts.get(t, 0) + 1

        top_vendors = sorted(vendor_counts, key=vendor_counts.get, reverse=True)[:5]
        top_product_types = sorted(type_counts, key=type_counts.get, reverse=True)[:5]

        detail = {
            "id": c["id"],
            "shopify_id": c["shopify_id"],
            "title": c["title"],
            "handle": c["handle"],
            "incoming_count": inc,
            "outgoing_count": out,
            "score": score,
            "product_count": len(coll_product_sids),
            "top_vendors": top_vendors,
            "top_product_types": top_product_types,
            "outgoing_links": list(link_graph.get(key, set())),
        }
        collection_details.append(detail)

        if inc == 0:
            orphan_collections.append(detail)
        elif inc < 2:
            under_linked.append(detail)

    for a in articles:
        key = f"article:{a['blog_handle']}/{a['handle']}"
        inc = incoming_counts.get(key, 0)
        out = outgoing_counts.get(key, 0)
        score = compute_interlinking_score(inc, out, False)

        detail = {
            "id": a["id"],
            "shopify_id": a["shopify_id"],
            "title": a["title"],
            "handle": a["handle"],
            "blog_handle": a["blog_handle"],
            "incoming_count": inc,
            "outgoing_count": out,
            "score": score,
            "item_type": "article",
            "outgoing_links": list(link_graph.get(key, set())),
        }
        article_details.append(detail)

        if inc == 0:
            orphan_articles.append(detail)
        elif inc < 2:
            under_linked.append({**detail, "item_type": "article"})

    missing_crosslinks = find_missing_crosslinks(
        collections, products_by_collection, link_graph
    )

    not_in_collection = [
        d for d in product_details if not d["in_collection"]
    ]

    total_links = len(all_links)
    all_items = product_details + collection_details + article_details
    avg_incoming = (
        round(sum(d["incoming_count"] for d in all_items) / len(all_items), 1)
        if all_items else 0
    )
    all_scores = [d["score"] for d in all_items]
    avg_score = round(sum(all_scores) / len(all_scores)) if all_scores else 0

    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    dist = rules.get("distribution", {})
    excellent_threshold = dist.get("excellent", 80)
    good_threshold = dist.get("good", 50)
    score_distribution = {
        "excellent": sum(1 for s in all_scores if s >= excellent_threshold),
        "good": sum(1 for s in all_scores if good_threshold <= s < excellent_threshold),
        "poor": sum(1 for s in all_scores if 1 <= s < good_threshold),
        "orphan": sum(1 for s in all_scores if s == 0),
    }

    vendor_type_index = {}
    for p in products:
        v = p.get("vendor") or ""
        t = p.get("product_type") or ""
        if v:
            vendor_type_index.setdefault(v, {})
            if t:
                vendor_type_index[v][t] = vendor_type_index[v].get(t, 0) + 1
            else:
                vendor_type_index[v]["(untyped)"] = vendor_type_index[v].get("(untyped)", 0) + 1

    analysis = {
        "vendor_type_index": vendor_type_index,
        "summary": {
            "total_links": total_links,
            "total_products": len(products),
            "total_collections": len(collections),
            "total_articles": len(articles),
            "orphan_products_count": len(orphan_products),
            "orphan_collections_count": len(orphan_collections),
            "orphan_articles_count": len(orphan_articles),
            "under_linked_count": len(under_linked),
            "not_in_collection_count": len(not_in_collection),
            "avg_incoming_links": avg_incoming,
            "avg_score": avg_score,
            "score_distribution": score_distribution,
            "missing_crosslinks_count": len(missing_crosslinks),
            "broken_links_count": len(broken_links),
        },
        "orphan_products": orphan_products,
        "orphan_collections": orphan_collections,
        "orphan_articles": orphan_articles,
        "under_linked": sorted(under_linked, key=lambda x: x["incoming_count"]),
        "not_in_collection": not_in_collection,
        "missing_crosslinks": missing_crosslinks,
        "broken_links": broken_links,
        "all_links": all_links[:200],
        "product_details": product_details,
        "collection_details": collection_details,
        "article_details": article_details,
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
        "Interlinking analysis complete for store %s: %d links, %d broken, %d orphan products, %d orphan collections, %d orphan articles",
        store_id, total_links, len(broken_links), len(orphan_products), len(orphan_collections), len(orphan_articles),
    )

    return analysis
