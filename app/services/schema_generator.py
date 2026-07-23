import json
import re
from datetime import datetime, timedelta, timezone


def _price_valid_until():
    return (datetime.now(timezone.utc) + timedelta(days=365)).date().isoformat()


def _gtin_field(barcode):
    """Map a barcode to the correct GTIN property by length (UPC-12, EAN-13, etc.)."""
    digits = re.sub(r"\D", "", barcode or "")
    return {8: "gtin8", 12: "gtin12", 13: "gtin13", 14: "gtin14"}.get(len(digits), "gtin")


# Fact labels already represented by dedicated schema.org fields — never duplicate
# them as additionalProperty entries.
_SPEC_EXCLUDE = {"brand", "price", "availability", "sku", "barcode / gtin", "type", "gtin", "upc"}
# Facts that describe the *varying* dimension of a variant group — kept per-variant
# (on hasVariant), not on the shared ProductGroup.
_VARYING_EXCLUDE = {"options / flavors", "pack / size"}


def _parse_variants(variants):
    if isinstance(variants, str):
        try:
            variants = json.loads(variants)
        except (json.JSONDecodeError, ValueError):
            variants = []
    return variants or []


def _variant_num(variant_id):
    vid = variant_id or ""
    return vid.split("/")[-1] if "gid://shopify/ProductVariant/" in vid else vid


def _build_offer(variant, product, store_domain, currency):
    handle = product.get("handle", "")
    num = _variant_num(variant.get("id", ""))
    url = f"https://{store_domain}/products/{handle}"
    offer = {
        "@type": "Offer",
        "url": f"{url}?variant={num}" if num else url,
        "priceCurrency": currency,
        "price": str(variant.get("price", "0")),
        "availability": "https://schema.org/InStock"
        if variant.get("available", True)
        else "https://schema.org/OutOfStock",
        "itemCondition": "https://schema.org/NewCondition",
        "priceValidUntil": _price_valid_until(),
    }
    if variant.get("sku"):
        offer["sku"] = variant["sku"]
    if variant.get("barcode"):
        offer[_gtin_field(variant["barcode"])] = variant["barcode"]
    return offer


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


def _variant_titles(variants):
    return [v.get("title") for v in variants if v.get("title") and v.get("title") != "Default Title"]


def _infer_variant_dimension(titles):
    """Best-effort label for what a variant set varies by (Strength / Size / Variant)."""
    joined = " ".join((t or "").lower() for t in titles)
    if re.search(r"\b\d+\s?mg\b", joined):
        return "Strength"
    if re.search(r"\b[\d.]+\s?(oz|lb|lbs|g|kg|ml|l|ct|count|pk|pack)\b", joined):
        return "Size"
    return "Variant"


