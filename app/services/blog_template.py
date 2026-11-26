"""Generate the Shopify theme files that style blog (wizard) articles.

Two files are pushed to the active theme:
  - templates/article.blog.liquid   -> applied to articles with templateSuffix='blog'
  - sections/seo-blog-article.liquid -> layout, stylesheet, BlogPosting + FAQPage JSON-LD

Mirrors news_template.py. A `.liquid` template (not JSON) is used for
portability across vintage and Online Store 2.0 themes. Per-store constants
(brand color, related-article limit) are substituted via token replacement —
NOT str.format, which would clash with Liquid's {{ }} / {% %}.

The section replaces the theme's own `main-article` section, which is where
the theme renders its BlogPosting schema, so this section emits its own.
"""

import re

BLOG_TEMPLATE_SUFFIX = "blog"
BLOG_TEMPLATE_FILE = "templates/article.blog.liquid"
BLOG_SECTION_FILE = "sections/seo-blog-article.liquid"
BLOG_SECTION_NAME = "seo-blog-article"

# Bump these to force a re-push ("outdated") on existing installs.
BLOG_TEMPLATE_VERSION = "v1"
BLOG_SECTION_VERSION = "v3"

BLOG_TEMPLATE_MARKER = f"A2Z Blog Template {BLOG_TEMPLATE_VERSION}"
BLOG_SECTION_MARKER = f"A2Z Blog Section {BLOG_SECTION_VERSION}"


def blog_section_marker(emit_faq):
    """Marker for the pushed section; it records the FAQ-markup choice so
    flipping the setting shows the template as needing a re-push."""
    return f"{BLOG_SECTION_MARKER} faq-{'on' if emit_faq else 'off'}"
BLOG_TEMPLATE_MARKER_PREFIX = "A2Z Blog Template"

BLOG_TEMPLATE_BLOB = (
    "{% comment %} " + BLOG_TEMPLATE_MARKER + " {% endcomment %}\n"
    "{% section '" + BLOG_SECTION_NAME + "' %}\n"
)

DEFAULT_BRAND_COLOR = "#1a73e8"
BODY_TEXT_COLOR = "#1f2937"
PAGE_BACKGROUND = "#ffffff"

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalize_hex(value, fallback=DEFAULT_BRAND_COLOR):
    """Return a lowercase 7-char hex color, or `fallback` when invalid."""
    v = (value or "").strip()
    if not _HEX_RE.match(v):
        return fallback
    if len(v) == 4:
        v = "#" + "".join(ch * 2 for ch in v[1:])
    return v.lower()


def hex_to_rgb(hex_color):
    h = normalize_hex(hex_color)
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def relative_luminance(rgb):
    def chan(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a, hex_b):
    la = relative_luminance(hex_to_rgb(hex_a))
    lb = relative_luminance(hex_to_rgb(hex_b))
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def link_color_warnings(brand_color, body_text=BODY_TEXT_COLOR, bg=PAGE_BACKGROUND):
    """WCAG checks for a link color used inside body copy.

    Links must be distinguishable from surrounding text (>= 3:1) and readable
    against the page background (>= 4.5:1). Returns human-readable warnings;
    empty list means the color passes both.
    """
    warnings = []
    vs_text = contrast_ratio(brand_color, body_text)
    vs_bg = contrast_ratio(brand_color, bg)
    if vs_text < 3.0:
        warnings.append(
            f"Link color is only {vs_text}:1 against body text (needs 3:1) — links won't stand out."
        )
    if vs_bg < 4.5:
        warnings.append(
            f"Link color is only {vs_bg}:1 against the page background (needs 4.5:1) — hard to read."
        )
    return warnings


def _rgba(hex_color, alpha):
    r, g, b = hex_to_rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


