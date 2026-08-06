"""Post-generation quality gate for AI-written SEO content.

LLMs overshoot character targets and ignore positional rules often enough
that generated content used to fail the app's own scoring checks on arrival
(meta lengths landing in the "ok" band, keyword mid-title, keyword stuffed
into every image alt). This module validates a parsed AI result against the
exact same rules the technical analyzer scores with, asks the model ONCE to
rephrase what failed, re-validates, and only then falls back to deterministic
clamps — so a broken phrase never reaches Shopify but rephrasing, not
truncation, does the fixing.

Everything here fails open: a validator error can never block or corrupt a
result. Corrections are recorded under result["_qa"] for debugging.
"""

import json
import logging
import re

from app.prompt_defaults import DEFAULTS
from app.services.technical_seo_analyzer import (
    _keyword_in_text,
    _keyword_position_in_title,
)
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

META_TITLE_MAX = 60
META_DESC_MAX = 160
ALT_MAX = 125


def _title_first_n():
    try:
        rules = get_setting("rules.technical_product", DEFAULTS["rules.technical_product"])
        return int(rules.get("thresholds", {}).get("keyword_at_title_start", 5))
    except Exception:
        return 5


def alt_text_of(entry):
    """Alt text of a suggestion entry — dicts may carry it under either key
    (generation uses suggested_alt, apply uses alt), strings are themselves."""
    if isinstance(entry, dict):
        return str(entry.get("suggested_alt") or entry.get("alt") or "")
    return str(entry)


def clean_keyword(kw):
    """Strip a trailing volume suffix — 'leather bag (2,400)' -> 'leather bag'."""
    if not kw or not isinstance(kw, str):
        return None
    cleaned = re.sub(r"\s*\([\d,.\s]*\)\s*$", "", kw).strip()
    return cleaned or None


def clamp_word_boundary(text, limit):
    if not text or len(text) <= limit:
        return text
    truncated = text[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return truncated if len(truncated) <= limit else text[:limit]


def strip_keyword_phrase(alt, keyword):
    """Remove the keyword phrase from an alt, tidying leftover separators."""
    if not alt or not keyword:
        return alt
    stripped = re.sub(re.escape(keyword), "", alt, flags=re.IGNORECASE)
    stripped = re.sub(r"\s{2,}", " ", stripped)
    stripped = re.sub(r"^[\s\-–—:,;]+|[\s\-–—:,;]+$", "", stripped)
    return stripped or alt


def find_violations(fields, primary_keyword):
    """Deterministic checks mirroring the technical analyzer's scoring.

    fields: {meta_title?, meta_description?, title?, alts?: [str]}
    Returns a list of {kind, field, detail, ...} dicts; empty = clean.
    """
    violations = []
    mt = fields.get("meta_title")
    if mt and len(mt) > META_TITLE_MAX:
        violations.append({
            "kind": "length", "field": "meta_title", "length": len(mt),
            "limit": META_TITLE_MAX, "aim": 50,
        })
    md = fields.get("meta_description")
    if md and len(md) > META_DESC_MAX:
        violations.append({
            "kind": "length", "field": "meta_description", "length": len(md),
            "limit": META_DESC_MAX, "aim": 150,
        })
    title = fields.get("title")
    if title and primary_keyword:
        if _keyword_position_in_title(primary_keyword, title, _title_first_n()) != 100:
            violations.append({"kind": "title_keyword", "field": "title"})
    alts = fields.get("alts")
    if alts and primary_keyword and len(alts) >= 2:
        with_kw = [i for i, a in enumerate(alts) if a and _keyword_in_text(primary_keyword, a)]
        if len(with_kw) == len(alts):
            # All alts carry the keyword — the scorer treats this as stuffing.
            violations.append({
                "kind": "alt_stuffing", "field": "alts",
                "indexes": list(range(1, len(alts))),
            })
    return violations


def _corrective_prompt(violations, fields, primary_keyword):
    lines = [
        "You wrote SEO content that violates hard rules. Rewrite ONLY the fields "
        "listed below. REPHRASE — do not truncate mid-phrase. Keep the meaning.",
    ]
    if primary_keyword:
        lines.append(f'Primary keyword: "{primary_keyword}"')
    keys = []
    for v in violations:
        if v["kind"] == "length":
            keys.append(v["field"])
            lines.append(
                f'- {v["field"]} is {v["length"]} characters: '
                f'"{fields.get(v["field"])}"\n'
                f'  Rewrite to AT MOST {v["limit"]} characters (aim for about {v["aim"]}). '
                "Keep the primary keyword near the front."
            )
        elif v["kind"] == "title_keyword":
            keys.append("title")
            lines.append(
                f'- title: "{fields.get("title")}"\n'
                f'  The primary keyword must begin within the first {_title_first_n()} words. '
                "REORDER the existing elements (brand, product name, pack size, count, "
                "flavor) — keep every element, drop nothing, invent nothing."
            )
        elif v["kind"] == "alt_stuffing":
            keys.append("alts")
            alts = fields.get("alts") or []
            numbered = "\n".join(f'  {i}: "{alts[i]}"' for i in v["indexes"] if i < len(alts))
            lines.append(
                "- Every image alt contains the primary keyword — search engines "
                "treat that as stuffing. Rewrite ONLY these alts WITHOUT the "
                f'phrase "{primary_keyword}" (describe the image naturally — angle, '
                f"detail, packaging, lifestyle; under {ALT_MAX} characters). Return "
                'them under "alts" as {"<index>": "<new alt>"}:\n' + numbered
            )
    lines.append(
        "Respond with ONLY a JSON object containing exactly these keys: "
        + ", ".join(sorted(set(keys)))
    )
    return "\n".join(lines)


def corrective_rewrite(client, model, violations, fields, primary_keyword,
                       max_tokens=600):
    """One bounded chat call rephrasing the violating fields. {} on failure."""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": _corrective_prompt(violations, fields, primary_keyword)}],
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=30,
        )
        content = resp.choices[0].message.content or ""
        start, end = content.find("{"), content.rfind("}") + 1
        if start == -1 or end <= start:
            return {}
        return json.loads(content[start:end])
    except Exception as e:
        logger.warning("Corrective rewrite failed: %s", e)
        return {}


