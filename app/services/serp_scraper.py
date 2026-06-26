"""
SERP scraper service for live SEO benchmark data.

Searches Google (via Brave Search API) for a keyword, scrapes the top ranking
pages for their SEO metadata, and compiles a benchmark for AI prompt injection.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import Counter
from urllib.parse import urlparse

import httpx
import requests
from bs4 import BeautifulSoup

from app.auth import decrypt_value
from app.database import db

logger = logging.getLogger(__name__)

SCRAPE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
SCRAPE_TIMEOUT = 8
SCRAPE_CONCURRENCY = 3
SCRAPE_DELAY = 0.5
CACHE_TTL_HOURS = 24
BRAVE_RATE_LIMIT_DELAY = 1.1
BRAVE_MAX_RETRIES = 3

STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "is", "it", "this", "that", "from", "as", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do",
    "does", "did", "will", "would", "shall", "should", "may", "might",
    "can", "could", "not", "no", "nor", "so", "if", "than", "too",
    "very", "just", "about", "up", "out", "all", "your", "our", "their",
    "its", "my", "we", "you", "they", "he", "she", "how", "what",
    "which", "who", "when", "where", "why", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "only",
})


def _get_brave_api_key():
    """Get decrypted Brave API key from ai_config table."""
    with db.get_cursor() as cur:
        cur.execute("SELECT brave_api_key_enc FROM ai_config ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if not row or not row.get("brave_api_key_enc"):
        return None
    return decrypt_value(row["brave_api_key_enc"])


def _get_cached(cache_key):
    """Get cached data if not expired."""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT data FROM serp_cache WHERE cache_key = %s AND expires_at > NOW()",
            (cache_key,),
        )
        row = cur.fetchone()
    return row["data"] if row else None


def _set_cached(cache_key, data):
    """Cache data with TTL."""
    with db.get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO serp_cache (cache_key, data, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '%s hours')
            ON CONFLICT (cache_key) DO UPDATE SET
                data = EXCLUDED.data,
                expires_at = NOW() + INTERVAL '%s hours',
                created_at = NOW()
            """,
            (cache_key, json.dumps(data), CACHE_TTL_HOURS, CACHE_TTL_HOURS),
        )


