import html as html_lib
import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from app.database import db
from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS

logger = logging.getLogger(__name__)

# LINK_RE is the regex fallback for extract_links_from_html when the HTML
# parser chokes on malformed markup. Link extraction and the in-place edit
# helpers in interlinking_routes.py otherwise use the parser + normalized
# href comparison.
LINK_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)

# Optional Shopify locale/market path prefix, e.g. /en, /fr-ca, /en-us.
_LOCALE_PREFIX_RE = re.compile(r"^/[a-z]{2}(?:-[a-z0-9]{2,})?(?=/)", re.IGNORECASE)

# Reserved storefront collection handles that always resolve (no synced row).
_RESERVED_COLLECTION_HANDLES = {"all"}

# Path segment charset for Shopify handles (lowercase alnum + hyphen) plus
# percent-encoding and dots, defensively.
_SEG = r"[\w\-.%]+"
_COLLECTION_PRODUCT_RE = re.compile(rf"^/collections/{_SEG}/products/({_SEG})/?$")
_PRODUCT_RE = re.compile(rf"^/products/({_SEG})/?$")
_ARTICLE_RE = re.compile(rf"^/blogs/({_SEG})/({_SEG})/?$")
_BLOG_INDEX_RE = re.compile(rf"^/blogs/({_SEG})/?$")
_COLLECTION_RE = re.compile(rf"^/collections/({_SEG})/?$")
_PAGE_RE = re.compile(rf"^/pages/({_SEG})/?$")

# Link types whose target can be verified for existence (and thus flagged broken).
CHECKABLE_TYPES = {"product", "collection", "article", "page", "blog_index"}
# Link types that contribute to a tracked entity's incoming-link count.
TRACKED_TARGET_TYPES = {"product", "collection", "article"}


