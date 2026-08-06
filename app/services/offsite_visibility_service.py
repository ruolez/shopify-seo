"""Off-site visibility audit — third-party corroboration of the brand.

AI answer engines cross-verify a brand against Reddit threads, review
platforms and "best of" roundups before recommending it; a store with zero
third-party mentions reads as unverifiable and gets omitted. This service runs
a bounded set of Brave searches (brand presence, Reddit, review platforms,
category roundups), classifies every result, fetches the top roundup pages to
check in-page brand/competitor mentions, and produces a scorecard + prioritized
actions. Reuses brave_search (24h serp_cache) so same-day re-runs are ~free.
"""

import logging
import re

import requests

from app.database import db
from app.services.serp_scraper import brave_search, _get_brave_api_key
from app.services.ai_visibility_tracker import _brand_terms, _competitor_terms

logger = logging.getLogger(__name__)

MAX_QUERIES = 22
LISTICLE_PAGES_TO_FETCH = 6

CLASSIFY_DOMAINS = {
    "review_platform": {
        "trustpilot.com", "sitejabber.com", "judge.me", "yelp.com", "bbb.org",
        "resellerratings.com", "g2.com", "reviews.io",
    },
    "forum": {"reddit.com", "quora.com"},
    "marketplace": {"amazon.com", "ebay.com", "walmart.com", "etsy.com", "aliexpress.com"},
    "social": {
        "facebook.com", "instagram.com", "youtube.com", "tiktok.com",
        "x.com", "twitter.com", "pinterest.com", "linkedin.com",
    },
}
# Platforms whose presence we surface individually in the scorecard.
TRACKED_REVIEW_PLATFORMS = ["trustpilot.com", "sitejabber.com", "bbb.org", "yelp.com"]

_LISTICLE_TITLE_RE = re.compile(r"\b(best|top\s?\d+|\d+\s+best|roundup|compared|comparison|vs\.?)\b", re.I)
_NEWSISH_RE = re.compile(r"\b(news|magazine|journal|review|guide|blog)\b", re.I)


def _domain_in(dom, dset):
    dom = (dom or "").lower()
    return any(dom == s or dom.endswith("." + s) for s in dset)


