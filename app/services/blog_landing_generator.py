"""The blog hub at /blogs/<handle>.

The theme renders the newest article's content as the blog index, so the hub is
a landing article kept newest (see _repin_landing_article in blog_routes). Its
body is built here: guides grouped by topic, each topic linked to the shop
collection that owns the head term, plus start-here, latest, news and a full
monthly archive so every guide stays one click from the hub. No tag links —
tag archives are noindexed because they competed with the guides themselves.
"""

import html
import re
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.services.keyword_identity import fold_token, norm, variant_key
from app.services.listing_archive import archive_css, archive_html, split_archive

START_HERE_COUNT = 4
LATEST_COUNT = 6
TOPIC_CARD_COUNT = 3
TOPIC_LIST_VISIBLE = 6
NEWS_COUNT = 3
WORDS_PER_MINUTE = 230
_META_SEP = ' <span aria-hidden="true">&middot;</span> '
MORE_GUIDES = {"name": "More guides", "h2": "More tobacco guides", "blurb": "", "collection_handle": None, "terms": []}

_COLLECTION_HREF = re.compile(r'href="(?:https?://[^/"]+)?/collections/([a-z0-9-]+)', re.I)


def _strip_html(text):
    # Drop style/script blocks with their contents first — plain tag-stripping
    # would leak raw CSS/JS text into excerpts.
    text = re.sub(r"<\s*(style|script)\b[^>]*>[\s\S]*?<\s*/\s*\1\s*>", "", text or "", flags=re.I)
    return re.sub(r"<[^>]+>", "", text)


def _truncate(text, length=160):
    text = text.strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


def _parse_date(dt):
    if isinstance(dt, str):
        try:
            return datetime.strptime(dt.replace("+00:00", "Z").split(".")[0].rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            try:
                return datetime.strptime(dt[:10], "%Y-%m-%d")
            except ValueError:
                return None
    return dt if isinstance(dt, datetime) else None


def _format_date(dt):
    dt = _parse_date(dt)
    return dt.strftime("%B %d, %Y") if dt else ""


def _pub_sort_key(a):
    """Stable sort key for published_at that tolerates datetime, str, or None.

    Falls back to synced_at so articles missing a publish date still sort by
    when they appeared instead of sinking to one end of the list."""
    pa = a.get("published_at") or a.get("synced_at")
    if not pa:
        return ""
    return pa.isoformat() if hasattr(pa, "isoformat") else str(pa)


def _get_excerpt(article):
    source = (article.get("summary") or "").strip() or (article.get("body") or "").strip()
    if not source:
        return ""
    # Unescape first: summaries hold entities ("&amp;") that would otherwise
    # be escaped twice and show as "&AMP;".
    text = " ".join(html.unescape(_strip_html(source)).split())
    return html.escape(_truncate(text))


def get_tags(articles):
    tag_counter = Counter()
    for article in articles:
        tags_str = article.get("tags") or ""
        for tag in tags_str.split(","):
            tag = tag.strip()
            if tag and tag.lower() != "landing-page":
                tag_counter[tag] += 1
    return [t for t, _ in tag_counter.most_common()]


def _folded_tokens(text):
    return [fold_token(t) for t in norm(text).split()]


def _earliest_term(text, topics):
    """(position, -term length, topic index) of the earliest topic term in
    `text`, or None. Earliest wins so "Filtered Cigars vs Cigarillos" files
    under filtered cigars; at one position the longer term wins."""
    tokens = _folded_tokens(text)
    best = None
    for ti, topic in enumerate(topics):
        for term in topic.get("terms") or []:
            term_tokens = _folded_tokens(term)
            n = len(term_tokens)
            if not n:
                continue
            for pos in range(len(tokens) - n + 1):
                if tokens[pos:pos + n] == term_tokens:
                    cand = (pos, -n, ti)
                    if best is None or cand < best:
                        best = cand
                    break
    return best


def assign_topic(article, topics):
    """Index of the article's topic, or None. The title decides first, then
    the primary keyword, then the handle of the first collection the body links to."""
    first_link = _COLLECTION_HREF.search(article.get("body") or "")
    sources = [
        article.get("title") or "",
        (article.get("primary_keyword") or "").split(",")[0],
        first_link.group(1).replace("-", " ") if first_link else "",
    ]
    for text in sources:
        hit = _earliest_term(text, topics)
        if hit:
            return hit[2]
    return None


def assign_topics(articles, topics):
    """[(topic, [articles])] in topic order, every article in exactly one
    group; guides no topic claims go to a trailing "More guides" group."""
    groups = [(t, []) for t in topics]
    leftovers = []
    for a in articles:
        idx = assign_topic(a, topics)
        (groups[idx][1] if idx is not None else leftovers).append(a)
    groups.append((MORE_GUIDES, leftovers))
    return [(t, posts) for t, posts in groups if posts]


def hub_keyword_conflicts(text, owned):
    """Owned primary keywords that `text` (a hub H1 or meta title) names as a
    phrase. `owned` maps variant_key -> owner dict with item_type, item_id and
    keyword. A brand suffix after '|' is ignored."""
    head = (text or "").split("|")[0]
    tokens = [t for t in norm(head).split() if t != "&"]
    found = {}
    for n in range(1, len(tokens) + 1):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i:i + n])
            key = variant_key(phrase)
            owner = owned.get(key)
            if owner and key not in found:
                found[key] = {"phrase": phrase, "keyword": owner.get("keyword"),
                              "item_type": owner.get("item_type"), "item_id": owner.get("item_id")}
    return list(found.values())


