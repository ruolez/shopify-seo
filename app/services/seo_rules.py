"""Shared scoring rules — single source of truth for the checks that both the
content-quality analyzer (seo_analyzer) and the keyword-alignment analyzer
(technical_seo_analyzer) run: meta title/description length, description word
count, and image alt coverage.

Thresholds are passed in by callers; the canonical values live in the
`rules.product_seo` / `rules.collection_seo` settings keys.
"""


def score_length(value_len, thresholds, field_label, unit="chars"):
    good = thresholds["good"]
    ok = thresholds["ok"]
    poor_min = thresholds.get("poor_min", 1)

    if good[0] <= value_len <= good[1]:
        return 100, f"Good length ({value_len} {unit})"
    elif ok[0] <= value_len < good[0] or good[1] < value_len <= ok[1]:
        return 60, f"Acceptable length ({value_len} {unit}), ideal is {good[0]}-{good[1]}"
    elif value_len >= poor_min:
        return 30, f"Poor length ({value_len} {unit}), ideal is {good[0]}-{good[1]}"
    else:
        return 0, f"Missing {field_label}"


def score_description_words(word_count, dw, entity_label, scores=(100, 70, 40, 20)):
    """Score a description by word count against the good/ok/short/very_short
    ladder in `dw`. Bands absent from `dw` are skipped (collections define no
    very_short band, so their ladder collapses to good/ok/short/missing)."""
    if word_count >= dw["good"]:
        return scores[0], f"Enough to be useful ({word_count} words)"
    if word_count >= dw["ok"]:
        return scores[1], (f"Thin ({word_count} words) — add what a shopper needs to choose "
                           f"(about {dw['good']}+ words); length itself is not the goal")
    short = dw.get("short")
    if short is not None and word_count >= short:
        return scores[2], f"Very thin ({word_count} words) — add what a shopper needs to choose"
    very_short = dw.get("very_short")
    if very_short is not None and word_count >= very_short:
        return scores[3], f"Very short ({word_count} words)"
    return 0, f"Missing {entity_label} description"


def score_image_alt(images):
    """Alt-text coverage as a 0-100 ratio. Returns (score, detail, with_alt, total)."""
    if not images:
        return 0, "No images found", 0, 0
    with_alt = sum(1 for img in images if img.get("alt"))
    total = len(images)
    ratio = with_alt / total if total > 0 else 0
    return int(ratio * 100), f"{with_alt}/{total} images have alt text", with_alt, total
