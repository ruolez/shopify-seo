import math
import re

from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS


DEFAULT_CTR_BENCHMARKS = {1: 0.10, 2: 0.07, 3: 0.05, 5: 0.035, 10: 0.02, 20: 0.01}

DEFAULT_COMPOSITE_SCORING = {
    "weights_with_search": {"on_page": 0.40, "search": 0.60},
    "weights_gsc_only": {"on_page": 0.50, "search": 0.50},
    "search_weights": {
        "search_visibility": 0.30,
        "click_performance": 0.30,
        "keyword_coverage": 0.20,
        "ranking_strength": 0.20,
    },
}


def _score_length(value_len, thresholds, field_label, unit="chars"):
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


def analyze_product_seo(product):
    rules = get_setting("rules.product_seo", DEFAULTS["rules.product_seo"])
    thresholds = rules["thresholds"]
    weights = rules["weights"]

    scores = {}
    details = {}

    title = product.get("meta_title") or product.get("title") or ""
    scores["meta_title"], details["meta_title"] = _score_length(
        len(title), thresholds["meta_title"], "meta title"
    )

    desc = product.get("meta_description") or ""
    scores["meta_description"], details["meta_description"] = _score_length(
        len(desc), thresholds["meta_description"], "meta description"
    )

    body = product.get("description") or ""
    text_only = re.sub(r"<[^>]+>", "", body)
    word_count = len(text_only.split())
    dw = thresholds["description_words"]
    if word_count >= dw["good"]:
        scores["description"] = 100
        details["description"] = f"Good length ({word_count} words)"
    elif word_count >= dw["ok"]:
        scores["description"] = 70
        details["description"] = (
            f"Acceptable ({word_count} words), aim for {dw['good']}+"
        )
    elif word_count >= dw["short"]:
        scores["description"] = 40
        details["description"] = f"Short ({word_count} words), aim for {dw['good']}+"
    elif word_count >= dw["very_short"]:
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

    overall = sum(scores[k] * weights[k] for k in weights if k in scores)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }


def analyze_collection_seo(collection):
    rules = get_setting("rules.collection_seo", DEFAULTS["rules.collection_seo"])
    thresholds = rules["thresholds"]
    weights = rules["weights"]

    scores = {}
    details = {}

    title = collection.get("meta_title") or collection.get("title") or ""
    scores["meta_title"], details["meta_title"] = _score_length(
        len(title), thresholds["meta_title"], "meta title"
    )

    desc = collection.get("meta_description") or ""
    scores["meta_description"], details["meta_description"] = _score_length(
        len(desc), thresholds["meta_description"], "meta description"
    )

    body = collection.get("description") or ""
    text_only = re.sub(r"<[^>]+>", "", body)
    word_count = len(text_only.split())
    dw = thresholds["description_words"]
    if word_count >= dw["good"]:
        scores["description"] = 100
        details["description"] = f"Good length ({word_count} words)"
    elif word_count >= dw["ok"]:
        scores["description"] = 60
        details["description"] = (
            f"Acceptable ({word_count} words), aim for {dw['good']}+"
        )
    elif word_count >= dw["short"]:
        scores["description"] = 30
        details["description"] = f"Short ({word_count} words), aim for {dw['good']}+"
    else:
        scores["description"] = 0
        details["description"] = "Missing collection description"

    overall = sum(scores[k] * weights[k] for k in weights if k in scores)

    return {
        "score": round(overall),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
    }


def _expected_ctr(position, benchmarks=None):
    if position is None or position <= 0:
        return 0.01
    benchmarks = benchmarks or DEFAULT_CTR_BENCHMARKS
    for threshold in sorted(benchmarks.keys()):
        if position <= threshold:
            return benchmarks[threshold]
    return 0.005


