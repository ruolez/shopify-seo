import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.auth import decrypt_value, encrypt_value
from app.database import db
from app.services.gsc_queries import is_junk_query
from app.services import gsc_page_totals as page_totals

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GSC_API_SERVICE = "searchconsole"
GSC_API_VERSION = "v1"
MAX_ROWS_PER_REQUEST = 25000
BACKFILL_DAYS = 90
INCREMENTAL_DAYS = 5
# Final rows replace stored rows this many days back: the window earlier pulls
# may have filled with preliminary (fresh) data.
FRESH_DAYS_KEPT = INCREMENTAL_DAYS

_pull_scheduler = None
_snapshot_scheduler = None


def _build_filters(store_id, date_from, date_to, device=None, country=None):
    clauses = ["store_id = %s", "date >= %s", "date <= %s"]
    params = [store_id, date_from, date_to]
    if device:
        clauses.append("device = %s")
        params.append(device)
    if country:
        clauses.append("country = %s")
        params.append(country)
    return " AND ".join(clauses), params


def _get_gsc_client_config(cur):
    cur.execute("SELECT gsc_client_id_enc, gsc_client_secret_enc FROM ai_config ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if not row or not row.get("gsc_client_id_enc") or not row.get("gsc_client_secret_enc"):
        return None
    return {
        "web": {
            "client_id": decrypt_value(row["gsc_client_id_enc"]),
            "client_secret": decrypt_value(row["gsc_client_secret_enc"]),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def start_oauth_flow(redirect_uri):
    with db.get_cursor() as cur:
        client_config = _get_gsc_client_config(cur)
    if not client_config:
        raise ValueError("GSC client ID and secret not configured in settings")

    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url, state


def complete_oauth_flow(code, state, redirect_uri):
    with db.get_cursor() as cur:
        client_config = _get_gsc_client_config(cur)
    if not client_config:
        raise ValueError("GSC client ID and secret not configured")

    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri, state=state)
    flow.fetch_token(code=code)
    credentials = flow.credentials

    return {
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None,
    }


def _build_service(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT access_token_enc, refresh_token_enc, token_expiry FROM gsc_credentials WHERE store_id = %s",
            (store_id,),
        )
        cred_row = cur.fetchone()
        if not cred_row:
            raise ValueError("No GSC credentials for this store")

        client_config = _get_gsc_client_config(cur)
        if not client_config:
            raise ValueError("GSC client config not set")

    access_token = decrypt_value(cred_row["access_token_enc"])
    refresh_token = decrypt_value(cred_row["refresh_token_enc"])
    client_id = client_config["web"]["client_id"]
    client_secret = client_config["web"]["client_secret"]

    # google-auth expects a naive UTC datetime; without it creds.expired is
    # always False and the proactive refresh below never runs.
    token_expiry = cred_row.get("token_expiry")
    if token_expiry is not None and token_expiry.tzinfo is not None:
        token_expiry = token_expiry.astimezone(timezone.utc).replace(tzinfo=None)

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
        expiry=token_expiry,
    )

    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with db.get_cursor() as cur:
            cur.execute(
                """UPDATE gsc_credentials
                   SET access_token_enc = %s, token_expiry = %s, updated_at = NOW()
                   WHERE store_id = %s""",
                (encrypt_value(creds.token), creds.expiry, store_id),
            )

    return build(GSC_API_SERVICE, GSC_API_VERSION, credentials=creds)


def list_properties(store_id):
    service = _build_service(store_id)
    site_list = service.sites().list().execute()
    sites = site_list.get("siteEntry", [])
    return [
        {"site_url": s["siteUrl"], "permission_level": s["permissionLevel"]}
        for s in sites
    ]


def save_credentials(store_id, token_data, email=None):
    access_enc = encrypt_value(token_data["access_token"])
    refresh_enc = encrypt_value(token_data["refresh_token"])
    expiry = token_data.get("token_expiry")

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO gsc_credentials (store_id, access_token_enc, refresh_token_enc, token_expiry, connected_email)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (store_id) DO UPDATE
               SET access_token_enc = EXCLUDED.access_token_enc,
                   refresh_token_enc = EXCLUDED.refresh_token_enc,
                   token_expiry = EXCLUDED.token_expiry,
                   connected_email = EXCLUDED.connected_email,
                   updated_at = NOW()""",
            (store_id, access_enc, refresh_enc, expiry, email),
        )


def select_property(store_id, site_url):
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE gsc_credentials SET gsc_property = %s, updated_at = NOW() WHERE store_id = %s",
            (site_url, store_id),
        )


def disconnect(store_id):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cur.execute("DELETE FROM gsc_daily_metrics WHERE store_id = %s", (store_id,))
        cur.execute("DELETE FROM gsc_data_pulls WHERE store_id = %s", (store_id,))


def get_connection_status(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT gsc_property, connected_email, created_at, updated_at FROM gsc_credentials WHERE store_id = %s",
            (store_id,),
        )
        cred = cur.fetchone()
        if not cred:
            return {"connected": False}

        cur.execute(
            """SELECT completed_at, rows_pulled, status
               FROM gsc_data_pulls WHERE store_id = %s
               ORDER BY completed_at DESC NULLS LAST LIMIT 1""",
            (store_id,),
        )
        last_pull = cur.fetchone()

        cur.execute(
            "SELECT MIN(date) as first_date, MAX(date) as last_date, COUNT(*) as total_rows FROM gsc_daily_metrics WHERE store_id = %s",
            (store_id,),
        )
        stats = cur.fetchone()

    return {
        "connected": True,
        "property": cred["gsc_property"],
        "email": cred["connected_email"],
        "connected_at": cred["created_at"].isoformat() if cred["created_at"] else None,
        "last_pull": {
            "completed_at": last_pull["completed_at"].isoformat() if last_pull and last_pull["completed_at"] else None,
            "rows_pulled": last_pull["rows_pulled"] if last_pull else 0,
            "status": last_pull["status"] if last_pull else None,
        } if last_pull else None,
        "data_range": {
            "first_date": stats["first_date"].isoformat() if stats and stats["first_date"] else None,
            "last_date": stats["last_date"].isoformat() if stats and stats["last_date"] else None,
            "total_rows": stats["total_rows"] if stats else 0,
        },
    }


def _fetch_gsc_data(service, site_url, date_from, date_to, start_row=0, data_state="final"):
    body = {
        "startDate": date_from,
        "endDate": date_to,
        "dimensions": ["date", "page", "query", "device", "country"],
        "rowLimit": MAX_ROWS_PER_REQUEST,
        "startRow": start_row,
        "dataState": data_state,
    }
    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    return response.get("rows", [])


def pull_data(store_id, date_from=None, date_to=None, pull_type="incremental"):
    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
        if not cred or not cred["gsc_property"]:
            raise ValueError("No GSC property selected")

    site_url = cred["gsc_property"]
    today = datetime.now(timezone.utc).date()

    if not date_from:
        if pull_type == "backfill":
            date_from = (today - timedelta(days=BACKFILL_DAYS)).isoformat()
        else:
            date_from = (today - timedelta(days=INCREMENTAL_DAYS)).isoformat()
    if not date_to:
        date_to = (today - timedelta(days=3)).isoformat()

    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO gsc_data_pulls (store_id, pull_type, date_from, date_to, status, started_at)
               VALUES (%s, %s, %s, %s, 'running', NOW()) RETURNING id""",
            (store_id, pull_type, date_from, date_to),
        )
        pull_id = cur.fetchone()["id"]

    try:
        service = _build_service(store_id)
        total_rows = 0
        dropped_queries = 0
        start_row = 0
        # Days that an earlier pull stored as fresh (preliminary) data. Final
        # rows replace them; combos missing from the final data must not linger.
        prelim_from = max(date_from, (today - timedelta(days=FRESH_DAYS_KEPT)).isoformat())
        final_window_cleared = False

        while True:
            rows = _fetch_gsc_data(service, site_url, date_from, date_to, start_row)
            if not rows:
                break
            if not final_window_cleared and prelim_from <= date_to:
                with db.get_cursor() as cur:
                    cur.execute(
                        "DELETE FROM gsc_daily_metrics WHERE store_id = %s AND date >= %s AND date <= %s",
                        (store_id, prelim_from, date_to),
                    )
                final_window_cleared = True

            with db.get_cursor() as cur:
                for row in rows:
                    keys = row.get("keys", [])
                    if len(keys) < 5:
                        continue
                    date_val, page, query, device, country = keys
                    if is_junk_query(query):
                        dropped_queries += 1
                        continue
                    cur.execute(
                        """INSERT INTO gsc_daily_metrics
                           (store_id, date, page, query, clicks, impressions, ctr, position, device, country)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (store_id, date, page, query, device, country)
                           DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                                         ctr = EXCLUDED.ctr, position = EXCLUDED.position""",
                        (store_id, date_val, page, query,
                         row.get("clicks", 0), row.get("impressions", 0),
                         row.get("ctr", 0), row.get("position", 0),
                         device, country),
                    )

            total_rows += len(rows)
            if len(rows) < MAX_ROWS_PER_REQUEST:
                break
            start_row += MAX_ROWS_PER_REQUEST
            time.sleep(0.5)

        fresh_from = (today - timedelta(days=2)).isoformat()
        fresh_to = today.isoformat()
        fresh_start = 0
        fresh_window_cleared = False
        while True:
            fresh_rows = _fetch_gsc_data(service, site_url, fresh_from, fresh_to, fresh_start, data_state="all")
            if not fresh_rows:
                break
            if not fresh_window_cleared:
                # Replace the previous fresh snapshot — page/query combos that
                # drop out of fresh data otherwise linger forever.
                with db.get_cursor() as cur:
                    cur.execute(
                        "DELETE FROM gsc_daily_metrics WHERE store_id = %s AND date >= %s AND date <= %s",
                        (store_id, fresh_from, fresh_to),
                    )
                fresh_window_cleared = True
            with db.get_cursor() as cur:
                for row in fresh_rows:
                    keys = row.get("keys", [])
                    if len(keys) < 5:
                        continue
                    date_val, page, query, device, country = keys
                    if is_junk_query(query):
                        dropped_queries += 1
                        continue
                    cur.execute(
                        """INSERT INTO gsc_daily_metrics
                           (store_id, date, page, query, clicks, impressions, ctr, position, device, country)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (store_id, date, page, query, device, country)
                           DO UPDATE SET clicks = EXCLUDED.clicks, impressions = EXCLUDED.impressions,
                                         ctr = EXCLUDED.ctr, position = EXCLUDED.position""",
                        (store_id, date_val, page, query,
                         row.get("clicks", 0), row.get("impressions", 0),
                         row.get("ctr", 0), row.get("position", 0),
                         device, country),
                    )
            total_rows += len(fresh_rows)
            if len(fresh_rows) < MAX_ROWS_PER_REQUEST:
                break
            fresh_start += MAX_ROWS_PER_REQUEST
            time.sleep(0.5)

        # Page totals without the query dimension — the only figures that
        # match what Search Console shows per URL (see gsc_page_totals).
        page_from = page_totals.backfill_days_if_empty(store_id, today, date_from, BACKFILL_DAYS)
        total_rows += page_totals.store_page_rows(
            store_id, page_from, date_to,
            page_totals.fetch_page_rows(service, site_url, page_from, date_to, "final"), is_fresh=False,
        )
        total_rows += page_totals.store_page_rows(
            store_id, fresh_from, fresh_to,
            page_totals.fetch_page_rows(service, site_url, fresh_from, fresh_to, "all"), is_fresh=True,
        )

        from app.services.gsc_extras import pull_extras
        extras_problems = pull_extras(service, site_url, store_id, date_from, date_to,
                                      (today - timedelta(days=BACKFILL_DAYS)).isoformat())

        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE gsc_data_pulls SET status = 'completed', rows_pulled = %s, completed_at = NOW() WHERE id = %s",
                (total_rows, pull_id),
            )

        if dropped_queries:
            logger.info("GSC pull skipped %d AI-prompt rows that are not search keywords", dropped_queries)
        logger.info("GSC pull completed: %d rows (incl. fresh) for store %d", total_rows, store_id)
        return {"status": "completed", "rows_pulled": total_rows, "warnings": extras_problems}

    except Exception as e:
        logger.error("GSC pull failed for store %d: %s", store_id, str(e))
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE gsc_data_pulls SET status = 'failed', error_message = %s, completed_at = NOW() WHERE id = %s",
                (str(e), pull_id),
            )
        raise


