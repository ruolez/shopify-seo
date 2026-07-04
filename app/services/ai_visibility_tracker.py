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


def _detect_mentions(text, brand_terms, brand_host, product_titles, competitors):
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
    return {
        "brand_mentioned": brand_mentioned,
        "domain_cited": domain_cited,
        "brand_rank": brand_rank,
        "products_mentioned": products,
        "competitors_mentioned": comps,
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
        cur.execute("SELECT name FROM stores WHERE id = %s", (store_id,))
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
    saved = []
    with db.get_cursor() as cur:
        for p in prompts:
            if not isinstance(p, str) or not p.strip():
                continue
            cur.execute(
                "INSERT INTO ai_prompts (store_id, prompt, intent) VALUES (%s, %s, 'buyer') RETURNING id",
                (store_id, p.strip()),
            )
            saved.append({"id": cur.fetchone()["id"], "prompt": p.strip()})
    return saved


def compute_share_of_voice(run_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_mention_results WHERE run_id = %s", (run_id,))
        rows = cur.fetchall()
    if not rows:
        return {"brand_mention_rate": 0, "citation_rate": 0, "share_of_voice": 0, "avg_rank": None, "competitors": {}}

    total = len(rows)
    brand_hits = sum(1 for r in rows if r["brand_mentioned"])
    cite_hits = sum(1 for r in rows if r["domain_cited"])
    ranks = [r["brand_rank"] for r in rows if r["brand_rank"]]
    comp_counts = {}
    comp_total = 0
    for r in rows:
        for c in (r["competitors_mentioned"] or []):
            comp_counts[c] = comp_counts.get(c, 0) + 1
            comp_total += 1
    sov = (brand_hits / (brand_hits + comp_total) * 100) if (brand_hits + comp_total) else 0
    return {
        "brand_mention_rate": round(brand_hits / total * 100, 2),
        "citation_rate": round(cite_hits / total * 100, 2),
        "share_of_voice": round(sov, 2),
        "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
        "competitors": comp_counts,
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
                "SELECT id, prompt FROM ai_prompts WHERE store_id = %s AND is_active = TRUE",
                (store_id,),
            )
            prompts = cur.fetchall()

        if not prompts:
            raise ValueError("No active prompts. Generate a prompt set first.")

        client, cfg, default_models = _client_and_config()
        models = run["models"] or default_models
        budget = float(cfg.get("daily_budget") or 10.0)
        brand_terms, brand_host = _brand_terms(store)
        competitors = _competitor_terms(store_id)
        titles = _product_titles(store_id)

        cost = 0.0
        capped = False
        for pr in prompts:
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
                det = _detect_mentions(text, brand_terms, brand_host, titles, competitors)
                with db.get_cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ai_mention_results
                            (run_id, prompt_id, model, prompt, response_text, brand_mentioned,
                             domain_cited, brand_rank, products_mentioned, competitors_mentioned, tokens_used)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id, pr["id"], model, pr["prompt"], text[:4000],
                            det["brand_mentioned"], det["domain_cited"], det["brand_rank"],
                            json.dumps(det["products_mentioned"]),
                            json.dumps(det["competitors_mentioned"]), tokens,
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
                    json.dumps({"competitors": agg["competitors"]}), run_id,
                ),
            )
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
