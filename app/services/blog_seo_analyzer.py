import re

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS
from app.services.seo_rules import score_length


def analyze_article_seo(article):
    rules = get_setting("rules.article_seo", DEFAULTS["rules.article_seo"])
    thresholds = rules.get("thresholds", {})
    defaults = DEFAULTS["rules.article_seo"]["thresholds"]

    scores = {}
    details = {}

    # Meta Title — shared scoring ladder; the settings defaults are the
    # single fallback (an inline copy here once drifted to [140,160]).
    title = article.get("meta_title") or article.get("title") or ""
    scores["meta_title"], details["meta_title"] = score_length(
        len(title), thresholds.get("meta_title") or defaults["meta_title"], "meta title"
    )

    # Meta Description
    desc = article.get("meta_description") or ""
    scores["meta_description"], details["meta_description"] = score_length(
        len(desc), thresholds.get("meta_description") or defaults["meta_description"], "meta description"
    )

    # Body Word Count
    wc = thresholds.get("word_count", {})
    wc_excellent = wc.get("excellent", 1500)
    wc_good = wc.get("good", 1000)
    wc_short = wc.get("short", 500)
    wc_very_short = wc.get("very_short", 1)

    body = article.get("body") or ""
    text_only = re.sub(r"<[^>]+>", "", body)
    word_count = len(text_only.split())
    if word_count >= wc_excellent:
        scores["word_count"] = 100
        details["word_count"] = f"Excellent length ({word_count} words)"
    elif word_count >= wc_good:
        scores["word_count"] = 60
        details["word_count"] = f"Good length ({word_count} words), aim for {wc_excellent}+"
    elif word_count >= wc_short:
        scores["word_count"] = 30
        details["word_count"] = f"Short ({word_count} words), aim for {wc_excellent}+"
    elif word_count >= wc_very_short:
        scores["word_count"] = 10
        details["word_count"] = f"Very short ({word_count} words), aim for {wc_excellent}+"
    else:
        scores["word_count"] = 0
        details["word_count"] = "No content"

    # Heading Structure
    hd = thresholds.get("headings", {})
    hd_good_h2 = hd.get("good_h2", 2)
    hd_good_h3 = hd.get("good_h3", 1)

    h2_count = len(re.findall(r"<h2[\s>]", body, re.IGNORECASE))
    h3_count = len(re.findall(r"<h3[\s>]", body, re.IGNORECASE))
    if h2_count >= hd_good_h2 and h3_count >= hd_good_h3:
        scores["headings"] = 100
        details["headings"] = f"Good hierarchy ({h2_count} H2, {h3_count} H3)"
    elif h2_count >= 1:
        scores["headings"] = 60
        details["headings"] = f"Has H2 headings ({h2_count}), add H3 subheadings"
    elif h3_count >= 1:
        scores["headings"] = 30
        details["headings"] = f"Has headings but flat structure, use H2+H3 hierarchy"
    else:
        scores["headings"] = 0
        details["headings"] = "No headings found"

    # Internal Links
    il = thresholds.get("internal_links", {})
    il_good = il.get("good", 3)
    il_ok = il.get("ok", 1)

    internal_links = len(re.findall(r'<a\s[^>]*href\s*=\s*["\']/', body, re.IGNORECASE))
    if internal_links >= il_good:
        scores["internal_links"] = 100
        details["internal_links"] = f"Good ({internal_links} internal links)"
    elif internal_links >= il_ok:
        scores["internal_links"] = 60
        details["internal_links"] = f"{internal_links} internal link(s), aim for {il_good}+"
    else:
        scores["internal_links"] = 0
        details["internal_links"] = "No internal links found"

    # Image + Alt Text
    image_url = article.get("image_url") or ""
    image_alt = article.get("image_alt") or ""
    if image_url and image_alt:
        scores["image"] = 100
        details["image"] = "Featured image with alt text"
    elif image_url:
        scores["image"] = 60
        details["image"] = "Featured image present, missing alt text"
    else:
        scores["image"] = 0
        details["image"] = "No featured image"

    # Summary/Excerpt
    sm = thresholds.get("summary", {})
    sm_good = sm.get("good", [50, 160])

    summary = article.get("summary") or ""
    summary_len = len(summary)
    if sm_good[0] <= summary_len <= sm_good[1]:
        scores["summary"] = 100
        details["summary"] = f"Good length ({summary_len} chars)"
    elif summary_len > 0:
        scores["summary"] = 60
        details["summary"] = f"Present but suboptimal ({summary_len} chars), ideal is {sm_good[0]}-{sm_good[1]}"
    else:
        scores["summary"] = 0
        details["summary"] = "Missing summary/excerpt"

    weights = rules.get("weights", {})
    overall = sum(scores[k] * weights.get(k, 0) for k in scores)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }
