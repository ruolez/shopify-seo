"""Demand-grounded keyword resolution.

One resolution per item, blending every keyword signal in the system:
on-page n-grams (relevance anchor), the page's real GSC queries, Semrush
keywords (URL-matched, semantic fallback), and Keyword Strategy mappings.
Candidates are scored relevance x demand x opportunity with a
cannibalization penalty; a manually assigned keyword (keyword_assignments
status accepted/applied) always wins the primary slot.

Without any demand data the ranking degenerates to relevance order, which
matches the old pure-n-gram behavior — a safe fallback.
"""

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.technical_seo_analyzer import _extract_keywords, _keyword_in_text, _strip_html
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)


# The shared identity primitives live in keyword_identity; re-exported here
# because most consumers import them from this module.
from app.services.keyword_identity import fold_token, variant_key  # noqa: F401
from app.services.keyword_identity import norm as _norm  # noqa: F401


def _rules():
    return get_setting("rules.keyword_resolution", DEFAULTS["rules.keyword_resolution"])


def _item_table(item_type):
    return "synced_products" if item_type == "product" else "synced_collections"


def _url_path(item_type, handle):
    prefix = "products" if item_type == "product" else "collections"
    return f"{prefix}/{handle}"


def build_prefetch(store_id, item_type, item_ids):
    """Batch the store-level queries for bulk resolution loops."""
    rules = _rules()
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=rules.get("gsc_days", 30))).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
    from app.services.url_utils import gsc_base_url
    base_url = gsc_base_url(cred.get("gsc_property") if cred else "")

    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT id, handle FROM {_item_table(item_type)} WHERE id = ANY(%s)",
            (list(item_ids),),
        )
        handles = {r["id"]: r["handle"] for r in cur.fetchall()}

    gsc_pages = {}
    if base_url and handles:
        page_urls = [f"{base_url}/{_url_path(item_type, h)}" for h in handles.values() if h]
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT page, query,
                          SUM(clicks) as clicks,
                          SUM(impressions) as impressions,
                          SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND page = ANY(%s) AND date >= %s AND date <= %s
                   GROUP BY page, query
                   HAVING SUM(impressions) > 0""",
                (store_id, page_urls, date_from, date_to),
            )
            for r in cur.fetchall():
                gsc_pages.setdefault(r["page"], []).append(r)
        for rows in gsc_pages.values():
            rows.sort(key=lambda r: float(r["clicks"] or 0), reverse=True)

    with db.get_cursor() as cur:
        # No volume cap here: the old store-wide top-500 starved items of
        # their own low-volume long-tail URL matches. Only the semantic
        # fallback pool (scored against every item in the batch) stays capped.
        cur.execute(
            """SELECT keyword, position, search_volume, cpc, url
               FROM semrush_keywords
               WHERE store_id = %s AND url IS NOT NULL AND url != ''
               ORDER BY search_volume DESC NULLS LAST""",
            (store_id,),
        )
        semrush = cur.fetchall()

    strategy = {}
    assignments = {}
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ckm.item_id, ckm.is_primary, ckm.relevance_score,
                      mk.keyword, mk.search_volume, mk.intent
               FROM content_keyword_map ckm
               JOIN master_keywords mk ON mk.id = ckm.master_keyword_id
               WHERE ckm.store_id = %s AND ckm.item_type = %s AND ckm.item_id = ANY(%s)
               ORDER BY ckm.relevance_score DESC NULLS LAST""",
            (store_id, item_type, list(item_ids)),
        )
        for r in cur.fetchall():
            bucket = strategy.setdefault(r["item_id"], [])
            if len(bucket) < 30:
                bucket.append(r)

        cur.execute(
            """SELECT ka.assigned_item_id, ka.status, ka.updated_at,
                      ka.assignment_reason,
                      mk.keyword, mk.search_volume, mk.intent
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ka.store_id = %s AND ka.assigned_item_type = %s
                 AND ka.assigned_item_id = ANY(%s)
                 AND ka.status IN ('accepted', 'applied')
               ORDER BY (ka.assignment_reason = 'manual_primary') DESC NULLS LAST,
                        ka.updated_at DESC""",
            (store_id, item_type, list(item_ids)),
        )
        for r in cur.fetchall():
            assignments.setdefault(r["assigned_item_id"], []).append(r)

    return {
        "base_url": base_url,
        "handles": handles,
        "gsc_pages": gsc_pages,
        "semrush": semrush,
        "semrush_semantic_pool": semrush[:500],
        "strategy": strategy,
        "assignments": assignments,
        "date_from": date_from,
        "date_to": date_to,
    }


