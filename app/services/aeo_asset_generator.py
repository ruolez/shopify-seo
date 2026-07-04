"""Generate storefront AEO assets: AI-crawler robots.txt rules, an llms.txt
document index, and Organization JSON-LD.

Generation is pure/read-only. Publishing (theme-asset upsert / page creation)
happens in the route layer via ShopifyClient and is always preview-first.
"""

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


def generate_organization_jsonld(store_id):
    """Organization schema from schema_settings, falling back to store fields."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM schema_settings WHERE store_id = %s", (store_id,))
        settings = cur.fetchone()
        cur.execute("SELECT * FROM stores WHERE id = %s", (store_id,))
        store = cur.fetchone()
    if not store:
        return None

    host = (store.get("primary_domain") or store.get("domain") or "").replace("https://", "").replace("http://", "").strip("/")
    url = (settings and settings.get("organization_url")) or (f"https://{host}" if host else "")
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": (settings and settings.get("organization_name")) or store.get("name"),
        "url": url,
    }
    logo = settings and settings.get("organization_logo_url")
    if logo:
        org["logo"] = logo
    return org
