"""Generate storefront AEO assets: AI-crawler robots.txt rules, an llms.txt
document index, and Organization JSON-LD.

Generation is pure/read-only. Publishing (theme-asset upsert / page creation)
happens in the route layer via ShopifyClient and is always preview-first.
"""

import json

from app.database import db
from app.services import url_utils
from app.services.ai_seo_analyzer import AI_BOTS


def generate_robots_liquid():
    """robots.txt.liquid that preserves Shopify defaults and explicitly welcomes
    AI answer-engine crawlers while blocking transactional paths."""
    lines = [
        "{% for group in robots.default_groups %}",
        "  {{ group.user_agent }}",
        "  {% for rule in group.rules %}{{ rule }}",
        "  {% endfor %}{% if group.sitemap %}{{ group.sitemap }}",
        "  {% endif %}",
        "{% endfor %}",
        "",
        "# --- AI answer-engine crawlers (managed by AI SEO) ---",
    ]
    for bot in AI_BOTS:
        lines.append(f"User-agent: {bot}")
        lines.append("Allow: /")
        lines.append("Disallow: /cart")
        lines.append("Disallow: /checkout")
        lines.append("Disallow: /account")
        lines.append("")
    return "\n".join(lines)


def generate_llms_txt(store_id):
    """Markdown document index for AI crawlers, grouped by content type."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
        cur.execute(
            "SELECT title, handle FROM synced_collections WHERE store_id = %s ORDER BY products_count DESC NULLS LAST LIMIT 40",
            (store_id,),
        )
        collections = cur.fetchall()
        cur.execute(
            "SELECT title, handle FROM synced_products WHERE store_id = %s ORDER BY synced_at DESC LIMIT 60",
            (store_id,),
        )
        products = cur.fetchall()
    if not store:
        return ""

    host = (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/")
    base = f"https://{host}" if host else ""
    name = store.get("name") or host

    out = [f"# {name}", ""]
    out.append(f"> Product catalog and category guides for {name}.")
    out.append("")
    if collections:
        out.append("## Collections")
        for c in collections:
            out.append(f"- [{c['title']}]({base}/collections/{c['handle']})")
        out.append("")
    if products:
        out.append("## Products")
        for p in products:
            out.append(f"- [{p['title']}]({base}/products/{p['handle']})")
        out.append("")
    return "\n".join(out)


def _store_host(store):
    return url_utils.store_host(store)


def generate_organization_jsonld(store_id, base_override=None):
    """Full-entity Organization schema — the signals AI engines use to recognise a
    brand as a named entity (@id, sameAs, description, contactPoint, knowsAbout).

    `base_override` swaps the computed base URL for a sentinel so the snippet
    builder can substitute `{{ shop.url }}` — keeping @id on whatever domain
    actually serves the page. Previews omit it and get the real domain."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone() or {}
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None

    host = _store_host(store)
    base = base_override or (f"https://{host}" if host else "")
    url = settings.get("organization_url") or base
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": f"{base}/#organization" if base else "",
        "name": settings.get("organization_name") or store.get("name"),
        "url": url,
    }
    if settings.get("organization_logo_url"):
        org["logo"] = settings["organization_logo_url"]
    if settings.get("organization_description"):
        org["description"] = settings["organization_description"]

    same_as = [s for s in (settings.get("organization_same_as") or []) if s]
    if same_as:
        org["sameAs"] = same_as
    knows = [k for k in (settings.get("organization_knows_about") or []) if k]
    if knows:
        org["knowsAbout"] = knows

    email = settings.get("organization_email")
    phone = settings.get("organization_phone")
    if email or phone:
        cp = {"@type": "ContactPoint", "contactType": "customer service"}
        if email:
            cp["email"] = email
        if phone:
            cp["telephone"] = phone
        org["contactPoint"] = cp
    return {k: v for k, v in org.items() if v}


