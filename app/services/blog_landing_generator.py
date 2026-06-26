import html
import re
from collections import Counter
from datetime import datetime


def _slugify(text):
    return re.sub(r"[^a-z0-9-]", "", text.strip().lower().replace(" ", "-"))


def _strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "")


def _truncate(text, length=160):
    text = text.strip()
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


def _format_date(dt):
    if isinstance(dt, str):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(dt.replace("+00:00", "Z").split(".")[0] + "Z", "%Y-%m-%dT%H:%M:%SZ")
                break
            except ValueError:
                continue
        else:
            return ""
    if not isinstance(dt, datetime):
        return ""
    return dt.strftime("%B %d, %Y")


def _get_excerpt(article):
    summary = (article.get("summary") or "").strip()
    if summary:
        return html.escape(_truncate(_strip_html(summary)))
    body = (article.get("body") or "").strip()
    if body:
        return html.escape(_truncate(_strip_html(body)))
    return ""


def get_tags(articles):
    tag_counter = Counter()
    for article in articles:
        tags_str = article.get("tags") or ""
        for tag in tags_str.split(","):
            tag = tag.strip()
            if tag and tag.lower() != "landing-page":
                tag_counter[tag] += 1
    return [t for t, _ in tag_counter.most_common()]


def generate_blog_landing_html(articles, blog_handle, blog_title, intro_text=None, hero_image_url=None, hero_image_alt=None):
    articles = [a for a in articles if a.get("is_published")]
    articles.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    articles = articles[:50]

    if not articles:
        return '<div class="blog-landing"><div class="blog-landing__empty"><p>No articles published yet.</p></div></div>'

    all_tags = get_tags(articles)

    css = _build_css()

    hero_html = ""
    if hero_image_url:
        safe_alt = html.escape(hero_image_alt or f"{blog_title} blog")
        hero_html = (
            f'<div class="blog-landing__hero">'
            f'<img src="{hero_image_url}" alt="{safe_alt}" width="1200" height="628" '
            f'fetchpriority="high" decoding="async" class="blog-landing__hero-img">'
            f'</div>'
        )

    intro_html = ""
    if intro_text and intro_text.strip():
        escaped_intro = html.escape(intro_text.strip())
        intro_html = f'<div class="blog-landing__intro"><p>{escaped_intro}</p></div>'
    tags_html = _build_tags(blog_handle, all_tags)
    h2_html = f'<h2 class="blog-landing__section-title">Latest {html.escape(blog_title)} Articles</h2>'
    grid_html = _build_grid(articles, blog_handle)

    return f"""<style>{css}</style>
<div class="blog-landing">
{hero_html}
{intro_html}
{tags_html}
{h2_html}
{grid_html}
</div>"""


def _build_tags(blog_handle, tags):
    if not tags:
        return ""
    tag_chips = f'<a href="/blogs/{html.escape(blog_handle)}" class="blog-landing__tag blog-landing__tag--active">All</a>'
    for tag in tags:
        slug = _slugify(tag)
        tag_chips += f'<a href="/blogs/{html.escape(blog_handle)}/tagged/{html.escape(slug)}" class="blog-landing__tag">{html.escape(tag)}</a>'

    toggle = '<button class="blog-landing__tags-toggle" style="display:none" aria-expanded="false"></button>'

    script = """<script>
(function(){
  var MAX_H=150;
  function init(){
    var c=document.querySelector('.blog-landing__tags');
    var b=document.querySelector('.blog-landing__tags-toggle');
    if(!c||!b)return;
    if(c.scrollHeight<=MAX_H)return;
    c.style.maxHeight=MAX_H+'px';
    c.style.overflow='hidden';
    var chips=c.querySelectorAll('.blog-landing__tag');
    var hidden=0;
    for(var i=0;i<chips.length;i++){if(chips[i].offsetTop>=MAX_H)hidden++;}
    b.textContent='Show all ('+hidden+' more)';
    b.style.display='';
    b.setAttribute('aria-expanded','false');
    b.addEventListener('click',function(){
      var expanded=c.style.maxHeight==='none';
      if(expanded){
        c.style.maxHeight=MAX_H+'px';
        c.style.overflow='hidden';
        var h=0;for(var i=0;i<chips.length;i++){if(chips[i].offsetTop>=MAX_H)h++;}
        b.textContent='Show all ('+h+' more)';
        b.setAttribute('aria-expanded','false');
      }else{
        c.style.maxHeight='none';
        c.style.overflow='visible';
        b.textContent='Show less';
        b.setAttribute('aria-expanded','true');
      }
    });
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',init);}else{init();}
})();
</script>"""

    return f'<div class="blog-landing__tags">{tag_chips}</div>\n{toggle}\n{script}'