def _fetch_gsc_totals(service, site_url, date_from, date_to, device=None, country=None):
    body = {
        "startDate": date_from,
        "endDate": date_to,
        "dataState": "all",
    }
    dim_filters = []
    if device:
        dim_filters.append({"dimension": "device", "expression": device})
    if country:
        dim_filters.append({"dimension": "country", "expression": country})
    if dim_filters:
        body["dimensionFilterGroups"] = [{"filters": dim_filters}]

    response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    rows = response.get("rows", [])
    if rows:
        r = rows[0]
        return {
            "total_clicks": float(r.get("clicks", 0)),
            "total_impressions": float(r.get("impressions", 0)),
            "avg_ctr": float(r.get("ctr", 0)),
            "avg_position": round(float(r.get("position", 0)), 1),
        }
    return {"total_clicks": 0, "total_impressions": 0, "avg_ctr": 0, "avg_position": 0}


def _get_overview_from_db(store_id, date_from, date_to, device=None, country=None):
    where, params = _build_filters(store_id, date_from, date_to, device, country)
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT COALESCE(SUM(clicks), 0) as total_clicks,
                      COALESCE(SUM(impressions), 0) as total_impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                      CASE WHEN COUNT(*) > 0 THEN SUM(position * impressions) / NULLIF(SUM(impressions), 0) ELSE 0 END as avg_position
               FROM gsc_daily_metrics
               WHERE {where}""",
            params,
        )
        row = cur.fetchone()
    return {
        "total_clicks": float(row["total_clicks"] or 0),
        "total_impressions": float(row["total_impressions"] or 0),
        "avg_ctr": float(row["avg_ctr"] or 0),
        "avg_position": round(float(row["avg_position"] or 0), 1),
    }


def get_overview(store_id, date_from, date_to, compare=True, device=None, country=None):
    try:
        service = _build_service(store_id)
        with db.get_cursor() as cur:
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
            cred = cur.fetchone()
        site_url = cred["gsc_property"] if cred else None

        if service and site_url:
            current = _fetch_gsc_totals(service, site_url, date_from, date_to, device, country)
            current_is_live = True
        else:
            current = _get_overview_from_db(store_id, date_from, date_to, device, country)
            current_is_live = False
    except Exception:
        current = _get_overview_from_db(store_id, date_from, date_to, device, country)
        current_is_live = False

    previous = None
    if compare:
        days_range = (datetime.fromisoformat(date_to) - datetime.fromisoformat(date_from)).days + 1
        prev_to = (datetime.fromisoformat(date_from) - timedelta(days=1)).date().isoformat()
        prev_from = (datetime.fromisoformat(date_from) - timedelta(days=days_range)).date().isoformat()
        # Compare like with like — mixing a live current against DB-only
        # previous (different data states) skewed every delta percentage.
        if current_is_live:
            try:
                previous = _fetch_gsc_totals(service, site_url, prev_from, prev_to, device, country)
            except Exception:
                previous = _get_overview_from_db(store_id, prev_from, prev_to, device, country)
        else:
            previous = _get_overview_from_db(store_id, prev_from, prev_to, device, country)

    result = {
        "total_clicks": float(current["total_clicks"] or 0),
        "total_impressions": float(current["total_impressions"] or 0),
        "avg_ctr": float(current["avg_ctr"] or 0),
        "avg_position": round(float(current["avg_position"] or 0), 1),
    }

    if previous:
        prev_clicks = float(previous["total_clicks"] or 0)
        prev_impressions = float(previous["total_impressions"] or 0)
        prev_ctr = float(previous["avg_ctr"] or 0)
        prev_position = float(previous["avg_position"] or 0)

        has_prev_data = prev_clicks > 0 or prev_impressions > 0
        if has_prev_data:
            result["changes"] = {
                "clicks": result["total_clicks"] - prev_clicks,
                "clicks_pct": ((result["total_clicks"] - prev_clicks) / prev_clicks * 100) if prev_clicks else None,
                "impressions": result["total_impressions"] - prev_impressions,
                "impressions_pct": ((result["total_impressions"] - prev_impressions) / prev_impressions * 100) if prev_impressions else None,
                "ctr": result["avg_ctr"] - prev_ctr,
                "position": result["avg_position"] - prev_position,
            }

    return result


def _fetch_gsc_by_dimension(store_id, date_from, date_to, dimension, key_name, device=None, country=None, limit=500):
    try:
        service = _build_service(store_id)
        with db.get_cursor() as cur:
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
            cred = cur.fetchone()
        site_url = cred["gsc_property"] if cred else None
        if not service or not site_url:
            raise ValueError("No GSC connection")

        body = {
            "startDate": date_from,
            "endDate": date_to,
            "dimensions": [dimension],
            "rowLimit": min(limit, MAX_ROWS_PER_REQUEST),
            "dataState": "all",
        }
        dim_filters = []
        if device:
            dim_filters.append({"dimension": "device", "expression": device})
        if country:
            dim_filters.append({"dimension": "country", "expression": country})
        if dim_filters:
            body["dimensionFilterGroups"] = [{"filters": dim_filters}]

        all_rows = []
        start = 0
        while True:
            body["startRow"] = start
            response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
            rows = response.get("rows", [])
            all_rows.extend(rows)
            # Stop once the caller's requested row count is satisfied — paging
            # through everything burned API quota and ignored the limit.
            if len(all_rows) >= limit or len(rows) < body["rowLimit"]:
                break
            start += len(rows)

        all_rows = all_rows[:limit]
        return [
            {
                key_name: r["keys"][0],
                "clicks": float(r.get("clicks", 0)),
                "impressions": float(r.get("impressions", 0)),
                "ctr": round(float(r.get("ctr", 0)), 4),
                "position": round(float(r.get("position", 0)), 1),
            }
            for r in all_rows
        ]
    except Exception as e:
        logger.warning("Live %s fetch failed, falling back to DB: %s", dimension, str(e))
        return None


def get_top_pages(store_id, date_from, date_to, limit=500, offset=0, sort_by="clicks", device=None, country=None):
    live = _fetch_gsc_by_dimension(store_id, date_from, date_to, "page", "page", device, country, limit + offset)
    if live is not None:
        # GSC returns click-sorted rows — honor sort_by like the DB path does,
        # and sort before slicing so pagination stays consistent.
        live.sort(key=lambda r: r["clicks"] if sort_by == "clicks" else r["impressions"], reverse=True)
        return live[offset:offset + limit]

    order = "total_clicks DESC" if sort_by == "clicks" else "total_impressions DESC"
    where, params = _build_filters(store_id, date_from, date_to, device, country)
    params.extend([limit, offset])
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT page,
                       SUM(clicks) as total_clicks,
                       SUM(impressions) as total_impressions,
                       CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                       SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
                FROM gsc_daily_metrics
                WHERE {where}
                GROUP BY page
                ORDER BY {order}
                LIMIT %s OFFSET %s""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "page": r["page"],
            "clicks": float(r["total_clicks"] or 0),
            "impressions": float(r["total_impressions"] or 0),
            "ctr": round(float(r["avg_ctr"] or 0), 4),
            "position": round(float(r["avg_position"] or 0), 1),
        }
        for r in rows
    ]


def get_top_queries(store_id, date_from, date_to, limit=500, offset=0, sort_by="clicks", device=None, country=None):
    live = _fetch_gsc_by_dimension(store_id, date_from, date_to, "query", "query", device, country, limit + offset)
    if live is not None:
        live.sort(key=lambda r: r["clicks"] if sort_by == "clicks" else r["impressions"], reverse=True)
        return live[offset:offset + limit]

    order = "total_clicks DESC" if sort_by == "clicks" else "total_impressions DESC"
    where, params = _build_filters(store_id, date_from, date_to, device, country)
    params.extend([limit, offset])
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT query,
                       SUM(clicks) as total_clicks,
                       SUM(impressions) as total_impressions,
                       CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                       SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
                FROM gsc_daily_metrics
                WHERE {where}
                GROUP BY query
                ORDER BY {order}
                LIMIT %s OFFSET %s""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "query": r["query"],
            "clicks": float(r["total_clicks"] or 0),
            "impressions": float(r["total_impressions"] or 0),
            "ctr": round(float(r["avg_ctr"] or 0), 4),
            "position": round(float(r["avg_position"] or 0), 1),
        }
        for r in rows
    ]


def _fetch_gsc_filtered(store_id, date_from, date_to, dimension, key_name, filter_dim, filter_val, device=None, country=None, limit=200):
    try:
        service = _build_service(store_id)
        with db.get_cursor() as cur:
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
            cred = cur.fetchone()
        site_url = cred["gsc_property"] if cred else None
        if not service or not site_url:
            raise ValueError("No GSC connection")

        dim_filters = [{"dimension": filter_dim, "operator": "equals", "expression": filter_val}]
        if device:
            dim_filters.append({"dimension": "device", "expression": device})
        if country:
            dim_filters.append({"dimension": "country", "expression": country})

        body = {
            "startDate": date_from,
            "endDate": date_to,
            "dimensions": [dimension],
            "rowLimit": min(limit, MAX_ROWS_PER_REQUEST),
            "dataState": "all",
            "dimensionFilterGroups": [{"filters": dim_filters}],
        }
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return [
            {
                key_name: r["keys"][0],
                "clicks": float(r.get("clicks", 0)),
                "impressions": float(r.get("impressions", 0)),
                "ctr": round(float(r.get("ctr", 0)), 4),
                "position": round(float(r.get("position", 0)), 1),
            }
            for r in response.get("rows", [])
        ]
    except Exception as e:
        logger.warning("Live filtered %s fetch failed, falling back to DB: %s", dimension, str(e))
        return None


def get_page_detail(store_id, page_url, date_from, date_to, limit=200, device=None, country=None):
    live = _fetch_gsc_filtered(store_id, date_from, date_to, "query", "query", "page", page_url, device, country, limit)
    if live is not None:
        return live

    where, params = _build_filters(store_id, date_from, date_to, device, country)
    params.extend([page_url, limit])
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT query,
                      SUM(clicks) as total_clicks,
                      SUM(impressions) as total_impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
               FROM gsc_daily_metrics
               WHERE {where} AND page = %s
               GROUP BY query
               ORDER BY total_clicks DESC
               LIMIT %s""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "query": r["query"],
            "clicks": float(r["total_clicks"] or 0),
            "impressions": float(r["total_impressions"] or 0),
            "ctr": round(float(r["avg_ctr"] or 0), 4),
            "position": round(float(r["avg_position"] or 0), 1),
        }
        for r in rows
    ]


def get_query_detail(store_id, query_text, date_from, date_to, limit=200, device=None, country=None):
    live = _fetch_gsc_filtered(store_id, date_from, date_to, "page", "page", "query", query_text, device, country, limit)
    if live is not None:
        return live

    where, params = _build_filters(store_id, date_from, date_to, device, country)
    params.extend([query_text, limit])
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT page,
                      SUM(clicks) as total_clicks,
                      SUM(impressions) as total_impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
               FROM gsc_daily_metrics
               WHERE {where} AND query = %s
               GROUP BY page
               ORDER BY total_clicks DESC
               LIMIT %s""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "page": r["page"],
            "clicks": float(r["total_clicks"] or 0),
            "impressions": float(r["total_impressions"] or 0),
            "ctr": round(float(r["avg_ctr"] or 0), 4),
            "position": round(float(r["avg_position"] or 0), 1),
        }
        for r in rows
    ]


def get_opportunities(store_id, date_from, date_to, limit=200, device=None, country=None):
    """CTR opportunities — position-aware: a query underperforms when its CTR
    is below half the expected CTR for its position (store-calibrated curve),
    not below a flat threshold that ignores where it ranks."""
    from app.services.ctr_benchmarks import (
        expected_ctr_for_position,
        get_expected_ctr_curve,
        is_ctr_underperformer,
    )

    curve = get_expected_ctr_curve(store_id)
    where, params = _build_filters(store_id, date_from, date_to, device, country)
    params.append(min(limit * 5, 2000))
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT page, query,
                      SUM(clicks) as total_clicks,
                      SUM(impressions) as total_impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                      SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
               FROM gsc_daily_metrics
               WHERE {where}
               GROUP BY page, query
               HAVING SUM(impressions) >= 10
               ORDER BY SUM(impressions) DESC, avg_ctr ASC
               LIMIT %s""",
            params,
        )
        all_rows = cur.fetchall()

    results = []
    for r in all_rows:
        ctr = float(r["avg_ctr"] or 0)
        impressions = float(r["total_impressions"] or 0)
        position = float(r["avg_position"] or 0)
        expected = expected_ctr_for_position(position, curve)
        if ctr < expected * 0.5 and impressions >= 10:
            potential_clicks = impressions * expected - float(r["total_clicks"] or 0)
            results.append({
                "page": r["page"],
                "query": r["query"],
                "clicks": float(r["total_clicks"] or 0),
                "impressions": impressions,
                "ctr": round(ctr, 4),
                "expected_ctr": round(expected, 4),
                "position": round(position, 1),
                # The app-wide definition (what Home queues). Rows without it
                # are early signals: too few impressions or not on page one.
                "confirmed": is_ctr_underperformer(position, ctr, impressions, curve),
                "potential_clicks": round(max(potential_clicks, 0), 0),
                "action": "Improve meta title & description" if position <= 10 else "Improve rankings first",
            })
    results.sort(key=lambda x: (x["confirmed"], x["potential_clicks"]), reverse=True)
    results = results[:limit]
    _attach_matched_items(store_id, results)
    return results


