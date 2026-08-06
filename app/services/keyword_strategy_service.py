import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.database import db
from app.services.ai_output_guard import guard_shopify_fields
from app.services.content_gap_service import classify_intent
from app.services.technical_seo_analyzer import (
    _generate_ngrams,
    _keyword_in_text,
    _strip_html,
    _tokenize,
)

logger = logging.getLogger(__name__)

RELEVANCE_WEIGHTS = {
    "title": 5,
    "meta_title": 4,
    "handle": 3,
    "body_first_100": 3,
    "tags": 2,
    "meta_description": 2,
    "body_rest": 1,
    "image_alt": 1,
}


# ---------------------------------------------------------------------------
# Phase 1: Master Keyword List
# ---------------------------------------------------------------------------


def consolidate_master_keywords(store_id):
    """Merge semrush_keywords + gsc_daily_metrics into master_keywords."""
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()

    with db.get_cursor() as cur:
        # Create a run record
        cur.execute(
            "INSERT INTO keyword_strategy_runs (store_id, run_type, status) "
            "VALUES (%s, 'consolidate', 'running') RETURNING id",
            (store_id,),
        )
        run_id = cur.fetchone()["id"]

    try:
        with db.get_cursor() as cur:
            # Semrush keywords: best position + max volume per keyword
            cur.execute(
                """SELECT LOWER(keyword) AS keyword,
                          MAX(search_volume) AS search_volume,
                          MAX(cpc) AS cpc,
                          MAX(competition) AS competition,
                          MIN(position) FILTER (WHERE position > 0) AS best_position,
                          MAX(traffic) AS traffic,
                          MAX(url) AS url
                   FROM semrush_keywords
                   WHERE store_id = %s AND keyword IS NOT NULL AND keyword != ''
                   GROUP BY LOWER(keyword)""",
                (store_id,),
            )
            semrush_data = {row["keyword"]: dict(row) for row in cur.fetchall()}

            # GSC: aggregate last 30 days per query
            cur.execute(
                """SELECT LOWER(query) AS keyword,
                          SUM(clicks) AS clicks,
                          SUM(impressions) AS impressions,
                          CASE WHEN SUM(impressions) > 0
                               THEN SUM(clicks) / SUM(impressions) ELSE 0 END AS ctr,
                          MIN(position) FILTER (WHERE position > 0) AS best_position,
                          (ARRAY_AGG(page ORDER BY clicks DESC))[1] AS top_page
                   FROM gsc_daily_metrics
                   WHERE store_id = %s AND date >= %s
                     AND query IS NOT NULL AND query != ''
                   GROUP BY LOWER(query)""",
                (store_id, date_from),
            )
            gsc_data = {row["keyword"]: dict(row) for row in cur.fetchall()}

        # Merge
        all_keywords = set(semrush_data.keys()) | set(gsc_data.keys())
        merged = []
        counts = {"from_semrush": 0, "from_gsc": 0, "from_both": 0}

        for kw in all_keywords:
            sr = semrush_data.get(kw)
            gc = gsc_data.get(kw)

            if sr and gc:
                source = "both"
                counts["from_both"] += 1
            elif sr:
                source = "semrush"
                counts["from_semrush"] += 1
            else:
                source = "gsc"
                counts["from_gsc"] += 1

            search_volume = (sr or {}).get("search_volume") or 0
            cpc = (sr or {}).get("cpc")
            competition = (sr or {}).get("competition")
            clicks = (gc or {}).get("clicks") or 0
            impressions = (gc or {}).get("impressions") or 0
            ctr = (gc or {}).get("ctr") or 0

            # Best position from either source
            sr_pos = (sr or {}).get("best_position")
            gc_pos = (gc or {}).get("best_position")
            positions = [p for p in [sr_pos, gc_pos] if p and p > 0]
            best_position = min(positions) if positions else None

            # Best URL
            current_url = (gc or {}).get("top_page") or (sr or {}).get("url")

            intent = classify_intent(kw)

            merged.append((
                store_id, kw, search_volume, cpc, competition,
                best_position, clicks, impressions, ctr,
                intent, source, current_url,
            ))

        # Batch upsert
        with db.get_cursor() as cur:
            for row in merged:
                cur.execute(
                    """INSERT INTO master_keywords
                       (store_id, keyword, search_volume, cpc, competition,
                        best_position, clicks, impressions, ctr,
                        intent, source, current_url, intent_source, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, 'rules', NOW())
                       ON CONFLICT (store_id, keyword) DO UPDATE SET
                         search_volume = EXCLUDED.search_volume,
                         cpc = EXCLUDED.cpc,
                         competition = EXCLUDED.competition,
                         best_position = EXCLUDED.best_position,
                         clicks = EXCLUDED.clicks,
                         impressions = EXCLUDED.impressions,
                         ctr = EXCLUDED.ctr,
                         intent = CASE
                             WHEN master_keywords.intent_source IN ('ai', 'manual')
                             THEN master_keywords.intent
                             ELSE EXCLUDED.intent
                         END,
                         intent_source = CASE
                             WHEN master_keywords.intent_source IN ('ai', 'manual')
                             THEN master_keywords.intent_source
                             ELSE 'rules'
                         END,
                         source = CASE
                             WHEN master_keywords.source = 'manual' THEN 'manual'
                             ELSE EXCLUDED.source
                         END,
                         current_url = EXCLUDED.current_url,
                         updated_at = NOW()""",
                    row,
                )

            cur.execute(
                "UPDATE keyword_strategy_runs SET status='completed', "
                "completed_items=%s, total_items=%s, summary=%s, completed_at=NOW() "
                "WHERE id=%s",
                (len(merged), len(merged), json.dumps(counts), run_id),
            )

        return {"total": len(merged), **counts, "run_id": run_id}

    except Exception as e:
        logger.error("Keyword consolidation failed: %s", e)
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE keyword_strategy_runs SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
                (str(e), run_id),
            )
        raise


def add_manual_keyword(store_id, keyword, search_volume=None, intent=None):
    if not keyword or not keyword.strip():
        raise ValueError("Keyword is required")
    kw = keyword.strip().lower()
    # Only a caller-supplied intent is authoritative; a rules-derived fallback
    # must stay overridable by the AI classification pass.
    explicit_intent = bool(intent)
    if not intent:
        intent = classify_intent(kw)
    intent_source = "manual" if explicit_intent else "rules"
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO master_keywords (store_id, keyword, search_volume, intent, intent_source, source)
               VALUES (%s, %s, %s, %s, %s, 'manual')
               ON CONFLICT (store_id, keyword) DO UPDATE SET
                 search_volume = COALESCE(EXCLUDED.search_volume, master_keywords.search_volume),
                 intent = CASE
                     WHEN EXCLUDED.intent_source = 'manual' THEN EXCLUDED.intent
                     WHEN master_keywords.intent_source IN ('ai', 'manual') THEN master_keywords.intent
                     ELSE EXCLUDED.intent
                 END,
                 intent_source = CASE
                     WHEN EXCLUDED.intent_source = 'manual' THEN 'manual'
                     WHEN master_keywords.intent_source IN ('ai', 'manual') THEN master_keywords.intent_source
                     ELSE COALESCE(master_keywords.intent_source, 'rules')
                 END,
                 updated_at = NOW()
               RETURNING id""",
            (store_id, kw, search_volume, intent, intent_source),
        )
        return cur.fetchone()["id"]


def delete_keyword(store_id, keyword_id):
    with db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM content_keyword_map WHERE master_keyword_id = %s AND store_id = %s",
            (keyword_id, store_id),
        )
        cur.execute(
            "DELETE FROM keyword_assignments WHERE master_keyword_id = %s AND store_id = %s",
            (keyword_id, store_id),
        )
        cur.execute(
            "DELETE FROM master_keywords WHERE id = %s AND store_id = %s",
            (keyword_id, store_id),
        )


def update_keyword_intent(store_id, keyword_id, intent):
    if intent not in ("transactional", "commercial", "informational", "navigational"):
        raise ValueError("Invalid intent")
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE master_keywords SET intent = %s, intent_source = 'manual', "
            "updated_at = NOW() WHERE id = %s AND store_id = %s",
            (intent, keyword_id, store_id),
        )


def update_keyword_status(store_id, keyword_id, status):
    if status not in ("active", "ignored", "archived"):
        raise ValueError("Invalid status")
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE master_keywords SET status = %s, updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (status, keyword_id, store_id),
        )


def get_master_keywords(store_id, search=None, intent=None, source=None,
                        cluster_id=None, status=None, sort_by="search_volume",
                        sort_dir="desc", limit=100, offset=0):
    allowed_sorts = {
        "keyword", "search_volume", "best_position", "clicks",
        "impressions", "ctr", "cpc", "competition", "intent", "source",
    }
    if sort_by not in allowed_sorts:
        sort_by = "search_volume"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    conditions = ["mk.store_id = %s"]
    params = [store_id]

    if search:
        conditions.append("mk.keyword ILIKE %s")
        params.append(f"%{search}%")
    if intent:
        conditions.append("mk.intent = %s")
        params.append(intent)
    if source:
        conditions.append("mk.source = %s")
        params.append(source)
    if cluster_id:
        conditions.append("mk.cluster_id = %s")
        params.append(cluster_id)
    if status:
        conditions.append("mk.status = %s")
        params.append(status)
    else:
        conditions.append("mk.status = 'active'")

    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM master_keywords mk WHERE {where}",
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"""SELECT mk.*, mkc.name AS cluster_name,
                       ka.assigned_item_type, ka.assigned_item_id, ka.status AS assignment_status
                FROM master_keywords mk
                LEFT JOIN master_keyword_clusters mkc ON mk.cluster_id = mkc.id
                LEFT JOIN keyword_assignments ka ON ka.master_keyword_id = mk.id AND ka.store_id = mk.store_id
                WHERE {where}
                ORDER BY mk.{sort_by} {sort_dir} NULLS LAST
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        keywords = cur.fetchall()

    return {"keywords": keywords, "total": total}


