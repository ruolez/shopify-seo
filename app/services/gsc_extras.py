"""Search Console data added in 2026 (Search Analytics API, Nov 16 2026).

- Brand split: Google labels each query BRANDED or NON_BRANDED
  (`brandedClassification`). Daily totals per label are exact (no anonymized
  queries missing); per-query labels replace our name regex where Google has
  one. Data starts Feb 21 2026, and only for properties with enough volume:
  an ineligible property gets no rows, not an error.
- Web multimodal (`type: webMultimodal`): Google Lens, Circle to Search and
  image uploads. Not part of the default `web` type.
"""
import logging
from datetime import timedelta

import psycopg2.extras

from app.database import db

logger = logging.getLogger(__name__)

ROW_LIMIT = 25000
BRAND_DATA_START = "2026-02-21"
QUERY_LABEL_DAYS = 90


def _query_all(service, site_url, body):
    rows, start = [], 0
    while True:
        batch = service.searchanalytics().query(
            siteUrl=site_url, body={**body, "rowLimit": ROW_LIMIT, "startRow": start}
        ).execute().get("rows", [])
        rows.extend(batch)
        if len(batch) < ROW_LIMIT:
            return rows
        start += ROW_LIMIT


def brand_daily_values(rows):
    """[(date, branded, clicks, impressions, position)] from rows keyed
    [date, brandedClassification]. Unknown labels are skipped. Pure."""
    out = []
    for r in rows:
        keys = r.get("keys") or []
        if len(keys) < 2 or keys[1] not in ("BRANDED", "NON_BRANDED"):
            continue
        out.append((keys[0], keys[1] == "BRANDED", r.get("clicks", 0), r.get("impressions", 0), r.get("position", 0)))
    return out


def query_label_values(rows):
    """{lowercased query: branded} from rows keyed [query, brandedClassification]. Pure."""
    labels = {}
    for r in rows:
        keys = r.get("keys") or []
        if len(keys) < 2 or keys[1] not in ("BRANDED", "NON_BRANDED") or not keys[0]:
            continue
        query = keys[0].strip().lower()
        # A query seen under both labels counts as branded.
        labels[query] = labels.get(query, False) or keys[1] == "BRANDED"
    return labels


def _first_day(store_id, table, default_from, backfill_from):
    with db.get_cursor() as cur:
        cur.execute(f"SELECT 1 FROM {table} WHERE store_id = %s LIMIT 1", (store_id,))
        return default_from if cur.fetchone() else backfill_from


def pull_brand_split(service, site_url, store_id, date_from, date_to, backfill_from):
    date_from = max(_first_day(store_id, "gsc_brand_daily", date_from, backfill_from), BRAND_DATA_START)
    values = brand_daily_values(_query_all(service, site_url, {
        "startDate": date_from, "endDate": date_to, "dimensions": ["date", "brandedClassification"],
    }))
    label_from = max((_as_date(date_to) - timedelta(days=QUERY_LABEL_DAYS)).isoformat(), BRAND_DATA_START)
    labels = query_label_values(_query_all(service, site_url, {
        "startDate": label_from, "endDate": date_to, "dimensions": ["query", "brandedClassification"],
    }))
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM gsc_brand_daily WHERE store_id = %s AND date BETWEEN %s AND %s",
                    (store_id, date_from, date_to))
        if values:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO gsc_brand_daily (store_id, date, branded, clicks, impressions, position) VALUES %s",
                [(store_id, *v) for v in values],
            )
        if labels:
            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO gsc_query_brand (store_id, query, branded, updated_at) VALUES %s
                   ON CONFLICT (store_id, query) DO UPDATE SET branded = EXCLUDED.branded, updated_at = NOW()""",
                [(store_id, q[:500], b) for q, b in labels.items()],
                template="(%s, %s, %s, NOW())",
            )
    return {"days": len({v[0] for v in values}), "labelled_queries": len(labels)}


def pull_multimodal(service, site_url, store_id, date_from, date_to, backfill_from):
    date_from = _first_day(store_id, "gsc_multimodal_daily", date_from, backfill_from)
    rows = _query_all(service, site_url, {
        "startDate": date_from, "endDate": date_to, "type": "webMultimodal", "dimensions": ["date", "page"],
    })
    values = [(store_id, r["keys"][0], r["keys"][1], r.get("clicks", 0), r.get("impressions", 0), r.get("position", 0))
              for r in rows if len(r.get("keys") or []) == 2]
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM gsc_multimodal_daily WHERE store_id = %s AND date BETWEEN %s AND %s",
                    (store_id, date_from, date_to))
        if values:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO gsc_multimodal_daily (store_id, date, page, clicks, impressions, position) VALUES %s",
                values,
            )
    return len(values)


def _as_date(value):
    from datetime import date

    return value if isinstance(value, date) else date.fromisoformat(str(value))


def pull_extras(service, site_url, store_id, date_from, date_to, backfill_from):
    """Both pulls; each failure is recorded as job health, never raised, so
    the main data pull still completes."""
    from app.services.job_health import record_job

    problems, detail = [], []
    for name, fn in (("brand split", pull_brand_split), ("Lens and image uploads", pull_multimodal)):
        try:
            out = fn(service, site_url, store_id, date_from, date_to, backfill_from)
            detail.append(f"{name}: {out}")
        except Exception as e:
            logger.warning("GSC %s pull failed for store %s: %s", name, store_id, e)
            problems.append(f"{name}: {e}")
    record_job("gsc_extras", store_id, ok=not problems, error="; ".join(problems) or None,
               detail="; ".join(detail))
    return problems


def brand_labels(store_id):
    """{lowercased query: branded} as Google labelled them; {} without data."""
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT query, branded FROM gsc_query_brand WHERE store_id = %s", (store_id,))
            return {r["query"]: r["branded"] for r in cur.fetchall()}
    except Exception:
        logger.warning("Brand labels unavailable for store %s", store_id)
        return {}


def search_split(store_id, date_from, date_to, top_pages=10):
    """Branded vs other searches, and Lens / image-upload traffic, for a range.
    Values are None when Google returned nothing for that part."""
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT branded, SUM(clicks) AS clicks, SUM(impressions) AS impressions
               FROM gsc_brand_daily WHERE store_id = %s AND date BETWEEN %s AND %s GROUP BY branded""",
            (store_id, date_from, date_to),
        )
        split = {("branded" if r["branded"] else "non_branded"): {
            "clicks": int(r["clicks"] or 0), "impressions": int(r["impressions"] or 0)} for r in cur.fetchall()}
        cur.execute(
            """SELECT page, SUM(clicks) AS clicks, SUM(impressions) AS impressions,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) AS position
               FROM gsc_multimodal_daily WHERE store_id = %s AND date BETWEEN %s AND %s
               GROUP BY page ORDER BY SUM(clicks) DESC, SUM(impressions) DESC""",
            (store_id, date_from, date_to),
        )
        pages = cur.fetchall()
    multimodal = None
    if pages:
        multimodal = {
            "clicks": int(sum(p["clicks"] or 0 for p in pages)),
            "impressions": int(sum(p["impressions"] or 0 for p in pages)),
            "pages": [{"page": p["page"], "clicks": int(p["clicks"] or 0), "impressions": int(p["impressions"] or 0),
                       "position": round(float(p["position"]), 1) if p["position"] else None}
                      for p in pages[:top_pages]],
        }
    return {"brand": split or None, "multimodal": multimodal}