def cdn_width(url, width):
    """The Shopify CDN URL resized to `width`; other URLs unchanged."""
    if not url or not ("cdn.shopify.com" in url or "/cdn/shop/" in url):
        return url
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "width"] + [("width", str(width))]
    return urlunparse(parts._replace(query=urlencode(query)))


def _srcset(url, widths):
    return ", ".join(f"{html.escape(cdn_width(url, w))} {w}w" for w in widths)


def _article_path(blog_handle, article):
    return f"/blogs/{blog_handle}/{article.get('handle') or ''}"


def _clicks_of(article, blog_handle, clicks):
    return (clicks or {}).get(_article_path(blog_handle, article).lower(), {}).get("clicks", 0)


def _by_clicks(articles, blog_handle, clicks):
    """Most-clicked first; newest first among equals."""
    newest_first = sorted(articles, key=_pub_sort_key, reverse=True)
    return sorted(newest_first, key=lambda a: _clicks_of(a, blog_handle, clicks), reverse=True)


def _usable_image(url):
    return bool(url) and "shopify-staged-uploads" not in url


def _reading_minutes(article):
    words = len(_strip_html(article.get("body") or "").split())
    return max(1, round(words / WORDS_PER_MINUTE)) if words else None


def _time_html(article):
    dt = _parse_date(article.get("published_at") or article.get("synced_at"))
    if not dt:
        return ""
    return f'<time datetime="{dt.strftime("%Y-%m-%d")}">{dt.strftime("%b %d, %Y").replace(" 0", " ")}</time>'


def _meta_html(article, with_minutes=True):
    parts = [_time_html(article)]
    minutes = _reading_minutes(article) if with_minutes else None
    if minutes:
        parts.append(f"{minutes} min read")
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return f'<p class="blog-landing__meta">{_META_SEP.join(parts)}</p>'


def _kicker(label):
    return f'<span class="blog-landing__kicker">{html.escape(label)}</span>' if label else ""


def _thumb(article, widths, sizes, display_width, eager=False):
    image_url = article.get("image_url") or ""
    if not _usable_image(image_url):
        return '<div class="blog-landing__thumb blog-landing__thumb--empty" aria-hidden="true"></div>'
    loading = 'fetchpriority="high"' if eager else 'loading="lazy"'
    # alt="" — the linked title sits right beside the image.
    return (f'<div class="blog-landing__thumb"><img src="{html.escape(cdn_width(image_url, display_width))}" '
            f'srcset="{_srcset(image_url, widths)}" sizes="{sizes}" alt="" width="{display_width}" '
            f'height="{round(display_width * 9 / 16)}" {loading} decoding="async"></div>')


def _title_link(article, blog_handle, tag="h3"):
    # The whole card is clickable through this link's ::after overlay, so the
    # anchor text stays the guide title alone.
    return (f'<{tag} class="blog-landing__title"><a href="{html.escape(_article_path(blog_handle, article))}">'
            f'{html.escape(article.get("title") or "Untitled")}</a></{tag}>')


def _card(article, blog_handle, kicker=None, feature=False):
    if feature:
        thumb = _thumb(article, (600, 900, 1200), "(max-width: 900px) 100vw, 760px", 900, eager=True)
        excerpt = _get_excerpt(article)
        excerpt_html = f'<p class="blog-landing__excerpt">{excerpt}</p>' if excerpt else ""
    else:
        thumb = _thumb(article, (400, 600, 900), "(max-width: 640px) 100vw, (max-width: 1000px) 50vw, 420px", 600)
        excerpt_html = ""
    variant = " blog-landing__card--feature" if feature else ""
    return (f'<div class="blog-landing__card{variant}">{thumb}<div class="blog-landing__card-body">'
            f'{_kicker(kicker)}{_title_link(article, blog_handle)}{excerpt_html}{_meta_html(article)}</div></div>')


def _row(article, blog_handle, kicker=None):
    thumb = _thumb(article, (200, 400), "160px", 400)
    return (f'<div class="blog-landing__row">{thumb}<div class="blog-landing__row-body">'
            f'{_kicker(kicker)}{_title_link(article, blog_handle)}{_meta_html(article)}</div></div>')