_ARTICLE_CSS = """
.a2z-blog { max-width: 760px; margin: 0 auto; padding: 0 20px; font-family: inherit; line-height: 1.7; }
.a2z-blog__hero { margin: 0 0 28px; border-radius: 14px; overflow: hidden; line-height: 0; }
.a2z-blog__hero img { width: 100%; height: auto; display: block; }
.a2z-blog__headline { font-size: 38px; line-height: 1.15; font-weight: 800; margin: 0 0 14px; letter-spacing: -0.01em; }
.a2z-blog__byline { font-size: 14px; color: #6b7280; margin: 0 0 28px; }
.a2z-blog__dot { color: #cbd5e1; margin: 0 6px; }
.a2z-blog__body { font-size: 18px; color: #1f2937; }
.a2z-blog__body p { margin: 0 0 1.2em; }
.a2z-blog__body h2 { font-size: 26px; font-weight: 700; margin: 2em 0 0.6em; line-height: 1.25; scroll-margin-top: 90px; }
.a2z-blog__body h3 { font-size: 21px; font-weight: 700; margin: 1.5em 0 0.5em; scroll-margin-top: 90px; }
.a2z-blog__body ul, .a2z-blog__body ol { margin: 0 0 1.2em; padding-left: 1.4em; }
.a2z-blog__body li { margin: 0.3em 0; }

/* Links: brand color + underline (WCAG 1.4.1 — never color alone). Specificity 0,2,1 beats theme `.rte a`;
   the two load-bearing properties are !important because `.rte a` rules vary by theme. */
.a2z-blog .a2z-blog__body a { color: @@BRAND_COLOR@@ !important; text-decoration: underline !important; text-decoration-thickness: 2px; text-underline-offset: 3px; text-decoration-color: @@LINK_SOFT@@; font-weight: 600; transition: text-decoration-color .15s, background-color .15s; }
.a2z-blog .a2z-blog__body a:hover { text-decoration-color: currentColor; background: @@LINK_SOFT_BG@@; border-radius: 2px; }
.a2z-blog .a2z-blog__body a:focus-visible { outline: 2px solid @@BRAND_COLOR@@; outline-offset: 2px; }
.a2z-blog .a2z-blog__body h2 a, .a2z-blog .a2z-blog__body h3 a { color: inherit !important; text-decoration: none !important; }

.a2z-blog__figure { margin: 2em 0; }
.a2z-blog__figure img { width: 100%; height: auto; display: block; border-radius: 12px; }
.a2z-blog__figure figcaption { font-size: 13px; color: #6b7280; margin-top: 8px; text-align: center; }
.a2z-blog__body img:not(.a2z-blog__figure img) { max-width: 100%; height: auto; border-radius: 12px; }

.a2z-blog__label { text-transform: uppercase; letter-spacing: .08em; font-size: 12px; font-weight: 700; color: @@BRAND_COLOR@@; margin: 0 0 8px; }
.a2z-blog__takeaways { background: @@LINK_SOFT_BG@@; border-left: 4px solid @@BRAND_COLOR@@; border-radius: 10px; padding: 18px 20px; margin: 1.6em 0; }
.a2z-blog__takeaways ul { margin: 0; padding-left: 1.2em; }
.a2z-blog__takeaways li { margin: .35em 0; }

.a2z-blog__toc { border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px 20px; margin: 1.6em 0; font-size: 15px; }
.a2z-blog__toc ol { margin: 0; padding-left: 1.2em; }
.a2z-blog__toc li { margin: .25em 0; }
.a2z-blog .a2z-blog__toc a { color: #374151 !important; text-decoration: none !important; font-weight: 500; }
.a2z-blog .a2z-blog__toc a:hover { color: @@BRAND_COLOR@@ !important; text-decoration: underline !important; background: none; }

.a2z-blog__callout { border-radius: 10px; padding: 14px 18px; margin: 1.6em 0; border: 1px solid #e5e7eb; border-left-width: 4px; font-size: 17px; }
.a2z-blog__callout--tip { border-left-color: #16a34a; background: #f0fdf4; }
.a2z-blog__callout--warning { border-left-color: #d97706; background: #fffbeb; }
.a2z-blog__callout--note { border-left-color: @@BRAND_COLOR@@; background: @@LINK_SOFT_BG@@; }
.a2z-blog__callout p:last-child { margin: 0; }

.a2z-blog__table-wrap { overflow-x: auto; margin: 1.8em 0; -webkit-overflow-scrolling: touch; }
.a2z-blog__compare { width: 100%; border-collapse: collapse; font-size: 15px; }
.a2z-blog__compare caption { caption-side: bottom; font-size: 13px; color: #6b7280; padding-top: 8px; text-align: left; }
.a2z-blog__compare th, .a2z-blog__compare td { padding: 10px 12px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }
.a2z-blog__compare thead th { background: #f9fafb; font-weight: 700; border-bottom: 2px solid #d1d5db; }
.a2z-blog__compare tbody tr:nth-child(even) { background: #fafafa; }
.a2z-blog__compare td:first-child { font-weight: 600; }

.a2z-blog__steps { counter-reset: a2zstep; list-style: none; padding: 0; margin: 1.6em 0; }
.a2z-blog__steps li { position: relative; padding-left: 48px; margin: 0 0 1em; }
.a2z-blog__steps li::before { counter-increment: a2zstep; content: counter(a2zstep); position: absolute; left: 0; top: 2px; width: 32px; height: 32px; border-radius: 50%; background: @@BRAND_COLOR@@; color: #fff; font-weight: 700; display: flex; align-items: center; justify-content: center; font-size: 15px; }

.a2z-blog__quote, .a2z-blog__body blockquote { border-left: 4px solid @@BRAND_COLOR@@; margin: 1.8em 0; padding: .4em 0 .4em 1.2em; font-size: 22px; line-height: 1.4; font-style: italic; color: #374151; }
.a2z-blog__quote p:last-child { margin: 0; }

.a2z-blog__faq { margin: 2.4em 0 0; }
.a2z-blog__faq details { border-bottom: 1px solid #e5e7eb; padding: 12px 0; }
.a2z-blog__faq summary { cursor: pointer; font-weight: 600; font-size: 17px; list-style: none; display: flex; justify-content: space-between; gap: 12px; }
.a2z-blog__faq summary::-webkit-details-marker { display: none; }
.a2z-blog__faq summary::after { content: "+"; color: @@BRAND_COLOR@@; font-weight: 700; flex: none; }
.a2z-blog__faq details[open] summary::after { content: "\\2013"; }
.a2z-blog__faq details > div { padding: 8px 0 4px; font-size: 16px; }

.a2z-blog__related { margin: 48px 0 8px; border-top: 1px solid #e5e7eb; padding-top: 24px; }
.a2z-blog__related-title { font-size: 20px; font-weight: 700; margin: 0 0 16px; }
.a2z-blog__related-list { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
.a2z-blog__related-item a { display: flex; gap: 12px; align-items: center; text-decoration: none; color: inherit; }
.a2z-blog__related-item img { width: 80px; height: 56px; object-fit: cover; border-radius: 8px; flex: none; }
.a2z-blog__related-item span { font-size: 14px; font-weight: 600; line-height: 1.35; }
@media (max-width: 600px) { .a2z-blog__headline { font-size: 29px; } .a2z-blog__body { font-size: 17px; } .a2z-blog__body h2 { font-size: 23px; } .a2z-blog__quote, .a2z-blog__body blockquote { font-size: 19px; } }
"""

