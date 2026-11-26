"""Which of the store's pages Google has actually indexed.

A page Google has not indexed cannot rank, however well its text is written —
and no on-page score shows that. This asks Search Console's URL Inspection API
about every product, collection, article and page on a rolling daily budget
(the API allows 2,000 inspections a day per property) and keeps the answer.
"""

import logging
import time

from app.database import db

logger = logging.getLogger(__name__)

DAILY_BUDGET = 300
RECHECK_AFTER_DAYS = 14

_SOURCES = (
    ("product", "synced_products", "'products/' || t.handle", "t.status = 'ACTIVE'"),
    ("collection", "synced_collections", "'collections/' || t.handle", "COALESCE(t.published, TRUE)"),
    ("article", "synced_articles",
     "'blogs/' || (SELECT b.handle FROM synced_blogs b WHERE b.id = t.blog_id) || '/' || t.handle",
     "t.is_published"),
    ("page", "synced_pages", "'pages/' || t.handle", "t.is_published AND NOT COALESCE(t.is_homepage, FALSE)"),
)

_EXPLANATIONS = {
    "Crawled - currently not indexed": (
        "Google read this page and chose not to index it — usually because it adds little that "
        "other pages (yours or competitors') don't already say.",
        "Make the description say something specific to this item, and link to it from related pages.",
    ),
    "Discovered - currently not indexed": (
        "Google knows this page exists but has not read it yet — it has not seemed worth the visit.",
        "Link to it from pages that already get traffic so Google reaches it sooner.",
    ),
    "URL is unknown to Google": (
        "Google has not picked this URL up yet. Shopify lists every published page in the sitemap, "
        "so this is usually crawl priority on a smaller site, not a technical block.",
        "Link to it from pages Google visits often — collections and articles that already get "
        "traffic (the plan above adds those links).",
    ),
    "Not found (404)": (
        "Google gets a 'not found' error for this address.",
        "If the page was removed on purpose, redirect its address to the closest live page.",
    ),
    "Soft 404": (
        "The page loads but looks empty or like an error page to Google.",
        "Give it real content, or redirect it if it is no longer needed.",
    ),
    "Duplicate without user-selected canonical": (
        "Google sees this as a copy of another page and indexed the other one.",
        "Make the content distinct, or accept that the other page represents both.",
    ),
    "Alternate page with proper canonical tag": (
        "This address points to another page as the main version, which is indexed instead. Usually fine.",
        "No action unless this is the page you want people to find.",
    ),
}
_FALLBACK = (
    "Google has not indexed this page.",
    "Open it in Search Console's URL Inspection for the detail.",
)


def explain_state(coverage_state):
    return _EXPLANATIONS.get(coverage_state, _FALLBACK)


def parse_inspection(response):
    status = ((response or {}).get("inspectionResult") or {}).get("indexStatusResult") or {}
    return {
        "indexed": status.get("verdict") == "PASS",
        "coverage_state": status.get("coverageState") or "Unknown",
        "last_crawl_time": status.get("lastCrawlTime"),
        "google_canonical": status.get("googleCanonical"),
    }


def _due_items(store_id, limit):
    """Never-checked items first, then the stalest; skips anything checked
    within RECHECK_AFTER_DAYS."""
    items = []
    with db.get_cursor() as cur:
        for item_type, table, path_sql, where in _SOURCES:
            cur.execute(
                f"""SELECT %s AS item_type, t.id AS item_id, {path_sql} AS path, s.checked_at
                    FROM {table} t
                    LEFT JOIN index_status s
                      ON s.store_id = t.store_id AND s.item_type = %s AND s.item_id = t.id
                    WHERE t.store_id = %s AND {where} AND COALESCE(t.handle, '') <> ''
                      AND (s.checked_at IS NULL OR s.checked_at < NOW() - make_interval(days => %s))""",
                (item_type, item_type, store_id, RECHECK_AFTER_DAYS),
            )
            items.extend(dict(r) for r in cur.fetchall())
    items.sort(key=lambda i: (i["checked_at"] is not None, i["checked_at"] or 0))
    return [i for i in items if i["path"]][:limit]


def prune_deleted(store_id):
    """Drop results for items that no longer exist (or no longer qualify):
    they kept counting as "not indexed" with no title."""
    with db.get_cursor() as cur:
        for item_type, table, _path, where in _SOURCES:
            cur.execute(
                f"""DELETE FROM index_status s
                    WHERE s.store_id = %s AND s.item_type = %s
                      AND NOT EXISTS (SELECT 1 FROM {table} t
                                      WHERE t.id = s.item_id AND t.store_id = s.store_id AND {where})""",
                (store_id, item_type),
            )


