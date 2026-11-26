"""What a page looks like as a Google result: the path, the title and the
description Shopify serves. Pure; callers load the rows."""

import re

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

PATH_PREFIX = {"product": "/products/", "collection": "/collections/", "page": "/pages/"}


def item_path(item_type, handle, blog_handle=None):
    if not handle:
        return None
    if item_type == "article":
        return f"/blogs/{blog_handle}/{handle}" if blog_handle else None
    prefix = PATH_PREFIX.get(item_type)
    return prefix + handle if prefix else None


def serp_preview(item_type, row, blog_handle=None):
    """{path, title, description, own_description} for one synced row.
    own_description is False when Google would have to write its own."""
    meta_title = (row.get("meta_title") or row.get("seo_title") or "").strip()
    meta_desc = (row.get("meta_description") or row.get("seo_description") or "").strip()
    return {
        "path": item_path(item_type, row.get("handle"), blog_handle),
        "title": meta_title or (row.get("title") or "").strip(),
        "description": _SPACE.sub(" ", _TAGS.sub(" ", meta_desc)).strip(),
        "own_description": bool(meta_desc),
    }
