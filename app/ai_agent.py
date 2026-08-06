import json
import logging
import random
import re
from urllib.parse import urlparse

from openai import OpenAI

logger = logging.getLogger(__name__)

from app.auth import decrypt_value
from app.database import db
from app.prompt_defaults import DEFAULTS
from app.services.seo_analyzer import analyze_collection_seo, analyze_product_seo, DEFAULT_CTR_BENCHMARKS, _expected_ctr
from app.services.blog_seo_analyzer import analyze_article_seo
from app.services.landing_page_seo_analyzer import analyze_synced_page, analyze_landing_page
from app.settings_manager import get_setting



def _get_search_context_for_item(item_type, item_id, store_id):
    """Build search context from GSC + Semrush data for a product, collection, or article.

    Returns a markdown-formatted string with real search performance data,
    or empty string if no data available.
    """
    from datetime import datetime, timedelta, timezone

    try:
        if item_type == "article":
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sa.handle, sa.store_id, sb.handle as blog_handle
                       FROM synced_articles sa
                       LEFT JOIN synced_blogs sb ON sb.id = sa.blog_id AND sb.store_id = sa.store_id
                       WHERE sa.id = %s""",
                    (item_id,),
                )
                item = cur.fetchone()
            if not item or not item.get("handle"):
                return ""
            blog_handle = item.get("blog_handle") or "news"
            handle = item["handle"]
            url_path = f"blogs/{blog_handle}/{handle}"
        else:
            with db.get_cursor() as cur:
                table = "synced_products" if item_type == "product" else "synced_collections"
                cur.execute(f"SELECT handle, store_id FROM {table} WHERE id = %s", (item_id,))
                item = cur.fetchone()

            if not item or not item.get("handle"):
                return ""

            handle = item["handle"]
            prefix = "products" if item_type == "product" else "collections"
            url_path = f"{prefix}/{handle}"

        effective_store_id = store_id or item["store_id"]

        with db.get_cursor() as cur:
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (effective_store_id,))
            cred = cur.fetchone()

        base_url = (cred["gsc_property"] or "").rstrip("/") if cred else ""
        page_url = f"{base_url}/{url_path}" if base_url else ""

        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=30)).isoformat()
        date_to = (today - timedelta(days=3)).isoformat()

        gsc_queries = []
        gsc_totals = None
        if page_url:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT query,
                              SUM(clicks) as clicks,
                              SUM(impressions) as impressions,
                              CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                              SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                       FROM gsc_daily_metrics
                       WHERE store_id = %s AND page = %s AND date >= %s AND date <= %s
                       GROUP BY query
                       HAVING SUM(impressions) > 0
                       ORDER BY SUM(clicks) DESC
                       LIMIT 15""",
                    (effective_store_id, page_url, date_from, date_to),
                )
                gsc_queries = cur.fetchall()

                cur.execute(
                    """SELECT SUM(clicks) as clicks,
                              SUM(impressions) as impressions,
                              CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as ctr,
                              SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                       FROM gsc_daily_metrics
                       WHERE store_id = %s AND page = %s AND date >= %s AND date <= %s""",
                    (effective_store_id, page_url, date_from, date_to),
                )
                gsc_totals = cur.fetchone()

        semrush_keywords = []
        item_url_path = url_path
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword, position, previous_position, search_volume, cpc, url
                   FROM semrush_keywords
                   WHERE store_id = %s AND url IS NOT NULL AND url != ''
                   ORDER BY search_volume DESC NULLS LAST
                   LIMIT 500""",
                (effective_store_id,),
            )
            all_sr = cur.fetchall()

        for r in all_sr:
            url = r["url"] or ""
            path = url.replace(base_url, "").strip("/") if base_url else ""
            if not path:
                try:
                    path = urlparse(url).path.strip("/")
                except Exception:
                    path = url
            if path == item_url_path:
                semrush_keywords.append(r)
            if len(semrush_keywords) >= 20:
                break

        # If no URL-matched keywords, find semantically relevant ones from the
        # store — shared heuristic with the keyword resolver so both rank alike.
        if not semrush_keywords:
            from app.services.keyword_resolver import _semantic_semrush_matches

            with db.get_cursor() as cur:
                if item_type == "product":
                    cur.execute("SELECT title, product_type, vendor FROM synced_products WHERE id = %s", (item_id,))
                else:
                    # Collections/articles have no product_type/vendor columns
                    table = "synced_collections" if item_type == "collection" else "synced_articles"
                    cur.execute(f"SELECT title, '' as product_type, '' as vendor FROM {table} WHERE id = %s", (item_id,))
                item_detail = dict(cur.fetchone() or {})
            item_detail["handle"] = item.get("handle")
            semrush_keywords = _semantic_semrush_matches(item_detail, all_sr)

        # The resolved primary (demand-grounded keyword strategy) is the
        # mandated target for products/collections; manual assignments are
        # already folded into it. Articles have no resolution — they keep the
        # direct manual-assignment lookup.
        resolved_primary = None
        resolution_note = None
        assigned_primary = None
        if item_type in ("product", "collection"):
            from app.services.keyword_resolver import get_or_refresh_resolution

            resolution = get_or_refresh_resolution(effective_store_id, item_type, item_id)
            if resolution and resolution.get("primary"):
                resolved_primary = resolution["primary"]
                resolution_note = resolution.get("note")
        else:
            assigned_primary = _get_assigned_primary(effective_store_id, item_type, item_id)
        if not gsc_queries and not semrush_keywords and not assigned_primary and not resolved_primary:
            return ""

        extra_kw = {}
        if item_type == "article":
            extra_kw["blog_handle"] = blog_handle
        inbound_links = _get_inbound_links(item_type, handle, effective_store_id, **extra_kw)
        image_alt_status = _get_image_alt_status(item_type, item_id, effective_store_id)
        faq_schema_status = _get_faq_schema_status(item_type, item_id, effective_store_id)
        cannibalization_risks = _get_cannibalization_risks(item_type, handle, effective_store_id, semrush_keywords, gsc_queries, **extra_kw)
        if resolved_primary and cannibalization_risks:
            # The resolved primary already survived the resolver's own
            # cannibalization/ownership checks — warning the model to
            # "differentiate away" from its mandated keyword is a contradiction.
            rp_norm = _norm_kw(resolved_primary.get("keyword"))
            cannibalization_risks = [r for r in cannibalization_risks
                                     if _norm_kw(r.get("keyword")) != rp_norm]

        from app.services.ctr_benchmarks import get_expected_ctr_curve
        ctr_curve = get_expected_ctr_curve(effective_store_id)

        return _format_search_context(gsc_totals, gsc_queries, semrush_keywords, inbound_links, image_alt_status, faq_schema_status, cannibalization_risks, assigned_primary=assigned_primary, resolved_primary=resolved_primary, resolution_note=resolution_note, ctr_curve=ctr_curve)

    except Exception as e:
        logger.warning("Search context fetch failed for %s %s: %s", item_type, item_id, e)
        return ""


def _get_inbound_links(item_type, handle, store_id, blog_handle=None):
    """Find existing inbound internal links to this item from articles, products, collections, and collection membership."""
    if item_type == "article":
        bh = blog_handle or "news"
        base_path = f"/blogs/{bh}/{handle}"
    else:
        prefix = "products" if item_type == "product" else "collections"
        base_path = f"/{prefix}/{handle}"
    # Delimiter-terminated only — a bare base_path prefix-matched longer
    # handles (/products/foo counted links to /products/foo-bar), inflating
    # the count that gates internal-linking recommendations.
    target_patterns = [f"{base_path}?", f"{base_path}#", f'{base_path}"', f"{base_path}'"]

    inbound = []
    try:
        with db.get_cursor() as cur:
            # Check blog articles
            cur.execute("SELECT id, title, body FROM synced_articles WHERE store_id = %s AND body IS NOT NULL", (store_id,))
            for row in cur.fetchall():
                body = row["body"] or ""
                for pat in target_patterns:
                    if pat in body:
                        inbound.append({"source_type": "article", "source_title": row["title"]})
                        break

            # Check collection descriptions
            cur.execute("SELECT id, title, description FROM synced_collections WHERE store_id = %s AND description IS NOT NULL", (store_id,))
            for row in cur.fetchall():
                desc = row["description"] or ""
                for pat in target_patterns:
                    if pat in desc:
                        inbound.append({"source_type": "collection", "source_title": row["title"]})
                        break

            # Check product descriptions
            cur.execute("SELECT id, title, description FROM synced_products WHERE store_id = %s AND description IS NOT NULL AND description != ''", (store_id,))
            for row in cur.fetchall():
                desc = row["description"] or ""
                for pat in target_patterns:
                    if pat in desc:
                        inbound.append({"source_type": "product", "source_title": row["title"]})
                        break

            # For products: check which collections this product belongs to (collection membership = internal link)
            if item_type == "product":
                cur.execute(
                    """SELECT sc.title
                       FROM collection_product_map cpm
                       JOIN synced_products sp ON sp.shopify_id = cpm.product_shopify_id AND sp.store_id = cpm.store_id
                       JOIN synced_collections sc ON sc.shopify_id = cpm.collection_shopify_id AND sc.store_id = cpm.store_id
                       WHERE sp.store_id = %s AND sp.handle = %s""",
                    (store_id, handle),
                )
                for row in cur.fetchall():
                    inbound.append({"source_type": "collection (member)", "source_title": row["title"]})

            # For collections: check how many products link back to this collection in their descriptions
            if item_type == "collection":
                cur.execute(
                    """SELECT COUNT(*) as cnt
                       FROM collection_product_map cpm
                       JOIN synced_collections sc ON sc.shopify_id = cpm.collection_shopify_id AND sc.store_id = cpm.store_id
                       WHERE sc.store_id = %s AND sc.handle = %s""",
                    (store_id, handle),
                )
                row = cur.fetchone()
                if row and row["cnt"] > 0:
                    inbound.append({"source_type": "product listings", "source_title": f"{row['cnt']} products in this collection"})
    except Exception:
        pass

    return inbound


def _get_cannibalization_risks(item_type, handle, store_id, semrush_keywords, gsc_queries, blog_handle=None):
    """Check if recommended keywords would cannibalize other pages on the store."""
    from datetime import datetime, timedelta, timezone

    keywords_to_check = set()
    for k in semrush_keywords[:10]:
        keywords_to_check.add((k.get("keyword") or "").lower())
    for q in (gsc_queries or [])[:10]:
        keywords_to_check.add((q.get("query") or "").lower())

    if not keywords_to_check:
        return []

    if item_type == "article":
        bh = blog_handle or "news"
        this_path_suffix = f"/blogs/{bh}/{handle}"
    else:
        prefix = "products" if item_type == "product" else "collections"
        this_path_suffix = f"/{prefix}/{handle}"

    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=30)).isoformat()
    date_to = (today - timedelta(days=3)).isoformat()

    risks = []
    try:
        with db.get_cursor() as cur:
            for kw in keywords_to_check:
                if not kw:
                    continue
                cur.execute(
                    """SELECT page, SUM(clicks) as clicks, SUM(impressions) as impressions,
                              SUM(position * impressions) / NULLIF(SUM(impressions), 0) as position
                       FROM gsc_daily_metrics
                       WHERE store_id = %s AND LOWER(query) = %s AND date >= %s AND date <= %s
                       GROUP BY page
                       HAVING SUM(impressions) > 0
                       ORDER BY SUM(impressions) DESC""",
                    (store_id, kw, date_from, date_to),
                )
                pages = cur.fetchall()
                if len(pages) > 1:
                    competing = [
                        p for p in pages
                        if this_path_suffix not in (p["page"] or "")
                    ]
                    if competing:
                        risks.append({
                            "keyword": kw,
                            "competing_pages": [
                                {"url": p["page"], "impressions": int(float(p["impressions"] or 0)),
                                 "position": round(float(p["position"] or 0), 1)}
                                for p in competing[:3]
                            ],
                        })
    except Exception:
        pass

    return risks


def _get_image_alt_status(item_type, item_id, store_id):
    """Check image alt text coverage for a product or collection's products."""
    try:
        if item_type not in ("product", "collection"):
            # An article/page id would alias into the collection query below
            # (independent SERIAL sequences) and report a random collection's images.
            return None
        if item_type == "product":
            with db.get_cursor() as cur:
                cur.execute("SELECT images FROM synced_products WHERE id = %s", (item_id,))
                row = cur.fetchone()
            products = [row] if row else []
        else:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sp.images FROM synced_products sp
                       JOIN collection_product_map cpm ON cpm.product_shopify_id = sp.shopify_id AND cpm.store_id = sp.store_id
                       JOIN synced_collections sc ON sc.shopify_id = cpm.collection_shopify_id AND sc.store_id = cpm.store_id
                       WHERE sc.id = %s AND sp.store_id = %s AND sp.status = 'ACTIVE' LIMIT 50""",
                    (item_id, store_id),
                )
                products = cur.fetchall()

        total_images = 0
        with_alt = 0
        for p in products:
            images = p.get("images") or []
            if isinstance(images, str):
                images = json.loads(images)
            for img in images:
                if img.get("media_id"):
                    total_images += 1
                    if img.get("alt"):
                        with_alt += 1

        return {"total": total_images, "with_alt": with_alt, "missing": total_images - with_alt}
    except Exception:
        return None


def _get_faq_schema_status(item_type, item_id, store_id):
    """Check if FAQ schema already exists for this item."""
    try:
        if item_type not in ("product", "collection"):
            return None
        table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(f"SELECT shopify_id FROM {table} WHERE id = %s", (item_id,))
            row = cur.fetchone()
            if not row:
                return None

            cur.execute(
                """SELECT schema_json, pushed_to_shopify, pushed_at
                   FROM schema_markup
                   WHERE store_id = %s AND resource_id = %s AND schema_type = 'FAQPage'""",
                (store_id, str(row["shopify_id"])),
            )
            schema_row = cur.fetchone()

        if schema_row:
            schema = schema_row["schema_json"]
            if isinstance(schema, str):
                schema = json.loads(schema)
            faq_count = len(schema.get("mainEntity", []))
            return {
                "exists": True,
                "faq_count": faq_count,
                "pushed": bool(schema_row["pushed_to_shopify"]),
                "pushed_at": schema_row["pushed_at"].isoformat() if schema_row["pushed_at"] else None,
            }
        return {"exists": False}
    except Exception:
        return None


def _norm_kw(kw):
    return re.sub(r"\s+", " ", (kw or "").strip().lower())


def _get_assigned_primary(store_id, item_type, item_id):
    """Manually assigned primary keyword (accepted/applied) for an item, or None."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT mk.keyword, mk.search_volume
                   FROM keyword_assignments ka
                   JOIN master_keywords mk ON mk.id = ka.master_keyword_id
                   WHERE ka.store_id = %s AND ka.assigned_item_type = %s
                     AND ka.assigned_item_id = %s AND ka.status IN ('accepted', 'applied')
                   ORDER BY ka.updated_at DESC LIMIT 1""",
                (store_id, item_type, item_id),
            )
            return cur.fetchone()
    except Exception:
        return None


def _format_search_context(gsc_totals, gsc_queries, semrush_keywords, inbound_links=None, image_alt_status=None, faq_schema_status=None, cannibalization_risks=None, assigned_primary=None, resolved_primary=None, resolution_note=None, ctr_curve=None):
    """Format GSC + Semrush data as markdown for AI prompt injection."""
    lines = ["## REAL SEARCH PERFORMANCE DATA (Google Search Console + Semrush)"]

    if assigned_primary:
        ap_vol = int(assigned_primary.get("search_volume") or 0)
        lines.append(
            f"\n### USER-ASSIGNED PRIMARY KEYWORD (must use)\n"
            f"\"{assigned_primary['keyword']}\""
            + (f" — volume: {ap_vol:,}" if ap_vol else "")
            + " — assigned manually by the store owner; use it as the primary keyword "
              "regardless of the recommendations below."
        )
    elif resolved_primary:
        rp_vol = int(resolved_primary.get("volume") or 0)
        rp_pos = resolved_primary.get("gsc_position") or resolved_primary.get("semrush_position")
        origin = ("assigned manually by the store owner"
                  if resolved_primary.get("manual")
                  else "selected by the store's demand-grounded keyword strategy "
                       "(relevance × demand × specificity, cannibalization-checked)")
        lines.append(
            f"\n### RESOLVED PRIMARY KEYWORD (must use)\n"
            f"\"{resolved_primary['keyword']}\""
            + (f" — volume: {rp_vol:,}" if rp_vol else "")
            + (f", current position: {float(rp_pos):.1f}" if rp_pos else "")
            + f" — {origin}. Use it as the primary keyword. Do NOT substitute a "
              "higher-volume keyword: high-volume head terms belong to other pages of this store."
            + (f"\nNote: {resolution_note}." if resolution_note else "")
        )

    if gsc_totals and float(gsc_totals.get("impressions") or 0) > 0:
        clicks = int(float(gsc_totals["clicks"] or 0))
        impressions = int(float(gsc_totals["impressions"] or 0))
        ctr = float(gsc_totals["ctr"] or 0)
        position = float(gsc_totals["position"] or 0)
        lines.append(
            f"\n### Current Performance (last 28 days)\n"
            f"- Total clicks: {clicks:,} | Impressions: {impressions:,} | "
            f"CTR: {ctr:.1%} | Avg Position: {position:.1f}"
        )

    if gsc_queries:
        lines.append("\n### Top Ranking Queries (by clicks)")
        lines.append("| Query | Clicks | Impressions | CTR | Position |")
        lines.append("|-------|--------|-------------|-----|----------|")
        for q in gsc_queries[:10]:
            qclicks = int(float(q["clicks"] or 0))
            qimpr = int(float(q["impressions"] or 0))
            qctr = float(q["ctr"] or 0)
            qpos = float(q["position"] or 0)
            lines.append(f"| {q['query']} | {qclicks:,} | {qimpr:,} | {qctr:.1%} | {qpos:.1f} |")

        opportunities = []
        for q in gsc_queries:
            qimpr = float(q["impressions"] or 0)
            qctr = float(q["ctr"] or 0)
            qpos = float(q["position"] or 0)
            expected = _expected_ctr(qpos, ctr_curve)
            if qimpr >= 50 and qctr < expected * 0.6:
                opportunities.append({
                    "query": q["query"],
                    "impressions": int(qimpr),
                    "ctr": qctr,
                    "position": qpos,
                    "expected_ctr": expected,
                })

        if opportunities:
            lines.append("\n### Quick-Win Opportunities (high impressions, low CTR)")
            for opp in opportunities[:5]:
                lines.append(
                    f"- \"{opp['query']}\" — {opp['impressions']:,} impressions, "
                    f"{opp['ctr']:.1%} CTR (expected {opp['expected_ctr']:.1%} for position {opp['position']:.1f})"
                )

    if semrush_keywords:
        total_volume = sum(int(k.get("search_volume") or 0) for k in semrush_keywords)
        best = min(semrush_keywords, key=lambda k: float(k.get("position") or 999))
        best_kw = best["keyword"]
        best_pos = float(best.get("position") or 0)

        lines.append(
            f"\n### Semrush Keyword Data\n"
            f"- Ranking keywords: {len(semrush_keywords)} | Total monthly search volume: {total_volume:,}\n"
            f"- Best position: {best_pos:.0f} for \"{best_kw}\""
        )

        striking = [k for k in semrush_keywords if 4 <= float(k.get("position") or 999) <= 20]
        if striking:
            striking_sorted = sorted(striking, key=lambda k: int(k.get("search_volume") or 0), reverse=True)
            lines.append("- Striking distance keywords (positions 4-20):")
            for k in striking_sorted[:8]:
                vol = int(k.get("search_volume") or 0)
                pos = float(k.get("position") or 0)
                lines.append(f"  - \"{k['keyword']}\" (vol: {vol:,}, pos: {pos:.0f})")

    primary_kw = None
    secondary_kws = []

    if semrush_keywords:
        # Always pick primary by highest volume first, regardless of position
        with_volume = [k for k in semrush_keywords if int(k.get("search_volume") or 0) > 0]
        if with_volume:
            by_vol = sorted(with_volume, key=lambda k: int(k.get("search_volume") or 0), reverse=True)
            primary_kw = by_vol[0]
            secondary_kws = by_vol[1:4]
        else:
            # No volume data — fall back to best position
            by_pos = sorted(semrush_keywords, key=lambda k: float(k.get("position") or 999))
            primary_kw = by_pos[0]
            secondary_kws = by_pos[1:4]

    if not primary_kw and gsc_queries:
        best_gsc = gsc_queries[0]
        primary_kw = {"keyword": best_gsc["query"], "search_volume": 0, "position": float(best_gsc.get("position") or 0)}

    if assigned_primary:
        # The manual assignment owns slot 1; demand-ranked picks become secondaries.
        if primary_kw and _norm_kw(primary_kw.get("keyword")) != _norm_kw(assigned_primary.get("keyword")):
            secondary_kws = [primary_kw] + list(secondary_kws)
        primary_kw = {"keyword": assigned_primary["keyword"],
                      "search_volume": assigned_primary.get("search_volume") or 0,
                      "position": assigned_primary.get("position") or 0}
    elif resolved_primary:
        # The keyword-strategy resolution owns slot 1; volume-ranked picks
        # become secondaries.
        if primary_kw and _norm_kw(primary_kw.get("keyword")) != _norm_kw(resolved_primary.get("keyword")):
            secondary_kws = [primary_kw] + list(secondary_kws)
        primary_kw = {"keyword": resolved_primary["keyword"],
                      "search_volume": resolved_primary.get("volume") or 0,
                      "position": (resolved_primary.get("gsc_position")
                                   or resolved_primary.get("semrush_position") or 0)}

    if primary_kw:
        secondary_kws = [k for k in secondary_kws
                         if _norm_kw(k.get("keyword")) != _norm_kw(primary_kw.get("keyword"))]
        vol = int(primary_kw.get("search_volume") or 0)
        pos = float(primary_kw.get("position") or 0)
        if assigned_primary or resolved_primary:
            lines.append("\n### RECOMMENDED TARGET KEYWORDS (primary chosen by the keyword strategy — use #1 as primary; do NOT swap in a higher-volume keyword)")
        else:
            lines.append("\n### RECOMMENDED TARGET KEYWORDS (sorted by search volume — use #1 as primary)")
        if assigned_primary:
            marker = "← USE THIS AS PRIMARY (user-assigned)"
        elif resolved_primary:
            marker = "← USE THIS AS PRIMARY (resolved by keyword strategy)"
        else:
            marker = "← USE THIS AS PRIMARY"
        lines.append(f"1. \"{primary_kw['keyword']}\" — volume: {vol:,}, position: {pos:.0f} {marker}")
        for i, kw in enumerate(secondary_kws):
            sv = int(kw.get("search_volume") or 0)
            sp = float(kw.get("position") or 0)
            lines.append(f"{i+2}. \"{kw['keyword']}\" — volume: {sv:,}, position: {sp:.0f}")
        if vol == 0 and secondary_kws and not assigned_primary and not resolved_primary:
            best_sec = max(secondary_kws, key=lambda k: int(k.get("search_volume") or 0))
            best_vol = int(best_sec.get("search_volume") or 0)
            if best_vol > 0:
                lines.append(f"\nWARNING: The #1 keyword has 0 volume. Consider \"{best_sec['keyword']}\" (vol: {best_vol:,}) as primary instead.")

    if inbound_links:
        lines.append(f"\n### EXISTING INBOUND INTERNAL LINKS ({len(inbound_links)} total)")
        for link in inbound_links[:10]:
            lines.append(f"- From {link['source_type']}: \"{link['source_title']}\"")
        if len(inbound_links) > 10:
            lines.append(f"- ... and {len(inbound_links) - 10} more")
        lines.append("\nIMPORTANT: Do NOT recommend 'internal_linking' if this page already has 3+ inbound links. "
                      "Focus on other optimizations instead.")
    elif inbound_links is not None:
        lines.append("\n### EXISTING INBOUND INTERNAL LINKS: None found")
        lines.append("This page has no internal links pointing to it — internal linking is recommended.")

    if image_alt_status:
        total = image_alt_status["total"]
        with_alt = image_alt_status["with_alt"]
        missing = image_alt_status["missing"]
        if total > 0:
            pct = round(with_alt / total * 100)
            lines.append(f"\n### IMAGE ALT TEXT STATUS: {with_alt}/{total} images have alt text ({pct}%)")
            if missing == 0:
                lines.append("All images have alt text. Do NOT recommend 'image_alt_text' unless the existing alt text is clearly poor quality (generic filenames, missing keywords).")
            elif missing <= 2:
                lines.append(f"Only {missing} image(s) missing alt text — minor issue.")

    if faq_schema_status:
        if faq_schema_status.get("exists"):
            lines.append(f"\n### FAQ SCHEMA STATUS: Already exists with {faq_schema_status['faq_count']} FAQ(s)")
            if faq_schema_status.get("pushed"):
                lines.append(f"Pushed to Shopify at {faq_schema_status.get('pushed_at', 'unknown')}.")
            lines.append("Do NOT recommend 'faq_schema' — it already exists. Suggest updating it only if the current questions are irrelevant to the search queries.")
        else:
            lines.append("\n### FAQ SCHEMA STATUS: None found — FAQ schema is recommended if relevant search queries exist.")

    if cannibalization_risks:
        lines.append(f"\n### CANNIBALIZATION RISKS ({len(cannibalization_risks)} keywords)")
        lines.append("These keywords have OTHER pages on this store also ranking for them:")
        for risk in cannibalization_risks[:5]:
            pages_str = ", ".join(
                f"{p['url']} (pos: {p['position']}, impr: {p['impressions']})"
                for p in risk["competing_pages"][:2]
            )
            lines.append(f"- \"{risk['keyword']}\" — also on: {pages_str}")
        lines.append("\nCAUTION: If you choose a keyword that another page already ranks well for, "
                      "explain the cannibalization risk in your recommendations. "
                      "Suggest differentiating the focus (e.g., long-tail variant) or "
                      "recommend consolidating traffic to the stronger page.")
    elif cannibalization_risks is not None:
        lines.append("\n### CANNIBALIZATION: No risks detected for these keywords.")

    return "\n".join(lines)


def _get_article_title_styles():
    return get_setting("lists.article_title_styles", DEFAULTS["lists.article_title_styles"])

def _split_keywords(keywords):
    primary = keywords[0] if keywords else ""
    secondary = keywords[1:] if len(keywords) > 1 else []
    return primary, secondary


def _fix_internal_link_handles(body_html, store_id):
    """Repair AI-mangled internal product/collection link handles (e.g.
    '16-oz.-bag' written from the title instead of the real '16-oz-bag')
    by matching hrefs against the store's synced handles."""
    if not body_html or not store_id:
        return body_html

    def norm(h):
        return re.sub(r"[^a-z0-9]", "", h.lower())

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle FROM synced_products WHERE store_id = %s AND handle IS NOT NULL AND handle != ''",
            (store_id,),
        )
        product_handles = [r["handle"] for r in cur.fetchall()]
        cur.execute(
            "SELECT handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != ''",
            (store_id,),
        )
        collection_handles = [r["handle"] for r in cur.fetchall()]

    exact = {"products": set(product_handles), "collections": set(collection_handles)}
    fuzzy = {
        "products": {norm(h): h for h in product_handles},
        "collections": {norm(h): h for h in collection_handles},
    }

    def repl(m):
        kind, handle = m.group(1), m.group(2)
        if handle in exact[kind]:
            return m.group(0)
        fixed = fuzzy[kind].get(norm(handle))
        if fixed:
            logger.info("Fixed mangled %s link handle: %s -> %s", kind, handle, fixed)
            return f'href="/{kind}/{fixed}"'
        return m.group(0)

    return re.sub(r'href="/(products|collections)/([^"?#]+)"', repl, body_html)


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_product_data",
            "description": "Get full product data including title, description, meta fields, images, and variants for a specific product by its database ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "The database ID of the product",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_seo_score",
            "description": "Run rule-based SEO analysis on a product and get a score breakdown with details for meta title, meta description, description length, and image alt text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "The database ID of the product to analyze",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_summary",
            "description": "Get a summary of the store including total products, average SEO score, and top issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    }
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_collection_data",
            "description": "Get full collection data including title, description, and meta fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "The database ID of the collection",
                    }
                },
                "required": ["collection_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_collection_seo_score",
            "description": "Run rule-based SEO analysis on a collection and get a score breakdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "The database ID of the collection to analyze",
                    }
                },
                "required": ["collection_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_search_performance",
            "description": (
                "Get real search performance data from Google Search Console and Semrush for a product or collection. "
                "Returns top ranking queries, click/impression metrics, striking-distance keywords, and recommended target keywords."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "The database ID of the product or collection",
                    },
                    "item_type": {
                        "type": "string",
                        "description": "Either 'product' or 'collection'",
                        "default": "product",
                    },
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_keyword_opportunities",
            "description": (
                "Get striking-distance keywords (positions 4-20) and CTR opportunity keywords for a store. "
                "Useful for finding quick-win SEO improvements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                },
                "required": ["store_id"],
            },
        },
    },
]

