"""Generate the Shopify theme files that style news articles.

Two files are pushed to the active theme:
  - templates/article.news.liquid  -> applied to articles with templateSuffix='news'
  - sections/seo-news-article.liquid -> the actual layout + NewsArticle JSON-LD

A `.liquid` template (not a JSON template) is used for portability across both
vintage and Online Store 2.0 themes. Per-store constants (publisher name, logo,
brand color, related-article limit) are substituted server-side via token
replacement (NOT str.format, which would clash with Liquid's {{ }} / {% %}).
"""

import json

NEWS_TEMPLATE_SUFFIX = "news"
NEWS_TEMPLATE_FILE = "templates/article.news.liquid"
NEWS_SECTION_FILE = "sections/seo-news-article.liquid"
NEWS_SECTION_NAME = "seo-news-article"

# Bump these to force a re-push ("outdated") on existing installs.
NEWS_TEMPLATE_VERSION = "v1"
NEWS_SECTION_VERSION = "v1"

NEWS_TEMPLATE_MARKER = f"A2Z News Template {NEWS_TEMPLATE_VERSION}"
NEWS_SECTION_MARKER = f"A2Z News Section {NEWS_SECTION_VERSION}"

NEWS_TEMPLATE_BLOB = (
    "{% comment %} " + NEWS_TEMPLATE_MARKER + " {% endcomment %}\n"
    "{% section '" + NEWS_SECTION_NAME + "' %}\n"
)

