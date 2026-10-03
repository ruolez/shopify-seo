"""The one way content reaches Shopify.

Every flow that changes a page's title, body or meta tags calls
apply_item_changes. It reads the live page first, refuses to overwrite edits
the app has not seen, refuses rewrites that drop protected content, pushes,
mirrors exactly what Shopify stored, re-scores, and records a full-value
history row that revert_change can undo.

Callers pass canonical field names — title, body, meta_title,
meta_description, summary — for every item type; the mapping to each Shopify
object and mirror table lives here.
"""

import html
import json
import logging
import re

from app.database import db
from app.services.ai_output_guard import (
    guard_shopify_fields,
    restore_missing_figures,
    restore_missing_images,
)
from app.services.content_preservation import missing_preserved

logger = logging.getLogger(__name__)

CONTENT_FIELDS = ("title", "body", "meta_title", "meta_description", "summary")

ITEM_CONFIG = {
    "product": {
        "table": "synced_products",
        "columns": {"title": "title", "body": "description", "meta_title": "meta_title",
                    "meta_description": "meta_description"},
        "shopify": {"body": "description_html"},
        "stamp": True,
    },
    "collection": {
        "table": "synced_collections",
        "columns": {"title": "title", "body": "description", "meta_title": "meta_title",
                    "meta_description": "meta_description"},
        "shopify": {"body": "description_html"},
        "stamp": True,
    },
    "article": {
        "table": "synced_articles",
        "columns": {"title": "title", "body": "body", "meta_title": "meta_title",
                    "meta_description": "meta_description", "summary": "summary"},
        "shopify": {},
        "stamp": True,
    },
    "page": {
        "table": "synced_pages",
        "columns": {"title": "title", "body": "body_html", "meta_title": "seo_title",
                    "meta_description": "seo_description"},
        "shopify": {"meta_title": "seo_title", "meta_description": "seo_description"},
        "stamp": False,
    },
}

# Legacy key the history readers expect for a body change.
_LEGACY_EVENT_KEYS = {"body": "description_html"}

_TAG_RE = re.compile(r"<[^>]+>")


def _norm(value):
    """Comparison form. Shopify re-serialises stored HTML (entities decoded or
    re-encoded, self-closing slashes dropped), so equal content must compare
    equal across those spellings."""
    text = html.unescape(value or "")
    text = re.sub(r"\s*/>", ">", text)
    return re.sub(r">\s+<", "><", re.sub(r"\s+", " ", text)).strip()


def shopify_fields(item_type, fields):
    mapping = ITEM_CONFIG[item_type]["shopify"]
    return {mapping.get(k, k): v for k, v in fields.items()}


def _visible_text(content):
    return " ".join([
        content.get("title") or "",
        _TAG_RE.sub(" ", content.get("body") or ""),
        content.get("meta_title") or "",
        content.get("meta_description") or "",
    ])


def plan_apply(mirror, live, fields, *, allow_dropped_blocks=False, primary_keyword=None):
    """Decide what, if anything, may be pushed. Pure.

    mirror: what the app last saw; live: what Shopify holds now; fields: the
    requested values. Returns {status: ok|noop|stale|blocked, ...}.
    """
    requested = {k: v for k, v in fields.items() if k in CONTENT_FIELDS and v is not None}

    stale = [k for k in requested if _norm(live.get(k)) != _norm(mirror.get(k))]
    if stale:
        return {"status": "stale", "fields": stale}

    guard_shopify_fields(requested)
    if "body" in requested:
        body = restore_missing_images(live.get("body"), requested["body"])
        body = restore_missing_figures(live.get("body"), body)
        requested["body"] = body
        if not allow_dropped_blocks:
            missing = missing_preserved(live.get("body"), body)
            if missing:
                return {"status": "blocked", "code": "dropped_blocks", "missing": missing}

    pushed = {k: v for k, v in requested.items() if _norm(v) != _norm(live.get(k))}
    if not pushed:
        return {"status": "noop"}

    if primary_keyword:
        from app.services.technical_seo_analyzer import _keyword_in_text

        if (_keyword_in_text(primary_keyword, _visible_text(live))
                and not _keyword_in_text(primary_keyword, _visible_text({**live, **pushed}))):
            return {"status": "blocked", "code": "dropped_primary", "missing": [primary_keyword]}

    return {"status": "ok", "pushed": pushed, "old": {k: live.get(k) or "" for k in pushed}}