KEYWORD_ANALYSIS_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_all_product_summaries",
            "description": (
                "Get paginated product summaries for a store including title, product_type, vendor, and current tags. "
                "Use offset and limit to paginate through all products. Returns products plus total count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of products to return (default 100, max 200)",
                        "default": 100,
                    },
                },
                "required": ["store_id"],
            },
        },
    },
]



ARTICLE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_article_data",
            "description": "Get article content and metadata for a specific article by its database ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "article_id": {
                        "type": "integer",
                        "description": "The database ID of the article",
                    }
                },
                "required": ["article_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_article_seo_score",
            "description": "Run rule-based SEO analysis on an article and get a score breakdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "article_id": {
                        "type": "integer",
                        "description": "The database ID of the article to analyze",
                    }
                },
                "required": ["article_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_products_for_linking",
            "description": "Get product titles and handles for internal linking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    }
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_collections_for_linking",
            "description": "Get collection titles and handles for internal linking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    }
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_articles_for_linking",
            "description": "Get other article titles and handles for cross-linking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                    "exclude_article_id": {
                        "type": "integer",
                        "description": "Article ID to exclude from results",
                    },
                },
                "required": ["store_id"],
            },
        },
    },
]

TOPIC_DISCOVERY_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web via Brave Search. Returns ranked results with title, url, "
                "description, and domain. Use for market research, competitor discovery, "
                "and validating SERP difficulty for candidate topics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 10, max 20)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_overview",
            "description": (
                "Get a store inventory overview: product-type distribution with counts, "
                "top vendors, total active product count, and collections with product counts. "
                "Use to build the coverage-vs-inventory matrix."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_store_products",
            "description": (
                "Search the store's ACTIVE products by keyword (matches title, product type, "
                "vendor, tags). Use to verify inventory depth (SKU counts) for a candidate "
                "topic or brand before recommending it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                    "query": {
                        "type": "string",
                        "description": "Keyword to search for (brand, product type, flavor, etc.)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 50)",
                        "default": 20,
                    },
                },
                "required": ["store_id", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_existing_articles",
            "description": (
                "Get all existing blog articles for the store (title, handle, tags, published "
                "state). Use for gap analysis and to pick internal-link targets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_keyword_opportunities",
            "description": (
                "Get striking-distance keywords (positions 4-20) and CTR opportunity keywords for a store. "
                "Useful for finding quick-win SEO improvements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                },
                "required": ["store_id"],
            },
        },
    },
]

LANDING_PAGE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_landing_page_data",
            "description": "Get builder landing page data including title, meta fields, and sections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "integer", "description": "The database ID of the landing page"},
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_synced_page_data",
            "description": "Get existing Shopify page data including HTML body and meta fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "integer", "description": "The database ID of the synced page"},
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_landing_page_seo",
            "description": "Run rule-based SEO analysis on a landing page and get a score breakdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "integer", "description": "The database ID of the page"},
                    "page_type": {"type": "string", "description": "Either 'synced' or 'builder'", "default": "synced"},
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_products_for_showcase",
            "description": "Search store products for use in product showcase sections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {"type": "integer", "description": "The database ID of the store"},
                    "keyword": {"type": "string", "description": "Search keyword"},
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_collections_for_linking",
            "description": "Get collection titles and handles for internal linking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {"type": "integer", "description": "The database ID of the store"},
                },
                "required": ["store_id"],
            },
        },
    },
]


def _handle_landing_page_tool_call(name, args):
    if name == "get_landing_page_data":
        page_id = args["page_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
            page = cur.fetchone()
        if not page:
            return {"error": f"Landing page {page_id} not found"}
        sections = page.get("sections") or []
        if isinstance(sections, str):
            sections = json.loads(sections)
        return {
            "id": page["id"],
            "title": page["title"],
            "handle": page["handle"],
            "meta_title": page["meta_title"],
            "meta_description": page["meta_description"],
            "sections": sections,
            "seo_score": page["seo_score"],
        }

    if name == "get_synced_page_data":
        page_id = args["page_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_pages WHERE id = %s", (page_id,))
            page = cur.fetchone()
        if not page:
            return {"error": f"Synced page {page_id} not found"}
        return {
            "id": page["id"],
            "title": page["title"],
            "handle": page["handle"],
            "body_html": page["body_html"],
            "seo_title": page["seo_title"],
            "seo_description": page["seo_description"],
            "is_published": page["is_published"],
            "is_homepage": page["is_homepage"],
            "seo_score": page["seo_score"],
        }

    if name == "analyze_landing_page_seo":
        page_id = args["page_id"]
        page_type = args.get("page_type", "synced")
        with db.get_cursor() as cur:
            if page_type == "builder":
                cur.execute("SELECT * FROM landing_pages WHERE id = %s", (page_id,))
            else:
                cur.execute("SELECT * FROM synced_pages WHERE id = %s", (page_id,))
            page = cur.fetchone()
        if not page:
            return {"error": f"Page {page_id} not found"}
        if page_type == "builder":
            return analyze_landing_page(dict(page))
        return analyze_synced_page(dict(page))

    if name == "get_store_products_for_showcase":
        sid = args["store_id"]
        keyword = args.get("keyword", "")
        pattern = f"%{keyword}%"
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT title, handle, images FROM synced_products
                WHERE store_id = %s AND status = 'ACTIVE'
                  AND (title ILIKE %s OR tags ILIKE %s OR product_type ILIKE %s)
                LIMIT 20""",
                (sid, pattern, pattern, pattern),
            )
            rows = cur.fetchall()
        results = []
        for r in rows:
            images = r.get("images") or []
            if isinstance(images, str):
                images = json.loads(images)
            image_url = images[0].get("url", "") if images else ""
            results.append({"title": r["title"], "handle": r["handle"], "image_url": image_url})
        return results

    if name == "get_store_collections_for_linking":
        sid = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' ORDER BY title LIMIT 50",
                (sid,),
            )
            return [{"title": r["title"], "handle": r["handle"]} for r in cur.fetchall()]

    return {"error": f"Unknown landing page tool: {name}"}


def _handle_article_tool_call(name, args):
    if name == "get_article_data":
        article_id = args["article_id"]
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT sa.*, sb.title as blog_title, sb.handle as blog_handle
                FROM synced_articles sa
                JOIN synced_blogs sb ON sb.id = sa.blog_id
                WHERE sa.id = %s""",
                (article_id,),
            )
            article = cur.fetchone()
        if not article:
            return {"error": f"Article {article_id} not found"}
        return {
            "id": article["id"],
            "title": article["title"],
            "handle": article["handle"],
            "body": article["body"],
            "summary": article["summary"],
            "tags": article["tags"],
            "author": article["author"],
            "meta_title": article["meta_title"],
            "meta_description": article["meta_description"],
            "image_url": article["image_url"],
            "image_alt": article["image_alt"],
            "blog_title": article["blog_title"],
            "blog_handle": article["blog_handle"],
            "is_published": article["is_published"],
        }

    if name == "analyze_article_seo_score":
        article_id = args["article_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
            article = cur.fetchone()
        if not article:
            return {"error": f"Article {article_id} not found"}
        return analyze_article_seo(dict(article))

    if name == "get_store_products_for_linking":
        sid = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' ORDER BY title LIMIT 50",
                (sid,),
            )
            return [{"title": r["title"], "handle": r["handle"]} for r in cur.fetchall()]

    if name == "get_store_collections_for_linking":
        sid = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' ORDER BY title LIMIT 50",
                (sid,),
            )
            return [{"title": r["title"], "handle": r["handle"]} for r in cur.fetchall()]

    if name == "get_store_articles_for_linking":
        sid = args["store_id"]
        exclude_id = args.get("exclude_article_id")
        with db.get_cursor() as cur:
            if exclude_id:
                cur.execute(
                    """SELECT sa.title, sa.handle, sb.handle as blog_handle
                    FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                    WHERE sa.store_id = %s AND sa.id != %s ORDER BY sa.title LIMIT 50""",
                    (sid, exclude_id),
                )
            else:
                cur.execute(
                    """SELECT sa.title, sa.handle, sb.handle as blog_handle
                    FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                    WHERE sa.store_id = %s ORDER BY sa.title LIMIT 50""",
                    (sid,),
                )
            return [
                {"title": r["title"], "handle": r["handle"], "blog_handle": r["blog_handle"]}
                for r in cur.fetchall()
            ]

    return {"error": f"Unknown article tool: {name}"}


def _handle_tool_call(name, args):
    if name == "get_product_data":
        product_id = args["product_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            product = cur.fetchone()
        if not product:
            return {"error": f"Product {product_id} not found"}
        return {
            "id": product["id"],
            "title": product["title"],
            "handle": product["handle"],
            "description": product["description"],
            "vendor": product["vendor"],
            "product_type": product["product_type"],
            "tags": product["tags"],
            "meta_title": product["meta_title"],
            "meta_description": product["meta_description"],
            "images": product["images"],
            "variants": product["variants"],
            "status": product["status"],
        }

    if name == "analyze_seo_score":
        product_id = args["product_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            product = cur.fetchone()
        if not product:
            return {"error": f"Product {product_id} not found"}
        return analyze_product_seo(dict(product))

    if name == "get_store_summary":
        store_id = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as total FROM synced_products WHERE store_id = %s",
                (store_id,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                "SELECT * FROM synced_products WHERE store_id = %s LIMIT 100",
                (store_id,),
            )
            products = cur.fetchall()

        scores = [analyze_product_seo(dict(p))["score"] for p in products]
        avg_score = round(sum(scores) / len(scores)) if scores else 0
        low_score_count = sum(1 for s in scores if s < 50)

        return {
            "store_id": store_id,
            "total_products": total,
            "analyzed_count": len(scores),
            "average_seo_score": avg_score,
            "products_below_50": low_score_count,
        }

    if name == "get_collection_data":
        collection_id = args["collection_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()
        if not collection:
            return {"error": f"Collection {collection_id} not found"}
        return {
            "id": collection["id"],
            "title": collection["title"],
            "handle": collection["handle"],
            "description": collection["description"],
            "meta_title": collection["meta_title"],
            "meta_description": collection["meta_description"],
            "products_count": collection["products_count"],
            "image_url": collection["image_url"],
        }

    if name == "analyze_collection_seo_score":
        collection_id = args["collection_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()
        if not collection:
            return {"error": f"Collection {collection_id} not found"}
        return analyze_collection_seo(dict(collection))

    if name == "get_search_performance":
        item_id = args["item_id"]
        item_type = args.get("item_type", "product")
        with db.get_cursor() as cur:
            table = "synced_products" if item_type == "product" else "synced_collections"
            cur.execute(f"SELECT store_id FROM {table} WHERE id = %s", (item_id,))
            row = cur.fetchone()
        if not row:
            return {"error": f"{item_type} {item_id} not found"}
        context = _get_search_context_for_item(item_type, item_id, row["store_id"])
        return {"search_context": context} if context else {"message": "No search data available for this item"}

    if name == "get_keyword_opportunities":
        sid = args["store_id"]
        from app.services.semrush_import_service import get_striking_distance
        striking = get_striking_distance(sid, pos_min=4, pos_max=20, limit=20)
        return {
            "striking_distance_keywords": striking[:20],
            "count": len(striking),
        }

    return {"error": f"Unknown tool: {name}"}


def _handle_keyword_tool_call(name, args):
    if name == "get_all_product_summaries":
        sid = args["store_id"]
        offset = max(args.get("offset", 0), 0)
        limit = min(max(args.get("limit", 100), 1), 200)
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as total FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
                (sid,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                """SELECT id, title, product_type, vendor, tags
                FROM synced_products
                WHERE store_id = %s AND status = 'ACTIVE'
                ORDER BY id
                OFFSET %s LIMIT %s""",
                (sid, offset, limit),
            )
            rows = cur.fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "products": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "product_type": r["product_type"],
                    "vendor": r["vendor"],
                    "tags": r["tags"],
                }
                for r in rows
            ],
        }
    return {"error": f"Unknown keyword tool: {name}"}


def _handle_discovery_tool_call(name, args, store_id):
    if name == "search_store_products":
        keyword = args.get("keyword", "")
        pattern = f"%{keyword}%"
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT title, handle FROM synced_products
                WHERE store_id = %s AND status = 'ACTIVE'
                  AND (title ILIKE %s OR tags ILIKE %s OR product_type ILIKE %s)
                LIMIT 20""",
                (store_id, pattern, pattern, pattern),
            )
            rows = cur.fetchall()
        return [{"title": r["title"], "handle": r["handle"]} for r in rows]
    return {"error": f"Unknown discovery tool: {name}"}



def _handle_topic_tool_call(name, args):
    if name == "search_web":
        from app.services.serp_scraper import brave_search
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        count = min(int(args.get("count") or 10), 20)
        results = brave_search(query, count)
        if results is None:
            return {
                "error": (
                    "Web search unavailable (Brave API key missing or request failed). "
                    "Proceed using store data and existing-article gap analysis only."
                )
            }
        return {"results": results}

    if name == "get_store_overview":
        store_id = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT COALESCE(product_type, '(none)') AS product_type, COUNT(*) AS count "
                "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
                "GROUP BY product_type ORDER BY count DESC",
                (store_id,),
            )
            types = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT COALESCE(vendor, '(none)') AS vendor, COUNT(*) AS count "
                "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
                "GROUP BY vendor ORDER BY count DESC LIMIT 20",
                (store_id,),
            )
            vendors = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT COUNT(*) AS total FROM synced_products "
                "WHERE store_id = %s AND status = 'ACTIVE'",
                (store_id,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                "SELECT title, handle, products_count FROM synced_collections "
                "WHERE store_id = %s ORDER BY products_count DESC NULLS LAST LIMIT 100",
                (store_id,),
            )
            collections = [dict(r) for r in cur.fetchall()]
        return {
            "total_active_products": total,
            "product_types": types,
            "top_vendors": vendors,
            "collections": collections,
        }

    if name == "search_store_products":
        store_id = args["store_id"]
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        limit = min(int(args.get("limit") or 20), 50)
        like = f"%{query}%"
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, title, handle, product_type, vendor FROM synced_products "
                "WHERE store_id = %s AND status = 'ACTIVE' AND "
                "(title ILIKE %s OR product_type ILIKE %s OR vendor ILIKE %s OR tags ILIKE %s) "
                "ORDER BY title LIMIT %s",
                (store_id, like, like, like, like, limit),
            )
            products = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT COUNT(*) AS total FROM synced_products "
                "WHERE store_id = %s AND status = 'ACTIVE' AND "
                "(title ILIKE %s OR product_type ILIKE %s OR vendor ILIKE %s OR tags ILIKE %s)",
                (store_id, like, like, like, like),
            )
            total = cur.fetchone()["total"]
        return {"total_matching": total, "products": products}

    if name == "get_existing_articles":
        store_id = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title, handle, tags, is_published FROM synced_articles "
                "WHERE store_id = %s ORDER BY published_at DESC NULLS LAST LIMIT 300",
                (store_id,),
            )
            articles = [dict(r) for r in cur.fetchall()]
            # Queued/in-progress wizard articles aren't in synced_articles until
            # they publish — include them so cannibalization checks see them.
            cur.execute(
                "SELECT COALESCE(generated_article->>'title', topic) AS title "
                "FROM article_wizard_sessions WHERE store_id = %s AND status != 'published' "
                "AND topic IS NOT NULL AND topic != '' "
                "AND updated_at > NOW() - INTERVAL '90 days' "
                "ORDER BY updated_at DESC LIMIT 50",
                (store_id,),
            )
            for r in cur.fetchall():
                articles.append({
                    "title": r["title"], "handle": None, "tags": None,
                    "is_published": False, "status": "queued",
                })
        return {"count": len(articles), "articles": articles}

    if name == "get_keyword_opportunities":
        return _handle_tool_call(name, args)

    return {"error": f"Unknown topic discovery tool: {name}"}


def _get_ai_config():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI not configured. Please set your OpenRouter API key in Settings.")
    return {
        "api_key": decrypt_value(config["api_key_enc"]),
        "model": config.get("model") or "anthropic/claude-sonnet-4",
        "image_model": config.get("image_model") or "google/gemini-2.5-flash-image",
        "topic_research_model": config.get("topic_research_model") or None,
        "topic_suggestion_count": int(config.get("topic_suggestion_count") or 3),
    }


DEFAULT_TARGET_WORD_COUNT = 500


def _sanitize_description(html: str) -> str:
    """Strip AI meta-artifacts (word counts, notes, disclaimers) from generated HTML."""
    html = re.sub(
        r"<p>\s*\(?\s*[Ww]ord\s*[Cc]ount\s*[:：]\s*\d+\s*(?:words?)?\s*\)?\s*</p>",
        "",
        html,
    )
    html = re.sub(
        r"<p>\s*\*{0,2}[Nn]ote\s*[:：]\s*\*{0,2}.*?</p>",
        "",
        html,
    )
    html = re.sub(
        r"(?:<hr\s*/?>|---)\s*(?:<p>.*?</p>\s*)*$",
        "",
        html,
    )
    html = re.sub(
        r"\(?\s*[Ww]ord\s*[Cc]ount\s*[:：]\s*\d+\s*(?:words?)?\s*\)?",
        "",
        html,
    )
    # The theme renders the title as the page H1 — a second h1 in the body
    # would be a duplicate; demote rather than strip so no content is lost.
    from app.services.ai_output_guard import demote_h1
    html = demote_h1(html)
    return html.strip()


def _build_faqpage_jsonld(faq):
    """Construct a valid schema.org FAQPage from question/answer pairs."""
    entities = [
        {
            "@type": "Question",
            "name": f.get("question", ""),
            "acceptedAnswer": {"@type": "Answer", "text": f.get("answer", "")},
        }
        for f in (faq or [])
        if isinstance(f, dict) and f.get("question") and f.get("answer")
    ]
    if not entities:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities,
    }


_GEO_STOPWORDS = {
    "the", "and", "for", "with", "pack", "packs", "box", "count", "from", "your", "our",
    "best", "buy", "shop", "online", "sale", "new", "pre", "priced", "kit", "set", "free",
    "shipping", "great", "quality", "premium", "product", "products", "item", "items", "this",
    "that", "these", "those", "each", "per", "you", "are", "was", "will", "can", "more",
}


def _geo_significant_terms(text):
    """Distinctive single words worth preserving from a title/meta field."""
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9&'-]{2,}", (text or "").lower()):
        if len(w) >= 4 and w not in _GEO_STOPWORDS and w not in out:
            out.append(w)
    return out


def _ranking_keywords_for_item(item_type, item_id, store_id, limit=12):
    """Top GSC queries + URL-matched Semrush keywords for a product/collection — the
    phrases the page already ranks for, which must survive optimization."""
    from urllib.parse import urlparse as _urlparse

    kws = []
    try:
        table = "synced_products" if item_type == "product" else "synced_collections"
        with db.get_cursor() as cur:
            cur.execute(f"SELECT handle, store_id FROM {table} WHERE id = %s", (item_id,))
            row = cur.fetchone()
            if not row or not row.get("handle"):
                return []
            sid = store_id or row["store_id"]
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (sid,))
            cred = cur.fetchone()
        base = (cred["gsc_property"].rstrip("/") if cred and cred.get("gsc_property") else "")
        path = f"{'products' if item_type == 'product' else 'collections'}/{row['handle']}"
        page_url = f"{base}/{path}" if base else ""
        if page_url:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT query FROM gsc_daily_metrics
                       WHERE store_id = %s AND page = %s AND date >= (CURRENT_DATE - INTERVAL '30 days')
                       GROUP BY query ORDER BY SUM(impressions) DESC LIMIT %s""",
                    (sid, page_url, limit),
                )
                kws += [r["query"] for r in cur.fetchall() if r["query"]]
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT keyword, url FROM semrush_keywords
                   WHERE store_id = %s AND url IS NOT NULL AND url <> ''
                   ORDER BY search_volume DESC NULLS LAST LIMIT 500""",
                (sid,),
            )
            for r in cur.fetchall():
                u = r["url"] or ""
                p = (u.replace(base, "").strip("/") if base else "") or _urlparse(u).path.strip("/")
                if p == path and r["keyword"]:
                    kws.append(r["keyword"])
                if len(kws) >= limit * 2:
                    break
    except Exception as e:
        logger.warning("Ranking-keyword lookup failed for %s %s: %s", item_type, item_id, e)
        return []

    seen, out = set(), []
    for k in kws:
        kl = (k or "").strip().lower()
        if kl and kl not in seen:
            seen.add(kl)
            out.append(k.strip())
    return out[:limit]


def _geo_preserve_keywords(item, entity_type):
    """Build the MUST-PRESERVE keyword list: existing meta/title terms + ranking phrases."""
    terms = _ranking_keywords_for_item(entity_type, item.get("id"), item.get("store_id"))
    lower = {t.lower() for t in terms}
    for src in (item.get("meta_title"), item.get("title"), item.get("vendor"), item.get("product_type")):
        for t in _geo_significant_terms(src):
            if t not in lower:
                terms.append(t)
                lower.add(t)
    return terms[:25]


_PACK_RE = re.compile(
    r"\b\d+\s*(?:ct|count|pack|packs|pk|pouch(?:es)?|cigarillos?|cigars?|pieces?|pcs|ml|mg|oz|g|lb)\b[^,;–—-]{0,20}",
    re.I,
)
# Variant titles that are pricing/promo tiers, not real product options.
_VARIANT_PROMO_RE = re.compile(r"^\s*\$?\d|save|\bbuy\b|\bdeal\b|%|\beach\b|\bpack of\b|\bcents?\b|c\s*$|/", re.I)
_SPEC_NAMESPACES = {"specs", "spec", "specification", "specifications", "custom", "details", "attributes", "facts"}
_SPEC_SKIP_NS = {"seo", "reviews", "judgeme", "loox", "yotpo", "global", "descriptors", "shopify"}
_SPEC_TYPES = {"single_line_text_field", "number_integer", "number_decimal", "boolean", "rating", "dimension", "weight", "volume"}
_SPEC_SKIP_KEYS = {"schema_json_ld", "faq_schema_json_ld"}


def _geo_fetch_metafield_specs(store_id, shopify_id):
    """Best-effort: pull spec-like metafields (namespace/type filtered) as {Label: value}."""
    from app.shopify_client import ShopifyClient

    with db.get_cursor() as cur:
        cur.execute("SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s", (store_id,))
        s = cur.fetchone()
    if not s:
        return {}
    client = ShopifyClient(s["domain"], decrypt_value(s["access_token_enc"]), s["api_version"])
    out = {}
    for mf in client.get_product_metafields(shopify_id):
        ns = (mf.get("namespace") or "").lower()
        key = mf.get("key") or ""
        typ = mf.get("type") or ""
        val = (mf.get("value") or "").strip()
        if key in _SPEC_SKIP_KEYS or ns in _SPEC_SKIP_NS or ns not in _SPEC_NAMESPACES:
            continue
        if typ not in _SPEC_TYPES or not val or len(val) > 80 or val[:1] in "[{":
            continue
        out[key.replace("_", " ").replace("-", " ").strip().title()] = val
        if len(out) >= 15:
            break
    return out


def _geo_product_facts(item, entity_type, include_metafields=True):
    """Verified facts (from store data) the model may safely state and tabulate.

    include_metafields=False skips the per-product Shopify metafield lookup — used
    by the synchronous bulk schema generate so a full-catalog run stays within the
    request timeout (variant/title-derived facts still populate).
    """
    facts = {}
    if item.get("vendor"):
        facts["Brand"] = item["vendor"].strip()
    ptype = (item.get("product_type") or item.get("collection_type") or "").strip()
    if ptype:
        facts["Type"] = ptype
    packs = _PACK_RE.findall(item.get("title") or "")
    if packs:
        facts["Pack / size"] = "; ".join(dict.fromkeys(p.strip() for p in packs))[:120]

    variants = item.get("variants") or []
    if isinstance(variants, str):
        try:
            variants = json.loads(variants)
        except (json.JSONDecodeError, ValueError):
            variants = []
    if variants:
        opts = list(dict.fromkeys(
            v.get("title") for v in variants
            if v.get("title") and v.get("title") != "Default Title" and not _VARIANT_PROMO_RE.search(v["title"])
        ))
        if len(opts) > 1:
            facts["Options / flavors"] = ", ".join(opts[:12])
        prices = []
        for v in variants:
            try:
                prices.append(float(v.get("price")))
            except (ValueError, TypeError):
                pass
        if prices:
            lo, hi = min(prices), max(prices)
            facts["Price"] = f"${lo:.2f}" if lo == hi else f"${lo:.2f}–${hi:.2f}"
        if any("available" in v for v in variants):
            facts["Availability"] = "In stock" if any(v.get("available") for v in variants) else "Out of stock"
        if variants[0].get("sku"):
            facts["SKU"] = variants[0]["sku"]
        if variants[0].get("barcode"):
            facts["Barcode / GTIN"] = variants[0]["barcode"]

    if include_metafields and entity_type == "product" and item.get("shopify_id") and item.get("store_id"):
        try:
            facts.update(_geo_fetch_metafield_specs(item["store_id"], item["shopify_id"]))
        except Exception as e:
            logger.warning("Metafield spec fetch failed for %s: %s", item.get("id"), e)
    return facts


def _collection_top_members(store_id, collection_shopify_id, limit=5):
    """Verified member products of a collection, for a grounded comparison matrix.

    Returns compact rows (title, brand, price, availability) from the store's own
    synced data — never fabricated. Ordered in-stock-first then cheapest (a stable,
    honest ordering; we have no real sales-rank data).
    """
    if not store_id or not collection_shopify_id:
        return []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sp.title, sp.vendor, sp.variants
               FROM collection_product_map m
               JOIN synced_products sp
                 ON sp.shopify_id = m.product_shopify_id AND sp.store_id = m.store_id
               WHERE m.store_id = %s AND m.collection_shopify_id = %s
               LIMIT 200""",
            (store_id, collection_shopify_id),
        )
        rows = cur.fetchall()
    members = []
    for r in rows:
        variants = r["variants"]
        if isinstance(variants, str):
            try:
                variants = json.loads(variants)
            except (json.JSONDecodeError, ValueError):
                variants = []
        variants = variants or []
        prices = []
        for v in variants:
            try:
                prices.append(float(v.get("price")))
            except (ValueError, TypeError):
                pass
        has_avail = any("available" in v for v in variants)
        available = any(v.get("available") for v in variants) if has_avail else None
        if prices:
            lo, hi = min(prices), max(prices)
            price = f"${lo:.2f}" if lo == hi else f"${lo:.2f}–${hi:.2f}"
        else:
            lo, price = float("inf"), ""
        members.append(
            {
                "title": (r["title"] or "").strip(),
                "brand": (r["vendor"] or "").strip(),
                "price": price,
                "available": available,
                "_lo": lo,
            }
        )
    members.sort(key=lambda m: (m["available"] is False, m["_lo"]))
    return [{k: v for k, v in m.items() if k != "_lo"} for m in members[:limit] if m["title"]]


