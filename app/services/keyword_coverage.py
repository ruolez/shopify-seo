"""Keyword ownership and intent-coverage analysis.

Answers, for any keyword or topic family: which page owns it (and in what
role), which intent variants are covered by the right content type, and
where the gaps are. Ownership precedence — a resolver-assigned primary on a
product/collection beats an accepted assignment, which beats an audit-elected
primary mapping; mere content_keyword_map presence is only a "mention" and
never makes a page an owner of record (the audit DELETEs and rebuilds that
table, so it cannot carry durable decisions).

Family detail also pivots ownership by page ("pages" / "unowned" keys) with a
staleness verdict per owning page: never_optimized -> stale -> low_score ->
fresh, first match wins. "stale" means the content demonstrably predates the
current primary: either an accepted assignment is newer than the last AI
optimize, or (products/collections) the resolved primary appears in neither
the title nor the meta title. keyword_resolution->>'resolved_at' is NOT used —
the 7-day background refresh re-stamps it, which would mark everything stale.
Limitations: synced_pages has no optimize timestamp, so pages can only be
never_optimized/low_score/fresh; the title/meta check is conservative (a page
optimized for its keyword only in the body reads stale).
"""

import logging

from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.blog_seo_analyzer import analyze_article_seo
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

INTENTS = ["transactional", "commercial", "informational", "navigational"]

_TITLE_TABLES = {
    "product": "synced_products",
    "collection": "synced_collections",
    "article": "synced_articles",
    "page": "synced_pages",
}


def _rules():
    return get_setting("rules.keyword_coverage", DEFAULTS["rules.keyword_coverage"])


def get_ownership_map(store_id, keyword_ids):
    """{kw_id: {"owner": best-or-None, "owners": [all ranked owners]}}"""
    if not keyword_ids:
        return {}
    from app.services.keyword_identity import variant_key

    # Rank 1 — resolver primaries, joined in Python by variant_key: ownership
    # folds plurals and word order (the claim identity every guard uses), which
    # SQL string equality — even a _norm mirror — cannot. "dutch masters" must
    # read as owned by the page whose primary is "dutch master".
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT item_type, item_id,
                      keyword_resolution->'primary'->>'keyword' AS kw
               FROM technical_seo_analyses
               WHERE store_id = %s
                 AND keyword_resolution->'primary'->>'keyword' IS NOT NULL""",
            (store_id,),
        )
        primary_rows = cur.fetchall()
        cur.execute(
            "SELECT id, keyword FROM master_keywords WHERE store_id = %s AND id = ANY(%s)",
            (store_id, list(keyword_ids)),
        )
        mk_rows = cur.fetchall()

    primaries_by_vk = {}
    for r in primary_rows:
        vk = variant_key(r["kw"])
        if vk:
            primaries_by_vk.setdefault(vk, []).append(r)

    rows = []
    for mk in mk_rows:
        for owner in primaries_by_vk.get(variant_key(mk["keyword"]), []):
            rows.append({"kw_id": mk["id"], "rank": 1, "role": "resolved_primary",
                         "item_type": owner["item_type"], "item_id": owner["item_id"]})

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT kw_id, rank, role, item_type, item_id FROM (
                 SELECT ka.master_keyword_id AS kw_id, 2 AS rank, 'assigned' AS role,
                        ka.assigned_item_type AS item_type, ka.assigned_item_id AS item_id
                   FROM keyword_assignments ka
                  WHERE ka.store_id = %(sid)s AND ka.status IN ('accepted', 'applied')
                    AND ka.assigned_item_type IS NOT NULL AND ka.assigned_item_id IS NOT NULL
                    AND ka.master_keyword_id = ANY(%(ids)s)
                 UNION ALL
                 SELECT ckm.master_keyword_id, 3, 'mapped_primary',
                        ckm.item_type, ckm.item_id
                   FROM content_keyword_map ckm
                  WHERE ckm.store_id = %(sid)s AND ckm.is_primary
                    AND ckm.master_keyword_id = ANY(%(ids)s)
               ) owners ORDER BY kw_id, rank""",
            {"sid": store_id, "ids": list(keyword_ids)},
        )
        rows.extend(cur.fetchall())

    rows.sort(key=lambda r: (r["kw_id"], r["rank"]))

    titles = _resolve_titles(store_id, rows)
    result = {}
    for row in rows:
        owner = {
            "role": row["role"],
            "item_type": row["item_type"],
            "item_id": row["item_id"],
            **titles.get((row["item_type"], row["item_id"]), {}),
        }
        entry = result.setdefault(row["kw_id"], {"owner": None, "owners": []})
        # rows arrive rank-ordered, so the first owner per keyword is the best
        if entry["owner"] is None:
            entry["owner"] = owner
        if not any(
            o["item_type"] == owner["item_type"] and o["item_id"] == owner["item_id"]
            for o in entry["owners"]
        ):
            entry["owners"].append(owner)
    return result


def _resolve_titles(store_id, owner_rows):
    ids_by_type = {}
    for row in owner_rows:
        ids_by_type.setdefault(row["item_type"], set()).add(row["item_id"])
    titles = {}
    with db.get_cursor() as cur:
        for item_type, ids in ids_by_type.items():
            table = _TITLE_TABLES.get(item_type)
            if not table:
                continue
            cur.execute(
                f"SELECT id, title, handle FROM {table} WHERE store_id = %s AND id = ANY(%s)",
                (store_id, list(ids)),
            )
            for r in cur.fetchall():
                titles[(item_type, r["id"])] = {"title": r["title"], "handle": r["handle"]}
    return titles


def _load_family_keywords(store_id, cluster_id=None, all_keywords=False):
    where = "store_id = %s AND status = 'active'"
    params = [store_id]
    if not all_keywords:
        if cluster_id is None:
            where += " AND cluster_id IS NULL"
        else:
            where += " AND cluster_id = %s"
            params.append(cluster_id)
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT id, keyword, intent, intent_source, cluster_id,
                       COALESCE(search_volume, 0) AS search_volume,
                       cpc, best_position, clicks, impressions, ctr, source
                FROM master_keywords WHERE {where}
                ORDER BY search_volume DESC NULLS LAST""",
            params,
        )
        return cur.fetchall()


def _intent_coverage(keywords, ownership, rules):
    """Per-intent cells + gap score for one family's keyword list."""
    required = rules["required_types"]
    weights = rules["gap_weights"]
    min_vol = rules["min_intent_volume"]

    cells = {}
    gap_score = 0
    for intent in INTENTS:
        bucket = [k for k in keywords if (k["intent"] or "commercial") == intent]
        volume = sum(k["search_volume"] for k in bucket)
        covered_volume = 0
        covered_by = []
        for k in bucket:
            owners = ownership.get(k["id"], {}).get("owners", [])
            hit = next(
                (o for o in owners if o["item_type"] in required.get(intent, [])), None
            )
            if hit:
                covered_volume += k["search_volume"]
                if not any(
                    c["item_type"] == hit["item_type"] and c["item_id"] == hit["item_id"]
                    for c in covered_by
                ):
                    covered_by.append(hit)
        na = volume < min_vol
        cells[intent] = {
            "count": len(bucket),
            "volume": volume,
            "covered_volume": covered_volume,
            "covered": covered_volume > 0,
            "na": na,
            "required_types": required.get(intent, []),
            "covered_by": covered_by[:5],
        }
        if not na:
            gap_score += (volume - covered_volume) * weights.get(intent, 0)
    return cells, round(gap_score)


def _staleness(item_type, score, last_optimized_at, assigned_at,
               primary_kw, title_kw_score, meta_kw_score, threshold):
    """(status, reason) for one owning page. First matching rule wins."""
    if item_type == "page":
        if not score:
            return "never_optimized", "Page has never been scored"
    elif last_optimized_at is None:
        return "never_optimized", "Never AI-optimized"

    if assigned_at and last_optimized_at and assigned_at > last_optimized_at:
        return "stale", (
            f"Keyword assigned {assigned_at:%Y-%m-%d}, after last optimize "
            f"{last_optimized_at:%Y-%m-%d}"
        )
    if (
        item_type in ("product", "collection")
        and primary_kw
        and title_kw_score == 0
        and meta_kw_score == 0
    ):
        return "stale", "Title and meta title do not contain the current primary keyword"

    if score is not None and score < threshold:
        return "low_score", f"SEO score {round(score)} is below {threshold}"

    return "fresh", None