_SECTION_TEMPLATE = """{% comment %} @@SECTION_MARKER@@ {% endcomment %}
{%- assign a2z_words = article.content | strip_html | split: ' ' | size -%}
{%- assign a2z_minutes = a2z_words | divided_by: 200 | at_least: 1 -%}

<article class="a2z-blog">
  {% if article.image %}
  <figure class="a2z-blog__hero">
    <img src="{{ article.image | image_url: width: 1600 }}"
         alt="{{ article.image.alt | default: article.title | escape }}"
         width="1600" height="837" fetchpriority="high" decoding="async">
  </figure>
  {% endif %}

  <header class="a2z-blog__header">
    <h1 class="a2z-blog__headline">{{ article.title }}</h1>
    <p class="a2z-blog__byline">
      <time datetime="{{ article.published_at | date: '%Y-%m-%d' }}">{{ article.published_at | date: '%B %-d, %Y' }}</time>
      <span class="a2z-blog__dot">&middot;</span>{{ a2z_minutes }} min read
      {%- comment -%} Updated = the last real content change (set by the app), not
      updated_at, which moves on every link, image or price edit. {%- endcomment -%}
      {%- assign a2z_content_mod = article.metafields.global.content_updated.value | default: article.published_at -%}
      {%- assign a2z_pub_ts = article.published_at | date: '%s' | plus: 0 -%}
      {%- assign a2z_mod_ts = a2z_content_mod | date: '%s' | plus: 0 -%}
      {%- assign a2z_day_gap = a2z_mod_ts | minus: a2z_pub_ts -%}
      {%- if a2z_day_gap > 86400 -%}
      <span class="a2z-blog__dot">&middot;</span>Updated {{ a2z_content_mod | date: '%B %-d, %Y' }}
      {%- endif -%}
    </p>
  </header>

  <div class="a2z-blog__body rte">{{ article.content }}</div>

  {%- capture related_items -%}
    {%- assign shown = 0 -%}
    {%- for a in blog.articles -%}
      {%- unless a.id == article.id or a.tags contains 'type:news' or a.tags contains 'landing-page' -%}
        {%- if shown < @@RELATED_LIMIT@@ -%}
        <li class="a2z-blog__related-item">
          <a href="{{ a.url }}">
            {% if a.image %}<img src="{{ a.image | image_url: width: 480 }}" alt="{{ a.title | escape }}" width="240" height="135" loading="lazy" decoding="async">{% endif %}
            <span>{{ a.title }}</span>
          </a>
        </li>
        {%- assign shown = shown | plus: 1 -%}
        {%- endif -%}
      {%- endunless -%}
    {%- endfor -%}
  {%- endcapture -%}
  {%- if related_items != blank -%}
  <aside class="a2z-blog__related">
    <h2 class="a2z-blog__related-title">Keep reading</h2>
    <ul class="a2z-blog__related-list">{{ related_items }}</ul>
  </aside>
  {%- endif -%}
</article>

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": {{ article.title | json }},
  {% if article.excerpt != blank %}"description": {{ article.excerpt | strip_html | json }},{% endif %}
  "datePublished": "{{ article.published_at | date: '%Y-%m-%dT%H:%M:%S%z' }}",
  "dateModified": "{{ article.metafields.global.content_updated.value | default: article.published_at | date: '%Y-%m-%dT%H:%M:%S%z' }}",
  {% if article.image %}"image": ["{{ article.image | image_url: width: 1600 | prepend: 'https:' }}"],{% endif %}
  {% if article.author != blank %}{% unless shop.name contains article.author %}"author": { "@type": "Person", "name": {{ article.author | json }} },{% else %}"author": { "@type": "Organization", "name": {{ shop.name | json }}, "url": {{ shop.url | json }} },{% endunless %}{% else %}"author": { "@type": "Organization", "name": {{ shop.name | json }}, "url": {{ shop.url | json }} },{% endif %}
  "publisher": { "@type": "Organization", "name": {{ shop.name | json }}, "@id": "{{ shop.url }}/#organization" },
  "mainEntityOfPage": { "@type": "WebPage", "@id": "{{ canonical_url }}" }
}
</script>
@@FAQ_JSONLD@@
<style>
@@ARTICLE_CSS@@
</style>
{% schema %}{}{% endschema %}
"""