_ENRICH_STOPWORDS = {
    "the", "and", "for", "with", "best", "guide", "test", "ultimate", "your",
    "from", "that", "this", "buy", "online", "how", "what", "which", "where",
}


def _topic_enrich_context(store_id, topic, primary):
    """Store context for enriching one topic suggestion: inventory profile,
    coverage lists for the overlap guard, and REAL candidate link targets whose
    names will resolve at schedule time."""
    tokens = [
        t for t in re.findall(r"[a-z0-9']+", f"{topic} {primary}".lower())
        if len(t) > 2 and t not in _ENRICH_STOPWORDS
    ]

    def _match(rows):
        out = []
        for r in rows:
            title = (r["title"] or "").lower()
            hits = sum(1 for t in tokens if t in title or t.rstrip("s") in title)
            if hits:
                out.append((hits, r["title"]))
        out.sort(key=lambda x: -x[0])
        return [t for _, t in out[:12]]

    with db.get_cursor() as cur:
        cur.execute("SELECT name, domain FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone() or {}
        cur.execute(
            "SELECT COALESCE(product_type, '(none)') AS product_type, COUNT(*) AS count "
            "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
            "GROUP BY product_type ORDER BY count DESC LIMIT 40",
            (store_id,),
        )
        types = cur.fetchall()
        cur.execute(
            "SELECT COALESCE(vendor, '(none)') AS vendor, COUNT(*) AS count "
            "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
            "GROUP BY vendor ORDER BY count DESC LIMIT 25",
            (store_id,),
        )
        vendors = cur.fetchall()
        cur.execute("SELECT title FROM synced_collections WHERE store_id = %s", (store_id,))
        coll_titles = _match(cur.fetchall())
        cur.execute(
            "SELECT title FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
            (store_id,),
        )
        prod_titles = _match(cur.fetchall())
        cur.execute(
            "SELECT title FROM synced_articles WHERE store_id = %s "
            "ORDER BY published_at DESC NULLS LAST LIMIT 200",
            (store_id,),
        )
        articles = [a["title"] for a in cur.fetchall()]
        cur.execute(
            "SELECT COALESCE(generated_article->>'title', topic) AS title "
            "FROM article_wizard_sessions WHERE store_id = %s AND status != 'published' "
            "AND topic IS NOT NULL AND topic != '' "
            "AND updated_at > NOW() - INTERVAL '90 days' LIMIT 50",
            (store_id,),
        )
        queued = [q["title"] for q in cur.fetchall()]
        cur.execute(
            "SELECT topic FROM topic_suggestions WHERE store_id = %s AND status = 'suggested' "
            "ORDER BY created_at DESC LIMIT 50",
            (store_id,),
        )
        others = [o["topic"] for o in cur.fetchall() if o["topic"] != topic]

    return {
        "store_name": store.get("name") or "",
        "store_domain": store.get("domain") or "",
        "profile": "\n".join(f"- {t['product_type']}: {t['count']} products" for t in types),
        "vendors": ", ".join(f"{v['vendor']} ({v['count']})" for v in vendors),
        "collections": "\n".join(f"- {t}" for t in coll_titles),
        "products": "\n".join(f"- {t}" for t in prod_titles),
        "articles": "\n".join(f"- {t}" for t in articles),
        "queued": "\n".join(f"- {t}" for t in queued),
        "other_suggestions": "\n".join(f"- {t}" for t in others),
    }


def _geo_research(item):
    """Best-effort Brave web research for real product facts. Rate-limit-safe and cached
    (serp_scraper.brave_search sleeps between calls and backs off on 429)."""
    try:
        from app.services.serp_scraper import brave_search

        brand = (item.get("vendor") or "").strip()
        title = (item.get("title") or "").strip()
        base = title if (brand and brand.lower() in title.lower()) or not brand else f"{brand} {title}"
        results = brave_search(f"{base} specifications review".strip(), count=5)
        if not results:
            return ""
        lines = []
        for r in results[:4]:
            desc = (r.get("description") or "").strip()
            if desc:
                lines.append(f"- {r.get('domain', '')}: {desc[:220]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("GEO Brave research failed: %s", e)
        return ""


_QUOTED_SPAN_RE = re.compile(r"[\"“”]([^\"“”]{20,300})[\"“”]")


def _quote_guard_norm(text):
    text = re.sub(r"<[^>]+>", " ", text or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text)).strip()


def _strip_unverifiable_quotes(description_html, source_text):
    """Remove quotes the model cannot have taken verbatim from its inputs.

    GEO's quotation lever only works with real quotes — an invented attribution is
    a brand-risk event at batch scale, so any blockquote or long quoted span not
    traceable to the provided context (existing copy, store facts, web research)
    is cut, blockquotes alone and inline quotes with their whole paragraph/list item.
    Returns (clean_html, removed_snippets)."""
    if not description_html:
        return description_html, []
    corpus = _quote_guard_norm(source_text)
    removed = []

    def _verified(quote):
        q = _quote_guard_norm(quote)
        return len(q) < 20 or q in corpus

    def _drop_bad_blockquote(m):
        if _verified(m.group(1)):
            return m.group(0)
        removed.append(re.sub(r"<[^>]+>", " ", m.group(1)).strip()[:120])
        return ""

    html = re.sub(
        r"<blockquote[^>]*>(.*?)</blockquote>", _drop_bad_blockquote, description_html, flags=re.S | re.I
    )

    def _drop_bad_container(m):
        for qm in _QUOTED_SPAN_RE.finditer(m.group(1)):
            if not _verified(qm.group(1)):
                removed.append(qm.group(1).strip()[:120])
                return ""
        return m.group(0)

    for tag in ("p", "li"):
        html = re.sub(rf"<{tag}[^>]*>(.*?)</{tag}>", _drop_bad_container, html, flags=re.S | re.I)
    return html, removed


def _generate_geo_content(client, config, item, entity_type, weak_signals=None):
    """Single structured LLM call to GEO-optimize a product/collection.

    Enhances existing content (never wholesale-replaces): the current meta and
    description are given to the model with a MUST-PRESERVE keyword list, verified
    product facts (for a Specifications table + Expert Verdict), and best-effort web
    research. A deterministic check reports any existing keyword dropped from the
    result. Returns a review-ready dict (no writes).
    """
    system_prompt = get_setting(
        f"prompts.ai_seo_{entity_type}", DEFAULTS[f"prompts.ai_seo_{entity_type}"]
    )
    weak_block = ""
    if weak_signals:
        weak_block = (
            "\n\nThe current page scores weakest on these AI-readiness signals — "
            "prioritise fixing them:\n" + "\n".join(f"- {s}" for s in weak_signals)
        )
    preserve = _geo_preserve_keywords(item, entity_type)

    # The resolved primary (keyword strategy) anchors the rewrite — GEO must
    # enhance the page, never retarget it away from its strategic keyword.
    primary_block = ""
    try:
        from app.services.keyword_resolver import get_or_refresh_resolution

        resolution = get_or_refresh_resolution(item.get("store_id"), entity_type, item.get("id"))
        rp = (resolution or {}).get("primary")
        if rp and rp.get("keyword"):
            primary_block = (
                f"\n\nPRIMARY KEYWORD (resolved by the store's keyword strategy — this page's "
                f"target): \"{rp['keyword']}\". Keep it in the meta title (at or near the front), "
                f"in the meta description, and in the opening of the body. Do NOT retarget the "
                f"page to a different keyword."
            )
            if rp["keyword"].lower() not in {k.lower() for k in preserve}:
                preserve.append(rp["keyword"])
    except Exception:
        logger.warning("GEO: resolution lookup failed for %s %s", entity_type, item.get("id"))

    preserve_block = ""
    if preserve:
        preserve_block = (
            "\n\nMUST-PRESERVE KEYWORDS — keep EVERY one of these appearing verbatim in the "
            "optimized content (meta and/or body); they carry existing SEO value and ranking:\n"
            + ", ".join(preserve)
        )

    facts = _geo_product_facts(item, entity_type)
    facts_block = ""
    if facts:
        facts_block = (
            "\n\nVERIFIED PRODUCT FACTS (from the store's own data — safe to state and to render as a "
            "Specifications table; use ONLY these values, do not invent others):\n"
            + "\n".join(f"- {k}: {v}" for k, v in facts.items())
        )
    research = _geo_research(item)
    research_block = ""
    if research:
        research_block = (
            "\n\nWEB RESEARCH CONTEXT (background only — include a detail ONLY if it clearly refers to "
            "THIS exact product and you are confident it is accurate; otherwise ignore it):\n" + research
        )

    members_block = ""
    if entity_type == "collection":
        members = _collection_top_members(item.get("store_id"), item.get("shopify_id"))
        if len(members) >= 2:
            lines = [
                f"- {m['title']} | Brand: {m['brand'] or 'n/a'} | Price: {m['price'] or 'n/a'} | "
                f"{'In stock' if m['available'] else ('Out of stock' if m['available'] is False else 'Availability: n/a')}"
                for m in members
            ]
            members_block = (
                "\n\nFEATURED PRODUCTS IN THIS COLLECTION (real member products from the store — render "
                "these as a COMPARISON MATRIX table; use ONLY these exact rows and values, never add "
                "products or invent data):\n" + "\n".join(lines)
            )

    # Deterministic per-item style variation so a batch run doesn't stamp one
    # identical section template across the whole catalog (a scaled-content footprint).
    style_variants = [
        "Name the facts table 'Specifications' and the closing summary 'Expert Verdict'; place the summary last.",
        "Name the facts table 'At a glance' and the closing summary 'Our verdict'; put the table right after the answer-first lead.",
        "Name the facts table 'Key details' and the closing summary 'The bottom line'; phrase section headings as How/Why questions.",
        "Name the facts table 'Quick facts' and the closing summary 'Who it's for'; phrase section headings as What/Which questions.",
    ]
    style_block = (
        "\n\nSTYLE VARIATION (apply so optimized pages don't all share one template): "
        + style_variants[int(item.get("id") or 0) % len(style_variants)]
    )

    user_prompt = (
        f"{entity_type.capitalize()} title: {item.get('title') or ''}\n"
        f"Brand/Vendor: {item.get('vendor') or 'n/a'}\n"
        f"Type: {item.get('product_type') or item.get('collection_type') or 'n/a'}\n"
        f"Current meta title: {item.get('meta_title') or '(none)'}\n"
        f"Current meta description: {item.get('meta_description') or '(none)'}\n\n"
        f"Current description HTML:\n{item.get('description') or '(none)'}"
        f"{primary_block}"
        f"{facts_block}"
        f"{members_block}"
        f"{research_block}"
        f"{preserve_block}"
        f"{weak_block}"
        f"{style_block}"
    )
    resp = client.chat.completions.create(
        model=config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )
    content = resp.choices[0].message.content or ""
    start, end = content.find("{"), content.rfind("}") + 1
    data = {}
    if start >= 0 and end > start:
        try:
            data = json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            data = {}
    if not data:
        return {"error": "AI did not return valid GEO content"}

    faq = data.get("faq") or []
    description = _sanitize_description(
        data.get("optimized_description_html") or data.get("description_html") or ""
    )
    # Quotes must be traceable verbatim to what the model was given — anything
    # else is presumed fabricated and cut before the user ever previews it.
    quote_sources = " ".join(
        [item.get("description") or "", item.get("title") or "", research]
        + [str(v) for v in facts.values()]
    )
    description, removed_quotes = _strip_unverifiable_quotes(description, quote_sources)
    meta_title = (data.get("meta_title") or "").strip()
    meta_description = (data.get("meta_description") or "").strip()

    # Deterministic keyword-preservation check across the new meta + body.
    new_text = (
        re.sub(r"<[^>]+>", " ", description) + " " + meta_title + " " + meta_description
    ).lower()
    preserved_keywords = [k for k in preserve if k.lower() in new_text]
    dropped_keywords = [k for k in preserve if k.lower() not in new_text]

    return {
        "optimized_description_html": description,
        "faq": faq,
        "faqpage_jsonld": _build_faqpage_jsonld(faq),
        "meta_title": meta_title,
        "meta_description": meta_description,
        "geo_edits_applied": data.get("geo_edits_applied") or [],
        "preserved_keywords": preserved_keywords,
        "dropped_keywords": dropped_keywords,
        "removed_quotes": removed_quotes,
        "expert_verdict": (data.get("expert_verdict") or "").strip(),
        "product_facts": facts,
    }


def _generate_fact_boost(client, config, item, entity_type):
    """Dedicated fact-density pass: research + weave in verifiable, relevant facts
    while preserving everything. Returns {"no_change": True, ...} if nothing solid
    is found (never fabricates)."""
    system_prompt = get_setting("prompts.ai_seo_facts", DEFAULTS["prompts.ai_seo_facts"])
    facts = _geo_product_facts(item, entity_type)
    research = _geo_research(item)
    facts_block = "\n".join(f"- {k}: {v}" for k, v in facts.items()) or "(none)"
    primary_note = ""
    try:
        from app.services.keyword_resolver import get_or_refresh_resolution

        resolution = get_or_refresh_resolution(item.get("store_id"), entity_type, item.get("id"))
        rp = (resolution or {}).get("primary")
        if rp and rp.get("keyword"):
            primary_note = (
                f"Primary keyword (must remain present and prominent in the rewritten "
                f"description): \"{rp['keyword']}\"\n"
            )
    except Exception:
        pass
    user_prompt = (
        f"{entity_type.capitalize()} title: {item.get('title') or ''}\n"
        f"Brand/Vendor: {item.get('vendor') or 'n/a'}\n"
        f"Type: {item.get('product_type') or item.get('collection_type') or 'n/a'}\n"
        f"{primary_note}\n"
        f"Current description HTML:\n{item.get('description') or '(none)'}\n\n"
        f"VERIFIED PRODUCT FACTS (store data — safe to state):\n{facts_block}\n\n"
        f"WEB RESEARCH CONTEXT (only use a detail if clearly about THIS exact item):\n"
        f"{research or '(no research results)'}"
    )
    resp = client.chat.completions.create(
        model=config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
    )
    content = resp.choices[0].message.content or ""
    start, end = content.find("{"), content.rfind("}") + 1
    data = {}
    if start >= 0 and end > start:
        try:
            data = json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            data = {}
    if not data:
        return {"no_change": True, "reason": "AI did not return valid output"}
    if data.get("no_change"):
        return {"no_change": True, "reason": data.get("reason") or "No verifiable, relevant facts found"}
    desc = _sanitize_description(data.get("optimized_description_html") or "")
    if not desc:
        return {"no_change": True, "reason": "No enriched content produced"}
    return {
        "no_change": False,
        "optimized_description_html": desc,
        "facts_added": data.get("facts_added") or [],
        "sources": data.get("sources") or [],
    }


def _generate_description(client, config, main_result, serp_context, serp_benchmark, product_id, search_context=None):
    """Dedicated AI call to generate an SEO-optimized product description.

    Uses all context from the main analysis call plus SERP data to produce
    a focused, high-quality HTML description.
    """
    try:
        target_word_count = DEFAULT_TARGET_WORD_COUNT
        if serp_benchmark and serp_benchmark.get("avg_word_count"):
            competitor_avg = serp_benchmark["avg_word_count"]
            # Always aim to exceed competitors by ~20%, never go below our default
            target_word_count = max(int(competitor_avg * 1.2), DEFAULT_TARGET_WORD_COUNT)

        logger.info(
            "Description generation starting — target: %d words, has_serp: %s",
            target_word_count,
            bool(serp_benchmark),
        )

        _desc_prompt_template = get_setting("prompts.product_description", DEFAULTS["prompts.product_description"])
        try:
            desc_system_prompt = _desc_prompt_template.format(target_word_count=target_word_count)
        except (KeyError, IndexError):
            desc_system_prompt = DEFAULTS["prompts.product_description"].format(target_word_count=target_word_count)

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            product = cur.fetchone()

        product_collections = []
        other_collections = []
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sc.title, sc.handle
                    FROM collection_product_map cpm
                    JOIN synced_collections sc
                      ON sc.shopify_id = cpm.collection_shopify_id
                     AND sc.store_id = cpm.store_id
                    WHERE cpm.product_shopify_id = %s
                      AND cpm.store_id = %s
                      AND sc.handle IS NOT NULL AND sc.handle != ''
                    ORDER BY sc.title""",
                    (product.get("shopify_id"), product.get("store_id")),
                )
                product_collections = cur.fetchall()

                existing_handles = {c["handle"] for c in product_collections}
                cur.execute(
                    """SELECT sc.title, sc.handle
                    FROM synced_collections sc
                    WHERE sc.store_id = %s
                      AND sc.handle IS NOT NULL AND sc.handle != ''
                    ORDER BY sc.title
                    LIMIT 30""",
                    (product.get("store_id"),),
                )
                all_collections = cur.fetchall()
                other_collections = [
                    c for c in all_collections if c["handle"] not in existing_handles
                ]
        except Exception:
            logger.exception("Failed to fetch collections for product interlinking")

        if product_collections:
            desc_system_prompt += (
                "\n- MUST include internal links to collections this product belongs to\n"
                "- Weave links naturally (e.g. 'part of our <a href=\"/collections/handle\">Collection Name</a> range')\n"
                "- Use relative URLs (/collections/{handle}) for portability\n"
                "- Anchor text should be descriptive — use collection names or descriptive phrases\n"
            )
        if other_collections:
            desc_system_prompt += (
                "\n- SHOULD include 2-4 links to other RELATED collections from the list provided\n"
                "- Only link to collections genuinely related to this product's category or use case\n"
            )

        user_parts = []

        if product:
            current_desc = product.get("description") or ""
            desc_word_count = len(current_desc.split()) if current_desc else 0
            user_parts.append(
                "PRODUCT DATA:\n"
                f"- Title: {product.get('title', 'N/A')}\n"
                f"- Vendor: {product.get('vendor', 'N/A')}\n"
                f"- Product Type: {product.get('product_type', 'N/A')}\n"
                f"- Current Tags: {product.get('tags', 'N/A')}\n"
                f"- Current Description Word Count: {desc_word_count}\n"
            )

        seo_parts = []
        if main_result.get("h1_tag"):
            seo_parts.append(f"- H1 Tag: {main_result['h1_tag']}")
        if main_result.get("optimized_meta_title"):
            seo_parts.append(f"- Meta Title: {main_result['optimized_meta_title']}")
        if main_result.get("optimized_meta_description"):
            seo_parts.append(f"- Meta Description: {main_result['optimized_meta_description']}")
        if main_result.get("keywords_used"):
            seo_parts.append(f"- Keywords to Incorporate: {', '.join(main_result['keywords_used'])}")
        if main_result.get("suggested_tags"):
            seo_parts.append(f"- Suggested Tags: {', '.join(main_result['suggested_tags'])}")
        if main_result.get("competitor_insights"):
            seo_parts.append(f"- Competitor Insights: {main_result['competitor_insights']}")
        if seo_parts:
            user_parts.append("SEO ANALYSIS (from main analysis):\n" + "\n".join(seo_parts))

        if serp_context:
            user_parts.append(f"SERP BENCHMARK:\n{serp_context}")

        if serp_benchmark and serp_benchmark.get("page_summaries"):
            h2_lines = []
            for s in serp_benchmark["page_summaries"][:5]:
                h2s = s.get("h2s", [])
                if h2s:
                    h2_lines.append(f"- {s['domain']}: {', '.join(h2s[:5])}")
            if h2_lines:
                user_parts.append(
                    "COMPETITOR H2 PATTERNS (use as structural inspiration):\n"
                    + "\n".join(h2_lines)
                )

        try:
            search_ctx = search_context
            if search_ctx is None and product and product.get("store_id"):
                search_ctx = _get_search_context_for_item("product", product_id, product["store_id"])
            if search_ctx:
                    user_parts.append(
                        "REAL SEARCH DATA (incorporate these keywords naturally):\n" + search_ctx
                    )
        except Exception:
            logger.warning("Search context for description generation failed for product %s", product_id)

        if product_collections:
            coll_text = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in product_collections)
            user_parts.append(
                "COLLECTIONS THIS PRODUCT BELONGS TO (link to these):\n" + coll_text
            )

        if other_collections:
            other_text = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in other_collections[:20])
            user_parts.append(
                "OTHER STORE COLLECTIONS (link to 2-4 related ones):\n" + other_text
            )

        user_parts.append(
            f"Write an SEO-optimized product description targeting ~{target_word_count} words (aim to be more comprehensive than competitors). "
            "Return raw HTML only."
        )

        desc_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": desc_system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.4,
        )

        desc_content = desc_response.choices[0].message.content or ""

        desc_content = re.sub(r"^```(?:html)?\s*\n?", "", desc_content)
        desc_content = re.sub(r"\n?```\s*$", "", desc_content)
        desc_content = _sanitize_description(desc_content)

        if desc_content:
            main_result["optimized_description"] = desc_content
            desc_words = len(desc_content.split())
            logger.info("Description generated — %d words (target: %d)", desc_words, target_word_count)
        else:
            logger.warning("Description generation returned empty content")

    except Exception:
        logger.exception("Description generation failed")

    return main_result


def _generate_collection_description(client, config, main_result, serp_context, serp_benchmark, collection_id, hierarchy_context=None, search_context=None):
    try:
        target_word_count = DEFAULT_TARGET_WORD_COUNT
        if serp_benchmark and serp_benchmark.get("avg_word_count"):
            competitor_avg = serp_benchmark["avg_word_count"]
            target_word_count = max(int(competitor_avg * 1.2), DEFAULT_TARGET_WORD_COUNT)

        logger.info(
            "Collection description generation starting — target: %d words",
            target_word_count,
        )

        collection_link_instruction = ""
        related_collections = []
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT gc.taxonomy_category
                    FROM generated_collections gc
                    WHERE gc.collection_id = %s AND gc.generation_mode = 'taxonomy' AND gc.taxonomy_category IS NOT NULL
                    LIMIT 1""",
                    (collection_id,),
                )
                tax_row = cur.fetchone()

                if tax_row:
                    cur.execute(
                        """SELECT DISTINCT sc.title, sc.handle
                        FROM generated_collections gc
                        JOIN synced_collections sc ON sc.id = gc.collection_id
                        WHERE gc.taxonomy_category = %s
                          AND gc.generation_mode = 'taxonomy'
                          AND gc.collection_id != %s
                          AND gc.store_id = (SELECT store_id FROM synced_collections WHERE id = %s)
                          AND sc.handle IS NOT NULL AND sc.handle != ''
                        ORDER BY sc.title""",
                        (tax_row["taxonomy_category"], collection_id, collection_id),
                    )
                    related_collections = cur.fetchall()

                if not related_collections:
                    cur.execute(
                        """SELECT sc.title, sc.handle
                        FROM synced_collections sc
                        WHERE sc.id != %s
                          AND sc.store_id = (SELECT store_id FROM synced_collections WHERE id = %s)
                          AND sc.handle IS NOT NULL AND sc.handle != ''
                        ORDER BY sc.title""",
                        (collection_id, collection_id),
                    )
                    related_collections = cur.fetchall()
        except Exception:
            logger.exception("Failed to fetch related collections for cross-linking")

        if related_collections:
            collection_link_instruction = (
                "\n- SHOULD include 2-5 internal links to RELATED COLLECTIONS from the list provided\n"
                "- Use relative URLs (/collections/{handle}) for collection links\n"
                "- Weave collection links naturally (e.g. 'Explore our [Collection Name](/collections/handle) for more...')\n"
                "- Only link to collections that are genuinely related to this collection's theme\n"
            )

        hierarchy_link_instruction = ""
        if hierarchy_context:
            hierarchy_link_instruction = (
                "\n- MUST include internal links based on this collection's hierarchy position:\n"
                "  " + hierarchy_context + "\n"
                "- Use relative URLs (/collections/{handle}) for all hierarchy links\n"
            )

        _coll_desc_template = get_setting("prompts.collection_description", DEFAULTS["prompts.collection_description"])
        try:
            desc_system_prompt = _coll_desc_template.format(target_word_count=target_word_count)
        except (KeyError, IndexError):
            desc_system_prompt = DEFAULTS["prompts.collection_description"].format(target_word_count=target_word_count)
        desc_system_prompt += collection_link_instruction + hierarchy_link_instruction

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()

        user_parts = []

        if collection:
            current_desc = collection.get("description") or ""
            desc_word_count = len(current_desc.split()) if current_desc else 0
            user_parts.append(
                "COLLECTION DATA:\n"
                f"- Title: {collection.get('title', 'N/A')}\n"
                f"- Products Count: {collection.get('products_count', 0)}\n"
                f"- Current Description Word Count: {desc_word_count}\n"
            )

        if related_collections:
            coll_links_text = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in related_collections[:30])
            user_parts.append(
                "RELATED COLLECTIONS (link to 2-5 relevant ones):\n" + coll_links_text
            )

        seo_parts = []
        if main_result.get("h1_tag"):
            seo_parts.append(f"- H1 Tag: {main_result['h1_tag']}")
        if main_result.get("optimized_meta_title"):
            seo_parts.append(f"- Meta Title: {main_result['optimized_meta_title']}")
        if main_result.get("optimized_meta_description"):
            seo_parts.append(f"- Meta Description: {main_result['optimized_meta_description']}")
        if main_result.get("keywords_used"):
            seo_parts.append(f"- Keywords to Incorporate: {', '.join(main_result['keywords_used'])}")
        if main_result.get("competitor_insights"):
            seo_parts.append(f"- Competitor Insights: {main_result['competitor_insights']}")
        if seo_parts:
            user_parts.append("SEO ANALYSIS (from main analysis):\n" + "\n".join(seo_parts))

        if serp_context:
            user_parts.append(f"SERP BENCHMARK:\n{serp_context}")

        if serp_benchmark and serp_benchmark.get("page_summaries"):
            h2_lines = []
            for s in serp_benchmark["page_summaries"][:5]:
                h2s = s.get("h2s", [])
                if h2s:
                    h2_lines.append(f"- {s['domain']}: {', '.join(h2s[:5])}")
            if h2_lines:
                user_parts.append(
                    "COMPETITOR H2 PATTERNS (use as structural inspiration):\n"
                    + "\n".join(h2_lines)
                )

        try:
            search_ctx = search_context
            if search_ctx is None and collection and collection.get("store_id"):
                search_ctx = _get_search_context_for_item("collection", collection_id, collection["store_id"])
            if search_ctx:
                    user_parts.append(
                        "REAL SEARCH DATA (incorporate these keywords naturally):\n" + search_ctx
                    )
        except Exception:
            logger.warning("Search context for collection description generation failed for %s", collection_id)

        user_parts.append(
            f"Write an SEO-optimized collection description targeting ~{target_word_count} words (aim to be more comprehensive than competitors). "
            "Return raw HTML only."
        )

        desc_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": desc_system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.4,
        )

        desc_content = desc_response.choices[0].message.content or ""

        desc_content = re.sub(r"^```(?:html)?\s*\n?", "", desc_content)
        desc_content = re.sub(r"\n?```\s*$", "", desc_content)
        desc_content = _sanitize_description(desc_content)

        if desc_content:
            main_result["optimized_description"] = desc_content
            desc_words = len(desc_content.split())
            logger.info("Collection description generated — %d words (target: %d)", desc_words, target_word_count)
        else:
            logger.warning("Collection description generation returned empty content")

    except Exception:
        logger.exception("Collection description generation failed")

    return main_result