def _fetch_owner_records(store_id, item_type, item_ids):
    """{item_id: record} with title/handle/score/optimize-freshness inputs."""
    records = {}
    with db.get_cursor() as cur:
        if item_type in ("product", "collection"):
            table = _TITLE_TABLES[item_type]
            cur.execute(
                f"""SELECT i.id, i.title, i.handle, i.last_optimized_at,
                           tsa.score,
                           tsa.keyword_resolution->'primary'->>'keyword' AS primary_kw,
                           COALESCE((tsa.keyword_resolution->'primary'->>'volume')::float, 0)
                               AS primary_volume,
                           COALESCE((tsa.keyword_resolution->'primary'->>'manual')::boolean, false)
                               AS primary_manual,
                           COALESCE((tsa.checks->'title_has_keyword'->>'score')::int, 100)
                               AS title_kw_score,
                           COALESCE((tsa.checks->'meta_title_has_keyword'->>'score')::int, 100)
                               AS meta_kw_score
                    FROM {table} i
                    LEFT JOIN technical_seo_analyses tsa
                      ON tsa.store_id = %s AND tsa.item_type = %s AND tsa.item_id = i.id
                    WHERE i.store_id = %s AND i.id = ANY(%s)""",
                (store_id, item_type, store_id, list(item_ids)),
            )
            records = {r["id"]: dict(r) for r in cur.fetchall()}
        elif item_type == "article":
            cur.execute(
                """SELECT id, title, handle, meta_title, meta_description, body,
                          summary, image_url, image_alt, last_optimized_at
                   FROM synced_articles WHERE store_id = %s AND id = ANY(%s)""",
                (store_id, list(item_ids)),
            )
            for r in cur.fetchall():
                rec = dict(r)
                try:
                    rec["score"] = analyze_article_seo(rec)["score"]
                except Exception:
                    rec["score"] = None
                records[r["id"]] = rec
        elif item_type == "page":
            cur.execute(
                "SELECT id, title, handle, seo_score AS score FROM synced_pages "
                "WHERE store_id = %s AND id = ANY(%s)",
                (store_id, list(item_ids)),
            )
            for r in cur.fetchall():
                records[r["id"]] = {**dict(r), "last_optimized_at": None}
    return records