def analyze_search_performance(gsc_data, semrush_data, store_baselines=None):
    """Compute search performance score from GSC + Semrush data.

    Args:
        gsc_data: dict with gsc_clicks, gsc_impressions, gsc_ctr, gsc_position (or None)
        semrush_data: dict with semrush_volume, semrush_position, semrush_keyword_count (or None)
        store_baselines: dict with impression percentiles for normalization (or None)

    Returns:
        dict with search_score and breakdown, or None if no data available.
    """
    gsc_data = gsc_data or {}
    semrush_data = semrush_data or {}

    has_gsc = gsc_data.get("gsc_impressions") is not None and float(gsc_data.get("gsc_impressions") or 0) > 0
    has_semrush = semrush_data.get("semrush_keyword_count") is not None and int(semrush_data.get("semrush_keyword_count") or 0) > 0

    if not has_gsc and not has_semrush:
        return None

    config = get_setting("rules.composite_scoring", DEFAULT_COMPOSITE_SCORING)
    search_weights = config.get("search_weights", DEFAULT_COMPOSITE_SCORING["search_weights"])

    scores = {}
    details = {}
    data_sources = []

    if has_gsc:
        data_sources.append("gsc")
        impressions = float(gsc_data.get("gsc_impressions") or 0)
        ctr = float(gsc_data.get("gsc_ctr") or 0)
        position = float(gsc_data.get("gsc_position") or 0)

        p90 = 1000
        if store_baselines and store_baselines.get("p90"):
            p90 = max(store_baselines["p90"], 10)

        if impressions <= 0:
            scores["search_visibility"] = 0
            details["search_visibility"] = "No impressions"
        else:
            log_score = math.log1p(impressions) / math.log1p(p90)
            scores["search_visibility"] = min(round(log_score * 100), 100)
            details["search_visibility"] = f"{impressions:,.0f} impressions"

        if position > 0:
            expected = _expected_ctr(position)
            if ctr >= expected:
                scores["click_performance"] = 100
                details["click_performance"] = f"CTR {ctr:.1%} exceeds expected {expected:.1%} for position {position:.1f}"
            elif ctr >= expected * 0.6:
                scores["click_performance"] = 70
                details["click_performance"] = f"CTR {ctr:.1%} near expected {expected:.1%} for position {position:.1f}"
            elif ctr >= expected * 0.3:
                scores["click_performance"] = 40
                details["click_performance"] = f"CTR {ctr:.1%} below expected {expected:.1%} for position {position:.1f}"
            else:
                scores["click_performance"] = 15
                details["click_performance"] = f"CTR {ctr:.1%} well below expected {expected:.1%} for position {position:.1f}"
        else:
            scores["click_performance"] = 0
            details["click_performance"] = "No position data"
    else:
        scores["search_visibility"] = 0
        details["search_visibility"] = "No GSC data"
        scores["click_performance"] = 0
        details["click_performance"] = "No GSC data"

    if has_semrush:
        data_sources.append("semrush")
        kw_count = int(semrush_data.get("semrush_keyword_count") or 0)
        best_pos = semrush_data.get("semrush_position")

        if kw_count == 0:
            scores["keyword_coverage"] = 0
            details["keyword_coverage"] = "No ranking keywords"
        elif kw_count <= 3:
            scores["keyword_coverage"] = 40
            details["keyword_coverage"] = f"{kw_count} ranking keywords"
        elif kw_count <= 10:
            scores["keyword_coverage"] = 70
            details["keyword_coverage"] = f"{kw_count} ranking keywords"
        else:
            scores["keyword_coverage"] = 100
            details["keyword_coverage"] = f"{kw_count} ranking keywords"

        if best_pos is not None:
            best_pos = float(best_pos)
            if best_pos <= 3:
                scores["ranking_strength"] = 100
                details["ranking_strength"] = f"Best position: {best_pos:.0f}"
            elif best_pos <= 10:
                scores["ranking_strength"] = 75
                details["ranking_strength"] = f"Best position: {best_pos:.0f}"
            elif best_pos <= 20:
                scores["ranking_strength"] = 50
                details["ranking_strength"] = f"Best position: {best_pos:.0f}"
            elif best_pos <= 50:
                scores["ranking_strength"] = 25
                details["ranking_strength"] = f"Best position: {best_pos:.0f}"
            else:
                scores["ranking_strength"] = 10
                details["ranking_strength"] = f"Best position: {best_pos:.0f}"
        else:
            scores["ranking_strength"] = 0
            details["ranking_strength"] = "No position data"
    else:
        scores["keyword_coverage"] = 0
        details["keyword_coverage"] = "No Semrush data"
        scores["ranking_strength"] = 0
        details["ranking_strength"] = "No Semrush data"

    weighted_score = sum(
        scores.get(k, 0) * search_weights.get(k, 0)
        for k in search_weights
    )

    return {
        "search_score": round(weighted_score),
        "breakdown": {k: {"score": scores[k], "detail": details[k]} for k in scores},
        "data_sources": data_sources,
    }


def compute_composite_score(on_page_result, search_result):
    """Blend on-page and search performance scores into a composite."""
    config = get_setting("rules.composite_scoring", DEFAULT_COMPOSITE_SCORING)

    on_page_score = on_page_result["score"] if on_page_result else 0

    if search_result is None:
        return {
            "composite_score": on_page_score,
            "on_page_score": on_page_score,
            "search_score": None,
            "search_breakdown": None,
        }

    search_score = search_result["search_score"]
    sources = search_result.get("data_sources", [])

    if "gsc" in sources and "semrush" in sources:
        weights = config.get("weights_with_search", DEFAULT_COMPOSITE_SCORING["weights_with_search"])
    else:
        weights = config.get("weights_gsc_only", DEFAULT_COMPOSITE_SCORING["weights_gsc_only"])

    composite = on_page_score * weights["on_page"] + search_score * weights["search"]

    return {
        "composite_score": round(composite),
        "on_page_score": on_page_score,
        "search_score": search_score,
        "search_breakdown": search_result["breakdown"],
    }
