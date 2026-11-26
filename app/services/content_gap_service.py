import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app.database import db

logger = logging.getLogger(__name__)


def check_cannibalization_risk(store_id, keyword):
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = today.isoformat()
    kw_lower = keyword.lower()
    existing_pages = []

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT page, SUM(clicks) as clicks, SUM(impressions) as impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND LOWER(query) = %s AND date >= %s AND date <= %s
               GROUP BY page HAVING SUM(impressions) >= 1
               ORDER BY impressions DESC""",
            (store_id, kw_lower, date_from, date_to),
        )
        for r in cur.fetchall():
            existing_pages.append({
                "url": r["page"],
                "source": "gsc",
                "position": round(float(r["avg_position"] or 0), 1),
                "impressions": float(r["impressions"] or 0),
                "clicks": float(r["clicks"] or 0),
            })

        cur.execute(
            "SELECT DISTINCT url, position FROM semrush_keywords WHERE store_id = %s AND LOWER(keyword) = %s AND url IS NOT NULL AND url != ''",
            (store_id, kw_lower),
        )
        gsc_urls = {p["url"] for p in existing_pages}
        for r in cur.fetchall():
            if r["url"] not in gsc_urls:
                existing_pages.append({
                    "url": r["url"],
                    "source": "semrush",
                    "position": r["position"],
                    "impressions": 0,
                    "clicks": 0,
                })

        kw_pattern = f"%{kw_lower}%"
        cur.execute(
            """SELECT 'product' as type, id, title, handle FROM synced_products
               WHERE store_id = %s AND status = 'ACTIVE'
                 AND (LOWER(title) LIKE %s OR LOWER(meta_title) LIKE %s)
               LIMIT 10""",
            (store_id, kw_pattern, kw_pattern),
        )
        on_site_matches = [{"type": r["type"], "id": r["id"], "title": r["title"], "handle": r["handle"]} for r in cur.fetchall()]

        cur.execute(
            """SELECT 'collection' as type, id, title, handle FROM synced_collections
               WHERE store_id = %s AND (LOWER(title) LIKE %s OR LOWER(meta_title) LIKE %s)
               LIMIT 10""",
            (store_id, kw_pattern, kw_pattern),
        )
        on_site_matches.extend([{"type": r["type"], "id": r["id"], "title": r["title"], "handle": r["handle"]} for r in cur.fetchall()])

    has_ranking_pages = len(existing_pages) > 0
    has_on_site_matches = len(on_site_matches) > 0
    top_position = min((p["position"] for p in existing_pages if p["position"]), default=999)

    if has_ranking_pages and top_position <= 10:
        risk_level = "high"
        recommendation = "A page already ranks in the top 10 for this keyword. Creating new content will likely cannibalize it. Optimize the existing page instead."
    elif has_ranking_pages or (has_on_site_matches and len(on_site_matches) >= 2):
        risk_level = "medium"
        recommendation = "Existing pages target this keyword. Consider differentiating with a long-tail variation or optimizing the existing page."
    elif has_on_site_matches:
        risk_level = "low"
        recommendation = "A related page exists but doesn't strongly target this keyword. Proceed with caution and ensure clear differentiation."
    else:
        risk_level = "none"
        recommendation = "No cannibalization risk detected. Safe to create new content."

    return {
        "risk_level": risk_level,
        "existing_pages": existing_pages,
        "on_site_matches": on_site_matches,
        "recommendation": recommendation,
    }


INTENT_TRANSACTIONAL = {"buy", "price", "cheap", "deal", "discount", "order", "shop", "for sale", "coupon", "wholesale", "bulk"}
INTENT_COMMERCIAL = {"best", "top", "review", "vs", "compare", "alternative", "recommended", "worth it", "pros cons"}
INTENT_INFORMATIONAL = {"how to", "what is", "guide", "tutorial", "why", "tips", "ideas", "meaning", "difference between", "can you", "does"}

INTENT_MULTIPLIERS = {"transactional": 4, "commercial": 3, "informational": 1.5, "navigational": 1}

CONTENT_TYPE_SIGNALS = {
    "product": {"buy", "price", "cheap", "order", "shop", "for sale", "pack", "box", "case"},
    "collection": {"best", "top", "types", "brands", "flavors", "styles", "varieties"},
    "blog": {"how to", "guide", "tutorial", "what is", "why", "tips", "ideas", "review"},
    "guide": {"vs", "compare", "alternative", "difference", "pros cons", "worth it"},
}


def _has_signal(kw, term):
    """Word-boundary match — raw substring matching classified 'ideal gift'
    as transactional ('deal') and 'suitcase' as a product ('case')."""
    return re.search(rf"\b{re.escape(term)}\b", kw) is not None


def classify_intent(keyword):
    kw = keyword.lower()
    for term in INTENT_TRANSACTIONAL:
        if _has_signal(kw, term):
            return "transactional"
    for term in INTENT_COMMERCIAL:
        if _has_signal(kw, term):
            return "commercial"
    for term in INTENT_INFORMATIONAL:
        if _has_signal(kw, term):
            return "informational"
    return "commercial"


def _suggest_content_type(keyword):
    kw = keyword.lower()
    for content_type, signals in CONTENT_TYPE_SIGNALS.items():
        for signal in signals:
            if _has_signal(kw, signal):
                return content_type
    return "collection"


def _match_to_catalog(store_id, keyword):
    pattern = f"%{keyword.lower()}%"
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT id, 'product' as type, title FROM synced_products
               WHERE store_id = %s AND status = 'ACTIVE'
                 AND (LOWER(title) LIKE %s OR LOWER(tags) LIKE %s OR LOWER(product_type) LIKE %s)
               LIMIT 1""",
            (store_id, pattern, pattern, pattern),
        )
        product = cur.fetchone()
        if product:
            return {"product_id": product["id"], "collection_id": None, "matched": product["title"]}

        cur.execute(
            """SELECT id, title FROM synced_collections
               WHERE store_id = %s AND (LOWER(title) LIKE %s)
               LIMIT 1""",
            (store_id, pattern),
        )
        collection = cur.fetchone()
        if collection:
            return {"product_id": None, "collection_id": collection["id"], "matched": collection["title"]}

    return {"product_id": None, "collection_id": None, "matched": None}


def discover_gaps(store_id):
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = today.isoformat()

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT keyword, position, search_volume, cpc, competition, url, competitor_positions "
            "FROM semrush_keywords WHERE store_id = %s AND source_type = 'keyword_gap'",
            (store_id,),
        )
        gap_keywords = cur.fetchall()

        cur.execute(
            """SELECT query, SUM(impressions) as impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND date >= %s AND date <= %s
               GROUP BY query""",
            (store_id, date_from, date_to),
        )
        gsc_queries = {r["query"].lower(): r for r in cur.fetchall()}

    gaps_to_upsert = []

    for kw in gap_keywords:
        keyword = kw["keyword"]
        volume = kw["search_volume"] or 0
        cpc = float(kw["cpc"]) if kw["cpc"] else 0
        competition = float(kw["competition"]) if kw["competition"] else 0.5
        our_pos = kw["position"]
        our_url = kw["url"] or ""
        comp_pos = kw["competitor_positions"]

        gsc = gsc_queries.get(keyword.lower())

        if our_pos is None or our_pos > 100:
            gap_type = "missing"
        elif our_pos > 20:
            gap_type = "weak"
        elif gsc and gsc["avg_position"] and float(gsc["avg_position"]) > 15:
            # Ranked per Semrush but GSC shows the page slipping past 15.
            gap_type = "declining"
        else:
            gap_type = "weak"

        intent = classify_intent(keyword)
        intent_mult = INTENT_MULTIPLIERS.get(intent, 1)

        catalog_match = _match_to_catalog(store_id, keyword)
        relevance_boost = 1.5 if catalog_match["matched"] else 1.0

        difficulty = max(competition * 100, 1)
        opportunity_score = volume * (1 / difficulty) * intent_mult * relevance_boost

        content_type = _suggest_content_type(keyword)

        gaps_to_upsert.append({
            "keyword": keyword,
            "search_volume": volume,
            "cpc": cpc,
            "competition": competition,
            "intent": intent,
            "gap_type": gap_type,
            "our_position": our_pos,
            "our_url": our_url,
            "competitor_positions": json.dumps(comp_pos) if comp_pos else None,
            "opportunity_score": round(opportunity_score, 2),
            "suggested_content_type": content_type,
            "matched_product_id": catalog_match["product_id"],
            "matched_collection_id": catalog_match["collection_id"],
        })

    with db.get_cursor() as cur:
        for g in gaps_to_upsert:
            cur.execute(
                """INSERT INTO content_gaps
                   (store_id, keyword, search_volume, cpc, competition, intent, gap_type,
                    our_position, our_url, competitor_positions, opportunity_score,
                    suggested_content_type, matched_product_id, matched_collection_id, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (store_id, keyword) DO UPDATE SET
                     search_volume = EXCLUDED.search_volume, cpc = EXCLUDED.cpc,
                     competition = EXCLUDED.competition, intent = EXCLUDED.intent,
                     gap_type = EXCLUDED.gap_type, our_position = EXCLUDED.our_position,
                     our_url = EXCLUDED.our_url, competitor_positions = EXCLUDED.competitor_positions,
                     opportunity_score = EXCLUDED.opportunity_score,
                     suggested_content_type = EXCLUDED.suggested_content_type,
                     matched_product_id = EXCLUDED.matched_product_id,
                     matched_collection_id = EXCLUDED.matched_collection_id,
                     updated_at = NOW()""",
                (store_id, g["keyword"], g["search_volume"], g["cpc"], g["competition"],
                 g["intent"], g["gap_type"], g["our_position"], g["our_url"],
                 g["competitor_positions"], g["opportunity_score"], g["suggested_content_type"],
                 g["matched_product_id"], g["matched_collection_id"]),
            )

    logger.info("Content gap discovery: %d gaps for store %d", len(gaps_to_upsert), store_id)
    return {"gaps_found": len(gaps_to_upsert)}


