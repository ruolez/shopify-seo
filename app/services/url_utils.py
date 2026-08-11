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


def store_host(store):
    """Public storefront host: the primary (custom) domain when set, else the
    myshopify domain. Schema/canonical URLs must always use this — the
    myshopify host redirects and breaks @id entity identity."""
    host = ((store or {}).get("primary_domain") or (store or {}).get("domain") or "").strip()
    return host.replace("https://", "").replace("http://", "").rstrip("/")


def store_base_url(store):
    host = store_host(store)
    return f"https://{host}" if host else None


def gsc_base_url(gsc_property, store=None):
    """Base URL for joining against gsc_daily_metrics.page, derived from a
    Search Console property.

    URL-prefix properties ("https://example.com/") are usable directly.
    Domain properties ("sc-domain:example.com") carry no scheme/host — naive
    concatenation built pages like "sc-domain:example.com/products/x" that
    never match, silently zeroing GSC demand. Fall back to the store's public
    host when it belongs to the domain, else https://<domain>. Returns "" for
    empty input (callers treat that as "no GSC")."""
    prop = (gsc_property or "").strip().rstrip("/")
    if not prop:
        return ""
    if prop.startswith("sc-domain:"):
        domain = prop[len("sc-domain:"):].strip("/")
        host = store_host(store) if store else ""
        if host and (host == domain or host.endswith("." + domain)):
            return f"https://{host}"
        return f"https://{domain}" if domain else ""
    return prop


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
