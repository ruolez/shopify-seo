"""AI visibility tracking — measure how often the store is mentioned/cited
by LLM answer engines.

Runs a managed buyer-intent prompt set through several OpenRouter models
(proxies for ChatGPT / Claude / Gemini), detects brand/product/competitor
mentions deterministically, and computes share-of-voice over time.

Caveat: base models via OpenRouter reflect training-data brand knowledge, not
live retrieval — this is "AI model mention tracking," not Perplexity/AI-Overview
citation tracking. Budget-gated on ai_config.daily_budget.
"""

import json
import logging
import re
import threading
from urllib.parse import urlparse

from openai import OpenAI

from app.database import db
from app.auth import decrypt_value

logger = logging.getLogger(__name__)

# Rough blended $/token estimate for budget gating (small models).
COST_PER_TOKEN = 0.6 / 1_000_000
# Web-grounded models carry search fees on top of tokens — budget them higher.
GROUNDED_COST_PER_TOKEN = 6.0 / 1_000_000
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")

# Retailer/brand extraction from model answers.
_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9][a-z0-9-]{1,40}\.)+(?:com|net|org|co|io|shop|store|cigars))\b", re.I)
_LIST_BOLD_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*\*\*\s*([^*\n]{2,45}?)\s*\*\*", re.M)
# Domains that are references, not competing storefronts.
_DOMAIN_STOPLIST = {
    "wikipedia.org", "reddit.com", "redditinc.com", "youtube.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "quora.com", "example.com", "google.com",
    "bing.com", "tiktok.com", "pinterest.com", "yelp.com",
}


def _stoplisted_domain(dom):
    """True for reference/social sites, including subdomains (en.wikipedia.org)."""
    return any(dom == s or dom.endswith("." + s) for s in _DOMAIN_STOPLIST)


def _cite_domain(url):
    """Bare hostname of a citation URL ('https://www.reddit.com/r/x' -> 'reddit.com')."""
    try:
        host = urlparse(url if "://" in (url or "") else "https://" + (url or "")).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _classify_source(domain, url_path, store_host, competitor_keys):
    """Bucket a cited domain the way the off-site audit buckets SERP results.

    Citations carry no page title, so the listicle check runs against the URL
    path slug instead. Lazy import — offsite_visibility_service imports from
    this module at top level."""
    from app.services.offsite_visibility_service import (
        CLASSIFY_DOMAINS, _domain_in, _LISTICLE_TITLE_RE, _NEWSISH_RE,
    )

    if store_host and (domain == store_host or domain.endswith("." + store_host)):
        return "own_site"
    if _domain_in(domain, CLASSIFY_DOMAINS["review_platform"]):
        return "review_platform"
    if _domain_in(domain, CLASSIFY_DOMAINS["forum"]):
        return "forum"
    if _domain_in(domain, CLASSIFY_DOMAINS["marketplace"]):
        return "marketplace"
    if _domain_in(domain, CLASSIFY_DOMAINS["social"]):
        return "social"
    if _key_in(_canonical_brand_key(domain), competitor_keys):
        return "competitor"
    if _LISTICLE_TITLE_RE.search((url_path or "").replace("-", " ").replace("/", " ")):
        return "listicle"
    if _NEWSISH_RE.search(domain):
        return "news"
    return "other"


# Generic place descriptors models use instead of naming a store — not competitors.
_GENERIC_RETAILERS = {
    "gas station", "gas stations", "convenience store", "convenience stores",
    "smoke shop", "smoke shops", "local smoke shop", "local smoke shops",
    "vape shop", "vape shops", "tobacco shop", "tobacco shops", "local tobacco shop",
    "online marketplace", "online marketplaces", "online retailers", "online stores",
    "online tobacco retailers", "grocery store", "grocery stores", "drugstore",
    "drugstores", "liquor store", "liquor stores", "local retailers", "head shop",
    "head shops", "specialty tobacco shops", "specialty tobacco shop", "dispensary",
    "dispensaries", "wholesale tobacco suppliers",
}
# "I don't know this brand" markers → the model has no real knowledge of the store.
_LABEL_STOPLIST = {
    "best for", "flavor profile", "the experience", "pros", "cons", "note", "tip", "tips",
    "overview", "summary", "price", "pricing", "quality", "key features", "why", "how",
    "availability", "shipping", "selection", "variety", "recommendation", "recommendations",
    "conclusion", "features", "description", "value", "taste", "strength", "verdict",
}
_UNKNOWN_RE = re.compile(
    r"don't have (?:any )?(?:specific |particular )?information|not familiar|no (?:specific )?information|"
    r"couldn't find|can't find|unable to find|i'm not aware|i am not aware|do not have details",
    re.I,
)


def _norm_brand(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower()).rstrip(".,;:")


def _canonical_brand_key(name):
    """Collapse a brand name or domain to one comparable token.

    'JR Cigars', 'jrcigars.com', 'www.jrcigars.com' -> 'jrcigars', so the same
    retailer named different ways de-duplicates to a single entry.
    """
    n = (name or "").lower().strip()
    m = re.match(r"^(?:www\.)?([a-z0-9-]+)\.[a-z0-9.]+$", n)
    root = m.group(1) if m else n
    return re.sub(r"[^a-z0-9]", "", root)


