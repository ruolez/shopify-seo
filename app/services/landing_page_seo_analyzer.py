import re

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS


def _strip_non_content(html_text):
    if not html_text:
        return ""
    text = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', ' ', html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<(nav|footer|header)[^>]*>.*?</\1>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    return text


def extract_page_content(raw_html):
    if not raw_html or len(raw_html) < 500:
        return raw_html or ""
    main_match = re.search(r'<main[^>]*>(.*?)</main>', raw_html, re.DOTALL | re.IGNORECASE)
    if main_match:
        content = main_match.group(1).strip()
    else:
        body_match = re.search(r'<body[^>]*>(.*?)</body>', raw_html, re.DOTALL | re.IGNORECASE)
        content = body_match.group(1).strip() if body_match else raw_html
    content = _strip_non_content(content)
    content = re.sub(r'<img\s[^>]*>', '', content, flags=re.IGNORECASE)
    content = re.sub(r'<div\s+id="shopify-section-[^"]*"[^>]*>', '<div>', content, flags=re.IGNORECASE)
    return content


def _count_words(html_text):
    text = _strip_non_content(html_text)
    text = re.sub(r"<[^>]+>", " ", text)
    return len(text.split())


def _count_images(html_text):
    return len(re.findall(r"<img\s", html_text or "", re.IGNORECASE))


def _count_images_with_alt(html_text):
    imgs = re.findall(r"<img\s[^>]*>", html_text or "", re.IGNORECASE)
    with_alt = 0
    for img in imgs:
        if re.search(r'alt\s*=\s*["\'][^"\']+["\']', img, re.IGNORECASE):
            with_alt += 1
    return with_alt, len(imgs)


def _count_internal_links(html_text):
    from app.services.content_preservation import count_internal_links
    return count_internal_links(html_text)


def _has_cta(html_text):
    cta_patterns = [
        r'class\s*=\s*["\'][^"\']*(?:cta|btn|button)[^"\']*["\']',
        r'(?:shop\s+now|buy\s+now|get\s+started|learn\s+more|sign\s+up|order\s+now)',
    ]
    for pattern in cta_patterns:
        if re.search(pattern, html_text or "", re.IGNORECASE):
            return True
    return False


def _has_schema(html_text):
    return bool(re.search(r'application/ld\+json', html_text or "", re.IGNORECASE))


def _check_images_dimensions(html_text):
    imgs = re.findall(r"<img\s[^>]*>", html_text or "", re.IGNORECASE)
    if not imgs:
        return True
    with_dims = 0
    for img in imgs:
        has_w = re.search(r'\bwidth\s*=', img, re.IGNORECASE)
        has_h = re.search(r'\bheight\s*=', img, re.IGNORECASE)
        if has_w and has_h:
            with_dims += 1
    return with_dims >= len(imgs) * 0.5