_SECTION_TEMPLATE = """{% comment %} @@SECTION_MARKER@@ {% endcomment %}
{%- assign cat = article.metafields.global.news_category -%}
{%- assign dmod = article.metafields.global.news_date_modified -%}

<article class="a2z-news">
  {% if article.image %}
  <figure class="a2z-news__hero">
    <img src="{{ article.image | image_url: width: 1600 }}"
         alt="{{ article.image.alt | default: article.title | escape }}"
         width="1600" height="900" fetchpriority="high" decoding="async">
  </figure>
  {% endif %}

  {% if cat != blank %}<p class="a2z-news__kicker">{{ cat | escape }}</p>{% endif %}

  <h1 class="a2z-news__headline">{{ article.title }}</h1>

  <p class="a2z-news__byline">
    By A2Z
    <span class="a2z-news__dot">&middot;</span>
    <time datetime="{{ article.published_at | date: '%Y-%m-%d' }}">{{ article.published_at | date: '%B %-d, %Y' }}</time>
    {% if dmod != blank %}<span class="a2z-news__updated"><span class="a2z-news__dot">&middot;</span> Updated {{ dmod | date: '%B %-d, %Y' }}</span>{% endif %}
  </p>

  <div class="a2z-news__body rte">{{ article.content }}</div>

  {%- capture related_items -%}
    {%- assign shown = 0 -%}
    {%- for a in blog.articles -%}
      {%- if a.id != article.id and a.tags contains 'type:news' and shown < @@RELATED_LIMIT@@ -%}
        <li class="a2z-news__related-item">
          <a href="{{ a.url }}">
            {% if a.image %}<img src="{{ a.image | image_url: width: 320 }}" alt="{{ a.title | escape }}" width="160" height="90" loading="lazy" decoding="async">{% endif %}
            <span>{{ a.title }}</span>
          </a>
        </li>
        {%- assign shown = shown | plus: 1 -%}
      {%- endif -%}
    {%- endfor -%}
  {%- endcapture -%}
  {%- if related_items != blank -%}
  <aside class="a2z-news__related">
    <h2 class="a2z-news__related-title">Related News</h2>
    <ul class="a2z-news__related-list">{{ related_items }}</ul>
  </aside>
  {%- endif -%}
</article>

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "NewsArticle",
  "headline": {{ article.title | truncate: 110, '' | json }},
  "datePublished": "{{ article.published_at | date: '%Y-%m-%dT%H:%M:%S%z' }}",
  "dateModified": "{% if dmod != blank %}{{ dmod | date: '%Y-%m-%dT%H:%M:%S%z' }}{% else %}{{ article.published_at | date: '%Y-%m-%dT%H:%M:%S%z' }}{% endif %}",
  {% if article.image %}"image": ["{{ article.image | image_url: width: 1600 | prepend: 'https:' }}"],{% endif %}
  {% if cat != blank %}"articleSection": {{ cat | json }},{% endif %}
  "author": { "@id": "{{ shop.url }}/#organization" },
  "publisher": { "@id": "{{ shop.url }}/#organization" },
  "mainEntityOfPage": { "@type": "WebPage", "@id": "{{ canonical_url }}" }
}
</script>

<style>
.a2z-news { max-width: 760px; margin: 0 auto; padding: 0 20px; font-family: inherit; line-height: 1.7; }
.a2z-news__hero { margin: 0 0 28px; border-radius: 14px; overflow: hidden; line-height: 0; }
.a2z-news__hero img { width: 100%; height: auto; display: block; }
.a2z-news__kicker { text-transform: uppercase; letter-spacing: 0.08em; font-size: 13px; font-weight: 700; color: @@BRAND_COLOR@@; margin: 0 0 8px; }
.a2z-news__headline { font-size: 38px; line-height: 1.15; font-weight: 800; margin: 0 0 14px; letter-spacing: -0.01em; }
.a2z-news__byline { font-size: 14px; color: #6b7280; margin: 0 0 28px; }
.a2z-news__dot { color: #cbd5e1; margin: 0 4px; }
.a2z-news__body { font-size: 18px; color: #1f2937; }
.a2z-news__body p { margin: 0 0 1.2em; }
.a2z-news__body h2 { font-size: 26px; font-weight: 700; margin: 1.8em 0 0.6em; line-height: 1.25; }
.a2z-news__body h3 { font-size: 21px; font-weight: 700; margin: 1.5em 0 0.5em; }
.a2z-news__body blockquote { border-left: 4px solid @@BRAND_COLOR@@; margin: 1.5em 0; padding: 0.2em 0 0.2em 1.2em; font-size: 21px; font-style: italic; color: #374151; }
.a2z-news__body a { color: @@BRAND_COLOR@@; text-decoration: underline; }
.a2z-news__figure { margin: 1.8em 0; }
.a2z-news__figure img { width: 100%; height: auto; display: block; border-radius: 12px; }
.a2z-news__figure figcaption { font-size: 13px; color: #6b7280; margin-top: 8px; text-align: center; }
.a2z-news__body .a2z-news__sources-title { font-size: 18px; margin-top: 2em; }
.a2z-news__body .a2z-news__sources { font-size: 14px; color: #6b7280; word-break: break-word; }
.a2z-news__body .a2z-news__sources a { color: @@BRAND_COLOR@@; }
.a2z-news__related { margin: 48px 0 8px; border-top: 1px solid #e5e7eb; padding-top: 24px; }
.a2z-news__related-title { font-size: 20px; font-weight: 700; margin: 0 0 16px; }
.a2z-news__related-list { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
.a2z-news__related-item a { display: flex; gap: 12px; align-items: center; text-decoration: none; color: inherit; }
.a2z-news__related-item img { width: 80px; height: 56px; object-fit: cover; border-radius: 8px; flex: none; }
.a2z-news__related-item span { font-size: 14px; font-weight: 600; line-height: 1.35; }
@media (max-width: 600px) { .a2z-news__headline { font-size: 29px; } .a2z-news__body { font-size: 17px; } }
</style>
{% schema %}{}{% endschema %}
"""


def generate_news_section(publisher_name, logo_url=None, brand_color="#1a73e8", related_limit=4):
    """Return the rendered sections/seo-news-article.liquid content for a store."""
    content = _SECTION_TEMPLATE
    content = content.replace("@@SECTION_MARKER@@", NEWS_SECTION_MARKER)
    content = content.replace("@@RELATED_LIMIT@@", str(int(related_limit)))
    content = content.replace("@@BRAND_COLOR@@", brand_color or "#1a73e8")
    content = content.replace("@@PUBLISHER_NAME_JSON@@", json.dumps(publisher_name or "News"))
    content = content.replace("@@LOGO_URL_JSON@@", json.dumps(logo_url or ""))
    return content


def get_news_theme_files(publisher_name, logo_url=None, brand_color="#1a73e8", related_limit=4):
    """Return {asset_key: content} for both theme files to push."""
    return {
        NEWS_SECTION_FILE: generate_news_section(publisher_name, logo_url, brand_color, related_limit),
        NEWS_TEMPLATE_FILE: NEWS_TEMPLATE_BLOB,
    }