def _extract_recommended_brands(text, brand_terms):
    """Deterministically pull the retailers/brands a model recommends in its answer.

    Uses list-item bold names (the common "1. **Store**" format) and bare domains,
    de-duplicating a retailer named both as a word and a domain (canonical key) and
    preferring the domain form for display. Excludes our own brand and reference sites.
    """
    brand_ck = {c for c in (_canonical_brand_key(t) for t in brand_terms) if c}
    seen = {}  # canonical_key -> {"name": human-name|None, "domain": domain|None}

    def add(raw, is_domain):
        ck = _canonical_brand_key(raw)
        if len(ck) < 3:
            return
        if is_domain and _stoplisted_domain(raw.lower()):
            return
        if not is_domain:
            r = raw.strip()
            # Bold section labels ("Best for:", "Flavor Profile:") aren't brands.
            if r.endswith(":") or len(r.split()) > 4 or _norm_brand(r) in _LABEL_STOPLIST:
                return
        if any(b in ck or ck in b for b in brand_ck):
            return
        e = seen.setdefault(ck, {"name": None, "domain": None})
        if is_domain:
            if not e["domain"]:
                e["domain"] = raw.lower()
        elif not e["name"]:
            e["name"] = raw.strip()

    for name in _LIST_BOLD_RE.findall(text or ""):
        add(name, False)
    for dom in _DOMAIN_RE.findall(text or ""):
        add(dom, True)
    return [
        {"key": k, "name": e["name"] or e["domain"], "domain": e["domain"]}
        for k, e in list(seen.items())[:20]
    ]


def _ai_config_row():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_config ORDER BY id LIMIT 1")
        return cur.fetchone()


def _client_and_config():
    cfg = _ai_config_row()
    if not cfg or not cfg.get("api_key_enc"):
        raise ValueError("AI not configured. Set your OpenRouter API key in Settings.")
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=decrypt_value(cfg["api_key_enc"]),
        timeout=90.0,
    )
    models = cfg.get("tracking_models") or [
        "openai/gpt-4o-mini",
        "anthropic/claude-haiku-4.5",
        "google/gemini-3-flash-preview",
    ]
    # Retrieval-backed models probe what live AI search surfaces actually say,
    # not just training-data memory. Merged in, deduped, order preserved.
    grounded = cfg.get("grounded_models") or []
    if not grounded or grounded == ["perplexity/sonar"]:
        # Never-customized default: pair Perplexity with an OpenAI search model so
        # grounded coverage spans both major retrieval surfaces (Perplexity + Bing).
        grounded = ["perplexity/sonar", "openai/gpt-4o-mini-search-preview"]
    models = list(dict.fromkeys(list(models) + list(grounded)))
    return client, cfg, models


def _is_grounded_model(model, cfg=None):
    """True for models that answer with live web retrieval."""
    if model.endswith(":online") or model.endswith("-search-preview") or model.startswith("perplexity/"):
        return True
    return model in ((cfg or {}).get("grounded_models") or [])


def _host(value):
    host = (value or "").replace("https://", "").replace("http://", "").strip("/").lower()
    return host[4:] if host.startswith("www.") else host


def _brand_terms(store):
    host = _host(store.get("primary_domain") or store.get("domain"))
    terms = set()
    if store.get("name"):
        terms.add(store["name"].lower())
    if host:
        terms.add(host)
        # bare brand token, e.g. "a2ztobacco" from "a2ztobacco.com"
        terms.add(host.split(".")[0])
    return {t for t in terms if len(t) >= 3}, host


def _competitor_terms(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT domain, name FROM competitors WHERE store_id = %s", (store_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        label = r["name"] or r["domain"]
        # terms → substring match against answer TEXT (mention detection).
        terms = {t for t in {_host(r["domain"]), _host(r["domain"]).split(".")[0], (r["name"] or "").lower()} if len(t) >= 3}
        # keys → exact canonical-key match against a run's brand key (tracked badge).
        # Include both the name- and domain-derived keys: a brand recommended by name
        # is keyed off its name, but may be tracked by a different domain.
        keys = {k for k in (_canonical_brand_key(r["name"]), _canonical_brand_key(r["domain"])) if k}
        out.append({"label": label, "terms": terms, "keys": keys})
    return out


def _product_titles(store_id, limit=300):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT title FROM synced_products WHERE store_id = %s AND title IS NOT NULL LIMIT %s",
            (store_id, limit),
        )
        return [r["title"] for r in cur.fetchall() if r["title"]]


def _detect_mentions(text, brand_terms, brand_host, product_titles, competitors, intent="buyer",
                     citations=None):
    low = text.lower()
    brand_mentioned = any(t in low for t in brand_terms)
    # Cited = one of the answer's source URLs is on our domain (grounded
    # answers return them); our domain written in the text counts too.
    cited_hosts = {urlparse(u).netloc.lower().removeprefix("www.") for u in (citations or []) if isinstance(u, str)}
    domain_cited = bool(brand_host) and (brand_host in low or brand_host in cited_hosts)

    # Rank = ordinal among LIST ITEMS, not the raw line index (which counted
    # intro paragraphs and blank lines, inflating avg_rank).
    brand_rank = None
    list_pos = 0
    for line in text.splitlines():
        if not _LIST_MARKER.match(line):
            continue
        list_pos += 1
        if any(t in line.lower() for t in brand_terms):
            brand_rank = list_pos
            break

    products = [t for t in product_titles if len(t) > 6 and t.lower() in low][:10]
    comps = [c["label"] for c in competitors if any(t in low for t in c["terms"])]
    recommended = _extract_recommended_brands(text, brand_terms)

    # For brand-aware prompts, "known" = named + no ignorance disclaimer.
    brand_known = None
    if intent == "brand":
        brand_known = brand_mentioned and not bool(_UNKNOWN_RE.search(text))

    return {
        "brand_mentioned": brand_mentioned,
        "domain_cited": domain_cited,
        "brand_rank": brand_rank,
        "products_mentioned": products,
        "competitors_mentioned": comps,
        "recommended_brands": recommended,
        "brand_known": brand_known,
    }


def _extract_citations(message):
    """URL citations from OpenRouter web-grounded responses (defensive: the
    annotations shape varies by provider; any failure yields [])."""
    try:
        ann = getattr(message, "annotations", None)
        if not ann and getattr(message, "model_extra", None):
            ann = message.model_extra.get("annotations")
        urls = []
        for a in ann or []:
            if not isinstance(a, dict):
                a = getattr(a, "model_dump", lambda: {})() or {}
            uc = a.get("url_citation") or {}
            url = uc.get("url")
            if url and url not in urls:
                urls.append(url)
        return urls[:20]
    except Exception:
        return []


