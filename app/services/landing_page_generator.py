import html
import json
import re


def _slugify(text):
    return re.sub(r"[^a-z0-9-]", "", text.strip().lower().replace(" ", "-"))


def _escape(text):
    return html.escape(text or "")


def generate_landing_page_html(page_data, products_by_id=None):
    sections = page_data.get("sections") or []
    if isinstance(sections, str):
        sections = json.loads(sections)
    sections.sort(key=lambda s: s.get("order", 0))

    css = _build_css()
    body_parts = []
    for section in sections:
        renderer = SECTION_RENDERERS.get(section.get("type"))
        if renderer:
            body_parts.append(renderer(section, products_by_id))

    joined = ''.join(body_parts)
    return f"<style>{css}</style>\n<div class='lp'>\n{joined}\n</div>"


def generate_schema_json(page_data, store_domain):
    schemas = []
    title = page_data.get("meta_title") or page_data.get("title") or ""
    description = page_data.get("meta_description") or ""
    handle = page_data.get("handle") or ""
    url = f"https://{store_domain}/pages/{handle}" if store_domain else ""

    schemas.append({
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": title,
        "description": description,
        "url": url,
    })

    schemas.append({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": f"https://{store_domain}/" if store_domain else "/"},
            {"@type": "ListItem", "position": 2, "name": title, "item": url},
        ],
    })

    sections = page_data.get("sections") or []
    if isinstance(sections, str):
        sections = json.loads(sections)

    for section in sections:
        if section.get("type") == "faq":
            items = section.get("data", {}).get("items") or []
            if items:
                schemas.append({
                    "@context": "https://schema.org",
                    "@type": "FAQPage",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": item.get("question", ""),
                            "acceptedAnswer": {
                                "@type": "Answer",
                                "text": item.get("answer", ""),
                            },
                        }
                        for item in items if item.get("question")
                    ],
                })

        if section.get("type") == "product_showcase":
            products = section.get("data", {}).get("products") or []
            if products:
                schemas.append({
                    "@context": "https://schema.org",
                    "@type": "ItemList",
                    "name": section.get("data", {}).get("heading", "Featured Products"),
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": i + 1,
                            "url": f"https://{store_domain}/products/{p.get('handle', '')}" if store_domain else f"/products/{p.get('handle', '')}",
                            "name": p.get("title", ""),
                        }
                        for i, p in enumerate(products)
                    ],
                })

    return schemas


# ── Section Renderers ──────────────────────────────────────────


def _render_hero(section, products_by_id=None):
    d = section.get("data", {})
    slides = d.get("slides") or []

    # Backward compat: convert old single-image hero to slides format
    if not slides:
        slides = [{
            "headline": d.get("headline", ""),
            "subheadline": d.get("subheadline", ""),
            "cta_text": d.get("cta_text", ""),
            "cta_url": d.get("cta_url", ""),
            "image_url": d.get("image_url", ""),
            "image_alt": d.get("image_alt", ""),
        }]

    categories = d.get("categories") or []

    if len(slides) == 1:
        return _render_single_hero(slides[0], categories)

    slide_count = len(slides)
    slides_html = ""
    dots_html = ""
    for i, slide in enumerate(slides):
        active = " lp__slide--active" if i == 0 else ""
        slides_html += _render_slide(slide, i, active)
        dot_active = " lp__dot--active" if i == 0 else ""
        dots_html += f'<button class="lp__dot{dot_active}" data-slide="{i}" aria-label="Slide {i+1}"></button>'

    cat_html = _render_category_badges(categories)

    return (
        f'<header class="lp__hero lp__hero--slider" data-slide-count="{slide_count}">\n'
        f'  <div class="lp__slides">{slides_html}</div>\n'
        f'  <button class="lp__arrow lp__arrow--prev" data-dir="-1" aria-label="Previous">&lsaquo;</button>\n'
        f'  <button class="lp__arrow lp__arrow--next" data-dir="1" aria-label="Next">&rsaquo;</button>\n'
        f'  <div class="lp__dots">{dots_html}</div>\n'
        f'  {cat_html}\n'
        f'  <script>{_slider_js()}</script>\n'
        f'</header>\n'
    )


