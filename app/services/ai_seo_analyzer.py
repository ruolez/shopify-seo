"""Deterministic AI-readiness (GEO/AEO/LLMO) scoring.

Mirrors seo_analyzer.py: pure functions, weights/thresholds from settings,
returns {"score": 0-100, "breakdown": {component: {"score", "detail"}}}.

No LLM calls — every sub-score is computed from text/markup heuristics so a
full store can be scanned for free. The qualitative levers a regex can't judge
(fluency, authoritative voice) are handled later in the review-first optimize
step, not here.

Signal design follows the Princeton GEO findings (Aggarwal et al., KDD 2024):
adding statistics, quotations and cited sources measurably lifts AI-citation
rate, and LLMs reward fact density, entity authority and extractable answers.
"""

import re
from datetime import datetime, timezone

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS


# AI crawler user-agents that answer engines use to fetch source content.
AI_BOTS = [
    "GPTBot",
    "ChatGPT-User",
    "OAI-SearchBot",
    "ClaudeBot",
    "anthropic-ai",
    "PerplexityBot",
    "Google-Extended",
    "CCBot",
    "Applebot-Extended",
]

_MEASURE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?"
    r"(?:%|percent|mg|ml|oz|lb|lbs|kg|g|cm|mm|in|inch(?:es)?|ft|"
    r"years?|yrs?|months?|weeks?|days?|hours?|hrs?|min(?:ute)?s?|x)\b",
    re.I,
)
_BARE_NUMBER_RE = re.compile(r"\b\d{2,}\b")
_CURRENCY_RE = re.compile(r"[$£€]\s?\d")
_REVIEW_RE = re.compile(
    r"\b\d[\d,]*\s?(?:reviews?|ratings?|customers?|sold|units?|orders?)\b", re.I
)
_CITATION_RE = re.compile(
    r"according to|\bresearch\b|\bstud(?:y|ies)\b|\bsurvey\b|source:|\[\d+\]", re.I
)
_QUOTE_RE = re.compile(r"[\"“][^\"”]{12,}[\"”]|<blockquote", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"<h[2-4][^>]*>(.*?)</h[2-4]>", re.I | re.S)
_LI_RE = re.compile(r"<li\b", re.I)
_TABLE_RE = re.compile(r"<table\b", re.I)
_EXTERNAL_LINK_RE = re.compile(r'href=["\']https?://([^/"\']+)', re.I)
_QUESTION_WORDS = re.compile(
    r"^(how|what|why|when|where|which|who|can|does|do|is|are|should|will)\b", re.I
)


def _strip_html(html):
    return _TAG_RE.sub(" ", html or "").replace("&nbsp;", " ")


def _looks_like_question(text):
    text = text.strip()
    return text.endswith("?") or bool(_QUESTION_WORDS.match(text))


def _fact_density(text, thresholds):
    """Statistics + citations + quotes per 100 words (Princeton GEO levers)."""
    words = text.split()
    wc = len(words)
    if wc < 20:
        return 0, "Too little text to assess fact density"

    signals = (
        len(_MEASURE_RE.findall(text))
        + len(_BARE_NUMBER_RE.findall(text))
        + len(_CURRENCY_RE.findall(text))
        + len(_REVIEW_RE.findall(text))
        + len(_CITATION_RE.findall(text))
        + len(_QUOTE_RE.findall(text))
    )
    density = signals / (wc / 100)
    t = thresholds["fact_density_per_100w"]
    if density >= t["good"]:
        score = 100
    elif density >= t["ok"]:
        score = 65
    elif density >= t["min"]:
        score = 35
    else:
        score = 10
    detail = f"{signals} fact/stat/quote signals in {wc} words ({density:.1f} per 100w)"
    return score, detail


def _extractability(text, html, h2s=None):
    """Question headings + answer-first leads + list/table structure."""
    score = 0
    parts = []

    heading_texts = list(h2s or [])
    heading_texts += [_strip_html(h) for h in _HEADING_RE.findall(html or "")]
    questions = sum(1 for h in heading_texts if _looks_like_question(h))
    if questions >= 2:
        score += 40
        parts.append(f"{questions} question-style headings")
    elif questions == 1:
        score += 25
        parts.append("1 question-style heading")

    lists = len(_LI_RE.findall(html or ""))
    tables = len(_TABLE_RE.findall(html or ""))
    if lists >= 3 or tables:
        score += 30
        parts.append(f"{lists} list items, {tables} tables")
    elif lists >= 1:
        score += 15
        parts.append(f"{lists} list items")

    first_sentence = re.split(r"(?<=[.!?])\s", text.strip(), 1)[0] if text.strip() else ""
    lead_words = len(first_sentence.split())
    if 0 < lead_words <= 30:
        score += 30
        parts.append("concise answer-first opening")
    elif lead_words:
        score += 10

    score = min(score, 100)
    detail = ", ".join(parts) if parts else "No extractable Q&A structure detected"
    return score, detail