def get_master_keyword_overview(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE status = 'active') AS total_active,
                 COUNT(*) AS total_all,
                 SUM(search_volume) FILTER (WHERE status = 'active') AS total_volume,
                 COUNT(*) FILTER (WHERE source = 'semrush' AND status = 'active') AS from_semrush,
                 COUNT(*) FILTER (WHERE source = 'gsc' AND status = 'active') AS from_gsc,
                 COUNT(*) FILTER (WHERE source = 'both' AND status = 'active') AS from_both,
                 COUNT(*) FILTER (WHERE source = 'manual' AND status = 'active') AS from_manual,
                 COUNT(*) FILTER (WHERE intent = 'transactional' AND status = 'active') AS intent_transactional,
                 COUNT(*) FILTER (WHERE intent = 'commercial' AND status = 'active') AS intent_commercial,
                 COUNT(*) FILTER (WHERE intent = 'informational' AND status = 'active') AS intent_informational,
                 COUNT(*) FILTER (WHERE intent = 'navigational' AND status = 'active') AS intent_navigational,
                 COUNT(*) FILTER (WHERE intent_source IN ('ai', 'manual') AND status = 'active') AS ai_classified,
                 COUNT(DISTINCT cluster_id) FILTER (WHERE cluster_id IS NOT NULL AND status = 'active') AS clusters_count
               FROM master_keywords WHERE store_id = %s""",
            (store_id,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Keyword Clustering
# ---------------------------------------------------------------------------


def cluster_keywords(store_id):
    """Group keywords by token-level Jaccard similarity."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, keyword, COALESCE(search_volume, 0) AS volume "
            "FROM master_keywords WHERE store_id = %s AND status = 'active'",
            (store_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return {"clusters": 0, "keywords": 0}

    volumes = {row["id"]: row["volume"] or 0 for row in rows}

    # Tokenize all keywords
    kw_tokens = {}
    for row in rows:
        tokens = set(_tokenize(row["keyword"]))
        if tokens:
            kw_tokens[row["id"]] = (row["keyword"], tokens)

    # Build clusters via union-find approach with Jaccard similarity
    parent = {}
    for kid in kw_tokens:
        parent[kid] = kid

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Inverted token index: Jaccard >= 0.5 requires at least one shared token,
    # so only candidate pairs sharing a token need comparing — pure-Python
    # all-pairs comparison hung for minutes at a few thousand keywords.
    token_index = {}
    for kid, (_, tokens) in kw_tokens.items():
        for tok in tokens:
            token_index.setdefault(tok, []).append(kid)

    compared = set()
    for tok, members in token_index.items():
        if len(members) < 2 or len(members) > 500:
            # a token shared by >500 keywords is a stop-word-like term; pairs
            # relying on it alone can't reach Jaccard 0.5 anyway for multi-token
            # keywords, and comparing them all would reintroduce the blowup
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = (a, b) if a < b else (b, a)
                if pair in compared:
                    continue
                compared.add(pair)
                t1 = kw_tokens[a][1]
                t2 = kw_tokens[b][1]
                intersection = len(t1 & t2)
                union_size = len(t1 | t2)
                if union_size > 0 and intersection / union_size >= 0.5:
                    union(a, b)

    # Group by cluster root
    clusters = {}
    for kid in kw_tokens:
        root = find(kid)
        if root not in clusters:
            clusters[root] = []
        clusters[root].append(kid)

    # Persist clusters
    with db.get_cursor() as cur:
        # Clear old clusters for this store
        cur.execute(
            "UPDATE master_keywords SET cluster_id = NULL WHERE store_id = %s",
            (store_id,),
        )
        cur.execute(
            "DELETE FROM master_keyword_clusters WHERE store_id = %s",
            (store_id,),
        )

        cluster_count = 0
        for root, member_ids in clusters.items():
            if len(member_ids) < 2:
                continue

            # Name by highest volume keyword
            best_kw = max(member_ids, key=lambda kid: volumes.get(kid, 0))
            cluster_name = kw_tokens[best_kw][0]
            total_vol = sum(volumes.get(kid, 0) for kid in member_ids)

            cur.execute(
                """INSERT INTO master_keyword_clusters (store_id, name, keyword_count, total_volume)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (store_id, name) DO UPDATE SET
                     keyword_count = EXCLUDED.keyword_count,
                     total_volume = EXCLUDED.total_volume
                   RETURNING id""",
                (store_id, cluster_name, len(member_ids), total_vol),
            )
            cluster_id = cur.fetchone()["id"]

            cur.execute(
                "UPDATE master_keywords SET cluster_id = %s WHERE store_id = %s AND id = ANY(%s)",
                (cluster_id, store_id, member_ids),
            )
            cluster_count += 1

    return {"clusters": cluster_count, "keywords": len(kw_tokens)}


def _get_kw_volume(store_id, keyword_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT search_volume FROM master_keywords WHERE id = %s", (keyword_id,))
        row = cur.fetchone()
        return (row["search_volume"] or 0) if row else 0


def get_clusters(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT mkc.*, COUNT(mk.id) AS actual_count
               FROM master_keyword_clusters mkc
               LEFT JOIN master_keywords mk ON mk.cluster_id = mkc.id AND mk.status = 'active'
               WHERE mkc.store_id = %s
               GROUP BY mkc.id
               ORDER BY mkc.total_volume DESC""",
            (store_id,),
        )
        return cur.fetchall()


def rename_cluster(store_id, cluster_id, name):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE master_keyword_clusters SET name = %s WHERE id = %s AND store_id = %s",
            (name, cluster_id, store_id),
        )


def merge_clusters(store_id, cluster_ids, new_name=None):
    if len(cluster_ids) < 2:
        raise ValueError("Need at least 2 clusters to merge")
    target_id = cluster_ids[0]
    with db.get_cursor() as cur:
        for cid in cluster_ids[1:]:
            cur.execute(
                "UPDATE master_keywords SET cluster_id = %s WHERE cluster_id = %s AND store_id = %s",
                (target_id, cid, store_id),
            )
            cur.execute(
                "DELETE FROM master_keyword_clusters WHERE id = %s AND store_id = %s",
                (cid, store_id),
            )
        # Update counts
        cur.execute(
            """UPDATE master_keyword_clusters SET
                 keyword_count = (SELECT COUNT(*) FROM master_keywords WHERE cluster_id = %s AND store_id = %s),
                 total_volume = COALESCE((SELECT SUM(search_volume) FROM master_keywords WHERE cluster_id = %s AND store_id = %s), 0)
               WHERE id = %s""",
            (target_id, store_id, target_id, store_id, target_id),
        )
        if new_name:
            cur.execute(
                "UPDATE master_keyword_clusters SET name = %s WHERE id = %s",
                (new_name, target_id),
            )


# ---------------------------------------------------------------------------
# Phase 2: Site-Wide Content Audit
# ---------------------------------------------------------------------------


def _get_all_content_items(store_id):
    """Fetch all content items for a store."""
    items = []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, title, handle, description, meta_title, meta_description,
                      tags, vendor, product_type, images
               FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'""",
            (store_id,),
        )
        for row in cur.fetchall():
            items.append(("product", dict(row)))

        cur.execute(
            """SELECT id, title, handle, description, meta_title, meta_description, image_url
               FROM synced_collections WHERE store_id = %s""",
            (store_id,),
        )
        for row in cur.fetchall():
            items.append(("collection", dict(row)))

        cur.execute(
            """SELECT id, title, handle, body, summary, tags, meta_title, meta_description,
                      image_url, image_alt
               FROM synced_articles WHERE store_id = %s""",
            (store_id,),
        )
        for row in cur.fetchall():
            items.append(("article", dict(row)))

        cur.execute(
            """SELECT id, title, handle, body_html, seo_title, seo_description
               FROM synced_pages WHERE store_id = %s""",
            (store_id,),
        )
        for row in cur.fetchall():
            items.append(("page", dict(row)))

    return items


def _get_text_fields(item_type, item):
    """Extract searchable text fields from a content item."""
    title = item.get("title") or ""
    handle = (item.get("handle") or "").replace("-", " ")
    meta_title = item.get("meta_title") or item.get("seo_title") or ""
    meta_description = item.get("meta_description") or item.get("seo_description") or ""
    tags = item.get("tags") or ""

    if item_type == "article":
        body_html = item.get("body") or ""
    elif item_type == "page":
        body_html = item.get("body_html") or ""
    else:
        body_html = item.get("description") or ""

    body_text = _strip_html(body_html)
    body_words = body_text.split()
    body_first_100 = " ".join(body_words[:100])
    body_rest = " ".join(body_words[100:])

    # Image alt texts
    image_alts = []
    if item_type == "product":
        images = item.get("images")
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except (ValueError, TypeError):
                images = []
        if isinstance(images, list):
            for img in images:
                alt = img.get("alt") or "" if isinstance(img, dict) else ""
                if alt:
                    image_alts.append(alt)
    elif item_type == "article":
        alt = item.get("image_alt") or ""
        if alt:
            image_alts.append(alt)

    return {
        "title": title,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "handle": handle,
        "tags": tags,
        "body_first_100": body_first_100,
        "body_rest": body_rest,
        "image_alt": " ".join(image_alts),
    }


def _build_item_text_index(items):
    """Pre-build lowercased text fields per item for fast lookup."""
    index = []
    for item_type, item in items:
        fields = _get_text_fields(item_type, item)
        lowered = {k: v.lower() for k, v in fields.items()}
        # Also build a combined text for quick substring pre-filter
        combined = " ".join(lowered.values())
        index.append((item_type, item["id"], lowered, combined))
    return index


def _match_keyword_to_items(kw, kw_tokens, item_index):
    """Match a single keyword against all items using pre-built index. Returns matches."""
    kw_lower = kw.lower()
    is_multi = len(kw_tokens) >= 2
    results = []

    for item_type, item_id, fields, combined in item_index:
        # Fast pre-filter: skip if keyword not even partially in combined text
        if kw_lower not in combined and (not is_multi or not kw_tokens.issubset(set(_tokenize(combined)))):
            continue

        found_in = []
        relevance = 0.0
        for field_name, weight in RELEVANCE_WEIGHTS.items():
            text = fields.get(field_name, "")
            if not text:
                continue
            if kw_lower in text:
                found_in.append(field_name)
                relevance += weight
            elif is_multi and kw_tokens.issubset(set(_tokenize(text))):
                found_in.append(field_name)
                relevance += weight

        if found_in:
            results.append((item_type, item_id, found_in, round(relevance, 2)))

    return results


def run_content_audit(store_id):
    """Run a full site-wide content audit as a background task."""
    # Prevent concurrent runs
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM keyword_strategy_runs "
            "WHERE store_id = %s AND run_type = 'audit' AND status = 'running'",
            (store_id,),
        )
        if cur.fetchone():
            return {"error": "Audit already running"}

        cur.execute(
            "INSERT INTO keyword_strategy_runs (store_id, run_type, status) "
            "VALUES (%s, 'audit', 'running') RETURNING id",
            (store_id,),
        )
        run_id = cur.fetchone()["id"]

    def _run():
        try:
            # Load master keywords
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT id, keyword FROM master_keywords WHERE store_id = %s AND status = 'active'",
                    (store_id,),
                )
                master_kw_list = cur.fetchall()

            if not master_kw_list:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE keyword_strategy_runs SET status='completed', "
                        "summary=%s, completed_at=NOW() WHERE id=%s",
                        (json.dumps({"error": "No active keywords. Run consolidation first."}), run_id),
                    )
                return

            items = _get_all_content_items(store_id)
            total_kw = len(master_kw_list)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET total_items = %s WHERE id = %s",
                    (total_kw, run_id),
                )

            # Clear old audit data
            with db.get_cursor() as cur:
                cur.execute(
                    "DELETE FROM content_keyword_map WHERE store_id = %s",
                    (store_id,),
                )

            # Pre-build text index for all items (done once)
            item_index = _build_item_text_index(items)
            logger.info("Audit: built text index for %d items, scanning %d keywords", len(item_index), total_kw)

            # Iterate keywords (not items) — keyword loop is the outer loop
            # Each keyword is checked against all items via the pre-built index
            completed = 0
            total_mapped = 0
            # Track primary per item: {(item_type, item_id): (best_relevance, best_kw_id)}
            primary_tracker = {}
            batch_rows = []

            for mk in master_kw_list:
                kw = mk["keyword"]
                kw_tokens = set(_tokenize(kw))
                matches = _match_keyword_to_items(kw, kw_tokens, item_index)

                for item_type, item_id, found_in, relevance in matches:
                    batch_rows.append((
                        store_id, mk["id"], item_type, item_id,
                        json.dumps(found_in), False, relevance,
                    ))
                    key = (item_type, item_id)
                    if key not in primary_tracker or relevance > primary_tracker[key][0]:
                        primary_tracker[key] = (relevance, mk["id"])
                    total_mapped += 1

                completed += 1
                if completed % 100 == 0:
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE keyword_strategy_runs SET completed_items = %s WHERE id = %s",
                            (completed, run_id),
                        )

                # Flush batch periodically to avoid huge memory
                if len(batch_rows) >= 5000:
                    _flush_audit_batch(batch_rows)
                    batch_rows = []

            # Flush remaining
            if batch_rows:
                _flush_audit_batch(batch_rows)

            # Set primary flags
            with db.get_cursor() as cur:
                for (item_type, item_id), (_, kw_id) in primary_tracker.items():
                    cur.execute(
                        "UPDATE content_keyword_map SET is_primary = TRUE "
                        "WHERE store_id = %s AND master_keyword_id = %s "
                        "AND item_type = %s AND item_id = %s",
                        (store_id, kw_id, item_type, item_id),
                    )

            summary = {
                "items_audited": len(items),
                "keywords_scanned": total_kw,
                "keywords_mapped": total_mapped,
                "products": sum(1 for t, _ in items if t == "product"),
                "collections": sum(1 for t, _ in items if t == "collection"),
                "articles": sum(1 for t, _ in items if t == "article"),
                "pages": sum(1 for t, _ in items if t == "page"),
            }

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET status='completed', "
                    "completed_items=%s, summary=%s, completed_at=NOW() WHERE id=%s",
                    (completed, json.dumps(summary), run_id),
                )

        except Exception as e:
            logger.error("Content audit failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
                    (str(e), run_id),
                )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"run_id": run_id}