def _attach_matched_items(store_id, rows):
    """Resolve /products/<handle> and /collections/<handle> pages to synced items
    so the UI can deep-link straight into the SEO optimize modal."""
    def _handle(path, seg):
        return path.split(seg, 1)[1].split("?")[0].split("#")[0].strip("/")

    handles = {"product": set(), "collection": set()}
    for r in rows:
        path = r["page"]
        if "/products/" in path:
            handles["product"].add(_handle(path, "/products/"))
        elif "/collections/" in path:
            handles["collection"].add(_handle(path, "/collections/"))
    maps = {"product": {}, "collection": {}}
    with db.get_cursor() as cur:
        if handles["product"]:
            cur.execute(
                "SELECT id, handle, title FROM synced_products WHERE store_id = %s AND handle = ANY(%s)",
                (store_id, list(handles["product"])),
            )
            maps["product"] = {m["handle"]: m for m in cur.fetchall()}
        if handles["collection"]:
            cur.execute(
                "SELECT id, handle, title FROM synced_collections WHERE store_id = %s AND handle = ANY(%s)",
                (store_id, list(handles["collection"])),
            )
            maps["collection"] = {m["handle"]: m for m in cur.fetchall()}
    for r in rows:
        r["matched_item"] = None
        path = r["page"]
        if "/products/" in path:
            m = maps["product"].get(_handle(path, "/products/"))
            if m:
                r["matched_item"] = {"item_type": "product", "id": m["id"], "title": m["title"]}
        elif "/collections/" in path:
            m = maps["collection"].get(_handle(path, "/collections/"))
            if m:
                r["matched_item"] = {"item_type": "collection", "id": m["id"], "title": m["title"]}