def _structured_data_score(schema_types, resource_type, thresholds):
    if schema_types is None:
        return 50, "Schema not crawled yet"
    if not schema_types:
        return 0, "No JSON-LD schema detected (as seen by AI crawlers)"
    desired = thresholds["desired_schema"].get(
        resource_type, thresholds["desired_schema"]["product"]
    )
    present = set(schema_types)
    have = [d for d in desired if d in present]
    score = round(len(have) / len(desired) * 100)
    detail = (
        f"{len(have)}/{len(desired)} key types present"
        + (f": {', '.join(have)}" if have else " — none of the key types found")
    )
    return score, detail


def _entity_authority(text, html, schema_types, vendor=None, store_domain=None):
    score = 0
    parts = []
    if vendor and vendor.lower() in text.lower():
        score += 30
        parts.append("brand named in body")
    if schema_types and "Organization" in schema_types:
        score += 30
        parts.append("Organization schema")
    external = {
        d.split(":")[0].lower()
        for d in _EXTERNAL_LINK_RE.findall(html or "")
        if not store_domain or store_domain.lower() not in d.lower()
    }
    if external:
        score += min(40, len(external) * 20)
        parts.append(f"{len(external)} outbound source link(s)")
    score = min(score, 100)
    detail = ", ".join(parts) if parts else "No brand/authority signals in content"
    return score, detail


def _freshness(updated_at, thresholds):
    if not updated_at:
        return 50, "No update timestamp"
    if isinstance(updated_at, str):
        try:
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return 50, "Unparseable update timestamp"
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    days = (now - updated_at).days
    t = thresholds["freshness_days"]
    if days <= t["fresh"]:
        score = 100
    elif days <= t["aging"]:
        score = 60
    else:
        score = 25
    return score, f"Last updated {days} days ago"


def _parse_robots_ai_rules(robots_txt):
    """Return {"bots": {bot: allowed_bool}, "blocks_sensitive": bool}."""
    blocked = set()
    blocks_sensitive = False
    current_agents = []
    for raw in (robots_txt or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            current_agents = [value.lower()]
        elif field == "disallow":
            for agent in current_agents:
                if value == "/":
                    if agent == "*":
                        blocked.update(b.lower() for b in AI_BOTS)
                    else:
                        blocked.add(agent)
                if value.rstrip("/").lower() in ("/cart", "/checkout", "/account", "/admin"):
                    blocks_sensitive = True
    bots = {bot: bot.lower() not in blocked for bot in AI_BOTS}
    return {"bots": bots, "blocks_sensitive": blocks_sensitive}


def analyze_store_crawlability(robots_txt, llms_txt_present):
    """Store-level AI crawlability sub-score (robots.txt + llms.txt)."""
    if robots_txt is None:
        return 50, "robots.txt not fetched"
    rules = _parse_robots_ai_rules(robots_txt)
    allowed = [b for b, ok in rules["bots"].items() if ok]
    score = round(len(allowed) / len(AI_BOTS) * 70)
    parts = [f"{len(allowed)}/{len(AI_BOTS)} AI crawlers allowed"]
    if llms_txt_present:
        score += 15
        parts.append("llms.txt present")
    else:
        parts.append("no llms.txt")
    if rules["blocks_sensitive"]:
        score += 15
        parts.append("cart/checkout blocked")
    return min(score, 100), ", ".join(parts)


def analyze_ai_visibility(
    item,
    live=None,
    schema_markup=None,
    crawlability=None,
    store_domain=None,
):
    """Score a product/collection's AI-answer-engine readiness.

    item: synced_products / synced_collections row (dict).
    live: scrape_page_seo_metadata() dict for the storefront URL, or None.
    schema_markup: list of pushed schema_markup rows (each with 'schema_type').
    crawlability: precomputed (score, detail) from analyze_store_crawlability,
        or None when the store hasn't been crawled.
    Returns {"score", "breakdown"}; degrades gracefully when live is None.
    """
    rules = get_setting("rules.ai_seo", DEFAULTS["rules.ai_seo"])
    weights = rules["weights"]
    thresholds = rules["thresholds"]
    resource_type = item.get("resource_type") or item.get("type") or "product"

    html = item.get("description") or ""
    text = _strip_html(html)
    h2s = (live or {}).get("h2s")

    pushed_types = [r["schema_type"] for r in (schema_markup or []) if r.get("schema_type")]
    if live is not None:
        schema_types = list(dict.fromkeys((live.get("schema_types") or []) + pushed_types))
    elif pushed_types:
        schema_types = pushed_types
    else:
        schema_types = None

    scores = {}
    details = {}

    scores["structured_data"], details["structured_data"] = _structured_data_score(
        schema_types, resource_type, thresholds
    )
    scores["extractability"], details["extractability"] = _extractability(text, html, h2s)
    scores["fact_density"], details["fact_density"] = _fact_density(text, thresholds)
    scores["crawlability"], details["crawlability"] = (
        crawlability if crawlability else (50, "Store crawlability not checked")
    )
    scores["entity_authority"], details["entity_authority"] = _entity_authority(
        text, html, schema_types, item.get("vendor"), store_domain
    )
    scores["freshness"], details["freshness"] = _freshness(
        item.get("last_optimized_at") or item.get("synced_at") or item.get("updated_at"),
        thresholds,
    )

    overall = sum(scores[k] * weights[k] for k in weights if k in scores)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }
