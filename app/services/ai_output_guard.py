"""Validation and sanitization for AI-generated content before it is written to Shopify.

Every apply path that pushes LLM output to the live store goes through these
helpers so a verbose, truncated, or hostile model response can't corrupt
storefront content.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Hard caps aligned with the scorer's "good" bands (title [50,60], description
# [120,160]) — generation now aims ~50/~150 and rephrases overshoots, so anything
# still longer at apply time is clamped rather than shipped in the "ok" band.
MAX_META_TITLE = 60
MAX_META_DESCRIPTION = 160
# Shopify's platform limit for the product/collection title field. Titles are
# never SEO-truncated (that would mangle merchandising) — this is corruption
# protection only.
MAX_TITLE = 255

_SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_EVENT_ATTR_RE = re.compile(
    r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_JS_URL_RE = re.compile(
    r"\s+(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'\s>]*\2", re.IGNORECASE
)
# The theme renders the product/collection title as the page's H1 — an <h1> in
# a description would be a duplicate H1, so demote (not strip: no content loss).
_H1_OPEN_RE = re.compile(r"<\s*h1(\s[^>]*)?>", re.IGNORECASE)
_H1_CLOSE_RE = re.compile(r"<\s*/\s*h1\s*>", re.IGNORECASE)


def demote_h1(html):
    """Rewrite <h1> tags to <h2> in description HTML."""
    if not html:
        return html
    return _H1_CLOSE_RE.sub("</h2>", _H1_OPEN_RE.sub(lambda m: f"<h2{m.group(1) or ''}>", html))


def sanitize_html(html):
    """Strip active content (script/style blocks, inline event handlers,
    javascript: URLs) from AI-generated HTML and demote any <h1> to <h2>.
    Returns "" for falsy input."""
    if not html:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub("", html)
    cleaned = _EVENT_ATTR_RE.sub("", cleaned)
    cleaned = _JS_URL_RE.sub("", cleaned)
    cleaned = demote_h1(cleaned)
    return cleaned.strip()


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def restore_missing_images(original_html, new_html):
    """Re-insert <img> tags that existed in the original description but are
    entirely absent from an AI rewrite. Injected hero images live in the
    description body precisely because some themes render collection.image
    nowhere else — a rewrite that drops them strips the page's only visual.
    Restores in original order at the top (where hero injection places them);
    if the rewrite kept ANY image, placement is left alone."""
    if not original_html or not new_html:
        return new_html
    originals = _IMG_TAG_RE.findall(original_html)
    if not originals or _IMG_TAG_RE.search(new_html):
        return new_html
    return "".join(originals) + new_html


def clamp_title(text):
    return _clamp(text, MAX_TITLE)


def _clamp(text, limit):
    if not text or len(text) <= limit:
        return text
    truncated = text[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return truncated if len(truncated) <= limit else text[:limit]


def clamp_meta_title(text):
    return _clamp(text, MAX_META_TITLE)


def clamp_meta_description(text):
    return _clamp(text, MAX_META_DESCRIPTION)


def guard_shopify_fields(fields):
    """Clamp meta lengths and sanitize AI-authored HTML in a Shopify field
    dict (in place). Safe on partial dicts — only touches present keys."""
    if fields.get("title"):
        fields["title"] = clamp_title(fields["title"])
    for key in ("meta_title", "seo_title"):
        if fields.get(key):
            fields[key] = clamp_meta_title(fields[key])
    for key in ("meta_description", "seo_description"):
        if fields.get(key):
            fields[key] = clamp_meta_description(fields[key])
    for key in ("description_html", "body_html", "body"):
        if fields.get(key):
            fields[key] = sanitize_html(fields[key])
    return fields


def validate_injected_html(original_html, new_html, expected_hrefs=None, min_length_ratio=0.8):
    """Check that an AI-rewritten HTML body is a plausible edit of the original.

    Guards the interlinking flows, where a model refusal or truncated response
    would otherwise replace (and locally overwrite) the live description.
    Returns (ok, reason).
    """
    if not new_html or not new_html.strip():
        return False, "AI returned empty content"

    for href in expected_hrefs or []:
        # Exact attribute match, not substring — a hallucinated URL that merely
        # starts with the expected one (e.g. /collections/x-extra-words) must
        # not pass as /collections/x.
        if href and not re.search(
            r"href\s*=\s*[\"']" + re.escape(href) + r"/?[\"']", new_html, re.IGNORECASE
        ):
            return False, f"AI output is missing the requested link ({href})"

    original_len = len(original_html or "")
    if original_len > 200 and len(new_html) < original_len * min_length_ratio:
        return False, (
            f"AI output is suspiciously short ({len(new_html)} vs {original_len} chars) — "
            "refusing to overwrite the original"
        )

    return True, ""


_FIGURE_BLOCK_RE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)
_IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_H2_OPEN_RE = re.compile(r"<h2\b[^>]*>", re.IGNORECASE)


def restore_missing_figures(original_html, new_html):
    """Per-figure guard for article rewrites: any <figure> from the original
    whose image src is missing from the rewrite is re-inserted just before the
    first H2 that followed it in the original (so it stays in its section);
    falls back to the FAQ section start, then to the end of the body.

    Unlike restore_missing_images (all-or-nothing, collection heroes), this
    tracks each inline figure individually — a rewrite that keeps the hero but
    drops two of three inline figures still gets them back."""
    if not original_html or not new_html:
        return new_html
    figures = list(_FIGURE_BLOCK_RE.finditer(original_html))
    if not figures:
        return new_html
    out = new_html
    for fig in figures:
        block = fig.group(0)
        src_m = _IMG_SRC_RE.search(block)
        if not src_m:
            continue
        src = src_m.group(1)
        if src in out:
            continue
        # Heading text of the next H2 after the figure in the original.
        after = original_html[fig.end():]
        h2 = re.search(r"<h2\b[^>]*>(.*?)</h2>", after, re.IGNORECASE | re.DOTALL)
        anchor_text = re.sub(r"<[^>]+>", "", h2.group(1)).strip() if h2 else ""
        faq = re.search(r'<section\b[^>]*class="[^"]*a2z-blog__faq', out, re.IGNORECASE)
        faq_start = faq.start() if faq else len(out)
        inserted = False
        if anchor_text:
            for m in re.finditer(r"<h2\b[^>]*>(.*?)</h2>", out, re.IGNORECASE | re.DOTALL):
                if re.sub(r"<[^>]+>", "", m.group(1)).strip() == anchor_text:
                    # Never land inside the FAQ section — stop at its opening tag.
                    pos = min(m.start(), faq_start)
                    out = out[:pos] + block + out[pos:]
                    inserted = True
                    break
        if not inserted:
            out = out[:faq_start] + block + out[faq_start:]
    return out


_HEADING_RE = re.compile(r"<h([2-4])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)


def empty_sections(html):
    """Headings (h2-h4) with nothing under them before the next heading of
    the same or a higher level: a sign the model dropped the section's text.
    A subheading with content counts as content. Returns the heading texts."""
    html = html or ""
    heads = list(_HEADING_RE.finditer(html))
    empty = []
    for i, h in enumerate(heads):
        level = int(h.group(1))
        end = len(html)
        for nxt in heads[i + 1:]:
            if int(nxt.group(1)) <= level:
                end = nxt.start()
                break
        body = html[h.end():end]
        if re.search(r"<img\b", body, re.IGNORECASE) or re.sub(r"<[^>]+>", "", body).strip():
            continue
        empty.append(re.sub(r"<[^>]+>", "", h.group(2)).strip())
    return empty


def drop_empty_headings(html, keep=()):
    """Remove headings that have nothing under them (see empty_sections),
    except those whose text is in keep. Used after a cleanup step deleted a
    section's only paragraph, so no bare heading is left behind."""
    html = html or ""
    keep = set(keep)
    while True:
        targets = [h for h in empty_sections(html) if h not in keep]
        if not targets:
            return html
        target = targets[0]
        for m in _HEADING_RE.finditer(html):
            if re.sub(r"<[^>]+>", "", m.group(2)).strip() == target:
                html = html[:m.start()] + html[m.end():]
                break
        else:
            return html