def _build_grid(articles, blog_handle):
    if not articles:
        return ""

    cards = ""
    for article in articles:
        title = html.escape(article.get("title") or "Untitled")
        handle = html.escape(article.get("handle") or "")
        link = f"/blogs/{html.escape(blog_handle)}/{handle}"
        image_url = article.get("image_url") or ""
        author = html.escape(article.get("author") or "")
        date = _format_date(article.get("published_at"))
        excerpt = _get_excerpt(article)
        tags_str = article.get("tags") or ""

        tag_badges = ""
        for tag in tags_str.split(",")[:2]:
            tag = tag.strip()
            if tag and tag.lower() != "landing-page":
                tag_badges += f'<span class="blog-landing__badge">{html.escape(tag)}</span>'

        if image_url and "shopify-staged-uploads" not in image_url:
            img_html = f'<div class="blog-landing__card-image"><img src="{image_url}" alt="{title}" width="600" height="338" loading="lazy" decoding="async"></div>'
        else:
            img_html = '<div class="blog-landing__card-image blog-landing__card-image--placeholder"></div>'

        meta_parts = []
        if date:
            meta_parts.append(date)
        if author:
            meta_parts.append(author)
        meta = " &middot; ".join(meta_parts)

        cards += f"""<div class="blog-landing__card">
  <a href="{link}" class="blog-landing__card-link">
    {img_html}
    <div class="blog-landing__card-body">
      <div class="blog-landing__badges">{tag_badges}</div>
      <h3 class="blog-landing__card-title">{title}</h3>
      <p class="blog-landing__card-meta">{meta}</p>
      <p class="blog-landing__card-excerpt">{excerpt}</p>
      <span class="blog-landing__read-more">Read more &rarr;</span>
    </div>
  </a>
</div>"""

    return f'<div class="blog-landing__grid">{cards}</div>'


