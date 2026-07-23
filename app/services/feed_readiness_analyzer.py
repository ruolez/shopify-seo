"""Feed readiness (agentic-commerce catalog quality) scoring.

Shopify catalogs are auto-integrated into ChatGPT shopping (Agentic
Storefronts): product cards there are ranked on FEED data — price/availability
accuracy, GTINs, images, structured attributes, review stats — not page HTML.
This analyzer scores each synced product's feed quality deterministically from
local data (no crawl, no LLM), mirroring ai_seo_analyzer's pure-function style.

Returns {"score": 0-100, "breakdown": {component: {"score", "detail"}},
"fixables": [str]} — fixables are concrete per-product actions.
"""

import re

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS

_TAG_RE = re.compile(r"<[^>]+>")
_PROMO_RE = re.compile(r"(!{2,}|\bSALE\b|\bFREE SHIPPING\b|\bBUY NOW\b)")


def _strip_html(html):
    return _TAG_RE.sub(" ", html or "").replace("&nbsp;", " ")


def _gtin(variants, thresholds, fixables):
    lengths = set(thresholds.get("gtin_lengths") or [8, 12, 13, 14])
    total = len(variants) or 1
    missing = []
    have = 0
    for v in variants:
        digits = re.sub(r"\D", "", str(v.get("barcode") or ""))
        if len(digits) in lengths:
            have += 1
        else:
            missing.append(v.get("title") or "variant")
    score = round(have / total * 100)
    if missing:
        names = ", ".join(missing[:4]) + ("…" if len(missing) > 4 else "")
        fixables.append(f"Add a GTIN/UPC barcode to: {names}")
    detail = f"{have}/{total} variants have a GTIN/UPC (no length-{'/'.join(map(str, sorted(lengths)))} barcode = invisible to product matching)"
    return score, detail


def _sku(variants, fixables):
    total = len(variants) or 1
    have = sum(1 for v in variants if (v.get("sku") or "").strip())
    if have < total:
        fixables.append(f"Add SKUs to {total - have} variant(s)")
    return round(have / total * 100), f"{have}/{total} variants have a SKU"


def _images(images, fixables):
    if not images:
        fixables.append("Add at least one product image")
        return 0, "No images — feed listings without images are skipped or buried"
    with_alt = sum(1 for i in images if (i.get("alt") or "").strip())
    if with_alt == len(images):
        return 100, f"{len(images)} image(s), all with alt text"
    fixables.append(f"Add alt text to {len(images) - with_alt} image(s)")
    return 60, f"{len(images)} image(s), {with_alt} with alt text"


def _product_type(product, fixables):
    if (product.get("product_type") or "").strip():
        return 100, f"Category set: {product['product_type']}"
    fixables.append("Set a product type / Shopify taxonomy category")
    return 0, "No product type — category is a core feed attribute"


def _title(title, thresholds, fixables):
    t = (title or "").strip()
    lo = thresholds["title_len"]["min"]
    hi = thresholds["title_len"]["max"]
    if not t:
        fixables.append("Add a product title")
        return 0, "Missing title"
    problems = []
    if len(t) < lo:
        problems.append(f"short ({len(t)} chars)")
    if len(t) > hi:
        problems.append(f"long ({len(t)} chars)")
    if len(t) > 10 and t.isupper():
        problems.append("ALL CAPS")
    if _PROMO_RE.search(t):
        problems.append("promo phrasing")
    if not problems:
        return 100, f"Clean title ({len(t)} chars)"
    fixables.append(f"Fix title: {', '.join(problems)}")
    return 60 if len(problems) == 1 else 20, f"Title issues: {', '.join(problems)}"


def _description(html, thresholds, fixables):
    words = len(_strip_html(html).split())
    need = thresholds["description_words"]["min"]
    if words >= need:
        return 100, f"Description present ({words} words)"
    if words:
        fixables.append(f"Expand the description (only {words} words)")
        return 50, f"Thin description ({words} words)"
    fixables.append("Add a product description")
    return 0, "No description"


def _price(variants, thresholds, fixables):
    if not variants:
        return 0, "No variants/prices"
    bad = 0
    compare_warn = False
    for v in variants:
        try:
            p = float(v.get("price") or 0)
        except (TypeError, ValueError):
            p = 0
        if p < thresholds.get("price_min", 0.01):
            bad += 1
        try:
            cap = float(v.get("compare_at_price") or 0)
            if cap and p and cap <= p:
                compare_warn = True
        except (TypeError, ValueError):
            pass
    if bad:
        fixables.append(f"Set a valid price on {bad} variant(s)")
        return 0, f"{bad} variant(s) with missing/zero price"
    detail = "All variants priced"
    if compare_warn:
        detail += " (compare-at ≤ price on some variants — shows as no discount)"
    return 100, detail


