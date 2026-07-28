"""Store-wide SEO issue aggregation and fix-by-issue-type.

Two aggregation paths, deduplicated:
- "content" issues are computed live from synced_products / synced_collections
  using the shared seo_rules thresholds (canonical for the four checks both
  analyzers share).
- "keyword" issues come from stored technical_seo_analyses.checks, skipping
  the four shared check keys so nothing is counted twice.

Only ACTIVE products and published collections are scanned — hidden items
aren't crawlable, so their issues are noise.
"""

import json
import logging
import re

from flask import Blueprint, jsonify, request

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.seo_rules import score_length, score_description_words, score_image_alt
from app.services.task_runner import spawn as spawn_task
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

seo_issues_bp = Blueprint("seo_issues", __name__)

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


def _scan_store(store_id):
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

    def keyword_issues(row, item_type):
        checks = row.get("kw_checks")
        if not checks:
            return
        for key, check in checks.items():
            if key in CONTENT_DEDUP_CHECK_KEYS or key not in ISSUE_DEFS:
                continue
            if check.get("issue"):
                add(key, item_type, row, check.get("detail") or check["issue"])

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
        keyword_issues(p, "product")

    for c in collections:
        if c.get("kw_analyzed_at"):
            coverage["collections"]["keyword_analyzed"] += 1
        for key, detail in _live_issues_for_collection(c, collection_thresholds).items():
            add(key, "collection", c, detail)
        duplicate_issues(c, "collection")
        keyword_issues(c, "collection")

    return items_by_issue, coverage


@seo_issues_bp.route("/api/seo/issues", methods=["GET"])
def list_issues():
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    items_by_issue, coverage = _scan_store(store_id)

    issues = []
    for key, definition in ISSUE_DEFS.items():
        items = items_by_issue.get(key) or []
        if not items:
            continue
        by_type = {}
        for it in items:
            by_type[it["item_type"]] = by_type.get(it["item_type"], 0) + 1
        samples = sorted(items, key=lambda it: it["item_type"] != "product")[:5]
        issues.append({
            "key": key,
            "label": definition["label"],
            "severity": definition["severity"],
            "source": definition["source"],
            "fixable": definition["fixable"],
            "fix_fields": definition["fix_fields"],
            "action_label": definition.get("action_label"),
            "action_href": definition.get("action_href"),
            "count": len(items),
            "by_type": by_type,
            "sample_items": [
                {"item_type": s["item_type"], "id": s["id"], "title": s["title"], "handle": s["handle"]}
                for s in samples
            ],
        })

    issues.sort(key=lambda i: (SEVERITY_RANK[i["severity"]], -i["count"]))

    return jsonify({"store_id": int(store_id), "coverage": coverage, "issues": issues})


def _resolve_issue_items(issue_key, store_id, item_type=None):
    """Affected items for one issue, optionally restricted to an item_type."""
    items_by_issue, _ = _scan_store(store_id)
    items = items_by_issue.get(issue_key) or []
    if item_type:
        items = [it for it in items if it["item_type"] == item_type]
    return items


@seo_issues_bp.route("/api/seo/issues/<issue_key>/items", methods=["GET"])
def issue_items(issue_key):
    definition = ISSUE_DEFS.get(issue_key)
    if not definition:
        return jsonify({"error": f"Unknown issue: {issue_key}"}), 404
    store_id = request.args.get("store_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    item_type = request.args.get("item_type") or None
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)

    items = _resolve_issue_items(issue_key, store_id, item_type)

    return jsonify({
        "issue": {
            "key": issue_key,
            "label": definition["label"],
            "severity": definition["severity"],
            "fixable": definition["fixable"],
            "fix_fields": definition["fix_fields"],
            "action_label": definition.get("action_label"),
            "action_href": definition.get("action_href"),
        },
        "total": len(items),
        "items": items[offset:offset + limit],
    })