def _gsc_contenders(store_id, queries, limit=6):
    """Pages Google actually shows for these queries (30d), best position
    first, position impression-weighted. Includes pages that own nothing —
    often the real ranking page is not among the primary holders."""
    import re as _re
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT page,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS pos,
                      SUM(clicks)::int AS clicks, SUM(impressions)::int AS impressions
               FROM gsc_daily_metrics
               WHERE store_id = %s
                 -- 30d window ending today-3: GSC's last ~3 days are partial,
                 -- and unsettled days skewed the conflict view's positions.
                 AND date >= CURRENT_DATE - 33 AND date <= CURRENT_DATE - 3
                 AND query = ANY(%s)
               GROUP BY page HAVING SUM(impressions) > 0
               ORDER BY pos ASC LIMIT %s""",
            (store_id, list(queries), limit),
        )
        rows = cur.fetchall()

    handles = {"product": {}, "collection": {}}
    parsed = []
    for r in rows:
        m = _re.search(r"/(products|collections)/([^/?#]+)", r["page"] or "")
        item_type = handle = None
        if m:
            item_type = "product" if m.group(1) == "products" else "collection"
            handle = m.group(2)
            handles[item_type][handle] = None
        parsed.append({**dict(r), "item_type": item_type, "handle": handle})

    with db.get_cursor() as cur:
        for item_type, hmap in handles.items():
            if not hmap:
                continue
            cur.execute(
                f"SELECT id, title, handle FROM {_TITLE_TABLES[item_type]} "
                f"WHERE store_id = %s AND handle = ANY(%s)",
                (store_id, list(hmap)),
            )
            for r in cur.fetchall():
                hmap[r["handle"]] = {"item_id": r["id"], "title": r["title"]}

    contenders = []
    for p in parsed:
        rec = handles.get(p["item_type"], {}).get(p["handle"]) if p["handle"] else None
        contenders.append({
            "url": p["page"],
            "item_type": p["item_type"] if rec else None,
            "item_id": rec["item_id"] if rec else None,
            "title": rec["title"] if rec else None,
            "handle": p["handle"] if rec else None,
            "gsc_position": round(float(p["pos"]), 1) if p["pos"] is not None else None,
            "gsc_clicks": p["clicks"],
            "gsc_impressions": p["impressions"],
        })
    return contenders


def _detect_conflicts(store_id, pages, keywords):
    """Groups of pages whose primaries are variants of one keyword
    (exact duplicate, plural, or word-order — same SERP). Scans ALL store
    primaries, not just this family's owner-of-record pages, because the
    ownership map keeps one best owner per keyword — the second holder of an
    exact-duplicate primary would otherwise be invisible. Tags family pages
    with conflict_key and attaches the GSC contenders for the shared query,
    so the UI shows which page Google actually prefers."""
    from app.services.keyword_resolver import variant_key

    fam_keys = {}
    for k in keywords:
        fam_keys.setdefault(variant_key(k["keyword"]), []).append(k)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT item_type, item_id,
                      keyword_resolution->'primary'->>'keyword' AS kw,
                      COALESCE((keyword_resolution->'primary'->>'manual')::boolean, false) AS manual
               FROM technical_seo_analyses
               WHERE store_id = %s AND keyword_resolution->'primary'->>'keyword' IS NOT NULL
               UNION ALL
               SELECT ka.assigned_item_type, ka.assigned_item_id, mk.keyword, true
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ka.store_id = %s AND ka.status IN ('accepted', 'applied')
                 AND ka.assigned_item_type IS NOT NULL""",
            (store_id, store_id),
        )
        holder_rows = cur.fetchall()

    groups = {}
    for r in holder_rows:
        key = variant_key(r["kw"])
        if key in fam_keys:
            holders = groups.setdefault(key, {})
            holders.setdefault((r["item_type"], r["item_id"]),
                               {"keyword": r["kw"], "manual": bool(r["manual"])})

    conflict_groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not conflict_groups:
        return []

    all_holder_ids = [ids for v in conflict_groups.values() for ids in v]
    titles = _resolve_titles(
        store_id, [{"item_type": t, "item_id": i} for t, i in all_holder_ids]
    )
    page_by_id = {(p["item_type"], p["item_id"]): p for p in pages}
    vol_by_kw = {k["keyword"]: k["search_volume"] for k in keywords}

    conflicts = []
    for key, holders in conflict_groups.items():
        variants = {h["keyword"] for h in holders.values()}
        variants |= {k["keyword"] for k in fam_keys[key]}
        gsc = _gsc_contenders(store_id, variants)
        gsc_by_item = {(c["item_type"], c["item_id"]): c for c in gsc if c["item_id"]}

        contenders = []
        for (item_type, item_id), h in holders.items():
            fam_page = page_by_id.get((item_type, item_id))
            if fam_page is not None:
                fam_page["conflict_key"] = key
            hit = gsc_by_item.get((item_type, item_id), {})
            meta = titles.get((item_type, item_id), {})
            contenders.append({
                "item_type": item_type,
                "item_id": item_id,
                "title": (fam_page or {}).get("title") or meta.get("title"),
                "handle": (fam_page or {}).get("handle") or meta.get("handle"),
                "primary_keyword": h["keyword"],
                "manual": h["manual"],
                "is_owner": True,
                "gsc_position": hit.get("gsc_position"),
                "gsc_clicks": hit.get("gsc_clicks", 0),
                "gsc_impressions": hit.get("gsc_impressions", 0),
            })
        for c in gsc:
            if (c["item_type"], c["item_id"]) not in holders:
                contenders.append({**c, "is_owner": False, "primary_keyword": None,
                                   "manual": False})

        contenders.sort(key=lambda c: (c["gsc_position"] is None, c["gsc_position"] or 0))
        conflicts.append({
            "key": key,
            "keywords": sorted(
                ({"keyword": v, "search_volume": vol_by_kw.get(v, 0)} for v in variants),
                key=lambda x: -x["search_volume"],
            ),
            "contenders": contenders,
        })
    conflicts.sort(key=lambda c: -max(k["search_volume"] for k in c["keywords"]))
    return conflicts


