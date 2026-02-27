import json
import logging
import re

from openai import OpenAI

logger = logging.getLogger(__name__)

from app.auth import decrypt_value
from app.database import db
from app.services.seo_analyzer import analyze_collection_seo, analyze_product_seo

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_product_data",
            "description": "Get full product data including title, description, meta fields, images, and variants for a specific product by its database ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "The database ID of the product",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_seo_score",
            "description": "Run rule-based SEO analysis on a product and get a score breakdown with details for meta title, meta description, description length, and image alt text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "The database ID of the product to analyze",
                    }
                },
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_summary",
            "description": "Get a summary of the store including total products, average SEO score, and top issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    }
                },
                "required": ["store_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_collection_data",
            "description": "Get full collection data including title, description, and meta fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "The database ID of the collection",
                    }
                },
                "required": ["collection_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_collection_seo_score",
            "description": "Run rule-based SEO analysis on a collection and get a score breakdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_id": {
                        "type": "integer",
                        "description": "The database ID of the collection to analyze",
                    }
                },
                "required": ["collection_id"],
            },
        },
    },
]

KEYWORD_ANALYSIS_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_all_product_summaries",
            "description": (
                "Get paginated product summaries for a store including title, product_type, vendor, and current tags. "
                "Use offset and limit to paginate through all products. Returns products plus total count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "store_id": {
                        "type": "integer",
                        "description": "The database ID of the store",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of products to return (default 100, max 200)",
                        "default": 100,
                    },
                },
                "required": ["store_id"],
            },
        },
    },
]

DISCOVERY_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_store_products",
            "description": (
                "Search the store's product catalog by keyword. Matches against product title, tags, and product_type. "
                "Returns up to 20 matching products with title and handle. "
                "Call multiple times with different keywords to broaden discovery."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Search term to match against product title, tags, or product_type",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
]


def _handle_tool_call(name, args):
    if name == "get_product_data":
        product_id = args["product_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            product = cur.fetchone()
        if not product:
            return {"error": f"Product {product_id} not found"}
        return {
            "id": product["id"],
            "title": product["title"],
            "handle": product["handle"],
            "description": product["description"],
            "vendor": product["vendor"],
            "product_type": product["product_type"],
            "tags": product["tags"],
            "meta_title": product["meta_title"],
            "meta_description": product["meta_description"],
            "images": product["images"],
            "variants": product["variants"],
            "status": product["status"],
        }

    if name == "analyze_seo_score":
        product_id = args["product_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            product = cur.fetchone()
        if not product:
            return {"error": f"Product {product_id} not found"}
        return analyze_product_seo(dict(product))

    if name == "get_store_summary":
        store_id = args["store_id"]
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as total FROM synced_products WHERE store_id = %s",
                (store_id,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                "SELECT * FROM synced_products WHERE store_id = %s LIMIT 100",
                (store_id,),
            )
            products = cur.fetchall()

        scores = [analyze_product_seo(dict(p))["score"] for p in products]
        avg_score = round(sum(scores) / len(scores)) if scores else 0
        low_score_count = sum(1 for s in scores if s < 50)

        return {
            "store_id": store_id,
            "total_products": total,
            "analyzed_count": len(scores),
            "average_seo_score": avg_score,
            "products_below_50": low_score_count,
        }

    if name == "get_collection_data":
        collection_id = args["collection_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()
        if not collection:
            return {"error": f"Collection {collection_id} not found"}
        return {
            "id": collection["id"],
            "title": collection["title"],
            "handle": collection["handle"],
            "description": collection["description"],
            "meta_title": collection["meta_title"],
            "meta_description": collection["meta_description"],
            "products_count": collection["products_count"],
            "image_url": collection["image_url"],
        }

    if name == "analyze_collection_seo_score":
        collection_id = args["collection_id"]
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()
        if not collection:
            return {"error": f"Collection {collection_id} not found"}
        return analyze_collection_seo(dict(collection))

    return {"error": f"Unknown tool: {name}"}


def _handle_keyword_tool_call(name, args):
    if name == "get_all_product_summaries":
        sid = args["store_id"]
        offset = max(args.get("offset", 0), 0)
        limit = min(max(args.get("limit", 100), 1), 200)
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as total FROM synced_products WHERE store_id = %s AND status = 'ACTIVE'",
                (sid,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                """SELECT id, title, product_type, vendor, tags
                FROM synced_products
                WHERE store_id = %s AND status = 'ACTIVE'
                ORDER BY id
                OFFSET %s LIMIT %s""",
                (sid, offset, limit),
            )
            rows = cur.fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "products": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "product_type": r["product_type"],
                    "vendor": r["vendor"],
                    "tags": r["tags"],
                }
                for r in rows
            ],
        }
    return {"error": f"Unknown keyword tool: {name}"}


