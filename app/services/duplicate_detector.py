"""Detect duplicate SEO titles / meta descriptions across items of any page type
(products, collections, blog/news articles, pages).

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


MIN_BODY_WORDS = 60
SIMILAR_BODY_THRESHOLD = 0.7
_SHINGLE = 4
_TAGS = re.compile(r"<[^>]+>")
_WORDS = re.compile(r"[a-z0-9']+")


def _shingles(body_html):
    words = _WORDS.findall(_TAGS.sub(" ", body_html or "").lower())
    if len(words) < MIN_BODY_WORDS:
        return None
    return {" ".join(words[i:i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)}


def find_similar_bodies(items, threshold=SIMILAR_BODY_THRESHOLD):
    """Groups of same-type items whose body text is nearly the same (Jaccard
    over 4-word runs). Templated or manufacturer-copied descriptions are the
    usual reason Google reads a page and declines to index it. Items need
    "type" and "body"; the body is dropped from the result. Largest group
    first; similarity is the lowest pairwise match that joined the group, 0-100.
    """
    by_type = {}
    for item in items:
        sh = _shingles(item.get("body"))
        if sh:
            by_type.setdefault(item["type"], []).append((item, sh))

    groups = []
    for members in by_type.values():
        parent = list(range(len(members)))
        weakest = {}

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i][1], members[j][1]
                # Sets this different in size cannot reach the threshold.
                if min(len(a), len(b)) / max(len(a), len(b)) < threshold:
                    continue
                score = len(a & b) / len(a | b)
                if score >= threshold:
                    ri, rj = find(i), find(j)
                    low = min(score, weakest.pop(ri, 1.0), weakest.pop(rj, 1.0))
                    parent[rj] = ri
                    weakest[find(i)] = low

        clusters = {}
        for idx in range(len(members)):
            clusters.setdefault(find(idx), []).append(idx)
        for root, idxs in clusters.items():
            if len(idxs) < 2:
                continue
            groups.append({
                "count": len(idxs),
                "similarity": round(weakest.get(root, 1.0) * 100),
                "items": [{k: v for k, v in members[i][0].items() if k != "body"} for i in idxs],
            })
    groups.sort(key=lambda g: (-g["count"], -g["similarity"]))
    return groups