def _build_css():
    return """
.blog-landing {
  font-family: inherit !important;
  width: 100vw !important;
  max-width: none !important;
  margin-left: calc(-50vw + 50%) !important;
  padding: 0 40px !important;
  box-sizing: border-box !important;
}
.blog-landing__hero { margin: 0 0 24px !important; border-radius: 12px !important; overflow: hidden !important; line-height: 0 !important; }
.blog-landing__hero-img { width: 100% !important; height: auto !important; display: block !important; border-radius: 12px !important; }
.blog-landing__empty { text-align: center; padding: 60px 20px; }
.blog-landing__empty p { color: #666; font-size: 16px; }
.blog-landing__intro { margin-bottom: 20px !important; }
.blog-landing__intro p { font-size: 15px !important; line-height: 1.7 !important; color: #444 !important; margin: 0 !important; }

.blog-landing__tags {
  display: flex !important; flex-wrap: wrap !important;
  justify-content: flex-start !important; gap: 5px !important;
  margin-bottom: 16px !important;
}
.blog-landing__tag {
  display: inline-block !important; padding: 3px 10px !important; border-radius: 20px !important;
  font-size: 11px !important; text-decoration: none !important; color: #666 !important;
  background: transparent !important; border: 1px solid #ddd !important;
  font-weight: 400 !important; text-transform: none !important; transition: all 0.2s !important;
}
.blog-landing__tag:hover { border-color: #999 !important; color: #333 !important; background: transparent !important; }
.blog-landing__tag--active { background: #333 !important; color: #fff !important; border-color: #333 !important; }
.blog-landing__tag--active:hover { background: #444 !important; color: #fff !important; border-color: #444 !important; }
.blog-landing__tags-toggle {
  padding: 3px 12px !important; border-radius: 20px !important; font-size: 11px !important;
  font-weight: 500 !important; color: #555 !important; background: #f5f5f5 !important;
  border: 1px solid #ddd !important; cursor: pointer !important; margin-bottom: 16px !important;
}
.blog-landing__tags-toggle:hover { background: #eee !important; border-color: #bbb !important; color: #333 !important; }

.blog-landing__grid {
  display: flex !important;
  flex-direction: column-reverse !important;
  gap: 20px !important;
  margin-bottom: 40px !important;
}
.blog-landing__card {
  border-radius: 10px !important; overflow: hidden !important;
  border: 1px solid #e5e5e5 !important; background: #fff !important;
  transition: transform 0.2s, box-shadow 0.2s !important;
  box-sizing: border-box !important;
}
.blog-landing__card:hover { transform: translateY(-2px) !important; box-shadow: 0 6px 20px rgba(0,0,0,0.08) !important; }
.blog-landing__card-link {
  display: flex !important; text-decoration: none !important; color: inherit !important;
  flex-direction: row !important; align-items: stretch !important;
}
.blog-landing__card-image {
  width: 320px !important; min-width: 320px !important; max-height: 220px !important; overflow: hidden !important;
}
.blog-landing__card-image img {
  width: 100% !important; height: 100% !important; object-fit: cover !important; display: block !important;
}
.blog-landing__card-image--placeholder {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
}
.blog-landing__card-body { padding: 20px 24px !important; flex: 1 !important; display: flex !important; flex-direction: column !important; justify-content: center !important; }
.blog-landing__badges { display: flex !important; gap: 6px !important; margin-bottom: 6px !important; flex-wrap: wrap !important; }
.blog-landing__badge {
  display: inline-block !important; padding: 2px 8px !important; border-radius: 4px !important;
  font-size: 11px !important; font-weight: 400 !important; text-transform: none !important;
  background: transparent !important; color: #999 !important;
}
.blog-landing__card-title {
  font-size: 17px !important; font-weight: 600 !important; margin: 0 0 6px !important; line-height: 1.3 !important;
  text-transform: none !important; word-wrap: break-word !important; overflow-wrap: break-word !important;
}
.blog-landing__card-meta { font-size: 12px !important; color: #888 !important; margin: 0 0 8px !important; }
.blog-landing__card-excerpt {
  font-size: 14px !important; color: #555 !important; line-height: 1.5 !important; margin: 0 0 12px !important;
  text-transform: none !important; word-wrap: break-word !important; overflow-wrap: break-word !important;
}
.blog-landing__read-more {
  font-size: 13px !important; font-weight: 600 !important; color: #111 !important;
}
.blog-landing__section-title {
  font-size: 20px !important; font-weight: 600 !important;
  margin: 0 0 16px !important; line-height: 1.3 !important;
  text-transform: none !important;
}

/* Hide theme-rendered articles — our card grid replaces them */
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

/* Clean up the article wrapper that holds our landing page */
.article .lightly-spaced-row { display: none !important; }
.article .more-link { display: none !important; }
.article .excerpt { margin: 0 !important; padding: 0 !important; }
.reading-column h2:not(.blog-landing__section-title) { display: none !important; }
.reading-column { padding-top: 0 !important; padding-bottom: 0 !important; }

/* Hide theme-rendered featured image on landing page article */
.reading-column > img { display: none !important; }
.reading-column > a:not([class]) > img { display: none !important; }
.article:first-child > a > img { display: none !important; }
.article:first-child .article__featured-image { display: none !important; }
.article:first-child .blog__article-image { display: none !important; }

@media (max-width: 700px) {
  .blog-landing { padding: 0 16px !important; }
  .blog-landing__card-link { flex-direction: column !important; }
  .blog-landing__card-image { width: 100% !important; min-width: 100% !important; max-height: 200px !important; }
}
"""
