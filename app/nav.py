"""The app's navigation map: one source for the sidebar, page headers,
breadcrumbs, related links and the command palette."""

GROUPS = [
    {"key": "today", "label": "Today", "items": [
        {"key": "dashboard", "label": "Home", "href": "/", "icon": "home", "badge": "home",
         "desc": "Your to-do list, ranked by the extra clicks a month each job could bring. Start at the top.",
         "related": ["seo", "gsc", "content/calendar"]},
    ]},
    {"key": "fix", "label": "Fix", "items": [
        {"key": "seo", "label": "Page SEO", "href": "/seo", "icon": "search", "badge": "seo",
         "desc": "Every product and collection with its on-page score. Fix what is flagged; search results show beside it.",
         "related": ["keyword-strategy", "gsc", "duplicates", "interlinking"]},
        {"key": "interlinking", "label": "Internal links", "href": "/interlinking", "icon": "link", "badge": "interlinking",
         "desc": "Links between your own pages. Fix orphans and broken links so Google and shoppers can find every page.",
         "related": ["seo", "blog", "keyword-strategy"]},
        {"key": "duplicates", "label": "Duplicates", "href": "/duplicates", "icon": "difference",
         "desc": "Pages sharing a title or description in Google's results. Give each its own so they stop competing.",
         "related": ["seo", "gsc"]},
        {"key": "images", "label": "Missing images", "href": "/images", "icon": "hide_image",
         "desc": "Active products without an image. Generate or upload one so the page is not bare.",
         "related": ["seo"]},
    ]},
    {"key": "create", "label": "Create", "items": [
        {"key": "content/calendar", "label": "Calendar", "href": "/content/calendar", "icon": "calendar_month",
         "badge": "content", "badge_tone": "error",
         "desc": "Everything scheduled to publish, blog and news together, on one posting rhythm.",
         "related": ["blog", "news"]},
        {"key": "blog", "label": "Blog posts", "href": "/blog", "icon": "edit_note",
         "desc": "Write, schedule and refresh blog posts. Drafts publish only after you approve them.",
         "related": ["content/calendar", "content-gaps", "keyword-strategy"]},
        {"key": "news", "label": "News", "href": "/news", "icon": "newspaper",
         "desc": "Turn supplier and industry news into Product News posts. Drafts wait in Scheduled for approval.",
         "related": ["content/calendar", "blog"]},
        {"key": "content-gaps", "label": "Content ideas", "href": "/content-gaps", "icon": "lightbulb",
         "badge": "content-gaps", "badge_tone": "quiet",
         "desc": "Searches competitors rank for and you do not. Each one is an idea for a collection or article.",
         "related": ["blog", "competitors", "keyword-strategy"]},
        {"key": "landing-pages", "label": "Landing pages", "href": "/landing-pages", "icon": "web",
         "desc": "Standalone pages for a campaign or topic, and SEO for the store's existing pages.",
         "related": ["seo", "keyword-strategy"]},
        {"key": "brands", "label": "Brands", "href": "/brands", "icon": "sell",
         "desc": "One collection page per brand you carry, each with its own description.",
         "related": ["seo", "landing-pages"]},
        {"key": "home-page", "label": "Home carousel", "href": "/home-page", "icon": "view_carousel",
         "desc": "The image carousel at the top of the store's home page, plus its title and description.",
         "related": ["landing-pages"]},
    ]},
    {"key": "research", "label": "Research", "items": [
        {"key": "gsc", "label": "Search Console", "href": "/gsc", "icon": "monitoring",
         "desc": "What Google reports: searches, clicks, positions, and which pages it has indexed.",
         "related": ["seo", "keyword-strategy", "duplicates"]},
        {"key": "keyword-strategy", "label": "Keywords", "href": "/keyword-strategy", "icon": "key",
         "desc": "Which page targets which search term. One page per term, so your own pages do not compete.",
         "related": ["gsc", "semrush", "content-gaps"]},
        {"key": "competitors", "label": "Competitors", "href": "/competitors", "icon": "groups",
         "badge": "competitors", "badge_tone": "quiet",
         "desc": "Where competitors rank against you on the terms you track, checked weekly.",
         "related": ["content-gaps", "keyword-strategy"]},
        {"key": "semrush", "label": "Semrush", "href": "/semrush", "icon": "query_stats",
         "desc": "Keyword data from your Semrush exports. It feeds keywords and priorities; upload a fresh export monthly.",
         "related": ["keyword-strategy", "competitors"]},
        {"key": "ai-seo", "label": "AI Search", "href": "/ai-seo", "icon": "auto_awesome",
         "badge": "ai-seo", "badge_tone": "error",
         "desc": "How ready your pages are to be quoted by AI assistants, and whether those answers mention you.",
         "related": ["blog", "seo"]},
    ]},
]

SETUP = [
    {"key": "stores", "label": "Stores", "href": "/stores", "icon": "storefront",
     "desc": "The Shopify stores this app syncs, and their connection details.",
     "related": ["settings"]},
    {"key": "settings", "label": "Settings", "href": "/settings", "icon": "settings",
     "desc": "Connections, AI models, prompts and scoring rules.",
     "related": ["stores"]},
]

_ALL = [(g, item) for g in GROUPS for item in g["items"]] + [({"key": "setup", "label": "Setup"}, i) for i in SETUP]
_BY_KEY = {item["key"]: item for _, item in _ALL}


def match(path):
    """The nav entry and group label for a request path (None, None off-map)."""
    for group, item in _ALL:
        href = item["href"]
        if path == href or (href != "/" and path.startswith(href + "/")):
            return item, group["label"]
    return None, None


def related(item):
    return [_BY_KEY[k] for k in (item or {}).get("related", []) if k in _BY_KEY]


def palette_entries():
    return [{"label": item["label"], "group": group["label"], "icon": item["icon"], "href": item["href"]}
            for group, item in _ALL]
