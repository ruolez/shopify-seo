"""Generate a native Shopify theme section (Liquid) for an SEO-optimized home
page image carousel, plus pure helpers to inject/remove that section into the
theme's ``templates/index.json``.

The rendered output is a self-contained ``sections/*.liquid`` file: scoped CSS,
markup, a vanilla-JS slider, and JSON-LD. All values are baked server-side by
Python, so the whole body is wrapped in ``{% raw %}`` to keep the theme's Liquid
engine from touching our HTML/CSS/JS — only the trailing ``{% schema %}`` block
is Liquid.
"""

import html
import json

SECTION_TYPE = "ai-home-carousel"
SECTION_FILENAME = f"sections/{SECTION_TYPE}.liquid"

# Scoped class prefix — avoids collisions with the merchant's theme CSS.
_P = "a2z-home"


def _esc(value):
    return html.escape(str(value or ""), quote=True)


# ── Liquid section rendering ───────────────────────────────────────


def _render_slide(slide, index):
    """Render one slide. First slide carries the page <h1>; the rest use <h2>."""
    headline = _esc(slide.get("headline", ""))
    subheadline = _esc(slide.get("subheadline", ""))
    cta_text = _esc(slide.get("cta_text", ""))
    cta_url = _esc(slide.get("cta_url", ""))
    image_url = _esc(slide.get("image_url", ""))
    image_alt = _esc(slide.get("image_alt") or slide.get("headline", ""))

    heading_tag = "h1" if index == 0 else "h2"
    active = f" {_P}__slide--active" if index == 0 else ""

    img_html = ""
    if image_url:
        # First slide is the LCP element: eager + high priority. Rest lazy.
        loading = 'fetchpriority="high" decoding="async"' if index == 0 else 'loading="lazy" decoding="async"'
        img_html = (
            f'<img class="{_P}__img" src="{image_url}" alt="{image_alt}" '
            f'width="1920" height="720" {loading}>'
        )

    cta_html = ""
    if cta_text and cta_url:
        cta_html = f'<a class="{_P}__cta" href="{cta_url}">{cta_text}</a>'

    sub_html = f'<p class="{_P}__sub">{subheadline}</p>' if subheadline else ""

    return (
        f'<div class="{_P}__slide{active}" data-index="{index}">'
        f'{img_html}'
        f'<div class="{_P}__overlay"></div>'
        f'<div class="{_P}__content">'
        f'<{heading_tag} class="{_P}__title">{headline}</{heading_tag}>'
        f'{sub_html}{cta_html}'
        f'</div>'
        f'</div>'
    )


def _slider_js():
    """Vanilla JS carousel: opacity cross-fade, 5s autoplay, arrows + dots.

    Adapted from landing_page_generator._slider_js with the a2z-home prefix.
    """
    return (
        "(function(){"
        f"var h=document.querySelector('.{_P}__hero');"
        "if(!h)return;"
        f"var slides=h.querySelectorAll('.{_P}__slide'),"
        f"dots=h.querySelectorAll('.{_P}__dot'),"
        "cur=0,n=slides.length,timer;"
        "if(n<2)return;"
        "function go(i){"
        f"slides[cur].classList.remove('{_P}__slide--active');"
        f"if(dots[cur])dots[cur].classList.remove('{_P}__dot--active');"
        "cur=(i+n)%n;"
        f"slides[cur].classList.add('{_P}__slide--active');"
        f"if(dots[cur])dots[cur].classList.add('{_P}__dot--active');"
        "}"
        "function auto(){timer=setInterval(function(){go(cur+1)},5000);}"
        f"var p=h.querySelector('.{_P}__arrow--prev'),nx=h.querySelector('.{_P}__arrow--next');"
        "if(p)p.onclick=function(){clearInterval(timer);go(cur-1);auto();};"
        "if(nx)nx.onclick=function(){clearInterval(timer);go(cur+1);auto();};"
        "dots.forEach(function(d,i){d.onclick=function(){clearInterval(timer);go(i);auto();};});"
        "auto();"
        "})()"
    )


