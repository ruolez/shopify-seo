"""Add internal links without rewriting the page.

Unindexed products and unranked collections share one cause: too few pages
link to them. This places one link at a time, deterministically: wrap the
first place the page already names the target, or, when it never does, add it
to a short "Related" line. No model re-emits the body, so nothing else on the
page can change. Applying goes through the apply pipeline (history + undo).
"""

import html
import re

from app.services.mention_linker import _TAG_SPLIT_RE, _link_first_mention, _phrase_pattern

RELATED_CLASS = "a2z-related"
MIN_ANCHOR_WORDS = 2
MIN_ANCHOR_CHARS = 5
_RELATED_RE = re.compile(rf'(<p class="{RELATED_CLASS}">.*?)(</p>)', re.DOTALL)
_FAQ_RE = re.compile(r'<section\b[^>]*class="[^"]*a2z-blog__faq', re.IGNORECASE)


def _already_links(body_html, url):
    return re.search(r'href\s*=\s*["\'](?:https?://[^/"\']+)?' + re.escape(url) + r'/?["\'?#]', body_html or "") is not None


def place_link(body_html, url, phrases, label, min_words=MIN_ANCHOR_WORDS, allow_related=True):
    """Returns (new_html, how) with how in "inline" | "related" | "already" | "none".

    phrases: wordings of the target to look for, best first. By default single
    words are skipped — "ZYN" pointing at one ZYN product is a misleading
    anchor; a collection's own keyword ("cigarillos") passes min_words=1.
    allow_related=False means "only if the page already names it".
    """
    body_html = body_html or ""
    if _already_links(body_html, url):
        return body_html, "already"

    segments = _TAG_SPLIT_RE.split(body_html)
    for phrase in phrases:
        if len((phrase or "").split()) < min_words or len((phrase or "").strip()) < MIN_ANCHOR_CHARS:
            continue
        pattern = _phrase_pattern(phrase.strip())
        if pattern and _link_first_mention(segments, pattern, url):
            return "".join(segments), "inline"

    if not allow_related:
        return body_html, "none"

    link = f'<a href="{url}">{html.escape(label)}</a>'
    if _RELATED_RE.search(body_html):
        return _RELATED_RE.sub(lambda m: f"{m.group(1)} · {link}{m.group(2)}", body_html, count=1), "related"
    block = f'<p class="{RELATED_CLASS}"><strong>Related:</strong> {link}</p>'
    faq = _FAQ_RE.search(body_html)
    if faq:
        return body_html[:faq.start()] + block + body_html[faq.start():], "related"
    return body_html + block, "related"


# ── Applying links (DB + Shopify) ─────────────────────────────────────────

import logging  # noqa: E402

from app.database import db  # noqa: E402

logger = logging.getLogger(__name__)

MAX_NEW_LINKS_PER_PAGE = 3
MIN_PRODUCTS_FOR_ONE_WORD = 20
_URL_PREFIX = {"product": "products", "collection": "collections"}


