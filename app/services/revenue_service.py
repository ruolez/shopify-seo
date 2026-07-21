"""Organic revenue attribution from Shopify order journeys.

Uses Shopify's customerJourneySummary (last non-direct visit approximation:
lastVisit, else firstVisit) — no Google Analytics needed. Honest limits:
Shopify's attribution window is ~30 days, the journey is computed
asynchronously (null on some orders/channels — kept as 'unknown', never
dropped), and totals will undercount versus GA.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app.auth import decrypt_value
from app.database import db
from app.services.url_utils import url_path_key
from app.settings_manager import get_setting, set_setting
from app.shopify_client import ShopifyClient

logger = logging.getLogger(__name__)

BACKFILL_DAYS = 90
PULL_OVERLAP_DAYS = 2
MAX_PAGES_PER_PULL = 40

SEARCH_ENGINE_HOSTS = (
    "google.", "bing.", "duckduckgo.", "yahoo.", "ecosia.", "search.brave.com",
    "baidu.", "yandex.", "startpage.com", "qwant.com",
)
AI_HOSTS = (
    "chatgpt.com", "chat.openai.com", "perplexity.ai", "copilot.microsoft.com",
    "gemini.google.com", "claude.ai", "you.com",
)
PAID_MEDIUMS = {"cpc", "ppc", "paid", "paid_search", "paidsearch", "cpm", "display"}


def classify_visit(visit):
    if not visit:
        return "unknown"
    utm = visit.get("utmParameters") or {}
    medium = (utm.get("medium") or "").lower()
    if medium in PAID_MEDIUMS:
        return "paid"

    ref = (visit.get("referrerUrl") or "").strip()
    host = urlparse(ref).netloc.lower() if ref else ""
    if host:
        if any(a in host for a in AI_HOSTS):
            return "ai"
        if any(host.startswith(s) or f".{s}" in f".{host}" for s in SEARCH_ENGINE_HOSTS):
            return "organic"

    source_type = (visit.get("sourceType") or "").lower()
    source = (visit.get("source") or "").lower()
    if source_type == "search" or source in ("google", "bing", "duckduckgo", "yahoo"):
        return "organic"
    if host:
        return "referral"
    if source in ("", "direct") and not medium:
        return "direct"
    return "unknown"


def _cursor_key(store_id):
    return f"revenue_last_pull_{store_id}"


def _error_key(store_id):
    return f"revenue_pull_error_{store_id}"


def pull_orders(store_id):
    """Incremental order pull. Returns dict with pulled count or error."""
    with db.get_cursor() as cur:
        cur.execute("SELECT domain, access_token_enc, api_version FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return {"error": "store not found"}

    last_seen = get_setting(_cursor_key(store_id))
    if last_seen:
        since = (datetime.fromisoformat(last_seen) - timedelta(days=PULL_OVERLAP_DAYS)).date()
    else:
        since = datetime.now(timezone.utc).date() - timedelta(days=BACKFILL_DAYS)
    query_filter = f"created_at:>={since.isoformat()}"

    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    pulled = 0
    max_created = last_seen
    cursor = None
    try:
        for _ in range(MAX_PAGES_PER_PULL):
            page = client.get_orders(query_filter=query_filter, cursor=cursor)
            nodes = page.get("nodes") or []
            for order in nodes:
                if order.get("test"):
                    continue
                _upsert_order(store_id, order)
                pulled += 1
                if not max_created or order["createdAt"] > max_created:
                    max_created = order["createdAt"]
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
    except Exception as e:
        # Most common cause: token missing the read_orders scope.
        logger.warning("Order pull failed for store %s: %s", store_id, e)
        set_setting(_error_key(store_id), str(e))
        return {"error": str(e), "pulled": pulled}

    if max_created:
        set_setting(_cursor_key(store_id), max_created)
    set_setting(_error_key(store_id), "")
    logger.info("Order pull for store %s: %d orders since %s", store_id, pulled, since)
    return {"pulled": pulled}


def _upsert_order(store_id, order):
    journey = order.get("customerJourneySummary") or {}
    visit = journey.get("lastVisit") or journey.get("firstVisit")
    attribution = classify_visit(visit)
    landing = (visit or {}).get("landingPage")
    money = ((order.get("currentTotalPriceSet") or {}).get("shopMoney")) or {}

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO organic_orders (
                   store_id, order_gid, order_name, created_at, total_price, currency,
                   attribution, landing_page, landing_path, referrer_url, source, source_type, journey, pulled_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (store_id, order_gid) DO UPDATE SET
                   total_price = EXCLUDED.total_price,
                   attribution = EXCLUDED.attribution,
                   landing_page = EXCLUDED.landing_page,
                   landing_path = EXCLUDED.landing_path,
                   referrer_url = EXCLUDED.referrer_url,
                   source = EXCLUDED.source,
                   source_type = EXCLUDED.source_type,
                   journey = EXCLUDED.journey,
                   pulled_at = NOW()""",
            (
                store_id, order["id"], order.get("name"), order["createdAt"],
                float(money.get("amount") or 0), money.get("currencyCode"),
                attribution, landing, url_path_key(landing)[:500] if landing else None,
                (visit or {}).get("referrerUrl"), (visit or {}).get("source"),
                (visit or {}).get("sourceType"), json.dumps(journey),
            ),
        )


