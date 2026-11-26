"""Collections that duplicate each other, and merging them.

Two collections are duplicates when they hold mostly the same products and
either share their name once category words are set aside ("Half & Half
Tobacco" / "Half & Half Pipe Tobacco") or hold nearly the same products
outright. Google picks one of such a pair and leaves the other unindexed, so
the pair splits links and clicks.

A merge keeps one collection: products only the other one has are added to
it (manual collections only; a smart collection's rules decide its
products), the other is hidden from the Online Store, and its address
redirects to the kept one. Hidden, not deleted: an undo publishes it again
and removes the redirect.
"""

import logging
import math
import re

from app.database import db
from app.services.keyword_identity import fold_token, tokens

logger = logging.getLogger(__name__)

# Words that name the product category rather than the brand or line.
CATEGORY_WORDS = {
    "tobacco", "pipe", "rolling", "cigar", "cigarillo", "cigarette", "wrap", "paper", "hookah", "shisha", "leaf", "blunt",
    "collection", "product", "the", "and", "shop", "all", "premium", "flavored", "flavor", "brand",
}
# Jaccard product overlap for two collections with the same name. Lower and
# a narrow collection ("Top Filter Tubes") pairs with a broad one that only
# shares its name ("Top Filter Tubes & Tobacco Products").
NAME_MATCH_OVERLAP = 0.4
# Jaccard overlap that makes two collections duplicates when their names
# share at least one word that is not a category word.
SAME_PRODUCTS_OVERLAP = 0.8
SAME_PRODUCTS_MIN = 3


def title_core(title):
    """Brand/line words of a collection title, plural-folded, category words
    removed. Pure."""
    words = re.findall(r"[a-z0-9]+", " ".join(tokens(title or "")))
    return frozenset(w for w in map(fold_token, words) if w not in CATEGORY_WORDS and len(w) > 1)


def pair_reason(a, b):
    """Why collections a and b ({title, products: set}) duplicate each other,
    or None. Pure."""
    pa, pb = a["products"], b["products"]
    if not pa or not pb:
        return None
    jaccard = len(pa & pb) / len(pa | pb)
    core_a, core_b = title_core(a["title"]), title_core(b["title"])
    if core_a and core_a == core_b and jaccard >= NAME_MATCH_OVERLAP:
        return "same_name"
    if core_a & core_b and jaccard >= SAME_PRODUCTS_OVERLAP and min(len(pa), len(pb)) >= SAME_PRODUCTS_MIN:
        return "same_products"
    return None


def keeper_rank(c):
    """Higher keeps. Google's indexed choice first, then search clicks (by
    doubling, so 3 vs 4 clicks is a tie), then a collection the store made
    over one this app generated, then size. Pure."""
    return (bool(c.get("indexed")), int(math.log2((c.get("clicks") or 0) + 1)), not c.get("generated"),
            len(c["products"]), -c["id"])


def find_pairs(collections):
    """[{keep, drop, reason, shared}] for every duplicate pair. Pure.
    A collection appears in at most one pair (its strongest match), so a
    merge never chains."""
    candidates = []
    for i, a in enumerate(collections):
        for b in collections[i + 1:]:
            reason = pair_reason(a, b)
            if reason:
                shared = len(a["products"] & b["products"])
                candidates.append((shared / len(a["products"] | b["products"]), reason, a, b))
    candidates.sort(key=lambda c: -c[0])
    used, pairs = set(), []
    for _score, reason, a, b in candidates:
        if a["id"] in used or b["id"] in used:
            continue
        used.update((a["id"], b["id"]))
        keep, drop = (a, b) if keeper_rank(a) >= keeper_rank(b) else (b, a)
        pairs.append({"keep": keep, "drop": drop, "reason": reason, "shared": len(a["products"] & b["products"])})
    return pairs