def _offsite_brand_terms(store):
    """Strict brand terms for off-site matching.

    The tracker's loose terms include the bare store name — fine inside answers
    to category-specific questions, but in open web search a short name like
    "A2Z" matches every unrelated "A2Z Plumbing". Here we require the domain,
    the domain token, or a spaced name+remainder variant derived from the
    domain (a2z + tobacco -> "a2z tobacco"); a short single-word store name on
    its own never counts.
    Returns (terms, query_name) — query_name is the most human-readable strict
    term, used to build the search queries.
    """
    host = (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/").lower()
    root = host.split(".")[0] if host else ""
    name = (store.get("name") or "").strip().lower()
    nn = re.sub(r"[^a-z0-9]", "", name)

    terms = set()
    if host:
        terms.add(host)
    if root and len(root) >= 6:
        terms.add(root)
    spaced = None
    if nn and root.startswith(nn) and len(root) > len(nn):
        spaced = f"{nn} {root[len(nn):]}"
        terms.add(spaced)
    # A name is specific enough on its own only if multi-word or reasonably long.
    if name and (len(name.split()) >= 2 or len(name) >= 6):
        terms.add(name)
    terms = {t for t in terms if len(t) >= 6}
    if not terms and host:
        terms = {host}

    query_name = spaced or (name if name in terms else None) or root or host
    return terms, query_name


_COMPOUND_RE_CACHE = {}


def _brand_present(text, terms):
    """True when the text refers to OUR brand.

    Domain-derived terms match as plain substrings. Spaced name terms use a
    negative lookahead so compound business names — "A2Z Tobacco & Vape",
    "A2Z Tobacco and Vapes" (real, unrelated brick-and-mortar shops) — don't
    count as us.
    """
    low = (text or "").lower()
    for t in terms:
        if "." in t or " " not in t:
            if t in low:
                return True
        else:
            rx = _COMPOUND_RE_CACHE.get(t)
            if rx is None:
                # "A2Z Tobacco & Vape", "A2Z Tobacco Station", "a2z tobacco vape
                # shop" are other businesses — a compound suffix disqualifies.
                rx = re.compile(
                    re.escape(t)
                    + r"(?!\s*(?:&|\+|and\s+vapes?\b|n\s+vapes?\b)"
                    + r"|\s+(?:vapes?|station|shop|shops|mart|market|outlet|express|zone|stop|plus|city|hut|corner|depot)\b)"
                )
                _COMPOUND_RE_CACHE[t] = rx
            if rx.search(low):
                return True
    return False


def _top_categories(store_id, n=4):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT product_type, COUNT(*) AS c FROM synced_products
               WHERE store_id = %s AND product_type <> ''
               GROUP BY product_type ORDER BY c DESC LIMIT %s""",
            (store_id, n),
        )
        cats = [r["product_type"].strip().lower() for r in cur.fetchall()]
        if not cats:
            cur.execute(
                "SELECT title FROM synced_collections WHERE store_id = %s ORDER BY products_count DESC NULLS LAST LIMIT %s",
                (store_id, n),
            )
            cats = [r["title"].strip().lower() for r in cur.fetchall() if r["title"]]
    return list(dict.fromkeys(cats))[:n]


def build_query_set(store, categories, query_name=None):
    """Bounded, deterministic (query, query_kind) list — ≤ MAX_QUERIES.

    query_name is the strict brand phrase from _offsite_brand_terms (e.g.
    "a2z tobacco", never a bare ambiguous "A2Z"); the domain is probed too.
    """
    name = query_name or (store.get("name") or "").strip()
    host = (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/").lower()
    queries = [
        (f'"{name}" reviews', "brand_reviews"),
        (f'site:reddit.com "{name}"', "reddit"),
        (f"{name} trustpilot", "review_platform"),
        (f'"{name}"', "brand_general"),
    ]
    if host:
        queries.append((f'"{host}"', "brand_general"))
    for cat in categories[:4]:
        queries.append((f"best {cat} online stores", "listicle"))
        queries.append((f"best place to buy {cat} online", "listicle"))
    queries.append((f"site:trustpilot.com {name}", "review_platform"))
    queries.append((f"site:sitejabber.com {name}", "review_platform"))
    return queries[:MAX_QUERIES]


def classify_result(result, store_host, competitors):
    """-> (classification, brand_present_in_snippet, competitor_labels)."""
    dom = (result.get("domain") or "").lower()
    text = f"{result.get('title') or ''} {result.get('description') or ''}".lower()

    if store_host and (dom == store_host or dom.endswith("." + store_host)):
        cls = "own_site"
    elif _domain_in(dom, CLASSIFY_DOMAINS["review_platform"]):
        cls = "review_platform"
    elif _domain_in(dom, CLASSIFY_DOMAINS["forum"]):
        cls = "forum"
    elif _domain_in(dom, CLASSIFY_DOMAINS["marketplace"]):
        cls = "marketplace"
    elif _domain_in(dom, CLASSIFY_DOMAINS["social"]):
        cls = "social"
    elif _LISTICLE_TITLE_RE.search(result.get("title") or ""):
        cls = "listicle"
    elif any(any(t in dom for t in c["terms"] if "." in t) for c in competitors):
        cls = "competitor"
    elif _NEWSISH_RE.search(dom):
        cls = "news"
    else:
        cls = "other"

    comp_labels = [c["label"] for c in competitors if any(t in text or t in dom for t in c["terms"])]
    return cls, comp_labels


def _fetch_listicle_mentions(urls, brand_terms, competitors):
    """{url: {"brand": bool, "competitors": [labels]}} via full-page fetches."""
    out = {}
    for url in urls[:LISTICLE_PAGES_TO_FETCH]:
        try:
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                out[url] = {"brand": None, "competitors": []}  # unknown (blocked)
                continue
            low = (resp.text or "").lower()
            out[url] = {
                "brand": _brand_present(low, brand_terms),
                "competitors": [c["label"] for c in competitors if any(t in low for t in c["terms"])],
            }
        except Exception:
            out[url] = {"brand": None, "competitors": []}
    return out


def compute_scorecard(rows, listicle_mentions, brand_terms):
    reddit_rows = [r for r in rows if r["classification"] == "forum" and "reddit" in (r["domain"] or "")]
    reddit_hits = [r for r in reddit_rows if r["brand_present"]]

    review_platforms = {}
    for plat in TRACKED_REVIEW_PLATFORMS:
        review_platforms[plat.split(".")[0]] = any(
            _domain_in(r["domain"], {plat}) and r["brand_present"] for r in rows
        )

    listicle_rows = [r for r in rows if r["classification"] == "listicle"]
    checked = {}
    for r in listicle_rows:
        if r["url"] in listicle_mentions:
            checked[r["url"]] = listicle_mentions[r["url"]]
    included = sum(1 for m in checked.values() if m.get("brand"))
    comp_inclusions = {}
    for m in checked.values():
        for label in m.get("competitors") or []:
            comp_inclusions[label] = comp_inclusions.get(label, 0) + 1

    news_mentions = sum(
        1 for r in rows
        if r["classification"] in ("news", "other") and r["brand_present"] and r["query_kind"] in ("brand_general", "brand_reviews")
    )
    own = sum(1 for r in rows if r["classification"] == "own_site")

    return {
        "reddit": {"found": bool(reddit_hits), "count": len(reddit_hits)},
        "review_platforms": review_platforms,
        "listicles": {
            "checked": len(checked),
            "included": included,
            "competitor_inclusions": comp_inclusions,
        },
        "news_mentions": news_mentions,
        "own_site_share": round(own / len(rows) * 100) if rows else 0,
    }


def build_actions(scorecard, categories):
    actions = []
    lists = scorecard["listicles"]
    comp_best = sorted(lists["competitor_inclusions"].items(), key=lambda kv: -kv[1])
    if lists["checked"] and not lists["included"]:
        why = "AI engines lift store recommendations from these roundups — absence means you can't be recommended."
        if comp_best:
            why += f" {comp_best[0][0]} appears in {comp_best[0][1]} of them."
        actions.append({
            "priority": "high",
            "title": f"Not present in any \"best {categories[0] if categories else 'category'}\" roundup checked",
            "why": why,
            "action": "Pitch the authors of the top roundups for inclusion, and publish your own comparison page targeting the same query so it can rank alongside them.",
        })
    if not scorecard["reddit"]["found"]:
        actions.append({
            "priority": "high",
            "title": "No Reddit threads mention your brand",
            "why": "Reddit is a top citation source for ChatGPT and Perplexity on 'trust-heavy' shopping questions.",
            "action": "Participate genuinely in your category's subreddits (answer questions, disclose affiliation). Never astroturf — communities and AI both discount it.",
        })
    missing_platforms = [k for k, v in scorecard["review_platforms"].items() if not v]
    if missing_platforms:
        actions.append({
            "priority": "medium",
            "title": f"No presence found on: {', '.join(missing_platforms)}",
            "why": "Review-platform profiles are the corroboration AI engines check before calling a store reputable.",
            "action": "Claim your Trustpilot/BBB profiles and start inviting post-purchase reviews.",
        })
    if not scorecard["news_mentions"]:
        actions.append({
            "priority": "medium",
            "title": "No third-party articles mention your brand",
            "why": "Independent coverage builds the entity authority models rely on.",
            "action": "Do digital PR in your niche: expert commentary, data pieces, or local-business coverage that earns a named mention.",
        })
    if not actions:
        actions.append({
            "priority": "low",
            "title": "Off-site presence looks healthy",
            "why": "You appear across the checked channels.",
            "action": "Keep review volume growing and re-audit monthly to catch regressions.",
        })
    return actions


def run_offsite_audit(run_id, store_id):
    with db.get_cursor() as cur:
        cur.execute("UPDATE offsite_runs SET status = 'running' WHERE id = %s", (run_id,))
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = dict(cur.fetchone())
    try:
        _loose_terms, store_host = _brand_terms(store)
        brand_terms, query_name = _offsite_brand_terms(store)
        competitors = _competitor_terms(store_id)
        categories = _top_categories(store_id)
        queries = build_query_set(store, categories, query_name)

        # A missing key is a configuration problem; a None result mid-loop is a
        # transient search failure. Conflating them blamed configuration for
        # rate limits and let mostly-failed audits complete silently.
        if not _get_brave_api_key():
            raise ValueError("Brave Search is not configured — add your Brave API key in Settings.")

        rows = []
        ran = 0
        failed = 0
        for query, kind in queries:
            results = brave_search(query, count=8)
            if results is None:
                failed += 1
                continue
            ran += 1
            for r in results:
                cls, comp_labels = classify_result(r, store_host, competitors)
                text = f"{r.get('title') or ''} {r.get('description') or ''} {r.get('url') or ''}".lower()
                brand_present = _brand_present(text, brand_terms)
                row = {
                    "query": query, "query_kind": kind, "position": r.get("position"),
                    "title": r.get("title"), "url": r.get("url"), "domain": r.get("domain"),
                    "description": r.get("description"), "classification": cls,
                    "brand_present": brand_present, "competitors_present": comp_labels,
                }
                rows.append(row)

        # Deep-check the top roundup pages: SERP snippets rarely show every
        # store a listicle covers — the page body is the truth.
        listicle_urls = list(dict.fromkeys(
            r["url"] for r in rows if r["classification"] == "listicle" and r["url"]
        ))
        mentions = _fetch_listicle_mentions(listicle_urls, brand_terms, competitors)
        for r in rows:
            m = mentions.get(r["url"])
            if m and m.get("brand") is not None:
                r["brand_present"] = r["brand_present"] or m["brand"]
                r["competitors_present"] = sorted(set(r["competitors_present"]) | set(m["competitors"]))

        import json as _json

        with db.get_cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT INTO offsite_results
                        (run_id, store_id, query, query_kind, position, title, url, domain,
                         description, classification, brand_present, competitors_present)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run_id, store_id, r["query"], r["query_kind"], r["position"],
                        r["title"], r["url"], r["domain"], r["description"],
                        r["classification"], r["brand_present"], _json.dumps(r["competitors_present"]),
                    ),
                )

        if ran == 0:
            raise ValueError(
                f"All {len(queries)} searches failed — Brave Search may be rate-limited "
                "or temporarily unavailable. Try again in a few minutes."
            )

        scorecard = compute_scorecard(rows, mentions, brand_terms)
        if failed:
            scorecard["queries_failed"] = failed
            scorecard["queries_planned"] = len(queries)
        actions = build_actions(scorecard, categories)
        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE offsite_runs SET status = 'completed', queries_run = %s,
                       scorecard = %s, actions = %s, completed_at = NOW() WHERE id = %s""",
                (ran, _json.dumps(scorecard), _json.dumps(actions), run_id),
            )
    except Exception as e:
        logger.exception("Off-site audit %s failed: %s", run_id, e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE offsite_runs SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), run_id),
            )
