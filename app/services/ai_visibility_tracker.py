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

from openai import OpenAI

from app.database import db
from app.auth import decrypt_value

logger = logging.getLogger(__name__)

# Rough blended $/token estimate for budget gating (small models).
COST_PER_TOKEN = 0.6 / 1_000_000
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")

# Retailer/brand extraction from model answers.
_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9][a-z0-9-]{1,40}\.)+(?:com|net|org|co|io|shop|store|cigars))\b", re.I)
_LIST_BOLD_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*\*\*\s*([^*\n]{2,45}?)\s*\*\*", re.M)
# Domains that are references, not competing storefronts.
_DOMAIN_STOPLIST = {
    "wikipedia.org", "reddit.com", "youtube.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "quora.com", "example.com", "google.com", "bing.com",
    "tiktok.com", "pinterest.com", "yelp.com",
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
        if is_domain and raw.lower() in _DOMAIN_STOPLIST:
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
    return client, cfg, models


def _host(value):
    return (value or "").replace("https://", "").replace("http://", "").strip("/").lower()


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
        terms = {t for t in {_host(r["domain"]), _host(r["domain"]).split(".")[0], (r["name"] or "").lower()} if len(t) >= 3}
        out.append({"label": label, "terms": terms})
    return out


def _product_titles(store_id, limit=300):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT title FROM synced_products WHERE store_id = %s AND title IS NOT NULL LIMIT %s",
            (store_id, limit),
        )
        return [r["title"] for r in cur.fetchall() if r["title"]]


def _detect_mentions(text, brand_terms, brand_host, product_titles, competitors, intent="buyer"):
    low = text.lower()
    brand_mentioned = any(t in low for t in brand_terms)
    domain_cited = bool(brand_host) and brand_host in low

    brand_rank = None
    for i, line in enumerate(text.splitlines()):
        if _LIST_MARKER.match(line) and any(t in line.lower() for t in brand_terms):
            brand_rank = i + 1
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


def _query_model(client, model, prompt):
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    content = resp.choices[0].message.content or ""
    tokens = getattr(resp, "usage", None)
    total = getattr(tokens, "total_tokens", 0) if tokens else 0
    return content, total


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
    content, _ = _query_model(client, cfg.get("model") or "openai/gpt-4o-mini", prompt)
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
        excluded = set()
        if run:
            cur.execute("SELECT brand_key FROM ai_excluded_brands WHERE store_id = %s", (run["store_id"],))
            excluded = {r["brand_key"] for r in cur.fetchall()}
        cur.execute(
            """SELECT m.*, p.intent FROM ai_mention_results m
               LEFT JOIN ai_prompts p ON p.id = m.prompt_id WHERE m.run_id = %s""",
            (run_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return {"brand_mention_rate": 0, "citation_rate": 0, "share_of_voice": 0,
                "avg_rank": None, "brand_known_rate": None, "competitors": []}

    buyer = [r for r in rows if (r.get("intent") or "buyer") != "brand"]
    brand_rows = [r for r in rows if r.get("intent") == "brand"]

    total_buyer = len(buyer) or 1
    brand_hits = sum(1 for r in buyer if r["brand_mentioned"])
    cite_hits = sum(1 for r in buyer if r["domain_cited"])
    ranks = [r["brand_rank"] for r in buyer if r["brand_rank"]]

    # Aggregate every retailer/brand the models recommended across buyer answers.
    counts, display, domains = {}, {}, {}
    for r in buyer:
        for b in (r["recommended_brands"] or []):
            key = b.get("key")
            if not key or key in excluded:
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

    configured = _competitor_terms(run["store_id"]) if run else []
    def _is_configured(key):
        return any(any(t in key or key in t for t in c["terms"]) for c in configured)

    competitors = sorted(
        ({"key": k, "name": display[k], "domain": domains.get(k), "count": v, "is_configured": _is_configured(k)} for k, v in counts.items()),
        key=lambda c: -c["count"],
    )[:25]

    brand_known_rate = None
    if brand_rows:
        known = sum(1 for r in brand_rows if r["brand_known"])
        brand_known_rate = round(known / len(brand_rows) * 100, 2)

    return {
        "brand_mention_rate": round(brand_hits / total_buyer * 100, 2),
        "citation_rate": round(cite_hits / total_buyer * 100, 2),
        "share_of_voice": round(sov, 2),
        "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "brand_known_rate": brand_known_rate,
        "competitors": competitors,
    }


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
        budget = float(cfg.get("daily_budget") or 10.0)
        runs_per_prompt = max(1, int(cfg.get("runs_per_prompt") or 3))
        brand_terms, brand_host = _brand_terms(store)
        competitors = _competitor_terms(store_id)
        titles = _product_titles(store_id)

        cost = 0.0
        capped = False
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
                    try:
                        text, tokens = _query_model(client, model, pr["prompt"])
                    except Exception as e:
                        logger.warning("Tracking query failed (%s): %s", model, e)
                        continue
                    cost += tokens * COST_PER_TOKEN
                    det = _detect_mentions(text, brand_terms, brand_host, titles, competitors, intent)
                    with db.get_cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO ai_mention_results
                                (run_id, prompt_id, model, prompt, response_text, brand_mentioned,
                                 domain_cited, brand_rank, products_mentioned, competitors_mentioned,
                                 recommended_brands, brand_known, run_index, tokens_used)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                run_id, pr["id"], model, pr["prompt"], text[:4000],
                                det["brand_mentioned"], det["domain_cited"], det["brand_rank"],
                                json.dumps(det["products_mentioned"]),
                                json.dumps(det["competitors_mentioned"]),
                                json.dumps(det["recommended_brands"]), det["brand_known"],
                                run_index, tokens,
                            ),
                        )

        agg = compute_share_of_voice(run_id)
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
                    json.dumps({"competitors": agg["competitors"], "brand_known_rate": agg["brand_known_rate"]}), run_id,
                ),
            )
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


def start_tracking_run(store_id, model_ids=None):
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO ai_tracking_runs (store_id, models, status) VALUES (%s, %s, 'pending') RETURNING id",
            (store_id, json.dumps(model_ids or [])),
        )
        run_id = cur.fetchone()["id"]
    threading.Thread(target=run_tracking, args=(run_id,), daemon=True).start()
    return run_id


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
    text, _ = _query_model(client, cfg.get("model") or "openai/gpt-4o-mini", prompt)
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