def _family_pages(store_id, keywords, ownership, rules):
    """Pivot one family's ownership by page -> (pages, unowned).

    pages: every item that best-owns >=1 family keyword, with its primary
    keyword, other owned family keywords, score and staleness verdict.
    unowned: family keywords with demand but no owner of record, annotated
    with the content types that should own them. Uncapped, volume desc.
    """
    threshold = rules.get("stale_score_threshold", 70)
    required = rules["required_types"]
    family_kw_texts = {k["keyword"] for k in keywords}

    buckets = {}
    unowned = []
    for k in keywords:
        owner = ownership.get(k["id"], {}).get("owner")
        if owner:
            buckets.setdefault((owner["item_type"], owner["item_id"]), []).append(
                (k, owner["role"])
            )
        elif k["search_volume"] > 0:
            intent = k["intent"] or "commercial"
            unowned.append({
                "id": k["id"],
                "keyword": k["keyword"],
                "search_volume": k["search_volume"],
                "intent": intent,
                "cpc": float(k["cpc"]) if k.get("cpc") is not None else None,
                "best_position": k.get("best_position"),
                "source": k.get("source"),
                "required_types": required.get(intent, []),
                "action": "research_topic"
                if intent in ("informational", "commercial")
                else "assign",
            })

    ids_by_type = {}
    for item_type, item_id in buckets:
        ids_by_type.setdefault(item_type, set()).add(item_id)
    records = {}
    for item_type, ids in ids_by_type.items():
        for item_id, rec in _fetch_owner_records(store_id, item_type, ids).items():
            records[(item_type, item_id)] = rec

    assignments = {}
    if buckets:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT ka.assigned_item_type, ka.assigned_item_id, ka.updated_at,
                          mk.keyword, COALESCE(mk.search_volume, 0) AS search_volume
                   FROM keyword_assignments ka
                   JOIN master_keywords mk ON mk.id = ka.master_keyword_id
                   WHERE ka.store_id = %s AND ka.status IN ('accepted', 'applied')
                     AND (ka.assigned_item_type, ka.assigned_item_id) IN %s
                   ORDER BY ka.updated_at DESC""",
                (store_id, tuple(buckets.keys())),
            )
            for r in cur.fetchall():
                # rows arrive newest-first; keep only the newest per item
                assignments.setdefault(
                    (r["assigned_item_type"], r["assigned_item_id"]), dict(r)
                )

    pages = []
    for (item_type, item_id), owned in buckets.items():
        rec = records.get((item_type, item_id))
        if not rec:
            continue
        assignment = assignments.get((item_type, item_id))

        if item_type in ("product", "collection") and rec.get("primary_kw"):
            primary = {
                "keyword": rec["primary_kw"],
                "search_volume": round(rec["primary_volume"] or 0),
                "role": "resolved_primary",
                "manual": rec["primary_manual"],
                "in_family": rec["primary_kw"] in family_kw_texts,
            }
        elif assignment:
            primary = {
                "keyword": assignment["keyword"],
                "search_volume": assignment["search_volume"],
                "role": "assigned",
                "manual": True,
                "in_family": assignment["keyword"] in family_kw_texts,
            }
        else:
            primary = None

        primary_text = primary["keyword"] if primary else None
        owned_keywords = [
            {
                "id": k["id"],
                "keyword": k["keyword"],
                "search_volume": k["search_volume"],
                "intent": k["intent"] or "commercial",
                "role": role,
            }
            for k, role in owned
            if k["keyword"] != primary_text
        ]

        status, reason = _staleness(
            item_type,
            rec.get("score"),
            rec.get("last_optimized_at"),
            assignment["updated_at"] if assignment else None,
            rec.get("primary_kw"),
            rec.get("title_kw_score", 100),
            rec.get("meta_kw_score", 100),
            threshold,
        )
        last_opt = rec.get("last_optimized_at")
        pages.append({
            "item_type": item_type,
            "item_id": item_id,
            "title": rec.get("title"),
            "handle": rec.get("handle"),
            "primary_keyword": primary,
            "owned_keywords": owned_keywords,
            "owned_volume": sum(k["search_volume"] for k, _ in owned),
            "seo_score": round(rec["score"]) if rec.get("score") is not None else None,
            "last_optimized_at": last_opt.isoformat() if last_opt else None,
            "staleness": status,
            "stale_reason": reason,
        })

    pages.sort(key=lambda p: p["owned_volume"], reverse=True)
    unowned.sort(key=lambda u: u["search_volume"], reverse=True)
    conflicts = _detect_conflicts(store_id, pages, keywords)
    return pages, unowned, conflicts


def get_coverage_matrix(store_id, min_volume=0, search=None, only_gaps=False,
                        sort_by="gap_score", limit=50, offset=0):
    rules = _rules()
    keywords = _load_family_keywords(store_id, all_keywords=True)
    ownership = get_ownership_map(store_id, [k["id"] for k in keywords])

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, description FROM master_keyword_clusters WHERE store_id = %s",
            (store_id,),
        )
        clusters = {r["id"]: r for r in cur.fetchall()}

    by_cluster = {}
    for k in keywords:
        by_cluster.setdefault(k["cluster_id"], []).append(k)

    families = []
    for cluster_id, kws in by_cluster.items():
        cluster = clusters.get(cluster_id)
        name = cluster["name"] if cluster else "unclustered"
        if search and search.lower() not in name.lower():
            continue
        total_volume = sum(k["search_volume"] for k in kws)
        if total_volume < min_volume:
            continue
        cells, gap_score = _intent_coverage(kws, ownership, rules)
        if only_gaps and gap_score == 0:
            continue
        owner_pages = set()
        for k in kws:
            owner = ownership.get(k["id"], {}).get("owner")
            if owner:
                owner_pages.add((owner["item_type"], owner["item_id"]))
        families.append({
            "cluster_id": cluster_id,
            "name": name,
            "description": (cluster or {}).get("description"),
            "keyword_count": len(kws),
            "total_volume": total_volume,
            "intents": cells,
            "gap_score": gap_score,
            "pages_count": len(owner_pages),
            "unclustered": cluster_id is None,
        })

    sort_keys = {
        "gap_score": lambda f: f["gap_score"],
        "total_volume": lambda f: f["total_volume"],
        "keyword_count": lambda f: f["keyword_count"],
        "name": lambda f: f["name"],
    }
    reverse = sort_by != "name"
    families.sort(key=sort_keys.get(sort_by, sort_keys["gap_score"]), reverse=reverse)
    # The unclustered bucket is a worklist, not a family — pin it last
    families.sort(key=lambda f: f["unclustered"])

    total = len(families)
    page = families[offset : offset + limit]
    gap_volume = sum(f["gap_score"] for f in families)
    return {"families": page, "total": total, "gap_volume": gap_volume}


def get_family_coverage(store_id, cluster_id, include_pages=True):
    """Full picture for one family (cluster_id 0/None = unclustered bucket)."""
    rules = _rules()
    cluster = None
    if cluster_id:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, name, description FROM master_keyword_clusters "
                "WHERE store_id = %s AND id = %s",
                (store_id, cluster_id),
            )
            cluster = cur.fetchone()
        if not cluster:
            return None

    keywords = _load_family_keywords(store_id, cluster_id=cluster_id or None)
    ownership = get_ownership_map(store_id, [k["id"] for k in keywords])
    cells, gap_score = _intent_coverage(keywords, ownership, rules)

    by_intent = {intent: [] for intent in INTENTS}
    for k in keywords:
        entry = dict(k)
        entry["owner"] = ownership.get(k["id"], {}).get("owner")
        by_intent.setdefault(k["intent"] or "commercial", []).append(entry)

    gaps = []
    for intent in INTENTS:
        cell = cells[intent]
        if cell["na"]:
            continue
        bucket = by_intent.get(intent, [])
        unowned = [k for k in bucket if not k["owner"]]
        if not cell["covered"]:
            gaps.append({
                "type": "intent_uncovered",
                "intent": intent,
                "volume": cell["volume"],
                "action": "research_topic" if intent in ("informational", "commercial") else "assign",
                "keywords": [
                    {"id": k["id"], "keyword": k["keyword"], "search_volume": k["search_volume"]}
                    for k in bucket[:8]
                ],
                "message": (
                    f"No {intent} variant is covered by any "
                    f"{' or '.join(cell['required_types'])}"
                ),
            })
        elif unowned:
            top = unowned[0]
            if top["search_volume"] >= rules["min_intent_volume"]:
                gaps.append({
                    "type": "keyword_unowned",
                    "intent": intent,
                    "keyword_id": top["id"],
                    "keyword": top["keyword"],
                    "volume": top["search_volume"],
                    "action": "assign",
                    "message": f"\"{top['keyword']}\" has no owning page",
                })

    result = {
        "cluster": cluster or {"id": None, "name": "unclustered", "description": None},
        "keyword_count": len(keywords),
        "total_volume": sum(k["search_volume"] for k in keywords),
        "keywords_by_intent": by_intent,
        "coverage": cells,
        "gap_score": gap_score,
        "gaps": gaps,
    }
    if include_pages:
        result["pages"], result["unowned"], result["conflicts"] = _family_pages(
            store_id, keywords, ownership, rules
        )
    return result


def get_keyword_detail(store_id, keyword_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT mk.*, mkc.name AS cluster_name
               FROM master_keywords mk
               LEFT JOIN master_keyword_clusters mkc ON mkc.id = mk.cluster_id
               WHERE mk.store_id = %s AND mk.id = %s""",
            (store_id, keyword_id),
        )
        kw = cur.fetchone()
        if not kw:
            return None
        cur.execute(
            "SELECT COUNT(*) AS n FROM content_keyword_map "
            "WHERE store_id = %s AND master_keyword_id = %s",
            (store_id, keyword_id),
        )
        mentions_count = cur.fetchone()["n"]

    ownership = get_ownership_map(store_id, [keyword_id]).get(keyword_id, {})
    family = (
        get_family_coverage(store_id, kw["cluster_id"], include_pages=False)
        if kw["cluster_id"]
        else None
    )

    return {
        "keyword": kw,
        "owner": ownership.get("owner"),
        "owners": ownership.get("owners", []),
        "mentions_count": mentions_count,
        "family": family,
    }


def get_keyword_mentions(store_id, keyword_id, limit=25, offset=0):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS total FROM content_keyword_map "
            "WHERE store_id = %s AND master_keyword_id = %s",
            (store_id, keyword_id),
        )
        total = cur.fetchone()["total"]
        cur.execute(
            """SELECT item_type, item_id, found_in, is_primary, relevance_score
               FROM content_keyword_map
               WHERE store_id = %s AND master_keyword_id = %s
               ORDER BY is_primary DESC, relevance_score DESC
               LIMIT %s OFFSET %s""",
            (store_id, keyword_id, limit, offset),
        )
        rows = cur.fetchall()

    titles = _resolve_titles(store_id, rows)
    mentions = []
    for row in rows:
        mentions.append({**dict(row), **titles.get((row["item_type"], row["item_id"]), {})})
    return {"mentions": mentions, "total": total}
