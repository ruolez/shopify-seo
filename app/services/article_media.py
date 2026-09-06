"""Shared in-body media + structure helpers for generated articles (blog + news).

Generated bodies carry standalone placeholder tokens like ``[[INLINE_IMAGE_1]]``
on their own line between paragraphs, plus a parallel list describing each
image. After the images are generated and uploaded to the Shopify CDN, each
token is swapped for a ``<figure>``. Any token with no successfully-uploaded
image is stripped so no broken markers ever ship.

Also home to the heading-id / table-of-contents pass and FAQ extraction used
by the visual blog pipeline. Pure functions; no DB access.
"""

import html
import json
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"\[\[INLINE_IMAGE_(\d+)\]\]")
BLOG_FIGURE_CLASS = "a2z-blog__figure"
NEWS_FIGURE_CLASS = "a2z-news__figure"

_H2_RE = re.compile(r"<h2(\s[^>]*)?>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_FIGURE_RE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)
_FAQ_SECTION_RE = re.compile(
    r'<section\b[^>]*class="[^"]*a2z-blog__faq[^"]*"[^>]*>(.*?)</section>', re.IGNORECASE | re.DOTALL
)
_DETAILS_RE = re.compile(
    r"<details\b[^>]*>\s*<summary\b[^>]*>(.*?)</summary>(.*?)</details>", re.IGNORECASE | re.DOTALL
)


# ── Upload ────────────────────────────────────────────────────────

def upload_image_to_cdn(client, image_bytes, filename, alt_text):
    """Stage, upload, and register an image as a Shopify File; return its
    permanent CDN url (staged-upload urls expire, so anything embedded in a
    body must go through here)."""
    mime = "image/webp" if filename.endswith(".webp") else "image/jpeg"
    staged = client.staged_upload_create(filename, mime, len(image_bytes), resource="IMAGE")
    resource_url = client.upload_staged_file(staged, image_bytes, content_type=mime)
    file_obj = client.file_create(resource_url, filename=filename, alt_text=alt_text)
    return client.file_get_url(file_obj.get("id"))


# ── Inline image tokens → figures ─────────────────────────────────

def figure_html(img, css_class=BLOG_FIGURE_CLASS):
    """Build a <figure> for one uploaded inline image."""
    src = html.escape(img.get("cdn_url", ""), quote=True)
    alt = html.escape(img.get("alt", "") or "", quote=True)
    caption = img.get("caption") or ""
    width = img.get("width") or 1024
    height = img.get("height") or 768
    figcaption = f"<figcaption>{html.escape(caption)}</figcaption>" if caption.strip() else ""
    return (
        f'<figure class="{css_class}">'
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


def strip_leftover_tokens(body):
    """Remove any unmatched [[INLINE_IMAGE_n]] tokens (and a wrapping <p>)."""
    return re.sub(
        r"(?:<p>\s*)?\[\[INLINE_IMAGE_\d+\]\](?:\s*</p>)?",
        "",
        body or "",
        flags=re.IGNORECASE,
    )


def fallback_insert(body, figures):
    """Insert figures at evenly-spaced paragraph boundaries when the body has no
    usable tokens. Splits on </p>; if there are too few paragraphs, appends."""
    parts = re.split(r"(</p>)", body, flags=re.IGNORECASE)
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

    inserts = {}
    for k, fig in enumerate(figures, start=1):
        idx = round(k * len(units) / (n + 1)) - 1
        idx = max(0, min(idx, len(units) - 1))
        inserts.setdefault(idx, []).append(fig)
    out = []
    for i, unit in enumerate(units):
        out.append(unit)
        for fig in inserts.get(i, []):
            out.append(fig)
    return "".join(out)


def insert_inline_images(body_html, images, figure_class=BLOG_FIGURE_CLASS):
    """Return body_html with inline-image tokens replaced by <figure> elements.

    images: list of dicts. Only entries with a truthy ``cdn_url`` are rendered.
    Each may include: token, cdn_url, alt, caption, width, height.
    """
    body = body_html or ""
    # Skip images already embedded (re-composing a composed body must be a no-op).
    usable = [
        img for img in (images or [])
        if img.get("cdn_url") and html.escape(img["cdn_url"], quote=True) not in body
    ]

    matched_any = False
    for img in usable:
        token = img.get("token") or ""
        m = TOKEN_RE.search(token)
        if not m:
            continue
        pattern = _token_pattern(m.group(1))
        if pattern.search(body):
            body, count = pattern.subn(figure_html(img, figure_class), body, count=1)
            if count:
                matched_any = True

    # Fallback: AI gave us images but forgot the tokens.
    if not matched_any and usable and not TOKEN_RE.search(body):
        figures = [figure_html(img, figure_class) for img in usable]
        body = fallback_insert(body, figures)

    return strip_leftover_tokens(body)


def tokenize_figures(body_html, images):
    """Inverse of insert_inline_images: swap each uploaded image's <figure>
    back to its [[INLINE_IMAGE_n]] token so a live (possibly hand-edited) body
    can be recomposed with a regenerated image. Figures not owned by an entry
    in `images` and entries without a cdn_url are left untouched."""
    body = body_html or ""
    for img in images or []:
        token = (img or {}).get("token") or ""
        src = html.escape((img or {}).get("cdn_url") or "", quote=True)
        if not src or not TOKEN_RE.search(token):
            continue
        for m in _FIGURE_RE.finditer(body):
            if src in m.group(0):
                body = body[: m.start()] + token + body[m.end():]
                break
    return body


# ── Restyle response parsing ──────────────────────────────────────

RESTYLE_PLAN_DELIM = "===MEDIA_PLAN==="
_HTML_HINT_RE = re.compile(r"<(p|h2|h3|ul|ol|div|section|table)\b", re.IGNORECASE)


def _strip_fences(text):
    text = re.sub(r"^\s*```(?:html|json)?\s*\n?", "", text or "", flags=re.IGNORECASE)
    return re.sub(r"\n?```\s*$", "", text).strip()


def _parse_json_object(text):
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_restyle_response(content):
    """Return (body_html, media_plan) from a restyle model reply.

    Preferred format is raw HTML, then a RESTYLE_PLAN_DELIM line, then the
    media-plan JSON — a 20K-character body inside a JSON string breaks on
    the first unescaped quote. Legacy single-JSON replies still parse, a
    JSON reply whose body string is malformed is recovered by regex, and a
    bare HTML reply is accepted with an empty plan. Body is "" when nothing
    HTML-like was found (refusals, prose)."""
    text = content or ""
    if RESTYLE_PLAN_DELIM in text:
        body_part, plan_part = text.split(RESTYLE_PLAN_DELIM, 1)
        body = _strip_fences(body_part)
        return (body if _HTML_HINT_RE.search(body) else "", _parse_json_object(_strip_fences(plan_part)) or {})
    obj = _parse_json_object(text)
    if obj and obj.get("body_html"):
        body = _strip_fences(str(obj["body_html"]))
        plan = obj.get("media_plan") if isinstance(obj.get("media_plan"), dict) else {}
        return (body if _HTML_HINT_RE.search(body) else "", plan)
    m = re.search(r'"body_html"\s*:\s*"(.*?)"\s*,\s*"media_plan"', text, re.DOTALL)
    if m:
        raw = m.group(1)
        try:
            body = json.loads('"' + raw + '"')
        except ValueError:
            body = raw
        body = _strip_fences(body)
        return (body if _HTML_HINT_RE.search(body) else "", {})
    body = _strip_fences(text)
    return (body if _HTML_HINT_RE.search(body) else "", {})


def count_figures(body_html):
    return len(_FIGURE_RE.findall(body_html or ""))


def planned_tokens(body_html):
    """Distinct token numbers present in a body, in document order."""
    seen = []
    for m in TOKEN_RE.finditer(body_html or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


# ── Heading ids + table of contents ───────────────────────────────

def slugify(text, max_len=60):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "section"


def _strip_tags(s):
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def add_heading_ids_and_toc(body_html, min_h2=3, toc_title="In this article"):
    """Give every <h2> a stable id and insert a <nav class="a2z-blog__toc">.

    The TOC goes right after the key-takeaways block when present, otherwise
    after the first paragraph. Idempotent: a body that already has a TOC nav
    is returned unchanged. H2s inside the FAQ section are skipped (the FAQ
    heading itself is listed; the questions live in <details>).
    """
    body = body_html or ""
    if 'class="a2z-blog__toc"' in body:
        return body

    used = set()
    entries = []

    def _with_id(m):
        attrs = m.group(1) or ""
        inner = m.group(2)
        text = _strip_tags(inner)
        if not text:
            return m.group(0)
        existing = re.search(r'\bid="([^"]+)"', attrs)
        if existing:
            hid = existing.group(1)
        else:
            base = slugify(text)
            hid = base
            i = 2
            while hid in used:
                hid = f"{base}-{i}"
                i += 1
            attrs = attrs + f' id="{hid}"'
        used.add(hid)
        entries.append((hid, text))
        return f"<h2{attrs}>{inner}</h2>"

    body = _H2_RE.sub(_with_id, body)
    if len(entries) < min_h2:
        return body

    items = "".join(
        f'<li><a href="#{html.escape(hid, quote=True)}">{html.escape(text)}</a></li>' for hid, text in entries
    )
    nav = (
        f'<nav class="a2z-blog__toc" aria-label="Table of contents">'
        f'<p class="a2z-blog__label">{html.escape(toc_title)}</p><ol>{items}</ol></nav>'
    )

    takeaways_end = re.search(
        r'<div\b[^>]*class="[^"]*a2z-blog__takeaways[^"]*"[^>]*>.*?</div>', body, re.IGNORECASE | re.DOTALL
    )
    if takeaways_end:
        pos = takeaways_end.end()
        return body[:pos] + nav + body[pos:]
    first_p = re.search(r"</p>", body, re.IGNORECASE)
    if first_p:
        pos = first_p.end()
        return body[:pos] + nav + body[pos:]
    return nav + body


# ── FAQ ───────────────────────────────────────────────────────────

def extract_faq(body_html):
    """Return [{question, answer}] from the <section class="a2z-blog__faq"> block."""
    m = _FAQ_SECTION_RE.search(body_html or "")
    if not m:
        return []
    out = []
    for q, a in _DETAILS_RE.findall(m.group(1)):
        question = _strip_tags(q)
        answer = _strip_tags(a)
        if question and answer:
            out.append({"question": question, "answer": answer})
    return out


def build_faqpage_jsonld(faq):
    entities = [
        {
            "@type": "Question",
            "name": f["question"],
            "acceptedAnswer": {"@type": "Answer", "text": f["answer"]},
        }
        for f in (faq or [])
        if isinstance(f, dict) and f.get("question") and f.get("answer")
    ]
    if not entities:
        return None
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": entities}


# ── Media plan normalization ──────────────────────────────────────

def normalize_media_plan(plan, inline_limit):
    """Coerce an LLM media plan into the canonical shape, trimmed to
    `inline_limit` inline specs with tokens renumbered 1..n."""
    plan = plan if isinstance(plan, dict) else {}
    hero = plan.get("hero") if isinstance(plan.get("hero"), dict) else {}
    inline_raw = plan.get("inline") if isinstance(plan.get("inline"), list) else []
    inline = []
    for spec in inline_raw:
        if not isinstance(spec, dict) or not (spec.get("prompt") or "").strip():
            continue
        if len(inline) >= max(0, int(inline_limit or 0)):
            break
        n = len(inline) + 1
        handles = spec.get("product_handles") or []
        if not isinstance(handles, list):
            handles = [handles]
        inline.append({
            "token": f"[[INLINE_IMAGE_{n}]]",
            "section": (spec.get("section") or "").strip(),
            "kind": (spec.get("kind") or "scene").strip().lower(),
            "prompt": spec.get("prompt", "").strip(),
            "alt": (spec.get("alt") or "").strip()[:125],
            "caption": (spec.get("caption") or "").strip(),
            "product_handles": [str(h).strip() for h in handles if str(h).strip()][:3],
        })
    return {
        "hero": {
            "prompt": (hero.get("prompt") or "").strip(),
            "alt": (hero.get("alt") or "").strip()[:125],
        },
        "inline": inline,
    }


# ── Compose ───────────────────────────────────────────────────────

def compose_article_body(body_html, inline_images, visuals=None, figure_class=BLOG_FIGURE_CLASS):
    """Final body assembly for a visual article: figures in, TOC in, no stray
    tokens. `visuals` is the resolved article_visuals dict (toc flag)."""
    visuals = visuals or {}
    body = insert_inline_images(body_html, inline_images, figure_class=figure_class)
    if visuals.get("toc", True):
        body = add_heading_ids_and_toc(body)
    return strip_leftover_tokens(body)


def faq_jsonld_string(body_html):
    payload = build_faqpage_jsonld(extract_faq(body_html))
    return json.dumps(payload, ensure_ascii=False) if payload else None
