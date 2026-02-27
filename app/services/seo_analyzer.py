import re


def analyze_product_seo(product):
    scores = {}
    details = {}

    title = product.get("meta_title") or product.get("title") or ""
    title_len = len(title)
    if 30 <= title_len <= 60:
        scores["meta_title"] = 100
        details["meta_title"] = f"Good length ({title_len} chars)"
    elif 20 <= title_len < 30 or 60 < title_len <= 70:
        scores["meta_title"] = 60
        details["meta_title"] = f"Acceptable length ({title_len} chars), ideal is 30-60"
    elif title_len > 0:
        scores["meta_title"] = 30
        details["meta_title"] = f"Poor length ({title_len} chars), ideal is 30-60"
    else:
        scores["meta_title"] = 0
        details["meta_title"] = "Missing meta title"

    desc = product.get("meta_description") or ""
    desc_len = len(desc)
    if 120 <= desc_len <= 160:
        scores["meta_description"] = 100
        details["meta_description"] = f"Good length ({desc_len} chars)"
    elif 80 <= desc_len < 120 or 160 < desc_len <= 200:
        scores["meta_description"] = 60
        details["meta_description"] = (
            f"Acceptable length ({desc_len} chars), ideal is 120-160"
        )
    elif desc_len > 0:
        scores["meta_description"] = 30
        details["meta_description"] = (
            f"Poor length ({desc_len} chars), ideal is 120-160"
        )
    else:
        scores["meta_description"] = 0
        details["meta_description"] = "Missing meta description"

    body = product.get("description") or ""
    text_only = re.sub(r"<[^>]+>", "", body)
    word_count = len(text_only.split())
    if word_count >= 300:
        scores["description"] = 100
        details["description"] = f"Good length ({word_count} words)"
    elif word_count >= 150:
        scores["description"] = 70
        details["description"] = (
            f"Acceptable ({word_count} words), aim for 300+"
        )
    elif word_count >= 50:
        scores["description"] = 40
        details["description"] = f"Short ({word_count} words), aim for 300+"
    elif word_count > 0:
        scores["description"] = 20
        details["description"] = f"Very short ({word_count} words)"
    else:
        scores["description"] = 0
        details["description"] = "Missing product description"

    images = product.get("images") or []
    if not images:
        scores["image_alt"] = 0
        details["image_alt"] = "No images found"
    else:
        with_alt = sum(1 for img in images if img.get("alt"))
        total = len(images)
        ratio = with_alt / total if total > 0 else 0
        scores["image_alt"] = int(ratio * 100)
        details["image_alt"] = f"{with_alt}/{total} images have alt text"

    weights = {
        "meta_title": 0.30,
        "meta_description": 0.30,
        "description": 0.25,
        "image_alt": 0.15,
    }
    overall = sum(scores[k] * weights[k] for k in weights)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }


def analyze_collection_seo(collection):
    scores = {}
    details = {}

    title = collection.get("meta_title") or collection.get("title") or ""
    title_len = len(title)
    if 30 <= title_len <= 60:
        scores["meta_title"] = 100
        details["meta_title"] = f"Good length ({title_len} chars)"
    elif 20 <= title_len < 30 or 60 < title_len <= 70:
        scores["meta_title"] = 60
        details["meta_title"] = f"Acceptable length ({title_len} chars), ideal is 30-60"
    elif title_len > 0:
        scores["meta_title"] = 30
        details["meta_title"] = f"Poor length ({title_len} chars), ideal is 30-60"
    else:
        scores["meta_title"] = 0
        details["meta_title"] = "Missing meta title"

    desc = collection.get("meta_description") or ""
    desc_len = len(desc)
    if 120 <= desc_len <= 160:
        scores["meta_description"] = 100
        details["meta_description"] = f"Good length ({desc_len} chars)"
    elif 80 <= desc_len < 120 or 160 < desc_len <= 200:
        scores["meta_description"] = 60
        details["meta_description"] = (
            f"Acceptable length ({desc_len} chars), ideal is 120-160"
        )
    elif desc_len > 0:
        scores["meta_description"] = 30
        details["meta_description"] = (
            f"Poor length ({desc_len} chars), ideal is 120-160"
        )
    else:
        scores["meta_description"] = 0
        details["meta_description"] = "Missing meta description"

    body = collection.get("description") or ""
    text_only = re.sub(r"<[^>]+>", "", body)
    word_count = len(text_only.split())
    if word_count >= 150:
        scores["description"] = 100
        details["description"] = f"Good length ({word_count} words)"
    elif word_count >= 75:
        scores["description"] = 60
        details["description"] = (
            f"Acceptable ({word_count} words), aim for 150+"
        )
    elif word_count > 0:
        scores["description"] = 30
        details["description"] = f"Short ({word_count} words), aim for 150+"
    else:
        scores["description"] = 0
        details["description"] = "Missing collection description"

    weights = {"meta_title": 0.35, "meta_description": 0.35, "description": 0.30}
    overall = sum(scores[k] * weights[k] for k in weights)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }
