"""Links that help Google find pages it has not indexed.

Every night, after the indexing check, each unindexed product, collection
and article gets up to PER_TARGET suggested source pages: indexed pages
that already name it, or are about the same brand or topic, best-read
first. A suggestion is one link placed by auto_linker (the page's own words
become the link, or it joins a short "Related" line) through the apply
pipeline, so each one has history and undo.

Suggestions wait for review unless the store turns on automatic mode
(setting links.auto_unindexed): then up to links.auto_unindexed_per_night
are applied each night. A source page carries at most SOURCE_CAP links
added this way, ever, so no page turns into a link list.
"""
import html
import logging
import math
import re

from app.database import db
from app.services.keyword_identity import fold_token, tokens

logger = logging.getLogger(__name__)

PER_TARGET = 2
SOURCE_CAP = 5
# Linked pages are re-checked this long after their first link, and count as
# stuck when Google still has not indexed them this long after it.
RECHECK_AFTER_LINK_DAYS = 7
STUCK_AFTER_DAYS = 21
_FILLER = {"the", "and", "for", "with", "all", "pack", "packs", "count", "box", "new", "best", "your", "how",
           "what", "why", "guide", "collection", "collections", "product", "products", "flavor", "flavors",
           "every", "top", "buy", "shop", "online", "review", "reviews", "ranked", "vs"}
# Title, then a size or pack suffix that starts with a number:
# " - 15 Pouches of 2", ", 6ct (1.2oz)", " (8 Packs of 5)".
_SIZE_SUFFIX_RE = re.compile(r"^(.*?)(?:\s+[-–—|]\s+|,\s*|\s*\()\s*\(?\d.*$")
_NUMERIC_RE = re.compile(r"^\d+[a-z]{0,3}$")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def text_of(html):
    """Lowercased text of an HTML body, tags replaced by spaces. Pure."""
    return _TAG_RE.sub(" ", html or "").lower()


def _words(text):
    return {fold_token(t) for t in tokens(text or "")
            if len(t) > 2 and t not in _FILLER and not _NUMERIC_RE.match(t)}


def _mentions(text, phrases, min_words):
    flat = _SPACE_RE.sub(" ", text or "")
    for phrase in phrases:
        p = _SPACE_RE.sub(" ", (phrase or "").strip().lower())
        if len(p.split()) >= min_words and len(p) >= 5 and p in flat:
            return True
    return False


def phrases_for(target, source):
    """Wordings of `target` that may become a link on `source`. Pure.

    A product's keyword is usually its category or family ("shisha
    tobacco", "game vega cigarillos"); as link text pointing at one product
    it misleads. So a product is linked in the text only where the page
    uses its own name: the full title, or the title before its size or pack
    suffix ("Game Leaf Black Russian Cigarillos" for "... - 15 Pouches of
    2"). A flavour after the dash stays: "Fumari Hookah Tobacco" alone names
    the whole line, not its watermelon flavour. Other pages keep their phrases (collection_phrases guards those)."""
    phrases = [p for p in (target.get("phrases") or [target["title"]]) if p and p.strip()]
    if target["type"] != "product":
        return phrases
    title = (target["title"] or "").strip()
    out = [title] if title else []
    m = _SIZE_SUFFIX_RE.match(title)
    if m and len(m.group(1).split()) >= 3:
        out.append(m.group(1).strip())
    return out


