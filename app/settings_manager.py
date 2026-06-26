import json
import logging
import threading
import time

from app.database import db
from app.prompt_defaults import DEFAULTS

logger = logging.getLogger(__name__)

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 60


def _cache_key(key):
    return f"setting:{key}"


def get_setting(key, default=None):
    ck = _cache_key(key)
    with _cache_lock:
        entry = _cache.get(ck)
        if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
            return entry["value"]

    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
    except Exception:
        row = None

    if row:
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        with _cache_lock:
            _cache[ck] = {"value": value, "ts": time.time()}
        return value

    fallback = default if default is not None else DEFAULTS.get(key)
    with _cache_lock:
        _cache[ck] = {"value": fallback, "ts": time.time()}
    return fallback


def set_setting(key, value):
    json_value = json.dumps(value)
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO settings (key, value, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
            (key, json_value),
        )
    with _cache_lock:
        _cache[_cache_key(key)] = {"value": value, "ts": time.time()}


def delete_setting(key):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM settings WHERE key = %s", (key,))
    with _cache_lock:
        _cache.pop(_cache_key(key), None)


def get_all_custom(prefix=None):
    with db.get_cursor() as cur:
        if prefix:
            cur.execute("SELECT key, value FROM settings WHERE key LIKE %s ORDER BY key", (prefix + "%",))
        else:
            cur.execute("SELECT key, value FROM settings ORDER BY key")
        rows = cur.fetchall()

    result = {}
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            result[row["key"]] = row["value"]
    return result


def get_effective(prefix=None):
    defaults = {k: v for k, v in DEFAULTS.items() if not prefix or k.startswith(prefix)}
    overrides = get_all_custom(prefix)
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def invalidate_cache():
    with _cache_lock:
        _cache.clear()