class _AnchorParser(HTMLParser):
    """Collect (href, anchor_text) for every <a> tag, decoding HTML entities."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._open = False
        self._href = None
        self._text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        if self._open:
            self._flush()
        href = None
        for k, v in attrs:
            if k.lower() == "href":
                href = v
                break
        self._href = href
        self._text_parts = []
        self._open = True

    def handle_data(self, data):
        if self._open:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._open:
            self._flush()

    def _flush(self):
        if self._href is not None:
            text = "".join(self._text_parts).strip()
            self.links.append((self._href.strip(), text))
        self._href = None
        self._text_parts = []
        self._open = False


def build_store_hosts(*domains):
    """Normalize one or more store domains into a set of bare hostnames
    (with and without a www. prefix) used to recognize internal links."""
    hosts = set()
    for d in domains:
        if not d:
            continue
        d = re.sub(r"^https?://", "", d.strip().lower()).rstrip("/").split("/")[0]
        d = d.split("@")[-1].split(":")[0]
        if not d:
            continue
        bare = d[4:] if d.startswith("www.") else d
        hosts.add(bare)
        hosts.add("www." + bare)
    return hosts


def _strip_locale(path):
    stripped = _LOCALE_PREFIX_RE.sub("", path, count=1)
    return stripped or "/"


def normalize_href_path(href):
    """Canonical path form for comparing hrefs across extraction and editing:
    entity-decode, drop scheme/host/query/fragment, strip the locale prefix,
    trim the trailing slash. Extraction decodes entities while raw HTML does
    not, so both sides of any comparison must go through this."""
    if not href:
        return ""
    parsed = urlparse(html_lib.unescape(href.strip()))
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    path = _strip_locale(path)
    return path.rstrip("/") or "/"


def classify_href(href, store_hosts):
    """Classify an href as an internal Shopify link, or return None if it is
    external, non-navigational, or unparseable.

    Returns a dict with: link_type, handle, non_canonical, and (articles only)
    blog_handle. link_type is one of: product, collection, article, page,
    blog_index, home, other_internal.
    """
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "sms:")):
        return None

    parsed = urlparse(href)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return None

    host = (parsed.netloc or "").lower()
    if host:
        host = host.split("@")[-1].split(":")[0]
        if host not in store_hosts:
            return None  # external link

    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    path = _strip_locale(path)

    m = _COLLECTION_PRODUCT_RE.match(path)
    if m:
        return {"link_type": "product", "handle": m.group(1), "non_canonical": True}

    m = _PRODUCT_RE.match(path)
    if m:
        return {"link_type": "product", "handle": m.group(1), "non_canonical": False}

    m = _ARTICLE_RE.match(path)
    if m:
        return {
            "link_type": "article",
            "handle": m.group(2),
            "blog_handle": m.group(1),
            "non_canonical": False,
        }

    m = _BLOG_INDEX_RE.match(path)
    if m:
        return {"link_type": "blog_index", "handle": m.group(1), "non_canonical": False}

    m = _COLLECTION_RE.match(path)
    if m:
        return {"link_type": "collection", "handle": m.group(1), "non_canonical": False}

    m = _PAGE_RE.match(path)
    if m:
        return {"link_type": "page", "handle": m.group(1), "non_canonical": False}

    if path == "/":
        return {"link_type": "home", "handle": "", "non_canonical": False}

    return {"link_type": "other_internal", "handle": path, "non_canonical": False}


def extract_links_from_html(html, store_hosts=None):
    """Extract internal Shopify links from an HTML body.

    store_hosts is a set produced by build_store_hosts(); absolute links to any
    other host are treated as external and dropped.
    """
    if not html:
        return []
    store_hosts = store_hosts or set()

    parser = _AnchorParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        parser.links = [
            (m.group(1), re.sub(r"<[^>]+>", "", m.group(2)))
            for m in LINK_RE.finditer(html)
        ]

    links = []
    for href, anchor_text in parser.links:
        info = classify_href(href, store_hosts)
        if not info:
            continue
        link = {
            "href": href,
            "anchor_text": anchor_text.strip(),
            "link_type": info["link_type"],
            "handle": info["handle"],
            "non_canonical": info.get("non_canonical", False),
        }
        if info.get("blog_handle"):
            link["blog_handle"] = info["blog_handle"]
        links.append(link)
    return links


def compute_interlinking_score(incoming_count, in_collection):
    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    t = rules.get("score_thresholds", {})
    if incoming_count >= t.get("incoming_good", 3):
        return 100
    if incoming_count >= t.get("incoming_ok", 1):
        return 60
    if in_collection:
        return t.get("in_collection_score", 30)
    return 0


def find_missing_crosslinks(collections, products_by_collection, link_graph, total_products=None):
    """Suggest collection pairs to cross-link, ranked by relatedness (Jaccard
    overlap of their product sets), not raw shared counts — raw counts are
    dominated by whichever collections happen to be huge."""
    suggestions = []

    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    ct = rules.get("crosslink_thresholds", {})
    min_shared = ct.get("shared_products", 3)
    min_relatedness = ct.get("min_relatedness", 0.25)
    max_share = ct.get("max_collection_share", 0.5)

    # Catch-all collections (holding most of the catalog) overlap with
    # everything, so any pair involving one says nothing about relatedness.
    if total_products:
        size_limit = max_share * total_products
        eligible = {
            sid for sid, prods in products_by_collection.items() if len(prods) <= size_limit
        }
    else:
        eligible = set(products_by_collection)

    coll_by_sid = {}
    product_to_collections = {}
    for c in collections:
        sid = c.get("shopify_id")
        coll_by_sid[sid] = c
        if sid not in eligible:
            continue
        for pid in products_by_collection.get(sid, set()):
            product_to_collections.setdefault(pid, set()).add(sid)

    candidate_pairs = set()
    for pid, coll_sids in product_to_collections.items():
        coll_list = sorted(coll_sids)
        for i in range(len(coll_list)):
            for j in range(i + 1, len(coll_list)):
                candidate_pairs.add((coll_list[i], coll_list[j]))

    for sid_a, sid_b in candidate_pairs:
        coll_a = coll_by_sid.get(sid_a)
        coll_b = coll_by_sid.get(sid_b)
        if not coll_a or not coll_b:
            continue

        handle_a = coll_a["handle"]
        handle_b = coll_b["handle"]

        a_links_to_b = f"collection:{handle_b}" in link_graph.get(f"collection:{handle_a}", set())
        b_links_to_a = f"collection:{handle_a}" in link_graph.get(f"collection:{handle_b}", set())

        if a_links_to_b and b_links_to_a:
            continue

        prods_a = products_by_collection.get(sid_a, set())
        prods_b = products_by_collection.get(sid_b, set())
        shared = len(prods_a & prods_b)
        if shared < min_shared:
            continue
        union = len(prods_a | prods_b)
        relatedness = shared / union if union else 0
        if relatedness < min_relatedness:
            continue

        suggestions.append({
            "collection_a_title": coll_a["title"],
            "collection_a_handle": handle_a,
            "collection_b_title": coll_b["title"],
            "collection_b_handle": handle_b,
            "shared_products": shared,
            "relatedness": round(relatedness, 3),
            "a_links_to_b": a_links_to_b,
            "b_links_to_a": b_links_to_a,
        })

    suggestions.sort(key=lambda s: (-s["relatedness"], -s["shared_products"]))
    return suggestions


def _entry_target_key(entry):
    t = entry["to_type"]
    if t == "article":
        return f"article:{entry.get('to_blog_handle', '')}/{entry['to_handle']}"
    if t == "blog_index":
        return f"blog:{entry['to_handle']}"
    if t == "home":
        return "home:"
    if t == "other_internal":
        return f"other:{entry['to_handle']}"
    return f"{t}:{entry['to_handle']}"


def _target_exists_live(entry, client):
    """Confirm a suspected-broken target against Shopify (includes drafts).
    Conservative: returns True (i.e. not broken) if the check cannot run."""
    t = entry["to_type"]
    h = entry["to_handle"]
    try:
        if t == "product":
            return client.product_exists(h)
        if t == "collection":
            return client.collection_exists(h)
        if t == "page":
            return client.page_exists(h)
        if t == "article":
            return client.article_exists(entry.get("to_blog_handle", ""), h)
        if t == "blog_index":
            return client.blog_exists(h)
    except Exception:
        logger.warning("Live link verification failed for %s:%s", t, h)
        return True
    return True


def _verify_broken_candidates(candidates, shopify_client):
    """Drop candidates whose target actually exists on Shopify. Without a client
    every candidate is reported (legacy behaviour)."""
    if not candidates:
        return []
    if not shopify_client:
        return candidates

    confirmed = []
    cache = {}
    for entry in candidates:
        key = _entry_target_key(entry)
        if key not in cache:
            cache[key] = _target_exists_live(entry, shopify_client)
        if not cache[key]:
            confirmed.append(entry)
    return confirmed


# Merchandising boilerplate that says nothing about a collection's topic.
_GENERIC_TITLE_WORDS = {
    "collection", "collections", "shop", "store", "products", "best",
    "premium", "online", "sale", "new", "our", "your", "with", "from",
}


def _title_words(title):
    return {
        w for w in re.findall(r"[a-z0-9']+", (title or "").lower())
        if len(w) >= 4 and w not in _GENERIC_TITLE_WORDS
    }


def build_collection_fix_candidates(
    store_id, coll, analysis, max_products=10, max_collections=8, max_articles=5
):
    """Rank candidate source pages for inbound links into one collection.

    Pure heuristics, no AI: products inside the collection, related collections
    by vendor/product-type overlap (catch-alls excluded), and articles whose
    titles mention the collection's topic words. Pages that already link to the
    collection are dropped.
    """
    handle = coll.get("handle")
    sid = coll.get("shopify_id")
    title = coll.get("title") or handle

    linked_from = set()
    for e in analysis.get("all_links") or []:
        if e.get("to_type") == "collection" and e.get("to_handle") == handle:
            linked_from.add((e.get("from_type"), e.get("from_handle")))

    words = _title_words(title)

    with db.get_cursor() as cur:
        cur.execute(
            """SELECT p.title, p.handle FROM collection_product_map m
               JOIN synced_products p
                 ON p.shopify_id = m.product_shopify_id AND p.store_id = m.store_id
               WHERE m.store_id = %s AND m.collection_shopify_id = %s
                 AND p.status = 'ACTIVE'
               ORDER BY p.title LIMIT 60""",
            (store_id, sid),
        )
        member_products = cur.fetchall()

    scored_products = []
    for p in member_products:
        if ("product", p["handle"]) in linked_from:
            continue
        overlap = len(words & _title_words(p["title"]))
        scored_products.append((overlap, p))
    scored_products.sort(key=lambda x: (-x[0], x[1]["title"]))
    products = [
        {"type": "product", "handle": p["handle"], "title": p["title"]}
        for _, p in scored_products[:max_products]
    ]

    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    max_share = rules.get("crosslink_thresholds", {}).get("max_collection_share", 0.5)
    total_products = (analysis.get("summary") or {}).get("total_products") or 0
    my_vendors = set(coll.get("top_vendors") or [])
    my_types = set(coll.get("top_product_types") or [])

    scored_colls = []
    for c in analysis.get("collection_details") or []:
        if c.get("handle") == handle:
            continue
        if total_products and (c.get("product_count") or 0) > max_share * total_products:
            continue
        if ("collection", c.get("handle")) in linked_from:
            continue
        score = (
            len(my_vendors & set(c.get("top_vendors") or [])) * 2
            + len(my_types & set(c.get("top_product_types") or []))
            + len(words & _title_words(c.get("title")))
        )
        if score > 0:
            scored_colls.append((score, c))
    scored_colls.sort(key=lambda x: -x[0])
    collections = [
        {"type": "collection", "handle": c["handle"], "title": c["title"]}
        for _, c in scored_colls[:max_collections]
    ]

    articles = []
    if words:
        pattern = "|".join(re.escape(w) for w in sorted(words)[:4])
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT sa.title, sa.handle, sa.body, sb.handle AS blog_handle
                   FROM synced_articles sa
                   JOIN synced_blogs sb ON sb.id = sa.blog_id
                   WHERE sa.store_id = %s AND sa.is_published = TRUE AND sa.title ~* %s
                   ORDER BY sa.id DESC LIMIT 20""",
                (store_id, pattern),
            )
            rows = cur.fetchall()
        for a in rows:
            if ("article", a["handle"]) in linked_from:
                continue
            articles.append({
                "type": "article",
                "handle": a["handle"],
                "blog_handle": a["blog_handle"],
                "title": a["title"],
                "snippet": re.sub(r"<[^>]+>", "", a["body"] or "")[:200],
            })
            if len(articles) >= max_articles:
                break

    return {"products": products, "collections": collections, "articles": articles}


