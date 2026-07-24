"""Keyword ownership and intent-coverage analysis.

Answers, for any keyword or topic family: which page owns it (and in what
role), which intent variants are covered by the right content type, and
where the gaps are. Ownership precedence — a resolver-assigned primary on a
product/collection beats an accepted assignment, which beats an audit-elected
primary mapping; mere content_keyword_map presence is only a "mention" and
never makes a page an owner of record (the audit DELETEs and rebuilds that
table, so it cannot carry durable decisions).
"""

import logging

from app.database import db
from app.prompt_defaults import DEFAULTS
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
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT kw_id, rank, role, item_type, item_id FROM (
                 SELECT mk.id AS kw_id, 1 AS rank, 'resolved_primary' AS role,
                        tsa.item_type, tsa.item_id
                   FROM master_keywords mk
                   JOIN technical_seo_analyses tsa
                     ON tsa.store_id = mk.store_id
                    AND (tsa.keyword_resolution->'primary'->>'keyword') = mk.keyword
                  WHERE mk.store_id = %(sid)s AND mk.id = ANY(%(ids)s)
                 UNION ALL
                 SELECT ka.master_keyword_id, 2, 'assigned',
                        ka.assigned_item_type, ka.assigned_item_id
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
        rows = cur.fetchall()

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
        families.append({
            "cluster_id": cluster_id,
            "name": name,
            "description": (cluster or {}).get("description"),
            "keyword_count": len(kws),
            "total_volume": total_volume,
            "intents": cells,
            "gap_score": gap_score,
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


def get_family_coverage(store_id, cluster_id):
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

    return {
        "cluster": cluster or {"id": None, "name": "unclustered", "description": None},
        "keyword_count": len(keywords),
        "total_volume": sum(k["search_volume"] for k in keywords),
        "keywords_by_intent": by_intent,
        "coverage": cells,
        "gap_score": gap_score,
        "gaps": gaps,
    }


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
    family = get_family_coverage(store_id, kw["cluster_id"]) if kw["cluster_id"] else None

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
