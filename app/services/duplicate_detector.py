"""Detect duplicate SEO titles / meta descriptions across synced products and collections.

Pure functions (no DB access) so they stay unit-testable. Callers pass a flat list
of item dicts already loaded from the DB.
"""

import re

_WHITESPACE = re.compile(r"\s+")

DUPLICATE_FIELDS = ("seo_title", "meta_description")


def normalize(value):
    """Case-insensitive, whitespace-collapsed key used to group colliding values."""
    if not value:
        return ""
    return _WHITESPACE.sub(" ", value.strip()).casefold()


def effective_title(item):
    """The value Shopify actually renders in <title>: SEO title, else the display name.

    A blank meta_title falls back to the product/collection title, so two items with
    no SEO title but the same name still produce duplicate <title> tags.
    """
    return (item.get("meta_title") or "").strip() or (item.get("title") or "").strip()


def _group_value(item, field):
    if field == "seo_title":
        return effective_title(item)
    return (item.get(field) or "").strip()


def find_duplicate_groups(items, field):
    """Group items that collide on `field`, scoped per store.

    field: "seo_title" (keys on effective title) or "meta_description".
    Empty values never form a group. Returns groups (count >= 2) sorted by count desc,
    then by the shared value for stable ordering.
    """
    buckets = {}
    for item in items:
        raw = _group_value(item, field)
        key = normalize(raw)
        if not key:
            continue
        buckets.setdefault((item["store_id"], key), []).append((raw, item))

    groups = []
    for (store_id, _key), members in buckets.items():
        if len(members) < 2:
            continue
        # Display the first non-blank raw value as the representative.
        value = members[0][0]
        groups.append(
            {
                "field": field,
                "value": value,
                "count": len(members),
                "store_id": store_id,
                "items": [m[1] for m in members],
            }
        )

    groups.sort(key=lambda g: (-g["count"], g["value"].casefold()))
    return groups
