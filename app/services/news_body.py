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


def render_sources_html(sources):
    """Render a 'Sources' section from a list of sources.

    Each item is either a URL string (legacy) or a ``{"url", "title"}`` dict.
    Anchor text is the descriptive title when present, otherwise the hostname.
    External links use rel="nofollow noopener" and open in a new tab. Returns
    '' when there are no usable sources.
    """
    items = []
    for s in sources or []:
        if isinstance(s, dict):
            u = (s.get("url") or "").strip()
            title = (s.get("title") or "").strip()
        else:
            u = (s or "").strip()
            title = ""
        if not u:
            continue
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
