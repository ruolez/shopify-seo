"""Post-generation quality gate for blog article bodies.

Deterministic anchor-text lint (Google link guidance: descriptive, concise,
no generic anchors, no chained links, no links in headings, one link per
destination) with a single bounded LLM rephrase for the anchors that need
wording rather than removal. Fails open: any exception returns the input.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

GENERIC_ANCHORS = {
    "click here", "here", "read more", "learn more", "more", "this page", "this link",
    "this article", "link", "browse", "shop now", "buy now", "check it out", "see more",
    "find out more", "view", "visit", "website", "page",
}
MAX_ANCHOR_WORDS = 8

_A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<(h[1-6])\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ADJACENT_RE = re.compile(r"(</a>)(\s*(?:,|and|or|&amp;|/)?\s*)(<a\b)", re.IGNORECASE)


def _text(s):
    return _TAG_RE.sub("", s or "").strip()


def _norm_anchor(text):
    return re.sub(r"[^a-z0-9 ]+", "", _text(text).lower()).strip()


def _unwrap_links_in(fragment):
    return _A_RE.sub(lambda m: m.group(2), fragment)


def lint_anchor_text(body_html, protected_hrefs=None):
    """Return (fixed_html, violations).

    Auto-fixed: links inside headings, back-to-back links, duplicate hrefs
    (first occurrence kept). Flagged for rewrite (`needs_llm`): generic and
    over-long anchors. `protected_hrefs` (hand-picked product/collection
    links) are never unwrapped by the duplicate pass beyond the first copy.
    """
    body = body_html or ""
    violations = []
    try:
        # 1. Links inside headings → plain text.
        def _fix_heading(m):
            inner = m.group(2)
            if _A_RE.search(inner):
                for a in _A_RE.finditer(inner):
                    violations.append({"kind": "link_in_heading", "href": _href_of(a.group(1)), "text": _text(a.group(2)), "fixed": True})
                return f"<{m.group(1)}>{_unwrap_links_in(inner)}</{m.group(1)}>"
            return m.group(0)

        body = _HEADING_RE.sub(_fix_heading, body)

        # 2. Back-to-back links → unwrap the second.
        def _fix_adjacent(m):
            return m.group(1) + m.group(2) + "\x00UNWRAP\x00" + m.group(3)

        marked = _ADJACENT_RE.sub(_fix_adjacent, body)
        if "\x00UNWRAP\x00" in marked:
            def _unwrap_marked(m):
                attrs, inner = m.group(1), m.group(2)
                violations.append({"kind": "adjacent_links", "href": _href_of(attrs), "text": _text(inner), "fixed": True})
                return inner

            marked = re.sub(r"\x00UNWRAP\x00<a\b([^>]*)>(.*?)</a>", _unwrap_marked, marked, flags=re.IGNORECASE | re.DOTALL)
            body = marked.replace("\x00UNWRAP\x00", "")

        # 3. Duplicate hrefs → keep the first, unwrap the rest.
        seen = set()

        def _dedupe(m):
            href = _href_of(m.group(1))
            key = href.rstrip("/").lower()
            if key and key in seen:
                violations.append({"kind": "duplicate_href", "href": href, "text": _text(m.group(2)), "fixed": True})
                return m.group(2)
            if key:
                seen.add(key)
            return m.group(0)

        body = _A_RE.sub(_dedupe, body)

        # 4/5. Generic or over-long anchors → needs rewording.
        for m in _A_RE.finditer(body):
            text = _text(m.group(2))
            norm = _norm_anchor(text)
            words = len(norm.split())
            if not norm or norm in GENERIC_ANCHORS:
                violations.append({"kind": "generic_anchor", "href": _href_of(m.group(1)), "text": text, "fixed": False, "needs_llm": True})
            elif words > MAX_ANCHOR_WORDS:
                violations.append({"kind": "long_anchor", "href": _href_of(m.group(1)), "text": text, "fixed": False, "needs_llm": True})
    except Exception:
        logger.exception("lint_anchor_text failed — body passed through")
        return body_html, violations
    return body, violations


def _href_of(attrs):
    m = _HREF_RE.search(attrs or "")
    return m.group(1) if m else ""


def _sentence_around(body, start, end, radius=160):
    lo = max(0, start - radius)
    hi = min(len(body), end + radius)
    return _text(body[lo:hi])


def rewrite_anchors(client, model, body_html, violations, destinations=None):
    """One bounded LLM call that reworders only the flagged anchors.

    destinations: optional {href: title} so the model knows what each page is.
    Replacement is by exact <a> element; anything still generic afterwards is
    left in place (logged) — never unwrapped, since a weak anchor beats a
    missing internal link.
    """
    todo = [v for v in violations if v.get("needs_llm")]
    if not todo or not client:
        return body_html
    try:
        items = []
        for v in todo:
            m = None
            for cand in _A_RE.finditer(body_html):
                if _href_of(cand.group(1)) == v["href"] and _text(cand.group(2)) == v["text"]:
                    m = cand
                    break
            if not m:
                continue
            items.append({
                "id": len(items) + 1,
                "href": v["href"],
                "current_anchor": v["text"],
                "destination": (destinations or {}).get(v["href"], ""),
                "sentence": _sentence_around(body_html, m.start(), m.end()),
                "_span": (m.start(), m.end(), m.group(1)),
            })
        if not items:
            return body_html
        prompt_items = [{k: v for k, v in it.items() if k != "_span"} for it in items]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You fix anchor text for internal links in a blog article. For each item, return a new "
                        "anchor of 2-6 words that names the destination page (its product/collection/article "
                        "name or topic) and reads naturally inside the given sentence. Never use 'click here', "
                        "'here', 'read more', 'this page', 'browse', 'shop now'. Keep the meaning of the sentence. "
                        "Return ONLY JSON: {\"anchors\": {\"<id>\": \"<new anchor>\"}}"
                    ),
                },
                {"role": "user", "content": json.dumps(prompt_items, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=600,
            timeout=30,
        )
        content = response.choices[0].message.content or ""
        start, end = content.find("{"), content.rfind("}") + 1
        anchors = json.loads(content[start:end]).get("anchors", {}) if start >= 0 else {}
        # Apply from the end so earlier spans stay valid.
        body = body_html
        for it in sorted(items, key=lambda x: x["_span"][0], reverse=True):
            new = (anchors.get(str(it["id"])) or "").strip()
            if not new or _norm_anchor(new) in GENERIC_ANCHORS or len(new.split()) > MAX_ANCHOR_WORDS:
                continue
            s, e, attrs = it["_span"]
            body = body[:s] + f"<a{attrs}>{new}</a>" + body[e:]
        return body
    except Exception as e:
        logger.warning("rewrite_anchors failed — anchors left as-is: %s", e)
        return body_html


def lint_and_fix_anchors(body_html, client=None, model=None, destinations=None):
    """Convenience: lint, rewrite flagged anchors once, re-lint. Returns
    (body, report) where report = {violations, rewritten, remaining}."""
    body, violations = lint_anchor_text(body_html)
    remaining = [v for v in violations if v.get("needs_llm")]
    rewritten = 0
    if remaining and client:
        before = body
        body = rewrite_anchors(client, model, body, remaining, destinations)
        if body != before:
            _, after = lint_anchor_text(body)
            still = [v for v in after if v.get("needs_llm")]
            rewritten = len(remaining) - len(still)
            remaining = still
    return body, {"violations": violations, "rewritten": rewritten, "remaining": remaining}


def validate_article_result(result, primary_keyword, client, config, hero_alt=None, inline_alts=None):
    """Meta title/description/title keyword + length gate, and alt balance
    across hero + inline alts (keyword in the hero alt only). Returns the
    (possibly corrected) result plus `balanced_alts` when alts were given."""
    try:
        from app.services.ai_content_validator import balance_alt_keyword, validate_named_fields

        result = validate_named_fields(
            result,
            {"meta_title": "meta_title", "meta_description": "meta_description", "title": "title"},
            primary_keyword, client, config,
        )
        alts = [hero_alt or ""] + [a or "" for a in (inline_alts or [])]
        if primary_keyword and any(alts):
            balanced = balance_alt_keyword(alts, primary_keyword)
            if isinstance(balanced, tuple):
                balanced = balanced[0]
            if balanced and list(balanced) != alts:
                result["balanced_alts"] = list(balanced)
    except Exception:
        logger.exception("validate_article_result failed — result passed through")
    return result