# A page counts as competing for a query only with real exposure on it: at
# least this share of the query's impressions. Two pages that each show up a
# handful of times (or a product beside its own collection on a brand search)
# are normal, not a split.
CANNIBALIZATION_MIN_SHARE = 0.10


def get_cannibalization(store_id, date_from, date_to, min_impressions=10, device=None, country=None):
    where, params = _build_filters(store_id, date_from, date_to, device, country)
    params.extend([min_impressions, CANNIBALIZATION_MIN_SHARE, CANNIBALIZATION_MIN_SHARE])
    with db.get_cursor() as cur:
        cur.execute(
            f"""WITH query_pages AS (
                 SELECT query,
                        page,
                        SUM(clicks) as total_clicks,
                        SUM(impressions) as total_impressions,
                        SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
                 FROM gsc_daily_metrics
                 WHERE {where}
                 GROUP BY query, page
                 HAVING SUM(impressions) >= %s
               ),
               shared AS (
                 SELECT *, total_impressions / NULLIF(SUM(total_impressions) OVER (PARTITION BY query), 0) AS share
                 FROM query_pages
               ),
               multi_page_queries AS (
                 SELECT query, COUNT(DISTINCT page) as page_count,
                        SUM(total_clicks) as total_clicks,
                        SUM(total_impressions) as total_impressions
                 FROM shared
                 WHERE share >= %s
                 GROUP BY query
                 HAVING COUNT(DISTINCT page) >= 2
               )
               SELECT qp.query, qp.page, qp.total_clicks, qp.total_impressions, qp.avg_position,
                      mpq.page_count
               FROM shared qp
               JOIN multi_page_queries mpq ON qp.query = mpq.query
               WHERE qp.share >= %s
               ORDER BY mpq.total_impressions DESC, mpq.page_count DESC, qp.query, qp.total_clicks DESC""",
            params,
        )
        rows = cur.fetchall()

    with db.get_cursor() as cur:
        cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
        cred = cur.fetchone()
        from app.services.url_utils import gsc_base_url
        base_url = gsc_base_url(cred.get("gsc_property") if cred else "")

        cur.execute("SELECT id, shopify_id, handle, title FROM synced_products WHERE store_id = %s", (store_id,))
        products = {p["handle"]: p for p in cur.fetchall() if p.get("handle")}

        cur.execute("SELECT id, shopify_id, handle, title FROM synced_collections WHERE store_id = %s", (store_id,))
        collections = {c["handle"]: c for c in cur.fetchall() if c.get("handle")}

    def _match_page(page_url):
        path = page_url.replace(base_url, "").strip("/")
        if path.startswith("products/"):
            handle = path.replace("products/", "").split("?")[0].split("#")[0]
            p = products.get(handle)
            if p:
                return {"type": "product", "id": p["id"], "title": p["title"]}
            # A product path with no live product = deleted/unpublished.
            return {"type": "removed", "id": None, "title": "/" + path}
        elif path.startswith("collections/"):
            handle = path.replace("collections/", "").split("?")[0].split("#")[0]
            c = collections.get(handle)
            if c:
                return {"type": "collection", "id": c["id"], "title": c["title"]}
            # Consolidated/deleted collections keep ranking inside the GSC
            # window for weeks — they are not fixable pages, just Google
            # catching up with a merge that already happened.
            return {"type": "removed", "id": None, "title": "/" + path}
        if path == "" or path == "/":
            return {"type": "page", "id": None, "title": "Homepage"}
        segments = path.split("?")[0].split("#")[0].strip("/").split("/")
        if segments[0] == "blogs" and len(segments) == 2:
            return {"type": "blog_index", "id": None, "title": "/" + path}
        if segments[0] == "blogs" and len(segments) >= 3 and segments[2] == "tagged":
            # Noindexed by the theme snippet; these drop out as Google recrawls.
            return {"type": "tag_archive", "id": None, "title": "/" + path}
        return {"type": "page", "id": None, "title": "/" + path}

    grouped = {}
    for r in rows:
        q = r["query"]
        if q not in grouped:
            grouped[q] = {
                "query": q,
                "page_count": 0,
                "removed_pages": 0,
                "total_impressions": 0,
                "total_clicks": 0,
                "pages": [],
            }
        match = _match_page(r["page"])
        if match["type"] == "removed":
            # Not a conflict the user can act on — the page is already gone
            # (consolidated/deleted); its GSC rows just haven't aged out yet.
            grouped[q]["removed_pages"] += 1
            continue
        clicks = float(r["total_clicks"] or 0)
        impressions = float(r["total_impressions"] or 0)
        position = round(float(r["avg_position"] or 0), 1)

        grouped[q]["total_impressions"] += impressions
        grouped[q]["total_clicks"] += clicks
        grouped[q]["pages"].append({
            "page": r["page"],
            "clicks": clicks,
            "impressions": impressions,
            "position": position,
            "match": match,
        })

    # A query needs 2+ LIVE pages to still be a conflict — groups that only
    # existed because of removed pages are already resolved.
    grouped = {q: g for q, g in grouped.items() if len(g["pages"]) >= 2}

    for g in grouped.values():
        pages = g["pages"]
        g["page_count"] = len(pages)
        if pages:
            winner = max(pages, key=lambda p: (p["clicks"], p["impressions"]))
            winner["is_winner"] = True

        page_count = g["page_count"]
        total_imp = g["total_impressions"]
        if page_count >= 5 and total_imp >= 100:
            g["severity"] = "high"
        elif page_count >= 3 or total_imp >= 50:
            g["severity"] = "medium"
        else:
            g["severity"] = "low"

    try:
        from app.services.semrush_import_service import get_keywords_by_list
        keyword_list = [g["query"] for g in grouped.values()]
        semrush_data = get_keywords_by_list(store_id, keyword_list)
        for g in grouped.values():
            sr = semrush_data.get(g["query"].lower(), {})
            g["semrush_volume"] = sr.get("search_volume")
            g["semrush_cpc"] = sr.get("cpc")
    except Exception:
        pass

    results = sorted(grouped.values(), key=lambda x: (
        {"high": 3, "medium": 2, "low": 1}.get(x["severity"], 0),
        x["total_impressions"],
    ), reverse=True)

    total_semrush_vol = sum(g.get("semrush_volume") or 0 for g in results)

    summary = {
        "total_queries": len(results),
        "total_pages_affected": sum(g["page_count"] for g in results),
        "total_impressions_at_risk": sum(g["total_impressions"] for g in results),
        "total_clicks_diluted": sum(g["total_clicks"] for g in results),
        "total_semrush_volume": total_semrush_vol,
        "high_count": sum(1 for g in results if g["severity"] == "high"),
        "medium_count": sum(1 for g in results if g["severity"] == "medium"),
        "low_count": sum(1 for g in results if g["severity"] == "low"),
    }

    return {"summary": summary, "groups": results}