def get_overview(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) as total,
                      SUM(search_volume) as total_volume,
                      AVG(opportunity_score) as avg_score,
                      COUNT(*) FILTER (WHERE gap_type = 'missing') as missing,
                      COUNT(*) FILTER (WHERE gap_type = 'weak') as weak,
                      COUNT(*) FILTER (WHERE gap_type = 'declining') as declining,
                      COUNT(*) FILTER (WHERE intent = 'transactional') as transactional,
                      COUNT(*) FILTER (WHERE intent = 'commercial') as commercial,
                      COUNT(*) FILTER (WHERE intent = 'informational') as informational,
                      COUNT(*) FILTER (WHERE status = 'new') as status_new,
                      COUNT(*) FILTER (WHERE status = 'briefed') as status_briefed,
                      COUNT(*) FILTER (WHERE status = 'in_progress') as status_in_progress,
                      COUNT(*) FILTER (WHERE status = 'published') as status_published,
                      COUNT(*) FILTER (WHERE status = 'dismissed') as status_dismissed
               FROM content_gaps WHERE store_id = %s""",
            (store_id,),
        )
        row = cur.fetchone()

    return {
        "total": row["total"],
        "total_volume": int(row["total_volume"] or 0),
        "avg_score": round(float(row["avg_score"] or 0), 1),
        "by_type": {"missing": row["missing"], "weak": row["weak"], "declining": row["declining"]},
        "by_intent": {"transactional": row["transactional"], "commercial": row["commercial"], "informational": row["informational"]},
        "by_status": {"new": row["status_new"], "briefed": row["status_briefed"],
                      "in_progress": row["status_in_progress"], "published": row["status_published"],
                      "dismissed": row["status_dismissed"]},
    }


def get_gaps(store_id, gap_type=None, intent=None, status=None, sort_by="opportunity_score",
             sort_dir="desc", limit=10000, offset=0, search=None):
    valid_sorts = {"opportunity_score": "opportunity_score", "search_volume": "search_volume",
                   "our_position": "our_position", "keyword": "keyword", "cpc": "cpc"}
    order_col = valid_sorts.get(sort_by, "opportunity_score")
    direction = "DESC" if sort_dir == "desc" else "ASC"
    nulls = "NULLS LAST" if direction == "DESC" else "NULLS FIRST"

    conditions = ["store_id = %s"]
    params = [store_id]
    if gap_type:
        conditions.append("gap_type = %s")
        params.append(gap_type)
    if intent:
        conditions.append("intent = %s")
        params.append(intent)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if search:
        conditions.append("keyword ILIKE %s")
        params.append(f"%{search}%")

    where = " AND ".join(conditions)

    with db.get_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) as total FROM content_gaps WHERE {where}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"""SELECT id, keyword, search_volume, cpc, competition, intent, gap_type,
                       our_position, our_url, competitor_positions, opportunity_score,
                       suggested_content_type, matched_product_id, matched_collection_id,
                       status, brief, created_at
                FROM content_gaps WHERE {where}
                ORDER BY {order_col} {direction} {nulls}
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return {
        "total": total,
        "gaps": [
            {
                "id": r["id"],
                "keyword": r["keyword"],
                "search_volume": r["search_volume"],
                "cpc": float(r["cpc"]) if r["cpc"] else None,
                "competition": float(r["competition"]) if r["competition"] else None,
                "intent": r["intent"],
                "gap_type": r["gap_type"],
                "our_position": r["our_position"],
                "our_url": r["our_url"],
                "competitor_positions": r["competitor_positions"],
                "opportunity_score": float(r["opportunity_score"] or 0),
                "suggested_content_type": r["suggested_content_type"],
                "matched_product_id": r["matched_product_id"],
                "matched_collection_id": r["matched_collection_id"],
                "status": r["status"],
                "has_brief": r["brief"] is not None,
                "brief": r["brief"],
            }
            for r in rows
        ],
    }


def get_gap(store_id, gap_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM content_gaps WHERE id = %s AND store_id = %s", (gap_id, store_id))
        r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "keyword": r["keyword"],
        "search_volume": r["search_volume"],
        "cpc": float(r["cpc"]) if r["cpc"] else None,
        "competition": float(r["competition"]) if r["competition"] else None,
        "intent": r["intent"],
        "gap_type": r["gap_type"],
        "our_position": r["our_position"],
        "our_url": r["our_url"],
        "competitor_positions": r["competitor_positions"],
        "opportunity_score": float(r["opportunity_score"] or 0),
        "suggested_content_type": r["suggested_content_type"],
        "matched_product_id": r["matched_product_id"],
        "matched_collection_id": r["matched_collection_id"],
        "status": r["status"],
        "brief": r["brief"],
    }


def update_gap_status(store_id, gap_id, status):
    valid = {"new", "briefed", "in_progress", "published", "dismissed"}
    if status not in valid:
        raise ValueError(f"Invalid status: {status}")
    with db.get_cursor() as cur:
        cur.execute("UPDATE content_gaps SET status = %s, updated_at = NOW() WHERE id = %s AND store_id = %s",
                    (status, gap_id, store_id))


def save_brief(store_id, gap_id, brief_data):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE content_gaps SET brief = %s, status = 'briefed', updated_at = NOW() WHERE id = %s AND store_id = %s",
            (json.dumps(brief_data), gap_id, store_id),
        )


def delete_gap(store_id, gap_id):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM content_gaps WHERE id = %s AND store_id = %s", (gap_id, store_id))


