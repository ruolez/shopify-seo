"""Single source of truth for "what counts as an SEO issue".

Every surface that shows an issue count or list — the sidebar badge, the home
action queue, the Issues tab, and the per-item issue panel — derives from the
same scan so the numbers can never disagree.

Two aggregation paths, deduplicated:
- "content" issues are computed live from synced_products / synced_collections
  using the shared seo_rules thresholds (canonical for the four checks both
  analyzers share).
- "keyword" issues come from stored technical_seo_analyses.checks, skipping
  the four shared check keys so nothing is counted twice.
Plus store-wide categories: duplicate metas and schema state.

Only ACTIVE products and published collections are scanned — hidden items
aren't crawlable, so their issues are noise.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.duplicate_detector import normalize as dup_normalize
from app.services.seo_rules import score_length, score_description_words, score_image_alt
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"high": 0, "med": 1, "low": 2}

# An analysis older than this is treated as drifted even if the page never
# changed: keyword demand data and scoring thresholds move underneath it.
def _analysis_stale_days():
    """One staleness constant per concept: an analysis is stale when older
    than the resolution TTL (rules.keyword_resolution.resolution_max_age_days).
    This module used to say 30 days while the Keyword Strategy freshness strip
    said 7 — the same store read "all fresh" on one page and "stale" on the
    other. The TTL sweeper keeps rows inside this window, so the count here
    stays near zero in steady state."""
    try:
        from app.prompt_defaults import DEFAULTS
        from app.settings_manager import get_setting
        rules = get_setting("rules.keyword_resolution", DEFAULTS["rules.keyword_resolution"])
        return int(rules.get("resolution_max_age_days", 7))
    except Exception:
        return 7

# Check keys shared between the analyzers; the live/content path is canonical
# for these, so the analysis path skips them.
CONTENT_DEDUP_CHECK_KEYS = {
    "meta_title_length",
    "meta_desc_length",
    "description_word_count",
    "image_alt_coverage",
}

# key -> {label, severity, source, fixable, fix_fields, checks, item_types}
# Optional action_label/action_href: non-AI-fixable issues that have a
# dedicated fixing surface elsewhere in the app link there instead.
#
# "fix_fields" is the WRITE scope — which Shopify fields an AI fix may touch.
# "checks" is the READ scope — which analyzer check keys the AI is shown as
# failing. Several issues share one field (five different issues all rewrite
# the description), so field scope alone made "Fix all with AI" silently fix
# every issue on that field; "checks" narrows it to what the button claims.
# Keyword-source issue keys ARE analyzer check keys; content-source issues map
# onto the four shared checks in CONTENT_DEDUP_CHECK_KEYS. Two issues sharing a
# check (missing vs. thin description) is correct — they are one check at
# different severities.
ISSUE_DEFS = {
    "meta_title_duplicate": {
        "label": "Duplicate meta title",
        "severity": "high", "source": "content", "fixable": False,
        "fix_fields": [], "checks": [], "item_types": ("product", "collection"),
        "action_label": "Fix in Duplicates", "action_href": "/duplicates",
    },
    "meta_description_duplicate": {
        "label": "Duplicate meta description",
        "severity": "high", "source": "content", "fixable": False,
        "fix_fields": [], "checks": [], "item_types": ("product", "collection"),
        "action_label": "Fix in Duplicates", "action_href": "/duplicates",
    },
    "meta_description_missing": {
        "label": "Missing meta description",
        "severity": "high", "source": "content", "fixable": True,
        "fix_fields": ["meta_description"], "checks": ["meta_desc_length"],
        "item_types": ("product", "collection"),
    },
    "description_missing": {
        "label": "Missing description",
        "severity": "high", "source": "content", "fixable": True,
        "fix_fields": ["description"], "checks": ["description_word_count"],
        "item_types": ("product", "collection"),
    },
    "title_has_keyword": {
        "label": "Keyword missing from title",
        "severity": "high", "source": "keyword", "fixable": True,
        "fix_fields": ["title"], "checks": ["title_has_keyword"],
        "item_types": ("product", "collection"),
    },
    # Length bands are truncation-risk heuristics, not Google rules — there is
    # no official character limit, snippets truncate by pixel width, and metas
    # are rewritten by Google ~2/3 of the time. Label them honestly.
    "meta_description_length": {
        "label": "Meta description may truncate in results",
        "severity": "low", "source": "content", "fixable": True,
        "fix_fields": ["meta_description"], "checks": ["meta_desc_length"],
        "item_types": ("product", "collection"),
    },
    "meta_title_length": {
        "label": "Meta title may truncate in results",
        "severity": "low", "source": "content", "fixable": True,
        "fix_fields": ["meta_title"], "checks": ["meta_title_length"],
        "item_types": ("product", "collection"),
    },
    "description_thin": {
        "label": "Thin description",
        "severity": "med", "source": "content", "fixable": True,
        "fix_fields": ["description"], "checks": ["description_word_count"],
        "item_types": ("product", "collection"),
    },
    "image_alt_missing": {
        "label": "Images missing alt text",
        "severity": "med", "source": "content", "fixable": True,
        "fix_fields": ["image_alt"], "checks": ["image_alt_coverage"],
        "item_types": ("product",),
    },
    "schema_missing": {
        "label": "Schema enrichment not pushed",
        "severity": "med", "source": "content", "fixable": False,
        "fix_fields": [], "checks": [], "item_types": ("product",),
        "action_label": "Fix in Schema", "action_href": "/seo?tab=schema",
    },
    "schema_stale": {
        "label": "Schema missing product image",
        "severity": "med", "source": "content", "fixable": False,
        "fix_fields": [], "checks": [], "item_types": ("product",),
        "action_label": "Fix in Images", "action_href": "/images",
    },
    "meta_title_has_keyword": {
        "label": "Keyword missing from meta title",
        "severity": "med", "source": "keyword", "fixable": True,
        "fix_fields": ["meta_title"], "checks": ["meta_title_has_keyword"],
        "item_types": ("product", "collection"),
    },
    "meta_desc_has_keyword": {
        "label": "Keyword missing from meta description",
        "severity": "med", "source": "keyword", "fixable": True,
        "fix_fields": ["meta_description"], "checks": ["meta_desc_has_keyword"],
        "item_types": ("product", "collection"),
    },
    "body_first_100_has_keyword": {
        "label": "Keyword not in first 100 words",
        "severity": "med", "source": "keyword", "fixable": True,
        "fix_fields": ["description"], "checks": ["body_first_100_has_keyword"],
        "item_types": ("product", "collection"),
    },
    # keyword_at_title_start is deliberately absent (like url_has_keyword):
    # Google has no keyword-position rule — the check survives in the item
    # modal as a truncation-visibility note, but an issue row with a bulk-fix
    # button invites mechanically front-loaded, boilerplate-shaped titles,
    # the exact pattern Google rewrites.
    "keyword_density": {
        "label": "Keyword stuffing risk",
        "severity": "low", "source": "keyword", "fixable": True,
        "fix_fields": ["description"], "checks": ["keyword_density"],
        "item_types": ("product", "collection"),
    },
    # source is presentational (UI pillar tag); routing to the stored-checks
    # path is by ISSUE_DEFS membership. Headings take no keyword input —
    # tagging them "Keywords" inflated the keyword-issues narrative.
    "heading_structure": {
        "label": "No subheadings in description",
        "severity": "low", "source": "content", "fixable": True,
        "fix_fields": ["description"], "checks": ["heading_structure"],
        "item_types": ("product", "collection"),
    },
    "image_alt_has_keyword": {
        "label": "Alt text keyword balance",
        "severity": "low", "source": "keyword", "fixable": True,
        "fix_fields": ["image_alt"], "checks": ["image_alt_has_keyword"],
        "item_types": ("product",),
    },
    # url_has_keyword is deliberately absent: changing handles needs redirects,
    # so the check is informational-only — it stays in the item modal's Checks
    # tab but would be pure noise in the store-wide issue list.
    "collection_image_missing": {
        "label": "No collection image",
        "severity": "low", "source": "content", "fixable": False,
        "fix_fields": [], "checks": [], "item_types": ("collection",),
        "action_label": "Fix in Collections",
        "action_href": "/seo?tab=collections&image=missing",
    },
    # The hero is attached to the Shopify collection object, but themes vary in
    # whether they render it. When they don't, the image only ever reaches
    # og:image/JSON-LD and the page itself shows no visual. Putting it in the
    # description body makes it render regardless of the theme.
    "collection_hero_not_in_body": {
        "label": "Hero image not shown in description",
        "severity": "med", "source": "content", "fixable": False,
        "fix_fields": [], "checks": [], "item_types": ("collection",),
        "bulk_fix": {
            "label": "Insert hero images",
            "endpoint": "/api/seo/collections/inject-hero-images",
            "confirm": "Insert each collection's existing hero image at the top of its "
                       "description and push to Shopify. No new images are generated or "
                       "uploaded — this only places the image you already have.",
        },
    },
}

# Checks that ride along whenever their field is being rewritten anyway. Not
# an issue type (deliberately weightless) and not an optimization — an <h1> in
# the description duplicates the page H1, and the fix prompt already forbids
# emitting one, so a scoped description rewrite must not leave an existing one
# behind. fix_field -> companion check keys.
FIX_GUARD_CHECKS = {
    "description": ["description_no_h1"],
}


def fix_scope(issue_key):
    """(fix_fields, check_keys) an AI fix for this issue may touch.

    check_keys is the issue's OWN checks — guard checks are added later, by the
    prompt builder, so that "is anything still broken?" tests stay honest.
    Returns (None, None) for issues with no AI fix; callers must treat a falsy
    scope as "not fixable", never as "unrestricted"."""
    definition = ISSUE_DEFS.get(issue_key)
    if not definition or not definition.get("fix_fields"):
        return None, None
    return list(definition["fix_fields"]), list(definition.get("checks") or [])

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_TAG_RE = re.compile(r"<img[\s>]", re.IGNORECASE)


def _word_count(html):
    return len(_TAG_RE.sub(" ", html or "").split())


def _live_issues_for_product(p, thresholds):
    """Map of issue_key -> detail for one product row (content checks only).

    Length/word-count checks flag only below the 'acceptable' band — an
    acceptable-but-not-ideal length is a polish opportunity, not an issue."""
    issues = {}

    md = p.get("meta_description") or ""
    if not md:
        issues["meta_description_missing"] = "No meta description set"
    else:
        score, detail = score_length(len(md), thresholds["meta_description"], "meta description")
        if score < 60:
            issues["meta_description_length"] = detail

    effective_title = p.get("meta_title") or p.get("title") or ""
    score, detail = score_length(len(effective_title), thresholds["meta_title"], "meta title")
    if score < 60:
        issues["meta_title_length"] = detail

    words = _word_count(p.get("description"))
    if words == 0:
        issues["description_missing"] = "No description"
    else:
        score, detail = score_description_words(words, thresholds["description_words"], "product")
        if score < 70:
            issues["description_thin"] = detail

    images = p.get("images") or []
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except (ValueError, TypeError):
            images = []
    alt_score, alt_detail, _, _ = score_image_alt(images)
    if alt_score < 100:
        issues["image_alt_missing"] = alt_detail

    return issues


def _live_issues_for_collection(c, thresholds):
    issues = {}

    md = c.get("meta_description") or ""
    if not md:
        issues["meta_description_missing"] = "No meta description set"
    else:
        score, detail = score_length(len(md), thresholds["meta_description"], "meta description")
        if score < 60:
            issues["meta_description_length"] = detail

    effective_title = c.get("meta_title") or c.get("title") or ""
    score, detail = score_length(len(effective_title), thresholds["meta_title"], "meta title")
    if score < 60:
        issues["meta_title_length"] = detail

    words = _word_count(c.get("description"))
    if words == 0:
        issues["description_missing"] = "No description"
    else:
        score, detail = score_description_words(words, thresholds["description_words"], "collection")
        if score < 70:
            issues["description_thin"] = detail

    if not c.get("image_url"):
        issues["collection_image_missing"] = "No collection image set"
    elif not _IMG_TAG_RE.search(c.get("description") or ""):
        issues["collection_hero_not_in_body"] = (
            "Hero image is set on the collection but the description has no <img> — "
            "themes that skip collection.image render no visual at all"
        )

    return issues


def keyword_issue_map(checks):
    """issue_key -> detail from stored keyword-analysis checks, filtered to
    the ISSUE_DEFS whitelist minus the content-dedup overlap."""
    out = {}
    for key, check in (checks or {}).items():
        if key in CONTENT_DEDUP_CHECK_KEYS or key not in ISSUE_DEFS:
            continue
        if check.get("issue"):
            out[key] = check.get("detail") or check["issue"]
    return out


# store_id -> {"ts": time, "data": (items_by_issue, coverage)}. Write-through:
# every fresh scan refreshes it, so the badge converges right after the Issues
# tab (which always scans fresh) is viewed.
_scan_cache = {}


def invalidate_scan_cache(store_id):
    """Drop the in-process scan cache for a store — called when an apply
    refreshes an analysis, so just-fixed items don't keep their issues (and
    stale fixable counts don't feed the measuring-window decision) for up to
    5 more minutes."""
    _scan_cache.pop(int(store_id), None)


def scan_store_issues(store_id):
    """One pass over the store: returns (items_by_issue, coverage).

    items_by_issue: issue_key -> list of item dicts
      {item_type, id, store_id, shopify_id, title, handle, status,
       detail, keyword_score, analyzed_at}
    """
    product_thresholds = get_setting("rules.product_seo", DEFAULTS["rules.product_seo"])["thresholds"]
    collection_thresholds = get_setting("rules.collection_seo", DEFAULTS["rules.collection_seo"])["thresholds"]

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sp.id, sp.store_id, sp.shopify_id, sp.title, sp.handle, sp.status,
                   sp.meta_title, sp.meta_description, sp.description, sp.images,
                   sp.shopify_updated_at, sp.last_optimized_at,
                   tsa.score AS kw_score, tsa.checks AS kw_checks, tsa.updated_at AS kw_analyzed_at
            FROM synced_products sp
            LEFT JOIN technical_seo_analyses tsa
              ON tsa.store_id = sp.store_id AND tsa.item_type = 'product' AND tsa.item_id = sp.id
            WHERE sp.store_id = %s AND sp.status = 'ACTIVE'
            ORDER BY sp.title
            """,
            (store_id,),
        )
        products = cur.fetchall()

        cur.execute(
            """
            SELECT sc.id, sc.store_id, sc.shopify_id, sc.title, sc.handle,
                   sc.meta_title, sc.meta_description, sc.description, sc.image_url,
                   sc.shopify_updated_at, sc.last_optimized_at,
                   tsa.score AS kw_score, tsa.checks AS kw_checks, tsa.updated_at AS kw_analyzed_at
            FROM synced_collections sc
            LEFT JOIN technical_seo_analyses tsa
              ON tsa.store_id = sc.store_id AND tsa.item_type = 'collection' AND tsa.item_id = sc.id
            WHERE sc.store_id = %s AND sc.published = TRUE
            ORDER BY sc.title
            """,
            (store_id,),
        )
        collections = cur.fetchall()

        cur.execute(
            """
            SELECT resource_id,
                   (schema_json::jsonb ? 'image') AS has_image,
                   (schema_json::jsonb ? 'schema_version') AS is_v2
            FROM schema_markup
            WHERE store_id = %s AND resource_type = 'product'
              AND schema_type = 'Product' AND pushed_to_shopify = TRUE
            """,
            (store_id,),
        )
        _schema_rows = cur.fetchall()
        schema_pushed = {str(r["resource_id"]) for r in _schema_rows}
        # v2 (Liquid-native) schema pulls images live from the storefront — the
        # slim enrichment payload never carries one, so only a product with no
        # images at all fails Google's required-image check. Legacy full-JSON
        # rows keep the stored-payload test.
        schema_v2 = {str(r["resource_id"]) for r in _schema_rows if r["is_v2"]}
        schema_imageless_legacy = {
            str(r["resource_id"]) for r in _schema_rows if not r["is_v2"] and not r["has_image"]
        }

    # Duplicate metas: group effective titles / non-empty descriptions across
    # both entity types in the rows already fetched (no extra queries).
    # Same normalization as /duplicates (duplicate_detector.normalize) so the
    # two surfaces agree on what's a duplicate. Coverage still differs by
    # design: /duplicates also scans articles and pages.
    title_groups = {}
    desc_groups = {}
    for row in list(products) + list(collections):
        t = dup_normalize(row.get("meta_title") or row.get("title") or "")
        if t:
            title_groups.setdefault(t, []).append(row)
        d = dup_normalize(row.get("meta_description") or "")
        if d:
            desc_groups.setdefault(d, []).append(row)
    dup_titles = {k: len(v) for k, v in title_groups.items() if len(v) >= 2}
    dup_descs = {k: len(v) for k, v in desc_groups.items() if len(v) >= 2}

    items_by_issue = {}
    # "Analyzed" is not the same as "analyzed against what the page says now".
    # Keyword issues are read from the STORED checks, so a row from months ago
    # is reported as gospel — the tile read 100% while most of the catalog was
    # still on a five-month-old verdict. Two extra buckets keep that visible:
    #   outdated — content changed after the analysis; the verdict is wrong now
    #   stale    — no content change, but old enough that keyword data has moved
    coverage = {
        "products": {"total": len(products), "keyword_analyzed": 0, "outdated": 0, "stale": 0},
        "collections": {"total": len(collections), "keyword_analyzed": 0, "outdated": 0, "stale": 0},
        "stale_days": _analysis_stale_days(),
        "oldest_analyzed_at": None,
    }
    _stale_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=_analysis_stale_days())

    def record_freshness(row, bucket):
        analyzed_at = row.get("kw_analyzed_at")
        if not analyzed_at:
            return
        if analyzed_at.tzinfo:
            analyzed_at = analyzed_at.replace(tzinfo=None)
        bucket["keyword_analyzed"] += 1
        oldest = coverage["oldest_analyzed_at"]
        if oldest is None or analyzed_at < oldest:
            coverage["oldest_analyzed_at"] = analyzed_at
        changed_at = max(
            (t for t in (row.get("shopify_updated_at"), row.get("last_optimized_at")) if t),
            default=None,
        )
        if changed_at and changed_at.tzinfo:
            changed_at = changed_at.replace(tzinfo=None)
        if changed_at and analyzed_at < changed_at:
            bucket["outdated"] += 1
        elif analyzed_at < _stale_cutoff:
            bucket["stale"] += 1

    def add(issue_key, item_type, row, detail):
        items_by_issue.setdefault(issue_key, []).append({
            "item_type": item_type,
            "id": row["id"],
            "store_id": row["store_id"],
            "shopify_id": row["shopify_id"],
            "title": row["title"],
            "handle": row["handle"],
            "status": row.get("status"),
            "detail": detail,
            "keyword_score": row.get("kw_score"),
            "analyzed_at": row["kw_analyzed_at"].isoformat() if row.get("kw_analyzed_at") else None,
            # Current values for the Issues tab's inline quick-edit prefill.
            "meta_title": row.get("meta_title"),
            "meta_description": row.get("meta_description"),
        })

    def duplicate_issues(row, item_type):
        t = dup_normalize(row.get("meta_title") or row.get("title") or "")
        if t in dup_titles:
            add("meta_title_duplicate", item_type, row,
                f"Shared with {dup_titles[t] - 1} other item(s): “{t[:60]}”")
        d = dup_normalize(row.get("meta_description") or "")
        if d in dup_descs:
            add("meta_description_duplicate", item_type, row,
                f"Shared with {dup_descs[d] - 1} other item(s): “{d[:60]}”")

    for p in products:
        record_freshness(p, coverage["products"])
        for key, detail in _live_issues_for_product(p, product_thresholds).items():
            add(key, "product", p, detail)
        duplicate_issues(p, "product")
        sid = str(p["shopify_id"])
        if sid not in schema_pushed:
            add("schema_missing", "product", p, "Spec enrichment metafield not generated/pushed — base schema still renders live via the theme snippet")
        else:
            imgs = p.get("images")
            if isinstance(imgs, str):
                try:
                    imgs = json.loads(imgs)
                except (ValueError, TypeError):
                    imgs = []
            if sid in schema_v2:
                if not imgs:
                    add("schema_stale", "product", p,
                        "Product has no images — the live schema fails Google's required-image "
                        "check; add a product image")
            elif sid in schema_imageless_legacy:
                add("schema_stale", "product", p,
                    "Pushed schema has no image field — regenerate now that the product has images"
                    if imgs else
                    "Pushed schema has no image field — add a product image, then regenerate")
        for key, detail in keyword_issue_map(p.get("kw_checks")).items():
            add(key, "product", p, detail)

    for c in collections:
        record_freshness(c, coverage["collections"])
        for key, detail in _live_issues_for_collection(c, collection_thresholds).items():
            add(key, "collection", c, detail)
        duplicate_issues(c, "collection")
        for key, detail in keyword_issue_map(c.get("kw_checks")).items():
            add(key, "collection", c, detail)

    # Serialize explicitly: jsonify would emit RFC-822 ("Sun, 09 Aug 2026 …"),
    # which new Date() in the browser reads as Invalid Date. The column is
    # timestamp-without-tz holding UTC, so mark it as UTC rather than letting
    # the browser parse it as local time.
    if coverage["oldest_analyzed_at"]:
        coverage["oldest_analyzed_at"] = coverage["oldest_analyzed_at"].isoformat() + "Z"

    _scan_cache[int(store_id)] = {"ts": time.time(), "data": (items_by_issue, coverage)}
    return items_by_issue, coverage


