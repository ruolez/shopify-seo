"""IndexNow integration — pushes new/updated URLs to Bing's index (which ChatGPT
search rides) within ~24h instead of waiting for organic crawl discovery.

Key hosting: Shopify can't serve root .txt files, so the verification key lives
on a page at /pages/<key> rendered by a layout-none template whose body is just
the key. IndexNow's keyLocation parameter allows the key file anywhere on the
host. The page is served as text/html (spec wants text/plain) — setup_key
verifies the live body and reports `verified` vs `verification_risk`; a
settings override `indexnow.key_location_override` supports self-hosting the
key at the domain root via a CDN/proxy if Bing rejects the page.

ping_urls NEVER raises — a ping failure must never affect a publish.
"""

import json
import logging
import threading
import uuid

import requests

from app.auth import decrypt_value
from app.database import db
from app.settings_manager import get_setting

logger = logging.getLogger(__name__)

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_TEMPLATE_KEY = "templates/page.indexnow-key.liquid"
KEY_TEMPLATE_SUFFIX = "indexnow-key"
KEY_TEMPLATE_BODY = "{% layout none %}{{ page.content | strip_html | strip }}"
MAX_URLS_PER_PING = 10000
SUBMIT_ALL_CAP = 5000


def _store(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _host(store):
    return (
        (store.get("primary_domain") or store.get("domain") or "")
        .replace("https://", "")
        .replace("http://", "")
        .strip("/")
    )


def _base_url(store):
    host = _host(store)
    return f"https://{host}" if host else None


def get_or_create_key(store_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT indexnow_key FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone()
        if row and row.get("indexnow_key"):
            return row["indexnow_key"]
        key = uuid.uuid4().hex
        cur.execute("UPDATE stores SET indexnow_key = %s WHERE id = %s", (key, store_id))
        return key


def _asset_row(store_id):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT content, status FROM aeo_assets WHERE store_id = %s AND asset_type = 'indexnow_key'",
            (store_id,),
        )
        return cur.fetchone()


def key_location(store_id, store=None):
    """The live URL serving the key: the settings override if set, else the
    published /pages/<handle> location recorded at setup."""
    override = get_setting("indexnow.key_location_override", None)
    if override:
        return override
    row = _asset_row(store_id)
    if row and row.get("status") == "published" and row.get("content"):
        return row["content"]
    return None


def setup_key(store_id):
    """Publish the key template + page and verify it serves the key.

    Returns {key, key_location, verified, body_preview}. Raises on Shopify errors.
    """
    from app.shopify_client import ShopifyClient

    store = _store(store_id)
    if not store:
        raise ValueError("Store not found")
    base = _base_url(store)
    if not base:
        raise ValueError("Store has no domain configured")

    key = get_or_create_key(store_id)
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])

    theme_id = client.get_main_theme_id()
    if not theme_id:
        raise ValueError("No main theme found")
    client.upsert_theme_asset(theme_id, KEY_TEMPLATE_KEY, KEY_TEMPLATE_BODY)

    existing = client.get_page_by_handle(key)
    if existing:
        page = client.update_page(existing["id"], {
            "title": "IndexNow key",
            "body": key,
            "is_published": True,
            "template_suffix": KEY_TEMPLATE_SUFFIX,
        })
    else:
        page = client.create_page(
            title="IndexNow key",
            body_html=key,
            handle=key,
            template_suffix=KEY_TEMPLATE_SUFFIX,
        )
    # IndexNow scopes a key file to its directory: a key under /pages/ can only
    # verify /pages/* URLs, so site-wide submission REQUIRES a root-level key.
    # Shopify can't serve root .txt files directly, but a URL redirect can claim
    # /<key>.txt and 301 to the key page; IndexNow's validator follows it and
    # reads the bare key (verified working, 202). `?view=<suffix>` forces the
    # alternate layout-none template — some themes/apps ignore the page's
    # templateSuffix on plain renders.
    plain = f"{base}/pages/{page['handle']}"
    view_url = f"{plain}?view={KEY_TEMPLATE_SUFFIX}"
    root_txt = f"{base}/{key}.txt"
    try:
        client.create_url_redirect(f"/{key}.txt", f"/pages/{page['handle']}?view={KEY_TEMPLATE_SUFFIX}")
    except Exception as e:
        logger.warning("IndexNow root-key redirect create failed: %s", e)

    # Prefer the root-scoped URL; fall back to the /pages/ URLs (path-scoped —
    # only useful for page submissions, but better than nothing).
    candidates = [root_txt, view_url, plain]
    loc = root_txt
    verified = False
    body_preview = ""
    for candidate in candidates:
        try:
            resp = requests.get(candidate, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            logger.warning("IndexNow key verification fetch failed for %s: %s", candidate, e)
            continue
        body = (resp.text or "").strip()
        if resp.status_code == 200 and body == key:
            loc, verified, body_preview = candidate, True, body[:200]
            break
        if resp.status_code == 200 and key in body and not verified:
            loc, verified, body_preview = candidate, None, body[:200]  # risk: key amid markup

    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO aeo_assets (store_id, asset_type, content, status, published_at, updated_at)
            VALUES (%s, 'indexnow_key', %s, 'published', NOW(), NOW())
            ON CONFLICT (store_id, asset_type) DO UPDATE SET
                content = EXCLUDED.content, status = 'published',
                published_at = NOW(), updated_at = NOW()
            """,
            (store_id, loc),
        )
    return {
        "key": key,
        "key_location": loc,
        "verified": verified,  # True | None (risk) | False
        "body_preview": body_preview,
    }


def remove_key(store_id):
    """Unpublish: delete the key page + template; keep the key for re-setup."""
    from app.shopify_client import ShopifyClient

    store = _store(store_id)
    if not store:
        raise ValueError("Store not found")
    client = ShopifyClient(store["domain"], decrypt_value(store["access_token_enc"]), store["api_version"])
    key = store.get("indexnow_key")
    if key:
        existing = client.get_page_by_handle(key)
        if existing:
            client.delete_page(existing["id"])
        try:
            client.delete_url_redirect_by_path(f"/{key}.txt")
        except Exception:
            pass
    theme_id = client.get_main_theme_id()
    if theme_id:
        try:
            client.delete_theme_asset(theme_id, KEY_TEMPLATE_KEY)
        except Exception:
            pass
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE aeo_assets SET status = 'draft', updated_at = NOW() WHERE store_id = %s AND asset_type = 'indexnow_key'",
            (store_id,),
        )


def _log_ping(store_id, urls, reason, status_code, ok, error=None):
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO indexnow_pings (store_id, url_count, sample_urls, reason, status_code, ok, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (store_id, len(urls), json.dumps(urls[:10]), reason, status_code, ok, error),
            )
    except Exception:
        pass


def ping_urls(store_id, urls, reason=""):
    """Submit URLs to IndexNow. Never raises; returns {ok, url_count, ...}.

    Silently no-ops (no log row) when the store has no published key — the
    feature is opt-in via setup.
    """
    try:
        urls = [u for u in (urls or []) if u][:MAX_URLS_PER_PING]
        if not urls:
            return {"ok": False, "skipped": "no urls"}
        store = _store(store_id)
        if not store or not store.get("indexnow_key"):
            return {"ok": False, "skipped": "no key"}
        loc = key_location(store_id, store)
        if not loc:
            return {"ok": False, "skipped": "key not published"}
        host = _host(store)
        payload = {
            "host": host,
            "key": store["indexnow_key"],
            "keyLocation": loc,
            "urlList": urls,
        }
        try:
            resp = requests.post(INDEXNOW_ENDPOINT, json=payload, timeout=10)
            ok = resp.status_code in (200, 202)
            _log_ping(store_id, urls, reason, resp.status_code, ok,
                      None if ok else (resp.text or "")[:500])
            # 403/422 mean the key did not validate: every later ping is
            # discarded too, so say so on Home instead of only in the log.
            from app.services.job_health import record_job
            record_job("indexnow", store_id, ok=ok,
                       error=None if ok else f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
            return {"ok": ok, "status_code": resp.status_code, "url_count": len(urls)}
        except Exception as e:
            _log_ping(store_id, urls, reason, None, False, str(e)[:500])
            return {"ok": False, "error": str(e)}
    except Exception as e:  # belt & braces: pings must never break a publish
        logger.warning("IndexNow ping failed unexpectedly: %s", e)
        return {"ok": False, "error": str(e)}


def ping_async(store_id, urls, reason=""):
    """Fire-and-forget ping — the one helper publish flows call."""
    urls = [u for u in (urls or []) if u]
    if not urls:
        return
    threading.Thread(target=ping_urls, args=(store_id, urls, reason), daemon=True).start()


def submit_all(store_id):
    """Bounded full-site submission from locally synced data."""
    store = _store(store_id)
    if not store:
        raise ValueError("Store not found")
    base = _base_url(store)
    if not base:
        raise ValueError("Store has no domain configured")
    urls = [f"{base}/"]
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT handle FROM synced_products WHERE store_id = %s AND handle <> '' AND LOWER(status) = 'active'",
            (store_id,),
        )
        urls += [f"{base}/products/{r['handle']}" for r in cur.fetchall()]
        cur.execute(
            "SELECT handle FROM synced_collections WHERE store_id = %s AND handle <> '' AND published = TRUE",
            (store_id,),
        )
        urls += [f"{base}/collections/{r['handle']}" for r in cur.fetchall()]
        try:
            cur.execute(
                """SELECT a.handle, b.handle AS blog_handle
                   FROM synced_articles a JOIN synced_blogs b ON b.id = a.blog_id
                   WHERE a.store_id = %s AND a.handle <> ''""",
                (store_id,),
            )
            urls += [f"{base}/blogs/{r['blog_handle']}/{r['handle']}" for r in cur.fetchall() if r.get("blog_handle")]
        except Exception:
            pass
    urls = urls[:SUBMIT_ALL_CAP]
    return ping_urls(store_id, urls, "manual_all")


def status(store_id):
    store = _store(store_id) or {}
    row = _asset_row(store_id)
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT url_count, sample_urls, reason, status_code, ok, error, created_at
               FROM indexnow_pings WHERE store_id = %s ORDER BY id DESC LIMIT 10""",
            (store_id,),
        )
        pings = [
            {**dict(p), "created_at": p["created_at"].isoformat() if p["created_at"] else None}
            for p in cur.fetchall()
        ]
    return {
        "key_set": bool(store.get("indexnow_key")),
        "published_status": row.get("status") if row else None,
        "key_location": key_location(store_id, store),
        "override_active": bool(get_setting("indexnow.key_location_override", None)),
        "last_ping": pings[0] if pings else None,
        "pings": pings,
    }