def generate_website_jsonld(store_id, base_override=None):
    """WebSite schema with SearchAction (sitelinks searchbox / site recognition)."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone() or {}
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None
    host = _store_host(store)
    base = base_override or (f"https://{host}" if host else "")
    if not base:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": f"{base}/#website",
        "name": settings.get("organization_name") or store.get("name"),
        "url": base,
        "publisher": {"@id": f"{base}/#organization"},
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint", "urlTemplate": f"{base}/search?q={{search_term_string}}"},
            "query-input": "required name=search_term_string",
        },
    }


# Dynamic BreadcrumbList — rendered by Shopify Liquid per page type at request time.
_BREADCRUMB_LIQUID = """{%- capture _crumbs -%}
{%- if template contains 'product' -%}
{%- assign _col = product.collections.first -%}
{"@type":"ListItem","position":1,"name":"Home","item":"{{ shop.url }}/"}
{%- if _col -%},{"@type":"ListItem","position":2,"name":{{ _col.title | json }},"item":"{{ shop.url }}{{ _col.url }}"}{%- endif -%}
,{"@type":"ListItem","position":{% if _col %}3{% else %}2{% endif %},"name":{{ product.title | json }},"item":"{{ shop.url }}{{ product.url }}"}
{%- elsif template contains 'collection' and collection -%}
{"@type":"ListItem","position":1,"name":"Home","item":"{{ shop.url }}/"},
{"@type":"ListItem","position":2,"name":{{ collection.title | json }},"item":"{{ shop.url }}{{ collection.url }}"}
{%- elsif template contains 'article' and article -%}
{"@type":"ListItem","position":1,"name":"Home","item":"{{ shop.url }}/"},
{"@type":"ListItem","position":2,"name":{{ blog.title | json }},"item":"{{ shop.url }}{{ blog.url }}"},
{"@type":"ListItem","position":3,"name":{{ article.title | json }},"item":"{{ shop.url }}{{ article.url }}"}
{%- elsif template contains 'page' and page -%}
{"@type":"ListItem","position":1,"name":"Home","item":"{{ shop.url }}/"},
{"@type":"ListItem","position":2,"name":{{ page.title | json }},"item":"{{ shop.url }}{{ page.url }}"}
{%- endif -%}
{%- endcapture -%}
{%- assign _crumbs = _crumbs | strip -%}
{%- if _crumbs != '' -%}
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","@id":"{{ canonical_url }}#breadcrumb","itemListElement":[{{ _crumbs }}]}
</script>
{%- endif -%}"""


# Product / ProductGroup / CollectionPage JSON-LD, built natively in Liquid so
# price, availability and ratings are always request-time fresh and every URL /
# @id lives on the serving (primary) domain via `shop.url`. The optional
# `seo.schema_enrichment` metafield (json) contributes only what Liquid cannot
# know: AI-verified additionalProperty specs plus optional description/category
# overrides. Ratings are read from review-app metafields (`reviews.*` standard
# namespace first, Stamped's `stamped.*` as fallback), strictly gated on a
# review count > 0 — never fabricated. FAQPage metafields are replayed verbatim.
_ITEM_SCHEMA_LIQUID = """{%- if template contains 'product' and product -%}
{%- assign _base = shop.url | append: '/products/' | append: product.handle -%}
{%- assign _enr = product.metafields.seo.schema_enrichment.value -%}
{%- assign _rc = 0 -%}
{%- assign _rv = '' -%}
{%- if product.metafields.reviews.rating_count.value and product.metafields.reviews.rating.value -%}
{%- assign _rc = product.metafields.reviews.rating_count.value | plus: 0 -%}
{%- comment -%} metafield type `rating` -> object exposing .rating; plain numbers pass through {%- endcomment -%}
{%- assign _rv = product.metafields.reviews.rating.value.rating | default: product.metafields.reviews.rating.value.value | default: product.metafields.reviews.rating.value -%}
{%- elsif product.metafields.stamped.reviews_count.value and product.metafields.stamped.reviews_average.value -%}
{%- assign _rc = product.metafields.stamped.reviews_count.value | plus: 0 -%}
{%- assign _rv = product.metafields.stamped.reviews_average.value -%}
{%- endif -%}
{%- assign _rvnum = _rv | plus: 0.0 | round: 1 -%}
{%- capture _rating -%}
{%- if _rc > 0 and _rvnum > 0 -%},"aggregateRating":{"@type":"AggregateRating","ratingValue":{{ _rvnum }},"reviewCount":{{ _rc }},"bestRating":5,"worstRating":1}{%- endif -%}
{%- endcapture -%}
{%- capture _entity -%}
"name":{{ product.title | json }}
,"description":{{ _enr.description | default: product.description | strip_html | truncate: 5000 | json }}
,"url":{{ _base | json }}
,"brand":{"@type":"Brand","name":{{ product.vendor | default: shop.name | json }}}
{%- assign _cat = _enr.category | default: product.type -%}
{%- if _cat != blank -%},"category":{{ _cat | json }}{%- endif -%}
{%- if product.images.size > 0 -%},"image":[{%- for _img in product.images limit: 8 -%}{{ _img | image_url: width: 1200 | prepend: 'https:' | json }}{%- unless forloop.last -%},{%- endunless -%}{%- endfor -%}]{%- endif -%}
,"mainEntityOfPage":{"@type":"WebPage","@id":{{ _base | json }},"url":{{ _base | json }},"isPartOf":{"@id":"{{ shop.url }}/#website"}}
{%- if _enr.additionalProperty -%},"additionalProperty":{{ _enr.additionalProperty | json }}{%- endif -%}
{%- endcapture -%}
{%- if product.variants.size > 1 -%}
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ProductGroup","@id":"{{ _base }}#product"
,"productGroupID":"{{ product.id }}"
,{{ _entity }}
,"variesBy":[{%- for _opt in product.options -%}{{ _opt | json }}{%- unless forloop.last -%},{%- endunless -%}{%- endfor -%}]
,"hasVariant":[
{%- for v in product.variants -%}
{"@type":"Product","@id":"{{ _base }}#variant-{{ v.id }}"
,"name":{{ product.title | append: ' - ' | append: v.title | json }}
{%- if v.sku != blank -%},"sku":{{ v.sku | json }},"mpn":{{ v.sku | json }}{%- endif -%}
{%- if v.barcode != blank -%}{%- assign _bl = v.barcode | size -%},"gtin{% case _bl %}{% when 8 %}8{% when 12 %}12{% when 13 %}13{% when 14 %}14{% endcase %}":{{ v.barcode | json }}{%- endif -%}
{%- assign _vimg = v.featured_image | default: product.featured_image -%}
{%- if _vimg -%},"image":{{ _vimg | image_url: width: 1200 | prepend: 'https:' | json }}{%- endif -%}
,"additionalProperty":[{%- for _opt in product.options -%}{"@type":"PropertyValue","name":{{ _opt | json }},"value":{{ v.options[forloop.index0] | json }}}{%- unless forloop.last -%},{%- endunless -%}{%- endfor -%}]
,"offers":{"@type":"Offer","url":"{{ _base }}?variant={{ v.id }}","priceCurrency":{{ cart.currency.iso_code | json }},"price":{{ v.price | divided_by: 100.0 }},"availability":"https://schema.org/{% if v.available %}InStock{% else %}OutOfStock{% endif %}","itemCondition":"https://schema.org/NewCondition","priceValidUntil":"{{ 'now' | date: '%s' | plus: 31536000 | date: '%Y-%m-%d' }}","seller":{"@id":"{{ shop.url }}/#organization"}}}
{%- unless forloop.last -%},{%- endunless -%}
{%- endfor -%}
]{{ _rating }}}
</script>
{%- else -%}
{%- assign v = product.variants.first -%}
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","@id":"{{ _base }}#product"
,{{ _entity }}
{%- if v.sku != blank -%},"sku":{{ v.sku | json }},"mpn":{{ v.sku | json }}{%- endif -%}
{%- if v.barcode != blank -%}{%- assign _bl = v.barcode | size -%},"gtin{% case _bl %}{% when 8 %}8{% when 12 %}12{% when 13 %}13{% when 14 %}14{% endcase %}":{{ v.barcode | json }}{%- endif -%}
,"offers":{"@type":"Offer","url":{{ _base | json }},"priceCurrency":{{ cart.currency.iso_code | json }},"price":{{ v.price | divided_by: 100.0 }},"availability":"https://schema.org/{% if v.available %}InStock{% else %}OutOfStock{% endif %}","itemCondition":"https://schema.org/NewCondition","priceValidUntil":"{{ 'now' | date: '%s' | plus: 31536000 | date: '%Y-%m-%d' }}","seller":{"@id":"{{ shop.url }}/#organization"}}{{ _rating }}}
</script>
{%- endif -%}
{%- if product.metafields.seo.faq_schema_json_ld != blank -%}
<script type="application/ld+json">{{ product.metafields.seo.faq_schema_json_ld.value | json }}</script>
{%- endif -%}
{%- elsif template contains 'collection' and collection -%}
{%- assign _cbase = shop.url | append: '/collections/' | append: collection.handle -%}
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"CollectionPage","@id":"{{ _cbase }}#collection"
,"name":{{ collection.title | json }}
{%- if collection.description != blank -%},"description":{{ collection.description | strip_html | truncate: 5000 | json }}{%- endif -%}
,"url":"{{ _cbase }}"
,"mainEntityOfPage":"{{ _cbase }}"
{%- if collection.image -%},"image":{{ collection.image | image_url: width: 1200 | prepend: 'https:' | json }}{%- endif -%}
,"isPartOf":{"@id":"{{ shop.url }}/#website"}
{%- if collection.products_count > 0 -%}
,"mainEntity":{"@type":"ItemList","numberOfItems":{{ collection.products_count }},"itemListElement":[
{%- for _p in collection.products limit: 12 -%}
{"@type":"ListItem","position":{{ forloop.index }},"url":"{{ shop.url }}/products/{{ _p.handle }}","name":{{ _p.title | json }}}
{%- unless forloop.last -%},{%- endunless -%}
{%- endfor -%}]}
{%- endif -%}}
</script>
{%- if collection.metafields.seo.faq_schema_json_ld != blank -%}
<script type="application/ld+json">{{ collection.metafields.seo.faq_schema_json_ld.value | json }}</script>
{%- endif -%}
{%- endif -%}"""


def generate_a11y_snippet():
    """Theme snippet: labels links/buttons that lack discernible text so the
    accessibility tree is well-formed for screen readers AND AI browsing agents
    (fixes 'Links must have discernible text' — e.g. the Powered-by-Shopify link)."""
    return (
        "{%- comment -%} AI SEO: accessibility helper — labels controls lacking discernible text (managed) {%- endcomment -%}\n"
        "{% raw %}\n"
        "<script>\n"
        "(function(){\n"
        "  function labelize(){\n"
        "    var els = document.querySelectorAll('a[href], button, [role=\"button\"], [role=\"link\"]');\n"
        "    for (var i=0;i<els.length;i++){\n"
        "      var el = els[i];\n"
        "      if ((el.textContent||'').replace(/\\s+/g,'').length) continue;\n"
        "      if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title')) continue;\n"
        "      var label='';\n"
        "      var img = el.querySelector('img[alt]');\n"
        "      if (img && (img.getAttribute('alt')||'').trim()) { label = img.getAttribute('alt').trim(); }\n"
        "      if (!label){ var st = el.querySelector('svg title'); if (st && (st.textContent||'').trim()) label = st.textContent.trim(); }\n"
        "      if (!label){ var inner = el.querySelector('[aria-label]'); if (inner) label = inner.getAttribute('aria-label'); }\n"
        "      if (!label && el.tagName==='A' && el.href){ try{ var h=new URL(el.href).hostname.replace(/^www\\./,''); var b=(h.split('.')[0]||''); if(b) label=b.charAt(0).toUpperCase()+b.slice(1); }catch(e){} }\n"
        "      if (label) el.setAttribute('aria-label', label);\n"
        "    }\n"
        "  }\n"
        "  function run(){ try{ labelize(); }catch(e){} }\n"
        "  if (document.readyState!=='loading') run(); else document.addEventListener('DOMContentLoaded', run);\n"
        "  setTimeout(run, 2000);\n"
        "})();\n"
        "</script>\n"
        "{% endraw %}"
    )


SNIPPET_VERSION = 2


def generate_site_schema_snippet(store_id):
    """Build the ai-seo-schema.liquid snippet: Organization + WebSite JSON-LD
    (on `{{ shop.url }}` so @id identity always matches the serving domain), a
    dynamic per-page BreadcrumbList, and Liquid-native Product / ProductGroup /
    CollectionPage JSON-LD with request-time-fresh offers and ratings."""
    org = generate_organization_jsonld(store_id, base_override="__BASE__")
    website = generate_website_jsonld(store_id, base_override="__BASE__")
    parts = ["{%- comment -%} AI SEO: site-wide schema v2 (managed — edit via the app) {%- endcomment -%}"]
    for entity in (org, website):
        if entity:
            payload = json.dumps(entity, ensure_ascii=False).replace("__BASE__", "{{ shop.url }}")
            parts.append('<script type="application/ld+json">\n' + payload + "\n</script>")
    parts.append(_BREADCRUMB_LIQUID)
    parts.append(_ITEM_SCHEMA_LIQUID)
    return "\n".join(parts)