def _query_model(client, model, prompt, temperature=0.3):
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    msg = resp.choices[0].message
    content = msg.content or ""
    tokens = getattr(resp, "usage", None)
    total = getattr(tokens, "total_tokens", 0) if tokens else 0
    return content, total, _extract_citations(msg)


def _extract_entities_llm(client, model, question, answer, store_name, brand_ck, citations=None):
    """One cheap structured call per answer: which retailers does it send buyers to,
    and which product brands does it mention? Far more reliable than the regex
    fallback at telling 'JR Cigars' from a 'Flavor profile:' heading. When the
    answer is web-grounded, `citations` (its cited URLs) is passed so the same
    call also attributes which sources back each retailer. Returns
    (entities, tokens); entities is None when the response can't be parsed."""
    sources_block, sources_instr = "", ""
    shape = '{"retailers": [{"name": string, "domain": string|null}], "product_brands": [string]}'
    if citations:
        sources_block = (
            "\nSOURCES the answer cited (numbered):\n"
            + "\n".join(f"[{i}] {u}" for i, u in enumerate(citations, 1)) + "\n"
        )
        sources_instr = (
            " For each retailer, also list which SOURCE numbers from the numbered list "
            "support or mention it — the cited pages the answer draws on when "
            "recommending it. Use [] when unclear."
        )
        shape = (
            '{"retailers": [{"name": string, "domain": string|null, "sources": [int]}], '
            '"product_brands": [string]}'
        )
    prompt = (
        "A shopper asked an AI assistant a question; the answer is below. Identify the "
        "entities the answer mentions.\n\n"
        f"QUESTION: {question}\n\nANSWER:\n{answer}\n{sources_block}\n"
        "Extract:\n"
        "1. retailers — actual stores, websites, or marketplaces the answer points to as "
        f"PLACES TO BUY. Include a domain only if it literally appears in the answer.{sources_instr}\n"
        "2. product_brands — product brands / manufacturers mentioned (things you buy, "
        "not places to buy).\n"
        "Do NOT include section headings, advice phrases, product categories, or generic "
        f'terms — only real, named businesses and brands. Do not include "{store_name}".\n'
        f"Return ONLY JSON: {shape}"
    )
    text, tokens, _cites = _query_model(client, model, prompt, temperature=0)
    s, e = text.find("{"), text.rfind("}") + 1
    if s < 0 or e <= s:
        return None, tokens
    try:
        data = json.loads(text[s:e])
    except (json.JSONDecodeError, ValueError):
        return None, tokens

    retailers, seen = [], set()
    for r in data.get("retailers") or []:
        if isinstance(r, str):
            r = {"name": r}
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or "").strip()
        if name and _norm_brand(name) in _GENERIC_RETAILERS:
            continue
        # The extractor sometimes returns a full URL as the domain — keep the host only.
        domain = _host(r.get("domain") or "").split("/")[0].removeprefix("www.") or None
        if domain and (_stoplisted_domain(domain) or "." not in domain):
            domain = None
        # Domain-derived keys are stable across runs; name variants aren't.
        key = _canonical_brand_key(domain or name)
        if not key or len(key) < 3 or key in seen:
            continue
        if any(b in key or key in b for b in brand_ck):
            continue
        seen.add(key)
        entry = {"key": key, "name": name or domain, "domain": domain}
        if citations:
            urls = []
            for idx in r.get("sources") or []:
                if isinstance(idx, int) and 1 <= idx <= len(citations) and citations[idx - 1] not in urls:
                    urls.append(citations[idx - 1])
            entry["urls"] = urls[:5]
        retailers.append(entry)

    brands, bseen = [], set()
    for b in data.get("product_brands") or []:
        if not isinstance(b, str) or not b.strip():
            continue
        name = b.strip()
        key = _canonical_brand_key(name)
        if not key or len(key) < 2 or key in bseen or key in seen:
            continue
        if any(bb in key or key in bb for bb in brand_ck):
            continue
        bseen.add(key)
        brands.append({"key": key, "name": name})
    return {"retailers": retailers[:20], "product_brands": brands[:25]}, tokens


def _vendor_keys(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT vendor FROM synced_products WHERE store_id = %s AND vendor <> ''",
            (store_id,),
        )
        return {k for k in (_canonical_brand_key(r["vendor"]) for r in cur.fetchall()) if k}


def _brand_prefs(store_id):
    """Excluded / tracked canonical-key sets from ai_brand_prefs."""
    with db.get_cursor() as cur:
        cur.execute("SELECT brand_key, status FROM ai_brand_prefs WHERE store_id = %s", (store_id,))
        rows = cur.fetchall()
    excluded = {r["brand_key"] for r in rows if r["status"] == "excluded"}
    tracked = {r["brand_key"] for r in rows if r["status"] == "tracked"}
    return excluded, tracked


def _key_in(key, keys):
    """Key-set membership tolerant of singular/plural name variants
    ('jrcigar' vs 'jrcigars') so prefs survive across runs."""
    if not key:
        return False
    if key in keys or (key + "s") in keys:
        return True
    return key.endswith("s") and key[:-1] in keys


