"""Refresh an existing article in place.

Half the blog earns no impressions and nobody revisits a post after it is
published. A refresh rewrites one article around the searches Google already
shows it for and the collections and products the store sells — same URL,
same images, existing links kept — and returns a before/after for a person to
approve. Nothing is written here; applying goes through the apply pipeline.
"""

import json
import logging
import re

from app.database import db
from app.services.keyword_identity import fold_token, tokens

logger = logging.getLogger(__name__)

MAX_QUERIES = 15
MAX_LINK_TARGETS = 10
_SECTIONS = ("META_TITLE", "META_DESCRIPTION", "SUMMARY", "CHANGES", "BODY")
_SECTION_RE = re.compile(r"^===(" + "|".join(_SECTIONS) + r")===\s*$", re.MULTILINE)


def parse_refresh_reply(text):
    """Split the model's delimited reply into fields. The body is raw HTML
    after a marker rather than a JSON string — long HTML inside JSON is what
    made earlier rewrite replies unparseable. None when there is no body."""
    parts = _SECTION_RE.split(text or "")
    found = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    body = re.sub(r"^```(?:html)?\s*\n?|\n?```\s*$", "", found.get("BODY", "")).strip()
    if not body:
        return None
    changes = [line.lstrip("-• ").strip() for line in found.get("CHANGES", "").splitlines()]
    return {
        "meta_title": found.get("META_TITLE", ""),
        "meta_description": found.get("META_DESCRIPTION", ""),
        "summary": found.get("SUMMARY", ""),
        "changes": [c for c in changes if c],
        "body": body,
    }


_FILLER_WORDS = {"the", "and", "for", "with", "all"}


def _topic_words(text):
    return {fold_token(t) for t in tokens(text) if len(t) > 2 and t not in _FILLER_WORDS}


def pick_link_targets(topic_text, items, limit=MAX_LINK_TARGETS):
    """Store pages worth linking from an article about topic_text: those
    sharing words with it, most shared first. Items are {title, handle}."""
    topic = _topic_words(topic_text)
    scored = []
    for position, item in enumerate(items):
        shared = len(topic & _topic_words(item.get("title") or ""))
        if shared:
            scored.append((-shared, position, item))
    return [item for _, _, item in sorted(scored)[:limit]]


def _context(article):
    from app.services.intro_link_service import _load_gsc_queries, article_path

    store_id = article["store_id"]
    queries = sorted(
        _load_gsc_queries(store_id).get(article_path(article), []), key=lambda q: -q[2])[:MAX_QUERIES]
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT title, handle FROM synced_collections WHERE store_id = %s AND COALESCE(published, TRUE) "
            "AND COALESCE(handle, '') <> '' ORDER BY products_count DESC NULLS LAST",
            (store_id,),
        )
        collections = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
            (store_id,),
        )
        products = [dict(r) for r in cur.fetchall()]
    topic = " ".join([article.get("title") or "", article.get("primary_keyword") or ""] + [q[0] for q in queries])
    return queries, pick_link_targets(topic, collections), pick_link_targets(topic, products)


def build_user_prompt(article, queries, collections, products):
    parts = [f"ARTICLE TITLE: {article['title']}"]
    if article.get("primary_keyword"):
        parts.append(f"WHAT THE ARTICLE IS ABOUT (its recorded keyword): {article['primary_keyword']}")
    if queries:
        parts.append(
            "SEARCHES GOOGLE SHOWED THIS ARTICLE FOR (last 90 days — query, impressions, clicks):\n"
            + "\n".join(f'- "{q}" — {int(i)} impressions, {int(c)} clicks' for q, c, i in queries)
        )
    else:
        parts.append("Google has shown this article for no searches in the last 90 days.")
    if collections:
        parts.append("COLLECTIONS THE STORE SELLS THAT RELATE TO THIS TOPIC:\n"
                     + "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in collections))
    if products:
        parts.append("PRODUCTS THE STORE SELLS THAT RELATE TO THIS TOPIC:\n"
                     + "\n".join(f"- {p['title']}: /products/{p['handle']}" for p in products))
    parts.append(f"CURRENT META TITLE: {article.get('meta_title') or '(none)'}")
    parts.append(f"CURRENT META DESCRIPTION: {article.get('meta_description') or '(none)'}")
    parts.append(f"CURRENT BODY (HTML):\n{article.get('body') or ''}")
    return "\n\n".join(parts)


def run_article_refresh(article_id):
    """Generate a refresh proposal. Returns a dict for the preview; raises
    ValueError with a user-facing reason when it cannot."""
    from openai import OpenAI

    from app.auth import decrypt_value
    from app.prompt_defaults import DEFAULTS
    from app.services.ai_output_guard import (
        clamp_meta_description,
        restore_missing_figures,
        restore_missing_images,
        sanitize_html,
    )
    from app.services.content_preservation import count_internal_links, missing_preserved
    from app.settings_manager import get_setting

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.*, sb.handle AS blog_handle FROM synced_articles sa
               LEFT JOIN synced_blogs sb ON sb.id = sa.blog_id WHERE sa.id = %s""",
            (article_id,),
        )
        article = cur.fetchone()
        cur.execute("SELECT api_key_enc, model FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not article:
        raise ValueError("Article not found")
    if not (article.get("body") or "").strip():
        raise ValueError("This article has no body to refresh")
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI is not configured — add an OpenRouter key in Settings")
    article = dict(article)

    queries, collections, products = _context(article)
    preserve = get_setting("prompts.preserve_existing", DEFAULTS["prompts.preserve_existing"])
    system = get_setting("prompts.article_refresh", DEFAULTS["prompts.article_refresh"]).replace(
        "{preserve_existing}", preserve)

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=decrypt_value(config["api_key_enc"]),
                    timeout=180.0)
    response = client.chat.completions.create(
        model=config.get("model") or "anthropic/claude-sonnet-4",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": build_user_prompt(article, queries, collections, products)},
        ],
        temperature=0.3,
    )
    parsed = parse_refresh_reply(response.choices[0].message.content or "")
    if not parsed:
        raise ValueError("The AI reply had no article body — try again")

    old_body = article.get("body") or ""
    body = sanitize_html(parsed["body"])
    body = restore_missing_figures(old_body, restore_missing_images(old_body, body))
    words = lambda html: len(re.sub(r"<[^>]+>", " ", html).split())  # noqa: E731
    return {
        "article_id": article_id,
        "title": article["title"],
        "body": body,
        "meta_title": parsed["meta_title"],
        "meta_description": clamp_meta_description(parsed["meta_description"]) if parsed["meta_description"] else "",
        "summary": parsed["summary"],
        "changes": parsed["changes"],
        "queries": [{"query": q, "clicks": int(c), "impressions": int(i)} for q, c, i in queries],
        "before": {
            "meta_title": article.get("meta_title") or "", "meta_description": article.get("meta_description") or "",
            "words": words(old_body), "internal_links": count_internal_links(old_body),
        },
        "after": {"words": words(body), "internal_links": count_internal_links(body)},
        "would_remove": missing_preserved(old_body, body),
    }