def inspect_batch(store_id, limit=DAILY_BUDGET):
    """Inspect up to `limit` due URLs. Returns {"checked": n, "not_indexed": n}."""
    from app.services.gsc_service import _build_service
    from app.services.url_utils import gsc_base_url

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        row = cur.fetchone()
    if not row or not row.get("gsc_property"):
        raise ValueError("Search Console is not connected for this store")
    site_url = row["gsc_property"]
    base = gsc_base_url(site_url).rstrip("/")

    prune_deleted(store_id)
    service = _build_service(store_id)
    checked = not_indexed = 0
    stopped = None
    due = _due_items(store_id, limit)
    for item in due:
        url = f"{base}/{item['path']}"
        try:
            response = service.urlInspection().index().inspect(
                body={"inspectionUrl": url, "siteUrl": site_url}).execute()
        except Exception as e:
            # Quota or auth trouble affects every later call too — stop and
            # pick up where this left off on the next run.
            logger.warning("URL inspection stopped at %s: %s", url, e)
            if checked == 0:
                raise RuntimeError(f"Search Console refused the index check: {e}") from e
            stopped = str(e)[:300]
            break
        parsed = parse_inspection(response)
        with db.get_cursor() as cur:
            cur.execute(
                """INSERT INTO index_status
                     (store_id, item_type, item_id, url, indexed, coverage_state, last_crawl_time,
                      google_canonical, checked_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (store_id, item_type, item_id) DO UPDATE SET
                     url = EXCLUDED.url, indexed = EXCLUDED.indexed,
                     coverage_state = EXCLUDED.coverage_state, last_crawl_time = EXCLUDED.last_crawl_time,
                     google_canonical = EXCLUDED.google_canonical, checked_at = NOW()""",
                (store_id, item["item_type"], item["item_id"], url, parsed["indexed"],
                 parsed["coverage_state"], parsed["last_crawl_time"], parsed["google_canonical"]),
            )
        checked += 1
        not_indexed += 0 if parsed["indexed"] else 1
        time.sleep(0.15)
    return {"checked": checked, "not_indexed": not_indexed, "due": len(due), "stopped_early": stopped}


_TITLE_SQL = """CASE s.item_type
    WHEN 'product' THEN (SELECT title FROM synced_products WHERE id = s.item_id)
    WHEN 'collection' THEN (SELECT title FROM synced_collections WHERE id = s.item_id)
    WHEN 'article' THEN (SELECT title FROM synced_articles WHERE id = s.item_id)
    ELSE (SELECT title FROM synced_pages WHERE id = s.item_id) END"""


def get_coverage(store_id, limit=500):
    """{"summary": {...}, "not_indexed": [...]} from what has been checked so far."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT item_type, COUNT(*) AS checked, COUNT(*) FILTER (WHERE indexed) AS indexed,
                      MAX(checked_at) AS last_checked
               FROM index_status WHERE store_id = %s GROUP BY item_type""",
            (store_id,),
        )
        by_type = {r["item_type"]: dict(r) for r in cur.fetchall()}
        cur.execute(
            f"""SELECT s.item_type, s.item_id, s.url, s.coverage_state, s.last_crawl_time, s.checked_at,
                       {_TITLE_SQL} AS title
                FROM index_status s
                WHERE s.store_id = %s AND NOT s.indexed
                ORDER BY s.item_type, s.coverage_state, s.checked_at DESC LIMIT %s""",
            (store_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["why"], r["action"] = explain_state(r["coverage_state"])
        for key in ("last_crawl_time", "checked_at"):
            r[key] = r[key].isoformat() if r[key] else None
    checked = sum(t["checked"] for t in by_type.values())
    indexed = sum(t["indexed"] for t in by_type.values())
    last = max((t["last_checked"] for t in by_type.values()), default=None)
    return {
        "summary": {
            "checked": checked, "indexed": indexed, "not_indexed": checked - indexed,
            "last_checked": last.isoformat() if last else None,
            "by_type": {k: {"checked": v["checked"], "indexed": v["indexed"]} for k, v in by_type.items()},
        },
        "not_indexed": rows,
    }


SITEMAP_STALE_DAYS = 14


def sitemap_problems(sitemaps, today):
    """What is wrong with the sitemaps Search Console knows, as sentences.
    Pure. sitemaps: entries from the Search Console sitemaps.list API."""
    from datetime import date

    if not sitemaps:
        return ["No sitemap is submitted in Search Console — submit /sitemap.xml so Google can find every page."]
    problems = []
    for s in sitemaps:
        path = s.get("path", "sitemap")
        if int(s.get("errors") or 0):
            problems.append(f"{path} has {s['errors']} error(s) in Search Console.")
        downloaded = (s.get("lastDownloaded") or "")[:10]
        if not downloaded:
            problems.append(f"Google has never downloaded {path}.")
        elif (today - date.fromisoformat(downloaded)).days > SITEMAP_STALE_DAYS:
            problems.append(f"Google last downloaded {path} on {downloaded} — more than {SITEMAP_STALE_DAYS} days ago.")
    return problems


def sitemap_status(store_id):
    """Sitemaps as Search Console reports them, plus any problems."""
    from datetime import datetime, timezone

    from app.services.gsc_service import _build_service

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        row = cur.fetchone()
    if not row or not row.get("gsc_property"):
        raise ValueError("Search Console is not connected for this store")
    listing = _build_service(store_id).sitemaps().list(siteUrl=row["gsc_property"]).execute()
    sitemaps = listing.get("sitemap", [])
    today = datetime.now(timezone.utc).date()
    return {
        "sitemaps": [{
            "path": s.get("path"),
            "last_downloaded": s.get("lastDownloaded"),
            "submitted": sum(int(c.get("submitted") or 0) for c in s.get("contents", []) if c.get("type") == "web"),
            "errors": int(s.get("errors") or 0),
            "warnings": int(s.get("warnings") or 0),
        } for s in sitemaps],
        "problems": sitemap_problems(sitemaps, today),
    }


def check_sitemaps(store_id):
    """Daily job: raise when the sitemap has a problem, so Home shows it."""
    status = sitemap_status(store_id)
    if status["problems"]:
        raise RuntimeError(" ".join(status["problems"]))
    return f"{sum(s['submitted'] for s in status['sitemaps'])} URLs in sitemap"