def generate_brief(store_id, gap_id):
    from app.auth import decrypt_value
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS
    from openai import OpenAI

    gap = get_gap(store_id, gap_id)
    if not gap:
        raise ValueError("Gap not found")

    cannibal_risk = check_cannibalization_risk(store_id, gap["keyword"])

    with db.get_cursor() as cur:
        cur.execute("SELECT api_key_enc, model FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI not configured")

    api_key = decrypt_value(config["api_key_enc"])
    model = config.get("model") or "anthropic/claude-sonnet-4"

    keyword = gap["keyword"].lower()
    kw_words = [w for w in keyword.split() if len(w) > 2]

    products = []
    collections = []
    detected_brand = None

    with db.get_cursor() as cur:
        cur.execute("SELECT DISTINCT LOWER(vendor) as vendor FROM synced_products WHERE store_id = %s AND vendor IS NOT NULL", (store_id,))
        all_vendors = {r["vendor"] for r in cur.fetchall()}

        for vendor in all_vendors:
            if vendor in keyword or keyword in vendor:
                detected_brand = vendor
                break
        if not detected_brand:
            for vendor in all_vendors:
                for word in kw_words:
                    if word == vendor or (len(word) >= 3 and word in vendor):
                        detected_brand = vendor
                        break
                if detected_brand:
                    break

        if detected_brand:
            cur.execute(
                "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' AND LOWER(vendor) = %s LIMIT 15",
                (store_id, detected_brand),
            )
            products = [{"title": r["title"], "handle": r["handle"], "score": 20} for r in cur.fetchall()]

            cur.execute(
                "SELECT title, handle FROM synced_collections WHERE store_id = %s AND LOWER(title) LIKE %s LIMIT 5",
                (store_id, f"%{detected_brand}%"),
            )
            collections = [{"title": r["title"], "handle": r["handle"], "score": 20} for r in cur.fetchall()]

        existing_handles = {p["handle"] for p in products}
        existing_col_handles = {c["handle"] for c in collections}

        full_pattern = f"%{keyword}%"
        cur.execute(
            """SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'
               AND (LOWER(title) LIKE %s OR LOWER(tags) LIKE %s OR LOWER(product_type) LIKE %s
                    OR LOWER(vendor) LIKE %s OR LOWER(meta_title) LIKE %s)
               LIMIT 20""",
            (store_id, full_pattern, full_pattern, full_pattern, full_pattern, full_pattern),
        )
        for r in cur.fetchall():
            if r["handle"] not in existing_handles:
                products.append({"title": r["title"], "handle": r["handle"], "score": 10})
                existing_handles.add(r["handle"])

        cur.execute(
            """SELECT title, handle FROM synced_collections WHERE store_id = %s
               AND (LOWER(title) LIKE %s OR LOWER(meta_title) LIKE %s OR LOWER(description) LIKE %s)
               LIMIT 10""",
            (store_id, full_pattern, full_pattern, full_pattern),
        )
        for r in cur.fetchall():
            if r["handle"] not in existing_col_handles:
                collections.append({"title": r["title"], "handle": r["handle"], "score": 10})
                existing_col_handles.add(r["handle"])

        if len(products) < 8 and len(kw_words) > 1:
            for word in kw_words:
                if len(products) >= 15:
                    break
                wp = f"%{word}%"
                cur.execute(
                    """SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'
                       AND (LOWER(title) LIKE %s OR LOWER(tags) LIKE %s OR LOWER(product_type) LIKE %s)
                       LIMIT 10""",
                    (store_id, wp, wp, wp),
                )
                for r in cur.fetchall():
                    if r["handle"] not in existing_handles:
                        products.append({"title": r["title"], "handle": r["handle"], "score": 5})
                        existing_handles.add(r["handle"])

        if len(collections) < 4 and len(kw_words) > 1:
            for word in kw_words:
                if len(collections) >= 8:
                    break
                wp = f"%{word}%"
                cur.execute(
                    """SELECT title, handle FROM synced_collections WHERE store_id = %s
                       AND (LOWER(title) LIKE %s OR LOWER(meta_title) LIKE %s)
                       LIMIT 5""",
                    (store_id, wp, wp),
                )
                for r in cur.fetchall():
                    if r["handle"] not in existing_col_handles:
                        collections.append({"title": r["title"], "handle": r["handle"], "score": 5})
                        existing_col_handles.add(r["handle"])

        products.sort(key=lambda x: x.get("score", 0), reverse=True)
        collections.sort(key=lambda x: x.get("score", 0), reverse=True)

    related_gaps = []
    with db.get_cursor() as cur:
        if kw_words:
            gap_clauses = " OR ".join(["keyword ILIKE %s"] * len(kw_words))
            gap_params = [f"%{w}%" for w in kw_words]
            cur.execute(
                f"SELECT keyword, search_volume FROM content_gaps WHERE store_id = %s AND ({gap_clauses}) AND id != %s ORDER BY search_volume DESC LIMIT 10",
                [store_id] + gap_params + [gap_id],
            )
            related_gaps = [r["keyword"] for r in cur.fetchall()]

    comp_info = ""
    if gap["competitor_positions"]:
        comp = gap["competitor_positions"] if isinstance(gap["competitor_positions"], dict) else json.loads(gap["competitor_positions"])
        comp_info = "\n".join([f"  - {domain}: position {pos}" for domain, pos in comp.items() if pos])

    target_links = min(max(5, len(products) + len(collections)), 15)
    prod_links = products[:min(10, target_links - min(len(collections), 5))]
    col_links = collections[:min(5, target_links - len(prod_links))]

    internal_links = "\n".join([f"  - /products/{p['handle']} ({p['title']})" for p in prod_links])
    internal_links += "\n" + "\n".join([f"  - /collections/{c['handle']} ({c['title']})" for c in col_links])

    cannibal_context = ""
    if cannibal_risk["risk_level"] in ("high", "medium"):
        pages_desc = "\n".join([f"  - {p['url']} (position {p['position']}, source: {p['source']})" for p in cannibal_risk["existing_pages"][:5]])
        matches_desc = "\n".join([f"  - /{m['type']}s/{m['handle']} ({m['title']})" for m in cannibal_risk["on_site_matches"][:5]])
        cannibal_context = (
            f"\n\nCANNIBALIZATION WARNING: The following pages on this site already rank or target this keyword:\n"
            f"{pages_desc}\n{matches_desc}\n\n"
            f"IMPORTANT: To avoid cannibalization, the new content brief MUST:\n"
            f"- Use a DIFFERENT primary angle or long-tail variation of the keyword\n"
            f"- Link TO the existing ranking page(s) rather than competing with them\n"
            f"- Target a different search intent or content format than existing pages\n"
            f"- Focus on unique subtopics not covered by existing pages\n"
        )

    prompt_template = get_setting("prompts.content_brief_generation", DEFAULTS.get("prompts.content_brief_generation", ""))
    prompt = prompt_template.format(
        keyword=gap["keyword"],
        volume=gap["search_volume"] or 0,
        cpc=gap["cpc"] or 0,
        intent=gap["intent"] or "commercial",
        our_position=gap["our_position"] or "not ranking",
        gap_type=gap["gap_type"],
        content_type=gap["suggested_content_type"] or "page",
        competitor_info=comp_info or "No competitor data",
        related_keywords=", ".join(related_gaps[:8]) if related_gaps else "none found",
        internal_links=internal_links,
    ) + cannibal_context

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key, timeout=120.0, max_retries=1)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        temperature=0.3,
    )
    content = response.choices[0].message.content or ""
    start = content.find("{")
    end = content.rfind("}") + 1
    if start >= 0 and end > start:
        brief_data = json.loads(content[start:end])
    else:
        brief_data = {"raw_response": content}

    brief_data["cannibalization_risk"] = cannibal_risk

    save_brief(store_id, gap_id, brief_data)
    return brief_data