# Search Console final data lags ~3 days; Google needs ~2 to recrawl.
FIX_VERIFY_LAG_DAYS = 2
FIX_VERIFY_MIN_DAYS = 10
FIX_VERIFY_MAX_DAYS = 28


def verify_cannibalization_fixes(store_id):
    """Judge each fix on data from after it only. Verified = the query still
    gets impressions and only one page shows for it; needs attention = two
    or more pages still split it once a full window has passed. A fix whose
    query got no impressions since stays pending (no evidence either way)."""
    today = datetime.now(timezone.utc).date()
    data_end = today - timedelta(days=3)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, query, page_url, applied_at, status FROM cannibalization_fixes WHERE store_id = %s AND status IN ('pending', 'needs_attention')",
            (store_id,),
        )
        pending = cur.fetchall()

    verified = needs_attention = still_pending = 0
    for fix in pending:
        if not fix["applied_at"]:
            still_pending += 1
            continue
        start = fix["applied_at"].date() + timedelta(days=FIX_VERIFY_LAG_DAYS)
        stop = min(data_end, start + timedelta(days=FIX_VERIFY_MAX_DAYS - 1))
        window_days = (stop - start).days + 1
        if window_days < FIX_VERIFY_MIN_DAYS:
            still_pending += 1
            continue
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT page FROM gsc_daily_metrics
                   WHERE store_id = %s AND query = %s AND date >= %s AND date <= %s
                   GROUP BY page HAVING SUM(impressions) >= 3""",
                (store_id, fix["query"], start.isoformat(), stop.isoformat()),
            )
            pages = cur.fetchall()
            if not pages:
                still_pending += 1
                continue
            if len(pages) == 1:
                cur.execute(
                    "UPDATE cannibalization_fixes SET status = 'verified', verified_at = NOW() WHERE id = %s",
                    (fix["id"],),
                )
                verified += 1
            else:
                cur.execute(
                    "UPDATE cannibalization_fixes SET status = 'needs_attention' WHERE id = %s",
                    (fix["id"],),
                )
                needs_attention += 1

    logger.info("Fix verification for store %d: %d verified, %d needs attention, %d still pending",
                store_id, verified, needs_attention, still_pending)
    return {"verified": verified, "needs_attention": needs_attention, "still_pending": still_pending}


def get_trends(store_id, date_from, date_to, device=None, country=None):
    try:
        service = _build_service(store_id)
        with db.get_cursor() as cur:
            cur.execute("SELECT gsc_property FROM gsc_credentials WHERE store_id = %s", (store_id,))
            cred = cur.fetchone()
        site_url = cred["gsc_property"] if cred else None

        if service and site_url:
            body = {
                "startDate": date_from,
                "endDate": date_to,
                "dimensions": ["date"],
                "rowLimit": 25000,
                "dataState": "all",
            }
            dim_filters = []
            if device:
                dim_filters.append({"dimension": "device", "expression": device})
            if country:
                dim_filters.append({"dimension": "country", "expression": country})
            if dim_filters:
                body["dimensionFilterGroups"] = [{"filters": dim_filters}]

            response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
            api_rows = response.get("rows", [])
            return [
                {
                    "date": r["keys"][0],
                    "clicks": float(r.get("clicks", 0)),
                    "impressions": float(r.get("impressions", 0)),
                    "ctr": round(float(r.get("ctr", 0)), 4),
                    "position": round(float(r.get("position", 0)), 1),
                }
                for r in sorted(api_rows, key=lambda x: x["keys"][0])
            ]
    except Exception as e:
        logger.warning("Live trends fetch failed, falling back to DB: %s", str(e))

    where, params = _build_filters(store_id, date_from, date_to, device, country)
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT date,
                      SUM(clicks) as total_clicks,
                      SUM(impressions) as total_impressions,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                      CASE WHEN SUM(impressions) > 0 THEN SUM(position * impressions) / SUM(impressions) ELSE 0 END as avg_position
               FROM gsc_daily_metrics
               WHERE {where}
               GROUP BY date
               ORDER BY date ASC""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "date": r["date"].isoformat(),
            "clicks": float(r["total_clicks"] or 0),
            "impressions": float(r["total_impressions"] or 0),
            "ctr": round(float(r["avg_ctr"] or 0), 4),
            "position": round(float(r["avg_position"] or 0), 1),
        }
        for r in rows
    ]


def take_seo_score_snapshot(store_id):
    """Record per-item score history using the same unified 3-pillar
    composition the priority queue shows (content + keyword + search) —
    a legacy content-only snapshot here would drift from the UI."""
    from app.services.ctr_benchmarks import get_expected_ctr_curve
    from app.services.seo_analyzer import analyze_product_seo, analyze_collection_seo
    from app.services.seo_priority_service import (
        _get_gsc_metrics_for_store,
        _get_semrush_metrics_for_store,
        compute_unified_item_score,
    )

    gsc_metrics = _get_gsc_metrics_for_store(store_id)
    semrush_metrics = _get_semrush_metrics_for_store(store_id)
    ctr_curve = get_expected_ctr_curve(store_id)

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT item_type, item_id, score FROM technical_seo_analyses WHERE store_id = %s",
            (store_id,),
        )
        kw_scores = {(r["item_type"], r["item_id"]): r["score"] for r in cur.fetchall()}

        cur.execute("SELECT id, shopify_id, handle, title, meta_title, meta_description, description, images FROM synced_products WHERE store_id = %s", (store_id,))
        products = cur.fetchall()

        for p in products:
            if p.get("images") and isinstance(p["images"], str):
                p["images"] = json.loads(p["images"])
            path = f"products/{p.get('handle') or ''}"
            seo = analyze_product_seo(p)
            unified = compute_unified_item_score(
                seo, kw_scores.get(("product", p["id"])),
                gsc_metrics.get(path, {}), semrush_metrics.get(path, {}), ctr_curve,
            )
            cur.execute(
                """INSERT INTO seo_score_history (store_id, item_type, item_id, shopify_id, title, score, breakdown, recorded_at)
                   VALUES (%s, 'product', %s, %s, %s, %s, %s, NOW())""",
                (store_id, p["id"], p["shopify_id"], p["title"], unified["seo_score"], json.dumps(seo["breakdown"])),
            )

        cur.execute("SELECT id, shopify_id, handle, title, meta_title, meta_description, description FROM synced_collections WHERE store_id = %s", (store_id,))
        collections = cur.fetchall()

        for c in collections:
            path = f"collections/{c.get('handle') or ''}"
            seo = analyze_collection_seo(c)
            unified = compute_unified_item_score(
                seo, kw_scores.get(("collection", c["id"])),
                gsc_metrics.get(path, {}), semrush_metrics.get(path, {}), ctr_curve,
            )
            cur.execute(
                """INSERT INTO seo_score_history (store_id, item_type, item_id, shopify_id, title, score, breakdown, recorded_at)
                   VALUES (%s, 'collection', %s, %s, %s, %s, %s, NOW())""",
                (store_id, c["id"], c["shopify_id"], c["title"], unified["seo_score"], json.dumps(seo["breakdown"])),
            )

    return {"products": len(products), "collections": len(collections)}


def get_score_trends(store_id, date_from, date_to, item_type=None):
    type_filter = "AND item_type = %s" if item_type else ""
    params = [store_id, date_from, date_to]
    if item_type:
        params.append(item_type)

    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT recorded_at::date as date,
                       AVG(score) as avg_score,
                       COUNT(*) FILTER (WHERE score >= 80) as good_count,
                       COUNT(*) FILTER (WHERE score >= 50 AND score < 80) as ok_count,
                       COUNT(*) FILTER (WHERE score < 50) as poor_count,
                       COUNT(*) as total_count
                FROM seo_score_history
                WHERE store_id = %s AND recorded_at >= %s AND recorded_at <= %s {type_filter}
                GROUP BY recorded_at::date
                ORDER BY date ASC""",
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "date": r["date"].isoformat(),
            "avg_score": round(float(r["avg_score"] or 0), 1),
            "good_count": r["good_count"],
            "ok_count": r["ok_count"],
            "poor_count": r["poor_count"],
            "total_count": r["total_count"],
        }
        for r in rows
    ]