def score_source(target, source):
    """(score, reason) when `source` is a sensible page to link from to
    `target`, else None. Pure.

    target: {type, id, title, vendor, phrases}
    source: {type, id, title, text (lowercased), clicks, indexed, vendor}
    """
    if (source["type"], source["id"]) == (target["type"], target["id"]):
        return None
    if source.get("indexed") is False:
        return None
    # Two pages with one title are duplicates; a link between them helps neither.
    if (source["title"] or "").strip().lower() == (target["title"] or "").strip().lower():
        return None
    min_words = 1 if target["type"] == "collection" else 2
    mention = _mentions(source.get("text"), phrases_for(target, source), min_words)
    source_words = _words(source["title"]) | _words(source.get("vendor"))
    shared = _words(target["title"]) & source_words
    brand = _words(target.get("vendor"))
    brand_shared = brand & source_words
    brand_hit = bool(brand) and (brand_shared == brand or len(brand_shared) >= 2)

    if source["type"] == "product" and fold_token((source.get("vendor") or "").strip().lower()) != \
            fold_token((target.get("vendor") or "").strip().lower()):
        return None
    if not mention:
        if target["type"] == "product" and brand:
            if not brand_hit:
                return None
        elif len(shared) < 2:
            return None

    score = (10 if mention else 0) + 3 * len(shared - brand) + (4 if brand_hit else 0) \
        + math.log1p(max(source.get("clicks") or 0, 0)) + (1 if source.get("indexed") else 0) \
        - (3 if source["type"] == "product" else 0)
    if mention:
        reason = "Already names it, so those words become the link"
    elif brand_hit:
        reason = f"About the same brand ({(target.get('vendor') or '').strip().lower()})"
    else:
        reason = "Same topic: " + ", ".join(sorted(shared))
    return round(score, 3), reason


def plan_links(targets, sources, per_target=PER_TARGET, used=None, skip=None, accept=None):
    """Pick source pages for each target. Pure apart from `accept`.

    targets are served in priority order (highest first); a source carries
    at most SOURCE_CAP links counting `used` ({(type, id): links already
    added or pending}); `skip` holds (source_type, source_id, target_type,
    target_id) pairs never to suggest again. `accept(target, source, reason)` may
    veto a pair (e.g. the page already links there) and returns extra
    fields for the suggestion.
    """
    used = dict(used or {})
    skip = skip or set()
    plan = []
    for target in sorted(targets, key=lambda t: -(t.get("priority") or 0)):
        scored = []
        for source in sources:
            if (source["type"], source["id"], target["type"], target["id"]) in skip:
                continue
            s = score_source(target, source)
            if s:
                scored.append((s, source))
        scored.sort(key=lambda pair: -pair[0][0])
        picked = 0
        for (score, reason), source in scored:
            if picked >= per_target:
                break
            key = (source["type"], source["id"])
            if used.get(key, 0) >= SOURCE_CAP:
                continue
            extra = accept(target, source, reason) if accept else {}
            if extra is None:
                continue
            used[key] = used.get(key, 0) + 1
            picked += 1
            plan.append({"source_type": source["type"], "source_id": source["id"],
                         "target_type": target["type"], "target_id": target["id"],
                         "score": score, "reason": reason, **(extra or {})})
    return plan


# ── Database ──────────────────────────────────────────────────────────────

def _targets(store_id):
    from app.services.unindexed_plan import get_unindexed_plan

    plan = get_unindexed_plan(store_id, check_sitemap=False)
    out = []
    for p in plan["pages"]:
        out.append({"type": p["item_type"], "id": p["item_id"], "title": p.get("title") or "",
                    "vendor": p.get("vendor"), "keyword": p.get("keyword"),
                    "priority": (10 if p["worth_indexing"] else 0) + min((p.get("volume") or 0) / 100, 9)})
    return out


