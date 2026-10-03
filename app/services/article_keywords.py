"""Record what each published article is about.

Articles used to publish without recording their keyword, so the gap and topic
tools kept recommending subjects the blog already covered. The keyword is
stored on the article (synced_articles.primary_keyword) and read as "covered
by an article" — deliberately NOT as a ledger claim: a blog post must never
stop a product or collection from targeting the same keyword.
"""

import json
import logging

from app.database import db
from app.services.keyword_identity import variant_key

logger = logging.getLogger(__name__)

MIN_BACKFILL_IMPRESSIONS = 20


def primary_of(target_keywords):
    """First target keyword of a wizard session. Stored as a JSON string or a
    list whose items are strings or {"keyword": ...} dicts."""
    if isinstance(target_keywords, str):
        try:
            target_keywords = json.loads(target_keywords)
        except (ValueError, TypeError):
            return None
    first = (target_keywords or [None])[0]
    if isinstance(first, dict):
        first = first.get("keyword")
    return (str(first).strip() or None) if first else None


def record_article_keyword(article_id, keyword):
    """Never raises — a publish must not fail on bookkeeping."""
    if not keyword:
        return False
    try:
        with db.get_cursor() as cur:
            cur.execute("UPDATE synced_articles SET primary_keyword = %s WHERE id = %s", (keyword, article_id))
        return True
    except Exception:
        logger.exception("Recording keyword failed for article %s", article_id)
        return False


def covered_by_articles(store_id):
    """variant_key -> {id, title, handle, blog_handle, keyword} for every
    published article with a recorded keyword."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.id, sa.title, sa.handle, sb.handle AS blog_handle, sa.primary_keyword AS keyword
               FROM synced_articles sa LEFT JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.is_published AND COALESCE(sa.primary_keyword, '') <> ''
               ORDER BY sa.published_at""",
            (store_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {variant_key(r["keyword"]): r for r in rows if variant_key(r["keyword"])}


def backfill_article_keywords(store_id):
    """One-off: give every article without a recorded keyword the keyword of
    the wizard session that wrote it, else its top Google query (90 days).
    Returns {"session": n, "google": n, "none": n}."""
    from app.services.intro_link_service import _load_gsc_queries, article_path

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (sa.id) sa.id, sa.handle, sb.handle AS blog_handle, aws.target_keywords
               FROM synced_articles sa
               LEFT JOIN synced_blogs sb ON sb.id = sa.blog_id
               LEFT JOIN article_wizard_sessions aws
                 ON aws.shopify_article_id = sa.shopify_id AND aws.store_id = sa.store_id
               WHERE sa.store_id = %s AND sa.is_published AND sa.primary_keyword IS NULL
               ORDER BY sa.id, aws.id DESC""",
            (store_id,),
        )
        articles = [dict(r) for r in cur.fetchall()]
    queries = _load_gsc_queries(store_id)
    counts = {"session": 0, "google": 0, "none": 0}
    for article in articles:
        keyword, source = primary_of(article.get("target_keywords")), "session"
        if not keyword:
            seen = [q for q in queries.get(article_path(article), []) if q[2] >= MIN_BACKFILL_IMPRESSIONS]
            keyword, source = (max(seen, key=lambda q: q[2])[0] if seen else None), "google"
        counts[source if record_article_keyword(article["id"], keyword) else "none"] += 1
    return counts