def _flush_audit_batch(batch_rows):
    """Insert a batch of content_keyword_map rows."""
    if not batch_rows:
        return
    with db.get_cursor() as cur:
        for row in batch_rows:
            cur.execute(
                """INSERT INTO content_keyword_map
                   (store_id, master_keyword_id, item_type, item_id,
                    found_in, is_primary, relevance_score)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (store_id, master_keyword_id, item_type, item_id)
                   DO UPDATE SET
                     found_in = EXCLUDED.found_in,
                     is_primary = EXCLUDED.is_primary,
                     relevance_score = EXCLUDED.relevance_score""",
                row,
            )


def get_audit_status(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT * FROM keyword_strategy_runs
               WHERE store_id = %s AND run_type = 'audit'
               ORDER BY started_at DESC LIMIT 1""",
            (store_id,),
        )
        return cur.fetchone()


def get_audit_results(store_id, item_type=None, search=None,
                      sort_by="keyword_count", sort_dir="desc",
                      limit=50, offset=0):
    """Get audit results grouped by content item."""
    allowed_sorts = {
        "title": "title",
        "item_type": "item_type",
        "primary_keyword": "primary_keyword",
        "primary_volume": "primary_volume",
        "keyword_count": "keyword_count",
    }
    sort_col = allowed_sorts.get(sort_by, "keyword_count")
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    conditions = []
    params = []

    if item_type:
        conditions.append("a.item_type = %s")
        params.append(item_type)
    if search:
        conditions.append("a.title ILIKE %s")
        params.append(f"%{search}%")

    where = " AND ".join(conditions) if conditions else "TRUE"

    with db.get_cursor() as cur:
        # Single efficient query: aggregate in a subquery, join titles via UNION
        cur.execute(
            f"""WITH audit_items AS (
                    SELECT ckm.item_type, ckm.item_id,
                           COUNT(*) AS keyword_count,
                           MAX(ckm.relevance_score) AS max_relevance
                    FROM content_keyword_map ckm
                    WHERE ckm.store_id = %s
                    GROUP BY ckm.item_type, ckm.item_id
                ),
                titles AS (
                    SELECT id, 'product' AS item_type, title, store_id FROM synced_products WHERE store_id = %s
                    UNION ALL
                    SELECT id, 'collection', title, store_id FROM synced_collections WHERE store_id = %s
                    UNION ALL
                    SELECT id, 'article', title, store_id FROM synced_articles WHERE store_id = %s
                    UNION ALL
                    SELECT id, 'page', title, store_id FROM synced_pages WHERE store_id = %s
                ),
                enriched AS (
                    SELECT ai.item_type, ai.item_id, t.title,
                           ai.keyword_count, ai.max_relevance,
                           pmk.keyword AS primary_keyword,
                           pmk.search_volume AS primary_volume
                    FROM audit_items ai
                    JOIN titles t ON t.item_type = ai.item_type AND t.id = ai.item_id
                    LEFT JOIN content_keyword_map pk
                        ON pk.store_id = %s AND pk.item_type = ai.item_type
                        AND pk.item_id = ai.item_id AND pk.is_primary = TRUE
                    LEFT JOIN master_keywords pmk ON pmk.id = pk.master_keyword_id
                )
                SELECT * FROM enriched a
                WHERE {where}
                ORDER BY {sort_col} {sort_dir} NULLS LAST
                LIMIT %s OFFSET %s""",
            (store_id, store_id, store_id, store_id, store_id, store_id) + tuple(params) + (limit, offset),
        )
        items = cur.fetchall()

        # Count total
        cur.execute(
            f"""WITH audit_items AS (
                    SELECT ckm.item_type, ckm.item_id
                    FROM content_keyword_map ckm
                    WHERE ckm.store_id = %s
                    GROUP BY ckm.item_type, ckm.item_id
                ),
                titles AS (
                    SELECT id, 'product' AS item_type, title, store_id FROM synced_products WHERE store_id = %s
                    UNION ALL
                    SELECT id, 'collection', title, store_id FROM synced_collections WHERE store_id = %s
                    UNION ALL
                    SELECT id, 'article', title, store_id FROM synced_articles WHERE store_id = %s
                    UNION ALL
                    SELECT id, 'page', title, store_id FROM synced_pages WHERE store_id = %s
                )
                SELECT COUNT(*) AS total FROM (
                    SELECT ai.item_type, ai.item_id, t.title
                    FROM audit_items ai
                    JOIN titles t ON t.item_type = ai.item_type AND t.id = ai.item_id
                ) a
                WHERE {where}""",
            (store_id, store_id, store_id, store_id, store_id) + tuple(params),
        )
        total = cur.fetchone()["total"]

    return {"items": items, "total": total}


def _get_item_title(item_type, item_id):
    table_map = {
        "product": "synced_products",
        "collection": "synced_collections",
        "article": "synced_articles",
        "page": "synced_pages",
    }
    table = table_map.get(item_type)
    if not table:
        return None
    with db.get_cursor() as cur:
        cur.execute(f"SELECT title FROM {table} WHERE id = %s", (item_id,))
        row = cur.fetchone()
        return row["title"] if row else None


def get_item_audit(store_id, item_type, item_id):
    """Get full keyword audit for a specific content item."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ckm.*, mk.keyword, mk.search_volume, mk.best_position,
                      mk.clicks, mk.impressions, mk.intent
               FROM content_keyword_map ckm
               JOIN master_keywords mk ON mk.id = ckm.master_keyword_id
               WHERE ckm.store_id = %s AND ckm.item_type = %s AND ckm.item_id = %s
               ORDER BY ckm.relevance_score DESC""",
            (store_id, item_type, item_id),
        )
        found = cur.fetchall()

    title = _get_item_title(item_type, item_id)
    return {"item_type": item_type, "item_id": item_id, "title": title, "keywords": found}


def get_cannibalization_report(store_id, limit=100, offset=0):
    """Find keywords that are primary for 2+ content items."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT mk.id AS keyword_id, mk.keyword, mk.search_volume, mk.best_position,
                      COUNT(*) AS competing_pages
               FROM content_keyword_map ckm
               JOIN master_keywords mk ON mk.id = ckm.master_keyword_id
               WHERE ckm.store_id = %s AND ckm.is_primary = TRUE
               GROUP BY mk.id, mk.keyword, mk.search_volume, mk.best_position
               HAVING COUNT(*) >= 2
               ORDER BY mk.search_volume DESC NULLS LAST""",
            (store_id,),
        )
        cannibalized = cur.fetchall()

    total = len(cannibalized)
    page = cannibalized[offset:offset + limit]

    results = []
    for row in page:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT ckm.item_type, ckm.item_id, ckm.relevance_score, ckm.found_in
                   FROM content_keyword_map ckm
                   WHERE ckm.store_id = %s AND ckm.master_keyword_id = %s AND ckm.is_primary = TRUE
                   ORDER BY ckm.relevance_score DESC""",
                (store_id, row["keyword_id"]),
            )
            pages = cur.fetchall()

        enriched_pages = []
        for p in pages:
            title = _get_item_title(p["item_type"], p["item_id"])
            enriched_pages.append({**dict(p), "title": title})

        results.append({
            **dict(row),
            "pages": enriched_pages,
            "severity": "high" if (row["search_volume"] or 0) > 100 else "medium",
        })

    return {"items": results, "total": total}