def _target(target_type, target_id):
    """{url, title, phrases} for a link target."""
    with db.get_cursor() as cur:
        if target_type == "article":
            cur.execute(
                """SELECT sa.title, sa.primary_keyword AS keyword, sb.handle || '/' || sa.handle AS path
                   FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id WHERE sa.id = %s""",
                (target_id,),
            )
            row = cur.fetchone()
            return row and {"url": f"/blogs/{row['path']}", "title": row["title"],
                            "phrases": [row["title"], row["keyword"] or ""]}
        table = "synced_products" if target_type == "product" else "synced_collections"
        cur.execute(
            f"""SELECT t.title, t.handle, a.keyword_resolution->'primary'->>'keyword' AS keyword
                       {", COALESCE(t.products_count, 0) AS products_count" if target_type == "collection" else ""}
                FROM {table} t
                LEFT JOIN technical_seo_analyses a
                  ON a.store_id = t.store_id AND a.item_type = %s AND a.item_id = t.id
                WHERE t.id = %s""",
            (target_type, target_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    phrases = collection_phrases(dict(row)) if target_type == "collection" else [row["title"], row["keyword"] or ""]
    return {"url": f"/{_URL_PREFIX[target_type]}/{row['handle']}", "title": row["title"], "phrases": phrases}


def add_link(store_id, source_type, source_id, target_type, target_id, client=None, allow_related=True):
    """Place one link from source to target and push it. Returns
    {status: "added"|"already"|"none", how, event_id}. Raises on Shopify errors."""
    from app.services.apply_pipeline import ApplyRefused, _load_mirror, apply_item_changes

    target = _target(target_type, target_id)
    if not target:
        raise ValueError(f"{target_type} not found")
    min_words = 1 if target_type == "collection" else MIN_ANCHOR_WORDS

    for attempt in (1, 2):
        _, mirror = _load_mirror(source_type, source_id)
        new_body, how = place_link(mirror["body"], target["url"], target["phrases"], target["title"],
                                   min_words=min_words, allow_related=allow_related)
        if how in ("already", "none"):
            return {"status": how, "how": how, "event_id": None}
        try:
            applied = apply_item_changes(
                store_id, source_type, source_id, {"body": new_body},
                source="auto_links", change_type="link_added", client=client,
                allow_dropped_primary=True, stamp=False, restore_media=False,
            )
            return {"status": "added", "how": how, "event_id": applied["event_id"]}
        except ApplyRefused as refused:
            # Stale: the pipeline just refreshed the mirror from Shopify, so
            # one retry places the link in the current body.
            if refused.status != "stale" or attempt == 2:
                raise
    return {"status": "none", "how": "none", "event_id": None}


def link_unindexed(store_id, client=None, progress=None):
    """Link every unindexed product people search for from the best article
    about its brand. At most MAX_NEW_LINKS_PER_PAGE new links per article per run."""
    from app.services.unindexed_plan import get_unindexed_plan

    per_source, result = {}, {"added": 0, "already": 0, "no_source": 0, "failed": 0}
    pages = [p for p in get_unindexed_plan(store_id)["pages"] if p["worth_indexing"] and p["item_type"] == "product"]
    for i, page in enumerate(pages):
        if progress:
            progress(i, len(pages))
        sources = [a for a in page.get("link_from") or [] if per_source.get(a["id"], 0) < MAX_NEW_LINKS_PER_PAGE]
        if not sources:
            result["no_source"] += 1
            continue
        try:
            out = add_link(store_id, "article", sources[0]["id"], "product", page["item_id"], client=client)
            result[out["status"] if out["status"] in result else "already"] += 1
            if out["status"] == "added":
                per_source[sources[0]["id"]] = per_source.get(sources[0]["id"], 0) + 1
        except Exception:
            logger.exception("Auto-link failed: article %s -> product %s", sources[0]["id"], page["item_id"])
            result["failed"] += 1
    return result


def _published_collections(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT c.id, c.title, c.handle, c.description, COALESCE(c.products_count, 0) AS products_count,
                      a.keyword_resolution->'primary'->>'keyword' AS keyword
               FROM synced_collections c
               LEFT JOIN technical_seo_analyses a
                 ON a.store_id = c.store_id AND a.item_type = 'collection' AND a.item_id = c.id
               WHERE c.store_id = %s AND COALESCE(c.published, TRUE) AND COALESCE(c.handle, '') <> ''
               ORDER BY c.products_count DESC NULLS LAST""",
            (store_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def _articles_with_clicks(store_id, guides_only=False):
    from datetime import timedelta

    from app.services.dashboard_service import gsc_data_end_date

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT sa.id, sa.title, sa.body, sa.primary_keyword, sb.handle || '/' || sa.handle AS path,
                       COALESCE(g.clicks, 0) AS clicks
                FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                LEFT JOIN (SELECT page, SUM(clicks) AS clicks FROM gsc_daily_metrics
                           WHERE store_id = %s AND date >= %s GROUP BY page) g
                  ON g.page LIKE '%%/blogs/' || sb.handle || '/' || sa.handle
                WHERE sa.store_id = %s AND sa.is_published
                  AND COALESCE(sa.tags, '') NOT ILIKE '%%landing-page%%'
                  {"AND sa.article_type = 'blog'" if guides_only else ""}
                ORDER BY COALESCE(g.clicks, 0) DESC, sa.id""",
            (store_id, gsc_data_end_date() - timedelta(days=28), store_id),
        )
        return [dict(r) for r in cur.fetchall()]


def collection_phrases(collection):
    """Wordings that may be linked to this collection: its title, and its
    keyword only when the keyword names the collection — it must contain the
    first identifying word of the handle. "flavored cigars" is not a name for
    the Grape Flavored Cigars collection; "pipe tobacco" is for Pipe Tobacco."""
    from app.services.keyword_identity import fold_token, tokens

    phrases = [collection["title"]]
    keyword = (collection.get("keyword") or "").strip()
    identity = [t for t in tokens((collection.get("handle") or "").replace("-", " ")) if not t.isdigit()]
    names_it = keyword and identity and fold_token(identity[0]) in {fold_token(t) for t in tokens(keyword)}
    # A lone word ("mellow") is only a safe anchor for a major category.
    big_enough = len(keyword.split()) > 1 or (collection.get("products_count") or 0) >= MIN_PRODUCTS_FOR_ONE_WORD
    if names_it and big_enough:
        phrases.append(keyword)
    return phrases


def _by_relevance(collections, article_title):
    """Collections sharing words with the article's title first (its own
    brand or topic), then the rest in their existing order."""
    from app.services.keyword_identity import fold_token, tokens

    topic = {fold_token(t) for t in tokens(article_title or "") if len(t) > 2}
    return sorted(collections, key=lambda c: -len(topic & {fold_token(t) for t in tokens(c["title"]) if len(t) > 2}))


def plan_article_collection_links(store_id):
    """Articles that name a collection (by its title or keyword) without
    linking to it. [{article_id, title, clicks, collections: [{id, title, url}]}]"""
    collections = _published_collections(store_id)
    plan = []
    for article in _articles_with_clicks(store_id):
        body, found = article["body"] or "", []
        for c in _by_relevance(collections, article["title"]):
            if len(found) >= MAX_NEW_LINKS_PER_PAGE:
                break
            url = f"/collections/{c['handle']}"
            new_body, how = place_link(body, url, collection_phrases(c), c["title"],
                                       min_words=1, allow_related=False)
            if how == "inline":
                body = new_body
                found.append({"id": c["id"], "title": c["title"], "url": url})
        if found:
            plan.append({"article_id": article["id"], "title": article["title"],
                         "clicks": int(article["clicks"]), "collections": found})
    return plan


def plan_collection_backlinks(store_id):
    """Collections whose description does not link to the most-read guide that
    links to them. [{collection_id, title, article_id, article_title}]"""
    guides = _articles_with_clicks(store_id, guides_only=True)
    plan = []
    for c in _published_collections(store_id):
        url = f"/collections/{c['handle']}"
        guide = next((g for g in guides if _already_links(g["body"] or "", url)), None)
        if not guide or _already_links(c["description"] or "", f"/blogs/{guide['path']}"):
            continue
        plan.append({"collection_id": c["id"], "title": c["title"],
                     "article_id": guide["id"], "article_title": guide["title"]})
    return plan


def run_plan(store_id, kind, client=None, progress=None):
    """Apply one whole plan. kind: unindexed | article_collection | collection_back."""
    if kind == "unindexed":
        return link_unindexed(store_id, client=client, progress=progress)
    result = {"added": 0, "already": 0, "failed": 0}
    if kind == "article_collection":
        jobs = [("article", row["article_id"], "collection", c["id"], False)
                for row in plan_article_collection_links(store_id) for c in row["collections"]]
    elif kind == "collection_back":
        jobs = [("collection", row["collection_id"], "article", row["article_id"], True)
                for row in plan_collection_backlinks(store_id)]
    else:
        raise ValueError("Unknown link plan")
    for i, (source_type, source_id, target_type, target_id, allow_related) in enumerate(jobs):
        if progress:
            progress(i, len(jobs))
        try:
            out = add_link(store_id, source_type, source_id, target_type, target_id,
                           client=client, allow_related=allow_related)
            result["added" if out["status"] == "added" else "already"] += 1
        except Exception:
            logger.exception("Auto-link failed: %s %s -> %s %s", source_type, source_id, target_type, target_id)
            result["failed"] += 1
    return result
