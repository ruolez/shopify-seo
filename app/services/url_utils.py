"""URL normalization shared across data sources.

GSC properties, Semrush exports, and Shopify handles all describe the same
pages with different URL shapes (http/https, www, trailing slashes, query
strings). Matching by naive string replacement broke whenever two sources
disagreed on the prefix — normalize everything to a bare path key instead.
"""

import re
from urllib.parse import unquote, urlparse

_LOCALE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)
_SHOPIFY_ROOTS = ("products", "collections", "pages", "blogs")


def url_path_key(url):
    """Normalize an absolute or relative URL to a path key like
    'products/some-handle' — insensitive to scheme, host, www, query,
    fragment, trailing slashes, and a leading locale prefix (/en/...)."""
    if not url:
        return ""
    u = str(url).strip()
    if "://" in u or u.startswith("//"):
        path = urlparse(u).path
    else:
        path = u
    path = unquote(path.split("?")[0].split("#")[0]).strip("/")
    parts = path.split("/")
    if len(parts) > 1 and _LOCALE_RE.fullmatch(parts[0]) and parts[1] in _SHOPIFY_ROOTS:
        parts = parts[1:]
    return "/".join(parts)