@seo_issues_bp.route("/api/seo/issues/<issue_key>/fix", methods=["POST"])
def fix_issue(issue_key):
    from app.routes.technical_seo_routes import _execute_bulk_fix_apply

    definition = ISSUE_DEFS.get(issue_key)
    if not definition:
        return jsonify({"error": f"Unknown issue: {issue_key}"}), 400
    if not definition["fixable"]:
        return jsonify({"error": f"'{definition['label']}' cannot be fixed automatically"}), 400

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    item_type = data.get("item_type")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400
    if item_type not in ("product", "collection"):
        return jsonify({"error": "item_type must be 'product' or 'collection'"}), 400

    limit = min(int(data.get("limit", 100)), 500)
    affected = _resolve_issue_items(issue_key, store_id, item_type)

    requested_ids = data.get("item_ids")
    if requested_ids:
        requested = {int(i) for i in requested_ids}
        affected = [it for it in affected if it["id"] in requested]

    affected = affected[:limit]
    if not affected:
        return jsonify({"error": "No affected items to fix"}), 400

    item_col = "product_id" if item_type == "product" else "collection_id"
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, TRUE, 'seo_issue_fix') RETURNING id",
            (len(affected),),
        )
        bulk_task_id = cur.fetchone()["id"]
        for it in affected:
            cur.execute(
                f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                (bulk_task_id, it["id"]),
            )

    spawn_task(
        "bulk_tasks", bulk_task_id, _execute_bulk_fix_apply,
        bulk_task_id, item_type, definition["fix_fields"],
    )

    return jsonify({
        "ok": True,
        "bulk_task_id": bulk_task_id,
        "total": len(affected),
        "issue_key": issue_key,
    }), 201


@seo_issues_bp.route("/api/seo/issues/analyze", methods=["POST"])
def analyze_unanalyzed():
    """Queue keyword-alignment analysis — one bulk task per item type.
    Default: only never-analyzed items. With {"force": true}: ALL visible
    items (used after scoring-threshold changes make stored analyses stale)."""
    from app.routes.technical_seo_routes import _execute_bulk_analyze

    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")
    force = bool(data.get("force"))
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

    tasks = []
    specs = [
        ("product", "synced_products", "product_id", "t.status = 'ACTIVE'"),
        ("collection", "synced_collections", "collection_id", "t.published = TRUE"),
    ]
    for item_type, table, item_col, visibility in specs:
        with db.get_cursor() as cur:
            if force:
                cur.execute(
                    f"SELECT t.id FROM {table} t WHERE t.store_id = %s AND {visibility} ORDER BY t.id",
                    (store_id,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT t.id FROM {table} t
                    LEFT JOIN technical_seo_analyses tsa
                      ON tsa.store_id = t.store_id AND tsa.item_type = %s AND tsa.item_id = t.id
                    WHERE t.store_id = %s AND {visibility}
                      AND tsa.id IS NULL
                    ORDER BY t.id
                    """,
                    (item_type, store_id),
                )
            ids = [r["id"] for r in cur.fetchall()]

        if not ids:
            continue

        with db.get_cursor() as cur:
            cur.execute(
                "INSERT INTO bulk_tasks (status, total, auto_apply, task_type) VALUES ('pending', %s, FALSE, 'tseo_analyze') RETURNING id",
                (len(ids),),
            )
            bulk_task_id = cur.fetchone()["id"]
            for iid in ids:
                cur.execute(
                    f"INSERT INTO bulk_task_items (bulk_task_id, {item_col}) VALUES (%s, %s)",
                    (bulk_task_id, iid),
                )

        spawn_task("bulk_tasks", bulk_task_id, _execute_bulk_analyze, bulk_task_id, item_type)
        tasks.append({"item_type": item_type, "bulk_task_id": bulk_task_id, "total": len(ids)})

    return jsonify({"tasks": tasks}), 201 if tasks else 200
