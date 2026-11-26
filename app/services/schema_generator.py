import json
import re

# Fact labels already represented by dedicated schema.org fields — never duplicate
# them as additionalProperty entries.
_SPEC_EXCLUDE = {"brand", "price", "availability", "sku", "barcode / gtin", "type", "gtin", "upc"}
# Facts that describe the *varying* dimension of a variant group — the theme
# snippet emits per-variant option PropertyValues natively, so shared enrichment
# must not repeat them.
_VARYING_EXCLUDE = {"options / flavors", "pack / size"}

ENRICHMENT_VERSION = 2

# Keys that must never appear in enrichment: commerce/rating data is rendered
# fresh by the theme snippet from live Liquid objects. Anything here in a
# pushed metafield would be a stale or fabricated duplicate.
_FORBIDDEN_ENRICHMENT_KEYS = {
    "offers",
    "price",
    "pricecurrency",
    "pricevaliduntil",
    "availability",
    "aggregaterating",
    "review",
    "reviews",
    "ratingvalue",
    "reviewcount",
}


def _additional_properties(spec_properties, extra_exclude=frozenset()):
    """Map a {label: value} facts dict to schema.org PropertyValue entries, skipping
    labels already represented by dedicated schema fields. Grounded — caller supplies
    only verified store facts, so nothing is fabricated here."""
    props = []
    for label, value in (spec_properties or {}).items():
        key = (label or "").strip().lower()
        val = str(value).strip() if value is not None else ""
        if not val or key in _SPEC_EXCLUDE or key in extra_exclude:
            continue
        props.append({"@type": "PropertyValue", "name": label, "value": val})
    return props


def generate_product_enrichment(product, spec_properties=None):
    """Slim enrichment payload for the `seo.schema_enrichment` metafield.

    The theme snippet (ai-seo-schema.liquid v2) builds the full Product /
    ProductGroup JSON-LD natively in Liquid — title, images, brand, variants,
    offers, availability and ratings all come from live storefront objects, so
    they can never go stale. This payload carries only what Liquid cannot know:
    AI-verified spec facts, plus optional description/category overrides.
    """
    enrichment = {"schema_version": ENRICHMENT_VERSION}

    props = _additional_properties(spec_properties, extra_exclude=_VARYING_EXCLUDE)
    if props:
        enrichment["additionalProperty"] = props

    return enrichment


def generate_collection_schema(collection, store_domain, org_settings=None):
    """CollectionPage record kept in `schema_markup` for scoring credit.

    The storefront CollectionPage JSON-LD is rendered natively by the theme
    snippet — this record mirrors it for the app's scoring/coverage views and
    is never pushed as a metafield."""
    url = f"https://{store_domain}/collections/{collection.get('handle', '')}"
    schema = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "@id": f"{url}#collection",
        "name": collection.get("meta_title") or collection.get("title", ""),
        "description": _strip_html(collection.get("meta_description") or collection.get("description", "")),
        "url": url,
        "mainEntityOfPage": url,
        "isPartOf": {"@id": f"https://{store_domain}/#website"},
    }
    if collection.get("image_url"):
        schema["image"] = collection["image_url"]
    return schema


def validate_enrichment(enrichment):
    errors = []

    if not isinstance(enrichment, dict):
        return False, ["Enrichment must be a JSON object"]

    if enrichment.get("schema_version") != ENRICHMENT_VERSION:
        errors.append(f"schema_version must be {ENRICHMENT_VERSION}")

    # Fabrication guard: commerce and rating data must never be baked into the
    # metafield — the snippet renders those from live Liquid objects.
    for key in enrichment:
        if key.strip().lower() in _FORBIDDEN_ENRICHMENT_KEYS:
            errors.append(f"Forbidden key '{key}' — offers/availability/ratings render in-theme, never in the metafield")

    description = enrichment.get("description")
    if description is not None:
        if not isinstance(description, str):
            errors.append("description must be a string")
        elif len(description) > 5000:
            errors.append("description exceeds 5000 characters")

    category = enrichment.get("category")
    if category is not None and not isinstance(category, str):
        errors.append("category must be a string")

    props = enrichment.get("additionalProperty")
    if props is not None:
        if not isinstance(props, list):
            errors.append("additionalProperty must be a list")
        else:
            for i, p in enumerate(props):
                if not isinstance(p, dict) or not str(p.get("name") or "").strip() or not str(p.get("value") or "").strip():
                    errors.append(f"additionalProperty[{i}] needs non-empty name and value")
                    break

    return len(errors) == 0, errors


def schema_to_script_tag(schema):
    json_str = json.dumps(schema, indent=2, ensure_ascii=False)
    return f'<script type="application/ld+json">\n{json_str}\n</script>'


def _strip_html(html):
    if not html:
        return ""
    clean = re.sub(r"<[^>]+>", "", html)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:5000]


# Google shows no FAQ rich results, so FAQPage markup earns nothing there.
# Visible FAQ sections stay; only the markup is gated (setting
# schema.emit_faq, off by default) so it can come back with one switch.
_FAQ_MICRODATA = re.compile(
    r'\s+itemscope(?:\s+itemprop="(?:mainEntity|acceptedAnswer)")?\s+itemtype="https://schema\.org/(?:FAQPage|Question|Answer)"'
    r'|\s+itemprop="(?:mainEntity|acceptedAnswer|name|text)"'
)


def faq_markup_enabled():
    from app.settings_manager import get_setting

    return bool(get_setting("schema.emit_faq", False))


def strip_faq_microdata(html):
    """Remove FAQPage/Question/Answer microdata attributes, keeping the text.
    Only for markup that is FAQ-only (it also drops itemprop name/text)."""
    return _FAQ_MICRODATA.sub("", html or "")
