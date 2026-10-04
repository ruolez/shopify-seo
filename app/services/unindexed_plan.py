"""What to do about pages Google has not indexed.

The Indexing check says which pages are missing from Google. This says which
of them are worth the effort and what the likely fix is: a page people search
for but nothing links to is a different job from one nobody looks for.
"""

from datetime import timedelta

from app.database import db
from app.services.keyword_identity import fold_token, tokens

MIN_LINKS = 3
MIN_DEMAND_VOLUME = 50
UNKNOWN_STATE = "URL is unknown to Google"
_FILLER = {"the", "and", "for", "with", "all", "pack", "packs", "count"}


def diagnose(page):
    """(likely reason, what to do) for one unindexed page. Pure."""
    links = page.get("incoming_links") or 0
    if page["coverage_state"] == UNKNOWN_STATE and not page.get("in_sitemap", True):
        return ("Google has never seen it, and it is not in your sitemap",
                "Check the product is published to the Online Store sales channel in Shopify.")
    # Collection grids list it, but a link inside an article or description
    # tells Google the page matters.
    if links == 0:
        return ("No article or description links to it",
                "Link to it from a related article so Google sees it as worth indexing.")
    if links < MIN_LINKS:
        return (f"Only {links} article{'s' if links != 1 else ''} or description{'s' if links != 1 else ''} link{'s' if links == 1 else ''} to it",
                "Add links from related articles or collection descriptions.")
    if _has_demand(page):
        return ("Google read it and found little that is new",
                "Rewrite the description to say what is specific to this item — size, flavour, who it suits.")
    return ("Google read it; there is little search demand for it",
            "Fine to leave. It can still be found through its collection.")


def _has_demand(page):
    return (page.get("volume") or 0) >= MIN_DEMAND_VOLUME or (page.get("impressions") or 0) > 0


def rank(pages):
    """Pages worth indexing first (most searched first), each with its reason
    and action. Pure."""
    out = []
    for page in pages:
        reason, action = diagnose(page)
        out.append({**page, "worth_indexing": _has_demand(page), "reason": reason, "action": action})
    out.sort(key=lambda p: (not p["worth_indexing"], -(p.get("volume") or 0), -(p.get("impressions") or 0),
                            p.get("incoming_links") or 0))
    return out


def _words(text):
    return {fold_token(t) for t in tokens(text or "") if len(t) > 2 and t not in _FILLER}


def pick_link_sources(item, articles, limit=2):
    """Articles that could carry a link to this item: ones about its brand,
    best brand match first, then most clicked. Product type alone is too broad
    ("cigars" matches half the blog), so no brand match means no suggestion. Pure."""
    brand = _words(item.get("vendor"))
    if not brand:
        return []
    related = [(len(brand & _words(a["title"])), a) for a in articles]
    related = [(shared, a) for shared, a in related if shared]
    related.sort(key=lambda pair: (-pair[0], -pair[1]["clicks"]))
    return [a for _, a in related[:limit]]


def _sitemap_paths(base_url):
    """Paths listed in the store's sitemap, or None when it cannot be read."""
    import re

    import requests

    try:
        index = requests.get(f"{base_url}/sitemap.xml", timeout=20).text
        paths = set()
        for child in re.findall(r"<loc>([^<]+)</loc>", index)[:40]:
            xml = requests.get(child.replace("&amp;", "&"), timeout=20).text
            for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
                paths.add(loc.split("://", 1)[-1].split("/", 1)[-1].split("?")[0].strip("/").lower())
        return paths
    except Exception:
        return None


def get_unindexed_plan(store_id):
    from app.services.dashboard_service import gsc_data_end_date
    from app.services.url_utils import gsc_base_url, url_path_key

    end = gsc_data_end_date()
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT s.item_type, s.item_id, s.url, s.coverage_state, s.last_crawl_time,
                      COALESCE(p.title, c.title, a.title) AS title,
                      p.vendor, p.product_type,
                      t.keyword_resolution->'primary'->>'keyword' AS keyword,
                      COALESCE((t.keyword_resolution->'primary'->>'volume')::float, 0) AS volume
               FROM index_status s
               LEFT JOIN synced_products p ON s.item_type = 'product' AND p.id = s.item_id
               LEFT JOIN synced_collections c ON s.item_type = 'collection' AND c.id = s.item_id
               LEFT JOIN synced_articles a ON s.item_type = 'article' AND a.id = s.item_id
               LEFT JOIN technical_seo_analyses t
                 ON t.store_id = s.store_id AND t.item_type = s.item_type AND t.item_id = s.item_id
               WHERE s.store_id = %s AND NOT s.indexed AND s.item_type IN ('product', 'collection', 'article')""",
            (store_id,),
        )
        pages = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s", (store_id,))
        links_row = cur.fetchone()
        cur.execute(
            """SELECT page, SUM(impressions) AS impressions FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s GROUP BY page""",
            (store_id, end - timedelta(days=90)),
        )
        impressions = {}
        for r in cur.fetchall():
            key = url_path_key(r["page"]).lower()
            impressions[key] = impressions.get(key, 0) + float(r["impressions"] or 0)
        cur.execute(
            """SELECT sa.id, sa.title, COALESCE(g.clicks, 0) AS clicks
               FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
               LEFT JOIN (SELECT page, SUM(clicks) AS clicks FROM gsc_daily_metrics
                          WHERE store_id = %s AND date >= %s GROUP BY page) g
                 ON g.page LIKE '%%/blogs/' || sb.handle || '/' || sa.handle
               WHERE sa.store_id = %s AND sa.is_published""",
            (store_id, end - timedelta(days=28), store_id),
        )
        articles = [{"id": r["id"], "title": r["title"], "clicks": float(r["clicks"])} for r in cur.fetchall()]
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()

    analysis = (links_row or {}).get("analysis_data") or {}
    incoming = {}
    for key, item_type in (("product_details", "product"), ("collection_details", "collection"),
                           ("article_details", "article")):
        for d in analysis.get(key) or []:
            incoming[(item_type, d.get("id"))] = d.get("incoming_count") or 0

    needs_sitemap = any(p["coverage_state"] == UNKNOWN_STATE for p in pages)
    sitemap = _sitemap_paths(gsc_base_url((cred or {}).get("gsc_property")).rstrip("/")) if needs_sitemap and cred else None

    for p in pages:
        path = url_path_key(p["url"]).lower()
        p["volume"] = int(p["volume"] or 0)
        p["impressions"] = round(impressions.get(path, 0))
        p["incoming_links"] = incoming.get((p["item_type"], p["item_id"]), 0)
        p["in_sitemap"] = True if sitemap is None else path in sitemap
        p["last_crawl_time"] = p["last_crawl_time"].isoformat() if p["last_crawl_time"] else None
        p["link_from"] = pick_link_sources(p, articles) if p["item_type"] == "product" else []

    ranked = rank(pages)
    return {
        "pages": ranked,
        "worth_indexing": sum(1 for p in ranked if p["worth_indexing"]),
        "low_demand": sum(1 for p in ranked if not p["worth_indexing"]),
        "sitemap_checked": sitemap is not None,
    }