def get_score_improvements(store_id, limit=100):
    with db.get_cursor() as cur:
        cur.execute(
            """WITH latest AS (
                 SELECT DISTINCT ON (item_type, item_id)
                        item_type, item_id, shopify_id, title, score, recorded_at
                 FROM seo_score_history WHERE store_id = %s
                 ORDER BY item_type, item_id, recorded_at DESC
               ),
               previous AS (
                 SELECT DISTINCT ON (item_type, item_id)
                        item_type, item_id, score as prev_score, recorded_at as prev_date
                 FROM seo_score_history WHERE store_id = %s
                   AND recorded_at < (SELECT MAX(recorded_at)::date FROM seo_score_history WHERE store_id = %s)
                 ORDER BY item_type, item_id, recorded_at DESC
               )
               SELECT l.item_type, l.item_id, l.shopify_id, l.title, l.score, l.recorded_at,
                      p.prev_score, p.prev_date,
                      (l.score - COALESCE(p.prev_score, l.score)) as score_change
               FROM latest l
               LEFT JOIN previous p ON l.item_type = p.item_type AND l.item_id = p.item_id
               ORDER BY ABS(l.score - COALESCE(p.prev_score, l.score)) DESC
               LIMIT %s""",
            (store_id, store_id, store_id, limit),
        )
        rows = cur.fetchall()

    return [
        {
            "item_type": r["item_type"],
            "item_id": r["item_id"],
            "shopify_id": r["shopify_id"],
            "title": r["title"],
            "score": r["score"],
            "prev_score": r["prev_score"],
            "score_change": r["score_change"] or 0,
            "recorded_at": r["recorded_at"].isoformat() if r["recorded_at"] else None,
        }
        for r in rows
    ]