def _collections(store_id):
    from app.services.gsc_page_totals import page_totals_by_path

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT c.id, c.shopify_id, c.title, c.handle, c.collection_type, c.products_count,
                      i.indexed, i.coverage_state,
                      EXISTS (SELECT 1 FROM generated_collections g WHERE g.collection_id = c.id) AS generated
               FROM synced_collections c
               LEFT JOIN index_status i ON i.store_id = c.store_id AND i.item_type = 'collection' AND i.item_id = c.id
               WHERE c.store_id = %s AND COALESCE(c.published, TRUE) AND COALESCE(c.handle, '') <> ''""",
            (store_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT collection_shopify_id, product_shopify_id FROM collection_product_map WHERE store_id = %s",
            (store_id,),
        )
        members = {}
        for r in cur.fetchall():
            members.setdefault(r["collection_shopify_id"], set()).add(r["product_shopify_id"])
    totals, _window = page_totals_by_path(store_id, days=90, path_contains="/collections/")
    for r in rows:
        r["products"] = members.get(r["shopify_id"], set())
        r["clicks"] = (totals.get(f"/collections/{r['handle']}".lower()) or {}).get("clicks", 0)
    return rows


def _public(c):
    return {k: c.get(k) for k in ("id", "title", "handle", "collection_type", "indexed", "coverage_state",
                                  "generated", "clicks")} | {"products": len(c["products"])}


def find_duplicate_collections(store_id):
    pairs = find_pairs(_collections(store_id))
    with db.get_cursor() as cur:
        cur.execute("SELECT drop_id FROM collection_merges WHERE store_id = %s AND undone_at IS NULL", (store_id,))
        merged = {r["drop_id"] for r in cur.fetchall()}
    out = []
    for p in pairs:
        if p["drop"]["id"] in merged:
            continue
        only_in_drop = len(p["drop"]["products"] - p["keep"]["products"])
        out.append({"keep": _public(p["keep"]), "drop": _public(p["drop"]), "reason": p["reason"],
                    "shared": p["shared"], "only_in_drop": only_in_drop,
                    "can_add_products": p["keep"]["collection_type"] != "smart"})
    return out


def merge_collections(store_id, keep_id, drop_id, client=None, merged_by="person"):
    """Merge drop into keep. Returns the merge row id and what was done."""
    from app.services.apply_pipeline import _client_for

    client = client or _client_for(store_id)
    by_id = {c["id"]: c for c in _collections(store_id) if c["id"] in (keep_id, drop_id)}
    keep, drop = by_id.get(keep_id), by_id.get(drop_id)
    if not keep or not drop:
        raise ValueError("Both collections must exist and be published")
    if pair_reason(keep, drop) is None:
        raise ValueError("These collections no longer look like duplicates")

    added = []
    missing = sorted(drop["products"] - keep["products"])
    if missing and keep["collection_type"] != "smart":
        client.add_products_to_collection(keep["shopify_id"], missing)
        added = missing
    redirect = client.create_url_redirect(f"/collections/{drop['handle']}", f"/collections/{keep['handle']}")
    client.unpublish_from_online_store(drop["shopify_id"])

    with db.get_cursor() as cur:
        cur.execute("UPDATE synced_collections SET published = FALSE WHERE id = %s", (drop_id,))
        cur.execute(
            """INSERT INTO collection_merges (store_id, keep_id, drop_id, drop_handle, keep_handle, added_products,
                                              redirect_id, redirect_status, merged_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (store_id, keep_id, drop_id, drop["handle"], keep["handle"], added, redirect.get("id"),
             redirect.get("status"), merged_by),
        )
        merge_id = cur.fetchone()["id"]
        if added:
            cur.execute(
                """INSERT INTO collection_product_map (store_id, collection_shopify_id, product_shopify_id)
                   SELECT %s, %s, unnest(%s::text[]) ON CONFLICT DO NOTHING""",
                (store_id, keep["shopify_id"], added),
            )
    return {"id": merge_id, "added_products": len(added), "skipped_products": len(missing) - len(added),
            "redirect": redirect.get("status")}


def undo_merge(merge_id, client=None):
    """Publish the hidden collection again and remove its redirect. Products
    added to the kept collection stay (removing them could drop ones the
    store added since)."""
    from app.services.apply_pipeline import _client_for

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT m.*, c.shopify_id FROM collection_merges m JOIN synced_collections c ON c.id = m.drop_id
               WHERE m.id = %s""",
            (merge_id,),
        )
        m = cur.fetchone()
    if not m or m["undone_at"]:
        raise ValueError("This merge was already undone")
    client = client or _client_for(m["store_id"])
    if m["redirect_status"] != "exists":
        client.delete_url_redirect_by_path(f"/collections/{m['drop_handle']}")
    client.publish_to_all_channels(m["shopify_id"])
    with db.get_cursor() as cur:
        cur.execute("UPDATE synced_collections SET published = TRUE WHERE id = %s", (m["drop_id"],))
        cur.execute("UPDATE collection_merges SET undone_at = NOW() WHERE id = %s", (merge_id,))
    return {"status": "undone"}


def list_merges(store_id, limit=50):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT m.id, m.keep_id, m.drop_id, m.keep_handle, m.drop_handle, cardinality(m.added_products) AS added,
                      m.created_at, m.undone_at, k.title AS keep_title, d.title AS drop_title
               FROM collection_merges m
               LEFT JOIN synced_collections k ON k.id = m.keep_id
               LEFT JOIN synced_collections d ON d.id = m.drop_id
               WHERE m.store_id = %s ORDER BY m.created_at DESC LIMIT %s""",
            (store_id, limit),
        )
        return [{**dict(r), "created_at": r["created_at"].isoformat(),
                 "undone_at": r["undone_at"].isoformat() if r["undone_at"] else None} for r in cur.fetchall()]