def generate_prompt_set(store_id, n=12):
    """One cheap LLM call to seed buyer-intent prompts; saves to ai_prompts."""
    client, cfg, _ = _client_and_config()
    with db.get_cursor() as cur:
        cur.execute("SELECT name, primary_domain, domain FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        cur.execute(
            "SELECT DISTINCT product_type FROM synced_products WHERE store_id = %s AND product_type <> '' LIMIT 25",
            (store_id,),
        )
        types = [r["product_type"] for r in cur.fetchall() if r["product_type"]]
        cur.execute(
            "SELECT DISTINCT vendor FROM synced_products WHERE store_id = %s AND vendor <> '' LIMIT 25",
            (store_id,),
        )
        vendors = [r["vendor"] for r in cur.fetchall() if r["vendor"]]

    prompt = (
        f"You generate buyer-intent search prompts that shoppers type into AI assistants "
        f"(ChatGPT, Perplexity, Gemini) when researching what to buy. The store '{store['name']}' "
        f"sells these product types: {', '.join(types[:20]) or 'general merchandise'}. "
        f"Brands carried: {', '.join(vendors[:15]) or 'various'}.\n\n"
        f"Write {n} natural, generic buyer-intent prompts (do NOT name this store) that a shopper "
        f"would ask an AI when deciding what/where to buy in these categories — a mix of "
        f"'best X', 'where to buy X', 'X vs Y', and 'recommend a X for <use case>'.\n"
        f"Return ONLY JSON: {{\"prompts\": [string, ...]}}."
    )
    content, _, _cites = _query_model(client, cfg.get("model") or "openai/gpt-4o-mini", prompt)
    s, e = content.find("{"), content.rfind("}") + 1
    prompts = []
    if s >= 0 and e > s:
        try:
            prompts = json.loads(content[s:e]).get("prompts", [])
        except (json.JSONDecodeError, ValueError):
            prompts = []
    # Brand-aware prompts (intent='brand') — measure what the models know about us.
    name = store["name"]
    host = _host(store.get("primary_domain") or store.get("domain"))
    category = (types[0] if types else "products")
    brand_prompts = [
        f"What do you know about {name}" + (f" ({host})" if host else "") + "?",
        f"Is {name} a reputable online store to buy {category}?",
        f"How does {name} compare to other {category} retailers online?",
    ]

    saved = []
    with db.get_cursor() as cur:
        for p in prompts:
            if not isinstance(p, str) or not p.strip():
                continue
            cur.execute(
                "INSERT INTO ai_prompts (store_id, prompt, intent) VALUES (%s, %s, 'buyer') RETURNING id",
                (store_id, p.strip()),
            )
            saved.append({"id": cur.fetchone()["id"], "prompt": p.strip(), "intent": "buyer"})
        for p in brand_prompts:
            cur.execute(
                "INSERT INTO ai_prompts (store_id, prompt, intent) VALUES (%s, %s, 'brand') RETURNING id",
                (store_id, p),
            )
            saved.append({"id": cur.fetchone()["id"], "prompt": p, "intent": "brand"})
    return saved