def _render_single_hero(slide, categories):
    headline = _escape(slide.get("headline", ""))
    subheadline = _escape(slide.get("subheadline", ""))
    cta_text = _escape(slide.get("cta_text", ""))
    cta_url = _escape(slide.get("cta_url", ""))
    image_url = slide.get("image_url", "")
    image_alt = _escape(slide.get("image_alt", headline))

    bg_style = ""
    img_html = ""
    if image_url:
        bg_style = f' style="background-image: url(\'{_escape(image_url)}\');"'
        img_html = (
            f'<img src="{_escape(image_url)}" alt="{image_alt}" '
            f'width="1200" height="628" fetchpriority="high" decoding="async" '
            f'class="lp__hero-bg-img">'
        )

    cta_html = ""
    if cta_text and cta_url:
        cta_html = f'<a href="{cta_url}" class="lp__hero-cta">{cta_text}</a>'

    sub_html = f'<p class="lp__hero-sub">{subheadline}</p>' if subheadline else ""
    cat_html = _render_category_badges(categories)

    return (
        f'<header class="lp__hero"{bg_style}>\n'
        f'  {img_html}\n'
        f'  <div class="lp__hero-overlay"></div>\n'
        f'  <div class="lp__hero-content">\n'
        f'    <h1 class="lp__hero-title">{headline}</h1>\n'
        f'    {sub_html}\n'
        f'    {cta_html}\n'
        f'  </div>\n'
        f'  {cat_html}\n'
        f'</header>\n'
    )


def _render_slide(slide, index, active_class):
    headline = _escape(slide.get("headline", ""))
    subheadline = _escape(slide.get("subheadline", ""))
    cta_text = _escape(slide.get("cta_text", ""))
    cta_url = _escape(slide.get("cta_url", ""))
    image_url = slide.get("image_url", "")
    image_alt = _escape(slide.get("image_alt", headline))

    img_html = ""
    if image_url:
        priority = 'fetchpriority="high" ' if index == 0 else 'loading="lazy" '
        img_html = (
            f'<img src="{_escape(image_url)}" alt="{image_alt}" '
            f'width="1200" height="628" {priority}decoding="async" '
            f'class="lp__hero-bg-img">'
        )

    cta_html = ""
    if cta_text and cta_url:
        cta_html = f'<a href="{cta_url}" class="lp__hero-cta">{cta_text}</a>'

    sub_html = f'<p class="lp__hero-sub">{subheadline}</p>' if subheadline else ""

    return (
        f'<div class="lp__slide{active_class}" data-index="{index}">\n'
        f'  {img_html}\n'
        f'  <div class="lp__hero-overlay"></div>\n'
        f'  <div class="lp__hero-content">\n'
        f'    <h1 class="lp__hero-title">{headline}</h1>\n'
        f'    {sub_html}\n'
        f'    {cta_html}\n'
        f'  </div>\n'
        f'</div>\n'
    )


def _render_category_badges(categories):
    if not categories:
        return ""
    badges = ""
    for cat in categories[:6]:
        name = _escape(cat.get("name", ""))
        url = _escape(cat.get("url", ""))
        if name:
            badges += f'<a href="{url}" class="lp__cat-badge">{name}</a>'
    return f'<div class="lp__cat-badges">{badges}</div>\n'


def _slider_js():
    return (
        "(function(){"
        "var h=document.querySelector('.lp__hero--slider');"
        "if(!h)return;"
        "var slides=h.querySelectorAll('.lp__slide'),"
        "dots=h.querySelectorAll('.lp__dot'),"
        "cur=0,n=slides.length,timer;"
        "function go(i){"
        "slides[cur].classList.remove('lp__slide--active');"
        "dots[cur].classList.remove('lp__dot--active');"
        "cur=(i+n)%n;"
        "slides[cur].classList.add('lp__slide--active');"
        "dots[cur].classList.add('lp__dot--active');"
        "}"
        "function auto(){timer=setInterval(function(){go(cur+1)},5000);}"
        "h.querySelector('.lp__arrow--prev').onclick=function(){clearInterval(timer);go(cur-1);auto();};"
        "h.querySelector('.lp__arrow--next').onclick=function(){clearInterval(timer);go(cur+1);auto();};"
        "dots.forEach(function(d,i){d.onclick=function(){clearInterval(timer);go(i);auto();};});"
        "auto();"
        "})()"
    )


