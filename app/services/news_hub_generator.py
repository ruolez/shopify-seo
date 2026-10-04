"""Generate the standalone News hub page HTML (a Shopify Page at /pages/news).

Distinct from the blog landing page: this lists only news articles
(article_type='news'), newest-first, with client-side category-chip filtering.
Cards link to /blogs/news/{handle} so article URLs stay under /blogs/news/.
"""

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.listing_archive import archive_css, archive_html, split_archive

NEWS_BLOG_HANDLE = "news"


def _strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "")


def _truncate(text, length=160):
    text = (text or "").strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


def _format_date(dt, tz=None):
    """Format a published_at value as "Month DD, YYYY".

    Stored timestamps are UTC; Shopify's storefront renders them in the store's
    local timezone, so we convert to `tz` (an IANA name) before extracting the
    calendar day — otherwise the hub date can differ from Shopify by one day
    near midnight UTC.
    """
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s.split("+")[0].split(".")[0].strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return ""
    if not isinstance(dt, datetime):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if tz:
        try:
            dt = dt.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    return dt.strftime("%B %d, %Y")


def _pub_sort_key(a):
    """Stable sort key for published_at that tolerates datetime, str, or None."""
    pa = a.get("published_at")
    if not pa:
        return ""
    return pa.isoformat() if hasattr(pa, "isoformat") else str(pa)


def _excerpt(article):
    summary = (article.get("summary") or "").strip()
    if summary:
        return html.escape(_truncate(_strip_html(summary)))
    body = (article.get("body") or "").strip()
    if body:
        return html.escape(_truncate(_strip_html(body)))
    return ""


RECENT_COUNT = 30


def _categories(articles):
    seen = []
    for a in articles:
        cat = (a.get("category") or "").strip()
        if cat and cat not in seen:
            seen.append(cat)
    return seen


def generate_news_hub_html(articles, intro_text=None, hero_image_url=None, hero_image_alt=None, heading="News", tz=None):
    articles = [a for a in articles if a.get("is_published")]
    articles.sort(key=_pub_sort_key, reverse=True)

    if not articles:
        return '<div class="news-hub"><div class="news-hub__empty"><p>No news published yet.</p></div></div>'

    css = _build_css() + archive_css("news-hub")

    hero_html = ""
    if hero_image_url:
        safe_alt = html.escape(hero_image_alt or f"{heading}")
        hero_html = (
            f'<div class="news-hub__hero">'
            f'<img src="{hero_image_url}" alt="{safe_alt}" width="1200" height="628" '
            f'fetchpriority="high" decoding="async" class="news-hub__hero-img">'
            f'</div>'
        )

    intro_html = ""
    if intro_text and intro_text.strip():
        intro_html = f'<div class="news-hub__intro"><p>{html.escape(intro_text.strip())}</p></div>'

    chips_html = _build_chips(_categories(articles))
    # Every post stays reachable: newest on top, the rest in a monthly archive.
    recent, months = split_archive(articles, RECENT_COUNT, _pub_sort_key)
    grid_html = _build_grid(recent, tz=tz)
    archive = archive_html(months, lambda posts: _build_grid(posts, tz=tz), "news-hub",
                           heading=f"Older {heading.lower()}")

    return f"""<style>{css}</style>
<div class="news-hub">
{hero_html}
{intro_html}
<h2 class="news-hub__section-title">Latest {html.escape(heading)}</h2>
{chips_html}
{grid_html}
{archive}
</div>"""


def _build_chips(categories):
    if not categories:
        return ""
    chips = '<button class="news-hub__chip news-hub__chip--active" data-cat="">All</button>'
    for cat in categories:
        chips += f'<button class="news-hub__chip" data-cat="{html.escape(cat)}">{html.escape(cat)}</button>'

    script = """<script>
(function(){
  function init(){
    var bar=document.querySelector('.news-hub__chips');
    if(!bar)return;
    bar.addEventListener('click',function(e){
      var btn=e.target.closest('.news-hub__chip');
      if(!btn)return;
      var cat=btn.getAttribute('data-cat')||'';
      bar.querySelectorAll('.news-hub__chip').forEach(function(c){c.classList.toggle('news-hub__chip--active',c===btn);});
      document.querySelectorAll('.news-hub__card').forEach(function(card){
        var show=!cat||card.getAttribute('data-cat')===cat;
        card.style.display=show?'':'none';
      });
      // A category can live entirely in the archive: open the months so its posts show.
      document.querySelectorAll('.news-hub__month').forEach(function(m){
        m.open=!!cat&&!!m.querySelector('.news-hub__card:not([style*="none"])');
        m.style.display=(!cat||m.open)?'':'none';
      });
    });
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',init);}else{init();}
})();
</script>"""
    return f'<div class="news-hub__chips">{chips}</div>\n{script}'


