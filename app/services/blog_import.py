"""Blog auto mode: parse a hand-written brief file and lock its fields.

A brief file holds one or more briefs, each a topic line followed by labelled
fields (H1, SEO Title, Meta Description, Keywords, URL Slug). The file's text
is authoritative: the pipeline writes around it, never over it. Every step
that can change one of these fields calls `apply_locked_fields`.
"""

import re

from app.services.ai_content_validator import META_DESC_MAX, META_TITLE_MAX, find_violations

SHOPIFY_TITLE_MAX = 255

LOCKABLE_FIELDS = ("title", "meta_title", "meta_description", "handle")

# Locked-field name -> key in the outline result / generated article.
OUTLINE_KEYS = {"title": "suggested_title", "meta_title": "meta_title", "meta_description": "meta_description"}
ARTICLE_KEYS = {"title": "title", "meta_title": "meta_title", "meta_description": "meta_description", "handle": "handle"}

_LABELS = {
    "h1": "title",
    "title": "title",
    "article title": "title",
    "seo title": "meta_title",
    "meta title": "meta_title",
    "meta description": "meta_description",
    "description": "meta_description",
    "keywords": "keywords",
    "keyword": "keywords",
    "url slug": "handle",
    "slug": "handle",
    "handle": "handle",
    "url": "handle",
}

_FIELD_RE = re.compile(
    r"^\s*(?:[-*+]\s+|#+\s*)?[*_]*\s*(" + "|".join(sorted(_LABELS, key=len, reverse=True)) + r")\s*[*_]*\s*:\s*[*_]*\s*(.*?)\s*[*_]*\s*$",
    re.IGNORECASE,
)
_NUMBERED_RE = re.compile(r"^\s*#*\s*\d+[.)]\s+(.+?)\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


def _strip_emphasis(text):
    return re.sub(r"^[*_]+|[*_]+$", "", text.strip()).strip()


def _split_keywords(value):
    return [k.strip() for k in re.split(r"[,;\n]", value or "") if k.strip()]


def dedupe_keywords(keywords):
    seen, out = set(), []
    for kw in keywords:
        key = kw.casefold()
        if key not in seen:
            seen.add(key)
            out.append(kw)
    return out


def _new_brief(topic=""):
    return {"topic": topic, "title": "", "meta_title": "", "meta_description": "", "handle": "", "keywords": []}


def parse_blog_briefs(text):
    """Split a brief file into briefs. Returns [{topic, title, meta_title,
    meta_description, handle, keywords}] in file order; fields the file does
    not give stay empty. A numbered line or markdown heading starts a brief; so
    does a second H1 inside one brief (files without topic lines)."""
    briefs = []
    current = None
    last_label = None
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        field = _FIELD_RE.match(raw)
        if field:
            key = _LABELS[field.group(1).lower()]
            value = _strip_emphasis(field.group(2))
            if current is None or (key == "title" and current["title"]):
                current = _new_brief()
                briefs.append(current)
            if key == "keywords":
                current["keywords"].extend(_split_keywords(value))
            else:
                current[key] = value
            last_label = key
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet and current is not None and last_label == "keywords":
            current["keywords"].extend(_split_keywords(bullet.group(1)))
            continue
        topic = _NUMBERED_RE.match(raw) or _HEADING_RE.match(raw)
        if topic:
            current = _new_brief(_strip_emphasis(topic.group(1)))
            briefs.append(current)
            last_label = None
    for b in briefs:
        b["keywords"] = dedupe_keywords(b["keywords"])
    return [b for b in briefs if any(b[k] for k in LOCKABLE_FIELDS) or b["keywords"]]


def normalize_handle(slug):
    """Shopify-safe handle: lowercase a-z0-9 joined by single hyphens."""
    cleaned = (slug or "").strip().lower().replace("'", "").replace("’", "")
    cleaned = re.sub(r"^https?://[^/]+", "", cleaned).strip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")


def check_brief(brief):
    """Normalize a parsed brief and attach `errors` (row cannot be imported)
    and `warnings` (imported as written). Returns a new dict."""
    out = dict(brief)
    errors, warnings = [], []
    if not out.get("title"):
        errors.append("Missing H1")
    elif len(out["title"]) > SHOPIFY_TITLE_MAX:
        errors.append(f"H1 is {len(out['title'])} characters; Shopify allows {SHOPIFY_TITLE_MAX}")
    if not out.get("keywords"):
        errors.append("Missing keywords")

    if out.get("handle"):
        handle = normalize_handle(out["handle"])
        if not handle:
            errors.append(f"URL slug '{out['handle']}' has no usable characters")
        elif handle != out["handle"]:
            warnings.append(f"URL slug normalized to '{handle}'")
        out["handle"] = handle

    primary = (out.get("keywords") or [None])[0]
    for v in find_violations(
        {k: out.get(k) for k in ("title", "meta_title", "meta_description")}, primary
    ):
        if v["kind"] == "length" and v["field"] == "meta_title":
            warnings.append(f"SEO title is {v['length']} characters (over {META_TITLE_MAX})")
        elif v["kind"] == "length":
            warnings.append(f"Meta description is {v['length']} characters (over {META_DESC_MAX})")
        elif v["kind"] == "title_keyword":
            warnings.append(f"H1 doesn't contain the primary keyword “{primary}”")
        elif v["kind"] == "meta_keyword_missing":
            label = "SEO title" if v["field"] == "meta_title" else "Meta description"
            warnings.append(f"{label} doesn't contain the primary keyword “{primary}”")
    for key, label in (("meta_title", "SEO title"), ("meta_description", "Meta description"), ("handle", "URL slug")):
        if not out.get(key):
            warnings.append(f"No {label} in the file; the AI writes it")
    out["errors"] = errors
    out["warnings"] = warnings
    return out


def locked_fields_of(brief):
    """The lockable fields a brief actually sets."""
    return {k: brief[k] for k in LOCKABLE_FIELDS if brief.get(k)}


def locked_article_keys(locked):
    """Generated-article keys the quality gate must leave alone."""
    return {ARTICLE_KEYS[k] for k in ("title", "meta_title", "meta_description") if (locked or {}).get(k)}


def apply_locked_fields(article, locked, key_map=ARTICLE_KEYS):
    """Copy of `article` with every locked value written over its key."""
    if not isinstance(article, dict):
        return article
    out = dict(article)
    for field, key in key_map.items():
        value = (locked or {}).get(field)
        if isinstance(value, str) and value:
            out[key] = value
    return out