def _analyze_html(body_html, meta_title, meta_description, schema_json=None):
    rules = get_setting("rules.landing_page_seo", DEFAULTS.get("rules.landing_page_seo", {}))
    weights = rules.get("weights", {
        "meta_title": 0.15,
        "meta_description": 0.15,
        "h1_tag": 0.10,
        "heading_hierarchy": 0.10,
        "content_length": 0.10,
        "image_alt_text": 0.10,
        "internal_links": 0.10,
        "content_structure": 0.05,
        "cta_present": 0.05,
        "mobile_friendly": 0.05,
        "schema_markup": 0.05,
    })
    thresholds = rules.get("thresholds", {})

    scores = {}
    details = {}

    # Meta Title
    mt = thresholds.get("meta_title", {})
    mt_good = mt.get("good", [50, 60])
    mt_ok = mt.get("ok", [35, 70])
    title_len = len(meta_title or "")
    if mt_good[0] <= title_len <= mt_good[1]:
        scores["meta_title"] = 100
        details["meta_title"] = f"Good length ({title_len} chars)"
    elif mt_ok[0] <= title_len <= mt_ok[1]:
        scores["meta_title"] = 60
        details["meta_title"] = f"Acceptable ({title_len} chars), ideal is {mt_good[0]}-{mt_good[1]}"
    elif title_len > 0:
        scores["meta_title"] = 30
        details["meta_title"] = f"Poor length ({title_len} chars), ideal is {mt_good[0]}-{mt_good[1]}"
    else:
        scores["meta_title"] = 0
        details["meta_title"] = "Missing meta title"

    # Meta Description
    md = thresholds.get("meta_description", {})
    md_good = md.get("good", [140, 160])
    md_ok = md.get("ok", [100, 200])
    desc_len = len(meta_description or "")
    if md_good[0] <= desc_len <= md_good[1]:
        scores["meta_description"] = 100
        details["meta_description"] = f"Good length ({desc_len} chars)"
    elif md_ok[0] <= desc_len <= md_ok[1]:
        scores["meta_description"] = 60
        details["meta_description"] = f"Acceptable ({desc_len} chars), ideal is {md_good[0]}-{md_good[1]}"
    elif desc_len > 0:
        scores["meta_description"] = 30
        details["meta_description"] = f"Poor length ({desc_len} chars), ideal is {md_good[0]}-{md_good[1]}"
    else:
        scores["meta_description"] = 0
        details["meta_description"] = "Missing meta description"

    # H1 Tag
    h1_count = len(re.findall(r"<h1[\s>]", body_html or "", re.IGNORECASE))
    if h1_count == 1:
        scores["h1_tag"] = 100
        details["h1_tag"] = "Exactly 1 H1 tag"
    elif h1_count == 0:
        scores["h1_tag"] = 0
        details["h1_tag"] = "No H1 tag found"
    else:
        scores["h1_tag"] = 30
        details["h1_tag"] = f"Multiple H1 tags ({h1_count}) — should have exactly 1"

    # Heading Hierarchy
    h2_count = len(re.findall(r"<h2[\s>]", body_html or "", re.IGNORECASE))
    h3_count = len(re.findall(r"<h3[\s>]", body_html or "", re.IGNORECASE))
    if h2_count >= 2 and h3_count >= 1:
        scores["heading_hierarchy"] = 100
        details["heading_hierarchy"] = f"Good hierarchy ({h2_count} H2, {h3_count} H3)"
    elif h2_count >= 1:
        scores["heading_hierarchy"] = 60
        details["heading_hierarchy"] = f"Has H2 headings ({h2_count}), consider adding H3"
    else:
        scores["heading_hierarchy"] = 0
        details["heading_hierarchy"] = "No heading structure found"

    # Content Length
    word_count = _count_words(body_html)
    cl = thresholds.get("content_length", {})
    cl_good = cl.get("good", 300)
    cl_ok = cl.get("ok", 150)
    if word_count >= cl_good:
        scores["content_length"] = 100
        details["content_length"] = f"Good length ({word_count} words)"
    elif word_count >= cl_ok:
        scores["content_length"] = 60
        details["content_length"] = f"Acceptable ({word_count} words), aim for {cl_good}+"
    else:
        scores["content_length"] = 0 if word_count == 0 else 30
        details["content_length"] = f"Short content ({word_count} words), aim for {cl_good}+"

    # Image Alt Text
    with_alt, total_imgs = _count_images_with_alt(body_html)
    if total_imgs == 0:
        scores["image_alt_text"] = 50
        details["image_alt_text"] = "No images found"
    else:
        ratio = with_alt / total_imgs
        if ratio >= 1.0:
            scores["image_alt_text"] = 100
            details["image_alt_text"] = f"All {total_imgs} images have alt text"
        elif ratio >= 0.5:
            scores["image_alt_text"] = 60
            details["image_alt_text"] = f"{with_alt}/{total_imgs} images have alt text"
        else:
            scores["image_alt_text"] = 20
            details["image_alt_text"] = f"Only {with_alt}/{total_imgs} images have alt text"

    # Internal Links
    il = thresholds.get("internal_links", {})
    il_good = il.get("good", 3)
    il_ok = il.get("ok", 1)
    link_count = _count_internal_links(body_html)
    if link_count >= il_good:
        scores["internal_links"] = 100
        details["internal_links"] = f"Good ({link_count} internal links)"
    elif link_count >= il_ok:
        scores["internal_links"] = 60
        details["internal_links"] = f"{link_count} internal link(s), aim for {il_good}+"
    else:
        scores["internal_links"] = 0
        details["internal_links"] = "No internal links found"

    # Content Structure
    sections = re.findall(r"<(?:section|article|div)[^>]*>", body_html or "", re.IGNORECASE)
    paragraphs = len(re.findall(r"<p[\s>]", body_html or "", re.IGNORECASE))
    if (len(sections) >= 2 or paragraphs >= 3) and h2_count >= 1:
        scores["content_structure"] = 100
        details["content_structure"] = "Well-structured with sections and paragraphs"
    elif paragraphs >= 2:
        scores["content_structure"] = 60
        details["content_structure"] = "Basic structure, consider adding more sections"
    else:
        scores["content_structure"] = 0
        details["content_structure"] = "Poor structure — single block of content"

    # CTA Present
    if _has_cta(body_html):
        scores["cta_present"] = 100
        details["cta_present"] = "CTA element found"
    else:
        scores["cta_present"] = 0
        details["cta_present"] = "No CTA button or link found"

    # Mobile Friendly (images have dimensions)
    if _check_images_dimensions(body_html):
        scores["mobile_friendly"] = 100
        details["mobile_friendly"] = "Images have width/height attributes"
    else:
        scores["mobile_friendly"] = 0
        details["mobile_friendly"] = "Images missing width/height — causes layout shift"

    # Schema Markup
    has_schema = _has_schema(body_html) or bool(schema_json)
    if has_schema:
        scores["schema_markup"] = 100
        details["schema_markup"] = "JSON-LD schema markup present"
    else:
        scores["schema_markup"] = 0
        details["schema_markup"] = "No schema markup found"

    overall = sum(scores.get(k, 0) * weights.get(k, 0) for k in weights)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }


def analyze_synced_page(page_row):
    body = _strip_non_content(page_row.get("body_html") or "")
    meta_title = page_row.get("seo_title") or page_row.get("title") or ""
    meta_desc = page_row.get("seo_description") or ""
    return _analyze_html(body, meta_title, meta_desc)


def analyze_landing_page(page_data):
    body = _strip_non_content(page_data.get("last_generated_html") or page_data.get("body_html") or "")
    meta_title = page_data.get("meta_title") or page_data.get("title") or ""
    meta_desc = page_data.get("meta_description") or ""
    schema = page_data.get("schema_json")
    return _analyze_html(body, meta_title, meta_desc, schema)