def _sources(store_id):
    from app.services.gsc_page_totals import page_totals_by_path

    totals, _ = page_totals_by_path(store_id, days=28)
    with db.get_cursor() as cur:
        cur.execute("SELECT item_type, item_id, indexed FROM index_status WHERE store_id = %s", (store_id,))
        indexed = {(r["item_type"], r["item_id"]): r["indexed"] for r in cur.fetchall()}
        cur.execute(
            """SELECT sa.id, sa.title, sa.body, '/blogs/' || sb.handle || '/' || sa.handle AS path
               FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.is_published
                 AND COALESCE(sa.tags, '') NOT ILIKE '%%landing-page%%'""",
            (store_id,),
        )
        articles = cur.fetchall()
        cur.execute(
            """SELECT id, title, description, '/collections/' || handle AS path FROM synced_collections
               WHERE store_id = %s AND COALESCE(published, TRUE) AND COALESCE(handle, '') <> ''""",
            (store_id,),
        )
        collections = cur.fetchall()
        cur.execute(
            """SELECT id, title, description, vendor, '/products/' || handle AS path FROM synced_products
               WHERE store_id = %s AND UPPER(COALESCE(status, 'ACTIVE')) = 'ACTIVE'
                 AND COALESCE(handle, '') <> '' AND COALESCE(vendor, '') <> ''""",
            (store_id,),
        )
        products = cur.fetchall()

    def clicks(path):
        return (totals.get(path.lower().rstrip("/")) or {}).get("clicks", 0)

    sources = []
    for kind, rows, body_col in (("article", articles, "body"), ("collection", collections, "description")):
        for r in rows:
            sources.append({"type": kind, "id": r["id"], "title": r["title"] or "", "text": text_of(r[body_col]),
                            "clicks": clicks(r["path"]), "indexed": indexed.get((kind, r["id"])), "vendor": None})
    # A product page only helps when Google already indexed it and people
    # reach it; the rest would add links nobody (and no crawler) follows.
    for r in products:
        c = clicks(r["path"])
        if indexed.get(("product", r["id"])) and c > 0:
            sources.append({"type": "product", "id": r["id"], "title": r["title"] or "",
                            "text": text_of(r["description"]), "clicks": c, "indexed": True, "vendor": r["vendor"]})
    return sources


