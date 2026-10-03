"""DB + Shopify side of the opening-paragraph collection link fix.

Loads the eligible posts (blog guides + Product News — industry/company news
has no buying intent, per the audit), measures them with intro_link, drafts
intro rewrites and applies approved ones. The pure rules live in
app.services.intro_link.
"""

import logging
import statistics
from datetime import date, timedelta
from urllib.parse import urlparse

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.ai_output_guard import sanitize_html
from app.services.interlinking_analyzer import build_store_hosts
from app.services.intro_link import (
    collection_links,
    dropped_collection_links,
    intro_link_status,
    locate_intro,
    pick_intro_collection,
    rank_collections,
    relevance_to_post,
    rewrite_intro,
    splice_intro,
    visible_text,
)
from app.services.link_candidates import text_tokens
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

PRODUCT_NEWS_CATEGORY = "Product News"
FIXABLE_STATUSES = {"needs_fix", "weak_anchor", "off_target", "no_collection"}
MAX_PLAN_ARTICLES = 50
QUERY_WINDOW_DAYS = 90

_ELIGIBLE_SQL = """
    SELECT sa.id, sa.store_id, sa.shopify_id, sa.title, sa.handle, sa.body, sa.tags,
           sa.meta_title, sa.article_type, sa.category, sb.handle AS blog_handle
    FROM synced_articles sa
    JOIN synced_blogs sb ON sb.id = sa.blog_id
    WHERE sa.store_id = %s AND sa.is_published = TRUE
      AND (sa.article_type = 'blog' OR (sa.article_type = 'news' AND sa.category = %s))
      AND COALESCE(sa.tags, '') NOT ILIKE '%%landing-page%%'
"""


def article_kind(article):
    return "product news" if article.get("article_type") == "news" else "guide"


def article_path(article):
    return f"/blogs/{article.get('blog_handle') or ''}/{article.get('handle') or ''}".lower()


def load_eligible_articles(store_id, article_ids=None):
    sql, params = _ELIGIBLE_SQL, [store_id, PRODUCT_NEWS_CATEGORY]
    if article_ids:
        sql += " AND sa.id = ANY(%s)"
        params.append(list(article_ids))
    with db.get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def load_collection_catalog(store_id):
    """{handle: {title, keyword}} for published collections; keyword is the
    resolver's primary (the anchor the audit wants)."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sc.handle, sc.title,
                      tsa.keyword_resolution->'primary'->>'keyword' AS keyword
               FROM synced_collections sc
               LEFT JOIN technical_seo_analyses tsa
                 ON tsa.store_id = sc.store_id AND tsa.item_type = 'collection' AND tsa.item_id = sc.id
               WHERE sc.store_id = %s AND COALESCE(sc.published, TRUE)
                 AND sc.handle IS NOT NULL AND sc.handle <> ''""",
            (store_id,),
        )
        return {
            r["handle"].lower(): {"title": r["title"] or r["handle"], "keyword": (r["keyword"] or "").strip()}
            for r in cur.fetchall()
        }


def load_store_hosts(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT domain, primary_domain FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone() or {}
    return build_store_hosts(row.get("domain"), row.get("primary_domain"))


def _load_gsc_queries(store_id):
    """{path: [(query, clicks, impressions)]} for blog URLs over the query window."""
    since = date.today() - timedelta(days=QUERY_WINDOW_DAYS + 3)
    out = {}
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT page, query, SUM(clicks) AS clicks, SUM(impressions) AS impressions
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND date >= %s AND page LIKE '%%/blogs/%%'
                     AND query IS NOT NULL AND query <> ''
                   GROUP BY page, query""",
                (store_id, since),
            )
            for r in cur.fetchall():
                path = urlparse(r["page"] or "").path.rstrip("/").lower()
                out.setdefault(path, []).append((r["query"], float(r["clicks"] or 0), float(r["impressions"] or 0)))
    except Exception as e:
        logger.warning("GSC queries unavailable for intro links: %s", e)
    return out


def _load_session_keywords(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT shopify_article_id, target_keywords FROM article_wizard_sessions
               WHERE store_id = %s AND shopify_article_id IS NOT NULL ORDER BY id""",
            (store_id,),
        )
        out = {}
        for r in cur.fetchall():
            kws = r["target_keywords"] or []
            first = kws[0] if kws else None
            if isinstance(first, dict):
                first = first.get("keyword")
            if first:
                out[r["shopify_article_id"]] = str(first)
        return out


