"""Per-store settings for the visual blog article pipeline.

`blog_visual_settings` holds nullable per-store overrides; NULL means "use the
default". `get_article_visuals(store_id)` is the single resolver every code
path (wizard, scheduler, template push, preview) reads from.
"""

import logging

from app.database import db
from app.services.blog_template import link_color_warnings, normalize_hex

logger = logging.getLogger(__name__)

STYLE_VISUAL = "visual"
STYLE_CLASSIC = "classic"
STYLES = (STYLE_VISUAL, STYLE_CLASSIC)

DEFAULTS = {
    "default_style": STYLE_VISUAL,
    "brand_color": "#1a73e8",
    "inline_image_count": 2,
    "max_images_per_article": 4,
    "product_composite": True,
    "visual_blocks": True,
    "toc": True,
    "related_limit": 3,
}

VISUAL_KEYS = tuple(DEFAULTS.keys())
_INT_KEYS = {"inline_image_count": (0, 4), "max_images_per_article": (1, 6), "related_limit": (0, 6)}
_BOOL_KEYS = ("product_composite", "visual_blocks", "toc")


def _clamp_int(value, lo, hi, fallback):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return fallback


def normalize_visuals(data, base=None):
    """Return a fully-populated, validated visuals dict.

    `data` may be partial; missing/None keys fall back to `base` (or DEFAULTS).
    """
    base = dict(base or DEFAULTS)
    out = dict(base)
    data = data or {}
    if data.get("default_style") in STYLES:
        out["default_style"] = data["default_style"]
    if data.get("brand_color") is not None:
        out["brand_color"] = normalize_hex(data["brand_color"], base["brand_color"])
    for key, (lo, hi) in _INT_KEYS.items():
        if data.get(key) is not None:
            out[key] = _clamp_int(data[key], lo, hi, base[key])
    for key in _BOOL_KEYS:
        if data.get(key) is not None:
            out[key] = bool(data[key])
    return out


def get_store_overrides(store_id):
    """Raw per-store row (NULLs preserved) or {} when none is saved."""
    if not store_id:
        return {}
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM blog_visual_settings WHERE store_id = %s", (store_id,))
            row = cur.fetchone()
    except Exception:
        logger.warning("blog_visual_settings read failed for store %s", store_id, exc_info=True)
        return {}
    if not row:
        return {}
    return {k: row.get(k) for k in VISUAL_KEYS}


def get_article_visuals(store_id):
    """Effective visuals config for a store (defaults merged with overrides)."""
    return normalize_visuals(get_store_overrides(store_id))


def save_store_visuals(store_id, data):
    """Upsert per-store overrides. Values are validated; keys absent from
    `data` are left untouched. Returns the effective config."""
    current = get_store_overrides(store_id)
    merged = normalize_visuals(data, base=normalize_visuals(current))
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO blog_visual_settings
                (store_id, default_style, brand_color, inline_image_count, max_images_per_article,
                 product_composite, visual_blocks, toc, related_limit, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (store_id) DO UPDATE SET
                default_style = EXCLUDED.default_style,
                brand_color = EXCLUDED.brand_color,
                inline_image_count = EXCLUDED.inline_image_count,
                max_images_per_article = EXCLUDED.max_images_per_article,
                product_composite = EXCLUDED.product_composite,
                visual_blocks = EXCLUDED.visual_blocks,
                toc = EXCLUDED.toc,
                related_limit = EXCLUDED.related_limit,
                updated_at = NOW()""",
            (
                store_id,
                merged["default_style"],
                merged["brand_color"],
                merged["inline_image_count"],
                merged["max_images_per_article"],
                merged["product_composite"],
                merged["visual_blocks"],
                merged["toc"],
                merged["related_limit"],
            ),
        )
    return merged


def visuals_with_warnings(visuals):
    return {**visuals, "link_color_warnings": link_color_warnings(visuals["brand_color"])}