def refresh_suggestions(store_id):
    """Recompute pending suggestions. Applied and dismissed ones are kept and
    never suggested again; pending ones for pages now indexed are dropped."""
    from app.services import auto_linker

    targets = _targets(store_id)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT s.source_type, s.source_id, s.target_type, s.target_id, s.status,
                      e.reverted_at IS NOT NULL AS undone
               FROM link_suggestions s LEFT JOIN seo_change_events e ON e.id = s.event_id
               WHERE s.store_id = %s""",
            (store_id,),
        )
        rows = cur.fetchall()
    skip = {(r["source_type"], r["source_id"], r["target_type"], r["target_id"]) for r in rows if r["status"] != "pending"}
    used = {}
    for r in rows:
        if r["status"] == "applied" and not r["undone"]:
            used[(r["source_type"], r["source_id"])] = used.get((r["source_type"], r["source_id"]), 0) + 1

    bodies, target_info = {}, {}

    def accept(target, source, reason):
        from app.services.apply_pipeline import _load_mirror

        tkey = (target["type"], target["id"])
        if tkey not in target_info:
            target_info[tkey] = auto_linker._target(target["type"], target["id"])
        info = target_info[tkey]
        if not info:
            return None
        skey = (source["type"], source["id"])
        if skey not in bodies:
            try:
                bodies[skey] = _load_mirror(source["type"], source["id"])[1]["body"]
            except ValueError:
                bodies[skey] = None
        if bodies[skey] is None:
            return None
        min_words = 1 if target["type"] == "collection" else auto_linker.MIN_ANCHOR_WORDS
        new_body, how = auto_linker.place_link(bodies[skey], info["url"],
                                               phrases_for({**target, "phrases": info["phrases"]}, source),
                                               info["title"], min_words=min_words)
        if how not in ("inline", "related"):
            return None
        anchor = info["title"]
        if how == "inline":
            m = re.search(r'<a href="' + re.escape(info["url"]) + r'"[^>]*>(.*?)</a>', new_body, re.DOTALL)
            anchor = html.unescape(_TAG_RE.sub("", m.group(1))).strip() if m else None
        out = {"how": how, "anchor": anchor}
        if how == "related":
            if reason.startswith("Already names"):
                out["reason"] = "Names it only where a link can't go (a heading or link); added to a short Related line"
            else:
                out["reason"] = reason + "; added to a short Related line"
        return out

    for t in targets:
        info = auto_linker._target(t["type"], t["id"]) if t["type"] in ("product", "collection", "article") else None
        target_info[(t["type"], t["id"])] = info
        t["phrases"] = (info or {}).get("phrases") or [t["title"]]

    plan = plan_links([t for t in targets if target_info.get((t["type"], t["id"]))], _sources(store_id),
                      used=used, skip=skip, accept=accept)

    with db.get_cursor() as cur:
        cur.execute("DELETE FROM link_suggestions WHERE store_id = %s AND status = 'pending'", (store_id,))
        for p in plan:
            cur.execute(
                """INSERT INTO link_suggestions
                   (store_id, target_type, target_id, source_type, source_id, how, anchor, reason, score)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (store_id, target_type, target_id, source_type, source_id) DO NOTHING""",
                (store_id, p["target_type"], p["target_id"], p["source_type"], p["source_id"],
                 p["how"], p.get("anchor"), p["reason"], p["score"]),
            )
    return {"targets": len(targets), "suggested": len(plan),
            "targets_covered": len({(p["target_type"], p["target_id"]) for p in plan})}


def _pair_phrases(s):
    """phrases_for() for a stored suggestion, from the current titles."""
    from app.services import auto_linker

    info = auto_linker._target(s["target_type"], s["target_id"])
    if not info:
        raise ValueError(f"{s['target_type']} not found")
    table = {"product": "synced_products", "collection": "synced_collections", "article": "synced_articles"}
    with db.get_cursor() as cur:
        cur.execute(f"SELECT title FROM {table[s['source_type']]} WHERE id = %s", (s["source_id"],))
        source = cur.fetchone() or {"title": ""}
        vendor = None
        if s["target_type"] == "product":
            cur.execute("SELECT vendor FROM synced_products WHERE id = %s", (s["target_id"],))
            vendor = (cur.fetchone() or {}).get("vendor")
    target = {"type": s["target_type"], "id": s["target_id"], "title": info["title"], "vendor": vendor,
              "phrases": info["phrases"]}
    return phrases_for(target, {"title": source["title"] or ""})


def _start_outcome(cur, store_id, target_type, target_id, linked_at=None):
    """Remember a target's index state when it gets its first link."""
    cur.execute(
        """INSERT INTO link_outcomes (store_id, target_type, target_id, linked_at, indexed_at_link, state_at_link)
           SELECT %(s)s, %(t)s, %(i)s, COALESCE(%(at)s, NOW()), x.indexed, x.coverage_state
           FROM (SELECT 1) one
           LEFT JOIN index_status x ON x.store_id = %(s)s AND x.item_type = %(t)s AND x.item_id = %(i)s
                                   AND x.checked_at <= COALESCE(%(at)s, NOW())
           ON CONFLICT (store_id, target_type, target_id) DO NOTHING""",
        {"s": store_id, "t": target_type, "i": target_id, "at": linked_at},
    )


def link_outcome(linked_at, indexed_at_link, indexed_now, checked_at, now, stuck_after_days=STUCK_AFTER_DAYS):
    """What happened to a page after its first link. Pure.

    already: Google had it indexed when the link went in (not counted).
    indexed: a check after the link found it indexed.
    waiting: not checked since the link went in.
    not_yet / stuck: checked since, still not indexed; stuck once the link
    is stuck_after_days old."""
    if indexed_at_link:
        return "already"
    if checked_at is None or checked_at <= linked_at:
        return "waiting"
    if indexed_now:
        return "indexed"
    return "stuck" if (now - linked_at).days >= stuck_after_days else "not_yet"


def backfill_outcomes(store_id):
    """Targets linked before outcomes were recorded."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT target_type, target_id, MIN(updated_at) AS linked_at FROM link_suggestions
               WHERE store_id = %s AND status = 'applied' GROUP BY target_type, target_id""",
            (store_id,),
        )
        for r in cur.fetchall():
            _start_outcome(cur, store_id, r["target_type"], r["target_id"], r["linked_at"])