def generate_content(store_id, gap_id, selected_title, content_type):
    from app.auth import decrypt_value
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS
    from openai import OpenAI

    gap = get_gap(store_id, gap_id)
    if not gap:
        raise ValueError("Gap not found")
    brief = gap.get("brief") or {}

    with db.get_cursor() as cur:
        cur.execute("SELECT api_key_enc, model FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI not configured")

    api_key = decrypt_value(config["api_key_enc"])
    model = config.get("model") or "anthropic/claude-sonnet-4"

    headings = brief.get("heading_structure", [])
    heading_text = "\n".join([f"{'  ' if h.get('tag') == 'h3' else ''}{h.get('tag', 'h2').upper()}: {h.get('text', '')}" for h in headings])
    secondary_kws = ", ".join(brief.get("secondary_keywords", [])[:10])
    questions = "\n".join([f"- {q}" for q in brief.get("questions_to_answer", [])[:5]])
    internal_links = "\n".join([f"- {l}" if isinstance(l, str) else f"- {l.get('url', l)}" for l in brief.get("internal_links", [])[:8]])
    word_count = brief.get("target_word_count", 1500)

    if content_type == "collection":
        prompt_key = "prompts.collection_description"
    elif content_type == "blog":
        prompt_key = "prompts.article_generate"
    else:
        prompt_key = "prompts.article_generate"

    base_prompt = get_setting(prompt_key, DEFAULTS.get(prompt_key, ""))

    content_prompt = (
        f"Write a complete, SEO-optimized {content_type} page for an ecommerce store.\n\n"
        f"TITLE: {selected_title}\n"
        f"PRIMARY KEYWORD: {gap.get('keyword', '')}\n"
        f"SECONDARY KEYWORDS: {secondary_kws}\n"
        f"SEARCH INTENT: {gap.get('intent', 'commercial')}\n"
        f"TARGET WORD COUNT: {word_count}\n\n"
        f"HEADING STRUCTURE TO FOLLOW:\n{heading_text}\n\n"
        f"QUESTIONS TO ANSWER:\n{questions}\n\n"
        f"INTERNAL LINKS TO INCLUDE:\n{internal_links}\n\n"
        + get_setting("prompts.content_gap_page_rules", DEFAULTS["prompts.content_gap_page_rules"])
        .replace("{word_count}", str(word_count))
    )

    cannibal_risk = brief.get("cannibalization_risk", {})
    if cannibal_risk.get("risk_level") in ("high", "medium"):
        existing = cannibal_risk.get("existing_pages", []) + [{"url": f"/{m['type']}s/{m['handle']}", "position": "N/A"} for m in cannibal_risk.get("on_site_matches", [])]
        existing_desc = "\n".join([f"  - {p['url']} (position: {p.get('position', 'N/A')})" for p in existing[:5]])
        content_prompt += (
            f"\nCANNIBALIZATION PREVENTION:\n"
            f"These existing pages already target or rank for this keyword:\n{existing_desc}\n"
            f"CRITICAL: Do NOT repeat the same primary keyword focus. Instead:\n"
            f"- Use a unique angle or long-tail variation as the primary focus\n"
            f"- Link TO the existing page(s) with descriptive anchor text\n"
            f"- Cover subtopics NOT already covered by existing pages\n"
            f"- Ensure the meta title is distinctly different from existing pages\n\n"
        )

    content_prompt += (
        f"\nReturn JSON only:\n"
        f'{{"title": "{selected_title}", '
        f'"body_html": "complete HTML content", '
        f'"meta_title": "50-60 char SEO title", '
        f'"meta_description": "145-160 char meta description", '
        f'"tags": ["tag1", "tag2", "tag3"], '
        f'"slug": "url-slug"}}'
    )

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key, timeout=120.0, max_retries=1)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": base_prompt},
            {"role": "user", "content": content_prompt},
        ],
        max_tokens=4000,
        temperature=0.4,
    )
    text = response.choices[0].message.content or ""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        result = json.loads(text[start:end])
    else:
        result = {"body_html": text, "title": selected_title}

    result["word_count"] = len((result.get("body_html") or "").split())
    return result


def _upload_image_to_cdn(client, image_bytes, filename, alt_text):
    """Stage, upload, and register an image as a Shopify File; return its final CDN url."""
    mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
    staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
    resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime, filename=filename)
    file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
    return client.file_get_url(file_obj.get("id"))