def _apply_length_fallbacks(fields, violations):
    for v in violations:
        if v["kind"] == "length":
            fields[v["field"]] = clamp_word_boundary(fields[v["field"]], v["limit"])


def _apply_alt_fallback(alts, primary_keyword):
    """Keep the keyword in the hero alt only; strip it from the rest."""
    return [alts[0]] + [strip_keyword_phrase(a, primary_keyword) for a in alts[1:]]


def _validate_fields(fields, primary_keyword, client, model, qa):
    """Shared core: violations -> one corrective call -> re-check -> fallbacks.

    Mutates and returns `fields`. `qa` collects an audit trail.
    """
    violations = find_violations(fields, primary_keyword)
    if not violations:
        return fields
    qa["violations"] = [
        {k: v for k, v in viol.items() if k != "indexes"} for viol in violations
    ]

    fix = {}
    if client is not None and model:
        fix = corrective_rewrite(client, model, violations, fields, primary_keyword)
    for key in ("meta_title", "meta_description", "title"):
        if isinstance(fix.get(key), str) and fix[key].strip():
            fields[key] = fix[key].strip()
    if isinstance(fix.get("alts"), dict) and fields.get("alts"):
        for idx_str, new_alt in fix["alts"].items():
            try:
                idx = int(idx_str)
            except (TypeError, ValueError):
                continue
            if 0 < idx < len(fields["alts"]) and isinstance(new_alt, str) and new_alt.strip():
                fields["alts"][idx] = new_alt.strip()[:ALT_MAX]
    if fix:
        qa["rephrased"] = sorted(fix.keys())

    remaining = find_violations(fields, primary_keyword)
    if remaining:
        _apply_length_fallbacks(fields, remaining)
        for v in remaining:
            if v["kind"] == "alt_stuffing" and fields.get("alts"):
                fields["alts"] = _apply_alt_fallback(fields["alts"], primary_keyword)
            elif v["kind"] == "title_keyword":
                # Never machine-mangle a title — log and leave it to the human.
                logger.info(
                    "Title still lacks leading keyword %r after rephrase: %r",
                    primary_keyword, fields.get("title"),
                )
        qa["fallbacks"] = [v["kind"] for v in remaining]
    return fields


def validate_seo_result(result, primary_keyword, client, config):
    """Quality-gate a parsed product_seo/collection_seo main result in place."""
    try:
        if not isinstance(result, dict) or result.get("error") or result.get("raw_response"):
            return result
        alts = None
        alt_entries = result.get("image_alt_suggestions")
        if isinstance(alt_entries, list) and alt_entries:
            alts = [(e.get("suggested_alt") or "") for e in alt_entries]
        fields = {
            "meta_title": result.get("optimized_meta_title"),
            "meta_description": result.get("optimized_meta_description"),
            "title": result.get("h1_tag"),
            "alts": alts,
        }
        qa = {}
        _validate_fields(fields, primary_keyword,
                         client, (config or {}).get("model"), qa)
        if fields.get("meta_title"):
            result["optimized_meta_title"] = fields["meta_title"]
        if fields.get("meta_description"):
            result["optimized_meta_description"] = fields["meta_description"]
        if fields.get("title"):
            result["h1_tag"] = fields["title"]
        if alts is not None and fields.get("alts"):
            for entry, new_alt in zip(alt_entries, fields["alts"]):
                entry["suggested_alt"] = new_alt
        if qa:
            result["_qa"] = qa
            logger.info("AI content QA (%s): %s",
                        primary_keyword or "no keyword", qa)
    except Exception:
        logger.exception("validate_seo_result failed — result passed through")
    return result