def can_revert(new_values, live):
    """Fields the page no longer holds as this change wrote them. Empty = safe to undo."""
    return [k for k, v in new_values.items() if _norm(live.get(k)) != _norm(v)]


def blocked_message(plan):
    if plan.get("code") == "dropped_primary":
        return (f'This change removes the page\'s primary keyword "{plan["missing"][0]}". '
                "Regenerate it, or apply anyway.")
    return ("This rewrite would remove content other tools added: "
            + ", ".join(plan["missing"]) + ". Regenerate it, or apply anyway.")


STALE_MESSAGE = (
    "This page changed in Shopify after the suggestion was made. "
    "The app has picked up the current version — generate a fresh suggestion."
)


class ApplyRefused(Exception):
    """A plan that must not be pushed. status/code/payload mirror plan_apply."""

    def __init__(self, plan, message):
        super().__init__(message)
        self.plan = plan
        self.status = plan["status"]
        self.code = plan.get("code") or plan["status"]


def _client_for(store_id):
    from app.auth import decrypt_value
    from app.shopify_client import ShopifyClient

    with db.get_cursor() as cur:
        cur.execute("SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        raise ValueError("Store not found")
    return ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])


def _load_mirror(item_type, item_id):
    cfg = ITEM_CONFIG[item_type]
    with db.get_cursor() as cur:
        cur.execute(f"SELECT * FROM {cfg['table']} WHERE id = %s", (item_id,))
        row = cur.fetchone()
    if not row:
        raise ValueError(f"{item_type} not found")
    return row, {k: row.get(col) or "" for k, col in cfg["columns"].items()}


def _write_mirror(item_type, item_id, values, stamp):
    cfg = ITEM_CONFIG[item_type]
    sets = [f"{cfg['columns'][k]} = %s" for k in values]
    params = list(values.values())
    if stamp and cfg["stamp"]:
        sets.append("last_optimized_at = NOW()")
    if not sets:
        return
    with db.get_cursor() as cur:
        cur.execute(f"UPDATE {cfg['table']} SET {', '.join(sets)} WHERE id = %s", (*params, item_id))


def _resolved_primary(store_id, item_type, item_id):
    if item_type not in ("product", "collection"):
        return None
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                   FROM technical_seo_analyses
                   WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                (store_id, item_type, item_id),
            )
            row = cur.fetchone()
        return (row or {}).get("kw")
    except Exception:
        logger.warning("Primary lookup failed for %s %s", item_type, item_id)
        return None


def _record_event(store_id, item_type, item_id, source, change_type, old, new, scores_before):
    legacy = {
        _LEGACY_EVENT_KEYS.get(k, k): {"old": (old.get(k) or "")[:200], "new": (new.get(k) or "")[:200]}
        for k in new
    }
    scores = scores_before or {}
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO seo_change_events
                 (store_id, item_type, item_id, change_type, source, changes_applied,
                  old_values, new_values, seo_score_before, search_score_before, composite_score_before)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (store_id, item_type, item_id, change_type, source, json.dumps(legacy),
             json.dumps(old), json.dumps(new),
             scores.get("seo"), scores.get("search"), scores.get("composite")),
        )
        return cur.fetchone()["id"]


def refusal_response(refused):
    """Flask response for a refused apply. 409 + code lets the UI offer
    "apply anyway" for blocked plans and a regenerate hint for stale ones."""
    from flask import jsonify

    return jsonify({
        "error": str(refused),
        "code": refused.code,
        "missing": refused.plan.get("missing") or [],
        "can_force": refused.status == "blocked",
    }), 409


def record_side_change(store_id, item_type, item_id, source, change_type, changed, scores_before=None):
    """History row for a change the pipeline does not carry (tags, image alt
    text). Shows in impact tracking; not undoable."""
    scores = scores_before or {}
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO seo_change_events
                 (store_id, item_type, item_id, change_type, source, changes_applied,
                  seo_score_before, search_score_before, composite_score_before)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (store_id, item_type, item_id, change_type, source,
             json.dumps({name: {"new": "updated"} for name in changed}),
             scores.get("seo"), scores.get("search"), scores.get("composite")),
        )
        return cur.fetchone()["id"]


