"""News-flavoured wrappers over article_media (kept for import stability).

The token → <figure> mechanics live in app.services.article_media; news
articles only differ in the figure CSS class.
"""

import html
from functools import partial
from urllib.parse import urlparse

from app.services.article_media import (  # noqa: F401 — re-exported
    NEWS_FIGURE_CLASS,
    TOKEN_RE,
    fallback_insert as _fallback_insert,
    strip_leftover_tokens as _strip_leftover_tokens,
)
from app.services.article_media import figure_html as _generic_figure_html
from app.services.article_media import insert_inline_images as _generic_insert_inline_images

_figure_html = partial(_generic_figure_html, css_class=NEWS_FIGURE_CLASS)
insert_inline_images = partial(_generic_insert_inline_images, figure_class=NEWS_FIGURE_CLASS)


def _source_host(url):
    href = url if url.startswith(("http://", "https://")) else "https://" + url
    try:
        host = (urlparse(href).hostname or "").lower()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


def _store_hosts(store):
    hosts = set()
    for key in ("primary_domain", "domain"):
        host = ((store or {}).get(key) or "").strip().lower()
        host = host.replace("https://", "").replace("http://", "").split("/")[0]
        if host.startswith("www."):
            host = host[4:]
        if host:
            hosts.add(host)
    return hosts


def clean_sources(sources, store=None):
    """Normalise a sources list to [{url, title}] fit for the published Sources section.

    Drops blanks, links to the store's own storefront (primary or myshopify
    domain, any subdomain), and entries that would render with the same anchor
    text — untitled sources show their hostname, so several pages from one
    site read as duplicates; titled sources are distinct when their titles are.
    """
    own_hosts = _store_hosts(store)
    seen = set()
    out = []
    for s in sources or []:
        if isinstance(s, dict):
            u = (s.get("url") or "").strip()
            title = (s.get("title") or "").strip()
        else:
            u = (s or "").strip()
            title = ""
        if not u:
            continue
        host = _source_host(u)
        if not host or any(host == own or host.endswith("." + own) for own in own_hosts):
            continue
        key = (host, title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": u, "title": title})
    return out


def render_sources_html(sources, store=None):
    """Render a 'Sources' section from a list of sources.

    Each item is either a URL string (legacy) or a ``{"url", "title"}`` dict;
    the list is passed through ``clean_sources`` first. Anchor text is the
    descriptive title when present, otherwise the hostname. External links use
    rel="nofollow noopener" and open in a new tab. Returns '' when there are no
    usable sources.
    """
    items = []
    for s in clean_sources(sources, store):
        u = s["url"]
        title = s["title"]
        href = u if u.startswith(("http://", "https://")) else "https://" + u
        if title:
            display = title
        else:
            try:
                display = urlparse(href).netloc or u
            except Exception:
                display = u
        items.append(
            f'<li><a href="{html.escape(href, quote=True)}" target="_blank" '
            f'rel="nofollow noopener">{html.escape(display)}</a></li>'
        )
    if not items:
        return ""
    return '<h2 class="a2z-news__sources-title">Sources</h2>\n<ul class="a2z-news__sources">' + "".join(items) + "</ul>"