def _semantic_semrush_matches(item, semrush_rows):
    """Relevance-score unmapped Semrush keywords against the item's identity
    (vendor / product_type / handle phrases) — mirrors the ai_agent fallback."""
    identity_phrases = []
    vendor = (item.get("vendor") or "").lower().strip()
    ptype = (item.get("product_type") or "").lower().strip()
    if vendor and len(vendor) > 2:
        identity_phrases.append(vendor)
    if ptype and len(ptype) > 2:
        identity_phrases.append(ptype)

    generic_words = set(_rules().get("semantic_generic_words") or [
        "the", "and", "for", "with", "pack", "packs", "box", "can", "cans",
        "100", "1pk", "5pk", "10pk", "pre", "priced", "new", "best", "buy",
        "shop", "online", "sale", "product"])
    handle_str = (item.get("handle") or "").replace("-", " ").lower()
    hw = handle_str.split()
    handle_words = [w for w in hw if len(w) > 2 and w not in generic_words]
    handle_bigrams = [f"{hw[i]} {hw[i+1]}" for i in range(len(hw) - 1)
                      if hw[i] not in generic_words and hw[i + 1] not in generic_words]

    scored = []
    seen = set()
    for r in semrush_rows:
        kw = _norm(r.get("keyword"))
        vol = int(r.get("search_volume") or 0)
        if not kw or vol <= 0 or kw in seen:
            continue
        rel = 0
        identity_match = any(p in kw for p in identity_phrases)
        bigram_match = any(bg in kw for bg in handle_bigrams)
        word_matches = sum(1 for w in handle_words if w in kw)
        if identity_match:
            rel += 10
        if bigram_match:
            rel += 5
        rel += word_matches * 2
        if not identity_match and not bigram_match and word_matches < 2:
            continue
        if rel >= 5:
            scored.append((rel, vol, r))
            seen.add(kw)
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s[2] for s in scored[:15]]


def _relevance(candidate, fields, rel_rules):
    """0..1 — where does this keyword actually appear on the page?"""
    kw = candidate["keyword"]
    points = 0
    for field_name, weight in (
        ("title", rel_rules.get("title", 40)),
        ("meta_title", rel_rules.get("meta_title", 15)),
        ("handle", rel_rules.get("handle", 15)),
        ("body_first_100", rel_rules.get("body_first_100", 15)),
        ("meta_description", rel_rules.get("meta_description", 10)),
        ("body", rel_rules.get("body", 5)),
    ):
        if _keyword_in_text(kw, fields.get(field_name) or ""):
            points += weight
    conf = candidate.get("confidence") or 0
    points += min(conf, 20) * rel_rules.get("ngram_confidence_scale", 2.0)
    ckm_rel = candidate.get("ckm_relevance")
    if ckm_rel:
        points = max(points, float(ckm_rel))
    return min(points, 100) / 100.0