def build_context(store_id):
    """Everything drafting/auditing needs for one store, loaded once per batch."""
    from app.routes.blog_routes import _gsc_article_metrics

    catalog = load_collection_catalog(store_id)
    clicks, window = _gsc_article_metrics(store_id)
    return {
        "store_id": store_id,
        "catalog": catalog,
        "hosts": load_store_hosts(store_id),
        "gsc_queries": _load_gsc_queries(store_id),
        "session_keywords": _load_session_keywords(store_id),
        "gsc_metrics": clicks,
        "gsc_window": window,
    }


def primary_query_for(article, ctx):
    """(query, source): the post's top GSC query that shares a word with its
    title, else its wizard primary keyword, else meta title, else title."""
    title_tokens = text_tokens(article.get("title") or "")
    rows = ctx["gsc_queries"].get(article_path(article)) or []
    relevant = [r for r in rows if text_tokens(r[0]) & title_tokens]
    if relevant:
        best = max(relevant, key=lambda r: (r[1], r[2]))
        return best[0], "gsc"
    kw = ctx["session_keywords"].get(article.get("shopify_id"))
    if kw:
        return kw, "wizard"
    if article.get("meta_title"):
        return article["meta_title"], "meta_title"
    return article.get("title") or "", "title"


def audit_article(article, ctx):
    """One row of the Intro links table: status, depth, planned target, clicks."""
    status = intro_link_status(article.get("body"), ctx["hosts"], ctx["catalog"])
    query, source = primary_query_for(article, ctx)
    target = None
    if status["status"] == "ok":
        linked_info = ctx["catalog"].get(status["first_link"]["handle"]) or {}
        if relevance_to_post(query, article.get("title"), linked_info) == 0:
            status["status"] = "off_target"
    if status["status"] in FIXABLE_STATUSES:
        target = pick_intro_collection(query, article.get("title"), status["linked_handles"], ctx["catalog"])
        if target and status["in_p1"] and target["handle"] == status["first_link"]["handle"]:
            target["mode"] = "reanchor"
    metrics = ctx["gsc_metrics"].get(article_path(article)) or {}
    return {
        "article_id": article["id"],
        "title": article.get("title"),
        "handle": article.get("handle"),
        "url_path": article_path(article),
        "kind": article_kind(article),
        "status": status["status"],
        "fixable": status["status"] in FIXABLE_STATUSES and target is not None,
        "in_p1": status["in_p1"],
        "in_first_two": status["in_first_two"],
        "words_before_first": status["words_before_first"],
        "first_link": status["first_link"],
        "linked_handles": status["linked_handles"],
        "primary_query": query,
        "query_source": source,
        "target": target,
        "clicks": metrics.get("clicks"),
        "impressions": metrics.get("impressions"),
    }


def summarize(rows):
    """The audit's table, recomputed live over the eligible posts."""
    depths = [r["words_before_first"] for r in rows if r["words_before_first"] is not None]
    total_clicks = sum(r["clicks"] or 0 for r in rows)
    clicks_without = sum(r["clicks"] or 0 for r in rows if not r["in_p1"])
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "posts": len(rows),
        "guides": sum(1 for r in rows if r["kind"] == "guide"),
        "product_news": sum(1 for r in rows if r["kind"] == "product news"),
        "in_p1": sum(1 for r in rows if r["in_p1"]),
        "in_first_two": sum(1 for r in rows if r["in_first_two"]),
        "median_words_before_first": statistics.median(depths) if depths else None,
        "clicks_share_without_p1": round(clicks_without / total_clicks, 3) if total_clicks else None,
        "fixable": sum(1 for r in rows if r["fixable"]),
        "by_status": by_status,
    }


def target_options(row, ctx, limit=8):
    """Dropdown choices for re-targeting a draft: collections the post already
    links, then the catalog's most relevant ones."""
    ranked = rank_collections(row["primary_query"], row["title"], ctx["catalog"])
    handles = [h for h in row["linked_handles"] if h in ctx["catalog"]]
    handles += [h for h, score in ranked if score > 0 and h not in handles][:limit]
    return [{"handle": h, "title": ctx["catalog"][h]["title"], "keyword": ctx["catalog"][h]["keyword"],
             "linked": h in row["linked_handles"]} for h in handles]


def make_ai_client():
    from openai import OpenAI

    from app.ai_agent import _get_ai_config

    config = _get_ai_config()
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=config["api_key"], timeout=120.0)
    return client, config["model"]