def build_article_fix_candidates(
    store_id, article, analysis, max_products=4, max_collections=5, max_articles=8
):
    """Rank candidate source pages for inbound links into one blog article:
    other articles on the topic (blogs naturally reference each other),
    topically matching collections ("read our guide"), and strongly matching
    products. Pages already linking to the article are dropped."""
    handle = article.get("handle")
    blog_handle = article.get("blog_handle")
    title = article.get("title") or handle

    linked_from = set()
    for e in analysis.get("all_links") or []:
        if (
            e.get("to_type") == "article"
            and e.get("to_handle") == handle
            and (not blog_handle or not e.get("to_blog_handle") or e.get("to_blog_handle") == blog_handle)
        ):
            linked_from.add((e.get("from_type"), e.get("from_handle")))

    words = _title_words(title)
    if not words:
        return {"products": [], "collections": [], "articles": []}
    pattern = "|".join(re.escape(w) for w in sorted(words)[:5])

    articles_out = []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT sa.title, sa.handle, sa.body, sb.handle AS blog_handle
               FROM synced_articles sa
               JOIN synced_blogs sb ON sb.id = sa.blog_id
               WHERE sa.store_id = %s AND sa.is_published = TRUE
                 AND sa.handle <> %s AND sa.title ~* %s
               ORDER BY sa.id DESC LIMIT 25""",
            (store_id, handle, pattern),
        )
        rows = cur.fetchall()
    scored = sorted(rows, key=lambda a: -len(words & _title_words(a["title"])))
    for a in scored:
        if ("article", a["handle"]) in linked_from:
            continue
        articles_out.append({
            "type": "article",
            "handle": a["handle"],
            "blog_handle": a["blog_handle"],
            "title": a["title"],
            "snippet": re.sub(r"<[^>]+>", "", a["body"] or "")[:200],
        })
        if len(articles_out) >= max_articles:
            break

    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    max_share = rules.get("crosslink_thresholds", {}).get("max_collection_share", 0.5)
    total_products = (analysis.get("summary") or {}).get("total_products") or 0

    scored_colls = []
    for c in analysis.get("collection_details") or []:
        if total_products and (c.get("product_count") or 0) > max_share * total_products:
            continue
        if ("collection", c.get("handle")) in linked_from:
            continue
        score = len(words & _title_words(c.get("title")))
        if score > 0:
            scored_colls.append((score, c))
    scored_colls.sort(key=lambda x: -x[0])
    collections_out = [
        {"type": "collection", "handle": c["handle"], "title": c["title"]}
        for _, c in scored_colls[:max_collections]
    ]

    products_out = []
    with db.get_cursor() as cur:
        cur.execute(
            """SELECT title, handle FROM synced_products
               WHERE store_id = %s AND status = 'ACTIVE' AND title ~* %s
               ORDER BY title LIMIT 20""",
            (store_id, pattern),
        )
        rows = cur.fetchall()
    scored_prods = sorted(rows, key=lambda p: -len(words & _title_words(p["title"])))
    for p in scored_prods:
        if ("product", p["handle"]) in linked_from:
            continue
        # products should only link out to genuinely on-topic guides
        if len(words & _title_words(p["title"])) < 2:
            continue
        products_out.append({"type": "product", "handle": p["handle"], "title": p["title"]})
        if len(products_out) >= max_products:
            break

    return {"products": products_out, "collections": collections_out, "articles": articles_out}


def analyze_store_interlinking(store_id, store_hosts=None, shopify_client=None):
    logger.info("Starting interlinking analysis for store %s", store_id)
    store_hosts = store_hosts or set()

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_id, title, handle, description, tags, vendor, product_type FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
            (store_id,),
        )
        products = [dict(r) for r in cur.fetchall()]

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, shopify_id, title, handle, description FROM synced_collections WHERE store_id = %s",
            (store_id,),
        )
        collections = [dict(r) for r in cur.fetchall()]

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT collection_shopify_id, product_shopify_id FROM collection_product_map WHERE store_id = %s",
            (store_id,),
        )
        mappings = cur.fetchall()

    # Only active products count toward collection membership — draft and
    # archived mappings inflate product counts and crosslink overlaps.
    active_sids = {p["shopify_id"] for p in products}
    products_by_collection = {}
    product_collections = {}
    for m in mappings:
        cid = m["collection_shopify_id"]
        pid = m["product_shopify_id"]
        if pid not in active_sids:
            continue
        products_by_collection.setdefault(cid, set()).add(pid)
        product_collections.setdefault(pid, set()).add(cid)

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT sa.id, sa.shopify_id, sa.title, sa.handle, sa.body, sa.tags,
                   sb.handle AS blog_handle
            FROM synced_articles sa
            JOIN synced_blogs sb ON sb.id = sa.blog_id
            WHERE sa.store_id = %s AND sa.is_published = TRUE
            """,
            (store_id,),
        )
        articles = [dict(r) for r in cur.fetchall()]

    with db.get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT handle FROM synced_blogs WHERE store_id = %s",
            (store_id,),
        )
        blog_handles = {r["handle"] for r in cur.fetchall()}

    # Build the set of valid link targets. Tracked entities come from synced
    # data; pages and blog indexes are valid-but-untracked. Reserved storefront
    # collections (e.g. /collections/all) always resolve.
    valid_keys = set()
    for p in products:
        valid_keys.add(f"product:{p['handle']}")
    for c in collections:
        valid_keys.add(f"collection:{c['handle']}")
    for a in articles:
        valid_keys.add(f"article:{a['blog_handle']}/{a['handle']}")
    for h in blog_handles:
        valid_keys.add(f"blog:{h}")
    for h in _RESERVED_COLLECTION_HANDLES:
        valid_keys.add(f"collection:{h}")

    if shopify_client:
        try:
            for page in shopify_client.get_all_pages():
                if page.get("handle"):
                    valid_keys.add(f"page:{page['handle']}")
        except Exception:
            logger.warning("Could not fetch pages for store %s; page links unverified", store_id)

    link_graph = {}
    all_links = []
    non_canonical_links = []
    broken_candidates = []

    incoming_sources = {}
    outgoing_counts = {}

    def _process_links(entity_key, entity_type, entity_handle, entity_title, links, entity_blog_handle=None):
        outgoing_targets = set()
        for link in links:
            link_entry = {
                "from_type": entity_type,
                "from_handle": entity_handle,
                "from_title": entity_title,
                "to_type": link["link_type"],
                "to_handle": link["handle"],
                "to_href": link.get("href", ""),
                "anchor_text": link["anchor_text"],
                "non_canonical": link.get("non_canonical", False),
            }
            if entity_type == "article" and entity_blog_handle:
                link_entry["from_blog_handle"] = entity_blog_handle
            if link["link_type"] == "article":
                link_entry["to_blog_handle"] = link.get("blog_handle", "")

            target_key = _entry_target_key(link_entry)
            if target_key == entity_key:
                continue  # ignore self-links

            outgoing_targets.add(target_key)
            all_links.append(link_entry)

            if link.get("non_canonical"):
                non_canonical_links.append(link_entry)

            if link["link_type"] in TRACKED_TARGET_TYPES:
                incoming_sources.setdefault(target_key, set()).add(entity_key)

            if link["link_type"] in CHECKABLE_TYPES and target_key not in valid_keys:
                broken_candidates.append(link_entry)

        link_graph[entity_key] = outgoing_targets
        outgoing_counts[entity_key] = len(outgoing_targets)

    for p in products:
        key = f"product:{p['handle']}"
        links = extract_links_from_html(p.get("description"), store_hosts)
        _process_links(key, "product", p["handle"], p["title"], links)

    for c in collections:
        key = f"collection:{c['handle']}"
        links = extract_links_from_html(c.get("description"), store_hosts)
        _process_links(key, "collection", c["handle"], c["title"], links)

    for a in articles:
        key = f"article:{a['blog_handle']}/{a['handle']}"
        links = extract_links_from_html(a.get("body"), store_hosts)
        _process_links(key, "article", a["handle"], a["title"], links, entity_blog_handle=a["blog_handle"])

    broken_links = _verify_broken_candidates(broken_candidates, shopify_client)
    if not shopify_client:
        for entry in broken_links:
            entry["unverified"] = True

    def _incoming(key):
        return len(incoming_sources.get(key, ()))

    orphan_products = []
    orphan_collections = []
    orphan_articles = []
    under_linked = []
    product_details = []
    collection_details = []
    article_details = []

    for p in products:
        key = f"product:{p['handle']}"
        inc = _incoming(key)
        out = outgoing_counts.get(key, 0)
        in_collection = p["shopify_id"] in product_collections
        score = compute_interlinking_score(inc, in_collection)

        detail = {
            "id": p["id"],
            "shopify_id": p["shopify_id"],
            "title": p["title"],
            "handle": p["handle"],
            "item_type": "product",
            "vendor": p.get("vendor") or "",
            "product_type": p.get("product_type") or "",
            "tags": p.get("tags") or "",
            "incoming_count": inc,
            "outgoing_count": out,
            "in_collection": in_collection,
            "collection_count": len(product_collections.get(p["shopify_id"], set())),
            "score": score,
            "outgoing_links": list(link_graph.get(key, set())),
        }
        product_details.append(detail)

        if inc == 0 and not in_collection:
            orphan_products.append(detail)
        elif inc == 0 and in_collection:
            under_linked.append({**detail, "reason": "in_collection_but_no_incoming_links"})
        elif inc < 2 and inc > 0:
            under_linked.append(detail)

    product_vendor_by_sid = {p["shopify_id"]: p.get("vendor") or "" for p in products}
    product_type_by_sid = {p["shopify_id"]: p.get("product_type") or "" for p in products}

    for c in collections:
        key = f"collection:{c['handle']}"
        inc = _incoming(key)
        out = outgoing_counts.get(key, 0)
        score = compute_interlinking_score(inc, True)

        coll_product_sids = products_by_collection.get(c.get("shopify_id"), set())
        vendor_counts = {}
        type_counts = {}
        for sid in coll_product_sids:
            v = product_vendor_by_sid.get(sid, "")
            t = product_type_by_sid.get(sid, "")
            if v:
                vendor_counts[v] = vendor_counts.get(v, 0) + 1
            if t:
                type_counts[t] = type_counts.get(t, 0) + 1

        top_vendors = sorted(vendor_counts, key=vendor_counts.get, reverse=True)[:5]
        top_product_types = sorted(type_counts, key=type_counts.get, reverse=True)[:5]

        detail = {
            "id": c["id"],
            "shopify_id": c["shopify_id"],
            "title": c["title"],
            "handle": c["handle"],
            "item_type": "collection",
            "incoming_count": inc,
            "outgoing_count": out,
            "score": score,
            "product_count": len(coll_product_sids),
            "top_vendors": top_vendors,
            "top_product_types": top_product_types,
            "outgoing_links": list(link_graph.get(key, set())),
        }
        collection_details.append(detail)

        if inc == 0:
            orphan_collections.append(detail)
        elif inc < 2:
            under_linked.append(detail)

    for a in articles:
        key = f"article:{a['blog_handle']}/{a['handle']}"
        inc = _incoming(key)
        out = outgoing_counts.get(key, 0)
        score = compute_interlinking_score(inc, False)

        detail = {
            "id": a["id"],
            "shopify_id": a["shopify_id"],
            "title": a["title"],
            "handle": a["handle"],
            "blog_handle": a["blog_handle"],
            "incoming_count": inc,
            "outgoing_count": out,
            "score": score,
            "item_type": "article",
            "outgoing_links": list(link_graph.get(key, set())),
        }
        article_details.append(detail)

        if inc == 0:
            orphan_articles.append(detail)
        elif inc < 2:
            under_linked.append({**detail, "item_type": "article"})

    missing_crosslinks = find_missing_crosslinks(
        collections, products_by_collection, link_graph, total_products=len(products)
    )

    not_in_collection = [
        d for d in product_details if not d["in_collection"]
    ]

    total_links = len(all_links)
    all_items = product_details + collection_details + article_details
    avg_incoming = (
        round(sum(d["incoming_count"] for d in all_items) / len(all_items), 1)
        if all_items else 0
    )
    all_scores = [d["score"] for d in all_items]
    avg_score = round(sum(all_scores) / len(all_scores)) if all_scores else 0

    rules = get_setting("rules.interlinking", DEFAULTS["rules.interlinking"])
    dist = rules.get("distribution", {})
    excellent_threshold = dist.get("excellent", 80)
    good_threshold = dist.get("good", 50)
    score_distribution = {
        "excellent": sum(1 for s in all_scores if s >= excellent_threshold),
        "good": sum(1 for s in all_scores if good_threshold <= s < excellent_threshold),
        "poor": sum(1 for s in all_scores if 1 <= s < good_threshold),
        "orphan": sum(1 for s in all_scores if s == 0),
    }

    vendor_type_index = {}
    for p in products:
        v = p.get("vendor") or ""
        t = p.get("product_type") or ""
        if v:
            vendor_type_index.setdefault(v, {})
            if t:
                vendor_type_index[v][t] = vendor_type_index[v].get(t, 0) + 1
            else:
                vendor_type_index[v]["(untyped)"] = vendor_type_index[v].get("(untyped)", 0) + 1

    analysis = {
        "stale": False,
        "verification": {"live_checked": bool(shopify_client)},
        "vendor_type_index": vendor_type_index,
        "summary": {
            "total_links": total_links,
            "total_products": len(products),
            "total_collections": len(collections),
            "total_articles": len(articles),
            "orphan_products_count": len(orphan_products),
            "orphan_collections_count": len(orphan_collections),
            "orphan_articles_count": len(orphan_articles),
            "under_linked_count": len(under_linked),
            "not_in_collection_count": len(not_in_collection),
            "avg_incoming_links": avg_incoming,
            "avg_score": avg_score,
            "score_distribution": score_distribution,
            "missing_crosslinks_count": len(missing_crosslinks),
            "broken_links_count": len(broken_links),
            "non_canonical_count": len(non_canonical_links),
        },
        "orphan_products": orphan_products,
        "orphan_collections": orphan_collections,
        "orphan_articles": orphan_articles,
        "under_linked": sorted(under_linked, key=lambda x: x["incoming_count"]),
        "not_in_collection": not_in_collection,
        "missing_crosslinks": missing_crosslinks,
        "broken_links": broken_links,
        "non_canonical_links": non_canonical_links,
        "all_links": all_links,
        "product_details": product_details,
        "collection_details": collection_details,
        "article_details": article_details,
    }

    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO interlinking_analysis (store_id, analysis_data, created_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (store_id) DO UPDATE SET
                analysis_data = EXCLUDED.analysis_data,
                created_at = NOW()
            """,
            (store_id, json.dumps(analysis)),
        )

    logger.info(
        "Interlinking analysis complete for store %s: %d links, %d broken, %d non-canonical, %d orphan products, %d orphan collections, %d orphan articles",
        store_id, total_links, len(broken_links), len(non_canonical_links),
        len(orphan_products), len(orphan_collections), len(orphan_articles),
    )

    return analysis