def _generate_brand_collection_description(client, config, main_result, serp_context, serp_benchmark, collection_id, interlink_collections=None, user_keywords=None, search_context=None):
    try:
        target_word_count = DEFAULT_TARGET_WORD_COUNT
        if serp_benchmark and serp_benchmark.get("avg_word_count"):
            competitor_avg = serp_benchmark["avg_word_count"]
            target_word_count = max(int(competitor_avg * 1.2), DEFAULT_TARGET_WORD_COUNT)

        logger.info(
            "Brand collection description generation starting — target: %d words",
            target_word_count,
        )

        interlink_collections = interlink_collections or []
        user_keywords = user_keywords or []

        interlink_instruction = ""
        if interlink_collections:
            links_text = "\n".join(
                f"- {c.get('title', 'N/A')}: /collections/{c.get('handle', '')}"
                for c in interlink_collections if c.get("handle")
            )
            interlink_instruction = (
                "\n- MUST include internal links to these related collections:\n"
                + links_text + "\n"
                "- Use relative URLs (/collections/{handle}) for collection links\n"
                "- Weave collection links naturally into the description\n"
            )
        else:
            try:
                with db.get_cursor() as cur:
                    cur.execute(
                        """SELECT sc.title, sc.handle
                        FROM synced_collections sc
                        WHERE sc.id != %s
                          AND sc.store_id = (SELECT store_id FROM synced_collections WHERE id = %s)
                          AND sc.handle IS NOT NULL AND sc.handle != ''
                        ORDER BY sc.title LIMIT 20""",
                        (collection_id, collection_id),
                    )
                    fallback_collections = cur.fetchall()
                if fallback_collections:
                    links_text = "\n".join(
                        f"- {c['title']}: /collections/{c['handle']}"
                        for c in fallback_collections
                    )
                    interlink_instruction = (
                        "\n- SHOULD include 2-5 internal links to RELATED COLLECTIONS from the list provided\n"
                        "- Use relative URLs (/collections/{handle}) for collection links\n"
                        "- Only link to collections that are genuinely related to this brand's theme\n"
                        "RELATED COLLECTIONS:\n" + links_text + "\n"
                    )
            except Exception:
                logger.exception("Failed to fetch fallback collections for brand interlinking")

        _brand_desc_template = get_setting("prompts.brand_description", DEFAULTS["prompts.brand_description"])
        try:
            desc_system_prompt = _brand_desc_template.format(target_word_count=target_word_count)
        except (KeyError, IndexError):
            desc_system_prompt = DEFAULTS["prompts.brand_description"].format(target_word_count=target_word_count)
        desc_system_prompt += interlink_instruction

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()

            cur.execute(
                "SELECT tag FROM generated_collections WHERE collection_id = %s AND generation_mode = 'brands' LIMIT 1",
                (collection_id,),
            )
            tag_row = cur.fetchone()

        if tag_row and tag_row["tag"] and tag_row["tag"].startswith("__brand__"):
            vendor_name = tag_row["tag"].replace("__brand__", "")
        else:
            vendor_name = collection.get("title", "") if collection else ""
        brand_context_parts = []

        if collection:
            cur_desc = collection.get("description") or ""
            desc_word_count = len(cur_desc.split()) if cur_desc else 0
            brand_context_parts.append(
                "BRAND COLLECTION DATA:\n"
                f"- Brand Name: {vendor_name}\n"
                f"- Products Count: {collection.get('products_count', 0)}\n"
                f"- Current Description Word Count: {desc_word_count}\n"
            )

        if vendor_name:
            try:
                with db.get_cursor() as cur:
                    cur.execute(
                        """SELECT product_type, COUNT(*) as cnt FROM synced_products
                        WHERE store_id = (SELECT store_id FROM synced_collections WHERE id = %s)
                          AND vendor = %s AND product_type IS NOT NULL AND product_type != ''
                        GROUP BY product_type ORDER BY cnt DESC LIMIT 10""",
                        (collection_id, vendor_name),
                    )
                    pt_rows = cur.fetchall()
                if pt_rows:
                    pt_list = ", ".join(r["product_type"] for r in pt_rows)
                    brand_context_parts.append(f"PRODUCT TYPES FOR THIS BRAND: {pt_list}")
            except Exception:
                pass

        if user_keywords:
            brand_context_parts.append(f"TARGET KEYWORDS: {', '.join(user_keywords)}")

        seo_parts = []
        if main_result.get("h1_tag"):
            seo_parts.append(f"- H1 Tag: {main_result['h1_tag']}")
        if main_result.get("optimized_meta_title"):
            seo_parts.append(f"- Meta Title: {main_result['optimized_meta_title']}")
        if main_result.get("optimized_meta_description"):
            seo_parts.append(f"- Meta Description: {main_result['optimized_meta_description']}")
        if main_result.get("keywords_used"):
            seo_parts.append(f"- Keywords to Incorporate: {', '.join(main_result['keywords_used'])}")
        if main_result.get("competitor_insights"):
            seo_parts.append(f"- Competitor Insights: {main_result['competitor_insights']}")
        if seo_parts:
            brand_context_parts.append("SEO ANALYSIS (from main analysis):\n" + "\n".join(seo_parts))

        if serp_context:
            brand_context_parts.append(f"SERP BENCHMARK:\n{serp_context}")

        if serp_benchmark and serp_benchmark.get("page_summaries"):
            h2_lines = []
            for s in serp_benchmark["page_summaries"][:5]:
                h2s = s.get("h2s", [])
                if h2s:
                    h2_lines.append(f"- {s['domain']}: {', '.join(h2s[:5])}")
            if h2_lines:
                brand_context_parts.append(
                    "COMPETITOR H2 PATTERNS (use as structural inspiration):\n"
                    + "\n".join(h2_lines)
                )

        try:
            search_ctx = search_context
            if search_ctx is None and collection and collection.get("store_id"):
                search_ctx = _get_search_context_for_item("collection", collection_id, collection["store_id"])
            if search_ctx:
                    brand_context_parts.append(
                        "REAL SEARCH DATA (incorporate these keywords naturally):\n" + search_ctx
                    )
        except Exception:
            logger.warning("Search context for brand description generation failed for %s", collection_id)

        brand_context_parts.append(
            f"Write an SEO-optimized brand collection description for '{vendor_name}' targeting ~{target_word_count} words. "
            "Return raw HTML only."
        )

        desc_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": desc_system_prompt},
                {"role": "user", "content": "\n\n".join(brand_context_parts)},
            ],
            temperature=0.4,
        )

        desc_content = desc_response.choices[0].message.content or ""
        desc_content = re.sub(r"^```(?:html)?\s*\n?", "", desc_content)
        desc_content = re.sub(r"\n?```\s*$", "", desc_content)
        desc_content = _sanitize_description(desc_content)

        if desc_content:
            image_url = collection.get("image_url", "") if collection else ""
            if image_url:
                alt_parts = [vendor_name] if vendor_name else []
                if user_keywords:
                    alt_parts.extend(user_keywords[:2])
                elif main_result.get("keywords_used"):
                    alt_parts.extend(main_result["keywords_used"][:2])
                alt_text = " — ".join(alt_parts) if alt_parts else vendor_name or "Brand collection"
                alt_text = alt_text.replace('"', "&quot;")

                image_html = (
                    f'<figure style="margin:0 0 1.5em 0;text-align:center;">'
                    f'<img src="{image_url}" alt="{alt_text}" '
                    f'loading="lazy" decoding="async" '
                    f'width="800" height="600" '
                    f'style="max-width:100%;height:auto;border-radius:8px;" '
                    f'fetchpriority="low">'
                    f'<figcaption style="margin-top:0.5em;font-size:0.9em;color:#666;">'
                    f'{vendor_name} Collection</figcaption>'
                    f'</figure>\n'
                )
                desc_content = image_html + desc_content
                logger.info("Injected collection image into brand description with SEO alt: %s", alt_text)

            main_result["optimized_description"] = desc_content
            desc_words = len(desc_content.split())
            logger.info("Brand description generated — %d words (target: %d)", desc_words, target_word_count)
        else:
            logger.warning("Brand description generation returned empty content")

    except Exception:
        logger.exception("Brand collection description generation failed")

    return main_result


ALLOWED_SECTION_TYPES = {"hero", "features", "product_showcase", "testimonials", "faq", "cta_banner", "trust_badges", "rich_text", "image_gallery", "statistics"}

REQUIRED_SECTION_DATA = {
    "hero": ["headline"],
    "features": ["items"],
    "faq": ["items"],
    "cta_banner": ["heading", "cta_text"],
    "testimonials": ["items"],
    "trust_badges": ["items"],
    "rich_text": ["content"],
    "statistics": ["items"],
}

def _validate_landing_page_generate(result):
    sections = result.get("sections")
    if not sections or not isinstance(sections, list):
        result["sections"] = []
        return result

    validated = []
    for section in sections:
        s_type = section.get("type", "")
        if s_type not in ALLOWED_SECTION_TYPES:
            continue
        data = section.get("data", {})
        required_keys = REQUIRED_SECTION_DATA.get(s_type, [])
        if all(k in data for k in required_keys):
            if s_type == "rich_text" and data.get("content"):
                import re
                data["content"] = re.sub(r'<script[^>]*>.*?</script>', '', data["content"], flags=re.DOTALL | re.IGNORECASE)
            validated.append(section)

    has_hero = any(s["type"] == "hero" for s in validated)
    has_cta = any(s["type"] == "cta_banner" for s in validated)

    if not has_hero:
        validated.insert(0, {
            "type": "hero",
            "order": 0,
            "data": {
                "headline": result.get("title", "Welcome"),
                "subheadline": result.get("meta_description", ""),
                "cta_text": "Shop Now",
                "cta_url": "/collections/all",
                "image_url": "",
                "image_alt": result.get("title", ""),
            },
        })
    if not has_cta:
        validated.append({
            "type": "cta_banner",
            "order": len(validated),
            "data": {
                "heading": "Ready to Get Started?",
                "subtext": "",
                "cta_text": "Shop Now",
                "cta_url": "/collections/all",
            },
        })

    for i, s in enumerate(validated):
        s["order"] = i

    result["sections"] = validated

    from app.services.ai_output_guard import clamp_meta_description, clamp_meta_title
    if result.get("meta_title"):
        result["meta_title"] = clamp_meta_title(result["meta_title"])
    if result.get("meta_description"):
        result["meta_description"] = clamp_meta_description(result["meta_description"])

    return result


def _validate_home_page_generate(result):
    """Clamp and sanitize the AI home-page carousel output."""
    slides = result.get("slides")
    if not slides or not isinstance(slides, list):
        result["slides"] = []
        return result

    cleaned = []
    for slide in slides[:5]:
        if not isinstance(slide, dict):
            continue
        cleaned.append({
            "headline": (slide.get("headline") or "").strip(),
            "subheadline": (slide.get("subheadline") or "").strip(),
            "image_prompt": (slide.get("image_prompt") or "").strip(),
            "image_alt": (slide.get("image_alt") or slide.get("headline") or "").strip(),
            "cta_text": (slide.get("cta_text") or "").strip(),
            "cta_url": (slide.get("cta_url") or "/collections/all").strip(),
            "image_url": "",
            "source": "ai",
        })
    result["slides"] = [s for s in cleaned if s["headline"]]

    from app.services.ai_output_guard import clamp_meta_description, clamp_meta_title
    if result.get("meta_title"):
        result["meta_title"] = clamp_meta_title(result["meta_title"])
    if result.get("meta_description"):
        result["meta_description"] = clamp_meta_description(result["meta_description"])

    return result