def draft_fix(client, model, article, ctx, target_handle=None):
    """Draft one intro rewrite. Nothing is written anywhere; the returned item
    goes into the plan task's result for review."""
    row = audit_article(article, ctx)
    body = article.get("body") or ""
    span = locate_intro(body)
    item = {
        **{k: row[k] for k in ("article_id", "title", "url_path", "kind", "status", "words_before_first",
                               "primary_query", "query_source", "clicks", "linked_handles")},
        "target_options": target_options(row, ctx),
        "valid": False,
        "violations": [],
    }
    target = row["target"]
    if target_handle:
        handle = target_handle.lower()
        if handle not in ctx["catalog"]:
            item["violations"] = [{"code": "unknown_target", "message": f"Collection '{target_handle}' not found"}]
            return item
        info = ctx["catalog"][handle]
        target = {"handle": handle, "title": info["title"], "keyword": info["keyword"], "href": f"/collections/{handle}",
                  "mode": "reanchor" if row["first_link"] and row["in_p1"] and row["first_link"]["handle"] == handle
                  else ("moved" if handle in row["linked_handles"] else "added"), "score": None}
    if not span:
        item["violations"] = [{"code": "no_intro", "message": "Article does not open with a paragraph"}]
        return item
    if not target:
        reason = "Already compliant" if row["status"] == "ok" else "No relevant collection to link"
        item["violations"] = [{"code": "no_target", "message": reason}]
        return item

    old_intro = body[span[0]:span[1]]
    item.update({"target": target, "before_intro": old_intro})
    system_prompt = get_setting("prompts.article_intro_link", DEFAULTS["prompts.article_intro_link"])
    new_intro, violations, attempts = rewrite_intro(
        client, model, system_prompt,
        {
            "title": article.get("title"),
            "kind": row["kind"],
            "primary_query": row["primary_query"],
            "old_intro": old_intro,
            "following_text": visible_text(body[span[1]:span[1] + 4000]),
            "target": target,
        },
        ctx["hosts"],
    )
    new_intro = sanitize_html(new_intro)
    new_body, unwrapped = splice_intro(body, span, new_intro, target["handle"], ctx["hosts"]) if new_intro else (body, 0)
    item.update({
        "after_intro": new_intro,
        "violations": violations,
        "valid": not violations,
        "attempts": attempts,
        "unwrapped": unwrapped,
        "dropped_collection_links": dropped_collection_links(old_intro, new_intro, target["handle"], ctx["hosts"]) if new_intro else [],
        "links_before": len(collection_links(body, ctx["hosts"])),
        "links_after": len(collection_links(new_body, ctx["hosts"])),
        "before_words": len(visible_text(old_intro).split()),
        "after_words": len(visible_text(new_intro).split()),
    })
    return item


def _same_intro(a, b):
    return " ".join((a or "").split()) == " ".join((b or "").split())


def apply_item(shopify_client, article, item, hosts):
    """Write one approved draft to Shopify. Re-reads the live body first and
    refuses when the live intro no longer matches what was drafted (a hand
    edit would otherwise be overwritten). Returns {status, ...}."""
    live = shopify_client.get_article(article["shopify_id"])
    if not live:
        return {"status": "failed", "error": "Article not found on Shopify"}
    live_body = live.get("body") or ""
    span = locate_intro(live_body)
    if not span or not _same_intro(live_body[span[0]:span[1]], item.get("before_intro")):
        return {"status": "stale", "error": "The live intro changed since this draft — redraft it"}

    # Only the new intro is AI-authored; the rest of the live body is pushed
    # back untouched (sanitizing it would strip embeds some posts carry).
    new_intro = sanitize_html(item["after_intro"])
    new_body, unwrapped = splice_intro(live_body, span, new_intro, item["target"]["handle"], hosts)
    # The intro check above is this flow's stale guard, and the splice unwraps
    # later copies of the link on purpose — so sync the mirror to the live body
    # and let the pipeline handle the write, mirror and history.
    from app.services.apply_pipeline import apply_item_changes

    with db.get_cursor() as cur:
        cur.execute("UPDATE synced_articles SET body = %s WHERE id = %s", (live_body, article["id"]))
    apply_item_changes(
        article["store_id"], "article", article["id"], {"body": new_body},
        source="intro_links", change_type="intro_link", client=shopify_client,
        allow_dropped_blocks=True, restore_media=False,
    )
    _patch_interlinking(article, item)
    return {"status": "applied", "unwrapped": unwrapped}


def _patch_interlinking(article, item):
    from app.services.interlinking_patch import patch_analysis, record_link_added

    target = item["target"]
    try:
        if target.get("mode") == "added":
            record_link_added(
                article["store_id"],
                {"type": "article", "handle": article.get("handle"), "blog_handle": article.get("blog_handle"),
                 "title": article.get("title")},
                {"type": "collection", "handle": target["handle"], "href": target["href"], "title": target.get("title")},
                anchor_text=target.get("keyword") or "",
            )
        else:
            patch_analysis(article["store_id"], lambda analysis: None)
    except Exception as e:
        logger.warning("Interlinking patch after intro fix failed for article %s: %s", article.get("id"), e)


# ── New articles: build the intro link in, then verify it ─────────────