def _availability(variants, status, fixables):
    active = (status or "").lower() == "active"
    any_avail = any(v.get("available") for v in variants)
    if active and any_avail:
        return 100, "Active with in-stock variants"
    if active:
        fixables.append("All variants out of stock — feeds deprioritize unavailable items")
        return 30, "Active but no variant available"
    fixables.append(f"Product status is '{status or 'unknown'}' — only active products enter feeds")
    return 0, f"Status: {status or 'unknown'}"


def _brand(vendor, fixables):
    if (vendor or "").strip():
        return 100, f"Brand/vendor set: {vendor}"
    fixables.append("Set the vendor (brand) field")
    return 0, "No vendor/brand — brand is a primary feed attribute"


def _schema_has_rating(schema):
    if not isinstance(schema, dict):
        return False
    if schema.get("aggregateRating"):
        return True
    return any(v.get("aggregateRating") for v in (schema.get("hasVariant") or []) if isinstance(v, dict))


def _review_signals(pushed_schema, live_schema_types):
    if _schema_has_rating(pushed_schema):
        return 100, "aggregateRating present in pushed Product schema"
    types = set(live_schema_types or [])
    if types & {"Review", "AggregateRating"}:
        return 100, "Review schema detected on the live page"
    return 0, "No review/rating data — AI shopping ranks reviewed products higher (install a review app)"


# Feed fields the sync doesn't capture yet — surfaced as informational only.
UNSYNCED_SIGNALS = [
    {"key": "weight", "label": "Shipping weight", "why": "Used for shipping-cost accuracy in agentic checkout", "status": "not synced"},
    {"key": "image_dimensions", "label": "Image resolution", "why": "Higher-resolution product images rank better in ChatGPT shopping cards", "status": "not synced"},
    {"key": "inventory_counts", "label": "Inventory quantity", "why": "Availability accuracy is a ranking factor", "status": "partial (in-stock flag only)"},
]

# Shopify admin settings the app can't read — manual eligibility checklist.
ELIGIBILITY_CHECKLIST = [
    {"key": "chatgpt_channel", "label": "ChatGPT / Agentic Storefront sales channel enabled", "detail": "Shopify auto-enrolls eligible stores — confirm the channel is on in Settings → Apps and sales channels.", "url": "https://admin.shopify.com/settings/apps"},
    {"key": "markets", "label": "Primary market pricing & currency active", "detail": "Feed pricing follows your primary market — confirm it's correct in Settings → Markets.", "url": "https://admin.shopify.com/settings/markets"},
    {"key": "returns", "label": "Return & refund policy published", "detail": "Return terms are a feed trust field. Settings → Policies.", "url": "https://admin.shopify.com/settings/legal"},
    {"key": "shipping", "label": "Shipping rates & policy published", "detail": "Shipping accuracy feeds agentic checkout. Settings → Shipping and delivery.", "url": "https://admin.shopify.com/settings/shipping"},
    {"key": "checkout", "label": "Accelerated checkout (Shop Pay) enabled", "detail": "Streamlined checkout improves agentic-purchase eligibility. Settings → Payments.", "url": "https://admin.shopify.com/settings/payments"},
]


def analyze_feed_readiness(product, pushed_schema=None, live_schema_types=None):
    """Score one synced_products row's feed quality. Pure — no DB access."""
    rules = get_setting("rules.feed_readiness", DEFAULTS["rules.feed_readiness"])
    weights = rules["weights"]
    thresholds = rules["thresholds"]

    variants = [v for v in (product.get("variants") or []) if isinstance(v, dict)]
    images = [i for i in (product.get("images") or []) if isinstance(i, dict)]

    fixables = []
    scores = {}
    details = {}
    scores["gtin"], details["gtin"] = _gtin(variants, thresholds, fixables)
    scores["sku"], details["sku"] = _sku(variants, fixables)
    scores["images"], details["images"] = _images(images, fixables)
    scores["product_type"], details["product_type"] = _product_type(product, fixables)
    scores["title"], details["title"] = _title(product.get("title"), thresholds, fixables)
    scores["description"], details["description"] = _description(product.get("description"), thresholds, fixables)
    scores["price"], details["price"] = _price(variants, thresholds, fixables)
    scores["availability"], details["availability"] = _availability(variants, product.get("status"), fixables)
    scores["brand"], details["brand"] = _brand(product.get("vendor"), fixables)
    scores["review_signals"], details["review_signals"] = _review_signals(pushed_schema, live_schema_types)

    overall = sum(scores[k] * weights.get(k, 0) for k in scores)
    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
        "fixables": fixables,
    }
