"""Insert generated inline images into a news article body.

The news_analyze task returns body HTML containing standalone placeholder
tokens like ``[[INLINE_IMAGE_1]]`` (ideally on their own line between
paragraphs), plus a parallel list describing each image. After the images are
generated and uploaded to the Shopify CDN, we swap each token for a
``<figure>``. Any token that has no successfully-uploaded image is stripped so
no broken markers ever ship.
"""

import html
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


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

TOKEN_RE = re.compile(r"\[\[INLINE_IMAGE_(\d+)\]\]")


def _figure_html(img):
    """Build a <figure> for one uploaded inline image."""
    src = html.escape(img.get("cdn_url", ""), quote=True)
    alt = html.escape(img.get("alt", "") or "", quote=True)
    caption = img.get("caption") or ""
    width = img.get("width") or 1024
    height = img.get("height") or 768
    figcaption = f"<figcaption>{html.escape(caption)}</figcaption>" if caption.strip() else ""
    return (
        f'<figure class="a2z-news__figure">'
        f'<img src="{src}" alt="{alt}" width="{width}" height="{height}" '
        f'loading="lazy" decoding="async">'
        f"{figcaption}</figure>"
    )


def _token_pattern(n):
    """Regex matching a token, absorbing a wrapping <p>…</p> if the AI used one."""
    return re.compile(
        r"(?:<p>\s*)?\[\[INLINE_IMAGE_" + str(n) + r"\]\](?:\s*</p>)?",
        re.IGNORECASE,
    )


def _strip_leftover_tokens(body):
    """Remove any unmatched [[INLINE_IMAGE_n]] tokens (and a wrapping <p>)."""
    return re.sub(
        r"(?:<p>\s*)?\[\[INLINE_IMAGE_\d+\]\](?:\s*</p>)?",
        "",
        body,
        flags=re.IGNORECASE,
    )


def _fallback_insert(body, figures):
    """Insert figures at evenly-spaced paragraph boundaries when the body has no
    usable tokens. Splits on </p>; if there are too few paragraphs, appends."""
    parts = re.split(r"(</p>)", body, flags=re.IGNORECASE)
    # Reassemble paragraph units: text + its closing </p>
    units = []
    buf = ""
    for seg in parts:
        buf += seg
        if seg.lower() == "</p>":
            units.append(buf)
            buf = ""
    if buf:
        units.append(buf)

    n = len(figures)
    if len(units) < 2:
        return body + "".join(figures)

    # Distribute figures after paragraphs at roughly even positions.
    result = list(units)
    inserts = {}
    for k, fig in enumerate(figures, start=1):
        idx = round(k * len(units) / (n + 1)) - 1
        idx = max(0, min(idx, len(units) - 1))
        inserts.setdefault(idx, []).append(fig)
    out = []
    for i, unit in enumerate(result):
        out.append(unit)
        for fig in inserts.get(i, []):
            out.append(fig)
    return "".join(out)


def insert_inline_images(body_html, images):
    """Return body_html with inline-image tokens replaced by <figure> elements.

    images: list of dicts. Only entries with a truthy ``cdn_url`` are rendered.
    Each may include: token, cdn_url, alt, caption, width, height.
    """
    body = body_html or ""
    usable = [img for img in (images or []) if img.get("cdn_url")]

    matched_any = False
    for img in usable:
        token = img.get("token") or ""
        m = TOKEN_RE.search(token)
        if not m:
            continue
        n = m.group(1)
        pattern = _token_pattern(n)
        if pattern.search(body):
            body, count = pattern.subn(_figure_html(img), body, count=1)
            if count:
                matched_any = True

    # Fallback: AI gave us images but forgot the tokens.
    if not matched_any and usable and not TOKEN_RE.search(body):
        figures = [_figure_html(img) for img in usable]
        body = _fallback_insert(body, figures)

    return _strip_leftover_tokens(body)