def validate_named_fields(result, field_map, primary_keyword, client, config):
    """Quality-gate arbitrary meta fields in place.

    `field_map` maps validator field names ("meta_title"/"meta_description"/
    "title") to the result's own key names — brand results use
    optimized_meta_*, GEO uses meta_* — so every generator gets the same
    rephrase-first, clamp-last treatment without renaming its output."""
    try:
        if not isinstance(result, dict) or result.get("error") or result.get("raw_response"):
            return result
        fields = {std: result.get(key) for std, key in field_map.items()}
        qa = {}
        _validate_fields(fields, primary_keyword, client, (config or {}).get("model"), qa)
        for std, key in field_map.items():
            if fields.get(std):
                result[key] = fields[std]
        if qa:
            result["_qa"] = qa
            logger.info("AI content QA (%s): %s", primary_keyword or "no keyword", qa)
    except Exception:
        logger.exception("validate_named_fields failed — result passed through")
    return result


def validate_technical_fixes(result, primary_keyword, client, config):
    """Quality-gate a technical_seo_fix result's fixes[] in place."""
    try:
        fixes = (result or {}).get("fixes")
        if not isinstance(fixes, list):
            return result
        by_field = {f.get("field"): f for f in fixes if isinstance(f, dict)}
        alt_fix = by_field.get("image_alt")
        alts = None
        if alt_fix and isinstance(alt_fix.get("proposed_value"), list):
            alts = [(a.get("alt") if isinstance(a, dict) else str(a)) or ""
                    for a in alt_fix["proposed_value"]]
        fields = {
            "meta_title": _fix_str(by_field.get("meta_title")),
            "meta_description": _fix_str(by_field.get("meta_description")),
            "title": _fix_str(by_field.get("title")),
            "alts": alts,
        }
        qa = {}
        _validate_fields(fields, primary_keyword,
                         client, (config or {}).get("model"), qa)
        for key in ("meta_title", "meta_description", "title"):
            if by_field.get(key) and fields.get(key):
                by_field[key]["proposed_value"] = fields[key]
        if alt_fix and alts is not None and fields.get("alts"):
            proposed = alt_fix["proposed_value"]
            for i, new_alt in enumerate(fields["alts"]):
                if i >= len(proposed):
                    break
                if isinstance(proposed[i], dict):
                    proposed[i]["alt"] = new_alt
                else:
                    proposed[i] = new_alt
        if qa:
            result["_qa"] = qa
            logger.info("Technical fix QA (%s): %s",
                        primary_keyword or "no keyword", qa)
    except Exception:
        logger.exception("validate_technical_fixes failed — result passed through")
    return result


def _fix_str(fix):
    if fix and isinstance(fix.get("proposed_value"), str):
        return fix["proposed_value"]
    return None


def ensure_applied_alt_balance(images, media_updates, primary_keyword):
    """Apply-time deterministic backstop: if the post-apply alt set would have
    the primary keyword in EVERY alt, strip it from non-hero updates (and, if
    needed, add one de-keyworded update for an existing non-hero alt) so the
    applied state can't score as stuffing. Returns the (possibly extended)
    media_updates list."""
    try:
        if not primary_keyword or not images or not media_updates:
            return media_updates
        alt_key = "alt" if any("alt" in (u or {}) for u in media_updates) else "suggested_alt"
        by_media = {u.get("media_id"): u for u in media_updates if u.get("media_id")}
        final_alts = []  # (media_id, alt, is_update)
        for img in images:
            mid = img.get("media_id")
            upd = by_media.get(mid)
            alt = (upd.get(alt_key) if upd else img.get("alt")) or ""
            final_alts.append((mid, alt, upd is not None))
        real = [(m, a, u) for m, a, u in final_alts if a]
        if len(real) < 2:
            return media_updates
        if not all(_keyword_in_text(primary_keyword, a) for _, a, _ in real):
            return media_updates
        hero_mid = real[0][0]
        changed = False
        for mid, alt, is_update in real[1:]:
            if mid is None or mid == hero_mid:
                continue
            stripped = strip_keyword_phrase(alt, primary_keyword)
            if stripped == alt:
                continue
            if is_update:
                by_media[mid][alt_key] = stripped
            else:
                media_updates.append({"media_id": mid, alt_key: stripped})
            changed = True
            if not is_update:
                break  # one de-keyworded existing alt is enough for balance
        if changed:
            logger.info("Rebalanced stuffed image alts for keyword %r", primary_keyword)
    except Exception:
        logger.exception("ensure_applied_alt_balance failed — updates passed through")
    return media_updates