def _render_features(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", "Features"))
    items = d.get("items") or []

    cards = ""
    for item in items:
        icon = _escape(item.get("icon", ""))
        title = _escape(item.get("title", ""))
        desc = _escape(item.get("description", ""))
        icon_html = f'<div class="lp__feature-icon">{icon}</div>' if icon else ""
        cards += (
            f'<div class="lp__feature-card">\n'
            f'  {icon_html}\n'
            f'  <h3 class="lp__feature-title">{title}</h3>\n'
            f'  <p class="lp__feature-desc">{desc}</p>\n'
            f'</div>\n'
        )

    return (
        f'<section class="lp__features">\n'
        f'  <h2 class="lp__section-heading">{heading}</h2>\n'
        f'  <div class="lp__features-grid">{cards}</div>\n'
        f'</section>\n'
    )


def _render_product_showcase(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", "Featured Products"))
    products = d.get("products") or []

    cards = ""
    for p in products:
        title = _escape(p.get("title", ""))
        phandle = _escape(p.get("handle", ""))
        image = p.get("image_url", "")
        price = _escape(p.get("price", ""))

        img_html = ""
        if image:
            img_html = (
                f'<img src="{_escape(image)}" alt="{title}" '
                f'width="300" height="300" loading="lazy" decoding="async" '
                f'class="lp__product-img">'
            )

        price_html = f'<span class="lp__product-price">{price}</span>' if price else ""
        cards += (
            f'<a href="/products/{phandle}" class="lp__product-card">\n'
            f'  {img_html}\n'
            f'  <h3 class="lp__product-title">{title}</h3>\n'
            f'  {price_html}\n'
            f'</a>\n'
        )

    return (
        f'<section class="lp__product-showcase">\n'
        f'  <h2 class="lp__section-heading">{heading}</h2>\n'
        f'  <div class="lp__products-grid">{cards}</div>\n'
        f'</section>\n'
    )


def _render_testimonials(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", "Testimonials"))
    items = d.get("items") or []

    quotes = ""
    for item in items:
        quote = _escape(item.get("quote", ""))
        author = _escape(item.get("author", ""))
        role = _escape(item.get("role", ""))
        quotes += (
            f'<blockquote class="lp__testimonial">\n'
            f'  <p class="lp__testimonial-text">&ldquo;{quote}&rdquo;</p>\n'
            f'  <footer class="lp__testimonial-author">'
            f'<strong>{author}</strong>'
            f'{"<span>, " + role + "</span>" if role else ""}'
            f'</footer>\n'
            f'</blockquote>\n'
        )

    return (
        f'<section class="lp__testimonials">\n'
        f'  <h2 class="lp__section-heading">{heading}</h2>\n'
        f'  <div class="lp__testimonials-grid">{quotes}</div>\n'
        f'</section>\n'
    )


def _render_faq(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", "Frequently Asked Questions"))
    items = d.get("items") or []

    entries = ""
    for item in items:
        q = _escape(item.get("question", ""))
        a = _escape(item.get("answer", ""))
        entries += (
            f'<details class="lp__faq-item">\n'
            f'  <summary class="lp__faq-question">{q}</summary>\n'
            f'  <div class="lp__faq-answer"><p>{a}</p></div>\n'
            f'</details>\n'
        )

    return (
        f'<section class="lp__faq">\n'
        f'  <h2 class="lp__section-heading">{heading}</h2>\n'
        f'  <div class="lp__faq-list">{entries}</div>\n'
        f'</section>\n'
    )


def _render_cta_banner(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", ""))
    subtext = _escape(d.get("subtext", ""))
    cta_text = _escape(d.get("cta_text", ""))
    cta_url = _escape(d.get("cta_url", ""))

    cta_html = ""
    if cta_text and cta_url:
        cta_html = f'<a href="{cta_url}" class="lp__cta-btn">{cta_text}</a>'

    sub_html = f'<p class="lp__cta-sub">{subtext}</p>' if subtext else ""

    return (
        f'<section class="lp__cta-banner">\n'
        f'  <h2 class="lp__cta-heading">{heading}</h2>\n'
        f'  {sub_html}\n'
        f'  {cta_html}\n'
        f'</section>\n'
    )


def _render_trust_badges(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", "Why Choose Us"))
    items = d.get("items") or []

    badges = ""
    for item in items:
        icon = _escape(item.get("icon", ""))
        text = _escape(item.get("text", ""))
        badges += (
            f'<div class="lp__badge">\n'
            f'  <div class="lp__badge-icon">{icon}</div>\n'
            f'  <span class="lp__badge-text">{text}</span>\n'
            f'</div>\n'
        )

    return (
        f'<section class="lp__trust-badges">\n'
        f'  <h2 class="lp__section-heading">{heading}</h2>\n'
        f'  <div class="lp__badges-grid">{badges}</div>\n'
        f'</section>\n'
    )


def _sanitize_rich_content(content):
    if not content:
        return ""
    for tag in ('script', 'style', 'noscript', 'iframe', 'form', 'nav', 'footer', 'header'):
        content = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', content, flags=re.DOTALL | re.IGNORECASE)
    return content


def _render_rich_text(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", ""))
    content = _sanitize_rich_content(d.get("content", ""))

    heading_html = f'<h2 class="lp__section-heading">{heading}</h2>' if heading else ""

    return (
        f'<section class="lp__rich-text">\n'
        f'  {heading_html}\n'
        f'  <div class="lp__rich-content">{content}</div>\n'
        f'</section>\n'
    )


def _render_image_gallery(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", "Gallery"))
    images = d.get("images") or []

    imgs = ""
    for img in images:
        url = _escape(img.get("url", ""))
        alt = _escape(img.get("alt", ""))
        if url:
            imgs += (
                f'<div class="lp__gallery-item">\n'
                f'  <img src="{url}" alt="{alt}" width="400" height="300" loading="lazy" decoding="async">\n'
                f'</div>\n'
            )

    return (
        f'<section class="lp__image-gallery">\n'
        f'  <h2 class="lp__section-heading">{heading}</h2>\n'
        f'  <div class="lp__gallery-grid">{imgs}</div>\n'
        f'</section>\n'
    )


def _render_statistics(section, products_by_id=None):
    d = section.get("data", {})
    heading = _escape(d.get("heading", ""))
    items = d.get("items") or []

    stats = ""
    for item in items:
        value = _escape(item.get("value", ""))
        label = _escape(item.get("label", ""))
        stats += (
            f'<div class="lp__stat-card">\n'
            f'  <div class="lp__stat-value">{value}</div>\n'
            f'  <div class="lp__stat-label">{label}</div>\n'
            f'</div>\n'
        )

    heading_html = f'<h2 class="lp__section-heading">{heading}</h2>' if heading else ""

    return (
        f'<section class="lp__statistics">\n'
        f'  {heading_html}\n'
        f'  <div class="lp__stats-grid">{stats}</div>\n'
        f'</section>\n'
    )


SECTION_RENDERERS = {
    "hero": _render_hero,
    "features": _render_features,
    "product_showcase": _render_product_showcase,
    "testimonials": _render_testimonials,
    "faq": _render_faq,
    "cta_banner": _render_cta_banner,
    "trust_badges": _render_trust_badges,
    "rich_text": _render_rich_text,
    "image_gallery": _render_image_gallery,
    "statistics": _render_statistics,
}


def _build_css():
    return """
.lp {
  font-family: inherit !important;
  width: 100vw !important;
  max-width: none !important;
  margin-left: calc(-50vw + 50%) !important;
  padding: 0 !important;
  box-sizing: border-box !important;
}
.lp section, .lp header { padding: 40px 40px !important; }
.lp__section-heading { font-size: 24px !important; font-weight: 700 !important; margin: 0 0 24px !important; text-align: center !important; }

/* Hero */
.lp__hero { position: relative !important; text-align: center !important; padding: 80px 40px !important; background: #111 !important; overflow: hidden !important; min-height: 480px !important; display: flex !important; flex-direction: column !important; align-items: center !important; justify-content: center !important; box-sizing: border-box !important; }
.lp__hero-bg-img { position: absolute !important; inset: 0 !important; width: 100% !important; height: 100% !important; object-fit: cover !important; z-index: 0 !important; }
.lp__hero-overlay { position: absolute !important; inset: 0 !important; background: linear-gradient(180deg, rgba(0,0,0,0.3) 0%, rgba(0,0,0,0.5) 50%, rgba(0,0,0,0.65) 100%) !important; z-index: 1 !important; }
.lp__hero-content { position: relative !important; z-index: 2 !important; max-width: 750px !important; }
.lp__hero-title { font-size: 42px !important; font-weight: 800 !important; margin: 0 0 16px !important; line-height: 1.15 !important; color: #fff !important; text-shadow: 0 2px 12px rgba(0,0,0,0.5) !important; letter-spacing: -0.5px !important; }
.lp__hero-sub { font-size: 18px !important; color: rgba(255,255,255,0.9) !important; margin: 0 0 28px !important; line-height: 1.6 !important; text-shadow: 0 1px 6px rgba(0,0,0,0.4) !important; }
.lp__hero-cta { display: inline-block !important; padding: 14px 36px !important; background: #fff !important; color: #111 !important; text-decoration: none !important; border-radius: 8px !important; font-weight: 700 !important; font-size: 15px !important; transition: transform 0.2s, box-shadow 0.2s !important; text-transform: uppercase !important; letter-spacing: 0.5px !important; }
.lp__hero-cta:hover { transform: translateY(-2px) !important; box-shadow: 0 6px 20px rgba(0,0,0,0.3) !important; }

/* Hero Slider */
.lp__hero--slider { padding: 0 !important; }
.lp__slides { position: relative !important; width: 100% !important; min-height: 480px !important; }
.lp__slide { position: absolute !important; inset: 0 !important; opacity: 0 !important; transition: opacity 0.6s ease !important; pointer-events: none !important; display: flex !important; flex-direction: column !important; align-items: center !important; justify-content: center !important; padding: 80px 40px !important; box-sizing: border-box !important; }
.lp__slide--active { position: relative !important; opacity: 1 !important; pointer-events: auto !important; min-height: 480px !important; }
.lp__arrow { position: absolute !important; top: 50% !important; transform: translateY(-50%) !important; z-index: 10 !important; background: rgba(255,255,255,0.2) !important; color: #fff !important; border: none !important; width: 44px !important; height: 44px !important; border-radius: 50% !important; font-size: 24px !important; cursor: pointer !important; backdrop-filter: blur(4px) !important; transition: background 0.2s !important; display: flex !important; align-items: center !important; justify-content: center !important; }
.lp__arrow:hover { background: rgba(255,255,255,0.35) !important; }
.lp__arrow--prev { left: 16px !important; }
.lp__arrow--next { right: 16px !important; }
.lp__dots { position: absolute !important; bottom: 16px !important; left: 50% !important; transform: translateX(-50%) !important; z-index: 10 !important; display: flex !important; gap: 8px !important; }
.lp__dot { width: 10px !important; height: 10px !important; border-radius: 50% !important; border: 2px solid rgba(255,255,255,0.7) !important; background: transparent !important; cursor: pointer !important; padding: 0 !important; transition: background 0.2s !important; }
.lp__dot--active { background: #fff !important; }

/* Category Badges */
.lp__cat-badges { position: absolute !important; bottom: 48px !important; left: 50% !important; transform: translateX(-50%) !important; z-index: 10 !important; display: flex !important; gap: 8px !important; flex-wrap: wrap !important; justify-content: center !important; }
.lp__cat-badge { display: inline-block !important; padding: 6px 16px !important; background: rgba(255,255,255,0.15) !important; color: #fff !important; border: 1px solid rgba(255,255,255,0.3) !important; border-radius: 20px !important; font-size: 12px !important; font-weight: 600 !important; text-decoration: none !important; backdrop-filter: blur(4px) !important; transition: background 0.2s !important; text-transform: uppercase !important; letter-spacing: 0.5px !important; }
.lp__cat-badge:hover { background: rgba(255,255,255,0.25) !important; }

/* Features */
.lp__features-grid { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)) !important; gap: 24px !important; }
.lp__feature-card { background: #fff !important; border: 1px solid #e5e7eb !important; border-radius: 12px !important; padding: 24px !important; text-align: center !important; }
.lp__feature-icon { font-size: 32px !important; margin-bottom: 12px !important; }
.lp__feature-title { font-size: 16px !important; font-weight: 600 !important; margin: 0 0 8px !important; }
.lp__feature-desc { font-size: 14px !important; color: #666 !important; margin: 0 !important; line-height: 1.5 !important; }

/* Product Showcase */
.lp__products-grid { display: grid !important; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)) !important; gap: 20px !important; }
.lp__product-card { text-decoration: none !important; color: inherit !important; border: 1px solid #e5e7eb !important; border-radius: 12px !important; overflow: hidden !important; transition: transform 0.2s, box-shadow 0.2s !important; display: block !important; }
.lp__product-card:hover { transform: translateY(-2px) !important; box-shadow: 0 6px 20px rgba(0,0,0,0.08) !important; }
.lp__product-img { width: 100% !important; height: 200px !important; object-fit: cover !important; display: block !important; }
.lp__product-title { font-size: 14px !important; font-weight: 600 !important; margin: 12px !important; }
.lp__product-price { font-size: 14px !important; color: #666 !important; margin: 0 12px 12px !important; display: block !important; }

/* Testimonials */
.lp__testimonials-grid { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)) !important; gap: 24px !important; }
.lp__testimonial { background: #f9fafb !important; border-radius: 12px !important; padding: 24px !important; margin: 0 !important; border: none !important; }
.lp__testimonial-text { font-size: 15px !important; line-height: 1.6 !important; color: #333 !important; margin: 0 0 12px !important; font-style: italic !important; }
.lp__testimonial-author { font-size: 13px !important; color: #666 !important; }

/* FAQ */
.lp__faq-list { max-width: 700px !important; margin: 0 auto !important; }
.lp__faq-item { border-bottom: 1px solid #e5e7eb !important; padding: 16px 0 !important; }
.lp__faq-question { font-size: 15px !important; font-weight: 600 !important; cursor: pointer !important; list-style: none !important; }
.lp__faq-question::-webkit-details-marker { display: none !important; }
.lp__faq-answer { padding: 12px 0 0 !important; }
.lp__faq-answer p { font-size: 14px !important; color: #555 !important; line-height: 1.6 !important; margin: 0 !important; }

/* CTA Banner */
.lp__cta-banner { text-align: center !important; background: #111 !important; color: #fff !important; border-radius: 12px !important; margin: 0 40px !important; }
.lp__cta-heading { font-size: 24px !important; font-weight: 700 !important; margin: 0 0 8px !important; color: #fff !important; }
.lp__cta-sub { font-size: 15px !important; color: #ccc !important; margin: 0 0 20px !important; }
.lp__cta-btn { display: inline-block !important; padding: 12px 28px !important; background: #fff !important; color: #111 !important; text-decoration: none !important; border-radius: 8px !important; font-weight: 600 !important; }
.lp__cta-btn:hover { background: #f3f3f3 !important; }

/* Trust Badges */
.lp__badges-grid { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)) !important; gap: 20px !important; text-align: center !important; }
.lp__badge { padding: 16px !important; }
.lp__badge-icon { font-size: 28px !important; margin-bottom: 8px !important; }
.lp__badge-text { font-size: 13px !important; font-weight: 500 !important; }

/* Rich Text */
.lp__rich-text { max-width: 800px !important; margin: 0 auto !important; }
.lp__rich-content { font-size: 15px !important; line-height: 1.7 !important; color: #333 !important; }
.lp__rich-content p { margin: 0 0 16px !important; }
.lp__rich-content h2, .lp__rich-content h3 { margin: 24px 0 12px !important; }

/* Image Gallery */
.lp__gallery-grid { display: grid !important; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)) !important; gap: 16px !important; }
.lp__gallery-item img { width: 100% !important; height: auto !important; border-radius: 8px !important; display: block !important; }

/* Statistics */
.lp__stats-grid { display: grid !important; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)) !important; gap: 24px !important; text-align: center !important; }
.lp__stat-value { font-size: 32px !important; font-weight: 800 !important; color: #111 !important; }
.lp__stat-label { font-size: 13px !important; color: #666 !important; margin-top: 4px !important; }

@media (max-width: 700px) {
  .lp section, .lp header { padding: 24px 16px !important; }
  .lp__hero-title { font-size: 24px !important; }
  .lp__hero { padding: 60px 20px !important; }
  .lp__cta-banner { margin: 0 16px !important; }
  .lp__features-grid, .lp__products-grid, .lp__testimonials-grid, .lp__gallery-grid {
    grid-template-columns: 1fr !important;
  }
  .lp__arrow { width: 36px !important; height: 36px !important; font-size: 20px !important; }
  .lp__cat-badges { bottom: 42px !important; }
  .lp__cat-badge { font-size: 11px !important; padding: 4px 12px !important; }
}
"""