def outcome_summary(store_id, now=None):
    """Counts of linked pages by link_outcome, plus the stuck ones."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    backfill_outcomes(store_id)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT o.*, i.indexed, i.checked_at, i.coverage_state,
                      COALESCE(p.title, c.title, a.title) AS title
               FROM link_outcomes o
               LEFT JOIN index_status i ON i.store_id = o.store_id AND i.item_type = o.target_type AND i.item_id = o.target_id
               LEFT JOIN synced_products p ON o.target_type = 'product' AND p.id = o.target_id
               LEFT JOIN synced_collections c ON o.target_type = 'collection' AND c.id = o.target_id
               LEFT JOIN synced_articles a ON o.target_type = 'article' AND a.id = o.target_id
               WHERE o.store_id = %s ORDER BY o.linked_at""",
            (store_id,),
        )
        rows = cur.fetchall()
    counts = {"already": 0, "indexed": 0, "waiting": 0, "not_yet": 0, "stuck": 0}
    stuck = []
    for r in rows:
        kind = link_outcome(r["linked_at"], r["indexed_at_link"], r["indexed"], r["checked_at"], now)
        counts[kind] += 1
        if kind == "stuck":
            stuck.append({"target_type": r["target_type"], "target_id": r["target_id"], "title": r["title"],
                          "coverage_state": r["coverage_state"], "linked_at": r["linked_at"].isoformat()})
    checked = counts["indexed"] + counts["not_yet"] + counts["stuck"]
    return {"counts": counts, "linked": len(rows) - counts["already"], "checked": checked,
            "stuck": stuck, "recheck_after_days": RECHECK_AFTER_LINK_DAYS, "stuck_after_days": STUCK_AFTER_DAYS}


def apply_suggestion(suggestion_id, applied_by="person", client=None):
    """Place the suggested link. Returns the updated status."""
    from app.services import auto_linker
    from app.services.apply_pipeline import ApplyRefused

    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM link_suggestions WHERE id = %s", (suggestion_id,))
        s = cur.fetchone()
    if not s or s["status"] != "pending":
        raise ValueError("This suggestion is no longer waiting")
    status, event_id, error = "failed", None, None
    try:
        out = auto_linker.add_link(s["store_id"], s["source_type"], s["source_id"], s["target_type"], s["target_id"],
                                   client=client, phrases=_pair_phrases(s))
        status = "applied" if out["status"] == "added" else "already"
        event_id = out.get("event_id")
    except ApplyRefused as refused:
        error = str(refused)
    except Exception as e:
        logger.exception("Link suggestion %s failed", suggestion_id)
        error = str(e)
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE link_suggestions SET status = %s, event_id = %s, error = %s, applied_by = %s, updated_at = NOW()
               WHERE id = %s""",
            (status, event_id, (error or None) and error[:1000], applied_by, suggestion_id),
        )
        if status == "applied":
            _start_outcome(cur, s["store_id"], s["target_type"], s["target_id"])
    return {"status": status, "event_id": event_id, "error": error}


def dismiss_suggestion(suggestion_id):
    with db.get_cursor() as cur:
        cur.execute("UPDATE link_suggestions SET status = 'dismissed', updated_at = NOW() WHERE id = %s AND status = 'pending'",
                    (suggestion_id,))
        return cur.rowcount > 0


def pending_ids(store_id, limit=None):
    """Pending suggestions, most useful first: pages people search for, then score."""
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT id FROM link_suggestions WHERE store_id = %s AND status = 'pending'
                ORDER BY score DESC, id {"LIMIT %s" if limit else ""}""",
            (store_id, limit) if limit else (store_id,),
        )
        return [r["id"] for r in cur.fetchall()]