def _handle_discovery_tool_call(name, args, store_id):
    if name == "search_store_products":
        keyword = args.get("keyword", "")
        pattern = f"%{keyword}%"
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT title, handle FROM synced_products
                WHERE store_id = %s AND status = 'ACTIVE'
                  AND (title ILIKE %s OR tags ILIKE %s OR product_type ILIKE %s)
                LIMIT 20""",
                (store_id, pattern, pattern, pattern),
            )
            rows = cur.fetchall()
        return [{"title": r["title"], "handle": r["handle"]} for r in rows]
    return {"error": f"Unknown discovery tool: {name}"}


def _discover_related_products(client, config, collection_title, store_id):
    try:
        system_prompt = (
            "You are a product discovery assistant. Your job is to search for products "
            "that belong in a given collection. Use the search_store_products tool to find "
            "products by keywords derived from the collection title, synonyms, and broader "
            "category terms. Try 2-4 different searches to get good coverage.\n\n"
            "When done searching, return a JSON object with a single key:\n"
            "- selected_products: array of {title, handle} — pick the 5-15 most relevant products"
        )
        user_prompt = (
            f'The collection is titled "{collection_title}". '
            "Search for products that would belong in this collection."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        for _ in range(6):
            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                tools=DISCOVERY_TOOL_DEFINITIONS,
                temperature=0.3,
            )

            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" or (
                choice.message.tool_calls and len(choice.message.tool_calls) > 0
            ):
                messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = json.loads(tool_call.function.arguments)
                    result = _handle_discovery_tool_call(fn_name, fn_args, store_id)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result),
                        }
                    )
            else:
                content = choice.message.content or ""
                try:
                    start = content.find("{")
                    end = content.rfind("}") + 1
                    if start >= 0 and end > start:
                        parsed = json.loads(content[start:end])
                        selected = parsed.get("selected_products", [])
                        validated = [
                            p for p in selected
                            if isinstance(p, dict) and p.get("title") and p.get("handle")
                        ]
                        if validated:
                            logger.info("AI discovery found %d products for '%s'", len(validated), collection_title)
                            return validated
                except (json.JSONDecodeError, ValueError):
                    pass
                break

    except Exception:
        logger.exception("AI product discovery failed for '%s'", collection_title)

    return []


def _get_ai_config():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM ai_config ORDER BY id LIMIT 1")
        config = cur.fetchone()
    if not config or not config.get("api_key_enc"):
        raise ValueError("AI not configured. Please set your OpenRouter API key in Settings.")
    return {
        "api_key": decrypt_value(config["api_key_enc"]),
        "model": config.get("model") or "anthropic/claude-sonnet-4",
    }


DEFAULT_TARGET_WORD_COUNT = 500


def _sanitize_description(html: str) -> str:
    """Strip AI meta-artifacts (word counts, notes, disclaimers) from generated HTML."""
    html = re.sub(
        r"<p>\s*\(?\s*[Ww]ord\s*[Cc]ount\s*[:：]\s*\d+\s*(?:words?)?\s*\)?\s*</p>",
        "",
        html,
    )
    html = re.sub(
        r"<p>\s*\*{0,2}[Nn]ote\s*[:：]\s*\*{0,2}.*?</p>",
        "",
        html,
    )
    html = re.sub(
        r"(?:<hr\s*/?>|---)\s*(?:<p>.*?</p>\s*)*$",
        "",
        html,
    )
    html = re.sub(
        r"\(?\s*[Ww]ord\s*[Cc]ount\s*[:：]\s*\d+\s*(?:words?)?\s*\)?",
        "",
        html,
    )
    return html.strip()


def _generate_description(client, config, main_result, serp_context, serp_benchmark, product_id):
    """Dedicated AI call to generate an SEO-optimized product description.

    Uses all context from the main analysis call plus SERP data to produce
    a focused, high-quality HTML description.
    """
    try:
        target_word_count = DEFAULT_TARGET_WORD_COUNT
        if serp_benchmark and serp_benchmark.get("avg_word_count"):
            competitor_avg = serp_benchmark["avg_word_count"]
            # Always aim to exceed competitors by ~20%, never go below our default
            target_word_count = max(int(competitor_avg * 1.2), DEFAULT_TARGET_WORD_COUNT)

        logger.info(
            "Description generation starting — target: %d words, has_serp: %s",
            target_word_count,
            bool(serp_benchmark),
        )

        desc_system_prompt = (
            "You are an expert e-commerce SEO copywriter. Your ONLY task is to write a "
            "comprehensive, SEO-optimized product description in valid HTML.\n\n"
            "REQUIREMENTS:\n"
            f"- Target word count: approximately {target_word_count} words (exceed competitor average for better rankings)\n"
            "- Output format: valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
            "- Do NOT include <h1> tags (Shopify uses the product title as H1)\n"
            "- Do NOT wrap output in ```html code fences or JSON — return raw HTML only\n"
            "- Do NOT include word counts, notes, disclaimers, or any meta-commentary in the output\n"
            "- Naturally incorporate the provided keywords throughout the content\n"
            "- Structure: opening overview paragraph → features/benefits → specifications/details → closing paragraph\n"
            "- Use H2 headings to break up sections (study the competitor H2 patterns provided)\n"
            "- Write persuasive, benefit-oriented copy that also serves SEO\n"
            "- Include relevant long-tail keyword variations naturally\n"
        )

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_products WHERE id = %s", (product_id,))
            product = cur.fetchone()

        product_collections = []
        other_collections = []
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sc.title, sc.handle
                    FROM collection_product_map cpm
                    JOIN synced_collections sc
                      ON sc.shopify_id = cpm.collection_shopify_id
                     AND sc.store_id = cpm.store_id
                    WHERE cpm.product_shopify_id = %s
                      AND cpm.store_id = %s
                      AND sc.handle IS NOT NULL AND sc.handle != ''
                    ORDER BY sc.title""",
                    (product.get("shopify_id"), product.get("store_id")),
                )
                product_collections = cur.fetchall()

                existing_handles = {c["handle"] for c in product_collections}
                cur.execute(
                    """SELECT sc.title, sc.handle
                    FROM synced_collections sc
                    WHERE sc.store_id = %s
                      AND sc.handle IS NOT NULL AND sc.handle != ''
                    ORDER BY sc.title
                    LIMIT 30""",
                    (product.get("store_id"),),
                )
                all_collections = cur.fetchall()
                other_collections = [
                    c for c in all_collections if c["handle"] not in existing_handles
                ]
        except Exception:
            logger.exception("Failed to fetch collections for product interlinking")

        if product_collections:
            desc_system_prompt += (
                "\n- MUST include internal links to collections this product belongs to\n"
                "- Weave links naturally (e.g. 'part of our <a href=\"/collections/handle\">Collection Name</a> range')\n"
                "- Use relative URLs (/collections/{handle}) for portability\n"
                "- Anchor text should be descriptive — use collection names or descriptive phrases\n"
            )
        if other_collections:
            desc_system_prompt += (
                "\n- SHOULD include 2-4 links to other RELATED collections from the list provided\n"
                "- Only link to collections genuinely related to this product's category or use case\n"
            )

        user_parts = []

        if product:
            current_desc = product.get("description") or ""
            desc_word_count = len(current_desc.split()) if current_desc else 0
            user_parts.append(
                "PRODUCT DATA:\n"
                f"- Title: {product.get('title', 'N/A')}\n"
                f"- Vendor: {product.get('vendor', 'N/A')}\n"
                f"- Product Type: {product.get('product_type', 'N/A')}\n"
                f"- Current Tags: {product.get('tags', 'N/A')}\n"
                f"- Current Description Word Count: {desc_word_count}\n"
            )

        seo_parts = []
        if main_result.get("h1_tag"):
            seo_parts.append(f"- H1 Tag: {main_result['h1_tag']}")
        if main_result.get("optimized_meta_title"):
            seo_parts.append(f"- Meta Title: {main_result['optimized_meta_title']}")
        if main_result.get("optimized_meta_description"):
            seo_parts.append(f"- Meta Description: {main_result['optimized_meta_description']}")
        if main_result.get("keywords_used"):
            seo_parts.append(f"- Keywords to Incorporate: {', '.join(main_result['keywords_used'])}")
        if main_result.get("suggested_tags"):
            seo_parts.append(f"- Suggested Tags: {', '.join(main_result['suggested_tags'])}")
        if main_result.get("competitor_insights"):
            seo_parts.append(f"- Competitor Insights: {main_result['competitor_insights']}")
        if seo_parts:
            user_parts.append("SEO ANALYSIS (from main analysis):\n" + "\n".join(seo_parts))

        if serp_context:
            user_parts.append(f"SERP BENCHMARK:\n{serp_context}")

        if serp_benchmark and serp_benchmark.get("page_summaries"):
            h2_lines = []
            for s in serp_benchmark["page_summaries"][:5]:
                h2s = s.get("h2s", [])
                if h2s:
                    h2_lines.append(f"- {s['domain']}: {', '.join(h2s[:5])}")
            if h2_lines:
                user_parts.append(
                    "COMPETITOR H2 PATTERNS (use as structural inspiration):\n"
                    + "\n".join(h2_lines)
                )

        if product_collections:
            coll_text = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in product_collections)
            user_parts.append(
                "COLLECTIONS THIS PRODUCT BELONGS TO (link to these):\n" + coll_text
            )

        if other_collections:
            other_text = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in other_collections[:20])
            user_parts.append(
                "OTHER STORE COLLECTIONS (link to 2-4 related ones):\n" + other_text
            )

        user_parts.append(
            f"Write an SEO-optimized product description targeting ~{target_word_count} words (aim to be more comprehensive than competitors). "
            "Return raw HTML only."
        )

        desc_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": desc_system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.4,
        )

        desc_content = desc_response.choices[0].message.content or ""

        desc_content = re.sub(r"^```(?:html)?\s*\n?", "", desc_content)
        desc_content = re.sub(r"\n?```\s*$", "", desc_content)
        desc_content = _sanitize_description(desc_content)

        if desc_content:
            main_result["optimized_description"] = desc_content
            desc_words = len(desc_content.split())
            logger.info("Description generated — %d words (target: %d)", desc_words, target_word_count)
        else:
            logger.warning("Description generation returned empty content")

    except Exception:
        logger.exception("Description generation failed")

    return main_result


