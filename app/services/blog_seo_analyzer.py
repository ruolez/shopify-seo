import re

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS
from app.services.content_preservation import count_internal_links
from app.services.seo_rules import score_length

_ALT_RE = re.compile(r"\balt\s*=\s*(?:\"[^\"]+\"|'[^']+')", re.IGNORECASE)


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
    wc_excellent = wc.get("excellent", 600)
    wc_good = wc.get("good", 400)
    wc_short = wc.get("short", 200)
    wc_very_short = wc.get("very_short", 1)

    body = article.get("body") or ""
    text_only = re.sub(r"<[^>]+>", "", body)
    word_count = len(text_only.split())
    if word_count >= wc_excellent:
        scores["word_count"] = 100
        details["word_count"] = f"Enough to cover the topic ({word_count} words)"
    elif word_count >= wc_good:
        scores["word_count"] = 60
        details["word_count"] = f"On the short side ({word_count} words) — check the topic is fully covered"
    elif word_count >= wc_short:
        scores["word_count"] = 30
        details["word_count"] = f"Thin ({word_count} words) — likely missing detail a reader needs"
    elif word_count >= wc_very_short:
        scores["word_count"] = 10
        details["word_count"] = f"Very thin ({word_count} words)"
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

    internal_links = count_internal_links(body)
    if internal_links >= il_good:
        scores["internal_links"] = 100
        details["internal_links"] = f"Good ({internal_links} internal links)"
    elif internal_links >= il_ok:
        scores["internal_links"] = 60
        details["internal_links"] = f"{internal_links} internal link(s), aim for {il_good}+"
    else:
        scores["internal_links"] = 0
        details["internal_links"] = "No internal links found"

    # Images: featured image + alt (60) and inline figures with alt (40).
    im = thresholds.get("image", {})
    inline_good = max(1, int(im.get("inline_good", 2)))
    image_url = article.get("image_url") or ""
    image_alt = article.get("image_alt") or ""
    inline_imgs = re.findall(r"<img\b[^>]*>", body, re.IGNORECASE)
    inline_with_alt = [t for t in inline_imgs if _ALT_RE.search(t)]
    inline_score = min(len(inline_with_alt), inline_good) / inline_good * 40
    if image_url and image_alt:
        featured_score, featured_detail = 60, "featured image with alt"
    elif image_url:
        featured_score, featured_detail = 36, "featured image missing alt"
    else:
        featured_score, featured_detail = 0, "no featured image"
    scores["image"] = round(featured_score + inline_score)
    if inline_imgs:
        inline_detail = f"{len(inline_with_alt)}/{len(inline_imgs)} in-body image(s) with alt (aim for {inline_good}+)"
    else:
        inline_detail = f"no in-body images (aim for {inline_good}+)"
    details["image"] = f"{featured_detail.capitalize()}; {inline_detail}"

    # Structure: comparison table + FAQ block (snippet / AI-citation signals)
    st = thresholds.get("structure", {})
    has_table = bool(re.search(r"<table[\s>]", body, re.IGNORECASE))
    has_faq = bool(re.search(r"<details[\s>]|a2z-blog__faq", body, re.IGNORECASE))
    scores["structure"] = int((st.get("table", 50) if has_table else 0) + (st.get("faq", 50) if has_faq else 0))
    parts = [("table" if has_table else "no table"), ("FAQ" if has_faq else "no FAQ")]
    details["structure"] = ", ".join(parts).capitalize() + (" — add a comparison table and FAQ" if scores["structure"] < 100 else "")

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

    # Product News is rewritten from a source, not written to a length — it
    # is as long as the story is.
    if article.get("article_type") == "news":
        scores.pop("word_count", None)

    weights = rules.get("weights", {})
    scored = [k for k in scores if weights.get(k, 0) > 0]
    total_weight = sum(weights[k] for k in scored)
    overall = sum(scores[k] * weights[k] for k in scored) / total_weight if total_weight else 0

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scored},
    }
