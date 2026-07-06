"""Generate storefront AEO assets: AI-crawler robots.txt rules, an llms.txt
document index, and Organization JSON-LD.

Generation is pure/read-only. Publishing (theme-asset upsert / page creation)
happens in the route layer via ShopifyClient and is always preview-first.
"""

import json

from app.database import db
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
    return (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/")


def generate_organization_jsonld(store_id):
    """Full-entity Organization schema — the signals AI engines use to recognise a
    brand as a named entity (@id, sameAs, description, contactPoint, knowsAbout)."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone() or {}
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None

    host = _store_host(store)
    base = f"https://{host}" if host else ""
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


def generate_website_jsonld(store_id):
    """WebSite schema with SearchAction (sitelinks searchbox / site recognition)."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone() or {}
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None
    host = _store_host(store)
    base = f"https://{host}" if host else ""
    if not base:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": f"{base}/#website",
        "name": settings.get("organization_name") or store.get("name"),
        "url": base,
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
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{{ _crumbs }}]}
</script>
{%- endif -%}"""


def generate_site_schema_snippet(store_id):
    """Build the ai-seo-schema.liquid snippet: static Organization + WebSite JSON-LD
    followed by a dynamic per-page BreadcrumbList."""
    org = generate_organization_jsonld(store_id)
    website = generate_website_jsonld(store_id)
    parts = ["{%- comment -%} AI SEO: site-wide schema (managed — edit via the app) {%- endcomment -%}"]
    if org:
        parts.append('<script type="application/ld+json">\n' + json.dumps(org, ensure_ascii=False) + "\n</script>")
    if website:
        parts.append('<script type="application/ld+json">\n' + json.dumps(website, ensure_ascii=False) + "\n</script>")
    parts.append(_BREADCRUMB_LIQUID)
    return "\n".join(parts)