def apply_item_changes(store_id, item_type, item_id, fields, *, source, change_type,
                       client=None, extra_fields=None, allow_dropped_blocks=False,
                       allow_dropped_primary=False, scores_before=None, stamp=True):
    """Push content changes for one item. Returns
    {status: "ok"|"noop", pushed, old, event_id}; raises ApplyRefused for a
    stale or blocked plan and ValueError when the item no longer exists.

    extra_fields go to Shopify untouched alongside the content (tags, product
    type); the caller mirrors those itself.
    """
    cfg = ITEM_CONFIG[item_type]
    row, mirror = _load_mirror(item_type, item_id)
    client = client or _client_for(store_id)

    live = client.get_item_content(item_type, row["shopify_id"])
    if live is None:
        raise ValueError(f"This {item_type} no longer exists in Shopify — sync the store")

    primary = None if allow_dropped_primary else _resolved_primary(store_id, item_type, item_id)
    plan = plan_apply(mirror, live, fields, allow_dropped_blocks=allow_dropped_blocks, primary_keyword=primary)

    if plan["status"] == "stale":
        _write_mirror(item_type, item_id, {k: live.get(k) or "" for k in plan["fields"]}, stamp=False)
        raise ApplyRefused(plan, STALE_MESSAGE)
    if plan["status"] == "blocked":
        raise ApplyRefused(plan, blocked_message(plan))

    pushed = plan.get("pushed", {})
    if plan["status"] == "noop" and not extra_fields:
        return {"status": "noop", "pushed": {}, "old": {}, "event_id": None}

    update = getattr(client, f"update_{item_type}")
    update(row["shopify_id"], {**shopify_fields(item_type, pushed), **(extra_fields or {})})

    if not pushed:
        return {"status": "ok", "pushed": {}, "old": {}, "event_id": None}

    # Mirror what Shopify actually stored (it may normalise HTML), so the next
    # stale check compares like with like.
    stored = pushed
    try:
        after = client.get_item_content(item_type, row["shopify_id"])
        if after:
            stored = {k: after.get(k) or "" for k in pushed}
    except Exception:
        logger.warning("Post-apply read failed for %s %s; mirroring pushed values", item_type, item_id)
    _write_mirror(item_type, item_id, stored, stamp=stamp)

    event_id = _record_event(store_id, item_type, item_id, source, change_type, plan["old"], stored, scores_before)

    from app.services.analysis_refresh import refresh_item_analysis
    refresh_item_analysis(store_id, item_type, item_id)

    return {"status": "ok", "pushed": stored, "old": plan["old"], "event_id": event_id}


def revert_change(event_id):
    """Undo one recorded change, if the page still holds what it wrote."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM seo_change_events WHERE id = %s", (event_id,))
        event = cur.fetchone()
    if not event:
        raise ValueError("Change not found")
    if event.get("reverted_at"):
        raise ValueError("This change was already undone")
    old, new = event.get("old_values"), event.get("new_values")
    if not old or not new:
        raise ValueError("This change was recorded before undo was available")

    item_type, item_id, store_id = event["item_type"], event["item_id"], event["store_id"]
    row, _ = _load_mirror(item_type, item_id)
    client = _client_for(store_id)
    live = client.get_item_content(item_type, row["shopify_id"])
    if live is None:
        raise ValueError(f"This {item_type} no longer exists in Shopify")
    changed = can_revert(new, live)
    if changed:
        raise ValueError(
            "Cannot undo — " + ", ".join(changed).replace("_", " ")
            + " changed again after this edit. Undo the later change first."
        )

    _write_mirror(item_type, item_id, {k: live.get(k) or "" for k in new}, stamp=False)
    result = apply_item_changes(
        store_id, item_type, item_id, old, source="undo", change_type="revert", client=client,
        allow_dropped_blocks=True, allow_dropped_primary=True, stamp=False,
    )
    with db.get_cursor() as cur:
        cur.execute("UPDATE seo_change_events SET reverted_at = NOW() WHERE id = %s", (event_id,))
    return result