def get_uncovered_keywords(store_id, min_volume=0, limit=100, offset=0):
    """Keywords with no content targeting them."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT mk.*
               FROM master_keywords mk
               LEFT JOIN content_keyword_map ckm ON ckm.master_keyword_id = mk.id
               WHERE mk.store_id = %s AND mk.status = 'active'
                 AND mk.search_volume >= %s
                 AND ckm.id IS NULL
               ORDER BY mk.search_volume DESC NULLS LAST""",
            (store_id, min_volume),
        )
        rows = cur.fetchall()

    total = len(rows)
    return {"keywords": rows[offset:offset + limit], "total": total}


# ---------------------------------------------------------------------------
# Phase 3: Optimal Keyword Assignment Plan
# ---------------------------------------------------------------------------


def generate_assignment_plan(store_id):
    """Generate keyword-to-page assignments using rule-based logic + AI refinement."""
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_strategy_runs (store_id, run_type, status) "
            "VALUES (%s, 'plan', 'running') RETURNING id",
            (store_id,),
        )
        run_id = cur.fetchone()["id"]

    def _run():
        try:
            with db.get_cursor() as cur:
                # Get all active keywords with their audit data
                cur.execute(
                    """SELECT mk.*,
                              (SELECT json_agg(json_build_object(
                                  'item_type', ckm.item_type,
                                  'item_id', ckm.item_id,
                                  'is_primary', ckm.is_primary,
                                  'relevance_score', ckm.relevance_score,
                                  'found_in', ckm.found_in
                              )) FROM content_keyword_map ckm
                              WHERE ckm.master_keyword_id = mk.id AND ckm.store_id = mk.store_id
                              ) AS content_matches
                       FROM master_keywords mk
                       WHERE mk.store_id = %s AND mk.status = 'active'
                       ORDER BY mk.search_volume DESC NULLS LAST""",
                    (store_id,),
                )
                keywords = cur.fetchall()

            if not keywords:
                with db.get_cursor() as cur:
                    cur.execute(
                        "UPDATE keyword_strategy_runs SET status='completed', "
                        "summary=%s, completed_at=NOW() WHERE id=%s",
                        (json.dumps({"error": "No active keywords"}), run_id),
                    )
                return

            total = len(keywords)
            assigned = 0

            # Resolver ownership: a keyword that is already a page's resolved
            # primary must be assigned to that page, never moved by the plan.
            from app.services.keyword_resolver import get_owned_primary_keywords
            from app.services.keyword_resolver import variant_key as _variant_key
            ownership = get_owned_primary_keywords(store_id)

            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET total_items = %s WHERE id = %s",
                    (total, run_id),
                )

            for mk in keywords:
                matches = mk.get("content_matches") or []
                if isinstance(matches, str):
                    try:
                        matches = json.loads(matches)
                    except (ValueError, TypeError):
                        matches = []

                kw = mk["keyword"]
                word_count = len(kw.split())
                intent = mk["intent"] or "commercial"
                volume = mk["search_volume"] or 0

                # Rule-based assignment
                best_match = None
                reason = ""
                priority = "medium"
                cannibalization_items = []

                if matches:
                    # Sort by relevance
                    matches.sort(key=lambda m: m.get("relevance_score", 0), reverse=True)

                    # Filter by content type preference
                    preferred_type = _preferred_content_type(kw, word_count, intent)
                    type_matches = [m for m in matches if m["item_type"] == preferred_type]
                    fallback_matches = matches

                    candidates = type_matches if type_matches else fallback_matches

                    if candidates:
                        best_match = candidates[0]
                        reason = f"Best {preferred_type} match by relevance ({best_match.get('relevance_score', 0)})"

                        if volume > 500:
                            priority = "high"
                        elif volume > 100:
                            priority = "medium"
                        else:
                            priority = "low"

                        # Identify cannibalization
                        if len(matches) > 1:
                            for m in matches:
                                if m["item_type"] == best_match["item_type"] and m["item_id"] == best_match["item_id"]:
                                    continue
                                action = "de-optimize" if m.get("is_primary") else "differentiate"
                                cannibalization_items.append({
                                    "item_type": m["item_type"],
                                    "item_id": m["item_id"],
                                    "action": action,
                                })

                owner = ownership.get(_variant_key(kw))
                if owner and (not best_match
                              or (owner["item_type"], owner["item_id"])
                              != (best_match["item_type"], best_match["item_id"])):
                    best_match = {"item_type": owner["item_type"], "item_id": owner["item_id"]}
                    reason = "Already this page's resolved primary — the plan follows keyword-strategy ownership"
                    cannibalization_items = [
                        c for c in cannibalization_items
                        if (c["item_type"], c["item_id"]) != (owner["item_type"], owner["item_id"])
                    ]

                if best_match:
                    with db.get_cursor() as cur:
                        cur.execute(
                            """INSERT INTO keyword_assignments
                               (store_id, master_keyword_id, assigned_item_type, assigned_item_id,
                                assignment_reason, priority, status, cannibalization_items)
                               VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s)
                               ON CONFLICT (store_id, master_keyword_id) DO UPDATE SET
                                 assigned_item_type = EXCLUDED.assigned_item_type,
                                 assigned_item_id = EXCLUDED.assigned_item_id,
                                 assignment_reason = EXCLUDED.assignment_reason,
                                 priority = EXCLUDED.priority,
                                 cannibalization_items = EXCLUDED.cannibalization_items,
                                 status = 'proposed',
                                 updated_at = NOW()""",
                            (
                                store_id, mk["id"],
                                best_match["item_type"], best_match["item_id"],
                                reason, priority,
                                json.dumps(cannibalization_items),
                            ),
                        )
                    assigned += 1

                if assigned % 50 == 0:
                    with db.get_cursor() as cur:
                        cur.execute(
                            "UPDATE keyword_strategy_runs SET completed_items = %s WHERE id = %s",
                            (assigned, run_id),
                        )

            summary = {"total_keywords": total, "assigned": assigned, "unassigned": total - assigned}
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET status='completed', "
                    "completed_items=%s, summary=%s, completed_at=NOW() WHERE id=%s",
                    (assigned, json.dumps(summary), run_id),
                )

        except Exception as e:
            logger.error("Assignment plan generation failed: %s", e)
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET status='failed', error=%s, completed_at=NOW() WHERE id=%s",
                    (str(e), run_id),
                )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"run_id": run_id}