def get_article_css(brand_color=DEFAULT_BRAND_COLOR):
    """Return the article stylesheet (no <style> wrapper) for a brand color.

    Used both inside the theme section and by the wizard preview so what the
    user reviews is what ships.
    """
    color = normalize_hex(brand_color)
    css = _ARTICLE_CSS
    css = css.replace("@@BRAND_COLOR@@", color)
    css = css.replace("@@LINK_SOFT@@", _rgba(color, 0.45))
    css = css.replace("@@LINK_SOFT_BG@@", _rgba(color, 0.08))
    return css.strip()


_FAQ_JSONLD_BLOCK = """{%- assign a2z_faq = article.metafields.global.faq_jsonld -%}
{%- if a2z_faq != blank -%}
<script type="application/ld+json">{{ a2z_faq.value | json }}</script>
{%- endif -%}
"""


def generate_blog_section(brand_color=DEFAULT_BRAND_COLOR, related_limit=3, emit_faq=None):
    """Return the rendered sections/seo-blog-article.liquid content. FAQPage
    JSON-LD stored on older articles is printed only while FAQ markup is on
    (schema.emit_faq); the visible FAQ section is unaffected."""
    if emit_faq is None:
        from app.services.schema_generator import faq_markup_enabled

        emit_faq = faq_markup_enabled()
    content = _SECTION_TEMPLATE
    content = content.replace("@@SECTION_MARKER@@", blog_section_marker(emit_faq))
    content = content.replace("@@FAQ_JSONLD@@", _FAQ_JSONLD_BLOCK if emit_faq else "")
    content = content.replace("@@RELATED_LIMIT@@", str(max(0, int(related_limit or 0))))
    content = content.replace("@@ARTICLE_CSS@@", get_article_css(brand_color))
    return content


def get_blog_theme_files(brand_color=DEFAULT_BRAND_COLOR, related_limit=3):
    """Return {asset_key: content} for both theme files to push."""
    return {
        BLOG_SECTION_FILE: generate_blog_section(brand_color, related_limit),
        BLOG_TEMPLATE_FILE: BLOG_TEMPLATE_BLOB,
    }
