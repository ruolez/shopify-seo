"""Opening-paragraph collection link: measure, choose, check, splice.

SEO audit (Sep 2026): the first text link to a collection is the one Google
weighs most (reasonable-surfer placement, first-anchor-counts), and mobile
readers rarely scroll past the intro — yet most posts put their first
collection link hundreds of words down. The fix *moves* the post's best
collection link into the opening paragraph (sentence 2 or 3, anchor = the
collection's target keyword) and unwraps its later copies, so the link count
stays flat.

Pure logic (no DB). The only model call is `rewrite_intro`, whose output is
never trusted: `check_new_intro` re-verifies every rule deterministically.
"""

import html as html_lib
import logging
import math
import re

from app.services.article_quality import GENERIC_ANCHORS, MAX_ANCHOR_WORDS, _ADJACENT_RE
from app.services.interlinking_analyzer import classify_href, normalize_href_path
from app.services.keyword_identity import fold_token, norm
from app.services.link_candidates import text_tokens

logger = logging.getLogger(__name__)

INTRO_MIN_WORDS = 35
INTRO_MAX_WORDS = 120
LINK_SENTENCES = (2, 3)
EXCLUDED_COLLECTION_HANDLES = {"all", "frontpage"}

_A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_P_RE = re.compile(r"<p\b([^>]*)>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"<h[1-6]\b", re.IGNORECASE)
_LEADING_SKIP_RE = re.compile(
    r"\s+|<!--.*?-->|<figure\b.*?</figure>|<img\b[^>]*>|<br\s*/?>"
    r"|<(?:section|article|div)\b(?![^>]*a2z-)[^>]*>|<h[2-4]\b[^>]*>.*?</h[2-4]>",
    re.IGNORECASE | re.DOTALL,
)
_SENTENCE_END_RE = re.compile(r"([.!?][\"”’)]?)\s+(?=[\"“(]?[A-Z0-9\x01])")
_ABBREVIATIONS = ("U.S.", "U.K.", "e.g.", "i.e.", "vs.", "Mr.", "Mrs.", "Dr.", "St.", "No.", "Inc.", "Co.")
_LINK_MARK = "\x01"


def visible_text(fragment):
    text = html_lib.unescape(_TAG_RE.sub(" ", fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def word_count(fragment):
    return len(visible_text(fragment).split())


def _href_of(attrs):
    m = _HREF_RE.search(attrs or "")
    return m.group(1) if m else ""


def _folded(tokens):
    return {fold_token(t) for t in tokens}


def _content_tokens(text):
    return _folded(text_tokens(text))


def collection_handle(href, store_hosts):
    """Handle of a linkable collection href, else None."""
    cls = classify_href(html_lib.unescape(href or ""), store_hosts or set())
    if not cls or cls.get("link_type") != "collection":
        return None
    handle = (cls.get("handle") or "").lower()
    return None if handle in EXCLUDED_COLLECTION_HANDLES else handle


def locate_intro(body_html):
    """(start, end) of the opening paragraph: the first <p> with visible text,
    after any leading figures/images, wrapper tags and an opening heading
    (older posts start with an H2). None when a block element such as the
    takeaways box comes first."""
    body = body_html or ""
    pos = 0
    while pos < len(body):
        m = _LEADING_SKIP_RE.match(body, pos)
        if m and m.end() > pos:
            pos = m.end()
            continue
        m = _P_RE.match(body, pos)
        if not m:
            return None
        if "a2z-" in m.group(1) or not visible_text(m.group(2)):
            pos = m.end()
            continue
        return (m.start(), m.end())
    return None


def body_paragraph_spans(body_html):
    """Spans of prose paragraphs in document order (block labels and empty
    paragraphs excluded) — the audit's paragraph numbering."""
    return [
        (m.start(), m.end())
        for m in _P_RE.finditer(body_html or "")
        if "a2z-" not in m.group(1) and visible_text(m.group(2))
    ]


def collection_links(body_html, store_hosts):
    """Collection links in document order: {handle, href, anchor, start, end,
    words_before}."""
    body = body_html or ""
    out = []
    for m in _A_RE.finditer(body):
        href = _href_of(m.group(1))
        handle = collection_handle(href, store_hosts)
        if not handle:
            continue
        out.append({
            "handle": handle,
            "href": href,
            "anchor": visible_text(m.group(2)),
            "start": m.start(),
            "end": m.end(),
            "words_before": word_count(body[: m.start()]),
        })
    return out


_ANCHOR_FILLER = {"collection", "selection", "range", "lineup", "full", "browse", "explore", "see", "check", "entire"}


def _anchor_tokens(text):
    return _folded(norm((text or "").replace("-", " ")).split())


def anchor_matches(anchor, keyword=None, title=None):
    """Anchor names the destination: it covers at least 2/3 of the
    collection keyword, or every descriptive word in it comes from the
    collection's keyword/title and there are at least two of them ("best blunt
    wraps" → Tobacco Blunt Wraps; a bare "wraps" is too vague)."""
    anchor_tokens = _anchor_tokens(anchor)
    kw_tokens = _anchor_tokens(keyword)
    if kw_tokens and len(kw_tokens & anchor_tokens) >= math.ceil(len(kw_tokens) * 2 / 3):
        return True
    descriptive = _folded(text_tokens((anchor or "").replace("-", " "))) - _ANCHOR_FILLER
    destination = kw_tokens | _folded(text_tokens((title or "").replace("-", " ")))
    return len(descriptive) >= 2 and descriptive <= destination


def intro_link_status(body_html, store_hosts, catalog_by_handle=None):
    """Measure one article against the audit's intro-link rules.

    status: ok | weak_anchor | needs_fix | no_collection | no_intro
    """
    catalog_by_handle = catalog_by_handle or {}
    body = body_html or ""
    intro = locate_intro(body)
    links = collection_links(body, store_hosts)
    paragraphs = body_paragraph_spans(body)
    first = links[0] if links else None

    def _within(link, spans):
        return any(s <= link["start"] < e for s, e in spans)

    in_p1 = bool(first and intro and intro[0] <= first["start"] < intro[1])
    in_first_two = bool(first and _within(first, paragraphs[:2]))
    linked_handles = list(dict.fromkeys(link["handle"] for link in links))

    if not intro:
        status = "no_intro"
    elif in_p1:
        info = catalog_by_handle.get(first["handle"]) or {}
        status = "ok" if anchor_matches(first["anchor"], info.get("keyword"), info.get("title")) else "weak_anchor"
    elif links:
        status = "needs_fix"
    else:
        status = "no_collection"

    return {
        "status": status,
        "intro_span": intro,
        "intro_words": word_count(body[intro[0]:intro[1]]) if intro else 0,
        "in_p1": in_p1,
        "in_first_two": in_first_two,
        "words_before_first": first["words_before"] if first else None,
        "first_link": {k: first[k] for k in ("handle", "href", "anchor")} if first else None,
        "linked_handles": linked_handles,
        "collection_link_count": len(links),
    }


def _candidate_tokens(cand):
    return _content_tokens(f"{cand.get('keyword') or ''} {cand.get('title') or ''}")


def _relevance(query_tokens, cand):
    cand_tokens = _candidate_tokens(cand)
    if not cand_tokens:
        return 0.0
    matched = len(cand_tokens & query_tokens)
    return matched + matched / len(cand_tokens)


GENERIC_WORD_MIN_COLLECTIONS = 5


def _name_token_sets(handle, cand):
    names = (cand.get("title"), cand.get("keyword"), (handle or "").replace("-", " "))
    return [t for t in (_content_tokens(n or "") for n in names) if t]


def generic_catalog_words(catalog_by_handle):
    """Words shared by many collection names ('cigarillo', 'pipe', 'tobacco')
    — category words, not identity. Brand words ('arrowhead') stay distinctive."""
    counts = {}
    for handle, cand in catalog_by_handle.items():
        for token in set().union(*_name_token_sets(handle, cand) or [set()]):
            counts[token] = counts.get(token, 0) + 1
    return {t for t, n in counts.items() if n >= GENERIC_WORD_MIN_COLLECTIONS}


def _full_match(query_tokens, handle, cand, generic):
    """The post's query/title names the collection outright: every distinctive
    word of its title, keyword or handle appears (category words may be
    missing — "Swisher Sweets Cigarillos" for a Swisher flavors post). A name
    made only of category words must match completely (/collections/pipe-tobacco)."""
    for tokens in _name_token_sets(handle, cand):
        distinctive = tokens - generic
        if (distinctive and distinctive <= query_tokens) or (not distinctive and tokens <= query_tokens):
            return True
    return False


def _extra_words(query_tokens, handle, generic):
    """Distinctive words the collection's handle adds beyond the post's topic —
    the general backwoods-cigars (0) beats backwoods-select-cigars (1)."""
    return len(_content_tokens((handle or "").replace("-", " ")) - query_tokens - generic)


def _names_brand(query_tokens, handle, cand, generic):
    """A distinctive (non-category) word of the collection is in the post —
    the Fumari collection for a Fumari launch beats generic Hookah Tobacco."""
    return any((tokens - generic) & query_tokens for tokens in _name_token_sets(handle, cand))


def relevance_to_post(query, title, cand):
    return _relevance(_content_tokens(f"{query or ''} {title or ''}"), cand)


def rank_collections(query, title, catalog_by_handle):
    """[(handle, score)] for every linkable catalog collection, most relevant
    to the post's query+title first (zero scores last)."""
    query_tokens = _content_tokens(f"{query or ''} {title or ''}")
    scored = [
        (handle, _relevance(query_tokens, info))
        for handle, info in catalog_by_handle.items()
        if handle not in EXCLUDED_COLLECTION_HANDLES
    ]
    return sorted(scored, key=lambda x: -x[1])


def pick_intro_collection(query, title, linked_handles, catalog_by_handle):
    """Choose the one collection the intro should link.

    Prefers a collection the article already links ('moved' — link count stays
    flat), ranked by relevance of its keyword+title to the post's primary
    query and title, earliest link winning ties. A store collection is
    'added' instead only when it names the post's topic outright (full match)
    and nothing linked does, or when nothing linked is relevant at all.
    Returns {handle, title, keyword, href, mode, score} or None.
    """
    query_tokens = _content_tokens(f"{query or ''} {title or ''}")

    def _pack(handle, mode, score):
        info = catalog_by_handle.get(handle) or {}
        return {
            "handle": handle,
            "title": info.get("title") or handle,
            "keyword": info.get("keyword") or "",
            "href": f"/collections/{handle}",
            "mode": mode,
            "score": round(score, 3),
        }

    generic = generic_catalog_words(catalog_by_handle)
    linked = [h for h in dict.fromkeys(linked_handles or []) if h in catalog_by_handle]
    best, best_score, best_key = None, 0.0, None
    for handle in linked:
        cand = catalog_by_handle[handle]
        score = _relevance(query_tokens, cand)
        full = _full_match(query_tokens, handle, cand, generic)
        key = (
            full,
            full and _names_brand(query_tokens, handle, cand, generic),
            -_extra_words(query_tokens, handle, generic) if full else 0,
            score,
        )
        if score > 0 and (best_key is None or key > best_key):
            best, best_score, best_key = handle, score, key

    full = sorted(
        (
            (handle, score) for handle, score in rank_collections(query, title, catalog_by_handle)
            if score > 0 and _full_match(query_tokens, handle, catalog_by_handle[handle], generic)
        ),
        key=lambda x: (
            not _names_brand(query_tokens, x[0], catalog_by_handle[x[0]], generic),
            _extra_words(query_tokens, x[0], generic),
            -x[1],
        ),
    )
    if best and (_full_match(query_tokens, best, catalog_by_handle[best], generic) or not full):
        return _pack(best, "moved", best_score)
    if full:
        handle, score = full[0]
        return _pack(handle, "moved" if handle in linked else "added", score)

    ranked = rank_collections(query, title, catalog_by_handle)
    catalog_best, catalog_score = ranked[0] if ranked else (None, 0.0)
    if catalog_best and catalog_score >= 1:
        return _pack(catalog_best, "added", catalog_score)
    if linked:
        return _pack(linked[0], "moved", 0.0)
    return None


def split_sentences(text):
    protected = text
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", "\x02"))
    marked = _SENTENCE_END_RE.sub(lambda m: m.group(1) + "\x00", protected)
    return [s.replace("\x02", ".").strip() for s in marked.split("\x00") if s.strip()]


def link_sentence_index(paragraph_html, link_start):
    """1-based sentence number holding the <a> that starts at `link_start`
    (offset within `paragraph_html`)."""
    marked = paragraph_html[:link_start] + _LINK_MARK + paragraph_html[link_start:]
    for i, sentence in enumerate(split_sentences(visible_text(marked)), start=1):
        if _LINK_MARK in sentence:
            return i
    return None


def _normalized_hrefs(fragment, skip_handle, store_hosts):
    out = []
    for m in _A_RE.finditer(fragment or ""):
        href = _href_of(m.group(1))
        if skip_handle and collection_handle(href, store_hosts) == skip_handle:
            continue
        out.append(normalize_href_path(href))
    return out


def check_new_intro(old_intro, new_intro, target, primary_query, store_hosts):
    """Deterministic gate for a rewritten intro. Returns [] when valid, else a
    list of {code, message}. `target` = {handle, keyword, title}."""
    violations = []

    def v(code, message):
        violations.append({"code": code, "message": message})

    new_intro = new_intro or ""
    paragraphs = list(_P_RE.finditer(new_intro))
    if not paragraphs or _TAG_RE.sub("", _P_RE.sub("", new_intro)).strip():
        v("format", "Intro must be one or two <p> paragraphs and nothing else")
    elif len(paragraphs) > 2:
        v("format", "Intro must be at most two paragraphs")
    if _HEADING_RE.search(new_intro):
        v("format", "No headings in the intro")
    if any(re.search(r"<p\b", p.group(2), re.IGNORECASE) for p in paragraphs):
        v("format", "Paragraphs must not be nested")

    handle = target.get("handle")
    target_links = [m for m in _A_RE.finditer(new_intro) if collection_handle(_href_of(m.group(1)), store_hosts) == handle]
    if len(target_links) != 1:
        v("target_count", f"Link /collections/{handle} exactly once (found {len(target_links)})")
    elif paragraphs:
        first_p = paragraphs[0]
        link = target_links[0]
        if not (first_p.start() <= link.start() < first_p.end()):
            v("placement", "The collection link must be in the first paragraph")
        else:
            idx = link_sentence_index(first_p.group(0), link.start() - first_p.start())
            if idx not in LINK_SENTENCES:
                v("placement", f"Put the collection link in sentence 2 or 3 (it is in sentence {idx})")
        anchor = visible_text(link.group(2))
        if not anchor_matches(anchor, target.get("keyword"), target.get("title")):
            v("anchor", f"Anchor text must name the collection keyword \"{target.get('keyword') or target.get('title')}\" (got \"{anchor}\")")
        if len(anchor.split()) > MAX_ANCHOR_WORDS or norm(anchor) in GENERIC_ANCHORS:
            v("anchor", f"Anchor must be 2-{MAX_ANCHOR_WORDS} descriptive words (got \"{anchor}\")")

    new_collections = [collection_handle(_href_of(m.group(1)), store_hosts) for m in _A_RE.finditer(new_intro)]
    new_collections = [h for h in new_collections if h]
    if new_collections and new_collections[0] != handle:
        v("target_not_first", f"/collections/{handle} must be the first collection link in the intro")

    old_hrefs = set(_normalized_hrefs(old_intro, handle, store_hosts))
    new_hrefs = set(_normalized_hrefs(new_intro, handle, store_hosts))
    for href in sorted(old_hrefs - new_hrefs):
        if not href.startswith("/collections/"):
            v("dropped_link", f"Keep the existing link to {href}")
    for href in sorted(new_hrefs - old_hrefs):
        v("new_link", f"Do not add a link to {href}")

    if _ADJACENT_RE.search(new_intro):
        v("adjacent_links", "Never put two links back to back")

    words = word_count(new_intro)
    if not INTRO_MIN_WORDS <= words <= INTRO_MAX_WORDS:
        v("length", f"Intro must be {INTRO_MIN_WORDS}-{INTRO_MAX_WORDS} words (got {words})")

    query_tokens = _content_tokens(primary_query or "")
    if query_tokens:
        present = query_tokens & _content_tokens(visible_text(new_intro))
        if len(present) < math.ceil(len(query_tokens) * 2 / 3):
            v("query_missing", f"Sentence 1 must answer and name \"{primary_query}\"")
    return violations


def dropped_collection_links(old_intro, new_intro, target_handle, store_hosts):
    """Collection links the old intro had that the rewrite let go (allowed —
    the intro keeps one collection link — but shown to the reviewer)."""
    old = set(_normalized_hrefs(old_intro, target_handle, store_hosts))
    new = set(_normalized_hrefs(new_intro, target_handle, store_hosts))
    return sorted(h for h in old - new if h.startswith("/collections/"))


def splice_intro(body_html, span, new_intro, target_handle, store_hosts):
    """Swap the intro and unwrap every later link to the target collection
    (anchor text kept) — the link moves, the count doesn't grow. Returns
    (new_body, unwrapped_count)."""
    start, end = span
    unwrapped = 0

    def _unwrap(m):
        nonlocal unwrapped
        if collection_handle(_href_of(m.group(1)), store_hosts) == target_handle:
            unwrapped += 1
            return m.group(2)
        return m.group(0)

    rest = _A_RE.sub(_unwrap, body_html[end:])
    return body_html[:start] + new_intro + rest, unwrapped


def _extract_intro_html(content):
    content = re.sub(r"^```(?:html)?\s*", "", (content or "").strip())
    content = re.sub(r"\s*```$", "", content)
    content = re.sub(r"<p\b[^>]*>\s*(?=<p\b)", "", content, flags=re.IGNORECASE)
    content = re.sub(r"</p>\s*(?=</p>)", "", content, flags=re.IGNORECASE)
    content = re.sub(r"<(strong|b|em)>\s*(<a\b[^>]*>.*?</a>)\s*</\1>", r"\2", content, flags=re.IGNORECASE | re.DOTALL)
    paragraphs = [m.group(0) for m in _P_RE.finditer(content)]
    if paragraphs:
        return "\n".join(paragraphs)
    return f"<p>{content}</p>" if content else ""


def build_intro_request(ctx):
    target = ctx["target"]
    lines = [
        f"ARTICLE TITLE: {ctx.get('title') or ''}",
        f"ARTICLE TYPE: {ctx.get('kind') or 'guide'}",
        f"PRIMARY QUERY (sentence 1 must answer it and name it): {ctx.get('primary_query') or ctx.get('title') or ''}",
        f"COLLECTION TO LINK: <a href=\"{target['href']}\">…</a> — collection \"{target.get('title') or ''}\", "
        f"anchor keyword \"{target.get('keyword') or target.get('title') or ''}\" (words from the collection name "
        "are fine if the keyword reads awkwardly)",
        "",
        "CURRENT OPENING PARAGRAPH (HTML):",
        ctx.get("old_intro") or "",
        "",
        "WHAT COMES RIGHT AFTER THE INTRO (do not repeat it):",
        (ctx.get("following_text") or "")[:1600],
    ]
    return "\n".join(lines)


def rewrite_intro(client, model, system_prompt, ctx, store_hosts):
    """One LLM rewrite + one corrective retry. Returns
    (intro_html, violations, attempts); violations == [] means valid."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_intro_request(ctx)},
    ]
    intro, violations = "", [{"code": "no_output", "message": "Model returned nothing"}]
    attempts = 0
    for attempts in (1, 2):
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, temperature=0.3, max_tokens=900, timeout=45,
            )
            content = response.choices[0].message.content or ""
        except Exception as e:
            logger.warning("rewrite_intro call failed: %s", e)
            violations = [{"code": "ai_error", "message": str(e)[:200]}]
            break
        intro = _extract_intro_html(content)
        check_query = ctx["check_query"] if "check_query" in ctx else ctx.get("primary_query")
        violations = check_new_intro(ctx.get("old_intro"), intro, ctx["target"], check_query, store_hosts)
        if not violations:
            break
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Fix these problems and return the corrected intro HTML only:\n- "
             + "\n- ".join(x["message"] for x in violations)},
        ]
    return intro, violations, attempts