def create_collection_on_shopify(client, store, proposal):
    """Create a collection on Shopify and mirror it into synced_collections.

    The one place that does this, shared by the Content Gaps publish flow and
    the Keyword Strategy "create a money page" flow, so the two can't drift.

    proposal: {title, body_html, meta_title, meta_description, handle?,
               mode: 'smart'|'manual', rules?, applied_disjunctively?,
               product_shopify_ids?, generate_image?, image_keywords?}

    Ordering is deliberate: the Shopify id is persisted immediately after
    creation, before publishing or any other follow-up, because a failure
    after creation must not lose the id — a re-run would mint a duplicate
    collection. Everything after the insert is best-effort.
    """
    store_id = store["store_id"] if "store_id" in store else store["id"]
    title = proposal["title"]
    body_html = proposal.get("body_html") or ""
    meta_title = proposal.get("meta_title") or ""
    meta_description = proposal.get("meta_description") or ""
    smart = proposal.get("mode") == "smart"

    if smart:
        created = client.create_smart_collection(
            title,
            description_html=body_html,
            seo_title=meta_title,
            seo_description=meta_description,
            rules=proposal.get("rules"),
            applied_disjunctively=bool(proposal.get("applied_disjunctively")),
            handle=proposal.get("handle") or None,
        )
    else:
        created = client.create_collection(title, body_html)
    shopify_id = created["shopify_id"]
    handle = created["handle"]

    # Persist before anything else can fail.
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO synced_collections
               (store_id, shopify_id, title, handle, description, meta_title, meta_description,
                collection_type, products_count, synced_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (store_id, shopify_id) DO UPDATE SET
                   title = EXCLUDED.title, description = EXCLUDED.description,
                   collection_type = EXCLUDED.collection_type, synced_at = NOW()
               RETURNING id""",
            (store_id, shopify_id, title, handle, body_html, meta_title, meta_description,
             "smart" if smart else "manual", created.get("products_count") or 0),
        )
        local_id = cur.fetchone()["id"]

    # create_smart_collection carries SEO inline; the plain create can't.
    if not smart and (meta_title or meta_description):
        try:
            client.update_collection(shopify_id, {"meta_title": meta_title,
                                                  "meta_description": meta_description})
        except Exception:
            logger.warning("Setting SEO fields failed for collection %s", shopify_id, exc_info=True)

    try:
        client.publish_to_all_channels(shopify_id)
    except Exception:
        logger.warning("Publish to sales channels failed for %s", shopify_id, exc_info=True)

    # collectionAddProducts errors on smart collections — their membership is
    # the rule set, and there is no supported way to pin extras alongside it.
    product_ids = proposal.get("product_shopify_ids") or []
    if product_ids and not smart:
        try:
            client.add_products_to_collection(shopify_id, product_ids)
        except Exception:
            logger.warning("Could not add products to new collection %s", shopify_id, exc_info=True)

    if proposal.get("generate_image"):
        try:
            from app.services.image_generator import generate_collection_image_ai
            img_bytes, filename, alt_text, w, h = generate_collection_image_ai(
                title, [], keywords=proposal.get("image_keywords") or [])
            if img_bytes:
                mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
                staged = client.staged_upload_create(filename, mime, len(img_bytes), resource="IMAGE")
                resource_url = client.upload_staged_file(staged, img_bytes, content_type=mime, filename=filename)
                client.update_collection_image(shopify_id, resource_url, alt_text)
        except Exception as e:
            logger.warning("Collection image generation failed: %s", str(e))

    # Membership is only knowable from Shopify: a rule set also matches draft
    # products and honors publication, neither of which we mirror locally.
    products_count = created.get("products_count")
    if products_count is None:
        try:
            products_count = client.get_collection_products_count(shopify_id)
        except Exception:
            products_count = len(product_ids) if not smart else None
    if products_count is not None:
        with db.get_cursor() as cur:
            cur.execute("UPDATE synced_collections SET products_count = %s WHERE id = %s",
                        (products_count, local_id))

    return {
        "shopify_id": shopify_id,
        "handle": handle,
        "local_id": local_id,
        "products_count": products_count,
        "collection_type": "smart" if smart else "manual",
        "url": f"https://{store['domain']}/collections/{handle}",
    }


def publish_content(store_id, gap_id, content_data):
    from app.auth import decrypt_value
    from app.services.ai_output_guard import guard_shopify_fields
    from app.shopify_client import ShopifyClient

    gap = get_gap(store_id, gap_id)
    if not gap:
        raise ValueError("Gap not found")

    content_type = content_data.get("content_type", "collection")
    title = content_data.get("title", "")
    guarded = guard_shopify_fields({
        "body_html": content_data.get("body_html", ""),
        "meta_title": content_data.get("meta_title", ""),
        "meta_description": content_data.get("meta_description", ""),
    })
    body_html = guarded["body_html"]
    meta_title = guarded["meta_title"]
    meta_description = guarded["meta_description"]
    tags = content_data.get("tags", [])
    slug = content_data.get("slug", "")
    generate_image = content_data.get("generate_image", False)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT s.domain, s.access_token_enc, s.api_version, s.id as store_id FROM stores s WHERE s.id = %s",
            (store_id,),
        )
        store = cur.fetchone()
    if not store:
        raise ValueError("Store not found")

    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    result = {}

    if content_type == "collection":
        product_shopify_ids = []
        if gap.get("matched_product_id"):
            with db.get_cursor() as cur:
                cur.execute("SELECT shopify_id FROM synced_products WHERE id = %s", (gap["matched_product_id"],))
                prod = cur.fetchone()
                if prod:
                    product_shopify_ids = [prod["shopify_id"]]

        result = create_collection_on_shopify(client, store, {
            "title": title,
            "body_html": body_html,
            "meta_title": meta_title,
            "meta_description": meta_description,
            "mode": "manual",
            "product_shopify_ids": product_shopify_ids,
            "generate_image": generate_image,
            "image_keywords": [gap.get("keyword", "")],
        })

    elif content_type == "blog":
        # Articles are published only by the Blog wizard, which runs research,
        # quality checks, the intro link, scheduling and review. This branch
        # skipped all of them and left the post out of the Blog list.
        raise ValueError("Blog posts are created in Blog › Create Post. Use “Write an article” from this gap.")

    elif content_type == "page":
        shopify_result = client.create_page(title, body_html, slug or None, True, meta_title, meta_description)
        shopify_id = shopify_result["shopify_id"]
        handle = shopify_result["handle"]

        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO synced_pages (store_id, shopify_id, title, handle, body_html, seo_title, seo_description, is_published, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
                   ON CONFLICT (store_id, shopify_id) DO UPDATE SET title = EXCLUDED.title, body_html = EXCLUDED.body_html, updated_at = NOW()""",
                (store_id, shopify_id, title, handle, body_html, meta_title, meta_description),
            )
        result = {"shopify_id": shopify_id, "handle": handle, "url": f"https://{store['domain']}/pages/{handle}"}

    with db.get_cursor() as cur:
        cur.execute("UPDATE content_gaps SET status = 'published', updated_at = NOW() WHERE id = %s AND store_id = %s", (gap_id, store_id))

    result["content_type"] = content_type
    return result