def _link_list(articles, link_of, with_date=True):
    if not articles:
        return ""
    items = []
    for a in articles:
        date_html = _time_html(a) if with_date else ""
        items.append(f'<li><a href="{html.escape(link_of(a))}">{html.escape(a.get("title") or "Untitled")}</a>'
                     f'{date_html}</li>')
    return f'<ul class="blog-landing__list">{"".join(items)}</ul>'


def _h2(text, anchor=None):
    id_attr = f' id="{anchor}"' if anchor else ""
    return f'<h2 class="blog-landing__section-title"{id_attr}>{html.escape(text)}</h2>'


def _eyebrow(text):
    return f'<p class="blog-landing__eyebrow">{html.escape(text)}</p>'


def _topic_anchor(topic):
    return "topic-" + re.sub(r"[^a-z0-9]+", "-", topic["name"].lower()).strip("-")


def _guides_label(count):
    return f"{count} guide" if count == 1 else f"{count} guides"


def _more_list(topic, rest, blog_handle):
    if not rest:
        return ""
    link_of = lambda a: _article_path(blog_handle, a)  # noqa: E731
    shown, hidden = rest[:TOPIC_LIST_VISIBLE], rest[TOPIC_LIST_VISIBLE:]
    # Collapsed links are still in the HTML, so crawlers follow every one.
    reveal = (f'<details class="blog-landing__reveal"><summary>Show {len(hidden)} more</summary>'
              f'{_link_list(hidden, link_of, with_date=False)}</details>') if hidden else ""
    title = ("" if topic is MORE_GUIDES else
             f'<h3 class="blog-landing__more-title">More {html.escape(topic["name"].lower())} guides</h3>')
    return (f'<div class="blog-landing__more">{title}'
            f'{_link_list(shown, link_of, with_date=False)}{reveal}</div>')


def _topic_section(topic, posts, blog_handle, clicks, collections, featured):
    ranked = _by_clicks(posts, blog_handle, clicks)
    # Guides already shown in "Start here" drop to the list, so the same
    # image never shows twice on the page.
    ranked.sort(key=lambda a: a.get("handle") in featured)
    # The leftover group is a plain list — one or two stray cards look broken.
    card_count = 0 if topic is MORE_GUIDES else TOPIC_CARD_COUNT
    cards, rest = ranked[:card_count], ranked[card_count:]
    blurb = f'<p class="blog-landing__blurb">{html.escape(topic["blurb"])}</p>' if topic.get("blurb") else ""
    shop = ""
    handle = topic.get("collection_handle")
    if handle and handle in (collections or {}):
        shop = (f'<a class="blog-landing__button" href="/collections/{html.escape(handle)}">'
                f'Shop {html.escape(topic["name"])} <span aria-hidden="true">&rarr;</span></a>')
    grid = (f'<div class="blog-landing__grid">{"".join(_card(a, blog_handle) for a in cards)}</div>'
            if cards else "")
    return (f'<section class="blog-landing__section blog-landing__topic">'
            f'<div class="blog-landing__head"><div class="blog-landing__head-text">'
            f'{_eyebrow(_guides_label(len(posts)))}{_h2(topic["h2"], _topic_anchor(topic))}{blurb}</div>{shop}</div>'
            f'{grid}{_more_list(topic, rest, blog_handle)}</section>')


def _topic_nav(groups):
    links = "".join(f'<li><a href="#{_topic_anchor(t)}">{html.escape(t["name"])}'
                    f'<span class="blog-landing__count">{len(posts)}</span></a></li>' for t, posts in groups)
    return (f'<nav class="blog-landing__topics" aria-label="Guide topics">'
            f'<p class="blog-landing__topics-label">Browse by topic</p><ul>{links}</ul></nav>')


def _intro_html(intro_text):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", intro_text or "") if p.strip()][:2]
    if not paragraphs:
        return ""
    return '<div class="blog-landing__intro">' + "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs) + "</div>"


def _hero_html(intro_text, hero_image_url, hero_image_alt, articles, groups):
    newest = _parse_date(articles[0].get("published_at") or articles[0].get("synced_at"))
    topic_count = sum(1 for t, _ in groups if t is not MORE_GUIDES)
    stats = [f"<li><strong>{len(articles)}</strong> guides</li>"]
    if topic_count:
        stats.append(f"<li><strong>{topic_count}</strong> topics</li>")
    if newest:
        stats.append(f'<li>Updated <time datetime="{newest.strftime("%Y-%m-%d")}">'
                     f'{newest.strftime("%B %d, %Y").replace(" 0", " ")}</time></li>')
    media = ""
    if hero_image_url:
        media = (f'<div class="blog-landing__hero-media"><img src="{html.escape(cdn_width(hero_image_url, 1200))}" '
                 f'srcset="{_srcset(hero_image_url, (600, 900, 1200, 1600))}" '
                 f'sizes="(max-width: 900px) 100vw, 640px" alt="{html.escape(hero_image_alt or "")}" '
                 f'width="1200" height="628" fetchpriority="high" decoding="async"></div>')
    variant = "" if media else " blog-landing__hero--text"
    return (f'<div class="blog-landing__hero{variant}"><div class="blog-landing__hero-text">'
            f'{_eyebrow("Guides, reviews & how-tos")}{_intro_html(intro_text)}'
            f'<ul class="blog-landing__stats">{"".join(stats)}</ul></div>{media}</div>')