def _preferred_content_type(keyword, word_count, intent):
    """Determine preferred content type for a keyword."""
    kw_lower = keyword.lower()

    # Informational → articles
    if intent == "informational":
        return "article"

    # Check for product signals (sizes, flavors, packs)
    product_signals = re.compile(
        r'\b(\d+\s*(oz|pack|ct|count|lb|gram|ml|bag|box|can|roll|piece|mg)s?'
        r'|small|medium|large|xl|xxl|single|double|triple'
        r'|flavor|variety|vanilla|chocolate|strawberry|mint)\b',
        re.IGNORECASE,
    )
    if product_signals.search(kw_lower):
        return "product"

    # Short head terms → collections
    if word_count <= 2 and intent in ("commercial", "transactional"):
        return "collection"

    # Long-tail commercial → products
    if word_count >= 3 and intent == "transactional":
        return "product"

    # Default: collections for high-volume, products for specific
    if word_count <= 2:
        return "collection"
    return "product"


def get_assignment_plan(store_id, status=None, item_type=None,
                        priority=None, search=None, limit=100, offset=0):
    conditions = ["ka.store_id = %s"]
    params = [store_id]

    if status:
        conditions.append("ka.status = %s")
        params.append(status)
    if item_type:
        conditions.append("ka.assigned_item_type = %s")
        params.append(item_type)
    if priority:
        conditions.append("ka.priority = %s")
        params.append(priority)
    if search:
        conditions.append("mk.keyword ILIKE %s")
        params.append(f"%{search}%")

    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM keyword_assignments ka "
            f"JOIN master_keywords mk ON mk.id = ka.master_keyword_id WHERE {where}",
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"""SELECT ka.*, mk.keyword, mk.search_volume, mk.best_position,
                       mk.clicks, mk.impressions, mk.intent
                FROM keyword_assignments ka
                JOIN master_keywords mk ON mk.id = ka.master_keyword_id
                WHERE {where}
                ORDER BY mk.search_volume DESC NULLS LAST
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        assignments = cur.fetchall()

    # Enrich with titles and cannibalization details
    enriched = []
    for a in assignments:
        a = dict(a)
        if a.get("assigned_item_type") and a.get("assigned_item_id"):
            a["assigned_title"] = _get_item_title(a["assigned_item_type"], a["assigned_item_id"])
        cannibs = a.get("cannibalization_items") or []
        if isinstance(cannibs, str):
            try:
                cannibs = json.loads(cannibs)
            except (ValueError, TypeError):
                cannibs = []
        for item in cannibs:
            item["title"] = _get_item_title(item.get("item_type"), item.get("item_id")) or f'{item.get("item_type", "")}#{item.get("item_id", "")}'
        a["cannibalization_items"] = cannibs
        # Parse proposed_changes if string
        pc = a.get("proposed_changes") or {}
        if isinstance(pc, str):
            try:
                pc = json.loads(pc)
            except (ValueError, TypeError):
                pc = {}
        a["proposed_changes"] = pc
        enriched.append(a)

    return {"assignments": enriched, "total": total}


def accept_assignment(store_id, assignment_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_assignments SET status = 'accepted', updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (assignment_id, store_id),
        )


def reject_assignment(store_id, assignment_id):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_assignments SET status = 'rejected', updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (assignment_id, store_id),
        )


def bulk_accept_assignments(store_id, assignment_ids):
    with db.get_cursor() as cur:
        for aid in assignment_ids:
            cur.execute(
                "UPDATE keyword_assignments SET status = 'accepted', updated_at = NOW() "
                "WHERE id = %s AND store_id = %s",
                (aid, store_id),
            )
    return {"accepted": len(assignment_ids)}


# ---------------------------------------------------------------------------
# Phase 4: Fix Application
# ---------------------------------------------------------------------------


def generate_fixes_for_assignment(store_id, assignment_id):
    """Generate proposed changes for an assignment using AI."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ka.*, mk.keyword, mk.search_volume, mk.intent
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ka.id = %s AND ka.store_id = %s""",
            (assignment_id, store_id),
        )
        assignment = cur.fetchone()

    if not assignment:
        raise ValueError("Assignment not found")

    item_type = assignment["assigned_item_type"]
    item_id = assignment["assigned_item_id"]
    keyword = assignment["keyword"]

    # Get current content
    current = _get_item_content(item_type, item_id)
    if not current:
        raise ValueError("Content item not found")

    # Get secondary keywords (other keywords mapped to this item)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT mk.keyword FROM content_keyword_map ckm
               JOIN master_keywords mk ON mk.id = ckm.master_keyword_id
               WHERE ckm.store_id = %s AND ckm.item_type = %s AND ckm.item_id = %s
                 AND mk.keyword != %s
               ORDER BY ckm.relevance_score DESC LIMIT 5""",
            (store_id, item_type, item_id, keyword),
        )
        secondary = [r["keyword"] for r in cur.fetchall()]

    # Simple rule-based fix generation (AI integration added in Phase 3 of implementation)
    proposed = {}
    title = current.get("title") or ""
    meta_title = current.get("meta_title") or current.get("seo_title") or ""
    meta_desc = current.get("meta_description") or current.get("seo_description") or ""

    # Check if keyword is missing from meta_title
    if not _keyword_in_text(keyword, meta_title):
        if meta_title:
            proposed["meta_title"] = f"{keyword.title()} - {meta_title}"[:60]
        else:
            proposed["meta_title"] = f"{keyword.title()} | {title}"[:60]

    # Check if keyword is missing from meta_description
    if not _keyword_in_text(keyword, meta_desc):
        if meta_desc:
            proposed["meta_description"] = f"{keyword.title()}. {meta_desc}"[:160]
        else:
            proposed["meta_description"] = f"Shop {keyword} - {title}. Find the best {keyword} options."[:160]

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_assignments SET proposed_changes = %s, updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (json.dumps(proposed), assignment_id, store_id),
        )

    return {"proposed_changes": proposed, "keyword": keyword, "secondary": secondary}