def _cached_scan(store_id, max_age):
    entry = _scan_cache.get(int(store_id))
    if entry and time.time() - entry["ts"] < max_age:
        return entry["data"]
    return scan_store_issues(store_id)


def count_store_issues(store_id, max_age=300):
    """Store-level totals for the sidebar badge and home action queue —
    derived from the same scan as the Issues tab so they always agree."""
    items_by_issue, _ = _cached_scan(store_id, max_age)
    seen = set()
    total = 0
    fixable = 0
    for key, items in items_by_issue.items():
        total += len(items)
        if ISSUE_DEFS[key]["fixable"]:
            fixable += len(items)
        for it in items:
            seen.add((it["item_type"], it["id"]))
    return {
        "total_issues": total,
        "items_with_issues": len(seen),
        "fixable_issues": fixable,
    }


def fixable_issue_counts(store_id, max_age=300):
    """(item_type, id) -> number of outstanding AI-fixable issues.

    Used by the priority queue to tell "recently optimized, nothing left to do"
    apart from "recently optimized, but still broken"."""
    items_by_issue, _ = _cached_scan(store_id, max_age)
    counts = {}
    for key, items in items_by_issue.items():
        if not ISSUE_DEFS[key]["fixable"]:
            continue
        for it in items:
            k = (it["item_type"], it["id"])
            counts[k] = counts.get(k, 0) + 1
    return counts


def item_issues(store_id, item_type, item_id, max_age=60):
    """The one item's issue list — exactly the entries the Issues tab holds
    for it, including the store-wide duplicate/schema categories."""
    items_by_issue, _ = _cached_scan(store_id, max_age)
    item_id = int(item_id)
    out = []
    for key, items in items_by_issue.items():
        for it in items:
            if it["item_type"] == item_type and it["id"] == item_id:
                definition = ISSUE_DEFS[key]
                out.append({
                    "key": key,
                    "label": definition["label"],
                    "severity": definition["severity"],
                    "fixable": definition["fixable"],
                    "fix_fields": definition["fix_fields"],
                    "action_label": definition.get("action_label"),
                    "action_href": definition.get("action_href"),
                    "detail": it["detail"],
                })
                break
    out.sort(key=lambda i: SEVERITY_RANK[i["severity"]])
    return out