def resolve_item_keywords(store_id, item_type, item_id, item=None, prefetch=None):
    """Full keyword resolution for one item. Returns
    {primary, candidates, resolved_at} or a shell when the
    item has no resolvable keywords at all."""
    rules = _rules()

    if item is None:
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {_item_table(item_type)} WHERE id = %s", (item_id,))
            item = cur.fetchone()
    if not item:
        return None
    item = dict(item)

    if prefetch is None:
        prefetch = build_prefetch(store_id, item_type, [item_id])

    handle = item.get("handle") or prefetch["handles"].get(item_id) or ""
    page_url = f"{prefetch['base_url']}/{_url_path(item_type, handle)}" if prefetch["base_url"] and handle else ""

    body_text = _strip_html(item.get("description") or "")
    body_words = body_text.split()
    fields = {
        "title": item.get("title") or "",
        "meta_title": item.get("meta_title") or item.get("title") or "",
        "handle": handle.replace("-", " "),
        "body_first_100": " ".join(body_words[:100]),
        "meta_description": item.get("meta_description") or "",
        "body": body_text,
    }

    # ── Candidate collection ─────────────────────────────────────────
    candidates = {}

    def get_cand(kw_display):
        key = _norm(kw_display)
        if not key:
            return None
        if key not in candidates:
            candidates[key] = {
                "keyword": key,
                "confidence": 0,
                "sources": [],
                "volume": None,
                "gsc_clicks": None,
                "gsc_impressions": None,
                "gsc_position": None,
                "semrush_position": None,
                "intent": None,
                "ckm_relevance": None,
                "cannibalization": None,
                "manual": False,
            }
        return candidates[key]

    def add_source(cand, source):
        if source not in cand["sources"]:
            cand["sources"].append(source)

    # 1. On-page n-grams (relevance anchor)
    on_page = _extract_keywords(item) or []
    for kw in on_page:
        cand = get_cand(kw["keyword"])
        if cand is None:
            continue
        cand["confidence"] = max(cand["confidence"], float(kw.get("confidence") or 0))
        if kw.get("packaging_only"):
            cand["packaging_only"] = True
        add_source(cand, "page")

    # 2. GSC page queries
    for r in (prefetch["gsc_pages"].get(page_url) or [])[:15]:
        cand = get_cand(r["query"])
        if cand is None:
            continue
        cand["gsc_clicks"] = float(r["clicks"] or 0)
        cand["gsc_impressions"] = float(r["impressions"] or 0)
        cand["gsc_position"] = round(float(r["position"] or 0), 1) if r["position"] else None
        add_source(cand, "gsc")

    # 3. Semrush URL-matched (4. semantic fallback when none)
    url_matched = []
    for r in prefetch["semrush"]:
        url = r["url"] or ""
        path = url.replace(prefetch["base_url"], "").strip("/") if prefetch["base_url"] else ""
        if not path:
            try:
                path = urlparse(url).path.strip("/")
            except Exception:
                path = url
        if path == _url_path(item_type, handle):
            url_matched.append(r)
        if len(url_matched) >= 20:
            break
    sr_rows = url_matched
    sr_source = "semrush"
    if not sr_rows:
        sr_rows = _semantic_semrush_matches(
            item, prefetch.get("semrush_semantic_pool") or prefetch["semrush"])
        sr_source = "semrush_semantic"
    for r in sr_rows:
        cand = get_cand(r["keyword"])
        if cand is None:
            continue
        vol = int(r.get("search_volume") or 0)
        if vol:
            cand["volume"] = max(cand["volume"] or 0, vol)
        pos = r.get("position")
        if pos not in (None, 0):
            cand["semrush_position"] = float(pos)
        add_source(cand, sr_source)

    # 5. Keyword Strategy mappings + manual assignments
    for r in prefetch["strategy"].get(item_id) or []:
        cand = get_cand(r["keyword"])
        if cand is None:
            continue
        if r.get("search_volume"):
            cand["volume"] = max(cand["volume"] or 0, int(r["search_volume"]))
        if r.get("intent"):
            cand["intent"] = r["intent"]
        if r.get("relevance_score") is not None:
            cand["ckm_relevance"] = max(cand["ckm_relevance"] or 0, float(r["relevance_score"]))
        add_source(cand, "strategy")

    manual_kw = None
    for r in prefetch["assignments"].get(item_id) or []:
        cand = get_cand(r["keyword"])
        if cand is None:
            continue
        cand["manual"] = True
        if r.get("search_volume"):
            cand["volume"] = max(cand["volume"] or 0, int(r["search_volume"]))
        if r.get("intent"):
            cand["intent"] = r["intent"]
        add_source(cand, "strategy")
        # Explicit user pins outrank plan-accepted assignments regardless of
        # recency — a later plan-accept must never silently override a pin.
        # Rows are ordered manual_primary-first, then updated_at DESC.
        if manual_kw is None:
            manual_kw = _norm(r["keyword"])

    if not candidates:
        return {
            "primary": None,
            "candidates": [],
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Scoring ──────────────────────────────────────────────────────
    rel_rules = rules.get("relevance", {})
    dem = rules.get("demand", {})
    opp = rules.get("opportunity", {})
    spec = rules.get("specificity", {})
    vendor_norm = _norm(item.get("vendor"))
    ptype_norm = _norm(item.get("product_type"))

    for cand in candidates.values():
        relevance = _relevance(cand, fields, rel_rules)
        cand["relevance"] = round(relevance, 3)

        # Specificity: a bare brand or category term is a collection keyword,
        # not a product primary — dozens of sibling products sharing it would
        # cannibalize each other. Such terms stay listed but are flagged
        # generic and can never win a product's primary slot, no matter the
        # volume.
        kw_tokens = set(cand["keyword"].split())
        vendor_tokens = set(vendor_norm.split()) if vendor_norm else set()
        word_count = len(kw_tokens)
        generic = False

        # Family-generic: keyword built ONLY from brand/category tokens (with
        # light plural folding) carries no distinguishing token of this item —
        # every sibling product would share it. Head terms like that belong to
        # the brand/category collection; product pages target the long tail.
        family_stems = {fold_token(t) for t in vendor_tokens} | {fold_token(t) for t in ptype_norm.split()}
        kw_stems = {fold_token(t) for t in kw_tokens}

        if item_type == "product" and vendor_tokens and kw_tokens.issubset(vendor_tokens):
            specificity = spec.get("vendor_only_product", 0.4)
            generic = True
        elif item_type == "product" and ptype_norm and cand["keyword"] == ptype_norm:
            specificity = spec.get("type_only_product", 0.5)
            generic = True
        elif item_type == "product" and family_stems and kw_stems and kw_stems.issubset(family_stems):
            specificity = spec.get("vendor_only_product", 0.4)
            generic = True
        elif word_count == 1:
            specificity = (spec.get("single_word_product", 0.5) if item_type == "product"
                           else spec.get("single_word_collection", 0.6))
        elif word_count >= 3:
            specificity = spec.get("multi_word_bonus", 1.05)
        else:
            specificity = 1.0
        # A primary must actually describe the item: it has to match the title
        # or handle (token-wise). High-volume store-level terms bleeding in via
        # body text stay listed as candidates but can never win the slot.
        identity_match = (_keyword_in_text(cand["keyword"], fields["title"])
                          or _keyword_in_text(cand["keyword"], fields["handle"]))
        if not identity_match:
            generic = True
        cand["specificity"] = specificity
        if generic:
            cand["generic"] = True

        vol_part = math.log1p(cand["volume"] or 0) / math.log1p(dem.get("volume_ref", 10000))
        impr_part = math.log1p(cand["gsc_impressions"] or 0) / math.log1p(dem.get("impressions_ref", 1000))
        demand = max(vol_part, impr_part)
        if demand <= 0:
            demand = dem.get("floor", 0.15)
        demand = min(demand, dem.get("cap", 1.5))

        pos = cand["gsc_position"] or cand["semrush_position"]
        band = opp.get("striking_band", [4, 20])
        if pos is None:
            opportunity = 1.0
        elif band[0] <= pos <= band[1]:
            opportunity = opp.get("striking_mult", 1.3)
        elif pos < band[0]:
            opportunity = opp.get("top_mult", 1.1)
        else:
            opportunity = opp.get("beyond_mult", 0.9)

        cand["score"] = round(100 * relevance * demand * opportunity * specificity, 1)

    # ── Cannibalization (batched over top candidates with demand) ────
    ranked = sorted(candidates.values(), key=lambda c: c["score"], reverse=True)
    to_check = [c["keyword"] for c in ranked[:rules.get("max_candidates", 15)]
                if c["volume"] or c["gsc_impressions"]]
    if to_check and page_url:
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT LOWER(query) as q, page,
                              SUM(position * impressions) / NULLIF(SUM(impressions), 0) as pos,
                              SUM(impressions) as impr
                       FROM gsc_daily_metrics
                       WHERE store_id = %s AND LOWER(query) = ANY(%s)
                         AND date >= %s AND date <= %s
                       GROUP BY LOWER(query), page
                       HAVING SUM(impressions) > 0""",
                    (store_id, to_check, prefetch["date_from"], prefetch["date_to"]),
                )
                rows = cur.fetchall()
            for r in rows:
                if (r["page"] or "").rstrip("/") == page_url.rstrip("/"):
                    continue
                pos = float(r["pos"] or 0)
                if pos and pos <= 10:
                    cand = candidates.get(r["q"])
                    if cand and (cand["cannibalization"] is None or pos < cand["cannibalization"]["position"]):
                        cand["cannibalization"] = {"competing_url": r["page"], "position": round(pos, 1)}
        except Exception:
            logger.warning("Cannibalization check failed for %s %s", item_type, item_id)

    penalty = rules.get("cannibalization_mult", 0.5)
    for cand in candidates.values():
        if cand["cannibalization"]:
            cand["score"] = round(cand["score"] * penalty, 1)

    # ── Primary selection ────────────────────────────────────────────
    ranked = sorted(candidates.values(), key=lambda c: c["score"], reverse=True)
    min_rel = rel_rules.get("min_relevance", 10) / 100.0

    primary = None
    note = None
    if manual_kw and manual_kw in candidates:
        primary = candidates[manual_kw]
        best_auto = next((c for c in ranked if not c["manual"] and c["relevance"] >= min_rel), None)
        if best_auto and best_auto["score"] > primary["score"]:
            note = f"higher-scoring alternative: {best_auto['keyword']}"
    else:
        primary = next(
            (c for c in ranked
             if c["relevance"] >= min_rel and not c.get("packaging_only") and not c.get("generic")),
            None,
        )
        if primary is None:
            primary = next((c for c in ranked if not c.get("generic")), None) or (ranked[0] if ranked else None)

    final = []
    if primary:
        final.append(primary)
    for c in ranked:
        if primary and c["keyword"] == primary["keyword"]:
            continue
        final.append(c)
        if len(final) >= rules.get("max_candidates", 15):
            break

    # detected_keywords-compatible entries: keyword + confidence stay; the
    # confidence badge now reflects the resolved score.
    for c in final:
        c["confidence"] = c["score"]

    result = {
        "primary": primary,
        "candidates": final,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        result["note"] = note
    return result


def _pick_unclaimed_alternative(res, claimed, min_rel):
    """Walk an item's candidate list for the best eligible keyword not in
    `claimed` (a set of variant_key strings). Returns the candidate dict
    or None."""
    primary_key = variant_key(res["primary"]["keyword"])
    for c in res.get("candidates", []):
        kw = c["keyword"]
        if variant_key(kw) == primary_key or variant_key(kw) in claimed:
            continue
        if c.get("generic") or c.get("packaging_only") or c.get("manual"):
            continue
        if (c.get("relevance") or 0) < min_rel:
            continue
        return c
    return None


def _swap_primary_if_claimed(store_id, item_type, item_id, res):
    """Keep the one-keyword-one-page invariant after a single-item refresh:
    when another item of this type owns the new primary with a stronger claim
    (manual, or higher score), walk to the next eligible unclaimed candidate.
    Returns True when the primary was swapped."""
    primary = res.get("primary") or {}
    kw = primary.get("keyword")
    if not kw or primary.get("manual"):
        return False  # manual wins — never displaced by another page's claim
    try:
        with db.get_cursor() as cur:
            # Both item types: one keyword, one page — store-wide.
            cur.execute(
                """SELECT keyword_resolution->'primary'->>'keyword' AS kw,
                          COALESCE((keyword_resolution->'primary'->>'score')::float, 0) AS score,
                          COALESCE((keyword_resolution->'primary'->>'manual')::boolean, false) AS manual
                   FROM technical_seo_analyses
                   WHERE store_id = %s AND NOT (item_type = %s AND item_id = %s)
                     AND keyword_resolution->'primary'->>'keyword' IS NOT NULL""",
                (store_id, item_type, item_id),
            )
            rows = cur.fetchall()
            # Manual assignments on any OTHER item (either type) reserve the
            # keyword too — the resolution cache may not reflect them yet.
            cur.execute(
                """SELECT mk.keyword AS kw
                   FROM keyword_assignments ka
                   JOIN master_keywords mk ON mk.id = ka.master_keyword_id
                   WHERE ka.store_id = %s AND ka.status IN ('accepted', 'applied')
                     AND NOT (ka.assigned_item_type = %s AND ka.assigned_item_id = %s)""",
                (store_id, item_type, item_id),
            )
            assignment_rows = cur.fetchall()
    except Exception:
        return False

    claimed = {}
    for r in rows:
        key = variant_key(r["kw"])
        prev = claimed.get(key)
        if prev is None or r["manual"] > prev["manual"] or r["score"] > prev["score"]:
            claimed[key] = {"score": float(r["score"] or 0), "manual": bool(r["manual"])}
    for r in assignment_rows:
        key = variant_key(r["kw"])
        if key:
            claimed[key] = {"score": float("inf"), "manual": True}

    owner = claimed.get(variant_key(kw))
    my_score = float(primary.get("score") or 0)
    # Ties count as claimed too (>=): twin pages resolve identical scores, and
    # the incumbent keeps the keyword — matching the bulk dedup semantics.
    if not owner or not (owner["manual"] or owner["score"] >= my_score):
        return False

    min_rel = _rules().get("relevance", {}).get("min_relevance", 10) / 100.0
    replacement = _pick_unclaimed_alternative(res, set(claimed), min_rel)
    if replacement is None:
        return False  # no viable alternative — sharing beats nothing
    res["primary"] = replacement
    res["note"] = f"'{kw}' is owned by a higher-scoring page; using next-best candidate"
    res["candidates"] = [replacement] + [c for c in res["candidates"] if c["keyword"] != replacement["keyword"]]
    return True


def get_or_refresh_resolution(store_id, item_type, item_id, max_age_days=None):
    """Persisted keyword resolution for an item, refreshed when it is missing,
    older than rules.keyword_resolution.resolution_max_age_days, or superseded
    by a newer manual assignment. Never raises — returns the stale resolution
    (or None) when the refresh fails, so callers can't be blocked by it."""
    if item_type not in ("product", "collection"):
        return None
    if max_age_days is None:
        max_age_days = _rules().get("resolution_max_age_days", 7)

    resolution = None
    analyzed_at = None
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword_resolution, updated_at FROM technical_seo_analyses
                   WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                (store_id, item_type, item_id),
            )
            row = cur.fetchone()
        if row:
            resolution = row["keyword_resolution"]
            analyzed_at = row["updated_at"]
    except Exception:
        logger.warning("Resolution read failed for %s %s", item_type, item_id)

    stale = not resolution or not resolution.get("primary")
    if not stale and analyzed_at:
        now = datetime.now(timezone.utc) if analyzed_at.tzinfo else datetime.now()
        if now - analyzed_at > timedelta(days=max_age_days):
            stale = True
        else:
            try:
                with db.get_cursor() as cur:
                    cur.execute(
                        """SELECT MAX(updated_at) AS latest FROM keyword_assignments
                           WHERE store_id = %s AND assigned_item_type = %s
                             AND assigned_item_id = %s AND status IN ('accepted', 'applied')""",
                        (store_id, item_type, item_id),
                    )
                    latest = (cur.fetchone() or {}).get("latest")
                if latest and latest > analyzed_at:
                    stale = True  # manual assignment newer than the analysis
            except Exception:
                pass

    if not stale:
        return resolution

    try:
        from app.routes.technical_seo_routes import _resolve_and_analyze, _upsert_analysis

        with db.get_cursor() as cur:
            cur.execute(
                f"SELECT * FROM {_item_table(item_type)} WHERE id = %s AND store_id = %s",
                (item_id, store_id),
            )
            item = cur.fetchone()
        if not item:
            return resolution

        # _resolve_and_analyze already ran _swap_primary_if_claimed before
        # computing the checks — a second swap here re-scanned the whole
        # store and re-analyzed the item again on every lazy refresh.
        analysis, fresh = _resolve_and_analyze(item_type, dict(item))
        _upsert_analysis(store_id, item_type, item_id, analysis, fresh)
        return fresh or resolution
    except Exception:
        logger.warning("Resolution refresh failed for %s %s", item_type, item_id)
        return resolution


