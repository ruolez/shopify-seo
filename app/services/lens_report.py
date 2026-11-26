"""Google Lens report: pages people reach by searching with a photo.

Search Console's webMultimodal search type (Lens, Circle to Search, image
uploads) is stored per page in gsc_multimodal_daily. This joins it to the
store's pages and checks the images Lens matches against: whether there are
any, whether each has alt text, whether the first image's alt names the
product, and whether there is more than one angle.
"""

import re
from urllib.parse import urlparse

from app.database import db
from app.services.keyword_identity import fold_token

LENS_DAYS = 90
_WORD_RE = re.compile(r"[a-z0-9]+")
_SKIP = {"the", "and", "with", "for", "of", "pack", "packs", "count", "ct", "oz", "box", "pouch", "pouches", "can",
         "cans", "bag"}


def _words(text):
    return {fold_token(w) for w in _WORD_RE.findall((text or "").lower()) if w not in _SKIP and not w.isdigit()}


def image_issues(title, images):
    """What would help Lens match this product's photos. Pure.
    images: [{url, alt}] in display order."""
    if not images:
        return ["no_images"]
    issues = []
    missing = sum(1 for i in images if not (i.get("alt") or "").strip())
    if missing:
        issues.append("missing_alt")
    first_alt = _words(images[0].get("alt"))
    name = _words(title)
    if first_alt and name and len(first_alt & name) < min(2, len(name)):
        issues.append("first_alt_off_name")
    if len(images) == 1:
        issues.append("one_image")
    return issues


def _item_for(path, products, collections, articles):
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) == 2 and parts[0] == "products":
        return products.get(parts[1])
    if len(parts) == 2 and parts[0] == "collections":
        return collections.get(parts[1])
    if len(parts) == 3 and parts[0] == "blogs":
        return articles.get((parts[1], parts[2]))
    if len(parts) == 4 and parts[0] == "collections" and parts[2] == "products":
        return products.get(parts[3])
    return None


def lens_report(store_id, days=LENS_DAYS, limit=50):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT page, SUM(clicks) AS clicks, SUM(impressions) AS impressions
               FROM gsc_multimodal_daily WHERE store_id = %s AND date > CURRENT_DATE - %s
               GROUP BY page""",
            (store_id, days),
        )
        rows = cur.fetchall()
        cur.execute("SELECT id, handle, title, images FROM synced_products WHERE store_id = %s", (store_id,))
        products = {r["handle"]: {"type": "product", **dict(r)} for r in cur.fetchall()}
        cur.execute("SELECT id, handle, title, image_url FROM synced_collections WHERE store_id = %s", (store_id,))
        collections = {r["handle"]: {"type": "collection", "images": [{"url": r["image_url"], "alt": None}]
                                     if r["image_url"] else [], **dict(r)} for r in cur.fetchall()}
        cur.execute(
            """SELECT a.id, a.handle, a.title, a.image_url, a.image_alt, b.handle AS blog FROM synced_articles a
               JOIN synced_blogs b ON b.id = a.blog_id WHERE a.store_id = %s""",
            (store_id,),
        )
        articles = {(r["blog"], r["handle"]): {"type": "article", "images": [{"url": r["image_url"], "alt": r["image_alt"]}]
                                                if r["image_url"] else [], **dict(r)} for r in cur.fetchall()}

    pages = {}
    for r in rows:
        path = urlparse(r["page"]).path.rstrip("/").lower()
        entry = pages.setdefault(path, {"path": path, "clicks": 0, "impressions": 0})
        entry["clicks"] += int(r["clicks"] or 0)
        entry["impressions"] += int(r["impressions"] or 0)

    out = []
    for p in sorted(pages.values(), key=lambda p: (-p["clicks"], -p["impressions"])):
        item = _item_for(p["path"], products, collections, articles)
        if item:
            images = item.get("images") or []
            p.update({"type": item["type"], "id": item["id"], "title": item["title"], "image_count": len(images),
                      "first_image": images[0]["url"] if images else None,
                      "first_alt": images[0].get("alt") if images else None,
                      # Collection images have no alt in the mirror; only products are checked.
                      "issues": image_issues(item["title"], images) if item["type"] == "product" else []})
        else:
            p.update({"type": None, "id": None, "title": None, "issues": []})
        out.append(p)
    totals = {"clicks": sum(p["clicks"] for p in out), "impressions": sum(p["impressions"] for p in out),
              "pages": len(out), "with_issues": sum(1 for p in out if p["issues"])}
    return {"days": days, "totals": totals, "pages": out[:limit]}