def apply_many(store_id, ids, applied_by="person", progress=None):
    from app.services.apply_pipeline import _client_for

    client = _client_for(store_id)
    out = {"applied": 0, "already": 0, "failed": 0, "errors": []}
    for i, sid in enumerate(ids):
        if progress:
            progress(i, len(ids))
        try:
            r = apply_suggestion(sid, applied_by=applied_by, client=client)
        except ValueError:
            continue
        key = r["status"] if r["status"] in out else "failed"
        out[key] += 1
        if r.get("error"):
            out["errors"].append(r["error"])
    out["errors"] = out["errors"][:5]
    return out


def auto_settings():
    from app.settings_manager import get_setting

    return {"auto": bool(get_setting("links.auto_unindexed", False)),
            "per_night": int(get_setting("links.auto_unindexed_per_night", 15) or 15)}


def run_nightly(store_id):
    """Daily job: refresh suggestions, then apply some when automatic mode is on."""
    refreshed = refresh_suggestions(store_id)
    settings = auto_settings()
    detail = f"{refreshed['suggested']} links suggested for {refreshed['targets_covered']} of {refreshed['targets']} unindexed pages"
    if settings["auto"]:
        applied = apply_many(store_id, pending_ids(store_id, settings["per_night"]), applied_by="auto")
        detail += f"; {applied['applied']} added automatically"
        if applied["failed"]:
            raise RuntimeError(f"{detail}; {applied['failed']} failed: {'; '.join(applied['errors'][:2])}")
    return detail


def list_suggestions(store_id, recent_days=30):
    """Pending suggestions grouped by target, and links added recently."""
    sql = """
        SELECT s.*,
               COALESCE(tp.title, tc.title, ta.title) AS target_title,
               COALESCE(sp.title, sc.title, sa.title) AS source_title,
               i.coverage_state, i.indexed AS target_indexed, e.reverted_at IS NOT NULL AS undone
        FROM link_suggestions s
        LEFT JOIN seo_change_events e ON e.id = s.event_id
        LEFT JOIN synced_products tp ON s.target_type = 'product' AND tp.id = s.target_id
        LEFT JOIN synced_collections tc ON s.target_type = 'collection' AND tc.id = s.target_id
        LEFT JOIN synced_articles ta ON s.target_type = 'article' AND ta.id = s.target_id
        LEFT JOIN synced_products sp ON s.source_type = 'product' AND sp.id = s.source_id
        LEFT JOIN synced_collections sc ON s.source_type = 'collection' AND sc.id = s.source_id
        LEFT JOIN synced_articles sa ON s.source_type = 'article' AND sa.id = s.source_id
        LEFT JOIN index_status i ON i.store_id = s.store_id AND i.item_type = s.target_type AND i.item_id = s.target_id
        WHERE s.store_id = %s AND {where}
        ORDER BY {order}"""
    with db.get_cursor() as cur:
        cur.execute(sql.format(where="s.status = 'pending'", order="s.score DESC, s.id"), (store_id,))
        pending = cur.fetchall()
        cur.execute(sql.format(where="s.status IN ('applied', 'failed') AND s.updated_at > NOW() - make_interval(days => %s)",
                               order="s.updated_at DESC"), (store_id, recent_days))
        recent = cur.fetchall()

    def row(r):
        return {"id": r["id"], "target_type": r["target_type"], "target_id": r["target_id"],
                "target_title": r["target_title"], "source_type": r["source_type"], "source_id": r["source_id"],
                "source_title": r["source_title"], "how": r["how"], "anchor": r["anchor"], "reason": r["reason"], "status": r["status"],
                "event_id": r["event_id"], "error": r["error"], "applied_by": r["applied_by"],
                "coverage_state": r["coverage_state"], "target_indexed": r["target_indexed"], "undone": r["undone"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}

    return {"pending": [row(r) for r in pending], "recent": [row(r) for r in recent], "settings": auto_settings(),
            "outcomes": outcome_summary(store_id)}