def compute_share_of_voice(run_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT store_id FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        domain_cache = {}
        store_host = ""
        if run:
            cur.execute(
                "SELECT brand_key, domain FROM brand_domains WHERE store_id = %s AND domain <> ''",
                (run["store_id"],),
            )
            domain_cache = {r["brand_key"]: r["domain"] for r in cur.fetchall()}
            cur.execute("SELECT * FROM stores WHERE id = %s", (run["store_id"],))
            store = cur.fetchone()
            if store:
                store_host = _host(store.get("primary_domain") or store.get("domain"))
        cur.execute(
            """SELECT m.*, p.intent FROM ai_mention_results m
               LEFT JOIN ai_prompts p ON p.id = m.prompt_id WHERE m.run_id = %s""",
            (run_id,),
        )
        rows = cur.fetchall()
    excluded, tracked = _brand_prefs(run["store_id"]) if run else (set(), set())
    if not rows:
        return {"brand_mention_rate": 0, "citation_rate": 0, "share_of_voice": 0,
                "avg_rank": None, "brand_known_rate": None, "competitors": [],
                "product_brands": [], "total_answers": 0, "brand_hits": 0,
                "sources": [], "grounded_answers_with_citations": 0}

    buyer = [r for r in rows if (r.get("intent") or "buyer") != "brand"]
    brand_rows = [r for r in rows if r.get("intent") == "brand"]

    total_buyer = len(buyer) or 1
    brand_hits = sum(1 for r in buyer if r["brand_mentioned"])
    cite_hits = sum(1 for r in buyer if r["domain_cited"])
    ranks = [r["brand_rank"] for r in buyer if r["brand_rank"]]

    # Aggregate every retailer the models recommended across buyer answers.
    counts, display, domains = {}, {}, {}
    for r in buyer:
        for b in (r["recommended_brands"] or []):
            key = b.get("key")
            if not key or _key_in(key, excluded):
                continue
            # Screens generic descriptors already stored by earlier runs.
            if _norm_brand(b.get("name") or "") in _GENERIC_RETAILERS:
                continue
            counts[key] = counts.get(key, 0) + 1
            name = b.get("name") or key
            # Prefer a human name (no dot) as the display label.
            if key not in display or ("." in display[key] and "." not in name):
                display[key] = name
            if b.get("domain") and not domains.get(key):
                domains[key] = b["domain"]
    competitor_total = sum(counts.values())
    sov = (brand_hits / (brand_hits + competitor_total) * 100) if (brand_hits + competitor_total) else 0

    competitors = sorted(
        ({"key": k, "name": display[k], "domain": domains.get(k) or domain_cache.get(k),
          "count": v, "tracked": _key_in(k, tracked)} for k, v in counts.items()),
        key=lambda c: -c["count"],
    )[:25]

    # Which web pages grounded answers actually cited, and which competitors each
    # page backs. Attribution per answer: the extractor's per-retailer source URLs
    # when present, plus a deterministic hostname match (competitor's own domain
    # appearing among the citations) that also covers runs recorded before
    # attribution existed.
    comp_domain_keys = {}
    for k in counts:
        d = _host(domains.get(k) or domain_cache.get(k) or "")
        if d:
            comp_domain_keys[d] = k
    src, comp_sources, grounded_cited = {}, {}, 0
    for r in buyer:
        if not r.get("grounded"):
            continue
        cited = {}  # domain -> first url in this answer
        for u in (r.get("citations") or []):
            d = _cite_domain(u)
            if d and d not in cited:
                cited[d] = u
        if not cited:
            continue
        grounded_cited += 1
        for d, u in cited.items():
            e = src.setdefault(d, {"count": 0, "cites_you": False, "comp_keys": set(), "url": u})
            e["count"] += 1
            if store_host and (d == store_host or d.endswith("." + store_host)):
                e["cites_you"] = True
        for b in (r["recommended_brands"] or []):
            key = b.get("key")
            if not key or key not in counts:
                continue
            attributed = {d for d in (_cite_domain(u) for u in (b.get("urls") or [])) if d in cited}
            bdom = _host(b.get("domain") or "")
            for d in cited:
                if d == bdom or comp_domain_keys.get(d) == key or _canonical_brand_key(d) == key:
                    attributed.add(d)
            for d in attributed:
                src[d]["comp_keys"].add(key)
                by_dom = comp_sources.setdefault(key, {})
                by_dom[d] = by_dom.get(d, 0) + 1
    for c in competitors:
        by_dom = comp_sources.get(c["key"])
        if by_dom:
            c["sources"] = [
                {"domain": d, "count": n}
                for d, n in sorted(by_dom.items(), key=lambda kv: -kv[1])[:5]
            ]
    sources = []
    for d, e in sorted(src.items(), key=lambda kv: -kv[1]["count"])[:15]:
        try:
            path = urlparse(e["url"]).path or ""
        except ValueError:
            path = ""
        sources.append({
            "domain": d,
            "count": e["count"],
            "classification": _classify_source(d, path, store_host, set(counts)),
            "cites_you": e["cites_you"],
            "competitors": sorted(e["comp_keys"], key=lambda k: -counts.get(k, 0))[:6],
            "url": e["url"],
        })

    # Product brands mentioned in answers — informational, kept out of SoV math.
    pb_counts, pb_display, pb_carried = {}, {}, {}
    for r in buyer:
        for b in (r.get("product_brands") or []):
            key = b.get("key")
            if not key or _key_in(key, excluded):
                continue
            pb_counts[key] = pb_counts.get(key, 0) + 1
            pb_display.setdefault(key, b.get("name") or key)
            pb_carried[key] = pb_carried.get(key) or bool(b.get("carried"))
    product_brands = sorted(
        ({"key": k, "name": pb_display[k], "count": v, "carried": pb_carried[k]}
         for k, v in pb_counts.items()),
        key=lambda c: -c["count"],
    )[:25]

    brand_known_rate = None
    if brand_rows:
        known = sum(1 for r in brand_rows if r["brand_known"])
        brand_known_rate = round(known / len(brand_rows) * 100, 2)

    # Memory (base-model recall) vs retrieval (live web-grounded answers) split.
    def _rates(rows_subset):
        n = len(rows_subset)
        if not n:
            return None
        return {
            "answers": n,
            "mention_rate": round(sum(1 for r in rows_subset if r["brand_mentioned"]) / n * 100, 2),
            "citation_rate": round(sum(1 for r in rows_subset if r["domain_cited"]) / n * 100, 2),
        }

    memory = _rates([r for r in buyer if not r.get("grounded")])
    retrieval = _rates([r for r in buyer if r.get("grounded")])

    # Per-model scoreboard: which models know / name / cite the store.
    by_model = {}
    for r in rows:
        m = by_model.setdefault(r["model"], {
            "model": r["model"], "grounded": bool(r.get("grounded")),
            "buyer_answers": 0, "mentions": 0, "cites": 0,
            "brand_answers": 0, "brand_known": 0,
        })
        if (r.get("intent") or "buyer") == "brand":
            m["brand_answers"] += 1
            if r.get("brand_known"):
                m["brand_known"] += 1
        else:
            m["buyer_answers"] += 1
            if r["brand_mentioned"]:
                m["mentions"] += 1
            if r["domain_cited"]:
                m["cites"] += 1
    models_out = []
    for m in by_model.values():
        models_out.append({
            "model": m["model"],
            "grounded": m["grounded"],
            "buyer_answers": m["buyer_answers"],
            "mention_rate": round(m["mentions"] / m["buyer_answers"] * 100, 1) if m["buyer_answers"] else None,
            "citation_rate": round(m["cites"] / m["buyer_answers"] * 100, 1) if m["buyer_answers"] else None,
            "brand_known_rate": round(m["brand_known"] / m["brand_answers"] * 100) if m["brand_answers"] else None,
        })
    models_out.sort(key=lambda m: (m["grounded"], m["model"]))

    return {
        "brand_mention_rate": round(brand_hits / total_buyer * 100, 2),
        "citation_rate": round(cite_hits / total_buyer * 100, 2),
        "share_of_voice": round(sov, 2),
        "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "brand_known_rate": brand_known_rate,
        "memory": memory,
        "retrieval": retrieval,
        "models": models_out,
        "competitors": competitors,
        "product_brands": product_brands,
        "total_answers": len(buyer),
        "brand_hits": brand_hits,
        "sources": sources,
        "grounded_answers_with_citations": grounded_cited,
    }


def _lookup_official_domain(name, store_host):
    """Find a brand's official website via a Brave search (cached in serp_cache).

    Returns a bare hostname (e.g. "dutchmasters.com") or None. Skips social/reference
    sites and our own store. Best-effort — any failure yields None.
    """
    from app.services.serp_scraper import brave_search

    try:
        results = brave_search(f"{name} official website", count=5) or []
    except Exception:
        return None
    for r in results:
        dom = _host(r.get("domain") or "")
        if not dom or _stoplisted_domain(dom):
            continue
        if store_host and (dom == store_host or dom.endswith("." + store_host)):
            continue
        return dom
    return None


def resolve_brand_domains(store_id, run_id, limit=25):
    """Fill missing official websites for a run's recommended brands.

    Persists every attempt to brand_domains (empty domain = looked-up, none found)
    so each brand is only ever queried once. Bounded by `limit` per call to cap
    Brave usage. Returns the number of brands resolved to a real domain.
    """
    agg = compute_share_of_voice(run_id)
    missing = [c for c in agg["competitors"] if not c.get("domain")]
    if not missing:
        return 0

    with db.get_cursor() as cur:
        cur.execute("SELECT primary_domain, domain FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone() or {}
        cur.execute("SELECT brand_key FROM brand_domains WHERE store_id = %s", (store_id,))
        already = {r["brand_key"] for r in cur.fetchall()}
    store_host = _host(store.get("primary_domain") or store.get("domain"))

    todo = [c for c in missing if c["key"] not in already][:limit]
    resolved = 0
    for c in todo:
        dom = _lookup_official_domain(c["name"], store_host) or ""
        if dom:
            resolved += 1
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO brand_domains (store_id, brand_key, name, domain)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (store_id, brand_key)
                   DO UPDATE SET domain = EXCLUDED.domain, name = EXCLUDED.name, resolved_at = NOW()""",
                (store_id, c["key"], c["name"], dom),
            )
    return resolved


def run_tracking(run_id):
    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tracking_runs SET status = 'running' WHERE id = %s", (run_id,))
        cur.execute("SELECT * FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
    try:
        store_id = run["store_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
            store = dict(cur.fetchone())
            cur.execute(
                "SELECT id, prompt, intent FROM ai_prompts WHERE store_id = %s AND is_active = TRUE",
                (store_id,),
            )
            prompts = cur.fetchall()

        if not prompts:
            raise ValueError("No active prompts. Generate a prompt set first.")

        client, cfg, default_models = _client_and_config()
        models = run["models"] or default_models
        # The budget is per day, not per run: earlier runs today count.
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(cost_estimate), 0) AS spent FROM ai_tracking_runs
                   WHERE store_id = %s AND id <> %s
                     AND COALESCE(completed_at, started_at)::date = CURRENT_DATE""",
                (store_id, run_id),
            )
            spent_today = float(cur.fetchone()["spent"] or 0)
        budget = max(0.0, float(cfg.get("daily_budget") or 10.0) - spent_today)
        runs_per_prompt = max(1, int(cfg.get("runs_per_prompt") or 3))
        brand_terms, brand_host = _brand_terms(store)
        brand_ck = {c for c in (_canonical_brand_key(t) for t in brand_terms) if c}
        competitors = _competitor_terms(store_id)
        titles = _product_titles(store_id)
        vendors = _vendor_keys(store_id)
        extract_model = cfg.get("model") or "openai/gpt-4o-mini"

        cost = 0.0
        capped = False
        planned = sum(
            (runs_per_prompt if (p.get("intent") or "buyer") == "buyer" else 1) for p in prompts
        ) * len(models)
        attempted = 0
        query_failures = 0
        last_query_error = None
        for pr in prompts:
            if capped:
                break
            intent = pr.get("intent") or "buyer"
            # Buyer prompts get multiple runs for signal; brand prompts are near-deterministic.
            n_runs = runs_per_prompt if intent == "buyer" else 1
            for run_index in range(n_runs):
                if capped:
                    break
                for model in models:
                    if cost >= budget:
                        capped = True
                        break
                    attempted += 1
                    try:
                        text, tokens, citations = _query_model(client, model, pr["prompt"])
                    except Exception as e:
                        logger.warning("Tracking query failed (%s): %s", model, e)
                        query_failures += 1
                        last_query_error = f"{model}: {e}"
                        continue
                    grounded = _is_grounded_model(model, cfg)
                    cost += tokens * (GROUNDED_COST_PER_TOKEN if grounded else COST_PER_TOKEN)
                    det = _detect_mentions(text, brand_terms, brand_host, titles, competitors, intent,
                                           citations=citations)
                    # Grounded answers cite real URLs — count our domain there too.
                    if grounded and brand_host and any(brand_host in (u or "").lower() for u in citations):
                        det["domain_cited"] = True
                    # LLM entity extraction beats the regex at separating real
                    # retailers from headings and product brands; regex result
                    # (already in det) stays as the fallback.
                    product_brands = []
                    if intent != "brand":
                        try:
                            entities, etok = _extract_entities_llm(
                                client, extract_model, pr["prompt"], text[:4000], store["name"], brand_ck,
                                citations=citations if grounded else None,
                            )
                            cost += etok * COST_PER_TOKEN
                            if entities is not None:
                                det["recommended_brands"] = entities["retailers"]
                                product_brands = [
                                    {**b, "carried": b["key"] in vendors} for b in entities["product_brands"]
                                ]
                        except Exception as e:
                            logger.warning("Entity extraction failed (%s): %s", extract_model, e)
                    with db.get_cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO ai_mention_results
                                (run_id, prompt_id, model, prompt, response_text, brand_mentioned,
                                 domain_cited, brand_rank, products_mentioned, competitors_mentioned,
                                 recommended_brands, product_brands, brand_known, run_index, tokens_used,
                                 grounded, citations)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                run_id, pr["id"], model, pr["prompt"], text[:4000],
                                det["brand_mentioned"], det["domain_cited"], det["brand_rank"],
                                json.dumps(det["products_mentioned"]),
                                json.dumps(det["competitors_mentioned"]),
                                json.dumps(det["recommended_brands"]),
                                json.dumps(product_brands), det["brand_known"],
                                run_index, tokens,
                                grounded, json.dumps(citations),
                            ),
                        )

        # A run where every model query threw is a broken check, not a report
        # of zero visibility — mark it failed instead of writing zeroed metrics.
        if attempted > 0 and query_failures >= attempted:
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_tracking_runs SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                    (f"All {attempted} model queries failed — last error: {last_query_error}", run_id),
                )
            return

        agg = compute_share_of_voice(run_id)
        summary = _run_summary(agg)
        summary["queries_planned"] = planned
        summary["queries_asked"] = attempted
        if query_failures:
            summary["query_failures"] = {
                "count": query_failures,
                "last_error": (last_query_error or "")[:500],
            }
        if capped:
            summary["truncated"] = (
                f"Daily budget ${budget:.2f} reached — {planned - attempted} of {planned} "
                "model queries skipped; raise the budget in Settings or re-run for full coverage"
            )
        with db.get_cursor() as cur:
            cur.execute(
                """
                UPDATE ai_tracking_runs SET status = %s, prompt_count = %s, brand_mention_rate = %s,
                    citation_rate = %s, share_of_voice = %s, avg_rank = %s, cost_estimate = %s,
                    summary = %s, completed_at = NOW() WHERE id = %s
                """,
                (
                    "budget_capped" if capped else "completed",
                    len(prompts), agg["brand_mention_rate"], agg["citation_rate"],
                    agg["share_of_voice"], agg["avg_rank"], round(cost, 4),
                    json.dumps(summary), run_id,
                ),
            )
        # Resolve official websites for the recommended brands so the competitor
        # panel shows real domains immediately. Best-effort — never fails the run.
        try:
            if agg["competitors"]:
                resolve_brand_domains(run["store_id"], run_id)
        except Exception as e:
            logger.warning("Brand-domain resolution failed for run %s: %s", run_id, e)
        # Auto-generate the competitive gap recommendations so they appear without
        # an extra click. Best-effort — a crawl/LLM hiccup must not fail the run.
        try:
            if agg["competitors"]:
                analyze_competitive_gap(run_id)
        except Exception as e:
            logger.warning("Auto gap-analysis failed for run %s: %s", run_id, e)
    except Exception as e:
        logger.exception("Tracking run %s failed: %s", run_id, e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE ai_tracking_runs SET status = 'failed', error = %s, completed_at = NOW() WHERE id = %s",
                (str(e), run_id),
            )