def _generate_collection_description(client, config, main_result, serp_context, serp_benchmark, collection_id, hierarchy_context=None):
    try:
        target_word_count = DEFAULT_TARGET_WORD_COUNT
        if serp_benchmark and serp_benchmark.get("avg_word_count"):
            competitor_avg = serp_benchmark["avg_word_count"]
            target_word_count = max(int(competitor_avg * 1.2), DEFAULT_TARGET_WORD_COUNT)

        logger.info(
            "Collection description generation starting — target: %d words",
            target_word_count,
        )

        collection_link_instruction = ""
        related_collections = []
        try:
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT gc.taxonomy_category
                    FROM generated_collections gc
                    WHERE gc.collection_id = %s AND gc.generation_mode = 'taxonomy' AND gc.taxonomy_category IS NOT NULL
                    LIMIT 1""",
                    (collection_id,),
                )
                tax_row = cur.fetchone()

                if tax_row:
                    cur.execute(
                        """SELECT DISTINCT sc.title, sc.handle
                        FROM generated_collections gc
                        JOIN synced_collections sc ON sc.id = gc.collection_id
                        WHERE gc.taxonomy_category = %s
                          AND gc.generation_mode = 'taxonomy'
                          AND gc.collection_id != %s
                          AND gc.store_id = (SELECT store_id FROM synced_collections WHERE id = %s)
                          AND sc.handle IS NOT NULL AND sc.handle != ''
                        ORDER BY sc.title""",
                        (tax_row["taxonomy_category"], collection_id, collection_id),
                    )
                    related_collections = cur.fetchall()

                if not related_collections:
                    cur.execute(
                        """SELECT sc.title, sc.handle
                        FROM synced_collections sc
                        WHERE sc.id != %s
                          AND sc.store_id = (SELECT store_id FROM synced_collections WHERE id = %s)
                          AND sc.handle IS NOT NULL AND sc.handle != ''
                        ORDER BY sc.title""",
                        (collection_id, collection_id),
                    )
                    related_collections = cur.fetchall()
        except Exception:
            logger.exception("Failed to fetch related collections for cross-linking")

        if related_collections:
            collection_link_instruction = (
                "\n- SHOULD include 2-5 internal links to RELATED COLLECTIONS from the list provided\n"
                "- Use relative URLs (/collections/{handle}) for collection links\n"
                "- Weave collection links naturally (e.g. 'Explore our [Collection Name](/collections/handle) for more...')\n"
                "- Only link to collections that are genuinely related to this collection's theme\n"
            )

        hierarchy_link_instruction = ""
        if hierarchy_context:
            hierarchy_link_instruction = (
                "\n- MUST include internal links based on this collection's hierarchy position:\n"
                "  " + hierarchy_context + "\n"
                "- Use relative URLs (/collections/{handle}) for all hierarchy links\n"
            )

        desc_system_prompt = (
            "You are an expert e-commerce SEO copywriter specializing in category/collection pages. "
            "Your ONLY task is to write a comprehensive, SEO-optimized collection description in valid HTML.\n\n"
            "TONE: Write SEO-rich copy that is specific and informative, NOT exaggerated. "
            "BANNED words: 'premium', 'top-quality', 'finest', 'ultimate', 'luxurious', 'exquisite', 'top-notch', 'unparalleled'. "
            "Replace vague superlatives with specific descriptors: product types, ingredients, use cases, brands, and variety. "
            "Sound like a knowledgeable store owner writing helpful content, not a marketing brochure.\n\n"
            "REQUIREMENTS:\n"
            f"- Target word count: approximately {target_word_count} words (exceed competitor average for better rankings)\n"
            "- Output format: valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
            "- Do NOT include <h1> tags (Shopify uses the collection title as H1)\n"
            "- Do NOT wrap output in ```html code fences or JSON — return raw HTML only\n"
            "- Do NOT include word counts, notes, disclaimers, or any meta-commentary in the output\n"
            "- Naturally incorporate the provided keywords throughout the content\n"
            "- Structure: category overview → product highlights (no product links — they're already on the page) → benefits → closing\n"
            "- Do NOT include links to individual products (/products/...) — Shopify already displays products on collection pages\n"
            + collection_link_instruction
            + hierarchy_link_instruction
        )

        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM synced_collections WHERE id = %s", (collection_id,))
            collection = cur.fetchone()

        user_parts = []

        if collection:
            current_desc = collection.get("description") or ""
            desc_word_count = len(current_desc.split()) if current_desc else 0
            user_parts.append(
                "COLLECTION DATA:\n"
                f"- Title: {collection.get('title', 'N/A')}\n"
                f"- Products Count: {collection.get('products_count', 0)}\n"
                f"- Current Description Word Count: {desc_word_count}\n"
            )

        if related_collections:
            coll_links_text = "\n".join(f"- {c['title']}: /collections/{c['handle']}" for c in related_collections[:30])
            user_parts.append(
                "RELATED COLLECTIONS (link to 2-5 relevant ones):\n" + coll_links_text
            )

        seo_parts = []
        if main_result.get("h1_tag"):
            seo_parts.append(f"- H1 Tag: {main_result['h1_tag']}")
        if main_result.get("optimized_meta_title"):
            seo_parts.append(f"- Meta Title: {main_result['optimized_meta_title']}")
        if main_result.get("optimized_meta_description"):
            seo_parts.append(f"- Meta Description: {main_result['optimized_meta_description']}")
        if main_result.get("keywords_used"):
            seo_parts.append(f"- Keywords to Incorporate: {', '.join(main_result['keywords_used'])}")
        if main_result.get("competitor_insights"):
            seo_parts.append(f"- Competitor Insights: {main_result['competitor_insights']}")
        if seo_parts:
            user_parts.append("SEO ANALYSIS (from main analysis):\n" + "\n".join(seo_parts))

        if serp_context:
            user_parts.append(f"SERP BENCHMARK:\n{serp_context}")

        if serp_benchmark and serp_benchmark.get("page_summaries"):
            h2_lines = []
            for s in serp_benchmark["page_summaries"][:5]:
                h2s = s.get("h2s", [])
                if h2s:
                    h2_lines.append(f"- {s['domain']}: {', '.join(h2s[:5])}")
            if h2_lines:
                user_parts.append(
                    "COMPETITOR H2 PATTERNS (use as structural inspiration):\n"
                    + "\n".join(h2_lines)
                )

        user_parts.append(
            f"Write an SEO-optimized collection description targeting ~{target_word_count} words (aim to be more comprehensive than competitors). "
            "Return raw HTML only."
        )

        desc_response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": desc_system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            temperature=0.4,
        )

        desc_content = desc_response.choices[0].message.content or ""

        desc_content = re.sub(r"^```(?:html)?\s*\n?", "", desc_content)
        desc_content = re.sub(r"\n?```\s*$", "", desc_content)
        desc_content = _sanitize_description(desc_content)

        if desc_content:
            main_result["optimized_description"] = desc_content
            desc_words = len(desc_content.split())
            logger.info("Collection description generated — %d words (target: %d)", desc_words, target_word_count)
        else:
            logger.warning("Collection description generation returned empty content")

    except Exception:
        logger.exception("Collection description generation failed")

    return main_result


def run_task(task_type, task_input, store_id=None):
    config = _get_ai_config()

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=config["api_key"],
    )

    if task_type == "product_seo":
        product_id = task_input.get("product_id")
        skip_tags = task_input.get("skip_tags", True)

        serp_context = ""
        serp_benchmark = None
        try:
            from app.services.serp_scraper import get_serp_benchmark
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sp.*, s.domain as store_domain
                    FROM synced_products sp
                    JOIN stores s ON s.id = sp.store_id
                    WHERE sp.id = %s""",
                    (product_id,),
                )
                product_row = cur.fetchone()
            if product_row:
                serp_context, serp_benchmark = get_serp_benchmark(
                    dict(product_row), return_raw=True
                )
        except Exception as e:
            logger.exception("SERP benchmark fetch failed: %s", e)

        serp_instruction = ""
        if serp_context:
            serp_instruction = (
                "\n\nYou also have live SERP benchmark data below. "
                "Use it to EXCEED what's actually ranking on Google — aim to beat competitor meta title and description lengths within optimal ranges.\n\n"
                + serp_context
            )

        tags_instruction = ""
        if skip_tags:
            tags_instruction = (
                "\n\nDo NOT suggest tags — they are managed separately via keyword analysis.\n"
            )
            tags_keys = ""
        else:
            tags_keys = (
                "- suggested_tags (array of strings): Relevant product tags for Shopify\n"
                "- keywords_used (array of strings): Keywords you incorporated into the optimizations\n"
            )

        system_prompt = (
            "You are an expert Shopify SEO consultant. Your job is to analyze product data "
            "and provide actionable SEO optimization recommendations. Use the tools available "
            "to fetch product data and analyze current SEO scores. Then provide specific, "
            "implementable suggestions.\n\n"
            "Do NOT generate a product description — a separate specialized process handles that.\n\n"
            "Format your final response as JSON with these keys:\n"
            "- optimized_meta_title (string): SEO-optimized meta title, 50-60 chars, aim to exceed competitor title lengths\n"
            "- optimized_meta_description (string): SEO-optimized meta description, 145-160 chars, aim to exceed competitor description lengths\n"
            "- h1_tag (string): Optimized product title / H1 tag for the Shopify product title field\n"
            + tags_keys +
            "- competitor_insights (string): Brief summary of competitor patterns and positioning\n"
            "- image_alt_suggestions (array of strings): "
            "Alt text suggestion for each product image, in the same order as the images array in the product data.\n"
            "- additional_recommendations (array of strings): Further SEO improvement suggestions\n"
            "- summary (string): Brief summary of all changes made and why"
            + serp_instruction
            + tags_instruction
        )
        user_prompt = (
            f"Analyze product ID {product_id} for SEO optimization. "
            "First get the product data, then analyze its SEO score, "
            "then provide your optimized recommendations."
        )
    elif task_type == "collection_seo":
        collection_id = task_input.get("collection_id")

        serp_context = ""
        serp_benchmark = None
        try:
            from app.services.serp_scraper import get_serp_benchmark
            with db.get_cursor() as cur:
                cur.execute(
                    """SELECT sc.*, s.domain as store_domain
                    FROM synced_collections sc
                    JOIN stores s ON s.id = sc.store_id
                    WHERE sc.id = %s""",
                    (collection_id,),
                )
                collection_row = cur.fetchone()
            if collection_row:
                serp_context, serp_benchmark = get_serp_benchmark(
                    dict(collection_row), return_raw=True, entity_type="collection"
                )
        except Exception as e:
            logger.exception("SERP benchmark fetch failed for collection: %s", e)

        serp_instruction = ""
        if serp_context:
            serp_instruction = (
                "\n\nYou also have live SERP benchmark data below. "
                "Use it to EXCEED what's actually ranking on Google — aim to beat competitor meta title and description lengths within optimal ranges.\n\n"
                + serp_context
            )

        hierarchy_instruction = ""
        hierarchy_context = task_input.get("hierarchy_context")
        if hierarchy_context:
            hierarchy_instruction = (
                "\n\nThis collection is part of a hierarchy:\n"
                + hierarchy_context
                + "\n\nInclude internal links to parent/sibling/child collections where relevant in your recommendations."
            )

        system_prompt = (
            "You are an expert Shopify SEO consultant. Your job is to analyze collection data "
            "and provide actionable SEO optimization recommendations. Use the tools available "
            "to fetch collection data and analyze current SEO scores. Then provide specific, "
            "implementable suggestions.\n\n"
            "Do NOT generate a collection description — a separate specialized process handles that.\n\n"
            "TONE: Write SEO-friendly titles and descriptions that are specific and keyword-rich, but NOT exaggerated. "
            "BANNED words: 'premium', 'top-quality', 'finest', 'ultimate', 'luxurious', 'exquisite', 'top-notch', 'unparalleled'. "
            "Instead of vague superlatives, use specific descriptors: product types, use cases, variety, and brand names. "
            "Good: 'Pipe Tobacco Collection — Aromatic, English & Virginia Blends' "
            "Bad: 'Premium Pipe Tobacco — The Finest Selection of Luxury Blends'\n\n"
            "Format your final response as JSON with these keys:\n"
            "- optimized_meta_title (string): SEO-optimized meta title, 50-60 chars, aim to exceed competitor title lengths\n"
            "- optimized_meta_description (string): SEO-optimized meta description, 145-160 chars, aim to exceed competitor description lengths\n"
            "- h1_tag (string): Optimized collection title / H1 tag — keep it clear and descriptive, avoid puffery adjectives\n"
            "- keywords_used (array of strings): Keywords you incorporated into the optimizations\n"
            "- competitor_insights (string): Brief summary of competitor patterns and positioning\n"
            "- additional_recommendations (array of strings): Further SEO improvement suggestions\n"
            "- summary (string): Brief summary of all changes made and why"
            + serp_instruction
            + hierarchy_instruction
        )
        user_prompt = (
            f"Analyze collection ID {collection_id} for SEO optimization. "
            "First get the collection data, then analyze its SEO score, "
            "then provide your optimized recommendations."
        )

    elif task_type == "keyword_analysis":
        ka_store_id = task_input.get("store_id") or store_id
        if not ka_store_id:
            return {"error": "store_id is required for keyword analysis"}

        # --- Phase 1: Build taxonomy by scanning all products ---
        logger.info("Keyword analysis Phase 1: building taxonomy for store %s", ka_store_id)

        phase1_system = (
            "You are an expert Shopify SEO consultant specializing in product taxonomy and tagging strategy. "
            "Your job is to analyze ALL products in a store and build a master tag taxonomy.\n\n"
            "WORKFLOW:\n"
            "1. Use get_all_product_summaries to retrieve ALL products (paginate with offset if total > limit)\n"
            "2. Identify patterns: product types, flavors/variants, categories, brands, packaging, use cases\n"
            "3. Build a master taxonomy of tag categories with specific allowed tags in each\n\n"
            "IMPORTANT:\n"
            "- You MUST paginate through ALL products before building the taxonomy\n"
            "- Tags should be SEO-friendly and useful for Shopify collections\n"
            "- Use consistent capitalization across all tags\n"
            "- Be comprehensive — every product must be taggable using ONLY tags from this taxonomy\n\n"
            "Return your final response as JSON with these keys:\n"
            "- categories (array): [{name: string, tags: [string]}] — the master taxonomy\n"
            "- global_tags (array of strings): tags that could apply to many products across categories\n"
            "- summary (string): explanation of taxonomy structure and key decisions"
        )
        phase1_user = (
            f"Analyze all products in store ID {ka_store_id} and build a comprehensive tag taxonomy. "
            "Start by fetching all products — paginate until you've seen every product."
        )

        p1_messages = [
            {"role": "system", "content": phase1_system},
            {"role": "user", "content": phase1_user},
        ]

        taxonomy_result = None
        for _ in range(20):
            response = client.chat.completions.create(
                model=config["model"],
                messages=p1_messages,
                tools=KEYWORD_ANALYSIS_TOOL_DEFINITIONS,
                temperature=0.3,
            )
            choice = response.choices[0]
            if choice.finish_reason == "tool_calls" or (
                choice.message.tool_calls and len(choice.message.tool_calls) > 0
            ):
                p1_messages.append(choice.message)
                for tool_call in choice.message.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = json.loads(tool_call.function.arguments)
                    result = _handle_keyword_tool_call(fn_name, fn_args)
                    p1_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    })
            else:
                content = choice.message.content or ""
                try:
                    start = content.find("{")
                    end = content.rfind("}") + 1
                    if start >= 0 and end > start:
                        taxonomy_result = json.loads(content[start:end])
                except (json.JSONDecodeError, ValueError):
                    pass
                break

        if not taxonomy_result or "categories" not in taxonomy_result:
            return {"error": "Phase 1 failed — could not build taxonomy", "raw_response": str(taxonomy_result)}

        categories = taxonomy_result.get("categories", [])
        global_tags = taxonomy_result.get("global_tags", [])
        summary = taxonomy_result.get("summary", "")

        all_allowed_tags = set()
        for cat in categories:
            for tag in cat.get("tags", []):
                all_allowed_tags.add(tag)
        for tag in global_tags:
            all_allowed_tags.add(tag)

        logger.info(
            "Phase 1 complete — %d categories, %d total unique tags",
            len(categories), len(all_allowed_tags),
        )

        # --- Phase 2: Assign tags in batches ---
        logger.info("Keyword analysis Phase 2: assigning tags in batches")

        with db.get_cursor() as cur:
            cur.execute(
                "SELECT id, title, product_type, vendor, tags FROM synced_products "
                "WHERE store_id = %s AND status = 'ACTIVE' ORDER BY id",
                (ka_store_id,),
            )
            all_products = cur.fetchall()

        taxonomy_text = json.dumps({"categories": categories, "global_tags": global_tags}, indent=1)

        all_assignments = []
        batch_size = 50
        for batch_start in range(0, len(all_products), batch_size):
            batch = all_products[batch_start:batch_start + batch_size]
            batch_data = [
                {"id": p["id"], "title": p["title"], "product_type": p["product_type"], "vendor": p["vendor"]}
                for p in batch
            ]

            phase2_system = (
                "You are a product tagging assistant. You MUST assign tags to products using ONLY "
                "the tags from the provided taxonomy. Do NOT invent new tags.\n\n"
                "RULES:\n"
                "- Every tag you assign MUST exist in the taxonomy categories or global_tags below\n"
                "- Each product should get 3-10 tags\n"
                "- Products that are variations of the same base item MUST share common tags\n"
                "- Return ONLY valid JSON, no explanation\n\n"
                f"TAXONOMY:\n{taxonomy_text}"
            )
            phase2_user = (
                "Assign tags to each product below. Return JSON: "
                '{\"assignments\": [{\"product_id\": <id>, \"assigned_tags\": [\"Tag1\", \"Tag2\"]}]}\n\n'
                f"PRODUCTS:\n{json.dumps(batch_data, indent=1)}"
            )

            try:
                batch_response = client.chat.completions.create(
                    model=config["model"],
                    messages=[
                        {"role": "system", "content": phase2_system},
                        {"role": "user", "content": phase2_user},
                    ],
                    temperature=0.2,
                )
                batch_content = batch_response.choices[0].message.content or ""
                bstart = batch_content.find("{")
                bend = batch_content.rfind("}") + 1
                if bstart >= 0 and bend > bstart:
                    batch_parsed = json.loads(batch_content[bstart:bend])
                    batch_assignments = batch_parsed.get("assignments", [])
                    for a in batch_assignments:
                        a["assigned_tags"] = [t for t in a.get("assigned_tags", []) if t in all_allowed_tags]
                    all_assignments.extend(batch_assignments)
                    logger.info(
                        "Phase 2 batch %d-%d: assigned tags to %d products",
                        batch_start, batch_start + len(batch), len(batch_assignments),
                    )
            except Exception:
                logger.exception("Phase 2 batch %d failed", batch_start)

        logger.info("Keyword analysis complete — %d total assignments", len(all_assignments))

        return {
            "categories": categories,
            "global_tags": global_tags,
            "product_assignments": all_assignments,
            "summary": summary,
        }

    elif task_type == "interlinking_recommendations":
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT analysis_data FROM interlinking_analysis WHERE store_id = %s",
                (store_id,),
            )
            row = cur.fetchone()

        if not row or not row.get("analysis_data"):
            return {"error": "No interlinking analysis data found. Run analysis first."}

        analysis = row["analysis_data"]
        if isinstance(analysis, str):
            analysis = json.loads(analysis)

        summary = analysis.get("summary", {})
        orphan_products = analysis.get("orphan_products", [])[:20]
        orphan_collections = analysis.get("orphan_collections", [])[:20]
        under_linked = analysis.get("under_linked", [])[:20]
        missing_crosslinks = analysis.get("missing_crosslinks", [])[:20]
        not_in_collection = analysis.get("not_in_collection", [])[:20]

        analysis_context = json.dumps({
            "summary": summary,
            "orphan_products": orphan_products,
            "orphan_collections": orphan_collections,
            "under_linked": under_linked,
            "missing_crosslinks": missing_crosslinks,
            "not_in_collection": not_in_collection,
        }, indent=2)

        system_prompt = (
            "You are an expert SEO consultant specializing in internal linking strategy. "
            "Analyze the interlinking data for a Shopify store and provide actionable recommendations.\n\n"
            "Return your response as JSON with these keys:\n"
            "- priority_fixes (array): Up to 10 most impactful link additions, each with:\n"
            "  - action: 'add_product_link' or 'add_collection_link'\n"
            "  - from_type: 'product' or 'collection'\n"
            "  - from_title: source page title\n"
            "  - from_handle: source page handle\n"
            "  - to_title: target page title\n"
            "  - to_handle: target page handle\n"
            "  - suggested_anchor: natural anchor text to use\n"
            "  - reason: why this link should be added\n"
            "- crosslink_suggestions (array): Up to 5 collection pairs that should link to each other, each with:\n"
            "  - collection_a: first collection title\n"
            "  - collection_b: second collection title\n"
            "  - shared_products: number of shared products\n"
            "  - reason: why these should be cross-linked\n"
            "- summary (string): 2-3 sentence overview of the store's interlinking health and top priorities\n\n"
            "Prioritize: orphan products first (they get zero SEO juice), then under-linked pages, then cross-links."
        )
        user_prompt = (
            "Here is the interlinking analysis data for the store:\n\n"
            + analysis_context
            + "\n\nProvide your prioritized recommendations."
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )

        content = response.choices[0].message.content or ""
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_response": content}

    elif task_type == "store_audit":
        system_prompt = (
            "You are an expert Shopify SEO consultant. Analyze the store's overall SEO health "
            "and provide a summary with top priorities for improvement. Format your response as JSON "
            "with keys: overall_assessment (string), top_issues (array of strings), "
            "quick_wins (array of strings), and priority_products (array of product IDs that need the most work)."
        )
        user_prompt = (
            f"Perform an SEO audit of store ID {store_id}. "
            "Get the store summary and identify key areas for improvement."
        )
    else:
        raise ValueError(f"Unknown task type: {task_type}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    main_result = None
    max_iterations = 10
    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=config["model"],
            messages=messages,
            tools=TOOL_DEFINITIONS,
            temperature=0.3,
        )

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls" or (
            choice.message.tool_calls and len(choice.message.tool_calls) > 0
        ):
            messages.append(choice.message)
            for tool_call in choice.message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                result = _handle_tool_call(fn_name, fn_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    }
                )
        else:
            content = choice.message.content or ""
            try:
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    main_result = json.loads(content[start:end])
            except (json.JSONDecodeError, ValueError):
                pass
            if main_result is None:
                main_result = {"raw_response": content}
            break

    if main_result is None:
        main_result = {"error": "Max iterations reached without final response"}

    if task_type == "product_seo":
        alt_suggestions = main_result.get("image_alt_suggestions", [])
        if alt_suggestions:
            with db.get_cursor() as cur:
                cur.execute("SELECT images FROM synced_products WHERE id = %s", (product_id,))
                row = cur.fetchone()
            images = (row.get("images") if row else None) or []
            if isinstance(images, str):
                images = json.loads(images)

            paired = []
            for i, img in enumerate(images):
                mid = img.get("media_id", "")
                if not mid:
                    continue
                if i < len(alt_suggestions):
                    alt = alt_suggestions[i]
                    if isinstance(alt, dict):
                        alt = alt.get("suggested_alt") or alt.get("alt") or ""
                    paired.append({"media_id": mid, "suggested_alt": str(alt)})
            main_result["image_alt_suggestions"] = paired

        main_result = _generate_description(client, config, main_result, serp_context, serp_benchmark, product_id)

    elif task_type == "collection_seo":
        main_result = _generate_collection_description(
            client, config, main_result, serp_context, serp_benchmark, collection_id,
            hierarchy_context=task_input.get("hierarchy_context"),
        )

    return main_result
