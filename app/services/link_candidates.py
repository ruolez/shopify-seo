"""Relevance-ranked product/collection candidates for article internal linking.

The news pipeline can only link to candidates it shows the AI, and prompts
forbid linking anything outside that list. With catalogs of hundreds of
products, an unranked LIMIT slice makes relevant targets invisible — so we
score every active product/collection title against the article text and
hand the AI the best-matching slice instead.
"""

import logging
import re

from app.database import db

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "with", "as",
    "at", "by", "its", "is", "are", "from", "this", "that", "up", "over", "under",
    "new", "all", "best", "top", "shop", "buy", "get", "your", "our", "how", "why",
    "what", "when", "you", "it", "be", "has", "have", "will", "more", "one",
}


def text_tokens(text):
    """Content-word token set for relevance matching: lowercase, HTML stripped,
    apostrophes removed so possessives/plurals tokenize alike, stopwords dropped."""
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = cleaned.casefold().replace("'", "").replace("’", "")
    return set(_WORD_RE.findall(cleaned)) - _STOPWORDS


def rank_by_relevance(article_text, candidates, limit):
    """Return the `limit` candidates whose titles best match `article_text`.

    Score = matched-token count (dominant) + matched fraction of the
    candidate's title (tie-break), so among equal matches the tighter title
    wins. Zero-score candidates fill any remaining slots in their original
    order, which keeps behavior at worst equal to the old unranked slice.
    """
    article_tokens = text_tokens(article_text)
    scored = []
    for idx, cand in enumerate(candidates):
        title_tokens = text_tokens(cand.get("title") or "")
        matched = title_tokens & article_tokens
        score = len(matched) + len(matched) / len(title_tokens) if title_tokens else 0.0
        scored.append((-score, idx, cand))
    scored.sort()
    return [cand for _, _, cand in scored[:limit]]


def fetch_ranked_candidates(store_id, article_text, product_limit=50, collection_limit=40):
    """Fetch active, linkable products and collections for `store_id` and return
    the (products, collections) slices most relevant to `article_text`."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT title, handle FROM synced_products
               WHERE store_id = %s AND status = 'ACTIVE'
                 AND handle IS NOT NULL AND handle != ''""",
            (store_id,),
        )
        products = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT title, handle FROM synced_collections
               WHERE store_id = %s AND handle IS NOT NULL AND handle != ''""",
            (store_id,),
        )
        collections = [dict(r) for r in cur.fetchall()]
    return (
        rank_by_relevance(article_text, products, product_limit),
        rank_by_relevance(article_text, collections, collection_limit),
    )