def generate_product_schema(product, store_domain, org_settings=None, spec_properties=None):
    """Product or ProductGroup JSON-LD.

    Multi-variant products (>=2 distinct variant titles) become a schema.org
    ProductGroup with per-variant hasVariant entries and a variesBy dimension —
    the AI-friendly representation for variant merchandising. Single-variant
    products stay a plain Product. `spec_properties` (a verified {label: value}
    facts dict) is emitted as additionalProperty so attributes like flavor,
    nicotine strength or material become machine-readable.
    """
    variants = _parse_variants(product.get("variants", []))
    titles = _variant_titles(variants)
    is_group = len(titles) >= 2

    handle = product.get("handle", "")
    product_url = f"https://{store_domain}/products/{handle}"

    schema = {
        "@context": "https://schema.org",
        "@type": "ProductGroup" if is_group else "Product",
        "name": product.get("title", ""),
        "description": _strip_html(product.get("description", "")),
        "url": product_url,
        "mainEntityOfPage": product_url,
    }

    images = product.get("images", [])
    if isinstance(images, str):
        images = json.loads(images)
    image_urls = [img.get("url", "") for img in images if img.get("url")] if images else []
    if image_urls:
        schema["image"] = image_urls

    if product.get("vendor"):
        schema["brand"] = {"@type": "Brand", "name": product["vendor"]}
    elif org_settings and org_settings.get("organization_name"):
        schema["brand"] = {"@type": "Brand", "name": org_settings["organization_name"]}

    if product.get("product_type"):
        schema["category"] = product["product_type"]

    currency = "USD"
    if org_settings and org_settings.get("default_currency"):
        currency = org_settings["default_currency"]

    if is_group:
        dimension = _infer_variant_dimension(titles)
        schema["@id"] = f"{product_url}#productgroup"
        schema["productGroupID"] = str(product.get("shopify_id") or handle)
        schema["variesBy"] = [dimension]
        shared = _additional_properties(spec_properties, extra_exclude=_VARYING_EXCLUDE)
        if shared:
            schema["additionalProperty"] = shared

        has_variant = []
        for v in variants:
            vt = v.get("title")
            if not vt or vt == "Default Title":
                continue
            vp = {
                "@type": "Product",
                "name": f"{product.get('title', '')} – {vt}".strip(" –"),
                "offers": _build_offer(v, product, store_domain, currency),
                "additionalProperty": [{"@type": "PropertyValue", "name": dimension, "value": vt}],
            }
            if v.get("sku"):
                vp["sku"] = v["sku"]
                vp["mpn"] = v["sku"]
            if v.get("barcode"):
                vp[_gtin_field(v["barcode"])] = v["barcode"]
            if image_urls:
                vp["image"] = image_urls[0]
            has_variant.append(vp)
        schema["hasVariant"] = has_variant
    else:
        schema["@id"] = f"{product_url}#product"
        props = _additional_properties(spec_properties)
        if props:
            schema["additionalProperty"] = props

        if len(variants) > 1:
            offers = [_build_offer(v, product, store_domain, currency) for v in variants]
            prices = []
            for o in offers:
                try:
                    prices.append(float(o["price"]))
                except (ValueError, TypeError):
                    prices.append(0)
            schema["offers"] = {
                "@type": "AggregateOffer",
                "lowPrice": str(min(prices)) if prices else "0",
                "highPrice": str(max(prices)) if prices else "0",
                "priceCurrency": currency,
                "offerCount": len(offers),
                "offers": offers,
            }
        elif variants:
            schema["offers"] = _build_offer(variants[0], product, store_domain, currency)
        else:
            schema["offers"] = {
                "@type": "Offer",
                "url": product_url,
                "priceCurrency": currency,
                "price": "0",
                "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/NewCondition",
                "priceValidUntil": _price_valid_until(),
            }

        first_variant = variants[0] if variants else {}
        if first_variant.get("sku"):
            schema["sku"] = first_variant["sku"]
            schema["mpn"] = first_variant["sku"]
        if first_variant.get("barcode"):
            schema[_gtin_field(first_variant["barcode"])] = first_variant["barcode"]

    # Real ratings only — never fabricate. Populated when review data is available.
    rating_value = product.get("rating_value") or product.get("rating")
    rating_count = product.get("rating_count") or product.get("reviews_count")
    try:
        if rating_value and rating_count and float(rating_count) > 0:
            schema["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": str(rating_value),
                "reviewCount": str(int(float(rating_count))),
                "bestRating": "5",
                "worstRating": "1",
            }
    except (ValueError, TypeError):
        pass

    return schema


def generate_collection_schema(collection, store_domain, org_settings=None):
    """CollectionPage schema for a collection/category page (AEO structured data)."""
    url = f"https://{store_domain}/collections/{collection.get('handle', '')}"
    schema = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "@id": f"{url}#collection",
        "name": collection.get("meta_title") or collection.get("title", ""),
        "description": _strip_html(collection.get("meta_description") or collection.get("description", "")),
        "url": url,
        "mainEntityOfPage": url,
    }
    if collection.get("image_url"):
        schema["image"] = collection["image_url"]
    if org_settings and org_settings.get("organization_name"):
        schema["isPartOf"] = {
            "@type": "WebSite",
            "name": org_settings["organization_name"],
            "url": f"https://{store_domain}",
        }
    return schema


def validate_schema(schema):
    errors = []

    if "@context" not in schema:
        errors.append("Missing @context")
    elif schema["@context"] != "https://schema.org":
        errors.append("@context should be 'https://schema.org'")

    if "@type" not in schema:
        errors.append("Missing @type")

    schema_type = schema.get("@type", "")

    # Google Merchant listing requires an image on every offer-bearing entity;
    # pushing image-less Product/ProductGroup schema produces one crawl error
    # per variant (learned the hard way — see Semrush "image field is required").
    if schema_type in ("Product", "ProductGroup") and not schema.get("image"):
        errors.append(f"{schema_type}: Missing image (required for merchant listing rich results)")

    if schema_type == "Product":
        if not schema.get("name"):
            errors.append("Product: Missing name")
        if not schema.get("offers"):
            errors.append("Product: Missing offers")
        else:
            offers = schema["offers"]
            if isinstance(offers, dict):
                if offers.get("@type") == "Offer" and not offers.get("price"):
                    errors.append("Product: Offer missing price")
                elif offers.get("@type") == "AggregateOffer":
                    if not offers.get("lowPrice"):
                        errors.append("Product: AggregateOffer missing lowPrice")
    elif schema_type == "ProductGroup":
        if not schema.get("name"):
            errors.append("ProductGroup: Missing name")
        variants = schema.get("hasVariant")
        if not variants:
            errors.append("ProductGroup: Missing hasVariant")
        else:
            for i, v in enumerate(variants):
                offer = v.get("offers")
                if not offer or not (isinstance(offer, dict) and offer.get("price")):
                    errors.append(f"ProductGroup: variant {i} missing offer price")
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