def get_owned_primary_keywords(store_id, exclude=None):
    """variant_key -> owning item for every resolved primary and
    accepted/applied assignment in the store, across products and collections.
    Keyed by variant_key (plural-folded, token-sorted) — the same identity
    dedup/claiming uses — so guards can't miss what claiming blocks. Each
    owner dict carries the display-form "keyword". Manual claims outrank
    resolved ones. `exclude` is an optional (item_type, item_id) whose own
    claims are skipped."""
    owned = {}
    exclude = tuple(exclude) if exclude else None

    def claim(kw, owner):
        key = variant_key(kw)
        if not key:
            return
        owner["keyword"] = kw
        prev = owned.get(key)
        if (prev is None or (owner["manual"] and not prev["manual"])
                or (owner["manual"] == prev["manual"]
                    and (owner["score"] or 0) > (prev["score"] or 0))):
            owned[key] = owner

    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT item_type, item_id,
                          keyword_resolution->'primary'->>'keyword' AS kw,
                          COALESCE((keyword_resolution->'primary'->>'score')::float, 0) AS score,
                          COALESCE((keyword_resolution->'primary'->>'manual')::boolean, false) AS manual,
                          COALESCE((keyword_resolution->'primary'->>'volume')::float, 0) AS volume
                   FROM technical_seo_analyses
                   WHERE store_id = %s
                     AND keyword_resolution->'primary'->>'keyword' IS NOT NULL""",
                (store_id,),
            )
            for r in cur.fetchall():
                if exclude and (r["item_type"], r["item_id"]) == exclude:
                    continue
                claim(r["kw"], {"item_type": r["item_type"], "item_id": r["item_id"],
                                "score": float(r["score"] or 0), "manual": bool(r["manual"]),
                                "volume": int(r["volume"] or 0)})

            cur.execute(
                """SELECT ka.assigned_item_type AS item_type, ka.assigned_item_id AS item_id,
                          mk.keyword, mk.search_volume
                   FROM keyword_assignments ka
                   JOIN master_keywords mk ON mk.id = ka.master_keyword_id
                   WHERE ka.store_id = %s AND ka.status IN ('accepted', 'applied')""",
                (store_id,),
            )
            for r in cur.fetchall():
                if exclude and (r["item_type"], r["item_id"]) == exclude:
                    continue
                claim(r["keyword"], {"item_type": r["item_type"], "item_id": r["item_id"],
                                     "score": None, "manual": True,
                                     "volume": int(r["search_volume"] or 0)})
    except Exception:
        logger.warning("Ownership map build failed for store %s", store_id)
    return owned


def keyword_ownership_guard(store_id, item_type, item_id, retarget_kw, ownership=None):
    """The shared retarget guard: returns (resolved_primary, conflict_error).
    conflict_error is set when the retarget keyword is already another page's
    primary — rewriting this page onto it would just move the cannibalization,
    not fix it. Pass a prebuilt `ownership` map when calling in a loop.

    Raises on internal failure — callers must fail closed (surface an error),
    never proceed as if the check passed."""
    resolved_primary = ""
    resolution = get_or_refresh_resolution(store_id, item_type, item_id)
    rp = (resolution or {}).get("primary")
    if rp and rp.get("keyword"):
        resolved_primary = rp["keyword"]
    # variant_key, not _norm: claiming folds plurals/word order, so the guard
    # must too — else "dutch master" slips past the "dutch masters" claim.
    tk = variant_key(retarget_kw or "")
    if tk and tk != variant_key(resolved_primary):
        if ownership is None:
            ownership = get_owned_primary_keywords(store_id, exclude=(item_type, item_id))
        owner = ownership.get(tk)
        if owner and (owner["item_type"], owner["item_id"]) != (item_type, item_id):
            return resolved_primary, (
                f'Target keyword "{retarget_kw}" is already the primary keyword of another page '
                f'({owner["item_type"]} #{owner["item_id"]}'
                + (", manually assigned" if owner.get("manual") else "")
                + "). Retargeting this page onto it would create new cannibalization — pick a different keyword."
            )
    return resolved_primary, None


def dedupe_primary_ownership(store_id, item_type):
    """One keyword, one page. After a bulk resolution, redistribute auto-picked
    primaries so no two items of a type share one — the anti-cannibalization
    rule from keyword clustering. The highest-scoring claimant keeps the
    keyword; every other item walks its candidate list for the best eligible
    unclaimed alternative (falling back to the shared keyword only when it has
    none). Manual assignments are untouchable and their keywords reserved.

    Returns the number of items reassigned.
    """
    from app.services.technical_seo_analyzer import (
        analyze_collection_technical_seo,
        analyze_product_technical_seo,
    )

    rules = _rules()
    min_rel = rules.get("relevance", {}).get("min_relevance", 10) / 100.0

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT item_id, keyword_resolution FROM technical_seo_analyses
               WHERE store_id = %s AND item_type = %s AND keyword_resolution IS NOT NULL""",
            (store_id, item_type),
        )
        rows = cur.fetchall()
        # Primaries of the OTHER item type and accepted assignments are not
        # redistributed by this pass, but they still reserve their slot —
        # ownership is store-wide, not per type.
        cur.execute(
            """SELECT keyword_resolution->'primary'->>'keyword' AS kw
               FROM technical_seo_analyses
               WHERE store_id = %s AND item_type != %s
                 AND keyword_resolution->'primary'->>'keyword' IS NOT NULL""",
            (store_id, item_type),
        )
        reserved_rows = cur.fetchall()
        cur.execute(
            """SELECT mk.keyword AS kw
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ka.store_id = %s AND ka.status IN ('accepted', 'applied')""",
            (store_id,),
        )
        reserved_rows += cur.fetchall()

    entries = []
    claimed = {variant_key(r["kw"]) for r in reserved_rows if r["kw"]}
    for r in rows:
        res = r["keyword_resolution"]
        if not res or not res.get("primary"):
            continue
        if res["primary"].get("manual"):
            claimed.add(variant_key(res["primary"]["keyword"]))
            continue
        entries.append({"item_id": r["item_id"], "res": res})

    # Strongest claim wins each keyword; process in descending score order.
    # Claims are keyed by variant_key, so singular/plural and word-order
    # variants ('dutch master' vs 'dutch masters') contest ONE slot.
    entries.sort(key=lambda e: e["res"]["primary"].get("score") or 0, reverse=True)

    demoted = []
    for e in entries:
        res = e["res"]
        primary_key = variant_key(res["primary"]["keyword"])
        if primary_key not in claimed:
            claimed.add(primary_key)
            continue
        replacement = _pick_unclaimed_alternative(res, claimed, min_rel)
        if replacement is None:
            continue  # no viable alternative — sharing beats nothing
        claimed.add(variant_key(replacement["keyword"]))
        old_kw = res["primary"]["keyword"]
        res["primary"] = replacement
        res["note"] = f"'{old_kw}' is owned by a higher-scoring page; using next-best candidate"
        res["candidates"] = [replacement] + [c for c in res["candidates"] if c["keyword"] != replacement["keyword"]]
        demoted.append(e)

    if not demoted:
        return 0

    table = _item_table(item_type)
    changed = 0
    for e in demoted:
        try:
            with db.get_cursor() as cur:
                cur.execute(f"SELECT * FROM {table} WHERE id = %s", (e["item_id"],))
                item = cur.fetchone()
            if not item:
                continue
            item = dict(item)
            if item_type == "collection":
                from app.routes.technical_seo_routes import _enrich_collection_data
                item = _enrich_collection_data(item)
                analysis = analyze_collection_technical_seo(item, primary_keyword=e["res"]["primary"]["keyword"])
            else:
                analysis = analyze_product_technical_seo(item, primary_keyword=e["res"]["primary"]["keyword"])
            analysis["detected_keywords"] = e["res"]["candidates"]
            with db.get_cursor() as cur:
                cur.execute(
                    """UPDATE technical_seo_analyses SET
                         score = %s, detected_keywords = %s, checks = %s,
                         issues_count = %s, fixable_count = %s,
                         keyword_resolution = %s, updated_at = NOW()
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (analysis["score"], json.dumps(analysis["detected_keywords"]),
                     json.dumps(analysis["checks"]), analysis["issues_count"],
                     analysis["fixable_count"], json.dumps(e["res"]),
                     store_id, item_type, e["item_id"]),
                )
            changed += 1
        except Exception:
            logger.warning("Ownership reassignment failed for %s %s", item_type, e["item_id"])
    return changed