class PullScheduler:
    def __init__(self, interval_hours=6):
        self.interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("GSC pull scheduler started (every %d hours)", self.interval // 3600)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("GSC pull scheduler stopped")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._pull_all_stores()
            except Exception as e:
                logger.error("GSC scheduled pull error: %s", str(e))
            self._stop_event.wait(self.interval)

    def _pull_all_stores(self):
        # Skip stores pulled recently — restarts/multiple ticks must not
        # double-pull and burn API quota.
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT gc.store_id FROM gsc_credentials gc
                   WHERE gc.gsc_property IS NOT NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM gsc_data_pulls p
                       WHERE p.store_id = gc.store_id
                         AND p.status IN ('completed', 'running')
                         AND p.started_at > NOW() - make_interval(secs => %s)
                     )""",
                (self.interval * 0.9,),
            )
            stores = cur.fetchall()

        from app.services.job_health import record_job, run_recorded

        for s in stores:
            store_id = s["store_id"]
            try:
                result = pull_data(store_id, pull_type="incremental")
            except Exception as e:
                logger.error("GSC pull failed for store %d: %s", store_id, str(e))
                record_job("gsc_pull", store_id, ok=False, error=e)
                continue
            record_job("gsc_pull", store_id, ok=True,
                       detail=f"{(result or {}).get('rows_pulled', 0)} rows")
            run_recorded("cannibalization_verify", store_id, verify_cannibalization_fixes, store_id)
            _consolidate_if_stale(store_id)


def _consolidate_if_stale(store_id, max_age_days=7):
    """Fresh GSC data is consolidation's input, but master_keywords (whose
    volumes/positions the resolver scores with) used to refresh only when
    someone pressed the Consolidate button — it once drifted 3+ months stale.
    Chain it after a pull when older than the freshness strip's own 7-day
    threshold, incremental classification included (mirrors the manual
    route). Non-fatal."""
    from datetime import datetime, timedelta, timezone
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT MAX(completed_at) AS at FROM keyword_strategy_runs
                   WHERE store_id = %s AND run_type = 'consolidate' AND status = 'completed'""",
                (store_id,),
            )
            at = (cur.fetchone() or {}).get("at")
        if at:
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - at < timedelta(days=max_age_days):
                return
        from app.services.job_health import record_job
        from app.services.keyword_strategy_service import consolidate_master_keywords
        try:
            result = consolidate_master_keywords(store_id)
        except Exception as e:
            record_job("keyword_consolidation", store_id, ok=False, error=e)
            raise
        record_job("keyword_consolidation", store_id, ok=True, detail=f"{result.get('total', 0)} keywords")
        logger.info("Auto-consolidated master keywords for store %s after GSC pull", store_id)
        try:
            from app.services.keyword_ai_classifier import run_ai_classification
            run_ai_classification(store_id, mode="incremental")
        except Exception as e:
            logger.warning("Post-consolidation classification skipped: %s", e)
    except Exception:
        logger.exception("Auto-consolidation failed for store %s (non-fatal)", store_id)