def get_revenue(store_id, days=28):
    now = datetime.now(timezone.utc)
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=days * 2)

    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM organic_orders WHERE store_id = %s", (store_id,))
        have_data = int(cur.fetchone()["c"] or 0) > 0

        if not have_data:
            err = get_setting(_error_key(store_id)) or ""
            scope_missing = "access" in err.lower() or "scope" in err.lower()
            return {"available": False, "scope_missing": scope_missing, "error": err or None}

        cur.execute(
            """SELECT COALESCE(SUM(total_price) FILTER (WHERE created_at >= %(cs)s), 0) AS cur_rev,
                      COUNT(*) FILTER (WHERE created_at >= %(cs)s) AS cur_orders,
                      COALESCE(SUM(total_price) FILTER (WHERE created_at < %(cs)s), 0) AS prev_rev
               FROM organic_orders
               WHERE store_id = %(sid)s AND attribution = 'organic' AND created_at >= %(ps)s""",
            {"sid": store_id, "cs": cur_start.replace(tzinfo=None), "ps": prev_start.replace(tzinfo=None)},
        )
        totals = cur.fetchone()

        cur.execute(
            """SELECT created_at::date AS date,
                      SUM(total_price) FILTER (WHERE attribution = 'organic') AS revenue,
                      COUNT(*) FILTER (WHERE attribution = 'organic') AS orders
               FROM organic_orders
               WHERE store_id = %s AND created_at >= %s
               GROUP BY created_at::date ORDER BY date""",
            (store_id, cur_start.replace(tzinfo=None)),
        )
        trend = cur.fetchall()

        cur.execute(
            """SELECT landing_path, SUM(total_price) AS revenue, COUNT(*) AS orders
               FROM organic_orders
               WHERE store_id = %s AND attribution = 'organic' AND created_at >= %s
                 AND landing_path IS NOT NULL
               GROUP BY landing_path ORDER BY revenue DESC LIMIT 8""",
            (store_id, cur_start.replace(tzinfo=None)),
        )
        top_pages = cur.fetchall()

        cur.execute(
            """SELECT attribution, SUM(total_price) AS revenue, COUNT(*) AS orders
               FROM organic_orders WHERE store_id = %s AND created_at >= %s
               GROUP BY attribution""",
            (store_id, cur_start.replace(tzinfo=None)),
        )
        split = {r["attribution"]: {"revenue": float(r["revenue"] or 0), "orders": int(r["orders"])}
                 for r in cur.fetchall()}

        titles = {}
        handles = {"products": [], "collections": []}
        for p in top_pages:
            parts = (p["landing_path"] or "").split("/")
            if len(parts) == 2 and parts[0] in handles:
                handles[parts[0]].append(parts[1])
        for prefix, table in (("products", "synced_products"), ("collections", "synced_collections")):
            if handles[prefix]:
                cur.execute(
                    f"SELECT handle, title FROM {table} WHERE store_id = %s AND handle = ANY(%s)",
                    (store_id, handles[prefix]),
                )
                for r in cur.fetchall():
                    titles[f"{prefix}/{r['handle']}"] = r["title"]

    cur_rev = float(totals["cur_rev"] or 0)
    prev_rev = float(totals["prev_rev"] or 0)
    cur_orders = int(totals["cur_orders"] or 0)

    return {
        "available": True,
        "scope_missing": False,
        "organic_revenue": round(cur_rev, 2),
        "organic_orders": cur_orders,
        "aov": round(cur_rev / cur_orders, 2) if cur_orders else None,
        "pct_change": round((cur_rev - prev_rev) / prev_rev * 100, 1) if prev_rev else None,
        "trend": [
            {"date": r["date"].isoformat(), "revenue": round(float(r["revenue"] or 0), 2),
             "orders": int(r["orders"] or 0)}
            for r in trend
        ],
        "top_pages": [
            {"landing_path": p["landing_path"], "title": titles.get(p["landing_path"]),
             "revenue": round(float(p["revenue"] or 0), 2), "orders": int(p["orders"])}
            for p in top_pages
        ],
        "attribution_split": split,
    }
