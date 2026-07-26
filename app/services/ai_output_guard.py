"""Validation and sanitization for AI-generated content before it is written to Shopify.

Every apply path that pushes LLM output to the live store goes through these
helpers so a verbose, truncated, or hostile model response can't corrupt
storefront content.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Hard caps — safety net above the SEO-ideal lengths (title ~60, description ~160),
# so intentionally longer AI output still fits what Shopify/search engines tolerate.
MAX_META_TITLE = 70
MAX_META_DESCRIPTION = 200

_SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_EVENT_ATTR_RE = re.compile(
    r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
_JS_URL_RE = re.compile(
    r"\s+(href|src)\s*=\s*([\"']?)\s*javascript:[^\"'\s>]*\2", re.IGNORECASE
)


def sanitize_html(html):
    """Strip active content (script/style blocks, inline event handlers,
    javascript: URLs) from AI-generated HTML. Returns "" for falsy input."""
    if not html:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub("", html)
    cleaned = _EVENT_ATTR_RE.sub("", cleaned)
    cleaned = _JS_URL_RE.sub("", cleaned)
    return cleaned.strip()


def _clamp(text, limit):
    if not text or len(text) <= limit:
        return text
    truncated = text[: limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return truncated if len(truncated) <= limit else text[:limit]


def clamp_meta_title(text):
    return _clamp(text, MAX_META_TITLE)


def clamp_meta_description(text):
    return _clamp(text, MAX_META_DESCRIPTION)


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
