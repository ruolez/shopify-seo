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

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.seo_rules import score_length, score_description_words, score_image_alt
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"high": 0, "med": 1, "low": 2}

# Check keys shared between the analyzers; the live/content path is canonical
# for these, so the analysis path skips them.
CONTENT_DEDUP_CHECK_KEYS = {
    "meta_title_length",
    "meta_desc_length",
    "description_word_count",
    "image_alt_coverage",
}

# key -> {label, severity, source, fixable, fix_fields, item_types}
# Optional action_label/action_href: non-AI-fixable issues that have a
# dedicated fixing surface elsewhere in the app link there instead.
ISSUE_DEFS = {
    "meta_title_duplicate": {
        "label": "Duplicate meta title",
        "severity": "high", "source": "content", "fixable": False,
        "fix_fields": [], "item_types": ("product", "collection"),
        "action_label": "Fix in Duplicates", "action_href": "/duplicates",
    },
    "meta_description_duplicate": {
        "label": "Duplicate meta description",
        "severity": "high", "source": "content", "fixable": False,
        "fix_fields": [], "item_types": ("product", "collection"),
        "action_label": "Fix in Duplicates", "action_href": "/duplicates",
    },
    "meta_description_missing": {
        "label": "Missing meta description",
        "severity": "high", "source": "content", "fixable": True,
        "fix_fields": ["meta_description"], "item_types": ("product", "collection"),
    },
    "description_missing": {
        "label": "Missing description",
        "severity": "high", "source": "content", "fixable": True,
        "fix_fields": ["description"], "item_types": ("product", "collection"),
    },
    "title_has_keyword": {
        "label": "Keyword missing from title",
        "severity": "high", "source": "keyword", "fixable": True,
        "fix_fields": ["title"], "item_types": ("product", "collection"),
    },
    "meta_description_length": {
        "label": "Meta description wrong length",
        "severity": "med", "source": "content", "fixable": True,
        "fix_fields": ["meta_description"], "item_types": ("product", "collection"),
    },
    "meta_title_length": {
        "label": "Meta title wrong length",
        "severity": "med", "source": "content", "fixable": True,
        "fix_fields": ["meta_title"], "item_types": ("product", "collection"),
    },
    "description_thin": {
        "label": "Thin description",
        "severity": "med", "source": "content", "fixable": True,
        "fix_fields": ["description"], "item_types": ("product", "collection"),
    },
    "image_alt_missing": {
        "label": "Images missing alt text",
        "severity": "med", "source": "content", "fixable": True,
        "fix_fields": ["image_alt"], "item_types": ("product",),
    },
    "schema_missing": {
        "label": "Schema enrichment not pushed",
        "severity": "med", "source": "content", "fixable": False,
        "fix_fields": [], "item_types": ("product",),
        "action_label": "Fix in Schema", "action_href": "/seo?tab=schema",
    },
    "schema_stale": {
        "label": "Schema missing product image",
        "severity": "med", "source": "content", "fixable": False,
        "fix_fields": [], "item_types": ("product",),
        "action_label": "Fix in Images", "action_href": "/images",
    },
    "meta_title_has_keyword": {
        "label": "Keyword missing from meta title",
        "severity": "med", "source": "keyword", "fixable": True,
        "fix_fields": ["meta_title"], "item_types": ("product", "collection"),
    },
    "meta_desc_has_keyword": {
        "label": "Keyword missing from meta description",
        "severity": "med", "source": "keyword", "fixable": True,
        "fix_fields": ["meta_description"], "item_types": ("product", "collection"),
    },
    "body_first_100_has_keyword": {
        "label": "Keyword not in first 100 words",
        "severity": "med", "source": "keyword", "fixable": True,
        "fix_fields": ["description"], "item_types": ("product", "collection"),
    },
    "keyword_at_title_start": {
        "label": "Keyword not at start of title",
        "severity": "low", "source": "keyword", "fixable": True,
        "fix_fields": ["title"], "item_types": ("product", "collection"),
    },
    "keyword_density": {
        "label": "Keyword stuffing risk",
        "severity": "low", "source": "keyword", "fixable": True,
        "fix_fields": ["description"], "item_types": ("product", "collection"),
    },
    "heading_structure": {
        "label": "No subheadings in description",
        "severity": "low", "source": "keyword", "fixable": True,
        "fix_fields": ["description"], "item_types": ("product", "collection"),
    },
    "image_alt_has_keyword": {
        "label": "Alt text keyword balance",
        "severity": "low", "source": "keyword", "fixable": True,
        "fix_fields": ["image_alt"], "item_types": ("product",),
    },
    # url_has_keyword is deliberately absent: changing handles needs redirects,
    # so the check is informational-only — it stays in the item modal's Checks
    # tab but would be pure noise in the store-wide issue list.
    "collection_image_missing": {
        "label": "No collection image",
        "severity": "low", "source": "content", "fixable": False,
        "fix_fields": [], "item_types": ("collection",),
    },
}

_TAG_RE = re.compile(r"<[^>]+>")


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
    title_groups = {}
    desc_groups = {}
    for row in list(products) + list(collections):
        t = ((row.get("meta_title") or row.get("title") or "").strip().lower())
        if t:
            title_groups.setdefault(t, []).append(row)
        d = (row.get("meta_description") or "").strip().lower()
        if d:
            desc_groups.setdefault(d, []).append(row)
    dup_titles = {k: len(v) for k, v in title_groups.items() if len(v) >= 2}
    dup_descs = {k: len(v) for k, v in desc_groups.items() if len(v) >= 2}

    items_by_issue = {}
    coverage = {
        "products": {"total": len(products), "keyword_analyzed": 0},
        "collections": {"total": len(collections), "keyword_analyzed": 0},
    }

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
        })

    def duplicate_issues(row, item_type):
        t = ((row.get("meta_title") or row.get("title") or "").strip().lower())
        if t in dup_titles:
            add("meta_title_duplicate", item_type, row,
                f"Shared with {dup_titles[t] - 1} other item(s): “{t[:60]}”")
        d = (row.get("meta_description") or "").strip().lower()
        if d in dup_descs:
            add("meta_description_duplicate", item_type, row,
                f"Shared with {dup_descs[d] - 1} other item(s): “{d[:60]}”")

    for p in products:
        if p.get("kw_analyzed_at"):
            coverage["products"]["keyword_analyzed"] += 1
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
        if c.get("kw_analyzed_at"):
            coverage["collections"]["keyword_analyzed"] += 1
        for key, detail in _live_issues_for_collection(c, collection_thresholds).items():
            add(key, "collection", c, detail)
        duplicate_issues(c, "collection")
        for key, detail in keyword_issue_map(c.get("kw_checks")).items():
            add(key, "collection", c, detail)

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