def get_page_product_matches(store_id, date_from, date_to, device=None, country=None, limit=500):
    where, params = _build_filters(store_id, date_from, date_to, device, country)
    with db.get_cursor() as cur:
        cur.execute(
            f"""SELECT page,
                       SUM(clicks) as total_clicks,
                       SUM(impressions) as total_impressions,
                       CASE WHEN SUM(impressions) > 0 THEN SUM(clicks) / SUM(impressions) ELSE 0 END as avg_ctr,
                       SUM(position * impressions) / NULLIF(SUM(impressions), 0) as avg_position
                FROM gsc_daily_metrics
                WHERE {where}
                GROUP BY page
                ORDER BY total_impressions DESC
                LIMIT %s""",
            params + [limit],
        )
        gsc_pages = cur.fetchall()

        cur.execute(
            "SELECT id, shopify_id, handle, title FROM synced_products WHERE store_id = %s",
            (store_id,),
        )
        products = cur.fetchall()

        cur.execute(
            "SELECT id, shopify_id, handle, title FROM synced_collections WHERE store_id = %s",
            (store_id,),
        )
        collections = cur.fetchall()

        cur.execute(
            "SELECT gsc_property FROM gsc_credentials WHERE store_id = %s",
            (store_id,),
        )
        cred = cur.fetchone()

    from app.services.url_utils import gsc_base_url
    base_url = gsc_base_url(cred.get("gsc_property") if cred else "")

    product_handles = {p["handle"]: p for p in products if p.get("handle")}
    collection_handles = {c["handle"]: c for c in collections if c.get("handle")}

    results = []
    for gp in gsc_pages:
        page_url = gp["page"]
        path = page_url.replace(base_url, "").strip("/")

        matched = None
        match_type = None

        if path.startswith("products/"):
            handle = path.replace("products/", "").split("?")[0].split("#")[0]
            if handle in product_handles:
                p = product_handles[handle]
                matched = {"id": p["id"], "shopify_id": p["shopify_id"], "title": p["title"], "handle": p["handle"]}
                match_type = "product"

        elif path.startswith("collections/"):
            handle = path.replace("collections/", "").split("?")[0].split("#")[0]
            if handle in collection_handles:
                c = collection_handles[handle]
                matched = {"id": c["id"], "shopify_id": c["shopify_id"], "title": c["title"], "handle": c["handle"]}
                match_type = "collection"

        results.append({
            "page": page_url,
            "clicks": float(gp["total_clicks"] or 0),
            "impressions": float(gp["total_impressions"] or 0),
            "ctr": round(float(gp["avg_ctr"] or 0), 4),
            "position": round(float(gp["avg_position"] or 0), 1),
            "match_type": match_type,
            "matched_item": matched,
        })

    return results


def get_available_countries(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT country FROM gsc_daily_metrics WHERE store_id = %s AND country IS NOT NULL ORDER BY country",
            (store_id,),
        )
        return [r["country"] for r in cur.fetchall()]


class SnapshotScheduler:
    def __init__(self, interval_hours=24):
        self.interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("SEO score snapshot scheduler started (every %d hours)", self.interval // 3600)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        self._stop_event.wait(300)
        while not self._stop_event.is_set():
            try:
                self._snapshot_all_stores()
            except Exception as e:
                logger.error("Score snapshot error: %s", str(e))
            self._stop_event.wait(self.interval)

    def _snapshot_all_stores(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT id FROM stores WHERE is_active = TRUE")
            stores = cur.fetchall()

        for s in stores:
            try:
                with db.get_cursor() as cur:
                    cur.execute(
                        "SELECT MAX(recorded_at)::date as last_date FROM seo_score_history WHERE store_id = %s",
                        (s["id"],),
                    )
                    row = cur.fetchone()
                    last_date = row["last_date"] if row else None

                today = datetime.now(timezone.utc).date()
                if last_date and last_date >= today:
                    continue

                take_seo_score_snapshot(s["id"])
                logger.info("Daily score snapshot completed for store %d", s["id"])
            except Exception as e:
                logger.error("Score snapshot failed for store %d: %s", s["id"], str(e))


def init_scheduler():
    global _pull_scheduler, _snapshot_scheduler
    if _pull_scheduler is None:
        _pull_scheduler = PullScheduler(interval_hours=6)
        _pull_scheduler.start()
    if _snapshot_scheduler is None:
        _snapshot_scheduler = SnapshotScheduler(interval_hours=24)
        _snapshot_scheduler.start()