def optimize_existing_page(store_id, gap_id, override_keyword=None, override_volume=None):
    from app.auth import decrypt_value
    from app.settings_manager import get_setting
    from app.prompt_defaults import DEFAULTS
    from openai import OpenAI
    from app.services.seo_analyzer import analyze_product_seo, analyze_collection_seo

    gap = get_gap(store_id, gap_id)
    if not gap:
        raise ValueError("Gap not found")

    if override_keyword:
        gap = dict(gap)
        gap["keyword"] = override_keyword
        if override_volume is not None:
            gap["search_volume"] = override_volume

    risk = (gap.get("brief") or {}).get("cannibalization_risk") or check_cannibalization_risk(store_id, gap["keyword"])

    best_page = None
    if risk.get("on_site_matches"):
        best_page = risk["on_site_matches"][0]
    elif risk.get("existing_pages"):
        ep = risk["existing_pages"][0]
        url = ep["url"]
        with db.get_cursor() as cur:
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
            cred = cur.fetchone()
        from app.services.url_utils import gsc_base_url
        base = gsc_base_url(cred["gsc_property"] if cred else "").rstrip("/")
        path = url.replace(base, "").strip("/")
        if path.startswith("products/"):
            handle = path.replace("products/", "").split("?")[0]
            with db.get_cursor() as cur:
                cur.execute("SELECT id, title, handle FROM synced_products WHERE store_id = %s AND handle = %s", (store_id, handle))
                p = cur.fetchone()
                if p:
                    best_page = {"type": "product", "id": p["id"], "title": p["title"], "handle": p["handle"]}
        elif path.startswith("collections/"):
            handle = path.replace("collections/", "").split("?")[0]
            with db.get_cursor() as cur:
                cur.execute("SELECT id, title, handle FROM synced_collections WHERE store_id = %s AND handle = %s", (store_id, handle))
                c = cur.fetchone()
                if c:
                    best_page = {"type": "collection", "id": c["id"], "title": c["title"], "handle": c["handle"]}

    if not best_page:
        raise ValueError("No existing page found to optimize")

    item_type = best_page["type"]
    item_id = best_page["id"]

    with db.get_cursor() as cur:
        if item_type == "product":
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
        else:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
        item = cur.fetchone()

    if not item:
        raise ValueError(f"{item_type} not found")

    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = today.isoformat()
    handle = item.get("handle", "")

    current_rankings = []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT query, SUM(clicks) as clicks, SUM(impressions) as impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
               FROM gsc_daily_metrics
               WHERE store_id = %s AND page LIKE %s AND date >= %s AND date <= %s
               GROUP BY query ORDER BY impressions DESC LIMIT 30""",
            (store_id, f"%{handle}%", date_from, date_to),
        )
        for r in cur.fetchall():
            current_rankings.append({
                "keyword": r["query"], "clicks": float(r["clicks"] or 0),
                "impressions": float(r["impressions"] or 0),
                "position": round(float(r["position"] or 0), 1), "source": "gsc",
                "volume": None,
            })

        gsc_kw_list = [r["keyword"] for r in current_rankings]
        if gsc_kw_list:
            placeholders = ",".join(["%s"] * len(gsc_kw_list))
            cur.execute(
                f"SELECT LOWER(keyword) as kw, search_volume FROM semrush_keywords WHERE store_id = %s AND LOWER(keyword) IN ({placeholders}) ORDER BY search_volume DESC",
                [store_id] + [k.lower() for k in gsc_kw_list],
            )
            vol_map = {}
            for r in cur.fetchall():
                if r["kw"] not in vol_map:
                    vol_map[r["kw"]] = r["search_volume"]
            for ranking in current_rankings:
                ranking["volume"] = vol_map.get(ranking["keyword"].lower())

        cur.execute(
            "SELECT keyword, position, search_volume FROM semrush_keywords WHERE store_id = %s AND url LIKE %s ORDER BY search_volume DESC LIMIT 20",
            (store_id, f"%{handle}%"),
        )
        gsc_kws = {r["keyword"].lower() for r in current_rankings}
        for r in cur.fetchall():
            if r["keyword"].lower() not in gsc_kws:
                current_rankings.append({
                    "keyword": r["keyword"], "clicks": 0, "impressions": 0,
                    "position": r["position"], "source": "semrush",
                    "volume": r["search_volume"],
                })

    seo_score = 0
    if item_type == "product":
        seo_result = analyze_product_seo(dict(item))
        seo_score = seo_result["score"]
    else:
        seo_result = analyze_collection_seo(dict(item))
        seo_score = seo_result["score"]

    # Resolver guard: never retarget a page onto a keyword another page owns,
    # and never let the gap keyword displace this page's resolved primary.
    resolved_primary = ""
    try:
        from app.services.keyword_resolver import get_or_refresh_resolution, get_owned_primary_keywords, variant_key

        resolution = get_or_refresh_resolution(store_id, item_type, item_id)
        rp = (resolution or {}).get("primary")
        if rp and rp.get("keyword"):
            resolved_primary = rp["keyword"]
        gap_kw = variant_key(gap["keyword"])
        if gap_kw and gap_kw != variant_key(resolved_primary):
            owner = get_owned_primary_keywords(store_id, exclude=(item_type, item_id)).get(gap_kw)
            if owner:
                raise ValueError(
                    f'"{gap["keyword"]}" is already the primary keyword of another page '
                    f'({owner["item_type"]} #{owner["item_id"]}). Optimizing this page for it '
                    f"would cannibalize — target it on its owning page or pick a variation."
                )
    except ValueError:
        raise
    except Exception:
        # Fail closed — this flow rewrites a live page onto a new keyword; a
        # broken ownership check must surface, not silently pass. (The route's
        # skip_cannibalization_check flag bypasses only the fuzzy risk warning,
        # never this hard ownership guard.)
        logger.exception("Content-gap resolver guard failed for %s %s", item_type, item_id)
        raise ValueError("Keyword safety check failed — try again")

    rankings_text = "\n".join([
        f"  - \"{r['keyword']}\" — pos {r['position']}, {r.get('impressions', 0)} impr, {r.get('clicks', 0)} clicks ({r['source']})"
        for r in current_rankings[:20]
    ])
    protected_kws = [r["keyword"] for r in current_rankings if r["position"] and r["position"] <= 10]
    protected_text = ", ".join(protected_kws[:10]) if protected_kws else "none in top 10"

    with db.get_cursor() as cur:
        cur.execute("SELECT api_key_enc, model FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI not configured")

    api_key = decrypt_value(config["api_key_enc"])
    model = config.get("model") or "anthropic/claude-sonnet-4"

    # Title and metas only. The page body is never sent here, so asking for a
    # new description would mean replacing it with one written blind.
    base_prompt = (
        "You are an expert Shopify SEO consultant. You adjust a page's title and meta tags "
        "so it also targets one additional keyword, without changing what the page is about. "
        "Return JSON only."
    )

    optimization_prompt = (
        f"You are optimizing an existing {item_type} page to ALSO rank for a new keyword.\n\n"
        f"CURRENT PAGE:\n"
        f"- Title: {item.get('title', '')}\n"
        f"- Meta Title: {item.get('meta_title', '')}\n"
        f"- Meta Description: {item.get('meta_description', '')}\n"
        f"- Handle: {handle}\n"
        f"- SEO Score: {seo_score}/100\n\n"
        f"CURRENT RANKINGS (keywords this page ranks for):\n{rankings_text}\n\n"
        f"PROTECTED KEYWORDS (top 10, do NOT weaken): {protected_text}\n\n"
        + (f"RESOLVED PRIMARY KEYWORD (this page's strategic target — it stays the primary; "
           f"the new keyword below is ADDED as a secondary, never a replacement): \"{resolved_primary}\"\n\n"
           if resolved_primary else "")
        + f"NEW TARGET KEYWORD: \"{gap['keyword']}\"\n"
        f"- Monthly Search Volume: {gap.get('search_volume', 0)}\n"
        f"- Search Intent: {gap.get('intent', 'commercial')}\n"
        f"- Competition: {gap.get('competition', 'unknown')}\n\n"
        + get_setting("prompts.content_gap_optimize_rules", DEFAULTS["prompts.content_gap_optimize_rules"])
        .replace("{keyword}", gap["keyword"])
        + f"Return JSON only:\n"
        f'{{"strategy": "2-3 sentence explanation of what changes you made and why",'
        f' "new_title": "optimized page title or null if no change needed",'
        f' "new_meta_title": "optimized meta title (50-60 chars)",'
        f' "new_meta_description": "optimized meta description (145-160 chars)",'
        f' "keywords_targeted": ["list of all keywords this page now targets"]}}'
    )

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key, timeout=120.0, max_retries=1)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": base_prompt},
            {"role": "user", "content": optimization_prompt},
        ],
        max_tokens=3000,
        temperature=0.3,
    )
    text = response.choices[0].message.content or ""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        ai_result = json.loads(text[start:end])
    else:
        ai_result = {"strategy": text}
    ai_result.pop("new_description_html", None)

    return {
        "gap": {"id": gap_id, "keyword": gap["keyword"], "volume": gap.get("search_volume"), "intent": gap.get("intent")},
        "page": {
            "type": item_type, "id": item_id, "title": item.get("title"), "handle": handle,
            "meta_title": item.get("meta_title"), "meta_description": item.get("meta_description"),
            "description": item.get("description"), "seo_score": seo_score,
        },
        "current_rankings": current_rankings,
        "optimization": ai_result,
    }


def apply_gap_optimization(store_id, gap_id, item_type, item_id, changes):
    from app.auth import decrypt_value
    from app.services.ai_output_guard import guard_shopify_fields
    from app.shopify_client import ShopifyClient

    with db.get_cursor() as cur:
        cur.execute("SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        raise ValueError("Store not found")

    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    table = "synced_products" if item_type == "product" else "synced_collections"

    with db.get_cursor() as cur:
        cur.execute(f"SELECT shopify_id FROM {table} WHERE id = %s", (item_id,))
        item = cur.fetchone()
    if not item:
        raise ValueError(f"{item_type} not found")

    fields = {}
    if changes.get("new_title"):
        fields["title"] = changes["new_title"]
    if changes.get("new_meta_title"):
        fields["meta_title"] = changes["new_meta_title"]
    if changes.get("new_meta_description"):
        fields["meta_description"] = changes["new_meta_description"]

    guard_shopify_fields(fields)

    if not fields:
        raise ValueError("Nothing to apply — no title or meta changes were provided")

    from app.services.apply_pipeline import apply_item_changes

    applied = apply_item_changes(
        store_id, item_type, item_id, fields,
        source="content_gap", change_type="gap_optimize", client=client,
        allow_dropped_primary=bool(changes.get("force")),
    )

    with db.get_cursor() as cur:
        cur.execute("UPDATE content_gaps SET status = 'published', updated_at = NOW() WHERE id = %s AND store_id = %s", (gap_id, store_id))

    return {"ok": True, "updated_fields": list(applied["pushed"].keys())}