def _run_summary(agg):
    return {
        "competitors": agg["competitors"],
        "product_brands": agg["product_brands"],
        "brand_known_rate": agg["brand_known_rate"],
        "memory": agg.get("memory"),
        "retrieval": agg.get("retrieval"),
        "models": agg.get("models"),
        "total_answers": agg["total_answers"],
        "brand_hits": agg["brand_hits"],
        "sources": agg.get("sources"),
    }


def start_tracking_run(store_id, model_ids=None):
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tracking_runs (store_id, models, status) VALUES (%s, %s, 'pending') RETURNING id",
            (store_id, json.dumps(model_ids or [])),
        )
        run_id = cur.fetchone()["id"]
    threading.Thread(target=run_tracking, args=(run_id,), daemon=True).start()
    return run_id


def reanalyze_run(run_id):
    """Re-extract entities from a run's stored answers with the LLM extractor and
    recompute its aggregates — cleans up runs recorded by the old regex extractor
    without paying for new model answers."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            raise ValueError("Run not found")
        prior_status = run["status"]
        cur.execute("UPDATE ai_tracking_runs SET status = 'reanalyzing' WHERE id = %s", (run_id,))
    try:
        store_id = run["store_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
            store = dict(cur.fetchone())
            cur.execute(
                """SELECT m.id, m.prompt, m.response_text, m.citations, m.grounded, p.intent
                   FROM ai_mention_results m
                   LEFT JOIN ai_prompts p ON p.id = m.prompt_id WHERE m.run_id = %s""",
                (run_id,),
            )
            rows = cur.fetchall()
        client, cfg, _ = _client_and_config()
        extract_model = cfg.get("model") or "openai/gpt-4o-mini"
        brand_terms, _bh = _brand_terms(store)
        brand_ck = {c for c in (_canonical_brand_key(t) for t in brand_terms) if c}
        vendors = _vendor_keys(store_id)
        cost = float(run.get("cost_estimate") or 0)
        for r in rows:
            if (r.get("intent") or "buyer") == "brand" or not r.get("response_text"):
                continue
            try:
                entities, tokens = _extract_entities_llm(
                    client, extract_model, r.get("prompt") or "", r["response_text"], store["name"], brand_ck,
                    citations=(r.get("citations") or None) if r.get("grounded") else None,
                )
            except Exception as e:
                logger.warning("Reanalyze extraction failed (%s): %s", extract_model, e)
                continue
            cost += tokens * COST_PER_TOKEN
            if entities is None:
                continue
            pbs = [{**b, "carried": b["key"] in vendors} for b in entities["product_brands"]]
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE ai_mention_results SET recommended_brands = %s, product_brands = %s WHERE id = %s",
                    (json.dumps(entities["retailers"]), json.dumps(pbs), r["id"]),
                )
        agg = compute_share_of_voice(run_id)
        final_status = prior_status if prior_status in ("completed", "budget_capped") else "completed"
        summary = _run_summary(agg)
        prior_summary = run.get("summary") or {}
        if isinstance(prior_summary, str):
            try:
                prior_summary = json.loads(prior_summary)
            except (json.JSONDecodeError, ValueError):
                prior_summary = {}
        for k in ("queries_planned", "queries_asked", "truncated", "query_failures"):
            if k in prior_summary:
                summary[k] = prior_summary[k]
        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE ai_tracking_runs SET status = %s, brand_mention_rate = %s, citation_rate = %s,
                       share_of_voice = %s, avg_rank = %s, cost_estimate = %s, summary = %s WHERE id = %s""",
                (
                    final_status, agg["brand_mention_rate"], agg["citation_rate"],
                    agg["share_of_voice"], agg["avg_rank"], round(cost, 4),
                    json.dumps(summary), run_id,
                ),
            )
        try:
            if agg["competitors"]:
                resolve_brand_domains(store_id, run_id)
        except Exception as e:
            logger.warning("Brand-domain resolution failed for run %s: %s", run_id, e)
    except Exception as e:
        logger.exception("Reanalyze of run %s failed: %s", run_id, e)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ai_tracking_runs SET status = %s WHERE id = %s", (prior_status, run_id))