def _build_grid(articles, tz=None):
    cards = ""
    for a in articles:
        title = html.escape(a.get("title") or "Untitled")
        handle = html.escape(a.get("handle") or "")
        link = f"/blogs/{NEWS_BLOG_HANDLE}/{handle}"
        image_url = a.get("image_url") or ""
        date = _format_date(a.get("published_at"), tz=tz)
        excerpt = _excerpt(a)
        category = a.get("category") or ""
        cat_badge = f'<span class="news-hub__badge">{html.escape(category)}</span>' if category else ""

        if image_url and "shopify-staged-uploads" not in image_url:
            img_html = f'<div class="news-hub__card-image"><img src="{image_url}" alt="{title}" width="600" height="338" loading="lazy" decoding="async"></div>'
        else:
            img_html = '<div class="news-hub__card-image news-hub__card-image--placeholder"></div>'

        cards += f"""<div class="news-hub__card" data-cat="{html.escape(category)}">
  {img_html}
  <div class="news-hub__card-body">
    <div class="news-hub__badges">{cat_badge}</div>
    <h3 class="news-hub__card-title"><a href="{link}" class="news-hub__card-link">{title}</a></h3>
    <p class="news-hub__card-meta">{date}</p>
    <p class="news-hub__card-excerpt">{excerpt}</p>
    <span class="news-hub__read-more" aria-hidden="true">Read more &rarr;</span>
  </div>
</div>"""

    return f'<div class="news-hub__grid">{cards}</div>'


def _build_css():
    return """
.news-hub { font-family: inherit !important; box-sizing: border-box !important; width: 100vw !important; max-width: none !important; margin-left: calc(-50vw + 50%) !important; padding: 0 40px !important; }
.news-hub__hero, .news-hub__intro, .news-hub__section-title, .news-hub__chips, .news-hub__grid { max-width: 1400px !important; margin-left: auto !important; margin-right: auto !important; }
.news-hub__hero { margin: 0 auto 24px !important; border-radius: 12px !important; overflow: hidden !important; line-height: 0 !important; }
.news-hub__hero-img { width: 100% !important; height: auto !important; display: block !important; border-radius: 12px !important; }
.news-hub__empty { text-align: center; padding: 60px 20px; color: #666; }
.news-hub__intro { margin-bottom: 20px !important; }
.news-hub__intro p { font-size: 15px !important; line-height: 1.7 !important; color: #444 !important; margin: 0 !important; }
.news-hub__section-title { font-size: 20px !important; font-weight: 600 !important; margin: 0 0 16px !important; line-height: 1.3 !important; text-transform: none !important; }
.news-hub__chips { display: flex !important; flex-wrap: wrap !important; gap: 6px !important; margin-bottom: 20px !important; }
.news-hub__chip { display: inline-block !important; padding: 4px 12px !important; border-radius: 20px !important; font-size: 12px !important; color: #555 !important; background: transparent !important; border: 1px solid #ddd !important; cursor: pointer !important; transition: all 0.2s !important; }
.news-hub__chip:hover { border-color: #999 !important; color: #222 !important; }
.news-hub__chip--active { background: #333 !important; color: #fff !important; border-color: #333 !important; }
.news-hub__grid { display: grid !important; grid-template-columns: repeat(3, minmax(0, 1fr)) !important; gap: 20px !important; margin: 0 auto 40px !important; }
.news-hub__card { position: relative !important; display: flex !important; flex-direction: column !important; border-radius: 10px !important; overflow: hidden !important; border: 1px solid #e5e5e5 !important; background: #fff !important; transition: transform 0.2s, box-shadow 0.2s !important; }
.news-hub__card:hover { transform: translateY(-2px) !important; box-shadow: 0 6px 20px rgba(0,0,0,0.08) !important; }
.news-hub__card-link { text-decoration: none !important; color: inherit !important; }
.news-hub__card-link:hover { text-decoration: underline !important; }
.news-hub__card-link::after { content: "" !important; position: absolute !important; inset: 0 !important; z-index: 1 !important; }
.news-hub__card-image { width: 100% !important; aspect-ratio: 16/9 !important; overflow: hidden !important; }
.news-hub__card-image img { width: 100% !important; height: 100% !important; object-fit: cover !important; display: block !important; }
.news-hub__card-image--placeholder { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important; }
.news-hub__card-body { padding: 16px 20px !important; flex: 1 !important; display: flex !important; flex-direction: column !important; }
.news-hub__badges { display: flex !important; gap: 6px !important; margin-bottom: 6px !important; }
.news-hub__badge { display: inline-block !important; padding: 2px 8px !important; border-radius: 4px !important; font-size: 11px !important; background: #f0f0f0 !important; color: #666 !important; }
.news-hub__card-title { font-size: 16px !important; font-weight: 600 !important; margin: 0 0 6px !important; line-height: 1.3 !important; text-transform: none !important; }
.news-hub__card-meta { font-size: 12px !important; color: #888 !important; margin: 0 0 8px !important; }
.news-hub__card-excerpt { font-size: 14px !important; color: #555 !important; line-height: 1.5 !important; margin: 0 0 12px !important; flex: 1 !important; }
.news-hub__read-more { font-size: 13px !important; font-weight: 600 !important; color: #111 !important; }
@media (max-width: 1024px) { .news-hub__grid { grid-template-columns: repeat(2, minmax(0, 1fr)) !important; } .news-hub { padding: 0 20px !important; } }
@media (max-width: 640px) { .news-hub__grid { grid-template-columns: 1fr !important; } }
"""