def _validate_main_result(main_result, task_type, task_input, product_id, collection_id, client, config, store_id=None):
    """Quality-gate a product_seo/collection_seo main result: resolve the
    primary keyword (task input > persisted resolution), then validate
    lengths / title keyword position / alt balance with one rephrase retry.
    With no vetted keyword, keyword-first enforcement is skipped (length and
    structure checks still run) — enforcing the model's own pick would lock
    in a keyword nobody chose. Fails open."""
    try:
        from app.services.ai_content_validator import clean_keyword, validate_seo_result

        store_id = store_id or task_input.get("store_id")
        primary_kw = clean_keyword(task_input.get("target_keyword"))
        if not primary_kw:
            item_type = "product" if task_type == "product_seo" else "collection"
            item_id = product_id if task_type == "product_seo" else collection_id
            try:
                with db.get_cursor() as cur:
                    if store_id:
                        cur.execute(
                            """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                               FROM technical_seo_analyses
                               WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                            (store_id, item_type, item_id),
                        )
                    else:
                        cur.execute(
                            """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                               FROM technical_seo_analyses
                               WHERE item_type = %s AND item_id = %s""",
                            (item_type, item_id),
                        )
                    row = cur.fetchone()
                primary_kw = clean_keyword(row["kw"]) if row else None
            except Exception:
                primary_kw = None
        validate_seo_result(main_result, primary_kw, client, config)
    except Exception:
        logger.exception("Main-result validation failed — passing through")


def run_task(task_type, task_input, store_id=None):
    config = _get_ai_config()

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config["api_key"],
        timeout=120.0,
    )

    if task_type == "product_seo":
        product_id = task_input.get("product_id")
        skip_tags = task_input.get("skip_tags", True)

        serp_context = ""
        serp_benchmark = None
        try:
            from app.services.serp_scraper import get_serp_benchmark
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sp.*, COALESCE(s.primary_domain, s.domain) as store_domain
                    FROM synced_products sp
                    JOIN stores s ON s.id = sp.store_id
                    WHERE sp.id = %s""",
                    (product_id,),
                )
                product_row = cur.fetchone()
            if product_row:
                # Benchmark SERPs for the resolved primary (keyword strategy),
                # not a title-derived query — competitor guidance must describe
                # the keyword the optimization will actually target.
                serp_kws = None
                try:
                    from app.services.keyword_resolver import get_or_refresh_resolution
                    _res = get_or_refresh_resolution(product_row["store_id"], "product", product_id)
                    _rp = (_res or {}).get("primary")
                    if _rp and _rp.get("keyword"):
                        serp_kws = [_rp["keyword"]]
                except Exception:
                    pass
                serp_context, serp_benchmark = get_serp_benchmark(
                    dict(product_row), target_keywords=serp_kws, return_raw=True
                )
        except Exception as e:
            logger.exception("SERP benchmark fetch failed: %s", e)

        serp_instruction = ""
        if serp_context:
            serp_instruction = (
                "\n\nYou also have live SERP benchmark data below. "
                "Use it to EXCEED what's actually ranking on Google — aim to beat competitor meta title and description lengths within optimal ranges.\n\n"
                + serp_context
            )

        tags_instruction = ""
        if skip_tags:
            tags_instruction = (
                "\n\nDo NOT suggest tags — they are managed separately via keyword analysis.\n"
            )
            # keywords_used stays in the schema — the description generator
            # uses it as its keyword steering signal.
            tags_keys = (
                "- keywords_used (array of strings): Keywords you incorporated into the optimizations\n"
            )
        else:
            tags_keys = (
                "- suggested_tags (array of strings): Relevant product tags for Shopify\n"
                "- keywords_used (array of strings): Keywords you incorporated into the optimizations\n"
            )

        _product_seo_template = get_setting("prompts.product_seo", DEFAULTS["prompts.product_seo"])
        try:
            system_prompt = _product_seo_template.format(tags_keys=tags_keys)
        except (KeyError, IndexError):
            system_prompt = DEFAULTS["prompts.product_seo"].format(tags_keys=tags_keys)
        search_data_instruction = ""
        prefetched_search_ctx = None
        try:
            effective_store = product_row["store_id"] if product_row else store_id
            if effective_store:
                search_ctx = _get_search_context_for_item("product", product_id, effective_store)
                prefetched_search_ctx = search_ctx or ""
                if search_ctx:
                    search_data_instruction = (
                        "\n\n## CRITICAL: Real Search Data Available\n"
                        "The following data comes from Google Search Console, Semrush, and the "
                        "store's keyword strategy. If a RESOLVED or USER-ASSIGNED PRIMARY KEYWORD "
                        "is present, it was selected by the store's demand-grounded keyword "
                        "strategy — use it as the primary. Use the rest of the data for secondary "
                        "keywords, CTR framing, and content ideas. Do NOT ignore this data in "
                        "favor of generic suggestions.\n\n"
                        + search_ctx
                    )
        except Exception as e:
            logger.warning("Search context injection failed for product %s: %s", product_id, e)

        priority_context = task_input.get("priority_context", "")
        target_keyword = task_input.get("target_keyword")
        priority_instruction = ""
        if target_keyword:
            tk_bits = []
            if task_input.get("target_keyword_volume"):
                tk_bits.append(f"search volume {int(task_input['target_keyword_volume']):,}")
            if task_input.get("target_keyword_position"):
                tk_bits.append(f"currently position {float(task_input['target_keyword_position']):.0f}")
            priority_instruction = (
                f"\n\n## TARGET KEYWORD (user-selected fix)\n"
                f"This optimization was launched to capture \"{target_keyword}\""
                + (f" ({', '.join(tk_bits)})" if tk_bits else "")
                + ". Treat it as the primary keyword for the meta title, meta description, and "
                  "the opening of the description — unless a RESOLVED or USER-ASSIGNED PRIMARY "
                  "KEYWORD in the search data below conflicts, in which case that keyword wins "
                  "and this one becomes a secondary.\n"
            )
        elif priority_context:
            priority_instruction = (
                f"\n\nPRIORITY FOCUS: This product was flagged with the following priority action: "
                f"\"{priority_context}\". Address this specific issue first in your analysis.\n"
            )

        claimed_keywords = task_input.get("claimed_keywords")
        claimed_instruction = ""
        if claimed_keywords:
            kw_list = ", ".join(f'"{kw}"' for kw in claimed_keywords)
            claimed_instruction = (
                f"\n\n## BULK OPTIMIZATION: Keywords Owned by Other Pages\n"
                f"The following primary keywords are owned by other pages of this store "
                f"(resolved primaries, manual assignments, or earlier items in this batch): {kw_list}\n"
                f"You MUST NOT use any of these as the primary keyword for this product. "
                f"Choose a different, more specific keyword (e.g. a long-tail variation) to avoid "
                f"cannibalization within the store. If the best keyword is already claimed, "
                f"differentiate with modifiers like brand, size, flavor, or use case.\n"
                f"EXCEPTION: if a keyword in this list matches the RESOLVED or USER-ASSIGNED "
                f"PRIMARY KEYWORD above, the resolved primary wins — use it as the primary anyway.\n"
            )

        system_prompt += serp_instruction + tags_instruction + search_data_instruction + priority_instruction + claimed_instruction
        user_prompt = (
            f"Analyze product ID {product_id} for SEO optimization. "
            "First get the product data, then analyze its SEO score, "
            "then provide your optimized recommendations."
        )
    elif task_type == "collection_seo":
        collection_id = task_input.get("collection_id")

        serp_context = ""
        serp_benchmark = None
        try:
            from app.services.serp_scraper import get_serp_benchmark
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sc.*, COALESCE(s.primary_domain, s.domain) as store_domain
                    FROM synced_collections sc
                    JOIN stores s ON s.id = sc.store_id
                    WHERE sc.id = %s""",
                    (collection_id,),
                )
                collection_row = cur.fetchone()
            if collection_row:
                serp_kws = None
                try:
                    from app.services.keyword_resolver import get_or_refresh_resolution
                    _res = get_or_refresh_resolution(collection_row["store_id"], "collection", collection_id)
                    _rp = (_res or {}).get("primary")
                    if _rp and _rp.get("keyword"):
                        serp_kws = [_rp["keyword"]]
                except Exception:
                    pass
                serp_context, serp_benchmark = get_serp_benchmark(
                    dict(collection_row), target_keywords=serp_kws, return_raw=True, entity_type="collection"
                )
        except Exception as e:
            logger.exception("SERP benchmark fetch failed for collection: %s", e)

        serp_instruction = ""
        if serp_context:
            serp_instruction = (
                "\n\nYou also have live SERP benchmark data below. "
                "Use it to EXCEED what's actually ranking on Google — aim to beat competitor meta title and description lengths within optimal ranges.\n\n"
                + serp_context
            )

        hierarchy_instruction = ""
        hierarchy_context = task_input.get("hierarchy_context")
        if hierarchy_context:
            hierarchy_instruction = (
                "\n\nThis collection is part of a hierarchy:\n"
                + hierarchy_context
                + "\n\nInclude internal links to parent/sibling/child collections where relevant in your recommendations."
            )

        search_data_instruction = ""
        prefetched_search_ctx = None
        try:
            effective_store = collection_row["store_id"] if collection_row else store_id
            if effective_store:
                search_ctx = _get_search_context_for_item("collection", collection_id, effective_store)
                prefetched_search_ctx = search_ctx or ""
                if search_ctx:
                    search_data_instruction = (
                        "\n\n## CRITICAL: Real Search Data Available\n"
                        "The following data comes from Google Search Console, Semrush, and the "
                        "store's keyword strategy. If a RESOLVED or USER-ASSIGNED PRIMARY KEYWORD "
                        "is present, it was selected by the store's demand-grounded keyword "
                        "strategy — use it as the primary. Use the rest of the data for secondary "
                        "keywords, CTR framing, and content ideas. Do NOT ignore this data in "
                        "favor of generic suggestions.\n\n"
                        + search_ctx
                    )
        except Exception as e:
            logger.warning("Search context injection failed for collection %s: %s", collection_id, e)

        priority_context = task_input.get("priority_context", "")
        target_keyword = task_input.get("target_keyword")
        priority_instruction = ""
        if target_keyword:
            tk_bits = []
            if task_input.get("target_keyword_volume"):
                tk_bits.append(f"search volume {int(task_input['target_keyword_volume']):,}")
            if task_input.get("target_keyword_position"):
                tk_bits.append(f"currently position {float(task_input['target_keyword_position']):.0f}")
            priority_instruction = (
                f"\n\n## TARGET KEYWORD (user-selected fix)\n"
                f"This optimization was launched to capture \"{target_keyword}\""
                + (f" ({', '.join(tk_bits)})" if tk_bits else "")
                + ". Treat it as the primary keyword for the meta title, meta description, and "
                  "the opening of the description — unless a RESOLVED or USER-ASSIGNED PRIMARY "
                  "KEYWORD in the search data below conflicts, in which case that keyword wins "
                  "and this one becomes a secondary.\n"
            )
        elif priority_context:
            priority_instruction = (
                f"\n\nPRIORITY FOCUS: This collection was flagged with the following priority action: "
                f"\"{priority_context}\". Address this specific issue first in your analysis.\n"
            )

        claimed_keywords = task_input.get("claimed_keywords")
        claimed_instruction = ""
        if claimed_keywords:
            kw_list = ", ".join(f'"{kw}"' for kw in claimed_keywords)
            claimed_instruction = (
                f"\n\n## BULK OPTIMIZATION: Keywords Owned by Other Pages\n"
                f"The following primary keywords are owned by other pages of this store "
                f"(resolved primaries, manual assignments, or earlier items in this batch): {kw_list}\n"
                f"You MUST NOT use any of these as the primary keyword for this collection. "
                f"Choose a different, more specific keyword (e.g. a long-tail variation) to avoid "
                f"cannibalization within the store.\n"
                f"EXCEPTION: if a keyword in this list matches the RESOLVED or USER-ASSIGNED "
                f"PRIMARY KEYWORD above, the resolved primary wins — use it as the primary anyway.\n"
            )

        system_prompt = get_setting("prompts.collection_seo", DEFAULTS["prompts.collection_seo"])
        system_prompt += serp_instruction + hierarchy_instruction + search_data_instruction + priority_instruction + claimed_instruction
        user_prompt = (
            f"Analyze collection ID {collection_id} for SEO optimization. "
            "First get the collection data, then analyze its SEO score, "
            "then provide your optimized recommendations."
        )

    elif task_type == "brand_seo":
        collection_id = task_input.get("collection_id")
        user_keywords = task_input.get("keywords", [])
        interlink_collections = task_input.get("interlink_collections", [])

        serp_context = ""
        serp_benchmark = None
        try:
            from app.services.serp_scraper import get_serp_benchmark
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sc.*, COALESCE(s.primary_domain, s.domain) as store_domain
                    FROM synced_collections sc
                    JOIN stores s ON s.id = sc.store_id
                    WHERE sc.id = %s""",
                    (collection_id,),
                )
                collection_row = cur.fetchone()
            if collection_row:
                serp_kws = None
                try:
                    from app.services.keyword_resolver import get_or_refresh_resolution
                    _res = get_or_refresh_resolution(collection_row["store_id"], "collection", collection_id)
                    _rp = (_res or {}).get("primary")
                    if _rp and _rp.get("keyword"):
                        serp_kws = [_rp["keyword"]]
                except Exception:
                    pass
                serp_context, serp_benchmark = get_serp_benchmark(
                    dict(collection_row), target_keywords=serp_kws, return_raw=True, entity_type="collection"
                )
        except Exception as e:
            logger.exception("SERP benchmark fetch failed for brand collection: %s", e)

        serp_instruction = ""
        if serp_context:
            serp_instruction = (
                "\n\nYou also have live SERP benchmark data below. "
                "Use it to EXCEED what's actually ranking on Google — aim to beat competitor meta title and description lengths within optimal ranges.\n\n"
                + serp_context
            )

        brand_context = ""
        vendor_name = collection_row.get("title", "") if collection_row else ""
        if vendor_name:
            brand_context = f"\n\nThis is a brand collection page for '{vendor_name}'."
        if user_keywords:
            brand_context += f"\nTarget keywords provided by the user: {', '.join(user_keywords)}"

        search_data_instruction = ""
        prefetched_search_ctx = None
        try:
            effective_store = collection_row["store_id"] if collection_row else store_id
            if effective_store:
                search_ctx = _get_search_context_for_item("collection", collection_id, effective_store)
                prefetched_search_ctx = search_ctx or ""
                if search_ctx:
                    search_data_instruction = (
                        "\n\n## CRITICAL: Real Search Data Available\n"
                        "The following data comes from Google Search Console and Semrush. "
                        "Use this data to inform your keyword choices. Prioritize keywords "
                        "that already have search volume and where this brand collection is in "
                        "striking distance (positions 4-20). Do NOT ignore this data in favor of generic suggestions.\n\n"
                        + search_ctx
                    )
        except Exception as e:
            logger.warning("Search context injection failed for brand collection %s: %s", collection_id, e)

        system_prompt = get_setting("prompts.brand_seo", DEFAULTS["prompts.brand_seo"])
        system_prompt += serp_instruction + brand_context + search_data_instruction
        user_prompt = (
            f"Analyze collection ID {collection_id} for SEO optimization. "
            "First get the collection data, then analyze its SEO score, "
            "then provide your optimized recommendations."
        )

    elif task_type == "keyword_analysis":
        ka_store_id = task_input.get("store_id") or store_id
        if not ka_store_id:
            return {"error": "store_id is required for keyword analysis"}

        # --- Phase 1: Build taxonomy by scanning all products ---
        logger.info("Keyword analysis Phase 1: building taxonomy for store %s", ka_store_id)

        phase1_system = get_setting("prompts.keyword_phase1", DEFAULTS["prompts.keyword_phase1"])

        # Embed the catalog directly instead of letting the model paginate a
        # tool 200 products at a time — the accumulated history was resent on
        # every iteration (O(pages^2) prompt tokens on large stores).
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT title, product_type, vendor FROM synced_products "
                "WHERE store_id = %s AND status = 'ACTIVE' ORDER BY id",
                (ka_store_id,),
            )
            _p1_products = cur.fetchall()

        _P1_MAX_LINES = 2000
        catalog_lines = [
            f"- {p['title']} | type: {p.get('product_type') or '-'} | vendor: {p.get('vendor') or '-'}"
            for p in _p1_products[:_P1_MAX_LINES]
        ]
        catalog_block = "\n".join(catalog_lines)
        if len(_p1_products) > _P1_MAX_LINES:
            from collections import Counter
            type_counts = Counter((p.get("product_type") or "-") for p in _p1_products)
            catalog_block += (
                f"\n... plus {len(_p1_products) - _P1_MAX_LINES} more products. "
                "Product-type distribution of the FULL catalog: "
                + ", ".join(f"{t} ({c})" for t, c in type_counts.most_common(50))
            )

        phase1_user = (
            f"Analyze all products in store ID {ka_store_id} and build a comprehensive tag taxonomy. "
            f"Here is the complete product catalog ({len(_p1_products)} products) — "
            "you do NOT need to fetch anything:\n\n"
            + catalog_block
        )

        p1_messages = [
            {"role": "system", "content": phase1_system},
            {"role": "user", "content": phase1_user},
        ]

        taxonomy_result = None
        for _ in range(20):
            response = client.chat.completions.create(
                model=config["model"],
                messages=p1_messages,
                tools=KEYWORD_ANALYSIS_TOOL_DEFINITIONS,
                temperature=0.3,
            )
            choice = response.choices[0]
            if choice.message.tool_calls:
                p1_messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        p1_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"error": "Invalid tool arguments (malformed JSON). Retry with valid JSON."}),
                        })
                        continue
                    result = _handle_keyword_tool_call(fn_name, fn_args)
                    p1_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })
            else:
                content = choice.message.content or ""
                try:
                    start = content.find("{")
                    end = content.rfind("}") + 1
                    if start >= 0 and end > start:
                        taxonomy_result = json.loads(content[start:end])
                except (json.JSONDecodeError, ValueError):
                    pass
                break

        if not taxonomy_result or "categories" not in taxonomy_result:
            return {"error": "Phase 1 failed — could not build taxonomy", "raw_response": str(taxonomy_result)}

        categories = taxonomy_result.get("categories", [])
        global_tags = taxonomy_result.get("global_tags", [])
        summary = taxonomy_result.get("summary", "")

        all_allowed_tags = set()
        for cat in categories:
            for tag in cat.get("tags", []):
                all_allowed_tags.add(tag)
        for tag in global_tags:
            all_allowed_tags.add(tag)
        # Case/whitespace-insensitive lookup mapping back to canonical casing —
        # exact-match filtering silently dropped case-variant tags.
        allowed_canonical = {str(t).strip().lower(): t for t in all_allowed_tags}

        logger.info(
            "Phase 1 complete — %d categories, %d total unique tags",
            len(categories), len(all_allowed_tags),
        )

        # --- Phase 2: Assign tags in batches ---
        logger.info("Keyword analysis Phase 2: assigning tags in batches")

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, title, product_type, vendor, tags FROM synced_products "
                "WHERE store_id = %s AND status = 'ACTIVE' ORDER BY id",
                (ka_store_id,),
            )
            all_products = cur.fetchall()

        taxonomy_text = json.dumps({"categories": categories, "global_tags": global_tags}, indent=1)

        all_assignments = []
        batch_size = 50
        for batch_start in range(0, len(all_products), batch_size):
            batch = all_products[batch_start:batch_start + batch_size]
            batch_data = [
                {"id": p["id"], "title": p["title"], "product_type": p["product_type"], "vendor": p["vendor"]}
                for p in batch
            ]

            _phase2_template = get_setting("prompts.keyword_phase2", DEFAULTS["prompts.keyword_phase2"])
            try:
                phase2_system = _phase2_template.format(taxonomy_text=taxonomy_text)
            except (KeyError, IndexError):
                phase2_system = DEFAULTS["prompts.keyword_phase2"].format(taxonomy_text=taxonomy_text)
            phase2_user = (
                "Assign tags to each product below. Return JSON: "
                '{\"assignments\": [{\"product_id\": <id>, \"assigned_tags\": [\"Tag1\", \"Tag2\"]}]}\n\n'
                f"PRODUCTS:\n{json.dumps(batch_data, indent=1)}"
            )

            try:
                batch_response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": phase2_system},
                        {"role": "user", "content": phase2_user},
                    ],
                    temperature=0.2,
                )
                batch_content = batch_response.choices[0].message.content or ""
                bstart = batch_content.find("{")
                bend = batch_content.rfind("}") + 1
                if bstart >= 0 and bend > bstart:
                    batch_parsed = json.loads(batch_content[bstart:bend])
                    batch_assignments = batch_parsed.get("assignments", [])
                    for a in batch_assignments:
                        a["assigned_tags"] = [
                            allowed_canonical[str(t).strip().lower()]
                            for t in a.get("assigned_tags", [])
                            if str(t).strip().lower() in allowed_canonical
                        ]
                    all_assignments.extend(batch_assignments)
                    logger.info(
                        "Phase 2 batch %d-%d: assigned tags to %d products",
                        batch_start, batch_start + len(batch), len(batch_assignments),
                    )
            except Exception:
                logger.exception("Phase 2 batch %d failed", batch_start)

        logger.info("Keyword analysis complete — %d total assignments", len(all_assignments))

        return {
            "categories": categories,
            "global_tags": global_tags,
            "product_assignments": all_assignments,
            "summary": summary,
        }

    elif task_type == "product_title_seo":
        product_id = task_input.get("product_id")

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT store_id, title, vendor, product_type, tags, handle, meta_title FROM synced_products WHERE id = %s",
                (product_id,),
            )
            product = cur.fetchone()

        if not product:
            return {"error": f"Product {product_id} not found"}

        context_payload = {
            "current_title": product["title"],
            "vendor": product.get("vendor") or "",
            "product_type": product.get("product_type") or "",
            "tags": product.get("tags") or "",
            "handle": product.get("handle") or "",
            "meta_title": product.get("meta_title") or "",
        }
        try:
            from app.services.keyword_resolver import get_or_refresh_resolution

            resolution = get_or_refresh_resolution(product["store_id"], "product", product_id)
            rp = (resolution or {}).get("primary")
            if rp and rp.get("keyword"):
                context_payload["primary_keyword"] = rp["keyword"]
        except Exception:
            pass
        product_context = json.dumps(context_payload, indent=2)

        system_prompt = get_setting("prompts.product_title", DEFAULTS["prompts.product_title"])
        user_prompt = f"Optimize the title for this product:\n\n{product_context}"
        if context_payload.get("primary_keyword"):
            user_prompt += (
                f"\n\nThe primary_keyword above was resolved by the store's keyword strategy — "
                f"the optimized title MUST contain it (or a close natural variant). Do not "
                f"substitute a different keyword."
            )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "dedupe_group":
        field = task_input.get("field")
        group_items = task_input.get("items") or []
        keep_id = task_input.get("keep_id")

        if field not in ("seo_title", "meta_description"):
            return {"error": f"Invalid dedupe field: {field}"}
        if len(group_items) < 2:
            return {"error": "Need at least 2 items to dedupe"}

        def _dedupe_primary(it):
            if it.get("type") not in ("product", "collection") or not it.get("id"):
                return ""
            try:
                with db.get_cursor() as cur:
                    if store_id:
                        cur.execute(
                            """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                               FROM technical_seo_analyses
                               WHERE store_id = %s AND item_type = %s AND item_id = %s""",
                            (store_id, it["type"], it["id"]),
                        )
                    else:
                        cur.execute(
                            """SELECT keyword_resolution->'primary'->>'keyword' AS kw
                               FROM technical_seo_analyses
                               WHERE item_type = %s AND item_id = %s""",
                            (it["type"], it["id"]),
                        )
                    row = cur.fetchone()
                return (row or {}).get("kw") or ""
            except Exception:
                return ""

        payload = {
            "field": field,
            "keep_id": keep_id,
            "items": [
                {
                    "id": it.get("id"),
                    "type": it.get("type"),
                    "title": it.get("title") or "",
                    "meta_title": it.get("meta_title") or "",
                    "meta_description": it.get("meta_description") or "",
                    "vendor": it.get("vendor") or "",
                    "product_type": it.get("product_type") or "",
                    "handle": it.get("handle") or "",
                    "blog_title": it.get("blog_title") or "",
                    "summary": (it.get("summary") or "")[:200],
                    "primary_keyword": _dedupe_primary(it),
                }
                for it in group_items
            ],
        }

        system_prompt = get_setting("prompts.dedupe_group", DEFAULTS["prompts.dedupe_group"])
        user_prompt = (
            "These items share the same value for the field '"
            + field
            + "'. Make each one unique:\n\n"
            + json.dumps(payload, indent=2)
            + "\n\nWhen an item has a primary_keyword, its rewritten value MUST keep that "
            "keyword — it was chosen by the store's keyword strategy; differentiate with "
            "other words, never by dropping the keyword."
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "news_analyze":
        source_text = (task_input.get("source_text") or "").strip()
        sid = task_input.get("store_id") or store_id
        if not source_text:
            return {"error": "No source_text provided"}

        allowed_categories = get_setting("lists.news_categories", DEFAULTS["lists.news_categories"]) or []

        news_candidates = []
        products = []
        collections = []
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT shopify_id, title, handle, category
                       FROM synced_articles
                       WHERE store_id = %s AND article_type = 'news' AND is_published = TRUE
                       ORDER BY published_at DESC NULLS LAST LIMIT 30""",
                    (sid,),
                )
                news_candidates = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT title, handle FROM synced_products WHERE store_id = %s LIMIT 40",
                    (sid,),
                )
                products = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT title, handle FROM synced_collections WHERE store_id = %s LIMIT 25",
                    (sid,),
                )
                collections = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning("news_analyze context fetch failed: %s", e)

        system_prompt = get_setting("prompts.news_analyze", DEFAULTS["prompts.news_analyze"])

        candidate_lines = "\n".join(
            f"- {c['title']} (shopify_id: {c['shopify_id']}, handle: {c['handle']}, category: {c.get('category') or 'n/a'})"
            for c in news_candidates
        ) or "(none yet)"
        product_lines = "\n".join(f"- {p['title']}: /products/{p['handle']}" for p in products) or "(none)"
        collection_lines = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in collections) or "(none)"

        user_prompt = (
            f"ALLOWED CATEGORIES (choose exactly one): {', '.join(allowed_categories)}\n\n"
            f"EXISTING NEWS ARTICLES (candidates for related_news / cross-linking):\n{candidate_lines}\n\n"
            f"PRODUCTS available for internal linking:\n{product_lines}\n\n"
            f"COLLECTIONS available for internal linking:\n{collection_lines}\n\n"
            f"RAW NEWS ARTICLE TEXT TO ENHANCE (preserve facts and wording):\n\n{source_text}"
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )

        content = response.choices[0].message.content or ""
        result = None
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            result = None
        if not isinstance(result, dict):
            return {"raw_response": content, "error": "Could not parse JSON from model"}

        # --- Server-side enforcement ---
        def _clamp(text, n):
            text = (text or "").strip()
            return text[:n]

        result["headline"] = _clamp(result.get("headline"), 110)
        result["meta_title"] = _clamp(result.get("meta_title"), 60)
        result["meta_description"] = _clamp(result.get("meta_description"), 160)
        result["summary"] = _clamp(result.get("summary"), 160)

        # Coerce category to an allowed value (case-insensitive).
        cat = (result.get("category") or "").strip()
        match = next((a for a in allowed_categories if a.lower() == cat.lower()), None)
        result["category"] = match or (allowed_categories[0] if allowed_categories else "News")

        # Tags: ensure type:news present, dedupe, keep order.
        tags = result.get("tags") or []
        if not isinstance(tags, list):
            tags = [t.strip() for t in str(tags).split(",") if t.strip()]
        norm = []
        for t in ["type:news"] + tags:
            t = str(t).strip()
            if t and t not in norm:
                norm.append(t)
        result["tags"] = norm

        # Sanitize body, then drop inline_images whose token isn't present.
        body_html = _sanitize_description(result.get("body_html") or "")
        result["body_html"] = body_html
        clean_inline = []
        for img in (result.get("inline_images") or []):
            if not isinstance(img, dict):
                continue
            token = (img.get("token") or "").strip()
            if token and token in body_html and img.get("prompt"):
                clean_inline.append({
                    "token": token,
                    "prompt": img.get("prompt"),
                    "alt": _clamp(img.get("alt"), 125),
                    "caption": (img.get("caption") or "").strip(),
                })
        result["inline_images"] = clean_inline
        result["hero_image_alt"] = _clamp(result.get("hero_image_alt"), 125)

        return result

    elif task_type == "news_links":
        body_html = task_input.get("body_html") or ""
        sid = task_input.get("store_id") or store_id
        headline = task_input.get("headline") or "News article"
        if not body_html.strip():
            return {"error": "No body_html provided"}

        products, collections, news_articles, blog_articles = [], [], [], []
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title, handle FROM synced_products WHERE store_id = %s AND handle IS NOT NULL LIMIT 50",
                    (sid,),
                )
                products = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL LIMIT 40",
                    (sid,),
                )
                collections = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    """SELECT sa.title, sa.handle, sb.handle AS blog_handle, sa.article_type
                       FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                       WHERE sa.store_id = %s AND sa.is_published = TRUE AND sa.handle IS NOT NULL
                       ORDER BY sa.published_at DESC NULLS LAST LIMIT 40""",
                    (sid,),
                )
                for r in cur.fetchall():
                    row = dict(r)
                    if row.get("article_type") == "news":
                        news_articles.append(row)
                    else:
                        blog_articles.append(row)
        except Exception as e:
            logger.warning("news_links candidate fetch failed: %s", e)

        system_prompt = get_setting("prompts.news_links", DEFAULTS["prompts.news_links"])

        user_parts = [f"ARTICLE HEADLINE: {headline}", f"EXISTING BODY HTML:\n{body_html}"]
        if products:
            user_parts.append("PRODUCTS FOR LINKING:\n" + "\n".join(
                f"- {p['title']}: /products/{p['handle']}" for p in products))
        if collections:
            user_parts.append("COLLECTIONS FOR LINKING:\n" + "\n".join(
                f"- {c['title']}: /collections/{c['handle']}" for c in collections))
        if news_articles:
            user_parts.append("OTHER NEWS ARTICLES FOR LINKING:\n" + "\n".join(
                f"- {a['title']}: /blogs/{a['blog_handle']}/{a['handle']}" for a in news_articles))
        if blog_articles:
            user_parts.append("BLOG ARTICLES FOR LINKING:\n" + "\n".join(
                f"- {a['title']}: /blogs/{a['blog_handle']}/{a['handle']}" for a in blog_articles))
        user_parts.append(
            "Add internal links to the items above, following all rules. "
            "Keep every [[INLINE_IMAGE_n]] token exactly as-is. Return raw HTML only."
        )

        link_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.3,
        )

        out = link_response.choices[0].message.content or ""
        out = re.sub(r"^```(?:html)?\s*\n?", "", out)
        out = re.sub(r"\n?```\s*$", "", out)
        out = _sanitize_description(out)

        # Safety net: never drop a planned inline-image token. Re-append any that
        # the model removed so the image is still placed at publish time.
        in_tokens = re.findall(r"\[\[INLINE_IMAGE_\d+\]\]", body_html)
        for tok in in_tokens:
            if tok not in out:
                out += f"\n{tok}"

        internal_links_count = len(re.findall(r'<a\s[^>]*href\s*=\s*["\']/', out, re.IGNORECASE))
        word_count = len(re.sub(r"<[^>]+>", "", out).split())
        return {
            "body_html": out,
            "stats": {"internal_links_count": internal_links_count, "word_count": word_count},
        }

    elif task_type == "collection_title_seo":
        collection_id = task_input.get("collection_id")

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT store_id, title, handle, meta_title, collection_type FROM synced_collections WHERE id = %s",
                (collection_id,),
            )
            collection = cur.fetchone()

        if not collection:
            return {"error": f"Collection {collection_id} not found"}

        context_payload = {
            "current_title": collection["title"],
            "handle": collection.get("handle") or "",
            "meta_title": collection.get("meta_title") or "",
            "collection_type": collection.get("collection_type") or "",
        }
        try:
            from app.services.keyword_resolver import get_or_refresh_resolution

            resolution = get_or_refresh_resolution(collection["store_id"], "collection", collection_id)
            rp = (resolution or {}).get("primary")
            if rp and rp.get("keyword"):
                context_payload["primary_keyword"] = rp["keyword"]
        except Exception:
            pass
        collection_context = json.dumps(context_payload, indent=2)

        system_prompt = get_setting("prompts.collection_title", DEFAULTS["prompts.collection_title"])
        user_prompt = f"Optimize the title for this collection:\n\n{collection_context}"
        if context_payload.get("primary_keyword"):
            user_prompt += (
                f"\n\nThe primary_keyword above was resolved by the store's keyword strategy — "
                f"the optimized title MUST contain it (or a close natural variant). Do not "
                f"substitute a different keyword."
            )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "interlinking_recommendations":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
                (store_id,),
            )
            row = cur.fetchone()

        if not row or not row.get("analysis_data"):
            return {"error": "No interlinking analysis data found. Run analysis first."}

        analysis = row["analysis_data"]
        if isinstance(analysis, str):
            analysis = json.loads(analysis)

        summary = analysis.get("summary", {})
        max_fixes = task_input.get("max_fixes", 10)
        data_limit = max(20, max_fixes * 2)
        orphan_products = analysis.get("orphan_products", [])[:data_limit]
        orphan_collections = analysis.get("orphan_collections", [])[:data_limit]
        orphan_articles = analysis.get("orphan_articles", [])[:data_limit]
        under_linked = analysis.get("under_linked", [])[:data_limit]
        missing_crosslinks = analysis.get("missing_crosslinks", [])[:data_limit]
        not_in_collection = analysis.get("not_in_collection", [])[:data_limit]

        vendor_type_index = analysis.get("vendor_type_index", {})

        all_collections_summary = [
            {
                "title": c.get("title"),
                "handle": c.get("handle"),
                "product_count": c.get("product_count", 0),
                "top_vendors": c.get("top_vendors", []),
                "top_product_types": c.get("top_product_types", []),
            }
            for c in analysis.get("collection_details", [])
        ]

        existing_links = analysis.get("all_links", [])[:200]
        non_canonical_links = analysis.get("non_canonical_links", [])[:data_limit]

        analysis_context = json.dumps({
            "summary": summary,
            "vendor_type_index": vendor_type_index,
            "all_collections_summary": all_collections_summary,
            "orphan_products": orphan_products,
            "orphan_collections": orphan_collections,
            "orphan_articles": orphan_articles,
            "under_linked": under_linked,
            "missing_crosslinks": missing_crosslinks,
            "not_in_collection": not_in_collection,
            "non_canonical_links": non_canonical_links,
            "existing_links": existing_links,
        }, indent=2)

        max_crosslinks = task_input.get("max_crosslinks", 5)

        base_system_prompt = get_setting("prompts.interlinking", DEFAULTS["prompts.interlinking"])

        chunk_size = 20
        num_chunks = max(1, -(-max_fixes // chunk_size))  # ceil division
        crosslinks_per_chunk = max(3, -(-max_crosslinks // num_chunks))

        all_fixes = []
        all_crosslinks = []
        summary_text = ""
        seen_fix_keys = set()
        seen_crosslink_keys = set()

        for chunk_idx in range(num_chunks):
            remaining_fixes = max_fixes - len(all_fixes)
            remaining_crosslinks = max_crosslinks - len(all_crosslinks)
            if remaining_fixes <= 0 and remaining_crosslinks <= 0:
                break

            this_fixes = min(chunk_size, remaining_fixes)
            this_crosslinks = min(crosslinks_per_chunk, remaining_crosslinks)

            system_prompt = base_system_prompt.replace(
                "{max_fixes}", str(this_fixes)
            ).replace(
                "{max_crosslinks}", str(this_crosslinks)
            )

            user_prompt = (
                "Here is the interlinking analysis data for the store:\n\n"
                + analysis_context
            )

            if seen_fix_keys:
                exclude_list = [f"{k[0]} -> {k[1]}" for k in list(seen_fix_keys)[:80]]
                user_prompt += (
                    "\n\nALREADY SUGGESTED (do NOT repeat these):\n"
                    + "\n".join(exclude_list)
                )

            user_prompt += "\n\nProvide your prioritized recommendations."

            max_tokens = 4096
            if this_fixes > 15:
                max_tokens = 8192

            try:
                response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=max_tokens,
                )

                content = response.choices[0].message.content or ""
                start = content.find("{")
                end = content.rfind("}") + 1
                if start < 0 or end <= start:
                    continue

                chunk_result = json.loads(content[start:end])
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Interlinking chunk %d returned unparsable output: %s", chunk_idx, e)
                continue
            except Exception as e:
                logger.exception("Interlinking chunk %d API call failed", chunk_idx)
                return {"error": f"AI request failed on recommendations chunk {chunk_idx + 1}: {e}"}

            if not summary_text and chunk_result.get("summary"):
                summary_text = chunk_result["summary"]

            for fix in chunk_result.get("priority_fixes", []):
                key = (fix.get("from_handle", ""), fix.get("to_handle", ""))
                if key not in seen_fix_keys:
                    seen_fix_keys.add(key)
                    all_fixes.append(fix)

            for cl in chunk_result.get("crosslink_suggestions", []):
                a = cl.get("collection_a_handle", "")
                b = cl.get("collection_b_handle", "")
                key = tuple(sorted([a, b]))
                if key not in seen_crosslink_keys:
                    seen_crosslink_keys.add(key)
                    all_crosslinks.append(cl)

            logger.info(
                "Interlinking chunk %d/%d: got %d fixes, %d crosslinks (total: %d/%d fixes, %d/%d crosslinks)",
                chunk_idx + 1, num_chunks,
                len(chunk_result.get("priority_fixes", [])),
                len(chunk_result.get("crosslink_suggestions", [])),
                len(all_fixes), max_fixes,
                len(all_crosslinks), max_crosslinks,
            )

        return {
            "priority_fixes": all_fixes[:max_fixes],
            "crosslink_suggestions": all_crosslinks[:max_crosslinks],
            "summary": summary_text or "Analysis complete.",
        }

    elif task_type == "interlinking_orphan_fix":
        from app.services.interlinking_analyzer import (
            build_article_fix_candidates,
            build_collection_fix_candidates,
            build_product_fix_candidates,
        )

        _BUILDERS = {
            "collection": build_collection_fix_candidates,
            "article": build_article_fix_candidates,
            "product": build_product_fix_candidates,
        }
        _DETAIL_KEYS = {
            "collection": "collection_details",
            "article": "article_details",
            "product": "product_details",
        }

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
                (store_id,),
            )
            row = cur.fetchone()
        if not row or not row.get("analysis_data"):
            return {"error": "No interlinking analysis data found. Run analysis first."}
        analysis = row["analysis_data"]
        if isinstance(analysis, str):
            analysis = json.loads(analysis)

        try:
            links_per_page = int(task_input.get("links_per_page") or 3)
        except (TypeError, ValueError):
            links_per_page = 3
        links_per_page = max(1, min(5, links_per_page))

        scope = task_input.get("scope") or task_input.get("item_type") or "collection"

        # Build (item_type, detail) target pairs. under_linked is mixed-type
        # and worst-first; orphan scopes are single-type.
        targets = []
        if scope == "under_linked":
            try:
                limit = int(task_input.get("limit") or 25)
            except (TypeError, ValueError):
                limit = 25
            limit = max(1, min(50, limit))
            for d in (analysis.get("under_linked") or [])[:limit]:
                t = d.get("item_type") or "product"
                if t in _BUILDERS:
                    targets.append((t, d))
        else:
            item_type = scope
            if item_type not in _BUILDERS:
                return {"error": f"Unsupported item_type: {item_type}"}
            details_by_handle = {
                c.get("handle"): c for c in analysis.get(_DETAIL_KEYS[item_type], [])
            }
            requested = task_input.get("handles") or []
            if requested:
                targets = [
                    (item_type, details_by_handle[h])
                    for h in requested if h in details_by_handle
                ]
            else:
                orphan_key = {
                    "collection": "orphan_collections",
                    "article": "orphan_articles",
                    "product": "orphan_products",
                }[item_type]
                targets = [
                    (item_type, details_by_handle.get(c.get("handle"), c))
                    for c in analysis.get(orphan_key, [])
                ]
        if not targets:
            return {"error": "No matching targets found in the analysis."}

        prompt_template = get_setting(
            "prompts.interlink_orphan_fix",
            DEFAULTS["prompts.interlink_orphan_fix"],
        )
        system_prompt = prompt_template.replace("{links_per_page}", str(links_per_page))

        plans = []
        planned_handles = set()
        seen_fix_keys = set()
        chunk_size = 5

        for start_i in range(0, len(targets), chunk_size):
            batch = targets[start_i:start_i + chunk_size]
            batch_ctx = []
            candidates_by_key = {}
            for t_type, coll in batch:
                key = (t_type, coll["handle"])
                cands = _BUILDERS[t_type](store_id, coll, analysis)
                candidates_by_key[key] = cands
                if not (cands["products"] or cands["collections"] or cands["articles"]):
                    plans.append({
                        "item_type": t_type,
                        "handle": coll["handle"],
                        "title": coll.get("title") or coll["handle"],
                        "blog_handle": coll.get("blog_handle"),
                        "fixes": [],
                        "note": f"No good source pages found for this {t_type}.",
                    })
                    planned_handles.add(key)
                    continue
                target_desc = {
                    "item_type": t_type,
                    "handle": coll["handle"],
                    "title": coll.get("title"),
                    "needs_outbound": (coll.get("outgoing_count") or 0) == 0,
                }
                if t_type in ("product", "collection"):
                    # Anchor equity flows to the resolved primary keyword —
                    # give the model the strategy's target, not just the title.
                    try:
                        _t_table = "synced_products" if t_type == "product" else "synced_collections"
                        with db.get_cursor() as cur:
                            cur.execute(
                                f"""SELECT tsa.keyword_resolution->'primary'->>'keyword' AS kw
                                   FROM {_t_table} t
                                   JOIN technical_seo_analyses tsa
                                     ON tsa.store_id = t.store_id AND tsa.item_type = %s AND tsa.item_id = t.id
                                   WHERE t.store_id = %s AND t.handle = %s""",
                                (t_type, store_id, coll["handle"]),
                            )
                            _kw_row = cur.fetchone()
                        if _kw_row and _kw_row.get("kw"):
                            target_desc["primary_keyword"] = _kw_row["kw"]
                    except Exception:
                        pass
                if t_type == "collection":
                    target_desc.update({
                        "product_count": coll.get("product_count", 0),
                        "top_vendors": coll.get("top_vendors", []),
                        "top_product_types": coll.get("top_product_types", []),
                    })
                elif t_type == "article":
                    target_desc["blog_handle"] = coll.get("blog_handle")
                else:
                    target_desc.update({
                        "vendor": coll.get("vendor") or "",
                        "product_type": coll.get("product_type") or "",
                    })
                batch_ctx.append({"target": target_desc, "candidates": cands})
            if not batch_ctx:
                continue

            user_prompt = (
                "Here are the target pages and their candidate source pages:\n\n"
                + json.dumps(batch_ctx, indent=2)
                + "\n\nWhen a target has a primary_keyword, prefer it (or a natural phrase "
                "containing it) as the anchor text for links pointing AT that target — "
                "fall back to the title only when the keyword reads unnaturally in context."
                + "\n\nReturn your link plans."
            )
            try:
                response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=4096,
                )
                content = response.choices[0].message.content or ""
                s, e = content.find("{"), content.rfind("}") + 1
                if s < 0 or e <= s:
                    raise ValueError("no JSON object in response")
                chunk_result = json.loads(content[s:e])
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("Orphan-fix chunk returned unusable output: %s", exc)
                for t_type, coll in batch:
                    key = (t_type, coll["handle"])
                    if key not in planned_handles:
                        plans.append({
                            "item_type": t_type,
                            "handle": coll["handle"],
                            "title": coll.get("title") or coll["handle"],
                            "blog_handle": coll.get("blog_handle"),
                            "fixes": [],
                            "note": "AI returned unusable output for this batch.",
                        })
                        planned_handles.add(key)
                continue
            except Exception as exc:
                logger.exception("Orphan-fix API call failed")
                return {"error": f"AI request failed: {exc}"}

            plans_by_handle = {}
            for p in chunk_result.get("plans", []):
                ph = p.get("handle") or p.get("collection_handle")
                plans_by_handle[(p.get("item_type"), ph)] = p
                plans_by_handle.setdefault((None, ph), p)
            for t_type, coll in batch:
                h = coll["handle"]
                key = (t_type, h)
                if key in planned_handles:
                    continue
                planned_handles.add(key)
                item_type = t_type
                cands = candidates_by_key[key]
                valid_sources = {
                    (c["type"], c["handle"]): c
                    for kind in ("products", "collections", "articles")
                    for c in cands[kind]
                }
                raw_plan = plans_by_handle.get((t_type, h)) or plans_by_handle.get((None, h)) or {}
                fixes = []
                for f in (raw_plan.get("fixes") or []):
                    src = valid_sources.get((f.get("from_type"), f.get("from_handle")))
                    if not src:
                        continue  # invented or already-linked source — discard
                    dedupe = (f.get("from_handle"), h)
                    if dedupe in seen_fix_keys:
                        continue
                    seen_fix_keys.add(dedupe)
                    fix = {
                        "action": f"add_{item_type}_link",
                        "direction": "inbound",
                        "from_type": src["type"],
                        "from_handle": src["handle"],
                        "from_title": src.get("title") or src["handle"],
                        "to_type": item_type,
                        "to_handle": h,
                        "to_title": coll.get("title") or h,
                        "suggested_anchor": (f.get("suggested_anchor") or coll.get("title") or h)[:120],
                        "reason": f.get("reason") or "",
                    }
                    if item_type == "article" and coll.get("blog_handle"):
                        fix["to_blog_handle"] = coll["blog_handle"]
                    if src.get("blog_handle"):
                        fix["from_blog_handle"] = src["blog_handle"]
                    fixes.append(fix)
                    if len(fixes) >= links_per_page:
                        break

                # Dead-end targets also get outbound links, from the same
                # validated candidate pool — the target page links out to them.
                outbound = []
                if (coll.get("outgoing_count") or 0) == 0:
                    for o in (raw_plan.get("outbound") or []):
                        src = valid_sources.get(
                            (o.get("type") or o.get("from_type"), o.get("handle") or o.get("from_handle"))
                        )
                        if not src:
                            continue
                        dedupe = (h, src["handle"])
                        if dedupe in seen_fix_keys:
                            continue
                        seen_fix_keys.add(dedupe)
                        fix = {
                            "action": "add_outbound_link",
                            "direction": "outbound",
                            "from_type": item_type,
                            "from_handle": h,
                            "from_title": coll.get("title") or h,
                            "to_type": src["type"],
                            "to_handle": src["handle"],
                            "to_title": src.get("title") or src["handle"],
                            "suggested_anchor": (o.get("suggested_anchor") or src.get("title") or src["handle"])[:120],
                            "reason": o.get("reason") or "",
                        }
                        if item_type == "article" and coll.get("blog_handle"):
                            fix["from_blog_handle"] = coll["blog_handle"]
                        if src.get("blog_handle"):
                            fix["to_blog_handle"] = src["blog_handle"]
                        outbound.append(fix)
                        if len(outbound) >= links_per_page:
                            break

                plan = {
                    "item_type": item_type,
                    "handle": h,
                    "title": coll.get("title") or h,
                    "blog_handle": coll.get("blog_handle"),
                    "fixes": fixes + outbound,
                }
                if not plan["fixes"]:
                    plan["note"] = f"AI selected no viable links for this {item_type}."
                plans.append(plan)

        inbound_total = sum(
            1 for p in plans for f in p["fixes"] if f.get("direction") != "outbound"
        )
        outbound_total = sum(
            1 for p in plans for f in p["fixes"] if f.get("direction") == "outbound"
        )
        summary = (
            f"Proposed {inbound_total} inbound"
            + (f" and {outbound_total} outbound" if outbound_total else "")
            + f" link{'s' if inbound_total + outbound_total != 1 else ''} "
            + f"across {len(plans)} page{'s' if len(plans) != 1 else ''}."
        )
        return {
            "plans": plans,
            "scope": scope,
            "links_per_page": links_per_page,
            "summary": summary,
        }

    elif task_type == "technical_seo_fix":
        item_type = task_input.get("item_type", "product")
        item_id = task_input.get("item_id")
        analysis_id = task_input.get("analysis_id")

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM technical_seo_analyses WHERE id = %s", (analysis_id,))
            analysis = cur.fetchone()

        if not analysis:
            return {"error": f"Analysis {analysis_id} not found"}

        with db.get_cursor() as cur:
            if item_type == "product":
                cur.execute("SELECT * FROM synced_products WHERE id = %s", (item_id,))
            else:
                cur.execute("SELECT * FROM synced_collections WHERE id = %s", (item_id,))
            item = cur.fetchone()

        if not item:
            return {"error": f"{item_type} {item_id} not found"}

        checks = analysis["checks"]
        if isinstance(checks, str):
            checks = json.loads(checks)
        detected_kws = analysis["detected_keywords"]
        if isinstance(detected_kws, str):
            detected_kws = json.loads(detected_kws)

        item_context = {
            "title": item.get("title") or "",
            "handle": item.get("handle") or "",
            "description": item.get("description") or "",
            "meta_title": item.get("meta_title") or "",
            "meta_description": item.get("meta_description") or "",
            "tags": item.get("tags") or "",
        }
        if item_type == "product":
            item_context["vendor"] = item.get("vendor") or ""
            item_context["product_type"] = item.get("product_type") or ""
            images = item.get("images") or []
            if isinstance(images, str):
                images = json.loads(images)
            item_context["image_count"] = len(images)
            item_context["images_without_alt"] = [
                {"media_id": img.get("media_id", ""), "src": img.get("url", "")}
                for img in images if not img.get("alt")
            ]
            # Full image list with current alts — alt-balance fixes rewrite
            # EXISTING alts, so the model must see them and their media_ids.
            # (Synced images store the URL under "url"; the filename in it is
            # often the only hint of what the image shows.)
            item_context["images"] = [
                {"media_id": img.get("media_id", ""), "alt": img.get("alt") or "",
                 "src": img.get("url", "")}
                for img in images
            ]

        _PROMO_RE = re.compile(r'\b(\d+)\s*for\s*\$?(\d+(?:\.\d{1,2})?[c¢]?)\b', re.IGNORECASE)
        promos = [m.group(0).strip() for m in _PROMO_RE.finditer(item_context.get("title", ""))] if item_type == "product" else []

        _PACKAGING_RE = re.compile(
            r'\b(\d+(?:\.\d+)?)\s*-?\s*'
            r'(packs?|ct|counts?|oz|lbs?|grams?|kg|ml|bags?|boxes?|cartons?|cases?|cans?|rolls?|pieces?|pcs?)\b',
            re.IGNORECASE,
        )
        packaging = [m.group(0).strip() for m in _PACKAGING_RE.finditer(item_context.get("title", ""))] if item_type == "product" else []

        failing_checks = {k: v for k, v in checks.items() if v.get("issue") and v.get("fix_type")}

        # Issue-type bulk fixes scope the AI to specific fields — everything
        # downstream (prompt, density/tags instructions) derives from
        # failing_checks, so filtering here scopes the whole fix.
        only_fields = task_input.get("only_fields")
        if only_fields:
            failing_checks = {
                k: v for k, v in failing_checks.items() if v.get("fix_type") in only_fields
            }

        sibling_context = ""
        category_context = ""
        if item_type == "product":
            vendor_val = item.get("vendor") or ""
            store_id_val = item.get("store_id")
            title_val = item.get("title") or ""

            if vendor_val and store_id_val:
                with db.get_cursor() as cur:
                    cur.execute(
                        """SELECT title, tags FROM synced_products
                        WHERE store_id = %s AND vendor = %s AND id != %s
                        ORDER BY title LIMIT 20""",
                        (store_id_val, vendor_val, item_id),
                    )
                    siblings = cur.fetchall()
                if siblings:
                    sibling_context = json.dumps(
                        [{"title": s["title"], "tags": s["tags"] or ""} for s in siblings],
                        indent=2,
                    )

            if store_id_val:
                tags_str = item.get("tags") or ""
                category = ""
                for tag in tags_str.split(","):
                    tag = tag.strip()
                    if tag.startswith("Type_"):
                        category = tag[5:].strip()
                        break
                if not category:
                    for phrase in ["Pipe Tobacco", "Filtered Cigars", "Premium Cigars",
                                   "Cigarillos", "Hookah Tobacco", "Filter Tubes"]:
                        if phrase.lower() in title_val.lower():
                            category = phrase
                            break

                if category:
                    with db.get_cursor() as cur:
                        cur.execute(
                            """SELECT vendor, title, tags FROM synced_products
                            WHERE store_id = %s AND vendor <> %s
                              AND (tags ILIKE %s OR title ILIKE %s)
                              AND tags IS NOT NULL AND tags <> ''
                            ORDER BY vendor, title LIMIT 10""",
                            (store_id_val, vendor_val,
                             f"%{category}%", f"%{category}%"),
                        )
                        peers = cur.fetchall()
                    if peers:
                        category_context = json.dumps(
                            [{"vendor": p["vendor"], "title": p["title"],
                              "tags": p["tags"]} for p in peers],
                            indent=2,
                        )

        analysis_payload = {
            "score": analysis["score"],
            "detected_keywords": detected_kws,
            "failing_checks": failing_checks,
            "item_data": item_context,
        }
        # Demand-grounded resolution (when the item has been re-analyzed since
        # the keyword resolver shipped) — tells the AI WHY this primary matters.
        resolution = analysis.get("keyword_resolution")
        if isinstance(resolution, str):
            try:
                resolution = json.loads(resolution)
            except (ValueError, TypeError):
                resolution = None
        if resolution and resolution.get("primary"):
            analysis_payload["primary_keyword"] = resolution["primary"]
            demand_candidates = [
                {k: c.get(k) for k in ("keyword", "volume", "gsc_clicks", "gsc_impressions",
                                       "gsc_position", "semrush_position", "sources")}
                for c in (resolution.get("candidates") or [])
                if c.get("volume") or c.get("gsc_impressions")
            ][:5]
            analysis_payload["search_demand"] = {
                "note": "The primary keyword was selected from real search demand "
                        "(GSC queries + Semrush volume), not just page text. Optimize for it.",
                "top_queries": demand_candidates,
                "manual": bool(resolution["primary"].get("manual")),
            }
        fix_context = task_input.get("fix_context") or {}
        if fix_context.get("kind") == "ctr_rewrite":
            analysis_payload["ctr_underperformance"] = {
                "note": "This page already ranks well for the queries below but earns far "
                        "fewer clicks than expected for its position. Rewrite meta_title and "
                        "meta_description to raise SERP click-through: keep the resolved "
                        "primary_keyword primary, and naturally work in the phrasing of these "
                        "real Google queries. Do not keyword-stuff.",
                "queries": fix_context.get("queries") or [],
                "position": fix_context.get("position"),
                "actual_ctr": fix_context.get("actual_ctr"),
                "expected_ctr": fix_context.get("expected_ctr"),
                "estimated_monthly_clicks_lost": fix_context.get("est_clicks_gain"),
            }
        if sibling_context:
            analysis_payload["sibling_products"] = json.loads(sibling_context)
        if category_context:
            analysis_payload["category_peers"] = json.loads(category_context)
        analysis_context = json.dumps(analysis_payload, indent=2)

        has_description_issues = any(
            v.get("fix_type") == "description"
            for v in failing_checks.values()
        )

        if item_type == "product":
            tags_rules = "- Tags: relevant, SEO-friendly product tags as an array\n"
            if sibling_context:
                tags_rules += (
                    "- BRAND CONSISTENCY: sibling_products lists other products from the same vendor. "
                    "All siblings MUST share the same base tags (brand name, product category, "
                    "common sizes/weights). Only add 1-2 tags unique to this variant (flavor, color).\n"
                    "- Reuse tags already assigned to siblings. Do NOT invent new base tags "
                    "if siblings already have established ones.\n"
                )
            if category_context:
                tags_rules += (
                    "- CATEGORY CONSISTENCY: category_peers lists products from other vendors in "
                    "the same category. Use the same tag formats for sizes, packaging, and category "
                    "terms as the peers (e.g., if peers use '16oz Bag', do NOT use '16 oz. Bag' "
                    "or '1lb bag').\n"
                )
            if promos:
                tags_rules += (
                    "- PROMOTIONAL PRICING: The title contains promotional pricing: "
                    + ", ".join(f'"{p}"' for p in promos)
                    + ". Create ONE dedicated tag for each promotional phrase exactly as written "
                    "(e.g., 'Promo_4 for 99c'). Do NOT split pricing fragments across other tags. "
                    "Do NOT include price numbers or currency symbols in non-promotional tags.\n"
                )
            brand = item_context.get("vendor", "")
            category = item_context.get("product_type", "")
            segments = []
            if brand:
                segments.append(f'Brand: "{brand}"')
            if category:
                segments.append(f'Category: "{category}"')
            if packaging:
                segments.append("Packaging/Size: " + ", ".join(f'"{p}"' for p in packaging))
            if promos:
                segments.append("Promo: " + ", ".join(f'"{p}"' for p in promos))
            if segments:
                tags_rules += (
                    "- TITLE STRUCTURE: The product title breaks down as: "
                    + "; ".join(segments)
                    + ". Everything else is likely flavor/variant. "
                    "Each segment should produce its own tag(s). "
                    "Do NOT mix packaging numbers with non-packaging words "
                    "(e.g., '15 Packs' is valid, 'Melonberry 15' is NOT). "
                    "Only create combined tags where the full packaging phrase is included "
                    "(e.g., 'Cigarillos 15 Packs' is OK, 'Cigarillos 15' is NOT). "
                    "Do NOT split multi-word category phrases — if the category is 'Pipe Tobacco', "
                    "never use 'Pipe' or 'Tobacco' as standalone tags or combine them with unrelated words. "
                    "Always keep the full category phrase together (e.g., 'Pipe Tobacco' is valid, 'Pipe' alone is NOT).\n"
                )
            tags_rules += (
                "- Tag structure: [brand tags] + [category tags] + [size/packaging tags] "
                "+ [variant-specific tags]\n"
                "- Image alt: write varied, descriptive alt texts; include the primary keyword in only "
                "the one or two most relevant images (typically the first/hero image); NEVER put the "
                "keyword in every alt. proposed_value MUST be an array of objects "
                '[{"media_id": "<exact media_id from the images list>", "alt": "<alt text>"}] — never bare strings. '
                "For missing alt text: one entry per images_without_alt item. For keyword-balance fixes "
                "(image_alt_has_keyword failing): rewrite the EXISTING alts from the images list — keep the "
                "keyword in the first/hero alt, remove it from the others by describing what each image shows\n"
            )
        else:
            tags_rules = "- Do NOT propose tags or image_alt fixes for collections\n"

        description_rules = ""
        if has_description_issues:
            import re as _re
            current_desc = item_context.get("description") or ""
            link_matches = _re.findall(r'<a\s[^>]*href="[^"]*"[^>]*>.*?</a>', current_desc)
            links_instruction = ""
            if link_matches:
                links_list = "\n".join(f"  {i+1}. {link}" for i, link in enumerate(link_matches))
                links_instruction = (
                    "- CRITICAL: The current description contains internal collection links that MUST be preserved.\n"
                    "  Include ALL of the following links in the rewritten description (copy them exactly as-is):\n"
                    + links_list + "\n"
                    "  Place them naturally within relevant paragraphs. Do NOT change the href or link text.\n"
                )

            stuffing_instruction = ""
            density_check = failing_checks.get("keyword_density", {})
            if density_check.get("issue"):
                primary_kw = detected_kws[0]["keyword"] if detected_kws else ""
                if primary_kw:
                    stuffing_instruction = (
                        f"- KEYWORD STUFFING FIX: The keyword '{primary_kw}' appears excessively in the "
                        "description. REDUCE exact-match repetitions, replacing most occurrences with "
                        "natural synonyms, related terms, or pronouns — but you MUST keep the exact "
                        "keyword at least once in the first paragraph and once in a heading. Never "
                        "remove it entirely, and do NOT add more mentions.\n"
                    )

            description_rules = (
                "- Description: if there are failing description checks (keyword_density, body_first_100_has_keyword, "
                "description_word_count, heading_structure), propose an improved HTML description\n"
                "- Keep the existing description content and structure, but enhance it to fix the failing checks\n"
                "- Use the primary keyword naturally in the first paragraph and in 1-2 headings; cover "
                "related terms and synonyms; never force repetition\n"
                + stuffing_instruction
                + "- If word count is low, expand with relevant product information (features, usage, benefits)\n"
                "- If headings are missing, add H2/H3 subheadings to structure the content\n"
                "- NEVER use <h1> tags in the description — the page already has an H1 (the title); use h2/h3 only\n"
                "- Output valid HTML (p, h2, h3, ul, li tags)\n"
                + links_instruction
            )

        _tech_fix_template = get_setting("prompts.technical_seo_fix", DEFAULTS["prompts.technical_seo_fix"])
        try:
            system_prompt = _tech_fix_template.format(tags_rules=tags_rules, description_rules=description_rules)
        except (KeyError, IndexError):
            system_prompt = DEFAULTS["prompts.technical_seo_fix"].format(tags_rules=tags_rules, description_rules=description_rules)
        user_prompt = (
            f"Here is the technical SEO analysis for this {item_type}. "
            "Propose specific fixes for the failing checks:\n\n"
            + analysis_context
        )
        if fix_context.get("kind") == "ctr_rewrite":
            fields_list = ", ".join(only_fields) if only_fields else "meta_title, meta_description"
            user_prompt += (
                f"\n\nThis is a targeted CTR fix: regardless of the failing checks (even if "
                f"none are failing), you MUST return a fix for each of these fields: "
                f"{fields_list} — rewritten to address the ctr_underperformance data above."
            )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                if only_fields:
                    result["fixes"] = [f for f in result.get("fixes", []) if f.get("field") in only_fields]
                from app.services.ai_content_validator import clean_keyword, validate_technical_fixes
                _res_primary = (resolution or {}).get("primary") if isinstance(resolution, dict) else None
                fix_kw = clean_keyword(
                    (_res_primary or {}).get("keyword")
                    or (detected_kws[0].get("keyword") if detected_kws else None)
                )
                validate_technical_fixes(result, fix_kw, client, config)
                return result
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "landing_page_seo":
        synced_page_id = task_input.get("synced_page_id")
        landing_page_id = task_input.get("landing_page_id")

        if synced_page_id:
            system_prompt = get_setting("prompts.landing_page_seo", DEFAULTS["prompts.landing_page_seo"])
            user_prompt = (
                f"Analyze synced page ID {synced_page_id} for SEO optimization. "
                "First get the page data, then analyze its SEO score, "
                "then provide your optimized recommendations."
            )
        else:
            system_prompt = get_setting("prompts.landing_page_seo", DEFAULTS["prompts.landing_page_seo"])
            user_prompt = (
                f"Analyze builder landing page ID {landing_page_id} for SEO optimization. "
                "First get the page data, then analyze its SEO score, "
                "then provide your optimized recommendations."
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        main_result = None
        for _ in range(10):
            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                tools=LANDING_PAGE_TOOL_DEFINITIONS,
                temperature=0.3,
            )
            choice = response.choices[0]
            if choice.message.tool_calls:
                messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"error": "Invalid tool arguments (malformed JSON). Retry with valid JSON."}),
                        })
                        continue
                    if fn_name == "analyze_landing_page_seo":
                        # Bind the page identity server-side — if the model omits
                        # page_type, a builder id would alias into synced_pages.
                        fn_args["page_type"] = "builder" if landing_page_id else "synced"
                        fn_args["page_id"] = landing_page_id or synced_page_id
                    result = _handle_landing_page_tool_call(fn_name, fn_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })
            else:
                content = choice.message.content or ""
                try:
                    start = content.find("{")
                    end = content.rfind("}") + 1
                    if start >= 0 and end > start:
                        main_result = json.loads(content[start:end])
                except (json.JSONDecodeError, ValueError):
                    pass
                if main_result is None:
                    main_result = {"raw_response": content}
                break

        if main_result is None:
            main_result = {"error": "Max iterations reached"}
        return main_result

    elif task_type == "landing_page_generate":
        topic = task_input.get("topic", "")
        keywords = task_input.get("keywords", [])

        system_prompt = get_setting("prompts.landing_page_generate", DEFAULTS["prompts.landing_page_generate"])

        products_context = ""
        if store_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' LIMIT 20",
                    (store_id,),
                )
                products = cur.fetchall()
            if products:
                products_context = "\n\nAVAILABLE PRODUCTS (use handles for product_showcase sections):\n"
                for p in products:
                    products_context += f"- {p['title']} (handle: {p['handle']})\n"

        collections_context = ""
        if store_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' LIMIT 20",
                    (store_id,),
                )
                collections = cur.fetchall()
            if collections:
                collections_context = "\nAVAILABLE COLLECTIONS (use for internal linking):\n"
                for c in collections:
                    collections_context += f"- {c['title']} (/collections/{c['handle']})\n"

        kw_text = f"\nTarget keywords: {', '.join(keywords)}" if keywords else ""
        user_prompt = (
            f"Generate a complete, SEO-optimized landing page about: {topic}\n"
            f"{kw_text}"
            f"{products_context}"
            f"{collections_context}"
            "\nReturn the complete page structure as JSON."
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                result = _validate_landing_page_generate(result)
                return result
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "home_page_generate":
        topic = task_input.get("topic", "")
        keywords = task_input.get("keywords", [])

        system_prompt = get_setting("prompts.home_page_generate", DEFAULTS["prompts.home_page_generate"])

        store_name = ""
        store_domain = ""
        if store_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT name, COALESCE(primary_domain, domain) AS store_domain FROM stores WHERE id = %s",
                    (store_id,),
                )
                srow = cur.fetchone()
            if srow:
                store_name = srow.get("name") or ""
                store_domain = srow.get("store_domain") or ""

        collections_context = ""
        if store_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' ORDER BY title LIMIT 30",
                    (store_id,),
                )
                collections = cur.fetchall()
            if collections:
                collections_context = "\nAVAILABLE COLLECTIONS (use for cta_url as /collections/handle):\n"
                for c in collections:
                    collections_context += f"- {c['title']} (/collections/{c['handle']})\n"

        serp_context = ""
        try:
            from app.services.serp_scraper import get_serp_benchmark
            primary_kw = keywords[0] if keywords else topic
            if primary_kw:
                serp_context = get_serp_benchmark(
                    {"title": primary_kw, "store_domain": store_domain},
                    target_keywords=keywords or [topic],
                    entity_type="collection",
                )
        except Exception as e:
            logger.warning("Home page SERP benchmark failed: %s", e)

        kw_text = f"\nTarget keywords: {', '.join(keywords)}" if keywords else ""
        serp_block = f"\n\nLIVE SERP BENCHMARK:\n{serp_context}" if serp_context else ""
        user_prompt = (
            f"Build the home page hero carousel for the store '{store_name or store_domain}'.\n"
            f"Store theme / topic: {topic}\n"
            f"{kw_text}"
            f"{collections_context}"
            f"{serp_block}"
            "\n\nReturn the carousel and home-page meta as JSON."
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                return _validate_home_page_generate(result)
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "landing_page_rewrite":
        synced_page_id = task_input.get("synced_page_id")
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_pages WHERE id = %s", (synced_page_id,))
            page = cur.fetchone()
        if not page:
            return {"error": f"Page {synced_page_id} not found"}

        system_prompt = get_setting("prompts.landing_page_rewrite", DEFAULTS["prompts.landing_page_rewrite"])

        products_context = ""
        if store_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' LIMIT 20",
                    (store_id,),
                )
                products = cur.fetchall()
            if products:
                products_context = "\nAVAILABLE PRODUCTS (use for internal links):\n"
                for p in products:
                    products_context += f"- {p['title']}: /products/{p['handle']}\n"

        collections_context = ""
        if store_id:
            with db.get_cursor() as cur:
                cur.execute(
                    "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL LIMIT 20",
                    (store_id,),
                )
                collections = cur.fetchall()
            if collections:
                collections_context = "\nAVAILABLE COLLECTIONS (use for internal links):\n"
                for c in collections:
                    collections_context += f"- {c['title']}: /collections/{c['handle']}\n"

        user_prompt = (
            f"Rewrite and optimize this page for SEO:\n\n"
            f"PAGE TITLE: {page['title']}\n"
            f"CURRENT HTML:\n{page['body_html'] or '(empty)'}\n"
            f"{products_context}"
            f"{collections_context}\n"
            f"Return the improved HTML only."
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )

        content = response.choices[0].message.content or ""
        content = re.sub(r"^```(?:html)?\s*\n?", "", content)
        content = re.sub(r"\n?```\s*$", "", content)
        return {"rewritten_html": content.strip()}

    elif task_type == "article_seo":
        article_id = task_input.get("article_id")

        with db.get_cursor() as cur:
            cur.execute("SELECT store_id FROM synced_articles WHERE id = %s", (article_id,))
            art_row = cur.fetchone()
        article_store_id = art_row["store_id"] if art_row else store_id

        system_prompt = get_setting("prompts.article_seo", DEFAULTS["prompts.article_seo"])

        search_data_instruction = ""
        try:
            if article_store_id:
                search_ctx = _get_search_context_for_item("article", article_id, article_store_id)
                if search_ctx:
                    search_data_instruction = (
                        "\n\n## CRITICAL: Real Search Data Available\n"
                        "The following data comes from Google Search Console and Semrush. "
                        "Use this data to inform your keyword choices. Prioritize keywords "
                        "that already have search volume and where this article is in striking "
                        "distance (positions 4-20). Do NOT ignore this data in favor of generic suggestions.\n\n"
                        + search_ctx
                    )
        except Exception as e:
            logger.warning("Search context injection failed for article %s: %s", article_id, e)

        system_prompt += search_data_instruction

        user_prompt = (
            f"Analyze article ID {article_id} for SEO optimization. "
            "First get the article data, then analyze its SEO score, "
            "then get products and collections for internal linking. "
            "Provide your optimized recommendations."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        main_result = None
        for _ in range(10):
            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                tools=ARTICLE_TOOL_DEFINITIONS,
                temperature=0.3,
            )
            choice = response.choices[0]
            if choice.message.tool_calls:
                messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"error": "Invalid tool arguments (malformed JSON). Retry with valid JSON."}),
                        })
                        continue
                    result = _handle_article_tool_call(fn_name, fn_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })
            else:
                content = choice.message.content or ""
                try:
                    start = content.find("{")
                    end = content.rfind("}") + 1
                    if start >= 0 and end > start:
                        main_result = json.loads(content[start:end])
                except (json.JSONDecodeError, ValueError):
                    pass
                if main_result is None:
                    main_result = {"raw_response": content}
                break

        if main_result is None:
            main_result = {"error": "Max iterations reached"}

        # Generate optimized body if we have enough context
        try:
            with db.get_cursor() as cur:
                cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
                article = cur.fetchone()

            if article:
                products = []
                collections = []
                other_articles = []
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' LIMIT 30",
                        (article_store_id,),
                    )
                    products = cur.fetchall()
                    cur.execute(
                        "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' LIMIT 30",
                        (article_store_id,),
                    )
                    collections = cur.fetchall()
                    cur.execute(
                        """SELECT sa.title, sa.handle, sb.handle as blog_handle
                        FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                        WHERE sa.store_id = %s AND sa.id != %s LIMIT 20""",
                        (article_store_id, article_id),
                    )
                    other_articles = cur.fetchall()

                link_context = ""
                if products:
                    link_context += "\nPRODUCTS FOR LINKING:\n" + "\n".join(
                        f"- {p['title']}: /products/{p['handle']}" for p in products
                    )
                if collections:
                    link_context += "\nCOLLECTIONS FOR LINKING:\n" + "\n".join(
                        f"- {c['title']}: /collections/{c['handle']}" for c in collections
                    )
                if other_articles:
                    link_context += "\nOTHER ARTICLES FOR CROSS-LINKING:\n" + "\n".join(
                        f"- {a['title']}: /blogs/{a['blog_handle']}/{a['handle']}" for a in other_articles
                    )

                body_system = get_setting("prompts.article_body", DEFAULTS["prompts.article_body"])

                body_user = (
                    f"ARTICLE TITLE: {article['title']}\n\n"
                    f"CURRENT BODY:\n{article.get('body', '')}\n\n"
                    f"KEYWORDS TO TARGET: {', '.join(main_result.get('keywords_used', []))}\n"
                    + link_context
                    + "\n\nRewrite and optimize this article body. Return raw HTML only."
                )

                body_response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": body_system},
                        {"role": "user", "content": body_user},
                    ],
                    temperature=0.4,
                )

                body_content = body_response.choices[0].message.content or ""
                body_content = re.sub(r"^```(?:html)?\s*\n?", "", body_content)
                body_content = re.sub(r"\n?```\s*$", "", body_content)
                body_content = _sanitize_description(body_content)

                if body_content:
                    main_result["optimized_body"] = body_content
        except Exception:
            logger.exception("Article body optimization failed")

        return main_result

    elif task_type == "article_refresh_links":
        article_id = task_input.get("article_id")
        product_ids = task_input.get("product_ids", [])
        collection_ids = task_input.get("collection_ids", [])

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_articles WHERE id = %s", (article_id,))
            article = cur.fetchone()
        if not article:
            return {"error": "Article not found"}

        refresh_store_id = article["store_id"]

        products = []
        collections = []
        other_articles = []
        with db.get_cursor() as cur:
            if product_ids:
                cur.execute(
                    "SELECT id, title, handle FROM synced_products WHERE id = ANY(%s)",
                    (product_ids,),
                )
                products = [dict(r) for r in cur.fetchall()]
            if collection_ids:
                cur.execute(
                    "SELECT id, title, handle FROM synced_collections WHERE id = ANY(%s)",
                    (collection_ids,),
                )
                collections = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """SELECT sa.title, sa.handle, sb.handle as blog_handle
                FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                WHERE sa.store_id = %s AND sa.id != %s ORDER BY sa.title LIMIT 20""",
                (refresh_store_id, article_id),
            )
            other_articles = [dict(r) for r in cur.fetchall()]

        system_prompt = get_setting("prompts.article_refresh_links", DEFAULTS["prompts.article_refresh_links"])

        user_parts = [
            f"ARTICLE TITLE: {article['title']}",
            f"EXISTING BODY HTML:\n{article.get('body', '')}",
        ]
        if products:
            user_parts.append("PRODUCTS FOR LINKING:\n" + "\n".join(
                f"- {p['title']}: /products/{p['handle']}" for p in products
            ))
        if collections:
            user_parts.append("COLLECTIONS FOR LINKING:\n" + "\n".join(
                f"- {c['title']}: /collections/{c['handle']}" for c in collections
            ))
        if other_articles:
            user_parts.append("OTHER ARTICLES FOR CROSS-LINKING:\n" + "\n".join(
                f"- {a['title']}: /blogs/{a['blog_handle']}/{a['handle']}" for a in other_articles
            ))
        user_parts.append("Add internal links to the products, collections, and articles listed above. Return raw HTML only.")

        body_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.3,
        )

        body_content = body_response.choices[0].message.content or ""
        body_content = re.sub(r"^```(?:html)?\s*\n?", "", body_content)
        body_content = re.sub(r"\n?```\s*$", "", body_content)
        body_content = _sanitize_description(body_content)

        text_only = re.sub(r"<[^>]+>", "", body_content)
        word_count = len(text_only.split())
        internal_links_count = len(re.findall(r'<a\s[^>]*href\s*=\s*["\']/', body_content, re.IGNORECASE))

        return {
            "refreshed_body": body_content,
            "stats": {
                "word_count": word_count,
                "internal_links_count": internal_links_count,
            },
        }

    elif task_type == "article_research":
        topic = task_input.get("topic", "")
        keywords = task_input.get("keywords", [])
        research_store_id = task_input.get("store_id") or store_id

        # Gather SERP data for the topic and keywords
        serp_results = []
        competitor_analysis = {"avg_word_count": 0, "common_headings": [], "top_domains": []}
        keyword_opportunities = []

        try:
            from app.services.serp_scraper import brave_search, _scrape_urls

            search_queries = [topic] + keywords[:3]
            all_urls = []
            for query in search_queries:
                try:
                    brave_results = brave_search(query)
                    if not brave_results:
                        continue
                    for r in brave_results[:5]:
                        serp_results.append({
                            "query": query,
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "description": r.get("description", ""),
                        })
                        if r.get("url"):
                            all_urls.append(r["url"])
                except Exception as e:
                    logger.warning("SERP search failed for '%s': %s", query, e)

            # Scrape top pages for analysis
            scraped_pages = _scrape_urls(all_urls[:8])
            word_counts = []
            all_headings = []
            domains = set()
            for page_data in scraped_pages:
                if page_data:
                    word_counts.append(page_data.get("word_count", 0))
                    all_headings.extend(page_data.get("h2s", []))
                    if page_data.get("domain"):
                        domains.add(page_data["domain"])

            if word_counts:
                competitor_analysis["avg_word_count"] = round(sum(word_counts) / len(word_counts))
            competitor_analysis["common_headings"] = list(set(all_headings))[:20]
            competitor_analysis["top_domains"] = list(domains)[:10]

        except Exception as e:
            logger.warning("SERP research failed: %s", e)

        # Use AI to analyze and suggest keywords + content angles
        content_angles = []
        rec_word_count = 0
        try:
            research_system = get_setting("prompts.article_research", DEFAULTS["prompts.article_research"])
            primary_kw, secondary_kws = _split_keywords(keywords)
            research_user = (
                f"Topic: {topic}\n"
                f"Primary Keyword: {primary_kw}\n"
                + (f"Secondary Keywords: {', '.join(secondary_kws)}\n" if secondary_kws else "")
                + f"Competitor Avg Word Count: {competitor_analysis['avg_word_count']}\n"
                f"Competitor Headings: {', '.join(competitor_analysis['common_headings'][:15])}\n"
                f"Top Domains: {', '.join(competitor_analysis['top_domains'][:5])}"
            )

            ai_response = client.chat.completions.create(
                model=config["model"],
                messages=[
                    {"role": "system", "content": research_system},
                    {"role": "user", "content": research_user},
                ],
                temperature=0.4,
            )
            ai_content = ai_response.choices[0].message.content or ""
            try:
                start = ai_content.find("{")
                end = ai_content.rfind("}") + 1
                if start >= 0 and end > start:
                    ai_data = json.loads(ai_content[start:end])
                    keyword_opportunities = ai_data.get("keyword_opportunities", [])
                    content_angles = ai_data.get("content_angle_suggestions", [])
                    rec_word_count = ai_data.get("recommended_word_count", 0)
            except (json.JSONDecodeError, ValueError):
                logger.warning("article_research: could not parse AI research response")
        except Exception as e:
            logger.warning("article_research AI analysis failed: %s", e)

        # AI-powered discovery of related products/collections
        related_products = []
        related_collections = []
        if research_store_id:
            try:
                # Pre-filter the catalog by topic/keyword relevance and cap the
                # list — serializing every product (with tags) into one prompt
                # blows the context window / cost on large stores.
                kw_text = " ".join(keywords) if isinstance(keywords, list) else str(keywords or "")
                terms = [t.strip() for t in re.split(r"[,\s]+", f"{topic} {kw_text}") if len(t.strip()) >= 4][:8]
                with db.get_cursor() as cur:
                    if terms:
                        like_clauses = " OR ".join(
                            ["title ILIKE %s OR product_type ILIKE %s OR vendor ILIKE %s"] * len(terms)
                        )
                        like_params = []
                        for t in terms:
                            like_params.extend([f"%{t}%"] * 3)
                        cur.execute(
                            f"""SELECT id, title, handle, vendor, product_type FROM synced_products
                                WHERE store_id = %s AND status = 'ACTIVE' AND ({like_clauses})
                                LIMIT 300""",
                            (research_store_id, *like_params),
                        )
                        all_products = [dict(r) for r in cur.fetchall()]
                    else:
                        all_products = []
                    if not all_products:
                        cur.execute(
                            "SELECT id, title, handle, vendor, product_type FROM synced_products "
                            "WHERE store_id = %s AND status = 'ACTIVE' ORDER BY id DESC LIMIT 300",
                            (research_store_id,),
                        )
                        all_products = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT id, title, handle, collection_type FROM synced_collections "
                        "WHERE store_id = %s AND handle IS NOT NULL AND handle != '' LIMIT 300",
                        (research_store_id,),
                    )
                    all_collections = [dict(r) for r in cur.fetchall()]

                if all_products or all_collections:
                    discovery_prompt = get_setting("prompts.article_discovery", DEFAULTS["prompts.article_discovery"])

                    product_list = "\n".join(
                        f"- ID:{p['id']} | {p['title']} | handle:{p['handle']} | vendor:{p.get('vendor', '')} | type:{p.get('product_type', '')}"
                        for p in all_products
                    )
                    collection_list = "\n".join(
                        f"- ID:{c['id']} | {c['title']} | handle:{c['handle']} | type:{c.get('collection_type', '')}"
                        for c in all_collections
                    )

                    d_primary_kw, d_secondary_kws = _split_keywords(keywords)
                    discovery_user = (
                        f"ARTICLE TOPIC: {topic}\n"
                        f"PRIMARY KEYWORD: {d_primary_kw}\n"
                        + (f"SECONDARY KEYWORDS: {', '.join(d_secondary_kws)}\n" if d_secondary_kws else "")
                        + f"\nAVAILABLE PRODUCTS ({len(all_products)}):\n{product_list}\n\n"
                        f"AVAILABLE COLLECTIONS ({len(all_collections)}):\n{collection_list}"
                    )

                    discovery_response = client.chat.completions.create(
                        model=config["model"],
                        messages=[
                            {"role": "system", "content": discovery_prompt},
                            {"role": "user", "content": discovery_user},
                        ],
                        temperature=0.3,
                    )

                    discovery_content = discovery_response.choices[0].message.content or ""
                    try:
                        d_start = discovery_content.find("{")
                        d_end = discovery_content.rfind("}") + 1
                        if d_start >= 0 and d_end > d_start:
                            discovery_data = json.loads(discovery_content[d_start:d_end])
                            related_products = discovery_data.get("selected_products", [])
                            related_collections = discovery_data.get("selected_collections", [])
                    except (json.JSONDecodeError, ValueError):
                        logger.warning("Failed to parse AI discovery response")
            except Exception as e:
                logger.exception("AI-powered product/collection discovery failed: %s", e)
                related_products = []
                related_collections = []

        recommended = rec_word_count if rec_word_count else max(
            int(competitor_analysis["avg_word_count"] * 1.2) if competitor_analysis["avg_word_count"] else 2000,
            1500,
        )

        return {
            "serp_results": serp_results[:15],
            "competitor_analysis": competitor_analysis,
            "keyword_opportunities": keyword_opportunities,
            "recommended_word_count": recommended,
            "content_angle_suggestions": content_angles,
            "related_products": related_products[:15],
            "related_collections": related_collections[:10],
        }

    elif task_type == "blog_seo_suggestions":
        blog_title = task_input.get("blog_title", "")
        blog_handle = task_input.get("blog_handle", "")
        current_intro = task_input.get("current_intro", "")
        article_titles = task_input.get("article_titles", [])
        article_tags = task_input.get("article_tags", [])
        suggestion_store_id = task_input.get("store_id") or store_id

        store_name = ""
        store_domain = ""
        if suggestion_store_id:
            with db.get_cursor() as cur:
                cur.execute("SELECT COALESCE(shop_name, name) as name, COALESCE(primary_domain, domain) as domain FROM stores WHERE id = %s", (suggestion_store_id,))
                store_row = cur.fetchone()
            if store_row:
                store_name = store_row["name"]
                store_domain = store_row["domain"]

        serp_context = ""
        try:
            from app.services.serp_scraper import brave_search
            blog_keywords = get_setting("blog.search_keywords", DEFAULTS.get("blog.search_keywords", "blog tips deals"))
            queries = [f"{store_name} blog", blog_keywords]
            if article_tags:
                queries.append(" ".join(article_tags[:3]) + " blog")
            for query in queries[:3]:
                try:
                    results = brave_search(query)
                    if results:
                        for r in results[:3]:
                            serp_context += f"- {r.get('title', '')}: {r.get('description', '')}\n"
                except Exception:
                    pass
        except Exception as e:
            logger.warning("SERP research for blog suggestions failed: %s", e)

        system_prompt = get_setting("prompts.blog_seo_suggestions", DEFAULTS["prompts.blog_seo_suggestions"])

        articles_context = ""
        if article_titles:
            articles_context = "\nArticle titles in this blog:\n" + "\n".join(f"- {t}" for t in article_titles[:20])
        tags_context = ""
        if article_tags:
            tags_context = f"\nArticle tags: {', '.join(article_tags[:15])}"

        user_prompt = (
            f"Store name: {store_name}\n"
            f"Store domain: {store_domain}\n"
            f"Current blog title: {blog_title}\n"
            f"Blog handle: /blogs/{blog_handle}\n"
            f"Current intro: {current_intro or '(none)'}\n"
            f"{articles_context}"
            f"{tags_context}\n"
        )
        if serp_context:
            user_prompt += f"\nCompetitor blog pages from SERP:\n{serp_context}"

        try:
            ai_response = client.chat.completions.create(
                model=config["model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
            )
            content = ai_response.choices[0].message.content or ""
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                return result
        except (json.JSONDecodeError, ValueError):
            pass
        except Exception as e:
            logger.exception("Blog SEO suggestions AI call failed: %s", e)
        return {"error": "Failed to generate suggestions"}

    elif task_type == "article_outline":
        topic = task_input.get("topic", "")
        keywords = task_input.get("keywords", [])
        research_data = task_input.get("research_data", {})
        outline_store_id = task_input.get("store_id") or store_id

        # Get products/collections for internal link planning (prefer session selections)
        products = []
        collections = []
        if outline_store_id:
            outline_session_id = task_input.get("session_id")
            target_product_ids = []
            target_collection_ids = []
            if outline_session_id:
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT target_product_ids, target_collection_ids FROM article_wizard_sessions WHERE id = %s",
                        (outline_session_id,),
                    )
                    sess = cur.fetchone()
                    if sess:
                        target_product_ids = sess.get("target_product_ids") or []
                        target_collection_ids = sess.get("target_collection_ids") or []

            with db.get_cursor() as cur:
                if target_product_ids:
                    cur.execute(
                        "SELECT id, title, handle FROM synced_products WHERE id = ANY(%s)",
                        (target_product_ids,),
                    )
                    products = [dict(r) for r in cur.fetchall()]
                else:
                    cur.execute(
                        "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' ORDER BY title LIMIT 30",
                        (outline_store_id,),
                    )
                    products = [dict(r) for r in cur.fetchall()]

                if target_collection_ids:
                    cur.execute(
                        "SELECT id, title, handle FROM synced_collections WHERE id = ANY(%s)",
                        (target_collection_ids,),
                    )
                    collections = [dict(r) for r in cur.fetchall()]
                else:
                    cur.execute(
                        "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' ORDER BY title LIMIT 30",
                        (outline_store_id,),
                    )
                    collections = [dict(r) for r in cur.fetchall()]

        title_style = random.choice(_get_article_title_styles())
        style_examples = "; ".join(f'"{e}"' for e in title_style["examples"])
        _outline_template = get_setting("prompts.article_outline", DEFAULTS["prompts.article_outline"])
        try:
            system_prompt = _outline_template.format(
                title_style=title_style["style"],
                title_instruction=title_style["instruction"],
                style_examples=style_examples,
            )
        except (KeyError, IndexError):
            system_prompt = DEFAULTS["prompts.article_outline"].format(
                title_style=title_style["style"],
                title_instruction=title_style["instruction"],
                style_examples=style_examples,
            )
        o_primary_kw, o_secondary_kws = _split_keywords(keywords)
        user_parts = [
            f"Topic: {topic}",
            f"Primary Keyword: {o_primary_kw}",
        ]
        if o_secondary_kws:
            user_parts.append(f"Secondary Keywords: {', '.join(o_secondary_kws)}")
        if research_data:
            user_parts.append(f"Recommended Word Count: {research_data.get('recommended_word_count', 2000)}")
            if research_data.get("competitor_analysis", {}).get("common_headings"):
                user_parts.append(f"Competitor Headings: {', '.join(research_data['competitor_analysis']['common_headings'][:15])}")
            if research_data.get("content_angle_suggestions"):
                user_parts.append(f"Content Angles: {', '.join(research_data['content_angle_suggestions'])}")
        if products:
            product_lines = "\n".join(f"- {p['title']}: /products/{p['handle']}" for p in products[:20])
            if target_product_ids:
                user_parts.append(
                    "Hand-Picked Products (deliberately selected for this article — EVERY one "
                    "must appear in internal_link_plan, each linked once in its most relevant section):\n"
                    + product_lines
                )
            else:
                user_parts.append("Products Available for Linking:\n" + product_lines)
        if collections:
            collection_lines = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in collections[:15])
            if target_collection_ids:
                user_parts.append(
                    "Hand-Picked Collections (deliberately selected for this article — EVERY one "
                    "must appear in internal_link_plan, each linked once in its most relevant section):\n"
                    + collection_lines
                )
            else:
                user_parts.append("Collections Available for Linking:\n" + collection_lines)

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.55,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "article_generate":
        topic = task_input.get("topic", "")
        keywords = task_input.get("keywords", [])
        outline_data = task_input.get("outline_data", {})
        research_data = task_input.get("research_data", {})
        gen_store_id = task_input.get("store_id") or store_id

        target_word_count = research_data.get("recommended_word_count", 2000) if research_data else 2000

        # Get linking context (prefer session selections)
        products = []
        collections = []
        other_articles = []
        if gen_store_id:
            gen_session_id = task_input.get("session_id")
            target_product_ids = []
            target_collection_ids = []
            if gen_session_id:
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT target_product_ids, target_collection_ids FROM article_wizard_sessions WHERE id = %s",
                        (gen_session_id,),
                    )
                    sess = cur.fetchone()
                    if sess:
                        target_product_ids = sess.get("target_product_ids") or []
                        target_collection_ids = sess.get("target_collection_ids") or []

            with db.get_cursor() as cur:
                if target_product_ids:
                    cur.execute(
                        "SELECT id, title, handle FROM synced_products WHERE id = ANY(%s)",
                        (target_product_ids,),
                    )
                    products = [dict(r) for r in cur.fetchall()]
                else:
                    cur.execute(
                        "SELECT title, handle FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' ORDER BY title LIMIT 30",
                        (gen_store_id,),
                    )
                    products = [dict(r) for r in cur.fetchall()]

                if target_collection_ids:
                    cur.execute(
                        "SELECT id, title, handle FROM synced_collections WHERE id = ANY(%s)",
                        (target_collection_ids,),
                    )
                    collections = [dict(r) for r in cur.fetchall()]
                else:
                    cur.execute(
                        "SELECT title, handle FROM synced_collections WHERE store_id = %s AND handle IS NOT NULL AND handle != '' ORDER BY title LIMIT 30",
                        (gen_store_id,),
                    )
                    collections = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """SELECT sa.title, sa.handle, sb.handle as blog_handle
                    FROM synced_articles sa JOIN synced_blogs sb ON sb.id = sa.blog_id
                    WHERE sa.store_id = %s ORDER BY sa.title LIMIT 20""",
                    (gen_store_id,),
                )
                other_articles = [dict(r) for r in cur.fetchall()]

        _gen_template = get_setting("prompts.article_generate", DEFAULTS["prompts.article_generate"])
        try:
            system_prompt = _gen_template.format(target_word_count=target_word_count)
        except (KeyError, IndexError):
            system_prompt = DEFAULTS["prompts.article_generate"].format(target_word_count=target_word_count)

        g_primary_kw, g_secondary_kws = _split_keywords(keywords)
        user_parts = [f"TOPIC: {topic}", f"PRIMARY KEYWORD: {g_primary_kw}"]
        if g_secondary_kws:
            user_parts.append(f"SECONDARY KEYWORDS: {', '.join(g_secondary_kws)}")

        if outline_data:
            user_parts.append(f"ARTICLE TITLE: {outline_data.get('suggested_title', topic)}")
            sections = outline_data.get("outline", [])
            if sections:
                outline_text = "\n".join(
                    f"{'  ' if s.get('level') == 'h3' else ''}{s.get('level', 'h2').upper()}: {s.get('heading', '')}"
                    + (f" (keywords: {', '.join(s.get('keywords', []))})" if s.get('keywords') else "")
                    + (f" — {s.get('notes', '')}" if s.get('notes') else "")
                    for s in sections
                )
                user_parts.append(f"OUTLINE:\n{outline_text}")
            if outline_data.get("internal_link_plan"):
                link_plan = "\n".join(
                    f"- \"{l.get('text', '')}\" → {l.get('url', '')} (in {l.get('section', '')})"
                    for l in outline_data["internal_link_plan"]
                )
                user_parts.append(f"INTERNAL LINK PLAN:\n{link_plan}")

        if products:
            product_lines = "\n".join(f"- {p['title']}: /products/{p['handle']}" for p in products[:20])
            if target_product_ids:
                user_parts.append(
                    "REQUIRED PRODUCT LINKS (hand-picked for this article — link EVERY product "
                    "below exactly once, with natural anchor text where most relevant):\n" + product_lines
                )
            else:
                user_parts.append("PRODUCTS FOR LINKING:\n" + product_lines)
        if collections:
            collection_lines = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in collections[:15])
            if target_collection_ids:
                user_parts.append(
                    "REQUIRED COLLECTION LINKS (hand-picked for this article — link EVERY collection "
                    "below exactly once, with natural anchor text where most relevant):\n" + collection_lines
                )
            else:
                user_parts.append("COLLECTIONS FOR LINKING:\n" + collection_lines)
        if other_articles:
            user_parts.append("OTHER ARTICLES FOR CROSS-LINKING:\n" + "\n".join(
                f"- {a['title']}: /blogs/{a['blog_handle']}/{a['handle']}" for a in other_articles[:10]
            ))

        user_parts.append(f"Write the full article body targeting ~{target_word_count} words. Return raw HTML only.")

        body_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.4,
        )

        body_content = body_response.choices[0].message.content or ""
        body_content = re.sub(r"^```(?:html)?\s*\n?", "", body_content)
        body_content = re.sub(r"\n?```\s*$", "", body_content)
        body_content = _sanitize_description(body_content)
        body_content = _fix_internal_link_handles(body_content, gen_store_id)

        # Count stats
        text_only = re.sub(r"<[^>]+>", "", body_content)
        word_count = len(text_only.split())
        internal_links_count = len(re.findall(r'<a\s[^>]*href\s*=\s*["\']/', body_content, re.IGNORECASE))
        keywords_found = [kw for kw in keywords if kw.lower() in text_only.lower()]

        # Generate meta fields
        meta_title = outline_data.get("meta_title", "")
        meta_description = outline_data.get("meta_description", "")
        article_title = outline_data.get("suggested_title", topic)

        # Generate summary
        summary = ""
        try:
            summary_response = client.chat.completions.create(
                model=config["model"],
                messages=[
                    {"role": "system", "content": get_setting("prompts.article_summary", DEFAULTS["prompts.article_summary"])},
                    {"role": "user", "content": f"Article title: {article_title}\nKeywords: {', '.join(keywords)}\nFirst paragraph: {text_only[:500]}"},
                ],
                temperature=0.3,
            )
            summary = summary_response.choices[0].message.content.strip()
        except Exception:
            summary = text_only[:155] + "..." if len(text_only) > 155 else text_only

        # Generate tags
        tags = keywords[:5] if keywords else []

        return {
            "title": article_title,
            "body_html": body_content,
            "meta_title": meta_title,
            "meta_description": meta_description,
            "summary": summary,
            "tags": tags,
            "word_count": word_count,
            "internal_links_count": internal_links_count,
            "keywords_used": keywords_found,
        }

    elif task_type == "ai_seo_product":
        product_id = task_input.get("product_id")
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            item = cur.fetchone()
        if not item:
            return {"error": "Product not found"}
        return _generate_geo_content(
            client, config, dict(item), "product", task_input.get("weak_signals")
        )

    elif task_type == "ai_seo_collection":
        collection_id = task_input.get("collection_id")
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            item = cur.fetchone()
        if not item:
            return {"error": "Collection not found"}
        return _generate_geo_content(
            client, config, dict(item), "collection", task_input.get("weak_signals")
        )

    elif task_type == "ai_seo_facts_product":
        product_id = task_input.get("product_id")
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            item = cur.fetchone()
        if not item:
            return {"error": "Product not found"}
        return _generate_fact_boost(client, config, dict(item), "product")

    elif task_type == "ai_seo_facts_collection":
        collection_id = task_input.get("collection_id")
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            item = cur.fetchone()
        if not item:
            return {"error": "Collection not found"}
        return _generate_fact_boost(client, config, dict(item), "collection")

    elif task_type == "store_audit":
        system_prompt = get_setting("prompts.store_audit", DEFAULTS["prompts.store_audit"])
        user_prompt = (
            f"Perform an SEO audit of store ID {store_id}. "
            "Get the store summary and identify key areas for improvement."
        )

    elif task_type == "topic_enrich":
        # Enrich ONE pipeline-sent topic suggestion (from the AI Search gap
        # analysis) into the full topic_discovery shape. Single grounded call —
        # winnability was already validated upstream, so no tool loop.
        suggestion_id = task_input.get("suggestion_id")
        if not suggestion_id:
            return {"error": "suggestion_id is required"}
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM topic_suggestions WHERE id = %s", (suggestion_id,))
            sug = cur.fetchone()
        if not sug:
            return {"error": "Suggestion not found"}
        te_store_id = sug["store_id"]
        seed_kw = sug["keywords"] or {}
        if isinstance(seed_kw, str):
            try:
                seed_kw = json.loads(seed_kw)
            except (json.JSONDecodeError, ValueError):
                seed_kw = {}
        primary = seed_kw.get("primary") or ""

        ctx = _topic_enrich_context(te_store_id, sug["topic"], primary)
        user_prompt = (
            f"STORE: {ctx['store_name']} ({ctx['store_domain']})\n\n"
            f"TOPIC TO ENRICH: {sug['topic']}\n"
            f"TARGET BUYER QUERY (primary keyword): {primary or '(derive from topic)'}\n"
            f"GAP-ANALYSIS REASONING: {sug['rationale'] or '(none)'}\n"
            f"SEED INTENT: {sug['search_intent'] or 'informational'}\n\n"
            f"PRODUCT CATEGORY INVENTORY:\n{ctx['profile']}\n\n"
            f"TOP VENDORS/BRANDS: {ctx['vendors']}\n\n"
            f"CANDIDATE LINK TARGETS — COLLECTIONS (copy names exactly):\n{ctx['collections'] or '(none matched)'}\n\n"
            f"CANDIDATE LINK TARGETS — PRODUCTS (copy names exactly):\n{ctx['products'] or '(none matched)'}\n\n"
            f"EXISTING BLOG ARTICLES:\n{ctx['articles'] or '(no blog articles yet)'}\n"
            + (f"\nARTICLES QUEUED OR IN PROGRESS (treat as existing):\n{ctx['queued']}\n" if ctx["queued"] else "")
            + (f"\nOTHER PENDING TOPIC SUGGESTIONS (avoid overlap):\n{ctx['other_suggestions']}\n" if ctx["other_suggestions"] else "")
        )
        resp = client.chat.completions.create(
            model=config.get("topic_research_model") or config["model"],
            messages=[
                {"role": "system", "content": get_setting("prompts.topic_enrich", DEFAULTS["prompts.topic_enrich"])},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
        )
        content = resp.choices[0].message.content or ""
        start, end = content.find("{"), content.rfind("}") + 1
        data = {}
        if start >= 0 and end > start:
            try:
                data = json.loads(content[start:end])
            except (json.JSONDecodeError, ValueError):
                data = {}
        if not data:
            return {"error": "AI did not return valid enrichment JSON"}

        kw = data.get("keywords") or {}
        if primary and not kw.get("primary"):
            kw["primary"] = primary
        intent = data.get("search_intent") or sug["search_intent"] or "Informational"
        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE topic_suggestions SET
                       topic = COALESCE(NULLIF(%s, ''), topic),
                       h1 = %s, meta_title = %s, meta_description = %s, url_slug = %s,
                       keywords = %s, search_intent = %s, rationale = COALESCE(NULLIF(%s, ''), rationale),
                       cannibalization_note = %s,
                       target_products = %s, target_collections = %s, target_articles = %s,
                       updated_at = NOW()
                   WHERE id = %s""",
                (
                    (data.get("topic") or "")[:500],
                    (data.get("h1") or "")[:500],
                    (data.get("meta_title") or "")[:255],
                    data.get("meta_description") or "",
                    (data.get("url_slug") or "")[:255],
                    json.dumps(kw), intent[:30],
                    data.get("rationale") or "",
                    data.get("cannibalization_note") or "",
                    json.dumps(data.get("target_products") or []),
                    json.dumps(data.get("target_collections") or []),
                    json.dumps(data.get("target_articles") or []),
                    suggestion_id,
                ),
            )
        return {"suggestion_id": suggestion_id, "topic": data.get("topic") or sug["topic"], "enriched": True}

    elif task_type == "topic_discovery":
        from datetime import datetime

        td_store_id = task_input.get("store_id") or store_id
        if not td_store_id:
            return {"error": "store_id is required for topic discovery"}

        try:
            td_count = int(task_input.get("count") or config["topic_suggestion_count"])
        except (TypeError, ValueError):
            td_count = 3
        td_count = max(1, min(td_count, 10))
        td_model = config.get("topic_research_model") or config["model"]

        with db.get_cursor() as cur:
            cur.execute("SELECT name, domain FROM stores WHERE id = %s", (td_store_id,))
            td_store = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(product_type, '(none)') AS product_type, COUNT(*) AS count "
                "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
                "GROUP BY product_type ORDER BY count DESC LIMIT 50",
                (td_store_id,),
            )
            td_types = cur.fetchall()
            cur.execute(
                "SELECT COALESCE(vendor, '(none)') AS vendor, COUNT(*) AS count "
                "FROM synced_products WHERE store_id = %s AND status = 'ACTIVE' "
                "GROUP BY vendor ORDER BY count DESC LIMIT 25",
                (td_store_id,),
            )
            td_vendors = cur.fetchall()
            cur.execute(
                "SELECT title, handle FROM synced_articles WHERE store_id = %s "
                "ORDER BY published_at DESC NULLS LAST LIMIT 300",
                (td_store_id,),
            )
            td_articles = cur.fetchall()
            cur.execute(
                "SELECT topic, status FROM topic_suggestions WHERE store_id = %s "
                "ORDER BY created_at DESC LIMIT 100",
                (td_store_id,),
            )
            td_prior = cur.fetchall()
            # Wizard sessions only reach synced_articles at publish time —
            # queued/scheduled/in-progress articles must still count as coverage.
            cur.execute(
                "SELECT COALESCE(generated_article->>'title', topic) AS title "
                "FROM article_wizard_sessions WHERE store_id = %s AND status != 'published' "
                "AND topic IS NOT NULL AND topic != '' "
                "AND updated_at > NOW() - INTERVAL '90 days' "
                "ORDER BY updated_at DESC LIMIT 50",
                (td_store_id,),
            )
            td_queued = cur.fetchall()
            # Keywords owned by money pages (resolver primaries + accepted
            # manual assignments) — a blog post must never target these as its
            # own primary. Cap by volume: the high-demand terms are the ones a
            # blog primary could realistically collide with.
            cur.execute(
                """SELECT DISTINCT ON (kw) kw, vol, item_type FROM (
                       SELECT keyword_resolution->'primary'->>'keyword' AS kw,
                              NULLIF(keyword_resolution->'primary'->>'volume', '')::numeric AS vol,
                              item_type
                       FROM technical_seo_analyses
                       WHERE store_id = %s AND keyword_resolution->'primary'->>'keyword' IS NOT NULL
                       UNION ALL
                       SELECT mk.keyword, mk.search_volume::numeric, ka.assigned_item_type
                       FROM keyword_assignments ka
                       JOIN master_keywords mk ON mk.id = ka.master_keyword_id
                       WHERE ka.store_id = %s AND ka.status IN ('accepted', 'applied')
                   ) sub WHERE kw IS NOT NULL
                   ORDER BY kw, vol DESC NULLS LAST""",
                (td_store_id, td_store_id),
            )
            td_reserved_all = cur.fetchall()
            td_reserved = sorted(
                td_reserved_all, key=lambda r: float(r["vol"] or 0), reverse=True
            )[:200]

        if not td_store:
            return {"error": "Store not found"}

        td_profile = "\n".join(
            f"- {t['product_type']}: {t['count']} products" for t in td_types
        )
        td_vendor_block = ", ".join(f"{v['vendor']} ({v['count']})" for v in td_vendors)
        td_article_block = "\n".join(
            f"- {a['title']} (/{a['handle']})" for a in td_articles
        ) or "(no blog articles yet)"
        td_prior_block = "\n".join(
            f"- \"{p['topic']}\" ({p['status']})" for p in td_prior
        )
        td_queued_block = "\n".join(f"- {q['title']}" for q in td_queued)

        td_user_prompt = (
            f"Current date: {datetime.now().strftime('%B %Y')}\n\n"
            f"STORE: {td_store['name']} ({td_store['domain']}), store ID {td_store_id}\n\n"
            f"PRODUCT CATEGORY INVENTORY (product type: count of active products):\n{td_profile}\n\n"
            f"TOP VENDORS/BRANDS: {td_vendor_block}\n\n"
            f"EXISTING BLOG ARTICLES ({len(td_articles)} total):\n{td_article_block}\n"
        )
        if td_queued_block:
            td_user_prompt += (
                "\nARTICLES ALREADY QUEUED OR IN PROGRESS (scheduled or being written, "
                "not yet live — treat these exactly like existing blog articles; do not "
                "suggest the same or closely overlapping topics):\n"
                + td_queued_block + "\n"
            )
        if td_prior_block:
            td_user_prompt += (
                "\nPREVIOUSLY SUGGESTED TOPICS - DO NOT REPEAT OR CLOSELY PARAPHRASE ANY OF THESE:\n"
                + td_prior_block + "\n"
            )
        if td_reserved:
            td_reserved_block = "\n".join(
                f"- \"{r['kw']}\"" + (f" (vol {int(r['vol']):,})" if r["vol"] else "") + f" — {r['item_type']} page"
                for r in td_reserved
            )
            td_user_prompt += (
                "\nRESERVED MONEY-PAGE KEYWORDS — each of these is the assigned primary "
                "keyword of a product or collection page on this store. A blog post must "
                "NEVER use any of them (or a trivial variation) as its own primary keyword; "
                "that would cannibalize the money page. Writing SUPPORTING informational "
                "content around them (how-to guides, education, comparisons) that links to "
                "the owning page is encouraged — but the article's primary keyword must be "
                "a distinct informational/commercial query:\n"
                + td_reserved_block + "\n"
            )
        td_seed = task_input.get("seed") or {}
        if td_seed.get("keywords"):
            td_seed_lines = "\n".join(
                f"- \"{k.get('keyword', k) if isinstance(k, dict) else k}\""
                + (f" (vol {int(k['volume']):,})" if isinstance(k, dict) and k.get("volume") else "")
                for k in td_seed["keywords"][:15]
            )
            td_user_prompt += (
                f"\nPRIORITY FOCUS — the user explicitly requested topics for the "
                f"'{td_seed.get('family', 'requested')}' keyword family. These "
                f"{td_seed.get('intent', 'informational')} keywords currently have NO "
                f"covering article on this store:\n{td_seed_lines}\n"
                "Every suggestion must target one of these keywords (or a close variant) "
                "as its primary, while still respecting the reserved money-page keyword "
                "rules and avoiding topics already covered above.\n"
            )
        td_user_prompt += (
            f"\nResearch the market with your tools (search_web, get_keyword_opportunities, "
            f"search_store_products, get_store_overview), validate winnability and inventory "
            f"depth for every candidate, then return exactly {td_count} suggestion(s) as JSON."
        )

        td_messages = [
            {"role": "system", "content": get_setting("prompts.topic_discovery", DEFAULTS["prompts.topic_discovery"])},
            {"role": "user", "content": td_user_prompt},
        ]

        td_content = ""
        td_search_calls = 0
        td_nudged = False
        for _ in range(20):
            response = client.chat.completions.create(
                model=td_model,
                messages=td_messages,
                tools=TOPIC_DISCOVERY_TOOL_DEFINITIONS,
                temperature=0.4,
            )
            choice = response.choices[0]
            if choice.message.tool_calls:
                td_messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments or "{}")
                    except (json.JSONDecodeError, TypeError):
                        td_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({"error": "Invalid tool arguments (malformed JSON). Retry with valid JSON."}),
                        })
                        continue
                    fn_args.setdefault("store_id", td_store_id)
                    result = _handle_topic_tool_call(fn_name, fn_args)
                    if fn_name == "search_web" and "error" not in result:
                        td_search_calls += 1
                    td_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result, default=str),
                    })
            else:
                content = choice.message.content or ""
                # Refuse a final answer produced without any real web research —
                # rationales citing "SERP analysis" would be fabricated.
                if td_search_calls == 0 and not td_nudged:
                    td_nudged = True
                    td_messages.append({"role": "assistant", "content": content})
                    td_messages.append({
                        "role": "user",
                        "content": (
                            "STOP - you have not run a single search_web query, so any claims "
                            "about SERPs, competitors, or market demand are unverified. Run real "
                            "search_web queries now: market sweeps for the top categories AND a "
                            "SERP winnability check on each candidate's primary keyword. Then "
                            "return the final JSON with evidence-based rationales."
                        ),
                    })
                    continue
                td_content = content
                break

        td_result = None
        try:
            start = td_content.find("{")
            end = td_content.rfind("}") + 1
            if start >= 0 and end > start:
                td_result = json.loads(td_content[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        if not td_result or not isinstance(td_result.get("suggestions"), list):
            return {"error": "Topic discovery did not return valid suggestions", "raw_response": td_content[:2000]}

        valid_intents = {"Informational", "Navigational", "Commercial", "Transactional"}
        suggestions = []
        for s in td_result.get("suggestions", []):
            if not isinstance(s, dict) or not (s.get("topic") or "").strip():
                continue
            kw = s.get("keywords")
            if isinstance(kw, list):
                kw = {
                    "primary": str(kw[0]) if kw else "",
                    "informational": [],
                    "commercial": [str(k) for k in kw[1:]],
                    "transactional": [],
                    "navigational": [],
                }
            elif not isinstance(kw, dict):
                kw = {"primary": "", "informational": [], "commercial": [], "transactional": [], "navigational": []}
            kw.setdefault("primary", "")
            for group in ("informational", "commercial", "transactional", "navigational"):
                if not isinstance(kw.get(group), list):
                    kw[group] = []
            slug = re.sub(r"[^a-z0-9]+", "-", (s.get("url_slug") or "").lower()).strip("-")
            intent = s.get("search_intent") or "Informational"
            if intent not in valid_intents:
                intent = intent.capitalize() if intent.capitalize() in valid_intents else "Informational"
            suggestions.append({
                "topic": str(s["topic"]).strip()[:500],
                "h1": (s.get("h1") or "").strip()[:500],
                "meta_title": (s.get("meta_title") or "").strip()[:60],
                "meta_description": (s.get("meta_description") or "").strip()[:155],
                "url_slug": slug[:255],
                "keywords": kw,
                "search_intent": intent,
                "opportunity_type": s.get("opportunity_type") if s.get("opportunity_type") in ("quick_win", "foundation") else "quick_win",
                "rationale": (s.get("rationale") or "").strip(),
                "cannibalization_note": (s.get("cannibalization_note") or "").strip(),
                "target_products": s.get("target_products") if isinstance(s.get("target_products"), list) else [],
                "target_collections": s.get("target_collections") if isinstance(s.get("target_collections"), list) else [],
                "target_articles": s.get("target_articles") if isinstance(s.get("target_articles"), list) else [],
            })
        if not suggestions:
            return {"error": "Topic discovery returned no usable suggestions", "raw_response": td_content[:2000]}

        rejected = td_result.get("rejected_topics")
        if not isinstance(rejected, list):
            rejected = []
        return {
            "market_summary": (td_result.get("market_summary") or "").strip(),
            "rejected_topics": [
                {"topic": str(r.get("topic") or ""), "reason": str(r.get("reason") or "")}
                for r in rejected if isinstance(r, dict)
            ],
            "suggestions": suggestions,
        }

    else:
        raise ValueError(f"Unknown task type: {task_type}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    main_result = None
    max_iterations = 10
    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=config["model"],
            messages=messages,
            tools=TOOL_DEFINITIONS,
            temperature=0.3,
        )

        choice = response.choices[0]

        if choice.message.tool_calls:
            messages.append(choice.message)
            for tool_call in choice.message.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments or "{}")
                except (json.JSONDecodeError, TypeError):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps({"error": "Invalid tool arguments (malformed JSON). Retry with valid JSON."}),
                    })
                    continue
                result = _handle_tool_call(fn_name, fn_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    }
                )
        else:
            content = choice.message.content or ""
            try:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    main_result = json.loads(content[start:end])
            except (json.JSONDecodeError, ValueError):
                pass
            if main_result is None:
                main_result = {"raw_response": content}
            break

    if main_result is None:
        main_result = {"error": "Max iterations reached without final response"}

    # A failed main analysis can't be applied — skip the paid description-
    # generation call whose output every apply path would discard anyway.
    if "error" in main_result or ("raw_response" in main_result and len(main_result) == 1):
        return main_result

    if task_type == "product_seo":
        alt_suggestions = main_result.get("image_alt_suggestions", [])
        if alt_suggestions:
            with db.get_cursor() as cur:
                cur.execute("SELECT images FROM synced_products WHERE id = %s", (product_id,))
                row = cur.fetchone()
            images = (row.get("images") if row else None) or []
            if isinstance(images, str):
                images = json.loads(images)

            def _alt_text(a):
                if isinstance(a, dict):
                    return str(a.get("suggested_alt") or a.get("alt") or "")
                return str(a)

            # Pair suggestions to media ids. The model answers positionally
            # against the full images array, so images without a media_id must
            # still consume their slot — but when counts only match the
            # media-bearing subset, pair against that instead of misaligning.
            media_imgs = [img for img in images if img.get("media_id")]
            by_media = {}
            explicit = [a for a in alt_suggestions if isinstance(a, dict) and a.get("media_id")]
            if explicit and len(explicit) == len(alt_suggestions):
                valid_mids = {img["media_id"] for img in media_imgs}
                by_media = {a["media_id"]: _alt_text(a) for a in explicit
                            if a["media_id"] in valid_mids}
            elif len(alt_suggestions) == len(images):
                by_media = {img["media_id"]: _alt_text(alt_suggestions[i])
                            for i, img in enumerate(images) if img.get("media_id")}
            elif len(alt_suggestions) == len(media_imgs):
                by_media = {img["media_id"]: _alt_text(alt_suggestions[i])
                            for i, img in enumerate(media_imgs)}
            else:
                logger.warning(
                    "Alt suggestion count %d matches neither %d images nor %d media images — pairing to min",
                    len(alt_suggestions), len(images), len(media_imgs),
                )
                by_media = {img["media_id"]: _alt_text(alt_suggestions[i])
                            for i, img in enumerate(media_imgs[: len(alt_suggestions)])}
            main_result["image_alt_suggestions"] = [
                {"media_id": mid, "suggested_alt": alt} for mid, alt in by_media.items() if alt
            ]

        _validate_main_result(main_result, task_type, task_input,
                              product_id if task_type == "product_seo" else None,
                              None, client, config, store_id=store_id)
        main_result = _generate_description(client, config, main_result, serp_context, serp_benchmark, product_id, search_context=prefetched_search_ctx)
        if task_input.get("target_keyword"):
            main_result["target_keyword"] = task_input["target_keyword"]

    elif task_type == "collection_seo":
        _validate_main_result(main_result, task_type, task_input,
                              None, collection_id, client, config, store_id=store_id)
        main_result = _generate_collection_description(
            client, config, main_result, serp_context, serp_benchmark, collection_id,
            hierarchy_context=task_input.get("hierarchy_context"),
            search_context=prefetched_search_ctx,
        )
        if task_input.get("target_keyword"):
            main_result["target_keyword"] = task_input["target_keyword"]

    elif task_type == "brand_seo":
        main_result = _generate_brand_collection_description(
            client, config, main_result, serp_context, serp_benchmark,
            collection_id, task_input.get("interlink_collections", []),
            task_input.get("keywords", []),
            search_context=prefetched_search_ctx,
        )

    return main_result
