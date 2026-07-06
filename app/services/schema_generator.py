import json
import re
from datetime import datetime, timedelta, timezone


def _price_valid_until():
    return (datetime.now(timezone.utc) + timedelta(days=365)).date().isoformat()


def _gtin_field(barcode):
    """Map a barcode to the correct GTIN property by length (UPC-12, EAN-13, etc.)."""
    digits = re.sub(r"\D", "", barcode or "")
    return {8: "gtin8", 12: "gtin12", 13: "gtin13", 14: "gtin14"}.get(len(digits), "gtin")


def generate_product_schema(product, store_domain, org_settings=None):
    schema = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.get("title", ""),
        "description": _strip_html(product.get("description", "")),
        "url": f"https://{store_domain}/products/{product.get('handle', '')}",
    }

    images = product.get("images", [])
    if isinstance(images, str):
        images = json.loads(images)
    if images:
        schema["image"] = [img.get("url", "") for img in images if img.get("url")]

    if product.get("vendor"):
        schema["brand"] = {"@type": "Brand", "name": product["vendor"]}
    elif org_settings and org_settings.get("organization_name"):
        schema["brand"] = {"@type": "Brand", "name": org_settings["organization_name"]}

    if product.get("product_type"):
        schema["category"] = product["product_type"]

    currency = "USD"
    if org_settings and org_settings.get("default_currency"):
        currency = org_settings["default_currency"]

    variants = product.get("variants", [])
    if isinstance(variants, str):
        variants = json.loads(variants)

    if variants:
        offers = []
        for variant in variants:
            variant_id = variant.get("id", "")
            if "gid://shopify/ProductVariant/" in variant_id:
                variant_num = variant_id.split("/")[-1]
            else:
                variant_num = variant_id

            offer = {
                "@type": "Offer",
                "url": f"https://{store_domain}/products/{product.get('handle', '')}?variant={variant_num}",
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

            offers.append(offer)

        if len(offers) == 1:
            schema["offers"] = offers[0]
        else:
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
    else:
        schema["offers"] = {
            "@type": "Offer",
            "url": f"https://{store_domain}/products/{product.get('handle', '')}",
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

    # Entity anchors help AI engines resolve this as a distinct product entity.
    product_url = f"https://{store_domain}/products/{product.get('handle', '')}"
    schema["@id"] = f"{product_url}#product"
    schema["mainEntityOfPage"] = product_url

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