def generate_implementation_steps(store_id, assignment_id):
    """Generate AI-powered implementation steps for a keyword assignment."""
    from openai import OpenAI
    from app.auth import decrypt_value

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ka.*, mk.keyword, mk.search_volume, mk.intent,
                      mk.best_position, mk.clicks, mk.impressions
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ka.id = %s AND ka.store_id = %s""",
            (assignment_id, store_id),
        )
        assignment = cur.fetchone()

    if not assignment:
        raise ValueError("Assignment not found")

    keyword = assignment["keyword"]
    intent = assignment["intent"] or "commercial"
    volume = assignment["search_volume"] or 0
    position = assignment["best_position"]
    item_type = assignment["assigned_item_type"]
    item_id = assignment["assigned_item_id"]

    # Get target page content
    target_content = _get_item_content(item_type, item_id)
    if not target_content:
        raise ValueError("Target content item not found")

    target_title = target_content.get("title") or ""
    target_meta_title = target_content.get("meta_title") or target_content.get("seo_title") or ""
    target_meta_desc = target_content.get("meta_description") or target_content.get("seo_description") or ""
    target_handle = target_content.get("handle") or ""
    target_tags = target_content.get("tags") or ""
    target_body = _strip_html(target_content.get("description") or target_content.get("body") or target_content.get("body_html") or "")[:500]

    # Get secondary keywords for target page
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT mk.keyword FROM content_keyword_map ckm
               JOIN master_keywords mk ON mk.id = ckm.master_keyword_id
               WHERE ckm.store_id = %s AND ckm.item_type = %s AND ckm.item_id = %s
                 AND mk.keyword != %s
               ORDER BY ckm.relevance_score DESC LIMIT 5""",
            (store_id, item_type, item_id, keyword),
        )
        secondary_keywords = [r["keyword"] for r in cur.fetchall()]

    # Get cannibalization items with their content
    cannibs_raw = assignment.get("cannibalization_items") or []
    if isinstance(cannibs_raw, str):
        try:
            cannibs_raw = json.loads(cannibs_raw)
        except (ValueError, TypeError):
            cannibs_raw = []

    def _stored_primary(c_type, c_id):
        if c_type not in ("product", "collection"):
            return ""
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                       FROM technical_seo_analyses
                       WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                    (store_id, c_type, c_id),
                )
                row = cur.fetchone()
            return (row or {}).get("kw") or ""
        except Exception:
            return ""

    competing_pages = []
    for c in cannibs_raw:
        c_type = c.get("item_type")
        c_id = c.get("item_id")
        c_action = c.get("action", "differentiate")
        c_content = _get_item_content(c_type, c_id)
        if c_content:
            competing_pages.append({
                "item_type": c_type,
                "item_id": c_id,
                "action": c_action,
                "title": c_content.get("title") or "",
                "meta_title": c_content.get("meta_title") or c_content.get("seo_title") or "",
                "meta_description": c_content.get("meta_description") or c_content.get("seo_description") or "",
                "handle": c_content.get("handle") or "",
                "tags": c_content.get("tags") or "",
                "body_snippet": _strip_html(c_content.get("description") or c_content.get("body") or c_content.get("body_html") or "")[:300],
                "resolved_primary": _stored_primary(c_type, c_id),
            })

    # Build AI prompt
    target_section = (
        f"TARGET PAGE (assign keyword here):\n"
        f"  Type: {item_type}\n"
        f"  ID: {item_id}\n"
        f"  Title: {target_title}\n"
        f"  Handle: {target_handle}\n"
        f"  Meta Title: {target_meta_title or '(empty)'}\n"
        f"  Meta Description: {target_meta_desc or '(empty)'}\n"
        f"  Tags: {target_tags or '(none)'}\n"
        f"  Body Preview: {target_body or '(empty)'}\n"
        f"  Secondary Keywords: {', '.join(secondary_keywords) if secondary_keywords else '(none)'}"
    )

    competing_section = ""
    if competing_pages:
        competing_section = "\nCOMPETING PAGES (currently also targeting this keyword):\n"
        for i, cp in enumerate(competing_pages, 1):
            competing_section += (
                f"\n  Page {i}:\n"
                f"    Type: {cp['item_type']}\n"
                f"    ID: {cp['item_id']}\n"
                f"    Title: {cp['title']}\n"
                f"    Handle: {cp['handle']}\n"
                f"    Meta Title: {cp['meta_title'] or '(empty)'}\n"
                f"    Meta Description: {cp['meta_description'] or '(empty)'}\n"
                f"    Tags: {cp['tags'] or '(none)'}\n"
                f"    Body Preview: {cp['body_snippet'] or '(empty)'}\n"
                # When the competing page's stored primary IS the contested
                # keyword, the ownership move is the point — don't protect it.
                + (f"    OWN PRIMARY KEYWORD (protected — never remove or weaken it on this page): \"{cp['resolved_primary']}\"\n"
                   if cp.get("resolved_primary")
                   and cp["resolved_primary"].strip().lower() != (keyword or "").strip().lower() else "")
                + f"    Recommended Action: {cp['action']}\n"
            )

    system_prompt = (
        "You are an expert Shopify SEO strategist. Generate a concrete implementation plan "
        "to assign a keyword to a target page and resolve any keyword cannibalization.\n\n"
        "For each step, specify the exact field to change, the current value, and the proposed new value.\n\n"
        "Rules:\n"
        "- meta_title must be under 60 characters\n"
        "- meta_description must be under 160 characters\n"
        "- Include the primary keyword naturally, don't just prepend it\n"
        "- For de-optimize steps, replace the keyword with a more specific long-tail variant or remove it\n"
        "- For differentiate steps, shift focus to a related but distinct keyword\n"
        "- A competing page's OWN PRIMARY KEYWORD is protected: de-optimize/differentiate steps must "
        "only touch the contested keyword, never that page's own primary\n"
        "- Only propose changes to fields that actually need modification\n"
        "- Be specific with proposed values — write the actual text, not placeholders\n\n"
        "Return ONLY valid JSON in this exact format:\n"
        "{\n"
        '  "summary": "Brief 1-2 sentence strategy explanation",\n'
        '  "steps": [\n'
        "    {\n"
        '      "step_number": 1,\n'
        '      "action": "optimize|de-optimize|differentiate",\n'
        '      "item_type": "product|collection|article|page",\n'
        '      "item_id": 123,\n'
        '      "title": "Page Title",\n'
        '      "field": "meta_title|meta_description|title|tags",\n'
        '      "current_value": "current text",\n'
        '      "proposed_value": "new optimized text",\n'
        '      "explanation": "Why this change is needed"\n'
        "    }\n"
        "  ]\n"
        "}"
    )

    user_prompt = (
        f"KEYWORD: {keyword}\n"
        f"Search Volume: {volume}\n"
        f"Intent: {intent}\n"
        f"Current Position: {position or 'Not ranking'}\n\n"
        f"{target_section}\n\n"
        f"{competing_section}\n\n"
        "Generate the implementation steps. Optimize the target page for this keyword, "
        "and de-optimize or differentiate the competing pages to resolve cannibalization. "
        "Return JSON only."
    )

    # Call AI
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_config ORDER BY id LIMIT 1")
        ai_config = cur.fetchone()

    if not ai_config or not ai_config.get("api_key_enc"):
        raise ValueError("AI not configured. Please set your OpenRouter API key in Settings.")

    api_key = decrypt_value(ai_config["api_key_enc"])
    model = ai_config.get("model") or "anthropic/claude-sonnet-4"

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("AI returned invalid JSON")

    steps = result.get("steps") or []
    summary = result.get("summary") or ""

    # Validate and clean steps
    valid_fields = {"meta_title", "meta_description", "title", "tags"}
    cleaned_steps = []
    for s in steps:
        if s.get("field") not in valid_fields:
            continue
        s["applied"] = False
        cleaned_steps.append(s)

    proposed_changes = {"summary": summary, "steps": cleaned_steps}

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_assignments SET proposed_changes = %s, updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (json.dumps(proposed_changes), assignment_id, store_id),
        )

    return proposed_changes