def start_reanalyze(run_id):
    threading.Thread(target=reanalyze_run, args=(run_id,), daemon=True).start()


_DOMAINISH_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)


def _crawl_homepages(urls):
    import asyncio
    from app.services.serp_scraper import scrape_serp_pages

    if not urls:
        return {}
    try:
        pages = asyncio.run(scrape_serp_pages(urls, max_pages=len(urls)))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            pages = loop.run_until_complete(scrape_serp_pages(urls, max_pages=len(urls)))
        finally:
            loop.close()
    return {p["domain"]: p for p in pages if p and p.get("domain")}


def analyze_competitive_gap(run_id):
    """Crawl the AI-recommended competitor sites, compare to our store's readiness,
    and produce prioritized improvement recommendations (stored on the run)."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_tracking_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
        if not run:
            raise ValueError("Run not found")
        store_id = run["store_id"]
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = dict(cur.fetchone())
        cur.execute(
            "SELECT prompt FROM ai_prompts WHERE store_id = %s AND (intent IS NULL OR intent <> 'brand') LIMIT 8",
            (store_id,),
        )
        buyer_prompts = [r["prompt"] for r in cur.fetchall()]
        cur.execute(
            """SELECT COUNT(*) n, ROUND(AVG(score)) overall,
                ROUND(AVG((breakdown->'structured_data'->>'score')::numeric)) sd,
                ROUND(AVG((breakdown->'extractability'->>'score')::numeric)) ex,
                ROUND(AVG((breakdown->'fact_density'->>'score')::numeric)) fd,
                ROUND(AVG((breakdown->'entity_authority'->>'score')::numeric)) ea
               FROM ai_seo_scores WHERE store_id = %s""",
            (store_id,),
        )
        prof = cur.fetchone() or {}

    agg = compute_share_of_voice(run_id)
    competitors = agg["competitors"]
    if not competitors:
        raise ValueError("No competitors found in this run yet — run tracking first.")

    domain_comps = [c for c in competitors if c.get("domain")][:5]
    scraped = _crawl_homepages([f"https://{c['domain']}" for c in domain_comps])

    comp_lines = []
    for c in competitors[:12]:
        label = c["name"] + (f" ({c['domain']})" if c.get("domain") else "")
        line = f"- {label} (recommended {c['count']}x by AI)"
        pg = scraped.get(c.get("domain")) if c.get("domain") else None
        if pg:
            schema = ", ".join(pg.get("schema_types") or []) or "none"
            line += f" — homepage schema: {schema}; word count: {pg.get('word_count', 0)}; H2s: {', '.join((pg.get('h2s') or [])[:4])}"
        comp_lines.append(line)

    client, cfg, _ = _client_and_config()
    host = _host(store.get("primary_domain") or store.get("domain"))
    prompt = (
        "You are a GEO/AEO strategist for an ecommerce store that is nearly invisible in AI shopping "
        "answers while the competitors below get recommended. Produce concrete, page-level fixes.\n\n"
        f"OUR STORE: {store['name']} ({host}). AI-readiness averages across {prof.get('n', 0)} product/collection "
        f"pages — overall {prof.get('overall')}/100, structured-data {prof.get('sd')}, extractability "
        f"{prof.get('ex')}, fact-density {prof.get('fd')}, entity-authority {prof.get('ea')}. "
        f"Current AI share-of-voice: {agg['share_of_voice']}%.\n\n"
        "BUYER QUERIES WE WANT TO WIN:\n" + "\n".join(f"- {p}" for p in buyer_prompts) + "\n\n"
        "COMPETITORS THE AI RECOMMENDS INSTEAD (with observable homepage signals):\n"
        + "\n".join(comp_lines) + "\n\n"
        "Give two kinds of recommendations, grounded in the gap between our signals and what these "
        "competitors do:\n"
        "1) page_improvements — specific changes to make on our PRODUCT and COLLECTION pages to close the "
        "gap (schema, extractable Q&A, comparison tables, specs, trust signals, etc.).\n"
        "2) content_ideas — new content/pages to publish (buying guides, comparisons, how-tos) that would "
        "let us compete for the buyer queries above the way these competitors do.\n\n"
        "Return ONLY JSON: {\"summary\": string, "
        "\"page_improvements\": [{\"title\": string, \"why\": string, \"action\": string, "
        "\"priority\": \"high\"|\"medium\"|\"low\", \"applies_to\": \"products\"|\"collections\"|\"both\"}], "
        "\"content_ideas\": [{\"title\": string, \"format\": string, \"target_query\": string, \"why\": string}]}."
    )
    text, _, _cites = _query_model(client, cfg.get("model") or "openai/gpt-4o-mini", prompt)
    s, e = text.find("{"), text.rfind("}") + 1
    analysis = {}
    if s >= 0 and e > s:
        try:
            analysis = json.loads(text[s:e])
        except (json.JSONDecodeError, ValueError):
            analysis = {}
    if not analysis:
        analysis = {"summary": text[:600], "page_improvements": [], "content_ideas": []}
    # Backward-compat: fold any old-style flat recommendations into page_improvements.
    if analysis.get("recommendations") and not analysis.get("page_improvements"):
        analysis["page_improvements"] = analysis.pop("recommendations")
    analysis["competitors_analyzed"] = [c["name"] for c in domain_comps]

    with db.get_cursor() as cur:
        cur.execute("UPDATE ai_tracking_runs SET gap_analysis = %s WHERE id = %s", (json.dumps(analysis), run_id))
    return analysis


class AiTrackingScheduler:
    """Periodically re-run tracking for every store that has active prompts."""

    def __init__(self, interval_hours=168):
        self.interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("AI visibility tracking scheduler started (every %d hours)", self.interval // 3600)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        # Delay first tick so startup isn't hammered.
        self._stop_event.wait(600)
        while not self._stop_event.is_set():
            try:
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT DISTINCT store_id FROM ai_prompts WHERE is_active = TRUE"
                    )
                    stores = [r["store_id"] for r in cur.fetchall()]
                for sid in stores:
                    start_tracking_run(sid)
            except Exception as e:
                logger.error("Scheduled tracking error: %s", e)
            self._stop_event.wait(self.interval)


_tracking_scheduler = None


def init_tracking_scheduler():
    global _tracking_scheduler
    if _tracking_scheduler is None:
        with db.get_cursor() as cur:
            cur.execute("SELECT tracking_interval_hours FROM ai_config ORDER BY id LIMIT 1")
            row = cur.fetchone()
        interval = (row and row.get("tracking_interval_hours")) or 168
        _tracking_scheduler = AiTrackingScheduler(interval_hours=interval)
        _tracking_scheduler.start()