def _strip_store_identity(keyword, product):
    """Remove store name, shop name, and domain fragments from a keyword."""
    if not keyword:
        return keyword

    store_domain = (product.get("store_domain") or "").strip().lower()
    store_name = (product.get("store_name") or "").strip()
    shop_name = (product.get("shop_name") or "").strip()

    # Also try to get names from DB if not on the product dict
    if not store_name and not shop_name:
        try:
            store_id = product.get("store_id")
            if store_id:
                with db.get_cursor() as cur:
                    cur.execute("SELECT name, shop_name, domain, primary_domain FROM stores WHERE id = %s", (store_id,))
                    row = cur.fetchone()
                if row:
                    store_name = (row.get("name") or "").strip()
                    shop_name = (row.get("shop_name") or "").strip()
                    if not store_domain:
                        store_domain = (row.get("primary_domain") or row.get("domain") or "").strip().lower()
        except Exception:
            pass

    remove_terms = set()
    for term in [store_name, shop_name]:
        if term and len(term) > 2:
            remove_terms.add(term)
    if store_domain:
        # Extract base name from domain: "a2ztobacco.com" -> "a2ztobacco", "tobacco-nation.myshopify.com" -> "tobacco-nation"
        base = store_domain.split(".")[0].replace("-", " ")
        if len(base) > 2:
            remove_terms.add(base)

    cleaned = keyword
    for term in remove_terms:
        cleaned = re.sub(re.escape(term), "", cleaned, flags=re.IGNORECASE)

    # Clean up leftover separators and whitespace
    cleaned = re.sub(r"\s*[-–—|:]\s*$", "", cleaned)
    cleaned = re.sub(r"^\s*[-–—|:]\s*", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    return cleaned if cleaned else keyword


def determine_primary_keyword(product, target_keywords=None):
    """Pick the best keyword to search for SERP benchmarking."""
    if target_keywords and target_keywords[0].strip():
        return target_keywords[0].strip()

    product_type = product.get("product_type")
    if product_type and product_type.strip():
        return product_type.strip()

    title = product.get("title") or ""
    vendor = product.get("vendor") or ""
    if vendor and title:
        title = re.sub(re.escape(vendor), "", title, flags=re.IGNORECASE).strip()
        title = re.sub(r"\s*[-|]\s*$", "", title).strip()

    words = title.split()[:5]
    return " ".join(words) if words else ""


def _build_specific_keyword(product):
    """Build a product-specific keyword from title (keeping vendor)."""
    title = (product.get("title") or "").strip()
    if not title:
        return ""
    words = title.split()[:8]
    return " ".join(words)


_COLLECTION_TITLE_PREFIXES = re.compile(
    r"^(?:Shop|Browse|All|Our|Best)\s+", re.IGNORECASE
)
_META_TITLE_SEPARATORS = re.compile(r"\s*[|]\s*|\s+[-–—]\s+")


def _determine_collection_keyword(collection):
    """Extract a search keyword from a collection, preferring meta_title."""
    raw = ""
    meta_title = (collection.get("meta_title") or "").strip()
    if meta_title:
        raw = meta_title
    else:
        title = (collection.get("title") or "").strip()
        if title:
            raw = title
        else:
            handle = (collection.get("handle") or "").strip()
            if handle:
                raw = handle.replace("-", " ")
    if not raw:
        return ""
    segments = _META_TITLE_SEPARATORS.split(raw)
    raw = segments[0].strip()
    raw = _COLLECTION_TITLE_PREFIXES.sub("", raw).strip()
    words = raw.split()[:6]
    return " ".join(words)


def determine_search_queries(product, target_keywords=None, entity_type="product"):
    """Return product-specific and competitor search queries.

    Returns a dict with:
        keyword: the core keyword used
        product_query: site:-scoped query to find this product (or None)
        competitor_query: broad query excluding the store domain
    """
    store_domain = (product.get("store_domain") or "").strip()

    if entity_type == "collection":
        keyword = _determine_collection_keyword(product)
    else:
        specific_keyword = _build_specific_keyword(product)
        if target_keywords and target_keywords[0].strip():
            keyword = target_keywords[0].strip()
        elif specific_keyword:
            keyword = specific_keyword
        else:
            keyword = determine_primary_keyword(product, target_keywords)

    if not keyword:
        return {"keyword": "", "product_query": None, "competitor_query": None}

    # Strip store name/domain from the keyword to avoid self-referential searches
    keyword = _strip_store_identity(keyword, product)

    if not keyword:
        return {"keyword": "", "product_query": None, "competitor_query": None}

    product_query = None
    if store_domain:
        competitor_query = f"{keyword} -site:{store_domain}"
    else:
        competitor_query = keyword

    return {
        "keyword": keyword,
        "product_query": product_query,
        "competitor_query": competitor_query,
    }


def _find_product_in_results(results, product):
    """Find the product's own listing in search results."""
    if not results:
        return None

    handle = (product.get("handle") or "").strip().lower()
    store_domain = (product.get("store_domain") or "").strip().lower()

    for r in results:
        url = (r.get("url") or "").lower()
        domain = (r.get("domain") or "").lower()

        if handle and handle in url:
            return r
        if store_domain and store_domain in domain:
            return r

    return None


_brave_last_call = 0.0


def brave_search(query, count=10):
    """Search Brave API with rate-limit awareness and retry on 429."""
    global _brave_last_call

    api_key = _get_brave_api_key()
    if not api_key:
        logger.warning("Brave search skipped — no API key configured")
        return None

    cache_key = hashlib.sha256(f"brave:{query}:{count}".encode()).hexdigest()
    cached = _get_cached(cache_key)
    if cached:
        logger.info("Brave search cache hit for query: %s", query)
        return cached

    for attempt in range(BRAVE_MAX_RETRIES):
        try:
            elapsed = time.time() - _brave_last_call
            if elapsed < BRAVE_RATE_LIMIT_DELAY:
                wait = BRAVE_RATE_LIMIT_DELAY - elapsed
                logger.info("Brave rate limit: waiting %.1fs before request", wait)
                time.sleep(wait)

            logger.info("Brave search API call for query: %s (attempt %d)", query, attempt + 1)
            _brave_last_call = time.time()

            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
                params={
                    "q": query,
                    "count": min(count, 20),
                    "country": "us",
                    "search_lang": "en",
                    "safesearch": "off",
                    "result_filter": "web",
                },
                timeout=30,
            )

            if resp.status_code == 429:
                reset = resp.headers.get("X-RateLimit-Reset", "")
                try:
                    backoff = int(reset.split(",")[0].strip())
                except (ValueError, IndexError):
                    backoff = 2 ** attempt
                backoff = max(backoff, 1)
                logger.warning(
                    "Brave 429 rate limited, retrying in %ds (attempt %d/%d)",
                    backoff, attempt + 1, BRAVE_MAX_RETRIES,
                )
                time.sleep(backoff)
                continue

            if resp.status_code != 200:
                logger.error("Brave search failed with status %s: %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            web_results = data.get("web", {}).get("results", [])
            logger.info("Brave search returned %d results for: %s", len(web_results), query)

            results = []
            for i, r in enumerate(web_results):
                results.append({
                    "position": i + 1,
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", ""),
                    "domain": r.get("meta_url", {}).get("hostname", ""),
                })

            _set_cached(cache_key, results)
            return results
        except requests.exceptions.Timeout:
            logger.warning("Brave search timeout for query: %s (attempt %d/%d)", query, attempt + 1, BRAVE_MAX_RETRIES)
            if attempt < BRAVE_MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            logger.exception("Brave search exception for query: %s", query)
            return None

    logger.error("Brave search exhausted %d retries for: %s", BRAVE_MAX_RETRIES, query)
    return None


GOOGLE_SEARCH_DELAY = 2.0
_google_last_call = 0.0

GOOGLE_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def google_search(query, count=20, gl="us", hl="en"):
    """Fetch Google search results by scraping the HTML results page.

    Returns a list of dicts: [{position, title, url, description, domain}, ...]
    Uses caching and rate limiting to avoid being blocked.
    """
    import random

    global _google_last_call

    cache_key = hashlib.sha256(f"google:{query}:{count}:{gl}".encode()).hexdigest()
    cached = _get_cached(cache_key)
    if cached:
        logger.info("Google search cache hit for: %s", query)
        return cached

    elapsed = time.time() - _google_last_call
    if elapsed < GOOGLE_SEARCH_DELAY:
        wait = GOOGLE_SEARCH_DELAY - elapsed + random.uniform(0.2, 1.0)
        time.sleep(wait)

    _google_last_call = time.time()

    params = {
        "q": query,
        "num": min(count, 30),
        "gl": gl,
        "hl": hl,
        "pws": "0",
    }

    headers = {
        "User-Agent": random.choice(GOOGLE_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Referer": "https://www.google.com/",
    }

    try:
        resp = requests.get(
            "https://www.google.com/search",
            params=params,
            headers=headers,
            timeout=15,
        )

        if resp.status_code == 429:
            logger.warning("Google search rate limited (429) for: %s", query)
            return None

        if resp.status_code != 200:
            logger.warning("Google search returned %s for: %s", resp.status_code, query)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        position = 0

        for g in soup.select("div.g, div[data-sokoban-container]"):
            link = g.select_one("a[href]")
            if not link:
                continue

            href = link.get("href", "")
            if not href.startswith("http") or "google.com" in href:
                continue

            position += 1

            title_el = g.select_one("h3")
            title = title_el.get_text(strip=True) if title_el else ""

            desc_el = g.select_one("div[data-sncf], div.VwiC3b, span.aCOpRe")
            description = desc_el.get_text(strip=True) if desc_el else ""

            try:
                domain = urlparse(href).hostname or ""
                if domain.startswith("www."):
                    domain = domain[4:]
            except Exception:
                domain = ""

            results.append({
                "position": position,
                "title": title,
                "url": href,
                "description": description,
                "domain": domain,
            })

            if position >= count:
                break

        logger.info("Google search parsed %d results for: %s", len(results), query)

        if results:
            _set_cached(cache_key, results)

        return results if results else None

    except requests.exceptions.Timeout:
        logger.warning("Google search timeout for: %s", query)
        return None
    except Exception:
        logger.exception("Google search failed for: %s", query)
        return None


def brave_image_search(query, count=5):
    global _brave_last_call

    api_key = _get_brave_api_key()
    if not api_key:
        logger.warning("Brave image search skipped — no API key configured")
        return None

    cache_key = hashlib.sha256(f"brave_img:{query}:{count}".encode()).hexdigest()
    cached = _get_cached(cache_key)
    if cached:
        logger.info("Brave image search cache hit for query: %s", query)
        return cached

    for attempt in range(BRAVE_MAX_RETRIES):
        try:
            elapsed = time.time() - _brave_last_call
            if elapsed < BRAVE_RATE_LIMIT_DELAY:
                time.sleep(BRAVE_RATE_LIMIT_DELAY - elapsed)

            logger.info("Brave image search API call for query: %s (attempt %d)", query, attempt + 1)
            _brave_last_call = time.time()

            resp = requests.get(
                "https://api.search.brave.com/res/v1/images/search",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
                params={
                    "q": query,
                    "count": min(count, 20),
                    "country": "us",
                    "search_lang": "en",
                    "safesearch": "off",
                },
                timeout=15,
            )

            if resp.status_code == 429:
                backoff = max(2 ** attempt, 1)
                logger.warning("Brave image 429 rate limited, retrying in %ds", backoff)
                time.sleep(backoff)
                continue

            if resp.status_code != 200:
                logger.error("Brave image search failed with status %s: %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            results = []
            for r in data.get("results", []):
                props = r.get("properties", {})
                results.append({
                    "title": r.get("title", ""),
                    "url": props.get("url") or r.get("url", ""),
                    "thumbnail": r.get("thumbnail", {}).get("src", ""),
                    "width": props.get("width") or r.get("width"),
                    "height": props.get("height") or r.get("height"),
                    "source": r.get("source", ""),
                })

            logger.info("Brave image search returned %d results for: %s", len(results), query)
            if results:
                _set_cached(cache_key, results)
            return results
        except Exception:
            logger.exception("Brave image search exception for query: %s", query)
            return None

    logger.error("Brave image search exhausted %d retries for: %s", BRAVE_MAX_RETRIES, query)
    return None


async def scrape_page_seo_metadata(client, url):
    """Fetch a single URL and extract SEO metadata."""
    try:
        response = await client.get(
            url,
            headers={"User-Agent": SCRAPE_USER_AGENT},
            timeout=SCRAPE_TIMEOUT,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else ""

        meta_desc_tag = soup.find("meta", attrs={"name": "description"})
        meta_desc = meta_desc_tag.get("content", "") if meta_desc_tag else ""

        h1_tag = soup.find("h1")
        h1_text = h1_tag.get_text(strip=True) if h1_tag else ""

        h2_tags = soup.find_all("h2", limit=5)
        h2_texts = [h2.get_text(strip=True) for h2 in h2_tags]

        body = soup.find("body")
        body_text = body.get_text(separator=" ", strip=True) if body else ""
        word_count = len(body_text.split())

        schema_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
        schema_types = []
        for script in schema_scripts:
            try:
                sd = json.loads(script.string or "")
                if isinstance(sd, dict) and "@type" in sd:
                    schema_types.append(sd["@type"])
                elif isinstance(sd, list):
                    for item in sd:
                        if isinstance(item, dict) and "@type" in item:
                            schema_types.append(item["@type"])
            except (json.JSONDecodeError, TypeError):
                pass

        og_title_tag = soup.find("meta", attrs={"property": "og:title"})
        og_title = og_title_tag.get("content", "") if og_title_tag else ""

        og_desc_tag = soup.find("meta", attrs={"property": "og:description"})
        og_desc = og_desc_tag.get("content", "") if og_desc_tag else ""

        canonical_tag = soup.find("link", attrs={"rel": "canonical"})
        canonical = canonical_tag.get("href", "") if canonical_tag else ""

        return {
            "url": url,
            "domain": urlparse(url).netloc,
            "title": title_text,
            "title_length": len(title_text),
            "meta_description": meta_desc,
            "meta_description_length": len(meta_desc),
            "h1": h1_text,
            "h2s": h2_texts,
            "word_count": word_count,
            "schema_types": schema_types,
            "has_schema": bool(schema_types),
            "og_title": og_title,
            "og_description": og_desc,
            "canonical": canonical,
        }
    except Exception:
        return None


async def scrape_serp_pages(urls, max_pages=10):
    """Scrape multiple URLs concurrently with rate limiting."""
    urls = urls[:max_pages]
    semaphore = asyncio.Semaphore(SCRAPE_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def _scrape(url, index):
            if index > 0:
                await asyncio.sleep(SCRAPE_DELAY * index)
            async with semaphore:
                return await scrape_page_seo_metadata(client, url)

        tasks = [_scrape(url, i) for i, url in enumerate(urls)]
        results = await asyncio.gather(*tasks)

    return [r for r in results if r is not None]


def compile_serp_benchmark(keyword, serp_results, scraped_pages, product_page=None):
    """Aggregate scraped data into a structured benchmark."""
    pages = scraped_pages if scraped_pages else []

    if not pages and not serp_results and not product_page:
        return None

    if pages:
        avg_title_len = sum(p["title_length"] for p in pages) / len(pages)
        avg_desc_len = sum(p["meta_description_length"] for p in pages) / len(pages)
        avg_word_count = sum(p["word_count"] for p in pages) / len(pages)
        schema_count = sum(1 for p in pages if p.get("has_schema"))
        schema_pct = round(schema_count / len(pages) * 100)

        all_title_words = []
        for p in pages:
            words = p["title"].lower().split()
            all_title_words.extend(
                w for w in words if len(w) > 3 and w not in STOP_WORDS
            )
        word_freq = Counter(all_title_words).most_common(10)
        common_title_words = [w for w, _ in word_freq]

        page_summaries = []
        for p in pages:
            page_summaries.append({
                "domain": p["domain"],
                "title": p["title"],
                "title_length": p["title_length"],
                "description_length": p["meta_description_length"],
                "word_count": p["word_count"],
                "schema_types": p.get("schema_types", []),
                "h2s": p.get("h2s", []),
            })
    else:
        titles = [r.get("title", "") for r in serp_results if r.get("title")]
        descs = [r.get("description", "") for r in serp_results if r.get("description")]
        avg_title_len = sum(len(t) for t in titles) / len(titles) if titles else 0
        avg_desc_len = sum(len(d) for d in descs) / len(descs) if descs else 0
        avg_word_count = 0
        schema_pct = 0
        common_title_words = []

        page_summaries = []
        for r in serp_results:
            page_summaries.append({
                "domain": r.get("domain", ""),
                "title": r.get("title", ""),
                "title_length": len(r.get("title", "")),
                "description_length": len(r.get("description", "")),
                "word_count": 0,
                "schema_types": [],
            })

    your_product = None
    if product_page:
        your_product = {
            "url": product_page.get("url", ""),
            "domain": product_page.get("domain", ""),
            "title": product_page.get("title", ""),
            "title_length": product_page.get("title_length", 0),
            "meta_description": product_page.get("meta_description", ""),
            "meta_description_length": product_page.get("meta_description_length", 0),
            "word_count": product_page.get("word_count", 0),
            "schema_types": product_page.get("schema_types", []),
            "has_schema": product_page.get("has_schema", False),
            "serp_position": product_page.get("serp_position"),
        }

    return {
        "keyword": keyword,
        "pages_analyzed": len(pages) or len(serp_results),
        "avg_title_length": round(avg_title_len),
        "avg_description_length": round(avg_desc_len),
        "avg_word_count": round(avg_word_count),
        "schema_usage_pct": schema_pct,
        "common_title_words": common_title_words,
        "page_summaries": page_summaries,
        "your_product": your_product,
    }


def format_serp_benchmark_for_prompt(benchmark):
    """Format benchmark as markdown for AI prompt injection."""
    if not benchmark:
        return ""

    keyword = benchmark["keyword"]
    your_product = benchmark.get("your_product")
    pages = benchmark.get("pages_analyzed", 0)
    avg_title = benchmark.get("avg_title_length", 0)
    avg_desc = benchmark.get("avg_description_length", 0)
    avg_words = benchmark.get("avg_word_count", 0)
    schema_pct = benchmark.get("schema_usage_pct", 0)
    common_words = benchmark.get("common_title_words", [])
    summaries = benchmark.get("page_summaries", [])

    if not your_product and pages == 0:
        return ""

    lines = []

    if your_product:
        pos = your_product.get("serp_position")
        pos_str = f"#{pos}" if pos else "Not found in top results"
        title = your_product.get("title", "N/A")
        title_len = your_product.get("title_length", 0)
        desc = your_product.get("meta_description", "N/A")
        desc_len = your_product.get("meta_description_length", 0)
        word_count = your_product.get("word_count", 0)
        schema_types = your_product.get("schema_types", [])

        lines.append(f'## YOUR PRODUCT\'S CURRENT SERP PRESENCE (for "{keyword}"):')
        lines.append("")
        lines.append(f'- Title: "{title}" ({title_len} chars)')
        desc_preview = desc[:80] + "..." if len(desc) > 80 else desc
        lines.append(f'- Meta Description: "{desc_preview}" ({desc_len} chars)')
        if word_count:
            lines.append(f"- Content word count: {word_count} words")
        if schema_types:
            lines.append(f"- Schema: {', '.join(schema_types)}")
        lines.append(f"- Position: {pos_str}")
        lines.append("")

    if pages > 0:
        lines.append(f'## COMPETITOR BENCHMARK (top pages ranking for "{keyword}"):')
        lines.append("")
        lines.append(f"### Averages across top {pages} competitor pages:")
        lines.append(f"- Avg meta title length: {avg_title} chars")
        lines.append(f"- Avg meta description length: {avg_desc} chars")

        if avg_words > 0:
            lines.append(f"- Avg content word count: {avg_words} words")

        lines.append(f"- Schema markup usage: {schema_pct}%")

        if common_words:
            lines.append(f"- Common words in titles: {', '.join(common_words[:7])}")

        if summaries:
            lines.append("")
            lines.append("### Top competitor pages (use as your benchmark):")
            for i, s in enumerate(summaries[:10], 1):
                title_preview = s["title"][:50] + "..." if len(s["title"]) > 50 else s["title"]
                entry = (
                    f'{i}. {s["domain"]} \u2014 "{title_preview}" '
                    f'({s["title_length"]} chars) | Desc: {s["description_length"]} chars'
                )
                if s.get("word_count"):
                    entry += f' | Words: {s["word_count"]}'
                lines.append(entry)

    lines.append("")
    lines.append(
        "IMPORTANT: Your recommendations MUST EXCEED these real competitor benchmarks — do not just match them. "
        "Meta titles should aim for 50-60 chars (use the full space Google displays). "
        "Meta descriptions should aim for 145-160 chars (maximize snippet real estate). "
        "If competitors are using shorter titles or descriptions, that is an opportunity to stand out with more complete, keyword-rich metadata. "
        "Compare your product's current SERP presence against the competitor data above."
    )

    return "\n".join(lines)


def _scrape_urls(urls):
    """Scrape a list of URLs, using cache where available."""
    cached_pages = []
    urls_to_scrape = []

    for url in urls:
        url_hash = hashlib.sha256(f"page:{url}".encode()).hexdigest()
        cached = _get_cached(url_hash)
        if cached:
            cached_pages.append(cached)
        else:
            urls_to_scrape.append(url)

    fresh_pages = []
    if urls_to_scrape:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    fresh_pages = pool.submit(
                        lambda: asyncio.run(scrape_serp_pages(urls_to_scrape))
                    ).result(timeout=60)
            else:
                fresh_pages = loop.run_until_complete(scrape_serp_pages(urls_to_scrape))
        except RuntimeError:
            fresh_pages = asyncio.run(scrape_serp_pages(urls_to_scrape))

        for page in fresh_pages:
            url_hash = hashlib.sha256(f"page:{page['url']}".encode()).hexdigest()
            _set_cached(url_hash, page)

    return cached_pages + fresh_pages


def get_serp_benchmark(product, target_keywords=None, return_raw=False, entity_type="product"):
    """
    Main orchestrator: two-phase search (product self-search + competitors).

    Returns formatted benchmark string or empty string on failure.
    When return_raw=True, returns (formatted_string, raw_benchmark_dict) tuple.
    """
    try:
        queries = determine_search_queries(product, target_keywords, entity_type=entity_type)
        keyword = queries["keyword"]
        if not keyword:
            logger.warning("SERP benchmark skipped — no keyword determined")
            return ("", None) if return_raw else ""

        logger.info("SERP benchmark starting for keyword: %s", keyword)
        product_page = None

        competitor_results = brave_search(queries["competitor_query"], count=10)

        # Filter out own store from results (Brave -site: is unreliable)
        store_domain = (product.get("store_domain") or "").strip().lower()
        if competitor_results and store_domain:
            own_domains = {store_domain}
            # Also exclude the .myshopify.com variant
            if not store_domain.endswith(".myshopify.com"):
                own_domains.add(store_domain.replace("www.", ""))
            with db.get_cursor() as cur:
                cur.execute("SELECT domain FROM stores WHERE primary_domain = %s OR domain = %s", (store_domain, store_domain))
                for row in cur.fetchall():
                    own_domains.add((row["domain"] or "").lower().strip())
            before_count = len(competitor_results)
            competitor_results = [
                r for r in competitor_results
                if not any(d in (r.get("url") or "").lower() for d in own_domains if d)
            ]
            filtered_count = before_count - len(competitor_results)
            if filtered_count > 0:
                logger.info("Filtered %d own-store results from SERP", filtered_count)

        if not competitor_results and not product_page:
            logger.warning("SERP benchmark — no competitor results and no product page found")
            return ("", None) if return_raw else ""

        competitor_pages = []
        if competitor_results:
            urls = [r["url"] for r in competitor_results if r.get("url")]
            competitor_pages = _scrape_urls(urls)
            logger.info("Scraped %d/%d competitor pages", len(competitor_pages), len(urls))

        benchmark = compile_serp_benchmark(
            keyword, competitor_results or [], competitor_pages, product_page=product_page
        )
        formatted = format_serp_benchmark_for_prompt(benchmark)
        logger.info(
            "SERP benchmark complete — keyword: %s, pages: %d, avg_words: %d",
            keyword,
            benchmark.get("pages_analyzed", 0) if benchmark else 0,
            benchmark.get("avg_word_count", 0) if benchmark else 0,
        )

        if return_raw:
            return (formatted, benchmark)
        return formatted

    except Exception:
        logger.exception("SERP benchmark failed")
        return ("", None) if return_raw else ""