def apply_single_step(store_id, assignment_id, step_index):
    """Apply a single implementation step to Shopify."""
    from app.shopify_client import DEFAULT_API_VERSION, ShopifyClient
    from app.auth import decrypt_value

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ka.*, s.domain, s.access_token_enc, s.api_version
               FROM keyword_assignments ka
               JOIN stores s ON s.id = ka.store_id
               WHERE ka.id = %s AND ka.store_id = %s""",
            (assignment_id, store_id),
        )
        assignment = cur.fetchone()

    if not assignment:
        raise ValueError("Assignment not found")

    proposed = assignment.get("proposed_changes") or {}
    if isinstance(proposed, str):
        try:
            proposed = json.loads(proposed)
        except (ValueError, TypeError):
            raise ValueError("No implementation steps found")

    steps = proposed.get("steps") or []
    if step_index < 0 or step_index >= len(steps):
        raise ValueError(f"Invalid step index: {step_index}")

    step = steps[step_index]
    if step.get("applied"):
        return {"already_applied": True}

    s_item_type = step["item_type"]
    s_item_id = step["item_id"]
    field = step["field"]
    new_value = step["proposed_value"]
    old_value = step.get("current_value") or ""

    if s_item_type == "page" and field == "tags":
        return {"error": "Pages have no tags field"}

    # Get the Shopify ID for this item
    content = _get_item_content(s_item_type, s_item_id)
    if not content:
        raise ValueError(f"Content item not found: {s_item_type}#{s_item_id}")

    shopify_id = content.get("shopify_id")
    if not shopify_id:
        raise ValueError("No Shopify ID for content item")

    # Build update payload
    update_fields = {}
    if field == "meta_title":
        update_fields["meta_title"] = new_value
    elif field == "meta_description":
        update_fields["meta_description"] = new_value
    elif field == "title":
        update_fields["title"] = new_value
    elif field == "tags":
        update_fields["tags"] = new_value

    guard_shopify_fields(update_fields)
    new_value = update_fields.get(field, new_value)

    # Apply to Shopify
    access_token = decrypt_value(assignment["access_token_enc"])
    client = ShopifyClient(
        assignment["domain"], access_token, assignment["api_version"] or DEFAULT_API_VERSION
    )

    if s_item_type == "product":
        client.update_product(shopify_id, update_fields)
    elif s_item_type == "collection":
        client.update_collection(shopify_id, update_fields)
    elif s_item_type == "article":
        if field in ("meta_title", "meta_description"):
            client.set_article_seo(
                shopify_id,
                update_fields.get("meta_title"),
                update_fields.get("meta_description"),
            )
        else:
            client.update_article(shopify_id, update_fields)
    elif s_item_type == "page":
        if field == "meta_title":
            update_fields = {"seo_title": new_value}
        elif field == "meta_description":
            update_fields = {"seo_description": new_value}
        client.update_page(shopify_id, update_fields)

    # Record in audit trail
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO keyword_strategy_changes
               (store_id, assignment_id, item_type, item_id, field_name, old_value, new_value)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (store_id, assignment_id, s_item_type, s_item_id, field, old_value, new_value),
        )

    # Update local synced table
    table_map = {
        "product": "synced_products",
        "collection": "synced_collections",
        "article": "synced_articles",
        "page": "synced_pages",
    }
    table = table_map.get(s_item_type)
    if table:
        col = field
        if s_item_type == "page" and field == "meta_title":
            col = "seo_title"
        elif s_item_type == "page" and field == "meta_description":
            col = "seo_description"
        known_cols = {
            "product": {"meta_title", "meta_description", "title", "tags"},
            "collection": {"meta_title", "meta_description", "title"},
            "article": {"meta_title", "meta_description", "title", "tags"},
            "page": {"seo_title", "seo_description", "title"},
        }
        if col in known_cols.get(s_item_type, set()):
            with db.get_cursor() as cur:
                cur.execute(f"UPDATE {table} SET {col} = %s WHERE id = %s", (new_value, s_item_id))

    # Mark step as applied in proposed_changes
    steps[step_index]["applied"] = True
    all_applied = all(s.get("applied") for s in steps)

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_assignments SET proposed_changes = %s, "
            "status = CASE WHEN %s THEN 'applied' ELSE status END, "
            "updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (json.dumps(proposed), all_applied, assignment_id, store_id),
        )

    from app.services.analysis_refresh import refresh_item_analysis
    refresh_item_analysis(store_id, s_item_type, s_item_id)

    return {"applied": True, "field": field, "item_type": s_item_type, "all_done": all_applied}


def _get_item_content(item_type, item_id):
    """Get current content fields for an item."""
    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
        elif item_type == "collection":
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
        elif item_type == "article":
            cur.execute("SELECT * FROM synced_articles WHERE id = %s", (item_id,))
        elif item_type == "page":
            cur.execute("SELECT * FROM synced_pages WHERE id = %s", (item_id,))
        else:
            return None
        return cur.fetchone()


def preview_fix(store_id, assignment_id):
    """Return diff between current and proposed values."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ka.*, mk.keyword
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ka.id = %s AND ka.store_id = %s""",
            (assignment_id, store_id),
        )
        assignment = cur.fetchone()

    if not assignment:
        return None

    current = _get_item_content(assignment["assigned_item_type"], assignment["assigned_item_id"])
    if not current:
        return None

    proposed = assignment.get("proposed_changes") or {}
    if isinstance(proposed, str):
        try:
            proposed = json.loads(proposed)
        except (ValueError, TypeError):
            proposed = {}

    diffs = []
    for field, new_value in proposed.items():
        old_value = current.get(field) or current.get(f"seo_{field}") or ""
        if old_value != new_value:
            diffs.append({
                "field": field,
                "old_value": old_value,
                "new_value": new_value,
            })

    return {
        "assignment_id": assignment_id,
        "keyword": assignment["keyword"],
        "item_type": assignment["assigned_item_type"],
        "item_id": assignment["assigned_item_id"],
        "title": current.get("title"),
        "diffs": diffs,
    }


def apply_fix(store_id, assignment_id):
    """Apply proposed changes to Shopify and record in audit trail."""
    from app.shopify_client import DEFAULT_API_VERSION, ShopifyClient

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ka.*, mk.keyword, s.domain, s.access_token_enc, s.api_version
               FROM keyword_assignments ka
               JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               JOIN stores s ON s.id = ka.store_id
               WHERE ka.id = %s AND ka.store_id = %s""",
            (assignment_id, store_id),
        )
        assignment = cur.fetchone()

    if not assignment:
        raise ValueError("Assignment not found")

    proposed = assignment.get("proposed_changes") or {}
    if isinstance(proposed, str):
        try:
            proposed = json.loads(proposed)
        except (ValueError, TypeError):
            proposed = {}

    if not proposed:
        raise ValueError("No proposed changes to apply")

    item_type = assignment["assigned_item_type"]
    item_id = assignment["assigned_item_id"]
    current = _get_item_content(item_type, item_id)
    if not current:
        raise ValueError("Content item not found")

    shopify_id = current.get("shopify_id")
    if not shopify_id:
        raise ValueError("No Shopify ID found for item")

    # Build update payload
    from app.auth import decrypt_value
    access_token = decrypt_value(assignment["access_token_enc"])
    client = ShopifyClient(
        assignment["domain"], access_token, assignment["api_version"] or DEFAULT_API_VERSION
    )

    update_fields = {}
    for field, value in proposed.items():
        if field == "meta_title":
            update_fields["meta_title"] = value
        elif field == "meta_description":
            update_fields["meta_description"] = value
        elif field == "title":
            update_fields["title"] = value
        elif field == "description":
            update_fields["description_html"] = value
        elif field == "tags":
            update_fields["tags"] = value

    guard_shopify_fields(update_fields)
    if "meta_title" in update_fields:
        proposed["meta_title"] = update_fields["meta_title"]
    if "meta_description" in update_fields:
        proposed["meta_description"] = update_fields["meta_description"]
    if "description_html" in update_fields:
        proposed["description"] = update_fields["description_html"]

    if update_fields:
        if item_type == "product":
            client.update_product(shopify_id, update_fields)
        elif item_type == "collection":
            client.update_collection(shopify_id, update_fields)
        elif item_type == "article":
            client.update_article(shopify_id, update_fields)
        elif item_type == "page":
            page_fields = dict(update_fields)
            if "meta_title" in page_fields:
                page_fields["seo_title"] = page_fields.pop("meta_title")
            if "meta_description" in page_fields:
                page_fields["seo_description"] = page_fields.pop("meta_description")
            client.update_page(shopify_id, page_fields)

    # Record the audit trail only now that the Shopify push succeeded — rows
    # for changes that never happened would make revert overwrite live values.
    with db.get_cursor() as cur:
        for field, new_value in proposed.items():
            old_value = current.get(field) or current.get(f"seo_{field}") or ""
            cur.execute(
                """INSERT INTO keyword_strategy_changes
                   (store_id, assignment_id, item_type, item_id, field_name, old_value, new_value)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (store_id, assignment_id, item_type, item_id, field, str(old_value), new_value),
            )

    # Update local DB
    with db.get_cursor() as cur:
        if item_type == "product":
            for field, value in proposed.items():
                if field in ("meta_title", "meta_description", "title", "tags", "description"):
                    cur.execute(
                        f"UPDATE synced_products SET {field} = %s WHERE id = %s",
                        (value, item_id),
                    )
        elif item_type == "collection":
            for field, value in proposed.items():
                if field in ("meta_title", "meta_description", "title", "description"):
                    cur.execute(
                        f"UPDATE synced_collections SET {field} = %s WHERE id = %s",
                        (value, item_id),
                    )
        elif item_type == "article":
            for field, value in proposed.items():
                if field in ("meta_title", "meta_description", "title"):
                    cur.execute(
                        f"UPDATE synced_articles SET {field} = %s WHERE id = %s",
                        (value, item_id),
                    )
        elif item_type == "page":
            page_col_map = {
                "meta_title": "seo_title",
                "meta_description": "seo_description",
                "title": "title",
            }
            for field, value in proposed.items():
                col = page_col_map.get(field)
                if col:
                    cur.execute(
                        f"UPDATE synced_pages SET {col} = %s WHERE id = %s",
                        (value, item_id),
                    )

        # Mark assignment as applied
        cur.execute(
            "UPDATE keyword_assignments SET status = 'applied', updated_at = NOW() "
            "WHERE id = %s AND store_id = %s",
            (assignment_id, store_id),
        )

    from app.services.analysis_refresh import refresh_item_analysis
    refresh_item_analysis(store_id, item_type, item_id)

    return {"applied": True, "fields": list(proposed.keys())}


def bulk_apply_fixes(store_id, assignment_ids):
    """Apply fixes for multiple assignments as a background task."""
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO keyword_strategy_runs (store_id, run_type, status, total_items) "
            "VALUES (%s, 'apply', 'running', %s) RETURNING id",
            (store_id, len(assignment_ids)),
        )
        run_id = cur.fetchone()["id"]

    def _run():
        completed = 0
        failed = 0
        first_error = None
        for aid in assignment_ids:
            try:
                apply_fix(store_id, aid)
                completed += 1
            except Exception as e:
                logger.error("Failed to apply fix %s: %s", aid, e)
                failed += 1
                if first_error is None:
                    first_error = f"assignment {aid}: {e}"
            with db.get_cursor() as cur:
                cur.execute(
                    "UPDATE keyword_strategy_runs SET completed_items=%s, failed_items=%s WHERE id=%s",
                    (completed, failed, run_id),
                )

        final_status = "failed" if failed and not completed else "completed"
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE keyword_strategy_runs SET status=%s, error=%s, "
                "completed_items=%s, failed_items=%s, completed_at=NOW() WHERE id=%s",
                (final_status, first_error, completed, failed, run_id),
            )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"run_id": run_id}


def revert_fix(store_id, change_id):
    """Revert a previously applied change."""
    from app.shopify_client import DEFAULT_API_VERSION, ShopifyClient
    from app.auth import decrypt_value

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT ksc.*, s.domain, s.access_token_enc, s.api_version
               FROM keyword_strategy_changes ksc
               JOIN stores s ON s.id = ksc.store_id
               WHERE ksc.id = %s AND ksc.store_id = %s AND ksc.reverted_at IS NULL""",
            (change_id, store_id),
        )
        change = cur.fetchone()

    if not change:
        raise ValueError("Change not found or already reverted")

    item = _get_item_content(change["item_type"], change["item_id"])
    if not item:
        raise ValueError("Content item not found")

    shopify_id = item.get("shopify_id")
    access_token = decrypt_value(change["access_token_enc"])
    client = ShopifyClient(
        change["domain"], access_token, change["api_version"] or DEFAULT_API_VERSION
    )

    field = change["field_name"]
    old_value = change["old_value"]

    update_fields = {}
    if field == "meta_title":
        update_fields["meta_title"] = old_value
    elif field == "meta_description":
        update_fields["meta_description"] = old_value
    elif field == "title":
        update_fields["title"] = old_value
    elif field == "description":
        update_fields["description_html"] = old_value

    if update_fields:
        if change["item_type"] == "product":
            client.update_product(shopify_id, update_fields)
        elif change["item_type"] == "collection":
            client.update_collection(shopify_id, update_fields)
        elif change["item_type"] == "article":
            client.update_article(shopify_id, update_fields)
        elif change["item_type"] == "page":
            page_fields = dict(update_fields)
            if "meta_title" in page_fields:
                page_fields["seo_title"] = page_fields.pop("meta_title")
            if "meta_description" in page_fields:
                page_fields["seo_description"] = page_fields.pop("meta_description")
            client.update_page(shopify_id, page_fields)

        table_map = {
            "product": "synced_products",
            "collection": "synced_collections",
            "article": "synced_articles",
            "page": "synced_pages",
        }
        col_maps = {
            "product": {
                "meta_title": "meta_title",
                "meta_description": "meta_description",
                "title": "title",
                "description": "description",
            },
            "collection": {
                "meta_title": "meta_title",
                "meta_description": "meta_description",
                "title": "title",
                "description": "description",
            },
            "article": {
                "meta_title": "meta_title",
                "meta_description": "meta_description",
                "title": "title",
            },
            "page": {
                "meta_title": "seo_title",
                "meta_description": "seo_description",
                "title": "title",
            },
        }
        col = col_maps.get(change["item_type"], {}).get(field)
        if col:
            table = table_map[change["item_type"]]
            with db.get_cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET {col} = %s WHERE id = %s",
                    (old_value, change["item_id"]),
                )

    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE keyword_strategy_changes SET reverted_at = NOW() WHERE id = %s",
            (change_id,),
        )

    return {"reverted": True, "field": field}