def _start_here(top, blog_handle, topic_of):
    feature, side = top[0], top[1:START_HERE_COUNT]
    rows = "".join(_row(a, blog_handle, topic_of.get(a.get("handle"))) for a in side)
    return (f'<section class="blog-landing__section">{_eyebrow("Most read this quarter")}'
            f'{_h2("Start here: our most-read guides")}<div class="blog-landing__featured">'
            f'{_card(feature, blog_handle, topic_of.get(feature.get("handle")), feature=True)}'
            f'<div class="blog-landing__stack">{rows}</div></div></section>')


def _feed(articles, link_of, topic_of=None):
    items = []
    for a in articles:
        kicker = _kicker((topic_of or {}).get(a.get("handle")))
        items.append(f'<li>{kicker}<a href="{html.escape(link_of(a))}">{html.escape(a.get("title") or "Untitled")}</a>'
                     f'{_time_html(a)}</li>')
    return f'<ul class="blog-landing__feed">{"".join(items)}</ul>'


def _latest_section(articles, blog_handle, topic_of):
    return (f'<section class="blog-landing__section blog-landing__panel">{_eyebrow("Just published")}'
            f'{_h2("Latest guides")}'
            f'{_feed(articles[:LATEST_COUNT], lambda a: _article_path(blog_handle, a), topic_of)}</section>')


def _news_section(news_articles, news_blog_handle, news_hub_url):
    if not news_articles:
        return ""
    newest = sorted(news_articles, key=_pub_sort_key, reverse=True)[:NEWS_COUNT]
    more = (f'<a class="blog-landing__button blog-landing__button--ghost" href="{html.escape(news_hub_url)}">'
            f'All tobacco industry news <span aria-hidden="true">&rarr;</span></a>' if news_hub_url else "")
    return (f'<section class="blog-landing__section blog-landing__panel blog-landing__panel--news">'
            f'{_eyebrow("From the newsroom")}{_h2("Industry news")}'
            f'{_feed(newest, lambda a: _article_path(news_blog_handle, a))}{more}</section>')


def generate_blog_landing_html(articles, blog_handle, intro_text=None, hero_image_url=None, hero_image_alt=None,
                               topics=None, clicks=None, collections=None, news_articles=None, news_hub_url=None):
    """Hub body for /blogs/<blog_handle>.

    clicks: {lowercased path: {"clicks": n}} from gsc_page_totals; collections:
    handles of collections that exist (a topic's shop link is dropped
    otherwise); news_articles share the blog (blog_handle) and link to news_hub_url."""
    # Never list the landing-page article inside its own hub.
    articles = [
        a for a in articles
        if a.get("is_published") and "landing-page" not in (a.get("tags") or "").lower()
    ]
    articles.sort(key=_pub_sort_key, reverse=True)

    if not articles:
        return '<div class="blog-landing"><div class="blog-landing__empty"><p>No articles published yet.</p></div></div>'

    groups = assign_topics(articles, topics or [])
    groups.sort(key=lambda g: sum(_clicks_of(a, blog_handle, clicks) for a in g[1]), reverse=True)
    # "More guides" stays last whatever its clicks.
    groups.sort(key=lambda g: g[0] is MORE_GUIDES)
    topic_of = {a.get("handle"): t["name"] for t, posts in groups if t is not MORE_GUIDES for a in posts}

    start_here, featured = "", set()
    if clicks:
        top = [a for a in _by_clicks(articles, blog_handle, clicks) if _clicks_of(a, blog_handle, clicks) > 0]
        top = top[:START_HERE_COUNT]
        if top:
            start_here = _start_here(top, blog_handle, topic_of)
            featured = {a.get("handle") for a in top}

    topics_html = "".join(_topic_section(t, posts, blog_handle, clicks, collections, featured) for t, posts in groups)
    updates = (f'<div class="blog-landing__duo">{_latest_section(articles, blog_handle, topic_of)}'
               f'{_news_section(news_articles, blog_handle, news_hub_url)}</div>')
    # Every guide, month by month — the hub is the only listing crawlers can
    # follow once tag and paginated views are noindexed.
    _, months = split_archive(articles, 0, _pub_sort_key)
    archive = archive_html(months, lambda posts: _link_list(posts, lambda a: _article_path(blog_handle, a)),
                           "blog-landing", heading="All guides by month")

    css = _build_css() + archive_css("blog-landing") + _archive_overrides()
    return f"""<style>{css}</style>
<div class="blog-landing">
{_hero_html(intro_text, hero_image_url, hero_image_alt, articles, groups)}
{_topic_nav(groups)}
{start_here}
{topics_html}
{updates}
{archive}
</div>"""


