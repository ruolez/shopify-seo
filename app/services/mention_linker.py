"""Deterministic exact-mention internal linking for article HTML.

The AI link pass decides which candidates to link, so a brand named five
times in an article can still ship unlinked. This pass runs after the AI:
any candidate whose title appears verbatim in the article's visible text
gets its first eligible mention wrapped in a link — no model judgment
involved, so the guarantee holds regardless of AI behavior.

Eligible mention = a text node outside headings, existing links, and
figcaptions. One link per destination, skipping destinations the article
already links, and never exceeding the overall internal-link cap.
"""

import logging
import re

logger = logging.getLogger(__name__)

_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")
_TAG_NAME_RE = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9]*)")
_NO_LINK_TAGS = {"a", "h1", "h2", "h3", "h4", "h5", "h6", "figcaption"}
_INTERNAL_HREF_RE = re.compile(r'<a\s[^>]*href\s*=\s*["\'](/[^"\']*)["\']', re.IGNORECASE)


def _phrase_pattern(title):
    """Word-bounded, case-insensitive pattern for `title` inside HTML text,
    tolerant of entity-encoded ampersands and flexible whitespace."""
    words = (title or "").split()
    if not words:
        return None
    parts = [re.escape(w).replace(r"\&", "&").replace("&", "(?:&amp;|&)") for w in words]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE)


def find_verbatim_mentions(body_html, candidates):
    """Return the candidates whose full title appears within a single text node,
    mirroring what link_unlinked_mentions could actually link — a title split
    across tags ("<p>Captain</p><p>Black</p>") is not a mention."""
    text_nodes = _TAG_SPLIT_RE.split(body_html or "")[::2]
    out = []
    for cand in candidates:
        pattern = _phrase_pattern((cand.get("title") or "").strip())
        if pattern and any(pattern.search(node) for node in text_nodes):
            out.append(cand)
    return out


def _link_first_mention(segments, pattern, url):
    """Wrap the first eligible occurrence of `pattern` in an <a> tag, in place.
    Segments alternate text/tag (odd indices are tags, from _TAG_SPLIT_RE.split)."""
    no_link_depth = 0
    for i, seg in enumerate(segments):
        if i % 2 == 1:
            m = _TAG_NAME_RE.match(seg)
            if m and m.group(1).lower() in _NO_LINK_TAGS:
                if seg.startswith("</"):
                    no_link_depth = max(0, no_link_depth - 1)
                elif not seg.endswith("/>"):
                    no_link_depth += 1
            continue
        if no_link_depth:
            continue
        m = pattern.search(seg)
        if not m:
            continue
        segments[i] = (
            seg[: m.start()] + f'<a href="{url}">' + m.group(0) + "</a>" + seg[m.end() :]
        )
        return True
    return False


def link_unlinked_mentions(body_html, candidates, max_total_links=12, min_words=2):
    """Link the first eligible mention of each unlinked candidate title.

    `candidates` is a priority-ordered list of {title, url} — put collections
    before products so a brand mention resolves to the brand collection.
    Single-word titles are skipped by default (`min_words=2`): words like
    "Sale" or "Cigarillos" match generic prose too easily for a pass with no
    judgment; the AI step still handles those.

    Returns (new_html, added) where `added` lists {title, url} actually linked.
    """
    segments = _TAG_SPLIT_RE.split(body_html or "")
    linked_hrefs = {m.group(1) for m in _INTERNAL_HREF_RE.finditer(body_html or "")}
    total = len(re.findall(r'<a\s[^>]*href\s*=\s*["\']/', body_html or "", re.IGNORECASE))
    added = []

    for cand in candidates:
        if total >= max_total_links:
            break
        url = (cand.get("url") or "").strip()
        title = (cand.get("title") or "").strip()
        if not url or not title or len(title.split()) < min_words or url in linked_hrefs:
            continue
        pattern = _phrase_pattern(title)
        if pattern and _link_first_mention(segments, pattern, url):
            linked_hrefs.add(url)
            total += 1
            added.append({"title": title, "url": url})

    return "".join(segments), added