def get_changes(store_id, limit=100, offset=0):
    """Get applied changes audit trail."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS total FROM keyword_strategy_changes WHERE store_id = %s",
            (store_id,),
        )
        total = cur.fetchone()["total"]

        cur.execute(
            """SELECT ksc.*, mk.keyword
               FROM keyword_strategy_changes ksc
               LEFT JOIN keyword_assignments ka ON ka.id = ksc.assignment_id
               LEFT JOIN master_keywords mk ON mk.id = ka.master_keyword_id
               WHERE ksc.store_id = %s
               ORDER BY ksc.applied_at DESC
               LIMIT %s OFFSET %s""",
            (store_id, limit, offset),
        )
        changes = cur.fetchall()

    # Enrich with titles (batched — one query per item type)
    changes = [dict(c) for c in changes]
    ids_by_type = {}
    for c in changes:
        ids_by_type.setdefault(c["item_type"], set()).add(c["item_id"])
    table_map = {
        "product": "synced_products",
        "collection": "synced_collections",
        "article": "synced_articles",
        "page": "synced_pages",
    }
    titles = {}
    with db.get_cursor() as cur:
        for item_type, ids in ids_by_type.items():
            table = table_map.get(item_type)
            if not table or not ids:
                continue
            cur.execute(
                f"SELECT id, title FROM {table} WHERE id = ANY(%s)",
                (list(ids),),
            )
            for r in cur.fetchall():
                titles[(item_type, r["id"])] = r["title"]
    for c in changes:
        c["title"] = titles.get((c["item_type"], c["item_id"]))

    return {"changes": changes, "total": total}


def get_runs(store_id, limit=20):
    """Get keyword strategy run history."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT * FROM keyword_strategy_runs
               WHERE store_id = %s
               ORDER BY started_at DESC LIMIT %s""",
            (store_id, limit),
        )
        return cur.fetchall()


def get_run(store_id, run_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT * FROM keyword_strategy_runs WHERE id = %s AND store_id = %s",
            (run_id, store_id),
        )
        return cur.fetchone()