def _styles():
    return (
        f".{_P}__hero{{position:relative;width:100%;height:70vh;min-height:480px;max-height:760px;overflow:hidden;background:#111}}"
        f".{_P}__slides{{position:absolute;inset:0}}"
        f".{_P}__slide{{position:absolute;inset:0;opacity:0;visibility:hidden;transition:opacity .8s ease;display:flex;align-items:center;justify-content:center}}"
        f".{_P}__slide--active{{opacity:1;visibility:visible}}"
        f".{_P}__img{{position:absolute;inset:-1px;width:calc(100% + 2px);height:calc(100% + 2px);object-fit:cover;display:block}}"
        f".{_P}__overlay{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.35),rgba(0,0,0,.6))}}"
        f".{_P}__content{{position:relative;z-index:2;max-width:760px;padding:32px 24px;text-align:center;color:#fff}}"
        f".{_P}__title{{margin:0 0 16px;font-size:clamp(28px,5vw,52px);line-height:1.1;font-weight:800;letter-spacing:-.02em;color:#fff !important;text-shadow:0 2px 14px rgba(0,0,0,.5)}}"
        f".{_P}__sub{{margin:0 0 24px;font-size:clamp(15px,2vw,20px);line-height:1.5;color:#fff !important;text-shadow:0 1px 10px rgba(0,0,0,.45)}}"
        f".{_P}__cta{{display:inline-block;background:#fff;color:#111;padding:14px 32px;border-radius:999px;font-weight:600;text-decoration:none;transition:transform .15s ease}}"
        f".{_P}__cta:hover{{transform:translateY(-2px)}}"
        f".{_P}__arrow{{position:absolute;top:50%;transform:translateY(-50%);z-index:3;width:44px;height:44px;border:0;border-radius:50%;background:rgba(255,255,255,.85);color:#111;font-size:24px;cursor:pointer;display:flex;align-items:center;justify-content:center}}"
        f".{_P}__arrow--prev{{left:16px}}"
        f".{_P}__arrow--next{{right:16px}}"
        f".{_P}__dots{{position:absolute;bottom:18px;left:0;right:0;z-index:3;display:flex;gap:8px;justify-content:center}}"
        f".{_P}__dot{{width:10px;height:10px;border:0;border-radius:50%;background:rgba(255,255,255,.5);cursor:pointer;padding:0}}"
        f".{_P}__dot--active{{background:#fff}}"
        f"@media(max-width:600px){{.{_P}__hero{{min-height:420px}}.{_P}__arrow{{display:none}}}}"
    )


def _schema_block():
    """Minimal, valid section schema so the section is editable/removable in the
    theme editor. No settings — content is managed by this app."""
    schema = {
        "name": "AI Home Carousel",
        "settings": [],
        "presets": [{"name": "AI Home Carousel"}],
    }
    return "{% schema %}\n" + json.dumps(schema, indent=2) + "\n{% endschema %}"


def _slides_of(home_page, store):
    slides = home_page.get("slides") or []
    if isinstance(slides, str):
        slides = json.loads(slides)
    if not slides:
        slides = [{
            "headline": home_page.get("meta_title") or (store.get("name") or "Welcome"),
            "subheadline": home_page.get("meta_description") or "",
            "cta_text": "Shop Now",
            "cta_url": "/collections/all",
            "image_url": "",
            "image_alt": "",
        }]
    return slides


def render_carousel_body(home_page, store):
    """Render just the carousel markup + CSS + JS (no Liquid wrappers).

    Used both for the live section body and for the editor preview iframe.
    Emits NO JSON-LD — Organization/WebSite entities are owned exclusively by
    ai-seo-schema.liquid, which renders on every page; a second copy here
    shared the same @ids with a conflicting name and corrupted the graph.
    """
    slides = _slides_of(home_page, store)
    slides_html = "".join(_render_slide(s, i) for i, s in enumerate(slides))

    dots_html = ""
    arrows_html = ""
    if len(slides) > 1:
        for i in range(len(slides)):
            active = f" {_P}__dot--active" if i == 0 else ""
            dots_html += (
                f'<button class="{_P}__dot{active}" data-slide="{i}" '
                f'aria-label="Go to slide {i + 1}"></button>'
            )
        dots_html = f'<div class="{_P}__dots">{dots_html}</div>'
        arrows_html = (
            f'<button class="{_P}__arrow {_P}__arrow--prev" aria-label="Previous slide">&lsaquo;</button>'
            f'<button class="{_P}__arrow {_P}__arrow--next" aria-label="Next slide">&rsaquo;</button>'
        )

    return (
        f'<section class="{_P}__hero" data-slide-count="{len(slides)}">'
        f'<div class="{_P}__slides">{slides_html}</div>'
        f'{arrows_html}{dots_html}'
        f'</section>'
        f'<style>{_styles()}</style>'
        f'<script>{_slider_js()}</script>'
    )


def render_home_carousel_section(home_page, store):
    """Return the full body of the ``sections/ai-home-carousel.liquid`` file."""
    body = render_carousel_body(home_page, store)
    return f"{{% raw %}}\n{body}\n{{% endraw %}}\n{_schema_block()}\n"


# ── index.json manipulation (pure) ─────────────────────────────────


def _load_index(index_json_raw):
    if index_json_raw:
        template = json.loads(index_json_raw)
    else:
        template = {}
    if not isinstance(template.get("sections"), dict):
        template["sections"] = {}
    if not isinstance(template.get("order"), list):
        template["order"] = list(template["sections"].keys())
    return template


def inject_section_into_index(index_json_raw, section_key):
    """Add our carousel section to ``templates/index.json`` at the top of the
    section order. Idempotent: re-running replaces the entry rather than
    duplicating it."""
    template = _load_index(index_json_raw)
    template["sections"][section_key] = {"type": SECTION_TYPE, "settings": {}}
    order = [k for k in template["order"] if k != section_key]
    order.insert(0, section_key)
    template["order"] = order
    return json.dumps(template, indent=2)


def remove_section_from_index(index_json_raw, section_key):
    """Remove our carousel section from ``templates/index.json``."""
    template = _load_index(index_json_raw)
    template["sections"].pop(section_key, None)
    template["order"] = [k for k in template["order"] if k != section_key]
    return json.dumps(template, indent=2)