def _build_css():
    return """
.blog-landing {
  --bl-ink: #2c2528; --bl-muted: #6f6669; --bl-green: #2c4339; --bl-green-deep: #22352d;
  --bl-sage: #889b92; --bl-sage-soft: #e6ece8; --bl-cream: #f4f2e9; --bl-line: #e6e1d6;
  --bl-radius: 14px;
  font-family: inherit !important; color: var(--bl-ink) !important;
  width: 100% !important; max-width: none !important; margin: 0 !important; padding: 0 !important;
  box-sizing: border-box !important;
}
.blog-landing *, .blog-landing *::before, .blog-landing *::after {
  box-sizing: border-box !important; text-transform: none !important; letter-spacing: normal !important;
}
.blog-landing ul, .blog-landing ol { list-style: none !important; margin: 0 !important; padding: 0 !important; }
.blog-landing li { margin: 0 !important; padding: 0 !important; }
.blog-landing li::before { content: none !important; }
.blog-landing p { margin: 0 !important; }
.blog-landing a { color: inherit !important; text-decoration: none !important; }
.blog-landing a:focus-visible, .blog-landing summary:focus-visible { outline: 2px solid var(--bl-sage) !important; outline-offset: 3px !important; border-radius: 4px !important; }
.blog-landing img { max-width: 100% !important; border: 0 !important; }
.blog-landing__empty { text-align: center; padding: 60px 20px; }
.blog-landing__empty p { color: var(--bl-muted); font-size: 16px; }

.blog-landing .blog-landing__eyebrow, .blog-landing .blog-landing__kicker, .blog-landing .blog-landing__topics-label {
  text-transform: uppercase !important; letter-spacing: 0.08em !important;
  font-size: 12px !important; font-weight: 700 !important; line-height: 1.4 !important;
}
.blog-landing__eyebrow { color: var(--bl-sage) !important; margin: 0 0 6px !important; }
.blog-landing__kicker { display: block !important; color: var(--bl-sage) !important; margin: 0 0 6px !important; font-size: 11px !important; }

/* Hero band */
.blog-landing__hero {
  display: grid !important; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr) !important;
  align-items: stretch !important; background: var(--bl-green) !important; color: var(--bl-cream) !important;
  border-radius: var(--bl-radius) !important; overflow: hidden !important; margin: 4px 0 28px !important;
}
.blog-landing__hero--text { grid-template-columns: 1fr !important; }
.blog-landing__hero-text { padding: clamp(28px, 4vw, 52px) !important; display: flex !important; flex-direction: column !important; justify-content: center !important; gap: 18px !important; }
.blog-landing__hero .blog-landing__eyebrow { color: #b9c8c0 !important; margin: 0 !important; }
.blog-landing__intro { max-width: 620px !important; display: grid !important; gap: 12px !important; }
.blog-landing__intro p { font-size: clamp(16px, 1.35vw, 18px) !important; line-height: 1.65 !important; color: var(--bl-cream) !important; }
.blog-landing__stats { display: flex !important; flex-wrap: wrap !important; gap: 10px 28px !important; padding-top: 18px !important; border-top: 1px solid rgba(244,242,233,0.18) !important; }
.blog-landing__stats li { font-size: 14px !important; color: #cfd9d3 !important; }
.blog-landing__stats strong { font-size: 22px !important; font-weight: 700 !important; color: #fff !important; margin-right: 4px !important; }
.blog-landing__hero-media { position: relative !important; min-height: 280px !important; }
.blog-landing__hero-media img { position: absolute !important; inset: 0 !important; width: 100% !important; height: 100% !important; object-fit: cover !important; display: block !important; }

/* Topic menu */
.blog-landing__topics { display: flex !important; align-items: center !important; gap: 14px !important; flex-wrap: wrap !important; margin: 0 0 48px !important; }
.blog-landing__topics-label { color: var(--bl-muted) !important; }
.blog-landing__topics ul { display: flex !important; flex-wrap: wrap !important; gap: 8px !important; }
.blog-landing__topics a {
  display: inline-flex !important; align-items: center !important; gap: 8px !important;
  padding: 7px 8px 7px 14px !important; border-radius: 999px !important; font-size: 14px !important; font-weight: 600 !important;
  background: #fff !important; border: 1px solid var(--bl-line) !important; color: var(--bl-ink) !important;
  transition: border-color .15s, background .15s !important;
}
.blog-landing__topics a:hover { border-color: var(--bl-green) !important; background: var(--bl-sage-soft) !important; }
.blog-landing__count { min-width: 24px !important; padding: 1px 7px !important; border-radius: 999px !important; background: var(--bl-cream) !important; color: var(--bl-muted) !important; font-size: 12px !important; font-weight: 700 !important; text-align: center !important; }

/* Sections */
.blog-landing__section { margin: 0 0 56px !important; }
.blog-landing__topic { padding-top: 40px !important; border-top: 1px solid var(--bl-line) !important; }
.blog-landing__section-title {
  font-size: clamp(22px, 2.2vw, 28px) !important; font-weight: 700 !important; line-height: 1.25 !important;
  color: var(--bl-ink) !important; margin: 0 0 18px !important; scroll-margin-top: 110px !important;
}
.blog-landing__head { display: flex !important; align-items: flex-end !important; justify-content: space-between !important; gap: 16px 32px !important; margin: 0 0 22px !important; }
.blog-landing__head .blog-landing__section-title { margin: 0 0 6px !important; }
.blog-landing__blurb { font-size: 15px !important; line-height: 1.55 !important; color: var(--bl-muted) !important; max-width: 640px !important; }
.blog-landing .blog-landing__button {
  display: inline-flex !important; align-items: center !important; gap: 8px !important; flex-shrink: 0 !important;
  padding: 11px 18px !important; border-radius: 999px !important; background: var(--bl-green) !important;
  color: #fff !important; font-size: 14px !important; font-weight: 700 !important; white-space: nowrap !important;
  transition: background .15s !important;
}
.blog-landing .blog-landing__button:hover { background: var(--bl-green-deep) !important; color: #fff !important; }
.blog-landing .blog-landing__button--ghost { background: transparent !important; color: var(--bl-green) !important; border: 1px solid var(--bl-green) !important; margin-top: 18px !important; }
.blog-landing .blog-landing__button--ghost:hover { background: var(--bl-sage-soft) !important; color: var(--bl-green) !important; }

/* Cards */
.blog-landing__grid { display: grid !important; grid-template-columns: repeat(3, minmax(0, 1fr)) !important; gap: 24px !important; }
.blog-landing__card, .blog-landing__row { position: relative !important; }
.blog-landing__card {
  display: flex !important; flex-direction: column !important; background: #fff !important;
  border: 1px solid var(--bl-line) !important; border-radius: var(--bl-radius) !important; overflow: hidden !important;
  transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease !important;
}
.blog-landing__card:hover { transform: translateY(-3px) !important; box-shadow: 0 12px 28px rgba(44,37,40,0.10) !important; border-color: #d8d2c4 !important; }
.blog-landing__thumb { aspect-ratio: 16 / 9 !important; overflow: hidden !important; background: var(--bl-cream) !important; line-height: 0 !important; }
.blog-landing__thumb img { width: 100% !important; height: 100% !important; object-fit: cover !important; display: block !important; transition: transform .35s ease !important; }
.blog-landing__card:hover .blog-landing__thumb img, .blog-landing__row:hover .blog-landing__thumb img { transform: scale(1.03) !important; }
.blog-landing__thumb--empty { background: linear-gradient(135deg, var(--bl-green) 0%, var(--bl-sage) 100%) !important; }
.blog-landing__card-body { display: flex !important; flex-direction: column !important; flex: 1 !important; padding: 18px 20px 20px !important; }
.blog-landing__title { margin: 0 0 10px !important; font-size: 17px !important; font-weight: 700 !important; line-height: 1.35 !important; color: var(--bl-ink) !important; }
.blog-landing__title a {
  display: -webkit-box !important; -webkit-box-orient: vertical !important; -webkit-line-clamp: 3 !important; overflow: hidden !important;
}
.blog-landing__title a::after { content: "" !important; position: absolute !important; inset: 0 !important; z-index: 1 !important; }
.blog-landing__card:hover .blog-landing__title a, .blog-landing__row:hover .blog-landing__title a { color: var(--bl-green) !important; }
.blog-landing__meta { margin-top: auto !important; font-size: 13px !important; color: var(--bl-muted) !important; }
.blog-landing__excerpt { font-size: 15px !important; line-height: 1.6 !important; color: var(--bl-muted) !important; margin: 0 0 16px !important; }

/* Most-read: one feature + a stack */
.blog-landing__featured { display: grid !important; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr) !important; gap: 24px !important; }
.blog-landing__card--feature .blog-landing__thumb { aspect-ratio: 2 / 1 !important; }
.blog-landing__card--feature .blog-landing__card-body { padding: 24px 26px 26px !important; }
.blog-landing__card--feature .blog-landing__title { font-size: clamp(20px, 2vw, 26px) !important; line-height: 1.25 !important; }
.blog-landing__stack { display: flex !important; flex-direction: column !important; gap: 14px !important; }
.blog-landing__row {
  display: grid !important; grid-template-columns: 38% minmax(0, 1fr) !important; gap: 18px !important; align-items: center !important;
  padding: 12px !important; background: #fff !important; border: 1px solid var(--bl-line) !important; border-radius: var(--bl-radius) !important;
  flex: 1 !important; transition: box-shadow .2s ease, border-color .2s ease !important;
}
.blog-landing__row:hover { box-shadow: 0 8px 22px rgba(44,37,40,0.08) !important; border-color: #d8d2c4 !important; }
.blog-landing__row .blog-landing__thumb { border-radius: 10px !important; aspect-ratio: auto !important; height: 100% !important; min-height: 110px !important; }
.blog-landing__row .blog-landing__title { font-size: 16px !important; margin-bottom: 6px !important; }

/* Topic link lists */
.blog-landing__more { margin-top: 24px !important; padding: 20px 24px !important; background: var(--bl-cream) !important; border-radius: var(--bl-radius) !important; }
.blog-landing__more-title { font-size: 14px !important; font-weight: 700 !important; color: var(--bl-ink) !important; margin: 0 0 6px !important; }
.blog-landing__list { display: grid !important; grid-template-columns: repeat(2, minmax(0, 1fr)) !important; column-gap: 40px !important; }
.blog-landing__list li { display: flex !important; align-items: baseline !important; justify-content: space-between !important; gap: 12px !important; border-bottom: 1px solid var(--bl-line) !important; }
.blog-landing__list a {
  position: relative !important; display: block !important; padding: 11px 0 11px 20px !important; font-size: 15px !important; line-height: 1.45 !important;
  color: var(--bl-ink) !important; overflow: hidden !important; text-overflow: ellipsis !important; white-space: nowrap !important; min-width: 0 !important; flex: 1 !important;
}
.blog-landing__list a::before { content: "\\2192" !important; position: absolute !important; left: 0 !important; color: var(--bl-sage) !important; transition: transform .15s !important; }
.blog-landing__list a:hover { color: var(--bl-green) !important; }
.blog-landing__list a:hover::before { transform: translateX(3px) !important; }
.blog-landing__list time { font-size: 13px !important; color: var(--bl-muted) !important; white-space: nowrap !important; }
.blog-landing__reveal > summary {
  display: inline-flex !important; align-items: center !important; gap: 6px !important; margin-top: 12px !important;
  cursor: pointer !important; list-style: none !important; font-size: 14px !important; font-weight: 700 !important; color: var(--bl-green) !important;
}
.blog-landing__reveal > summary::-webkit-details-marker { display: none !important; }
.blog-landing__reveal > summary::after { content: "+" !important; font-size: 18px !important; line-height: 1 !important; }
.blog-landing__reveal[open] > summary { display: none !important; }

/* Latest + news */
.blog-landing__duo { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)) !important; align-items: start !important; gap: 24px !important; margin: 0 0 56px !important; padding-top: 40px !important; border-top: 1px solid var(--bl-line) !important; }
.blog-landing__duo > .blog-landing__section { margin: 0 !important; }
.blog-landing__panel { padding: 28px !important; border: 1px solid var(--bl-line) !important; border-radius: var(--bl-radius) !important; background: #fff !important; }
.blog-landing__panel--news { background: var(--bl-cream) !important; border-color: transparent !important; }
.blog-landing__feed li { display: grid !important; gap: 4px !important; padding: 14px 0 !important; border-bottom: 1px solid var(--bl-line) !important; }
.blog-landing__feed li:first-child { padding-top: 0 !important; }
.blog-landing__feed .blog-landing__kicker { margin: 0 !important; }
.blog-landing__feed a { display: -webkit-box !important; -webkit-box-orient: vertical !important; -webkit-line-clamp: 2 !important; overflow: hidden !important; font-size: 16px !important; font-weight: 600 !important; line-height: 1.4 !important; color: var(--bl-ink) !important; }
.blog-landing__feed a:hover { color: var(--bl-green) !important; text-decoration: underline !important; text-underline-offset: 3px !important; }
.blog-landing__feed time { font-size: 13px !important; color: var(--bl-muted) !important; }

/* Theme: let the hub use the full container and tidy the page title */
.reading-column:has(.blog-landing) { width: auto !important; max-width: none !important; margin: 0 !important; padding: 0 !important; }
.excerpt.rte:has(.blog-landing) { max-width: none !important; }
.page-title h1 { text-transform: none !important; color: #2c2528 !important; font-weight: 700 !important; font-size: clamp(28px, 3.4vw, 42px) !important; line-height: 1.15 !important; letter-spacing: -0.01em !important; }
.page-title h1 svg { width: 18px !important; height: 18px !important; opacity: .45 !important; }
.blog-shop-row { border-bottom: 0 !important; padding: 14px 0 0 !important; margin: 0 !important; }
.blog-shop-row h2 { color: #6f6669 !important; font-size: 12px !important; letter-spacing: .08em !important; }
.blog-shop-row ul { gap: 8px !important; }
.blog-shop-row a { display: inline-block !important; padding: 6px 14px !important; border-radius: 999px !important; background: #f4f2e9 !important; font-size: 14px !important; color: #2c2528 !important; text-decoration: none !important; }
.blog-shop-row a:hover { background: #e6ece8 !important; }
.shopify-section:has(.blog-shop-row) > .section { max-width: 1296px !important; margin-left: auto !important; margin-right: auto !important; padding-left: 20px !important; padding-right: 20px !important; border-top: 0 !important; }

/* Hide theme-rendered articles — the hub replaces them */
.article:not(:first-child):not(:has(.blog-landing)),
article:not(:first-child):not(:has(.blog-landing)),
.blog-articles__article:not(:first-child):not(:has(.blog-landing)),
.articles .article:not(:first-child),
.article.full.column:not(:first-child) {
  display: none !important;
  visibility: hidden !important;
  height: 0 !important;
  overflow: hidden !important;
  margin: 0 !important;
  padding: 0 !important;
  border: none !important;
}

/* Hide theme pagination, tag filters, and other blog chrome */
.blog-listing .pagination,
.pagination { display: none !important; }
a[href*="/tagged/landing-page"] { display: none !important; }
.page-title .tags { display: none !important; }
.tags.plain.spaced { display: none !important; }

/* Clean up the article wrapper that holds the hub */
.article .lightly-spaced-row { display: none !important; }
.article .more-link { display: none !important; }
.article .excerpt { margin: 0 !important; padding: 0 !important; }
.reading-column h2:not(.blog-landing__section-title) { display: none !important; }
.reading-column { padding-top: 0 !important; padding-bottom: 0 !important; }

/* Hide theme-rendered featured image on the landing article */
.reading-column > img { display: none !important; }
.reading-column > a:not([class]) > img { display: none !important; }
.article:first-child > a > img { display: none !important; }
.article:first-child .article__featured-image { display: none !important; }
.article:first-child .blog__article-image { display: none !important; }

@media (max-width: 1000px) {
  .blog-landing__grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; }
  .blog-landing__featured { grid-template-columns: 1fr !important; }
  .blog-landing__stack { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)) !important; }
}
@media (max-width: 900px) {
  .blog-landing__hero { grid-template-columns: 1fr !important; }
  .blog-landing__hero-media { order: -1 !important; min-height: 0 !important; aspect-ratio: 1200 / 628 !important; }
}
@media (max-width: 640px) {
  .blog-landing__grid, .blog-landing__list { grid-template-columns: 1fr !important; }
  .blog-landing__head { flex-direction: column !important; align-items: flex-start !important; }
  .blog-landing__topics { flex-wrap: nowrap !important; }
  .blog-landing__topics-label { display: none !important; }
  .blog-landing__topics ul { flex-wrap: nowrap !important; overflow-x: auto !important; padding-bottom: 6px !important; scrollbar-width: none !important; }
  .blog-landing__topics li { flex: 0 0 auto !important; }
  .blog-landing__row { grid-template-columns: 112px minmax(0, 1fr) !important; }
  .blog-landing__grid { gap: 12px !important; }
  .blog-landing__grid .blog-landing__card { display: grid !important; grid-template-columns: 112px minmax(0, 1fr) !important; align-items: center !important; gap: 14px !important; padding: 10px !important; }
  .blog-landing__grid .blog-landing__thumb { border-radius: 8px !important; aspect-ratio: 1 / 1 !important; }
  .blog-landing__grid .blog-landing__card-body { padding: 0 !important; }
  .blog-landing__grid .blog-landing__title { font-size: 15px !important; margin-bottom: 4px !important; }
  .blog-landing__more, .blog-landing__panel { padding: 18px !important; }
  .blog-landing__section { margin-bottom: 40px !important; }
  .blog-landing__topic, .blog-landing__duo { padding-top: 32px !important; }
  .blog-landing__list a { white-space: normal !important; }
}
"""


