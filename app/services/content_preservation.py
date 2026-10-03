"""Hand a rewriting model the content it is about to replace.

Description generators used to see only the current description's word count,
so a full optimize silently erased whatever other tools had added — spec
tables, verdict sections, injected internal links. Every generator that
rewrites a body now gets the existing HTML and one shared preservation rule.
"""

import re

MIN_WORDS_WORTH_PRESERVING = 40
MAX_EXISTING_CHARS = 20000

_TAG_RE = re.compile(r"<[^>]+>")


def existing_description_block(current_html, preserve_rule):
    html = (current_html or "").strip()
    if len(_TAG_RE.sub(" ", html).split()) < MIN_WORDS_WORTH_PRESERVING:
        return None
    return (
        "CURRENT DESCRIPTION (HTML) — revise and extend this; do not start over:\n"
        f"{html[:MAX_EXISTING_CHARS]}\n\n"
        f"{preserve_rule}"
    )


_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
_HREF_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\'#]+)', re.IGNORECASE)
_HEADING_RE = re.compile(r"<h[2-4]\b[^>]*>(.*?)</h[2-4]>", re.IGNORECASE | re.DOTALL)
_INTERNAL_PATH_RE = re.compile(r"^(?:https?://[^/]+)?(/(?:collections|products|blogs|pages)/[^?#\s]+)", re.IGNORECASE)

# Sections other tools add on purpose; a rewrite must not quietly remove them.
_PROTECTED_SECTIONS = (
    ("Specifications section", re.compile(r"specification|at[- ]a[- ]glance", re.IGNORECASE)),
    ("Expert Verdict section", re.compile(r"verdict", re.IGNORECASE)),
    ("FAQ section", re.compile(r"\bfaq\b|frequently asked", re.IGNORECASE)),
)


def _internal_paths(html):
    paths = []
    for href in _HREF_RE.findall(html):
        m = _INTERNAL_PATH_RE.match(href.strip())
        if m:
            path = m.group(1).rstrip("/").lower()
            if path not in paths:
                paths.append(path)
    return paths


def _heading_texts(html):
    return [_TAG_RE.sub("", h).strip() for h in _HEADING_RE.findall(html)]


def missing_preserved(old_html, new_html):
    """What a rewrite removed that the preservation rule protects, as short
    labels for the user. Empty list = nothing lost."""
    old_html, new_html = old_html or "", new_html or ""
    missing = []

    lost_tables = len(_TABLE_RE.findall(old_html)) - len(_TABLE_RE.findall(new_html))
    if lost_tables > 0:
        missing.append(f"{lost_tables} table{'s' if lost_tables != 1 else ''}")

    old_headings, new_headings = _heading_texts(old_html), _heading_texts(new_html)
    for label, pattern in _PROTECTED_SECTIONS:
        if any(pattern.search(h) for h in old_headings) and not any(pattern.search(h) for h in new_headings):
            missing.append(label)

    kept = set(_internal_paths(new_html))
    missing.extend(f"internal link {p}" for p in _internal_paths(old_html) if p not in kept)
    return missing


def count_internal_links(html):
    """Links to the store's own pages: relative hrefs, plus absolute hrefs to a
    storefront path (an absolute link to your own domain is still internal)."""
    count = 0
    for href in _HREF_RE.findall(html or ""):
        href = href.strip()
        if (href.startswith("/") and not href.startswith("//")) or _INTERNAL_PATH_RE.match(href):
            count += 1
    return count