def fallback_link_candidates(store_id, topic_text, research_data=None, limit=30):
    """(products, collections) to offer the writer when nothing was hand-picked:
    research's AI-chosen collections first, then the catalog ranked by relevance
    to the topic — never an alphabetical slice."""
    from app.services.link_candidates import fetch_ranked_candidates

    products, collections = fetch_ranked_candidates(store_id, topic_text, product_limit=limit, collection_limit=limit)
    seeded = [
        {"title": c.get("title"), "handle": c.get("handle")}
        for c in ((research_data or {}).get("related_collections") or [])
        if isinstance(c, dict) and c.get("handle")
    ]
    seen, merged = set(), []
    for c in seeded + collections:
        if c["handle"] not in seen:
            seen.add(c["handle"])
            merged.append(c)
    return products, merged[:limit]


def enforce_product_news_intro(client, model, store_id, body_html, headline):
    """New Product News: move the most relevant collection the link pass
    already placed into the lede's sentence 2-3. Returns (body, qa)."""
    linked = [link["handle"] for link in collection_links(body_html, load_store_hosts(store_id))]
    target = choose_intro_collection(store_id, headline, headline, linked)
    return enforce_intro_link(client, model, store_id, body_html, target, headline, headline,
                              kind="product news", check_query=False)


def choose_intro_collection(store_id, primary_keyword, title, candidate_handles):
    """The collection a new article's intro should link: the best of the
    writer's candidates (hand-picked / research), else the best catalog match."""
    catalog = load_collection_catalog(store_id)
    handles = [h.lower() for h in candidate_handles if h]
    return pick_intro_collection(primary_keyword, title, handles, catalog)


def anchor_guidance(target):
    """Anchor wording to offer the writer: the collection keyword, plus its
    title when the resolver keyword reads badly on its own ("tobacco bulk")."""
    keyword, title = target.get("keyword") or "", target.get("title") or ""
    if keyword and title and keyword.lower() not in title.lower():
        return f"the collection keyword \"{keyword}\" or words from its name \"{title}\" — whichever reads naturally"
    return f"\"{keyword or title}\""


def intro_link_prompt_block(target, primary_keyword):
    return (
        "INTRO (required):\n"
        f"- Sentence 1 answers \"{primary_keyword}\" directly. No scene-setting, no 'the landscape has shifted', "
        "no 'whether you are a seasoned enthusiast or a newcomer'.\n"
        "- Sentence 2 or 3 of the FIRST paragraph links the shop collection, written exactly as "
        f"<a href=\"{target['href']}\">ANCHOR</a>, where ANCHOR is {anchor_guidance(target)}. It is for readers "
        "who already know what they want — one helpful sentence in your own words, not a sales pitch, no stock "
        "phrases like 'skip the reading'.\n"
        f"- This is the article's first link to {target['href']}; do not link that collection again later.\n"
        "- Keep the opening paragraph to 45-100 words.\n"
        "- As everywhere in the article, every link is a complete <a href=\"/...\"> tag with its URL."
    )


def enforce_intro_link(client, model, store_id, body_html, target, primary_keyword, title, kind="guide", check_query=True):
    """Post-generation guarantee: if the writer missed the intro link (or used
    a weak anchor), rewrite just the intro. Returns (body, qa). News passes
    check_query=False: its 'query' is a full headline, too long to require
    verbatim coverage."""
    if not target or not body_html:
        return body_html, {"status": "skipped"}
    catalog = load_collection_catalog(store_id)
    hosts = load_store_hosts(store_id)
    status = intro_link_status(body_html, hosts, catalog)
    first = status["first_link"]
    if status["status"] == "ok" and first["handle"] == target["handle"]:
        return body_html, {"status": "ok", "handle": first["handle"]}
    span = status["intro_span"]
    if not span:
        return body_html, {"status": "no_intro"}
    old_intro = body_html[span[0]:span[1]]
    system_prompt = get_setting("prompts.article_intro_link", DEFAULTS["prompts.article_intro_link"])
    new_intro, violations, attempts = rewrite_intro(
        client, model, system_prompt,
        {
            "title": title,
            "kind": kind,
            "primary_query": primary_keyword,
            "old_intro": old_intro,
            "following_text": visible_text(body_html[span[1]:span[1] + 4000]),
            "target": target,
            **({} if check_query else {"check_query": ""}),
        },
        hosts,
    )
    if violations or not new_intro:
        return body_html, {"status": "repair_failed", "handle": target["handle"],
                           "violations": [v["message"] for v in violations]}
    new_body, unwrapped = splice_intro(body_html, span, sanitize_html(new_intro), target["handle"], hosts)
    return new_body, {"status": "repaired", "handle": target["handle"], "unwrapped": unwrapped, "attempts": attempts,
                      "was": status["status"]}