def _archive_overrides():
    return """
.blog-landing__archive { max-width: none !important; margin: 0 0 24px !important; padding-top: 40px !important; border-top: 1px solid var(--bl-line) !important; }
.blog-landing__archive { display: grid !important; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)) !important; gap: 0 32px !important; align-items: start !important; }
.blog-landing__archive > .blog-landing__section-title { grid-column: 1 / -1 !important; }
.blog-landing__month { border-top: 0 !important; border-bottom: 1px solid var(--bl-line) !important; padding: 0 !important; }
.blog-landing__month > summary {
  display: flex !important; gap: 6px !important; align-items: baseline !important; list-style: none !important;
  padding: 14px 2px !important; font-size: 15px !important; font-weight: 700 !important; color: var(--bl-ink) !important; cursor: pointer !important;
}
.blog-landing__month > summary::-webkit-details-marker { display: none !important; }
.blog-landing__month > summary span { font-weight: 600 !important; color: var(--bl-muted) !important; font-size: 13px !important; }
.blog-landing__month > summary::after { content: "+" !important; color: var(--bl-sage) !important; font-size: 18px !important; margin-left: auto !important; }
.blog-landing__month[open] { grid-column: 1 / -1 !important; }
.blog-landing__month[open] > summary { margin: 0 !important; }
.blog-landing__month[open] > summary::after { content: "\\2212" !important; }
.blog-landing__month .blog-landing__list { padding-bottom: 14px !important; }
.blog-landing__month .blog-landing__list li:last-child { border-bottom: 0 !important; }
"""
