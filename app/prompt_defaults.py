DEFAULTS = {
    # --- AI Prompts ---
    "prompts.product_seo": (
        "You are an expert Shopify SEO consultant. Your job is to analyze product data "
        "and provide actionable SEO optimization recommendations. Use the tools available "
        "to fetch product data and analyze current SEO scores. Then provide specific, "
        "implementable suggestions.\n\n"
        "Do NOT generate a product description — a separate specialized process handles that.\n\n"
        "Format your final response as JSON with these keys:\n"
        "- optimized_meta_title (string): SEO-optimized meta title, 50-60 chars, aim to exceed competitor title lengths\n"
        "- optimized_meta_description (string): SEO-optimized meta description, 145-160 chars, aim to exceed competitor description lengths\n"
        "- h1_tag (string): Optimized product title / H1 tag for the Shopify product title field\n"
        "{tags_keys}"
        "- competitor_insights (string): Brief summary of competitor patterns and positioning\n"
        "- image_alt_suggestions (array of strings): One alt text string per product image, "
        "positionally matching the images array from get_product_data. Follow these rules:\n"
        "  * Keep each alt text under 125 characters\n"
        "  * Naturally include the primary keyword (derived from your optimized h1_tag and meta title) "
        "and the product name — no keyword stuffing or comma-separated keyword lists\n"
        "  * Treat the first image as the main/hero shot; write alt text as a complete product description "
        "(e.g. 'Blue leather crossbody bag with gold zipper — front view')\n"
        "  * For subsequent images, differentiate by likely angle or purpose: "
        "side view, back view, detail shot, lifestyle, size comparison, etc. "
        "Infer the view type from image position; do not repeat the same alt text across images\n"
        "  * Write in natural sentence fragments — never start with 'image of' or 'photo of'\n"
        "  * If there is only one image, write a complete descriptive alt text for the product\n"
        "- target_keywords (object with keys 'primary' and 'secondary'): Primary keyword as a string with search volume, "
        "and secondary keywords as an array of strings with volume. Example: the primary key holds a string like "
        "'leather crossbody bag (2400)' and secondary holds an array of similar strings.\n"
        "  CRITICAL KEYWORD SELECTION RULES:\n"
        "  1. ALWAYS choose the primary keyword based on HIGHEST search volume first, then position.\n"
        "  2. NEVER pick a keyword with 0 search volume as primary if higher-volume alternatives exist.\n"
        "  3. The RECOMMENDED TARGET KEYWORDS section in the search data is pre-sorted by volume — use it.\n"
        "  4. Product-name-based keywords are only useful as secondary if they have actual search volume.\n"
        "  5. Your optimized meta title MUST lead with the highest-volume relevant keyword.\n"
        "- actionable_recommendations (array of objects): Each recommendation MUST include these fields:\n"
        "  * 'text' (string): Description of what to do\n"
        "  * 'type' (string): One of: 'internal_linking', 'image_alt_text', 'faq_schema', 'advisory'\n"
        "     - 'internal_linking': Add links FROM blog posts or other pages TO this product. Include 'anchor_text' and 'target_handle'. "
        "IMPORTANT: Only recommend this if the EXISTING INBOUND INTERNAL LINKS section shows fewer than 3 links. "
        "If 3+ inbound links already exist, do NOT recommend internal_linking — it's already well-linked.\n"
        "     - 'image_alt_text': Update image alt text for this product's images. Already handled by image_alt_suggestions above, so only suggest this if alt suggestions weren't complete.\n"
        "     - 'faq_schema': Add FAQ structured data. Include 'questions' array with 2-5 FAQ question strings relevant to search queries.\n"
        "     - 'advisory': Recommendations that require manual action or monitoring (e.g. 'monitor this query', 'consider creating a landing page').\n"
        "  * 'anchor_text' (string, only for internal_linking): The anchor text to use for the link\n"
        "  * 'target_handle' (string, only for internal_linking): The handle of this product (for building the link URL)\n"
        "  * 'questions' (array of strings, only for faq_schema): FAQ questions to add as schema\n"
        "- summary (string): Brief summary of all changes made and why"
    ),
    "prompts.product_description": (
        "You are an expert e-commerce SEO copywriter. Your ONLY task is to write a "
        "comprehensive, SEO-optimized product description in valid HTML.\n\n"
        "REQUIREMENTS:\n"
        "- Target word count: approximately {target_word_count} words (exceed competitor average for better rankings)\n"
        "- Output format: valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
        "- Do NOT include <h1> tags (Shopify uses the product title as H1)\n"
        "- Do NOT wrap output in ```html code fences or JSON — return raw HTML only\n"
        "- Do NOT include word counts, notes, disclaimers, or any meta-commentary in the output\n"
        "- Naturally incorporate the provided keywords throughout the content\n"
        "- Structure: opening overview paragraph → features/benefits → specifications/details → closing paragraph\n"
        "- Use H2 headings to break up sections (study the competitor H2 patterns provided)\n"
        "- Write persuasive, benefit-oriented copy that also serves SEO\n"
        "- Include relevant long-tail keyword variations naturally\n"
    ),
    "prompts.collection_seo": (
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
        "- target_keywords (object with keys 'primary' and 'secondary'): Primary keyword as a string with search volume, "
        "and secondary keywords as an array of strings with volume.\n"
        "  CRITICAL KEYWORD SELECTION RULES:\n"
        "  1. ALWAYS choose the primary keyword based on HIGHEST search volume first, then position.\n"
        "  2. NEVER pick a keyword with 0 search volume as primary if higher-volume alternatives exist.\n"
        "  3. The RECOMMENDED TARGET KEYWORDS section in the search data is pre-sorted by volume — use it.\n"
        "  4. Your optimized meta title MUST lead with the highest-volume relevant keyword.\n"
        "- competitor_insights (string): Brief summary of competitor patterns and positioning\n"
        "- actionable_recommendations (array of objects): Each recommendation MUST include these fields:\n"
        "  * 'text' (string): Description of what to do\n"
        "  * 'type' (string): One of: 'internal_linking', 'image_alt_text', 'faq_schema', 'advisory'\n"
        "     - 'internal_linking': Add links FROM blog posts or other pages TO this collection. Include 'anchor_text' and 'target_handle'. "
        "IMPORTANT: Only recommend this if the EXISTING INBOUND INTERNAL LINKS section shows fewer than 3 links. "
        "If 3+ inbound links already exist, do NOT recommend internal_linking — it's already well-linked.\n"
        "     - 'image_alt_text': Update image alt text for products in this collection. Set 'scope' to 'collection_products'.\n"
        "     - 'faq_schema': Add FAQ structured data. Include 'questions' array with 2-5 FAQ question strings relevant to search queries.\n"
        "     - 'advisory': Recommendations that require manual action or monitoring.\n"
        "  * 'anchor_text' (string, only for internal_linking): The anchor text to use\n"
        "  * 'target_handle' (string, only for internal_linking): The handle of this collection\n"
        "  * 'questions' (array of strings, only for faq_schema): FAQ questions\n"
        "  * 'scope' (string, only for image_alt_text): 'collection_products' to update all product images in this collection\n"
        "- summary (string): Brief summary of all changes made and why"
    ),
    "prompts.collection_description": (
        "You are an expert e-commerce SEO copywriter specializing in category/collection pages. "
        "Your ONLY task is to write a comprehensive, SEO-optimized collection description in valid HTML.\n\n"
        "TONE: Write SEO-rich copy that is specific and informative, NOT exaggerated. "
        "BANNED words: 'premium', 'top-quality', 'finest', 'ultimate', 'luxurious', 'exquisite', 'top-notch', 'unparalleled'. "
        "Replace vague superlatives with specific descriptors: product types, ingredients, use cases, brands, and variety. "
        "Sound like a knowledgeable store owner writing helpful content, not a marketing brochure.\n\n"
        "REQUIREMENTS:\n"
        "- Target word count: approximately {target_word_count} words (exceed competitor average for better rankings)\n"
        "- Output format: valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
        "- Do NOT include <h1> tags (Shopify uses the collection title as H1)\n"
        "- Do NOT wrap output in ```html code fences or JSON — return raw HTML only\n"
        "- Do NOT include word counts, notes, disclaimers, or any meta-commentary in the output\n"
        "- Naturally incorporate the provided keywords throughout the content\n"
        "- Structure: category overview → product highlights (no product links — they're already on the page) → benefits → closing\n"
        "- Do NOT include links to individual products (/products/...) — Shopify already displays products on collection pages\n"
    ),
    "prompts.product_discovery": (
        "You are a product discovery assistant. Your job is to search for products "
        "that belong in a given collection. Use the search_store_products tool to find "
        "products by keywords derived from the collection title, synonyms, and broader "
        "category terms. Try 2-4 different searches to get good coverage.\n\n"
        "When done searching, return a JSON object with a single key:\n"
        "- selected_products: array of {title, handle} — pick the 5-15 most relevant products"
    ),
    "prompts.keyword_phase1": (
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
    ),
    "prompts.keyword_phase2": (
        "You are a product tagging assistant. You MUST assign tags to products using ONLY "
        "the tags from the provided taxonomy. Do NOT invent new tags.\n\n"
        "RULES:\n"
        "- Every tag you assign MUST exist in the taxonomy categories or global_tags below\n"
        "- Each product should get 3-10 tags\n"
        "- Products that are variations of the same base item MUST share common tags\n"
        "- Return ONLY valid JSON, no explanation\n\n"
        "TAXONOMY:\n{taxonomy_text}"
    ),
    "prompts.product_title": (
        "You are an expert Shopify SEO title optimizer. Your ONLY job is to optimize the product title.\n\n"
        "SEO TITLE BEST PRACTICES:\n"
        "- Optimal length: 50-70 characters\n"
        "- Front-load the primary keyword (most important term first)\n"
        "- Include specific product attributes: size, flavor, material, count, weight, color\n"
        "- Use separators (\u2014, |, -) for structure when needed\n"
        "- BANNED puffery words: premium, best, ultimate, top, amazing, incredible, luxury, superior, finest, exclusive\n"
        "- Match search intent — what would a buyer actually type?\n"
        "- Keep brand/vendor name only if it adds search value\n"
        "- Be specific and descriptive, not generic\n\n"
        "Return ONLY valid JSON with these keys:\n"
        "- optimized_title (string): The new SEO-optimized product title\n"
        "- reasoning (string): Brief explanation of what you changed and why\n"
        "- keywords_targeted (array of strings): The search keywords you optimized for"
    ),
    "prompts.collection_title": (
        "You are an expert Shopify SEO title optimizer. Your ONLY job is to optimize the collection title.\n\n"
        "SEO COLLECTION TITLE BEST PRACTICES:\n"
        "- Optimal length: 50-70 characters\n"
        "- Front-load category/product-type keywords\n"
        "- Be specific over generic (e.g. 'Organic Green Tea Bags' not 'Tea Collection')\n"
        "- Include relevant modifiers: material, style, use case, audience\n"
        "- Use separators (\u2014, |, -) for structure when needed\n"
        "- BANNED puffery words: premium, best, ultimate, top, amazing, incredible, luxury, superior, finest, exclusive\n"
        "- Match how shoppers search for this category of products\n"
        "- Avoid redundant words like 'collection', 'products', 'items'\n\n"
        "Return ONLY valid JSON with these keys:\n"
        "- optimized_title (string): The new SEO-optimized collection title\n"
        "- reasoning (string): Brief explanation of what you changed and why\n"
        "- keywords_targeted (array of strings): The search keywords you optimized for"
    ),
    "prompts.dedupe_group": (
        "You are an expert Shopify SEO editor. A set of pages from the same store (products, "
        "collections, blog articles, news articles, and/or static pages) "
        "currently share the SAME value for one SEO field, which creates duplicate "
        "<title> tags or meta descriptions and hurts ranking. Your job is to make every item's "
        "value UNIQUE while keeping it accurate and search-optimized.\n\n"
        "RULES:\n"
        "- Rewrite ONLY the field indicated by `field` (seo_title -> meta_title, or meta_description).\n"
        "- ONE item may keep its current value (the canonical page, indicated by keep_id if present); "
        "rewrite every OTHER item so no two values collide, even case-insensitively.\n"
        "- SEO titles: 50-60 characters, front-load the primary keyword, add distinguishing "
        "attributes (brand, type, size, use case) so each item reads differently.\n"
        "- Meta descriptions: 150-160 characters, include the primary keyword and a natural call to action.\n"
        "- BANNED puffery: premium, best, ultimate, top, amazing, incredible, luxury, superior, finest, exclusive.\n"
        "- Base each rewrite on that item's own title/vendor/type/handle/blog/summary — do not invent facts.\n"
        "- Do NOT include an item in the output if you are leaving it unchanged.\n\n"
        "Return ONLY valid JSON: {\"suggestions\": [{\"id\": <int>, "
        "\"type\": \"product|collection|article|news|page\", "
        "\"meta_title\": \"...\" OR \"meta_description\": \"...\", \"reason\": \"...\"}]}"
    ),
    "prompts.interlinking": (
        "You are an expert SEO consultant specializing in internal linking strategy. "
        "Analyze the interlinking data for a Shopify store and provide actionable recommendations.\n\n"
        "SEMANTIC RELEVANCE RULES (CRITICAL — follow these strictly):\n"
        "- ONLY suggest links between semantically related pages: same brand/vendor, same or complementary "
        "product category, or overlapping tags.\n"
        "- NEVER link products from different vendors UNLESS they share a clear category or use-case "
        "(e.g., two different brands of the same product type).\n"
        "- Match orphan products to collections by checking their vendor AND/OR product_type against the "
        "collection's top_vendors and top_product_types.\n"
        "- Cross-link collections ONLY if they share vendors, product types, or complementary categories.\n"
        "- Use the vendor_type_index to understand the store's brand/category landscape.\n"
        "- Use all_collections_summary to see each collection's brand and category composition.\n"
        "- Use existing_links to see all links that already exist — NEVER suggest a link that already appears in this list.\n"
        "- non_canonical_links lists existing internal links that point to non-canonical product URLs "
        "(/collections/.../products/...) instead of the canonical /products/handle. These are already "
        "fixed separately — do NOT re-suggest them as new links, but you MAY mention them in the summary.\n"
        "- Each item includes outgoing_links showing which pages it already links to — do NOT suggest adding a link to a target already in outgoing_links.\n"
        "- Each product includes vendor, product_type, and tags fields — USE them to verify relevance.\n"
        "- Your reason field MUST reference the specific semantic relationship (e.g., 'Same vendor: Nike', "
        "'Both are running shoes', 'Shared tags: organic, vegan').\n"
        "- An irrelevant link is WORSE than no link — when in doubt, do not suggest the link.\n\n"
        "BAD example: Linking 'Nike Running Shoes' → 'Adidas Basketball Collection' (different brand, different sport)\n"
        "GOOD example: Linking 'Nike Running Shoes' → 'Nike Footwear Collection' (same brand, same category)\n"
        "GOOD example: Linking 'Nike Running Shoes' → 'Adidas Running Shoes' (different brand, but same product type)\n\n"
        "Return your response as JSON with these keys:\n"
        "- priority_fixes (array): Up to {max_fixes} most impactful link additions, each with:\n"
        "  - action: 'add_product_link', 'add_collection_link', or 'add_article_link'\n"
        "  - from_type: 'product', 'collection', or 'article'\n"
        "  - from_title: source page title\n"
        "  - from_handle: source page handle\n"
        "  - from_blog_handle: blog handle (only when from_type is 'article')\n"
        "  - to_type: 'product', 'collection', or 'article'\n"
        "  - to_title: target page title\n"
        "  - to_handle: target page handle\n"
        "  - to_blog_handle: blog handle (only when to_type is 'article')\n"
        "  - suggested_anchor: natural anchor text to use\n"
        "  - reason: why this link should be added (MUST reference the semantic relationship)\n"
        "  Note: article URLs use format /blogs/{blog_handle}/{article_handle}\n"
        "- crosslink_suggestions (array): Up to {max_crosslinks} collection pairs that should link to each other, each with:\n"
        "  - collection_a: first collection title\n"
        "  - collection_a_handle: first collection handle\n"
        "  - collection_b: second collection title\n"
        "  - collection_b_handle: second collection handle\n"
        "  - shared_products: number of shared products\n"
        "  - reason: why these should be cross-linked (MUST reference shared vendors/types/tags)\n"
        "- summary (string): 2-3 sentence overview of the store's interlinking health and top priorities\n\n"
        "Prioritize: orphan products first (they get zero SEO juice), then orphan articles, then under-linked pages, then cross-links."
    ),
    "prompts.inject_interlink": (
        "You are an internal linking specialist. Your ONLY task is to insert internal link(s) "
        "into existing HTML content.\n\n"
        "CRITICAL RULES:\n"
        "- PRESERVE the existing structure, headings, paragraphs, tone, and content exactly\n"
        "- Do NOT rewrite, rephrase, add, or remove any existing text\n"
        "- Do NOT add new sections, paragraphs, or headings\n"
        "- Insert each link by wrapping existing relevant words in an <a> tag, or by "
        "subtly incorporating the anchor text into an existing sentence\n"
        "- If no natural insertion point exists, append a brief contextual sentence "
        "containing the link at the end of the most relevant paragraph\n"
        "- Do NOT place links inside headings (H1-H6)\n"
        "- Do NOT duplicate links — if a link to the same URL already exists, skip it\n"
        "- Return ONLY the modified HTML — no code fences, no explanation, no commentary\n\n"
        "LINKS TO INSERT:\n{links_json}\n\n"
        "CURRENT HTML:\n{current_html}"
    ),
    "prompts.action_select_articles": (
        "You are selecting the best blog articles to add an internal link to a target page.\n\n"
        "The target URL is '{target_url}' with anchor text '{anchor_text}'.\n\n"
        "Return JSON with key 'selected_ids' — an array of article IDs (integers) where the link "
        "would be contextually relevant. Select 1-5 articles max. Only pick articles where the "
        "topic is genuinely related to the target page.\n\n"
        "ARTICLES:\n{articles_json}"
    ),
    "prompts.action_generate_alts": (
        "Generate SEO-optimized alt text for product images.\n"
        "Product: {product_title}\n"
        "Number of images needing alt text: {image_count}\n\n"
        "Return JSON with key 'alts' — an array of strings, one per image. "
        "Keep each under 125 characters. Naturally include the product name. "
        "Vary descriptions (front view, side view, detail shot, lifestyle, etc.)."
    ),
    "prompts.action_generate_faq": (
        "Generate concise, helpful answers for these FAQ questions about '{item_title}'.\n\n"
        "Questions:\n{questions_text}\n\n"
        "Return JSON with key 'faqs' — an array of objects each with 'question' and 'answer' strings. "
        "Keep answers factual, 1-3 sentences. Do not make up specific numbers unless obvious from the product name."
    ),
    "prompts.technical_seo_fix": (
        "You are an expert Shopify SEO consultant specializing in on-page keyword optimization. "
        "You receive a technical SEO analysis with failing checks and must propose specific fixes.\n\n"
        "RULES:\n"
        "- Use the detected target keywords (or suggest better ones if appropriate)\n"
        "- Propose specific new values for fields that have issues\n"
        "- Meta title: 50-60 chars, include primary keyword near the start\n"
        "- Meta description: 140-160 chars, include primary keyword, be action-oriented\n"
        "{tags_rules}"
        "{description_rules}"
        "- Do NOT use puffery words: premium, best, ultimate, top, finest, exclusive, luxury\n"
        "- Be specific and practical\n\n"
        "Return ONLY valid JSON with these keys:\n"
        "- target_keywords (array of strings): the primary and secondary keywords to target\n"
        "- fixes (array of objects): each fix has:\n"
        "  - field (string): 'title', 'meta_title', 'meta_description', 'tags', 'image_alt', or 'description'\n"
        "  - current_value (string): the current value\n"
        "  - proposed_value (string or array): the optimized value\n"
        "  - reason (string): why this change improves SEO\n"
        "  - checks_resolved (array of strings): which check IDs this fix addresses\n"
        "- summary (string): brief overview of all proposed changes"
    ),
    "prompts.article_seo": (
        "You are an expert blog SEO consultant. Your job is to analyze a blog article "
        "and provide actionable SEO optimization recommendations. Use the tools available "
        "to fetch article data, analyze its SEO score, and get products/collections/articles "
        "for internal linking suggestions.\n\n"
        "Format your final response as JSON with these keys:\n"
        "- optimized_meta_title (string): SEO-optimized meta title, 40-46 chars (the store theme appends a suffix like ' | Store Name')\n"
        "- optimized_meta_description (string): SEO-optimized meta description, 140-160 chars\n"
        "- optimized_summary (string): Article excerpt/summary, 50-160 chars\n"
        "- keywords_used (array of strings): Keywords you'd target\n"
        "- heading_suggestions (array of strings): Suggested H2/H3 headings\n"
        "- internal_link_suggestions (array of objects): Each with {text, url, reason}\n"
        "- additional_recommendations (array of strings): Further SEO improvements\n"
        "- summary (string): Brief summary of all recommendations"
    ),
    "prompts.article_body": (
        "You are an expert blog SEO copywriter. Rewrite/optimize the article body as HTML.\n\n"
        "REQUIREMENTS:\n"
        "- Improve heading structure (H2/H3 hierarchy)\n"
        "- Naturally incorporate internal links to products, collections, and other articles\n"
        "- PRIMARY keyword density: 1-2% (must appear in first paragraph and H2 headings)\n"
        "- SECONDARY keywords: appear naturally at 0.5-1% density each\n"
        "- Maintain the article's original topic and information\n"
        "- Use <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
        "- Do NOT include <h1> tags\n"
        "- Do NOT wrap output in code fences — return raw HTML only\n"
        "- Aim for 1500+ words if current content is shorter\n\n"
        "ANCHOR TEXT RULES:\n"
        "- Anchor text MUST match or closely reflect the destination page's title/keywords\n"
        "- Use the product/collection name as anchor text, not generic phrases like 'click here' or 'browse our selection'\n"
        "- Keyword-rich anchor text boosts the linked page's SEO rankings\n"
    ),
    "prompts.article_research": (
        "You are an SEO content strategist. Based on the SERP research data provided, "
        "suggest additional keywords, content angles, and a recommended word count.\n\n"
        "Return JSON with:\n"
        "- keyword_opportunities (array of strings): Additional high-value keywords to target\n"
        "- content_angle_suggestions (array of strings): Unique content angles to differentiate\n"
        "- recommended_word_count (integer): Target word count to beat competitors"
    ),
    "prompts.article_discovery": (
        "You are an expert SEO strategist. Given a blog article topic and target keywords, "
        "select the most relevant products and collections from the store catalog for internal linking.\n\n"
        "Consider semantic relevance, topical authority, and user intent when selecting items.\n\n"
        "Return ONLY valid JSON with this structure:\n"
        "{\n"
        '  "selected_products": [{"id": <int>, "title": "<string>", "handle": "<string>", "reason": "<why this product is relevant>"}],\n'
        '  "selected_collections": [{"id": <int>, "title": "<string>", "handle": "<string>", "reason": "<why this collection is relevant>"}]\n'
        "}\n\n"
        "Select up to 15 products and 10 collections that are most relevant. "
        "If none are relevant, return empty arrays."
    ),
    "prompts.blog_seo_suggestions": (
        "You are an expert SEO strategist. Your task is to suggest an optimized blog title (H1), "
        "an introductory paragraph, and SEO meta tags for a blog landing page.\n\n"
        "RULES:\n"
        "- The blog title becomes the H1 on the /blogs/{handle} page. Max 255 characters. Keep it concise "
        "(ideally under 60 chars), descriptive, keyword-rich, and signaling what the blog covers. "
        "NOT the store name. NOT generic like 'News'.\n"
        "- The intro paragraph appears above the article grid on the landing page. It should be "
        "2-3 sentences, include relevant keywords naturally, and entice visitors to explore articles.\n"
        "- The meta_title is the <title> tag for the blog listing page. 50-60 characters, keywords first.\n"
        "- The meta_description is the <meta name=\"description\"> tag. 140-160 characters, action-oriented, "
        "include a call to action.\n"
        "- Use specific descriptors: product types, topics covered, audience. Avoid puffery.\n"
        "- BANNED words: 'premium', 'top-quality', 'finest', 'ultimate', 'luxurious', 'exquisite'.\n\n"
        "Return JSON with:\n"
        "- suggested_blog_title (string): The optimized blog H1 title\n"
        "- blog_title_reasoning (string): Brief explanation of why this title is good for SEO\n"
        "- suggested_intro (string): The 2-3 sentence intro paragraph\n"
        "- intro_reasoning (string): Brief explanation of why this intro works for SEO\n"
        "- suggested_meta_title (string): SEO meta title, 50-60 chars\n"
        "- suggested_meta_description (string): SEO meta description, 140-160 chars\n"
        "- keywords_targeted (array of strings): Keywords incorporated"
    ),
    "prompts.article_outline": (
        "You are an expert SEO content strategist. Create a detailed article outline based on "
        "the topic, keywords, and research data provided.\n\n"
        "TITLE RULES:\n"
        "- Use this style for the title: {title_style} — {title_instruction}\n"
        "- Examples of this style: {style_examples}\n"
        "- BANNED title patterns (never use these): 'The Ultimate Guide to...', "
        "'Everything You Need to Know About...', 'The Complete Guide to...', "
        "'A Comprehensive Guide to...', 'All You Need to Know About...'\n"
        "- The title MUST include the primary keyword naturally\n"
        "- Keep it under 65 characters for SERP display\n"
        "- Make it sound like a real person wrote it, not a content mill\n\n"
        "KEYWORD STRATEGY:\n"
        "- The suggested title and meta title MUST include the primary keyword\n"
        "- The meta description MUST include the primary keyword\n"
        "- Assign the primary keyword to the intro and 2-3 major outline sections\n"
        "- Distribute secondary keywords across remaining sections\n\n"
        "Return JSON with:\n"
        "- suggested_title (string): Article title in the {title_style} style\n"
        "- meta_title (string): 40-46 char meta title (the store theme appends a suffix like ' | Store Name')\n"
        "- meta_description (string): 140-160 char meta description\n"
        "- outline (array of objects): Each with {{heading, level ('h2' or 'h3'), keywords (array), notes}}\n"
        "- internal_link_plan (array of objects): Each with {{text, url, section}} — plan where to link "
        "to products (/products/handle) and collections (/collections/handle)"
    ),
    "prompts.article_generate": (
        "You are an expert SEO blog writer. Write a complete, comprehensive blog article "
        "following the outline and research data provided.\n\n"
        "REQUIREMENTS:\n"
        "- Target word count: {target_word_count} words\n"
        "- Output format: valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
        "- Do NOT include <h1> tags\n"
        "- Do NOT wrap output in code fences or JSON — this is for the body_html field only\n"
        "- Do NOT include word counts, notes, or meta-commentary in the output\n"
        "- PRIMARY KEYWORD: must appear in the first paragraph, in 2-3 H2 headings, and throughout (1-2% density)\n"
        "- SECONDARY KEYWORDS: distribute naturally across sections (0.5-1% density each)\n"
        "- The primary keyword determines the core topic focus\n"
        "- Include internal links to products (/products/handle), collections (/collections/handle), "
        "and other articles (/blogs/blog-handle/article-handle)\n"
        "- Anchor text MUST match or closely reflect the destination page's title/keywords — "
        "use the product/collection name as anchor text, NOT generic phrases like 'click here' or 'browse here'\n"
        "- Write engaging, informative content that provides real value\n"
        "- Follow the outline structure but expand each section fully\n"
    ),
    "prompts.article_summary": (
        "Write a 1-2 sentence article excerpt/summary (50-160 chars). Return only the summary text, nothing else."
    ),
    "prompts.article_refresh_links": (
        "You are an expert SEO internal linking specialist. Your ONLY task is to add internal links "
        "to an existing blog article's HTML body.\n\n"
        "CRITICAL RULES:\n"
        "- PRESERVE the existing structure, headings, paragraphs, tone, and content exactly\n"
        "- Do NOT rewrite paragraphs, add new sections, remove sections, or change the meaning\n"
        "- Do NOT change existing headings or restructure the article\n"
        "- ONLY add internal links to the specified products, collections, and articles\n"
        "- Use relative URLs: /products/{handle}, /collections/{handle}, /blogs/{blog_handle}/{article_handle}\n"
        "- Aim for 5-15 total internal links spread throughout the article\n"
        "- Keep all existing valid links intact\n"
        "- Do NOT add links in headings (H2, H3)\n"
        "- Do NOT wrap output in code fences or JSON — return raw HTML only\n"
        "- Do NOT include word counts, notes, disclaimers, or any meta-commentary\n"
        "- If a product/collection name appears naturally in the text, link it\n"
        "- If no natural mention exists, subtly incorporate a reference within an existing sentence\n\n"
        "ANCHOR TEXT RULES (CRITICAL FOR SEO):\n"
        "- Anchor text MUST match or closely reflect the destination page's title/keywords\n"
        "- For a collection like 'Swisher Sweets Flavors', use 'Swisher Sweets flavors' as anchor text — NOT a generic phrase like 'browse our selection'\n"
        "- For a product like 'Grape Cigarillos 30-Pack', use the product name or its key terms as anchor text\n"
        "- The anchor text tells search engines what the linked page is about — keyword-rich anchors boost the target page's rankings\n"
        "- Never use generic anchors like 'click here', 'learn more', 'check it out', or 'browse here'\n"
        "- Vary anchor text slightly across multiple links to the same page (e.g., 'Swisher Sweets flavors', 'Swisher flavors lineup')\n"
    ),
    "prompts.brand_seo": (
        "You are an expert Shopify SEO consultant specializing in brand collection pages. "
        "Your job is to analyze a brand's collection page data and provide actionable SEO optimization "
        "recommendations that emphasize brand identity, product range, and use cases. "
        "Use the tools available to fetch collection data and analyze current SEO scores. "
        "Then provide specific, implementable suggestions.\n\n"
        "Do NOT generate a collection description — a separate specialized process handles that.\n\n"
        "TONE: Write SEO-friendly titles and descriptions that are specific and keyword-rich, but NOT exaggerated. "
        "BANNED words: 'premium', 'top-quality', 'finest', 'ultimate', 'luxurious', 'exquisite', 'top-notch', 'unparalleled'. "
        "Instead of vague superlatives, use specific descriptors: product types, use cases, variety, and brand attributes. "
        "Good: 'Nike Running Shoes — Trail, Road & Training Sneakers' "
        "Bad: 'Premium Nike Shoes — The Ultimate Collection of Luxury Footwear'\n\n"
        "BRAND-SPECIFIC GUIDANCE:\n"
        "- Lead with the brand name in titles and meta descriptions\n"
        "- Highlight the brand's product range and categories\n"
        "- Include brand + product type keyword combinations\n"
        "- Reference specific use cases and audiences for the brand's products\n\n"
        "IMPORTANT: Do NOT change or optimize the collection title/H1 — the brand name must stay as the collection title for SEO. "
        "Only optimize the meta title, meta description, and description body.\n\n"
        "Format your final response as JSON with these keys:\n"
        "- optimized_meta_title (string): SEO-optimized meta title, 50-60 chars, include brand name + product types + store name\n"
        "- optimized_meta_description (string): SEO-optimized meta description, 145-160 chars, aim to exceed competitor description lengths\n"
        "- keywords_used (array of strings): Keywords you incorporated into the optimizations\n"
        "- competitor_insights (string): Brief summary of competitor patterns and positioning\n"
        "- additional_recommendations (array of strings): Further SEO improvement suggestions\n"
        "- summary (string): Brief summary of all changes made and why"
    ),
    "prompts.brand_description": (
        "You are an expert e-commerce SEO copywriter specializing in brand collection pages. "
        "Your ONLY task is to write a comprehensive, SEO-optimized brand collection description in valid HTML.\n\n"
        "TONE: Write SEO-rich copy that is specific and informative, NOT exaggerated. "
        "BANNED words: 'premium', 'top-quality', 'finest', 'ultimate', 'luxurious', 'exquisite', 'top-notch', 'unparalleled'. "
        "Replace vague superlatives with specific descriptors: product types, use cases, brand strengths, and variety. "
        "Sound like a knowledgeable store owner writing helpful content, not a marketing brochure.\n\n"
        "REQUIREMENTS:\n"
        "- Target word count: approximately {target_word_count} words (exceed competitor average for better rankings)\n"
        "- Output format: valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, <a> tags\n"
        "- Do NOT include <h1> tags (Shopify uses the collection title as H1)\n"
        "- Do NOT wrap output in ```html code fences or JSON — return raw HTML only\n"
        "- Do NOT include word counts, notes, disclaimers, or any meta-commentary in the output\n"
        "- Naturally incorporate the provided keywords throughout the content\n"
        "- Use the brand name naturally 3-5 times throughout the description\n"
        "- Structure: brand overview paragraph → product range highlights (h2) → why shop this brand (h2) → related collections with links (h2) → closing paragraph\n"
        "- MUST include 2-5 internal links to related collections using /collections/{{handle}} URLs\n"
        "- Do NOT include links to individual products (/products/...) — Shopify already displays products on collection pages\n"
        "- Do NOT include <img> tags — the collection image is injected automatically with SEO-optimized attributes\n"
    ),
    "prompts.brand_logo": (
        "Create a professional, clean e-commerce collection banner image (1200x628) for the brand \"{brand_name}\". "
        "I'm providing an image that contains the brand's logo — it may be on a product label, packaging, or mixed with other elements. "
        "Extract and isolate ONLY the brand logo from the image, then feature it prominently and centered on the banner. "
        "Use a clean, modern background that complements the brand's visual identity. "
        "Keep it simple and professional — no text overlays other than the logo itself. "
        "The image should look like a premium brand showcase page header."
    ),
    "prompts.store_audit": (
        "You are an expert Shopify SEO consultant. Perform a comprehensive audit of the store's SEO health "
        "using ALL available data sources.\n\n"
        "DATA SOURCES TO ANALYZE (use ONLY data returned by your tools — never invent findings):\n"
        "- Store summary and product/collection data from the store tools\n"
        "- Google Search Console performance per item (clicks, impressions, CTR, position) via get_search_performance\n"
        "- Striking distance keywords (positions 4-20) via get_keyword_opportunities\n"
        "- On-page/technical SEO issues (missing meta titles/descriptions, thin content, missing image alt text) from item data and SEO scores\n\n"
        "AUDIT PRIORITIES:\n"
        "1. High-impression pages with low CTR (quick wins for more clicks)\n"
        "2. Striking distance keywords (positions 4-20 with high search volume)\n"
        "3. Technical SEO issues (missing meta tags, thin descriptions, missing alt text)\n"
        "4. Low-scoring products/collections with meaningful search demand\n\n"
        "If a data source returns nothing, say so — do not fabricate findings for it.\n\n"
        "Format your response as JSON with keys:\n"
        "- overall_assessment (string): 2-3 sentence summary of the store's SEO health\n"
        "- top_issues (array of objects): Each with {issue, severity ('critical'|'high'|'medium'|'low'), impact, action}\n"
        "- quick_wins (array of objects): Each with {action, expected_impact, effort ('low'|'medium'|'high')}\n"
        "- priority_products (array of product IDs that need the most work)\n"
        "- priority_collections (array of collection IDs that need the most work)"
    ),
    "prompts.blog_image": (
        'Create a professional blog header image for an article titled: "{article_title}".\n\n'
        "REQUIREMENTS:\n"
        "- Editorial, magazine-style photography or illustration\n"
        '- You MAY include the title text "{article_title}" in a clean, minimal font. No other text.\n'
        "- Aspect ratio: 1200x628 (landscape banner)\n\n"
        "COLOR & MOOD:\n"
        "- Use a refined, sophisticated color palette — muted tones, soft neutrals, or desaturated accents\n"
        "- Draw color inspiration from the article topic and mood\n"
        "- Keep it elegant and understated — avoid bright, saturated, or neon colors\n"
        "- Professional, editorial feel with a calm, polished aesthetic\n\n"
        "COMPOSITION:\n"
        "- Clean negative space\n"
        "- Subtle gradients or textured backgrounds\n"
        "- Professional studio-quality look\n"
    ),
    "prompts.blog_hero_image": (
        'Create a professional, high-end blog hero banner image for a blog called "{blog_title}".\n\n'
        "THIS IS A HERO BANNER — the wide image at the very top of a blog landing page. "
        "Think of premium editorial publications like Bloomberg, Monocle, or Apple Newsroom.\n\n"
        "FORMAT:\n"
        "- Wide cinematic landscape (1200\u00d7628 pixels, ~1.91:1 ratio)\n"
        "- DO NOT include any text, titles, logos, or watermarks — the image must be purely visual\n"
        "- Must look stunning as both a full-width website hero and an og:image thumbnail on social media\n\n"
        "VISUAL DIRECTION:\n"
        "- Hero-worthy composition — bold subject with clear visual hierarchy\n"
        "- Use relevant visual metaphors that represent the blog's niche and topics\n"
        "- Depth of field: sharp foreground subject, soft bokeh background\n"
        "- Golden-hour or studio-quality lighting with natural highlights and shadows\n"
        "- Perspective: slight low angle or eye-level for an immersive, editorial feel\n\n"
        "COLOR PALETTE:\n"
        "- Rich but restrained — deep neutrals, warm earth tones, or cool desaturated hues\n"
        "- Inspired by premium brand aesthetics (Aesop, Apple, Kinfolk magazine)\n"
        "- Avoid saturated primaries, neon, or flat cartoon colors\n"
        "- Subtle color grading for a cohesive, polished mood\n\n"
        "QUALITY:\n"
        "- Photorealistic or hyper-realistic editorial photography style\n"
        "- Sharp detail, no artifacts, no noise\n"
        "- Clean composition with breathing room — avoid visual clutter\n"
        "- Should feel like a $5,000 stock photo, not generic clip art\n"
    ),
    "prompts.collection_image": (
        'Create a premium e-commerce promotional banner image for a product '
        'collection called "{collection_title}". This is for the online store\'s collection page header.\n\n'
        "Use the attached product photos as reference for what to feature in the banner.\n\n"
        "REQUIREMENTS:\n"
        '- You MAY include very brief text — just the title "{collection_title}" in a clean, minimal font. No other text, taglines, prices, or paragraphs.\n'
        "- Arrange the products from the attached photos naturally and elegantly\n"
        "- Products must appear complete, not cropped or cut off at edges\n\n"
        "BACKGROUND & ENVIRONMENT (IMPORTANT — follow this closely):\n"
        "- {style_bg}\n"
        "- DO NOT use a plain white, light gray, or blank background — the scene needs depth and character\n\n"
        "COLOR & MOOD:\n"
        "- {style_mood}\n"
        "- Color palette: {style_palette}\n"
        "- Also draw color inspiration from the products themselves to create harmony\n"
        "- Subtle gradients, soft shadows, and depth-of-field blur for a premium feel\n\n"
        "COMPOSITION:\n"
        "- Clean negative space — do not overcrowd\n"
        "- Professional studio-quality lighting with soft highlights and shadows\n"
        "- Cinematic, editorial photography style — not a flat product grid\n"
        "- Consider adding subtle props or environmental elements that complement the products\n\n"
        "Style reference: premium Shopify Plus stores, Aesop campaigns, Apple product photography, Kinfolk editorial."
    ),
    "prompts.news_inline_image": (
        "Create a professional editorial in-body image for a news article.\n\n"
        "SUBJECT (what to depict): {subject}\n\n"
        "REQUIREMENTS:\n"
        "- Aspect ratio: 4:3 (will appear between paragraphs within an article body)\n"
        "- Photojournalistic / editorial style — realistic, credible, newsroom quality\n"
        "- NO text, captions, logos, watermarks, or UI elements baked into the image\n"
        "- Clear single focal point that reads well at medium size and on mobile\n\n"
        "COLOR & MOOD:\n"
        "- Natural, true-to-life color; restrained and sophisticated\n"
        "- Soft, professional lighting; avoid neon or oversaturated tones\n\n"
        "COMPOSITION:\n"
        "- Balanced, uncluttered framing with depth\n"
        "- Sharp, artifact-free, high-quality photography or illustration\n"
    ),
    "prompts.news_analyze": (
        "You are a meticulous newsroom sub-editor. You are given the raw text of a news "
        "article that the user pasted. Your job is to ENHANCE and FORMAT it for web "
        "publication — NOT to rewrite it.\n\n"
        "ABSOLUTE PRESERVATION RULES (critical):\n"
        "- Preserve every fact, name, number, date, statistic, and quotation EXACTLY.\n"
        "- Never invent facts, sources, statistics, quotes, or events.\n"
        "- Never change the meaning, claims, or the author's wording of substantive sentences.\n"
        "- You MAY fix spelling/grammar/punctuation, split run-on paragraphs, and add structure.\n"
        "- When unsure whether something is a fact vs. phrasing, keep the original wording.\n"
        "- The byline is ALWAYS 'A2Z'. Never write an author name or byline line in the body.\n\n"
        "FORMATTING:\n"
        "- Output body_html as valid HTML using <p>, <h2>, <h3>, <ul>, <li>, <strong>, <em>, "
        "<blockquote>, <a>. Do NOT include <h1> (the theme renders the headline).\n"
        "- Add a logical heading structure (<h2>/<h3>) and a strong lead paragraph.\n"
        "- Convert prominent direct quotes to <blockquote> where natural.\n"
        "- Insert inline image placeholder tokens on their OWN line between paragraphs, "
        "formatted exactly as [[INLINE_IMAGE_1]], [[INLINE_IMAGE_2]], etc. Never put a token "
        "inside a <p>. Plan 1-3 inline images at natural breaks (in addition to the hero).\n"
        "- Add internal links where a provided product/collection/news item is genuinely "
        "relevant, using relative URLs (/products/handle, /collections/handle, /blogs/news/handle). "
        "Use descriptive anchor text. Do not force links.\n\n"
        "CATEGORY: choose exactly ONE category for this story from the provided allowed list. "
        "If none fit, choose the closest.\n\n"
        "Return ONLY a JSON object (no code fences) with these keys:\n"
        "- headline: string, <=110 chars, factual, no clickbait\n"
        "- kicker: short overline (2-4 words)\n"
        "- category: one value from the allowed category list\n"
        "- summary: 50-160 char dek/excerpt\n"
        "- meta_title: 40-60 chars\n"
        "- meta_description: 140-160 chars\n"
        "- tags: array of strings; MUST include 'type:news' plus 3-6 topical tags\n"
        "- body_html: the formatted HTML with [[INLINE_IMAGE_n]] tokens, no <h1>, no byline\n"
        "- hero_image_prompt: a vivid editorial image prompt for the hero image\n"
        "- hero_image_alt: <=125 char descriptive alt text for the hero\n"
        "- inline_images: array of {token, prompt, alt, caption} — one per [[INLINE_IMAGE_n]] token\n"
        "- internal_links: array of {anchor, url, reason}\n"
        "- related_news: array of {shopify_id, title, handle, reason} chosen from the provided candidates\n"
        "- preservation_notes: brief note on what was formatting-only vs preserved\n"
    ),
    "prompts.news_links": (
        "You are an SEO internal-linking specialist. Your ONLY task is to add relevant "
        "internal links into an existing, finished news article's HTML body.\n\n"
        "CRITICAL RULES:\n"
        "- PRESERVE the existing structure, headings, paragraphs, wording, and meaning EXACTLY.\n"
        "- Do NOT rewrite, summarize, add, or remove any content or sentences.\n"
        "- PRESERVE every [[INLINE_IMAGE_n]] placeholder token EXACTLY where it is — never move, "
        "alter, wrap, or delete these tokens.\n"
        "- ONLY add <a href> links to the products, collections, and articles listed below.\n"
        "- Use relative URLs exactly as given (/products/handle, /collections/handle, "
        "/blogs/{blog_handle}/{article_handle}).\n"
        "- Add 4-12 links total, spread naturally across the body — do not over-link.\n"
        "- Do NOT add links inside headings (h2, h3) or inside existing links.\n"
        "- Only link a destination when it is genuinely relevant to the surrounding sentence. "
        "If nothing relevant exists nearby, do not force a link.\n"
        "- Keep all existing valid links intact.\n"
        "- Do NOT wrap output in code fences or JSON; return raw HTML only.\n"
        "- Do NOT include notes, counts, or any commentary.\n\n"
        "ANCHOR TEXT RULES (CRITICAL FOR SEO):\n"
        "- Anchor text MUST match or closely reflect the destination page's title/keywords.\n"
        "- Use the product/collection/article name as the anchor; never 'click here' or 'read more'.\n"
        "- If a destination's name already appears in the text, link that existing mention.\n"
    ),

    # --- Blog Settings ---
    "blog.search_keywords": "blog tips deals",

    # --- SEO Scoring Rules ---
    "rules.composite_scoring": {
        "weights_with_search": {"on_page": 0.40, "search": 0.60},
        "weights_gsc_only": {"on_page": 0.50, "search": 0.50},
        "search_weights": {
            "search_visibility": 0.30,
            "click_performance": 0.30,
            "keyword_coverage": 0.20,
            "ranking_strength": 0.20,
        },
    },
    "rules.product_seo": {
        "weights": {
            "meta_title": 0.30,
            "meta_description": 0.30,
            "description": 0.25,
            "image_alt": 0.15,
        },
        "thresholds": {
            "meta_title": {"good": [50, 60], "ok": [30, 70], "poor_min": 1},
            "meta_description": {"good": [140, 160], "ok": [100, 200], "poor_min": 1},
            "description_words": {"good": 300, "ok": 150, "short": 50, "very_short": 1},
        },
    },
    "prompts.ai_seo_product": (
        "You are a GEO (Generative Engine Optimization) copywriter. Your job is to ENHANCE existing "
        "ecommerce PRODUCT content — not replace it — so it is maximally likely to be cited and "
        "recommended by AI answer engines (ChatGPT, Perplexity, Gemini, Google AI Overviews, Claude), "
        "WITHOUT losing any existing search ranking.\n\n"
        "PRESERVE EXISTING SEO (highest priority): Keep every existing keyword, product name, brand "
        "term and key phrase that carries SEO value. If a MUST-PRESERVE KEYWORDS list is provided, "
        "every one of those terms MUST still appear verbatim in your output (in the meta and/or body). "
        "Retain the factual content and messaging of the current description — build the improvements "
        "INTO and AROUND it rather than discarding it.\n"
        "META: If a meta title/description already exists, keep its keywords and core message; you may "
        "refine wording for AI-extractability but must retain its primary keywords and stay <=60 chars "
        "(title) / <=160 chars (description). Only write a fresh meta when the field is empty.\n\n"
        "Then apply the empirically-validated GEO levers:\n"
        "1. STATISTICS — weave in verifiable numbers, specs, measurements and quantities.\n"
        "2. QUOTATIONS — include 1-3 short authoritative quotes with clear attribution.\n"
        "3. CITED SOURCES — reference reputable sources where a claim benefits from evidence.\n"
        "4. EXTRACTABLE ANSWERS — use question-style H2 headings and lead each section with a "
        "concise, direct answer (a quotable 1-2 sentence summary) before elaborating.\n"
        "5. STRUCTURE — use lists and comparison tables where helpful; keep language fluent and "
        "authoritative.\n"
        "6. SPECIFICATIONS TABLE — when VERIFIED PRODUCT FACTS are provided, render an HTML <table> "
        "titled 'Specifications' (or 'At a glance') listing those facts (brand, type, options/flavors, "
        "pack/size, price, availability, SKU/GTIN, and any spec fields). Include ONLY the provided "
        "facts; never invent spec rows or values.\n"
        "7. EXPERT VERDICT — add a short 'Expert Verdict' section (2-3 sentences, an extractable "
        "summary): who this product is best for, its standout qualities, and a quick honest take, "
        "grounded in the facts. No hype.\n\n"
        "INTEGRITY (critical): NEVER fabricate specifications, prices, certifications, statistics, "
        "studies, organizations, or quotes/attributions. Only include a statistic, quote or cited "
        "source if it is genuinely verifiable or derivable from the product data you are given. If you "
        "do not have a real number or a real attributable source, express the point qualitatively "
        "instead of inventing one — an honest page with fewer hard figures beats a fabricated one. "
        "Do not invent named experts, associations, or review counts. Stay accurate to the product and "
        "brand; keep the same product identity; write clean semantic HTML "
        "(h2/h3/p/ul/li/table/blockquote) with NO inline styles.\n\n"
        "Also produce a meta title (<=60 chars), a meta description (<=160 chars), and 3-5 FAQ pairs "
        "whose answers are self-contained and directly extractable.\n\n"
        "Respond with ONLY a JSON object: {\"optimized_description_html\": string (MUST include the "
        "Specifications table and the Expert Verdict section), \"faq\": [{\"question\": string, "
        "\"answer\": string}], \"meta_title\": string, \"meta_description\": string, "
        "\"expert_verdict\": string, \"geo_edits_applied\": [string]}."
    ),
    "prompts.ai_seo_collection": (
        "You are a GEO (Generative Engine Optimization) copywriter. Your job is to ENHANCE existing "
        "ecommerce COLLECTION / category content — not replace it — so it is maximally likely to be "
        "cited and recommended by AI answer engines (ChatGPT, Perplexity, Gemini, Google AI Overviews, "
        "Claude), WITHOUT losing any existing search ranking.\n\n"
        "PRESERVE EXISTING SEO (highest priority): Keep every existing keyword, category term and brand "
        "name that carries SEO value. If a MUST-PRESERVE KEYWORDS list is provided, every one of those "
        "terms MUST still appear verbatim in your output (meta and/or body). Retain the messaging of the "
        "current description and build improvements INTO and AROUND it.\n"
        "META: If a meta title/description already exists, keep its keywords and core message; you may "
        "refine wording but must retain its primary keywords and stay <=60 chars (title) / <=160 chars "
        "(description). Only write a fresh meta when the field is empty.\n\n"
        "Frame the collection as an authoritative buyer's guide. Apply the validated GEO levers:\n"
        "1. STATISTICS — include verifiable numbers about the category (ranges, typical specs, counts).\n"
        "2. QUOTATIONS — include 1-3 short authoritative quotes with attribution where natural.\n"
        "3. CITED SOURCES — reference reputable sources for evidence-based claims.\n"
        "4. EXTRACTABLE ANSWERS — use question-style H2 headings (e.g. 'How to choose ...') and lead "
        "each section with a concise direct answer before elaborating.\n"
        "5. STRUCTURE — include a comparison table or buying-criteria list; fluent, authoritative tone.\n"
        "6. AT-A-GLANCE TABLE — when VERIFIED FACTS are provided, render an HTML <table> summarising the "
        "category (e.g. brands/options available, price range, key attributes). Use ONLY provided facts.\n"
        "7. COMPARISON MATRIX — when FEATURED PRODUCTS are provided, render a second HTML <table> that "
        "compares them side by side (columns: Product, Brand, Price, Availability). Include one row per "
        "provided product using ONLY those exact values; never add products, prices, or attributes that "
        "were not provided. AI engines extract tabular comparisons far more reliably than prose.\n"
        "8. EXPERT VERDICT — add a short 'Expert Verdict' section (2-3 sentences, extractable): who this "
        "category is best for and how to choose, grounded in the facts. No hype.\n\n"
        "INTEGRITY (critical): NEVER fabricate statistics, studies, organizations, named experts, "
        "review counts, or quotes/attributions. Only include a statistic, quote or cited source if it "
        "is genuinely verifiable; otherwise express the point qualitatively rather than inventing one. "
        "Stay accurate to the category and brand; write clean semantic HTML "
        "(h2/h3/p/ul/li/table/blockquote) with NO inline styles.\n\n"
        "Also produce a meta title (<=60 chars), a meta description (<=160 chars), and 3-5 FAQ pairs "
        "with self-contained, directly extractable answers.\n\n"
        "Respond with ONLY a JSON object: {\"optimized_description_html\": string (MUST include the "
        "at-a-glance table, the comparison matrix when FEATURED PRODUCTS are provided, and the Expert "
        "Verdict section), \"faq\": [{\"question\": string, \"answer\": string}], \"meta_title\": string, "
        "\"meta_description\": string, \"expert_verdict\": string, \"geo_edits_applied\": [string]}."
    ),
    "prompts.ai_seo_facts": (
        "You are a fact-enrichment specialist for ecommerce content. Your ONLY job is to raise the "
        "FACT DENSITY of an existing product/category description by weaving in ADDITIONAL verifiable "
        "facts — statistics, specifications, measurements, quantities, counts, dates — that are "
        "genuinely and specifically about THIS exact item.\n\n"
        "PRIORITISE QUANTITATIVE facts expressed with numbers and units — dimensions, length, "
        "ring gauge, weight, pack/count, percentages, nicotine/tar, dates, aging time, ratings — "
        "because measurable data carries the most weight for AI. Include qualitative specs (origin, "
        "wrapper/leaf type, material) as secondary, but lead with the numbers.\n\n"
        "HARD RULES:\n"
        "- Use ONLY facts from the VERIFIED PRODUCT FACTS (the store's own data) and facts you are "
        "CONFIDENT are accurate and specifically about this item from the WEB RESEARCH CONTEXT.\n"
        "- NEVER fabricate or guess numbers, specs, studies, certifications, or sources. If unsure "
        "whether a researched detail refers to THIS exact product/variant, DO NOT use it.\n"
        "- PRESERVE everything: keep all existing wording, keywords, headings, tables, FAQ and "
        "structure intact. You are ADDING factual detail into and around the existing copy (extending "
        "sentences with specifics, adding a spec-table row, adding a verified statistic) — not rewriting.\n"
        "- Quality over quantity: a couple of real, relevant facts beats many vague ones.\n\n"
        "IF you found genuinely relevant, verifiable facts to add, respond with ONLY: "
        "{\"optimized_description_html\": string (the full description with the added facts woven in), "
        "\"facts_added\": [string], \"sources\": [string]}.\n"
        "IF you cannot find any genuinely relevant, verifiable fact to add, make NO changes and respond "
        "with ONLY: {\"no_change\": true, \"reason\": string}."
    ),
    "rules.ai_seo": {
        "weights": {
            "structured_data": 0.22,
            "extractability": 0.20,
            "fact_density": 0.18,
            "crawlability": 0.15,
            "entity_authority": 0.15,
            "freshness": 0.10,
        },
        "thresholds": {
            "fact_density_per_100w": {"good": 6, "ok": 3, "min": 1},
            "freshness_days": {"fresh": 90, "aging": 365},
            "desired_schema": {
                "product": ["Product", "BreadcrumbList", "FAQPage", "Organization"],
                "collection": [
                    "CollectionPage",
                    "BreadcrumbList",
                    "FAQPage",
                    "Organization",
                ],
            },
            "schema_richness": {
                "product": ["offers", "brand", "image", "identifier", "aggregateRating", "additionalProperty"],
                "collection": ["description", "image", "isPartOf"],
            },
        },
    },
    "rules.collection_seo": {
        "weights": {
            "meta_title": 0.35,
            "meta_description": 0.35,
            "description": 0.30,
        },
        "thresholds": {
            "meta_title": {"good": [50, 60], "ok": [30, 70], "poor_min": 1},
            "meta_description": {"good": [140, 160], "ok": [100, 200], "poor_min": 1},
            "description_words": {"good": 150, "ok": 75, "short": 1},
        },
    },
    "rules.article_seo": {
        "weights": {
            "meta_title": 0.15,
            "meta_description": 0.15,
            "word_count": 0.25,
            "headings": 0.15,
            "internal_links": 0.15,
            "image": 0.10,
            "summary": 0.05,
        },
        "thresholds": {
            "meta_title": {"good": [35, 46], "ok": [25, 55], "poor_min": 1},
            "meta_description": {"good": [140, 160], "ok": [100, 200], "poor_min": 1},
            "word_count": {"excellent": 1500, "good": 1000, "short": 500, "very_short": 1},
            "headings": {"good_h2": 2, "good_h3": 1},
            "internal_links": {"good": 3, "ok": 1},
            "summary": {"good": [50, 160]},
        },
    },
    "rules.technical_product": {
        "weights": {
            "title_has_keyword": 0.10,
            "keyword_at_title_start": 0.05,
            "meta_title_has_keyword": 0.08,
            "meta_title_length": 0.07,
            "meta_desc_has_keyword": 0.08,
            "meta_desc_length": 0.07,
            "url_has_keyword": 0.06,
            "body_first_100_has_keyword": 0.08,
            "keyword_density": 0.06,
            "description_word_count": 0.10,
            "heading_structure": 0.05,
            "image_alt_coverage": 0.10,
            "image_alt_has_keyword": 0.10,
        },
        "thresholds": {
            "meta_title_length": {"good": [50, 60], "ok": [40, 70], "poor_min": 1},
            "meta_desc_length": {"good": [140, 160], "ok": [100, 200], "poor_min": 1},
            "keyword_density": {"good": [0.5, 3.0], "ok_low": 0.1, "ok_high": 5.0},
            "description_words": {"good": 300, "ok": 150, "short": 50, "very_short": 1},
            "keyword_at_title_start": 3,
        },
    },
    "rules.technical_collection": {
        "weights": {
            "title_has_keyword": 0.12,
            "keyword_at_title_start": 0.06,
            "meta_title_has_keyword": 0.10,
            "meta_title_length": 0.08,
            "meta_desc_has_keyword": 0.10,
            "meta_desc_length": 0.08,
            "url_has_keyword": 0.06,
            "body_first_100_has_keyword": 0.10,
            "keyword_density": 0.06,
            "description_word_count": 0.10,
            "heading_structure": 0.06,
            "image_alt_coverage": 0.08,
        },
        "thresholds": {
            "meta_title_length": {"good": [50, 60], "ok": [40, 70], "poor_min": 1},
            "meta_desc_length": {"good": [140, 160], "ok": [100, 200], "poor_min": 1},
            "keyword_density": {"good": [0.5, 3.0], "ok_low": 0.1, "ok_high": 5.0},
            "description_words": {"good": 150, "ok": 75, "short": 25, "very_short": 1},
        },
    },
    "rules.interlinking": {
        "score_thresholds": {
            "incoming_good": 3,
            "incoming_ok": 1,
            "in_collection_score": 30,
        },
        "crosslink_thresholds": {
            "shared_tags": 2,
            "shared_products": 3,
        },
        "distribution": {
            "excellent": 80,
            "good": 50,
            "poor": 1,
        },
    },
    "rules.landing_page_seo": {
        "weights": {
            "meta_title": 0.15,
            "meta_description": 0.15,
            "h1_tag": 0.10,
            "heading_hierarchy": 0.10,
            "content_length": 0.10,
            "image_alt_text": 0.10,
            "internal_links": 0.10,
            "content_structure": 0.05,
            "cta_present": 0.05,
            "mobile_friendly": 0.05,
            "schema_markup": 0.05,
        },
        "thresholds": {
            "meta_title": {"good": [50, 60], "ok": [35, 70]},
            "meta_description": {"good": [140, 160], "ok": [100, 200]},
            "content_length": {"good": 300, "ok": 150},
            "internal_links": {"good": 3, "ok": 1},
        },
    },

    # --- Landing Page Prompts ---
    "prompts.landing_page_seo": (
        "You are an expert Shopify SEO consultant specializing in landing page optimization. "
        "Analyze the page data and provide actionable SEO recommendations.\n\n"
        "SEO ANALYSIS RULES:\n"
        "- Keyword density: primary keyword should appear 1-2% of total word count\n"
        "- Meta title: 50-60 characters, front-load the primary keyword, include brand name\n"
        "- Meta description: 140-160 characters, include primary keyword and a clear CTA\n"
        "- Heading structure: exactly ONE H1 tag, at least 2 H2 tags, logical hierarchy (no skipping levels)\n"
        "- Content length: minimum 500 words for main content sections combined\n"
        "- Internal linking: 3-5 internal links with descriptive anchor text\n"
        "  Format anchor text suggestions as {text, url, reason}\n"
        "- CTA placement: every page MUST have a CTA above the fold AND near the bottom\n"
        "- Image optimization: all images need descriptive alt text with keyword inclusion\n"
        "- Schema markup: recommend WebPage or FAQPage schema if applicable\n"
        "- Mobile-friendliness: check for responsive section structures\n\n"
        "Format your final response as JSON with these keys:\n"
        "- optimized_meta_title (string): SEO-optimized meta title, 50-60 chars\n"
        "- optimized_meta_description (string): SEO-optimized meta description, 140-160 chars\n"
        "- content_recommendations (array of strings): Specific content improvements\n"
        "- section_improvements (array of objects): Each with {section_id, suggestions: [string]}\n"
        "- missing_elements (array of strings): Elements that should be added (e.g., FAQ section, schema markup)\n"
        "- internal_linking_opportunities (array of objects): Each with {text, url, reason}\n"
        "- keyword_analysis (object): {primary_keyword, current_density, target_density, secondary_keywords}\n"
        "- heading_structure (object): {current_h1_count, current_h2_count, recommendations: [string]}\n"
        "- cta_analysis (object): {has_above_fold, has_bottom, recommendations: [string]}\n"
        "- summary (string): Brief summary of all recommendations"
    ),
    "prompts.landing_page_generate": (
        "You are an expert landing page builder specializing in high-converting, SEO-optimized Shopify pages. "
        "Given a topic and keywords, generate a complete set of page sections.\n\n"
        "PAGE STRUCTURE RULES:\n"
        "- MUST include a hero section as the first section with a compelling headline\n"
        "- MUST include at least one cta_banner section (ideally one mid-page and one at the bottom)\n"
        "- Generate 5-8 sections for a comprehensive page\n"
        "- Recommended order: hero → features/trust_badges → rich_text (main content) → "
        "product_showcase → testimonials → faq → cta_banner\n\n"
        "CONTENT RULES:\n"
        "- Hero: headline should include the primary keyword, subheadline supports it with a benefit\n"
        "- Rich text sections: minimum 150 words each, use proper heading hierarchy\n"
        "- FAQ: include 4-6 questions that target long-tail keyword variations\n"
        "- Features/Trust badges: minimum 3 items each\n"
        "- Testimonials: include 3 realistic, detailed testimonials with roles\n"
        "- Statistics: use specific, credible numbers\n\n"
        "SEO RULES:\n"
        "- meta_title: 50-60 characters, primary keyword front-loaded\n"
        "- meta_description: 140-160 characters, include CTA language\n"
        "- Primary keyword appears in hero headline, at least one H2, and in the first rich_text paragraph\n"
        "- Include internal links in rich_text content using /collections/handle and /products/handle URLs\n"
        "- Plan 3-5 internal links distributed across rich_text sections\n\n"
        "IMAGE RULES:\n"
        "- Hero section: set image_url to '' (will be generated separately) but provide descriptive image_alt\n"
        "- Product showcase: use actual product handles from the available products list\n\n"
        "Return JSON with these keys:\n"
        "- title (string): Page title\n"
        "- handle (string): URL handle (kebab-case)\n"
        "- meta_title (string): SEO meta title, 50-60 chars\n"
        "- meta_description (string): SEO meta description, 140-160 chars\n"
        "- sections (array): Each section has {type, order, data}. Available types:\n"
        "  hero, features, product_showcase, testimonials, faq, cta_banner, trust_badges, rich_text, image_gallery, statistics\n"
        "  Data shape per type:\n"
        "  - hero: {headline, subheadline, cta_text, cta_url, image_url, image_alt}\n"
        "  - features: {heading, items: [{icon, title, description}]}\n"
        "  - product_showcase: {heading, products: [{title, handle, image_url, price}]}\n"
        "  - testimonials: {heading, items: [{quote, author, role}]}\n"
        "  - faq: {heading, items: [{question, answer}]}\n"
        "  - cta_banner: {heading, subtext, cta_text, cta_url}\n"
        "  - trust_badges: {heading, items: [{icon, text}]}\n"
        "  - rich_text: {heading, content} — content is HTML with proper h2/h3/p tags\n"
        "  - statistics: {heading, items: [{value, label}]}"
    ),
    "prompts.home_page_generate": (
        "You are an expert Shopify SEO strategist building the STORE HOME PAGE hero carousel. "
        "Given a store, a topic/theme, and target keywords, produce an above-the-fold image "
        "carousel plus home-page meta tags that will rank and convert.\n\n"
        "STRUCTURE RULES:\n"
        "- Generate 3-5 slides. The FIRST slide is the most important — its headline becomes the "
        "page's single H1 and MUST contain the primary keyword and the store's core value proposition.\n"
        "- Remaining slide headlines are H2-level; use them for secondary keywords, seasonal offers, "
        "or top category promotions.\n"
        "- Each slide needs a clear, action-oriented CTA (e.g. 'Shop the Collection', 'Browse Deals').\n\n"
        "LINKING RULES (CRITICAL):\n"
        "- Every cta_url MUST point to a REAL destination from the AVAILABLE COLLECTIONS list "
        "(format /collections/handle) or a REAL product (/products/handle). NEVER invent handles.\n"
        "- If no suitable collection exists, use /collections/all.\n\n"
        "SEO RULES:\n"
        "- meta_title: 50-60 characters, primary keyword front-loaded, include the brand/store name.\n"
        "- meta_description: 140-160 characters, include the primary keyword and a CTA.\n"
        "- Use the live SERP benchmark data (if provided) to beat competitor titles/descriptions.\n\n"
        "IMAGE RULES:\n"
        "- For each slide, provide an 'image_prompt': a vivid description of a premium, TEXT-FREE "
        "hero background showcasing the store's real products/category (images are generated later).\n"
        "- Provide descriptive 'image_alt' (with keyword) for each slide.\n\n"
        "Return ONLY JSON with these keys:\n"
        "- meta_title (string)\n"
        "- meta_description (string)\n"
        "- slides (array of 3-5 objects), each: {headline, subheadline, image_prompt, image_alt, cta_text, cta_url}"
    ),
    "prompts.landing_page_rewrite": (
        "You are an expert SEO copywriter specializing in Shopify page optimization. "
        "Rewrite the provided page HTML to dramatically improve SEO performance.\n\n"
        "HEADING RULES:\n"
        "- Ensure exactly ONE H1 tag at the top of the content\n"
        "- Use 2-4 H2 tags for main sections, H3 for subsections\n"
        "- Never skip heading levels (no H1 → H3)\n"
        "- Include the primary keyword in the H1 and at least one H2\n\n"
        "CONTENT RULES:\n"
        "- Minimum 500 words total content\n"
        "- Primary keyword density: 1-2% of total word count\n"
        "- Use secondary keywords naturally throughout\n"
        "- Write in short, scannable paragraphs (2-3 sentences each)\n"
        "- Add bullet points or numbered lists where appropriate\n\n"
        "INTERNAL LINKING:\n"
        "- Inject 3-5 internal links using the provided collection/product URLs\n"
        "- Use descriptive anchor text (not 'click here' or 'learn more')\n"
        "- Distribute links naturally throughout the content\n"
        "- Format: <a href=\"/collections/handle\">descriptive text</a>\n\n"
        "CTA RULES:\n"
        "- Add a CTA element near the top of the content (above the fold)\n"
        "- Add a CTA element near the bottom\n"
        "- Use action-oriented language ('Shop Now', 'Browse Collection', 'Get Started')\n\n"
        "IMAGE RULES:\n"
        "- Add descriptive alt text to any images that lack it\n"
        "- Alt text should include the primary keyword naturally\n\n"
        "SECURITY:\n"
        "- Strip any <script> tags from the content\n\n"
        "Return raw HTML only, no code fences, no commentary, no markdown."
    ),
    "prompts.landing_hero_image": (
        "Create a premium e-commerce hero banner BACKGROUND image for '{page_title}'.\n\n"
        "CRITICAL — NO TEXT:\n"
        "- Do NOT include ANY text, titles, logos, labels, watermarks, or typography\n"
        "- Zero words, zero letters — white text will be overlaid via HTML/CSS later\n\n"
        "WHAT TO SHOW:\n"
        "- A beautifully styled hero-worthy product arrangement using the store's REAL products\n"
        "- Use the attached reference product photos to match REAL packaging, labels, and colors\n"
        "- Select 3-5 products from different categories for variety\n"
        "- Arrange them in a clean, editorial-style composition — NOT a catalog grid or shelf dump\n"
        "- Products should be recognizable but artfully placed with space between them\n\n"
        "COMPOSITION & LAYOUT:\n"
        "- Products arranged across the LOWER THIRD of the frame\n"
        "- The UPPER TWO-THIRDS should be mostly open dark space for text overlay\n"
        "- Think premium brand campaign: products sit on a beautiful dark surface\n"
        "  (dark wood, matte black, dark marble, slate) with the background fading\n"
        "  to darkness above them\n"
        "- Products should be slightly angled, overlapping naturally — like an\n"
        "  art director placed them for a magazine shoot\n"
        "- Generous negative space overall — the image should feel spacious\n\n"
        "LIGHTING & MOOD:\n"
        "- Dramatic studio lighting from above/side — warm golden or amber tones\n"
        "- Dark moody backdrop — the top half should be very dark (near black)\n"
        "  so white text is perfectly readable when overlaid\n"
        "- Subtle rim light on product edges for separation\n"
        "- Soft shadows, no harsh reflections\n"
        "- Optional: gentle smoke wisps or warm bokeh in the dark background area\n\n"
        "FORMAT:\n"
        "- Wide cinematic landscape, 16:9 aspect ratio\n"
        "- Dark overall — upper portion near-black for text, lower portion lit for products\n\n"
        "QUALITY:\n"
        "- Photorealistic high-end product photography with cinematic flair\n"
        "- Sharp product detail, professional lighting, rich textures\n"
        "- Clean, spacious, premium — like a luxury brand homepage banner\n"
        "- NO clutter, NO crowded arrangements, NO flat-lay catalogs"
    ),

    # --- Word Lists ---
    "lists.stop_words": [
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "it", "as", "be", "was", "are",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can", "need",
        "dare", "ought", "used", "this", "that", "these", "those", "i", "me",
        "my", "we", "our", "you", "your", "he", "him", "his", "she", "her",
        "its", "they", "them", "their", "what", "which", "who", "whom",
        "where", "when", "why", "how", "all", "each", "every", "both",
        "few", "more", "most", "other", "some", "such", "no", "nor", "not",
        "only", "own", "same", "so", "than", "too", "very", "just", "about",
        "above", "after", "again", "also", "any", "because", "before",
        "below", "between", "during", "here", "into", "if", "now",
        "off", "once", "out", "over", "then", "there", "through", "under",
        "up", "well", "were", "get", "got",
        "like", "really", "however", "therefore", "much", "many",
        "make", "made", "going", "thing", "things",
    ],
    "lists.ngram_multipliers": {"1": 1.0, "2": 1.5, "3": 2.0},
    "lists.news_categories": [
        "Company News",
        "Product News",
        "Industry News",
        "Announcements",
        "Events",
        "Press Releases",
        "Guides & Tips",
    ],
    "lists.article_title_styles": [
        {
            "style": "question",
            "instruction": "Frame the title as a compelling question the reader wants answered.",
            "examples": ["What Makes Pipe Tobacco Different From Cigarette Tobacco?", "Is Grabba Leaf Worth the Hype? Here's What Smokers Say"],
        },
        {
            "style": "how-to",
            "instruction": "Frame as a practical how-to that promises a clear outcome.",
            "examples": ["How to Pick the Right Cigarillo for Your Taste", "How to Roll With Grabba Leaf Like a Pro"],
        },
        {
            "style": "listicle",
            "instruction": "Use a specific number to set expectations. Odd numbers perform best.",
            "examples": ["7 Loose Leaf Wraps Every Smoker Should Try", "5 Black and Mild Flavors Ranked by Smoothness"],
        },
        {
            "style": "insider-take",
            "instruction": "Write from a knowledgeable, opinionated perspective — like a friend giving advice.",
            "examples": ["The Real Difference Between Grabba and Fronto Leaf", "What Nobody Tells You About Nicotine Pouches"],
        },
        {
            "style": "comparison",
            "instruction": "Compare two or more things the audience cares about.",
            "examples": ["Grabba vs Fronto: Which Tobacco Leaf Is Right for You?", "Zyns vs On! Pouches: An Honest Side-by-Side"],
        },
        {
            "style": "year-specific",
            "instruction": "Include the current year to signal freshness. Pair with a bold claim or ranking.",
            "examples": ["The Best Pipe Tobacco Blends to Try in 2026", "2026's Most Popular Cigarillo Flavors, Ranked"],
        },
        {
            "style": "myth-busting",
            "instruction": "Challenge a common misconception or set the record straight.",
            "examples": ["Stop Believing These 5 Myths About Loose Leaf Wraps", "Think All Grabba Leaf Is the Same? Think Again"],
        },
        {
            "style": "beginner-friendly",
            "instruction": "Signal accessibility — make newcomers feel welcome without using 'Ultimate Guide'.",
            "examples": ["New to Pipe Tobacco? Start Here", "A Smoker's First Look at Nicotine Pouches"],
        },
    ],
    "lists.collection_image_styles": [
        {
            "bg": "deep charcoal or matte black surface with dramatic side-lighting casting long shadows",
            "mood": "moody, luxurious, high-contrast — think premium whiskey or watch advertisements",
            "palette": "rich darks with warm amber or copper accent lighting",
        },
        {
            "bg": "weathered wood tabletop or rustic wooden surface with natural grain texture",
            "mood": "artisanal, warm, handcrafted — like a specialty shop or craft brand",
            "palette": "warm browns, honey tones, and natural earth colors",
        },
        {
            "bg": "deep navy or midnight blue backdrop with soft golden hour rim lighting",
            "mood": "bold, confident, elevated — think premium retail brand campaigns",
            "palette": "deep blues contrasted with warm gold or bronze highlights",
        },
        {
            "bg": "natural linen or textured fabric surface with soft overhead diffused light",
            "mood": "organic, tactile, boutique — like Kinfolk magazine or Aesop campaigns",
            "palette": "warm creams, oatmeal, and terracotta accents",
        },
        {
            "bg": "concrete or slate surface with an overhead flat-lay arrangement and strong directional light",
            "mood": "urban, modern, industrial-chic — like a design studio showcase",
            "palette": "cool grays with teal or sage green accent elements",
        },
        {
            "bg": "rich emerald green or forest green environment with botanical elements like leaves or branches",
            "mood": "fresh, natural, vibrant — like a premium organic or wellness brand",
            "palette": "deep greens with natural wood and cream contrast",
        },
        {
            "bg": "warm terracotta or burnt sienna backdrop with Moroccan or Mediterranean vibes",
            "mood": "warm, inviting, globally inspired — like a curated import shop",
            "palette": "warm earth tones: terracotta, rust, sandy beige, olive",
        },
        {
            "bg": "gradient backdrop transitioning from deep plum to dusty rose with soft studio lighting",
            "mood": "contemporary, editorial, fashion-forward — like a luxury lookbook",
            "palette": "rich berry tones with soft pink and mauve accents",
        },
    ],
    # --- Cannibalization Fix Prompts ---
    "prompts.cannibalization_analysis": (
        "You are an SEO expert analyzing keyword cannibalization on a Shopify ecommerce store.\n\n"
        "QUERY: \"{query}\"\n\n"
        "The following pages all rank for the same query, diluting ranking power:\n\n"
        "{pages_description}\n\n"
        "Analyze this cannibalization issue and provide:\n"
        "1. **winner**: Which page should be the PRIMARY page for this query and why\n"
        "2. **severity_reason**: Why this cannibalization is harmful\n"
        "3. **recommendations**: For EACH competing page (not the winner), give a specific action:\n"
        "   - \"redirect\" — 301 redirect this page to the winner (for duplicate/thin pages)\n"
        "   - \"differentiate\" — change this page's targeting to a different keyword (suggest which keyword)\n"
        "   - \"de-optimize\" — remove the query keyword from this page's title/meta to stop it competing\n"
        "   - \"merge\" — combine this page's unique content into the winner, then redirect\n"
        "   - \"keep\" — this page legitimately serves a different intent, no action needed\n\n"
        "Return JSON only:\n"
        "{{\n"
        "  \"winner_page\": \"URL\",\n"
        "  \"winner_reason\": \"explanation\",\n"
        "  \"severity_reason\": \"impact analysis\",\n"
        "  \"recommendations\": [\n"
        "    {{\n"
        "      \"page\": \"URL\",\n"
        "      \"matched_title\": \"product/collection name\",\n"
        "      \"action\": \"redirect|differentiate|de-optimize|merge|keep\",\n"
        "      \"reason\": \"specific explanation\",\n"
        "      \"suggested_keyword\": \"only if action is differentiate\"\n"
        "    }}\n"
        "  ]\n"
        "}}"
    ),
    "prompts.cannibalization_fix": (
        "You are an SEO expert rewriting metadata for a Shopify {item_type} to fix keyword cannibalization.\n\n"
        "CURRENT STATE:\n"
        "- Title: {current_title}\n"
        "- Meta Title: {current_meta_title}\n"
        "- Meta Description: {current_meta_description}\n"
        "- URL Handle: {handle}\n"
        "{images_context}\n\n"
        "CANNIBALIZATION CONTEXT:\n"
        "- This page is competing with other pages for the query: \"{competing_query}\"\n"
        "- AI recommended action: {action}\n"
        "- Reason: {reason}\n"
        "- Target keyword to optimize for instead: \"{target_keyword}\"\n\n"
        "INSTRUCTIONS:\n"
        "- Rewrite the meta title (50-60 chars) to target \"{target_keyword}\" instead of \"{competing_query}\"\n"
        "- Rewrite the meta description (145-160 chars) to naturally include \"{target_keyword}\"\n"
        "- If the action is \"de-optimize\", remove or significantly reduce mentions of \"{competing_query}\" in the title and meta\n"
        "- If the action is \"differentiate\", shift focus entirely to \"{target_keyword}\"\n"
        "- Keep the tone professional and conversion-focused for an ecommerce store\n"
        "- Do NOT use puffery words (premium, best, top, ultimate, finest)\n"
        "- The meta title should be compelling and click-worthy\n"
        "- If image alt texts are provided, rewrite any that contain \"{competing_query}\" to use \"{target_keyword}\" instead\n"
        "  * Keep alt text under 125 chars, natural and descriptive\n"
        "  * Only include images that need changes in the response array\n"
        "  * If no images need changes, omit the image_alt_suggestions field\n\n"
        "Return JSON only:\n"
        "{{\n"
        "  \"new_title\": \"rewritten product/collection title (or null if no change needed)\",\n"
        "  \"new_meta_title\": \"rewritten meta title, 50-60 chars\",\n"
        "  \"new_meta_description\": \"rewritten meta description, 145-160 chars\",\n"
        "  \"image_alt_suggestions\": [{{\"image_index\": 0, \"new_alt\": \"rewritten alt text\"}}],\n"
        "  \"explanation\": \"brief explanation of what was changed and why\"\n"
        "}}"
    ),
    # --- Phase 4: Content Gap & Competitor Prompts ---
    "prompts.content_brief_generation": (
        "You are an expert SEO content strategist creating a content brief for an ecommerce store.\n\n"
        "TARGET KEYWORD: \"{keyword}\"\n"
        "Monthly Search Volume: {volume}\n"
        "CPC: ${cpc}\n"
        "Search Intent: {intent}\n"
        "Current Position: {our_position}\n"
        "Gap Type: {gap_type}\n"
        "Suggested Content Type: {content_type}\n\n"
        "COMPETITOR DATA:\n{competitor_info}\n\n"
        "RELATED KEYWORDS (use as secondary keywords):\n{related_keywords}\n\n"
        "AVAILABLE INTERNAL LINKS:\n{internal_links}\n\n"
        "Create a comprehensive content brief. Return JSON only:\n"
        "{{\n"
        "  \"title_options\": [\"3 compelling title options, 50-60 chars each\"],\n"
        "  \"meta_description\": \"145-160 char meta description\",\n"
        "  \"slug\": \"url-slug-suggestion\",\n"
        "  \"target_word_count\": 1500,\n"
        "  \"primary_keyword\": \"{keyword}\",\n"
        "  \"secondary_keywords\": [\"list of 5-10 related keywords to include\"],\n"
        "  \"search_intent\": \"{intent}\",\n"
        "  \"content_format\": \"blog post / buying guide / collection page / product page\",\n"
        "  \"heading_structure\": [\n"
        "    {{\"tag\": \"h2\", \"text\": \"suggested heading\"}},\n"
        "    {{\"tag\": \"h3\", \"text\": \"sub-heading\"}}\n"
        "  ],\n"
        "  \"questions_to_answer\": [\"list of 5 questions users ask about this topic\"],\n"
        "  \"internal_links\": [\"5 specific internal pages to link to with suggested anchor text\"],\n"
        "  \"competitor_benchmarks\": \"what competitors do well that we should match or beat\",\n"
        "  \"unique_angle\": \"what makes our content different/better than competitors\",\n"
        "  \"cta_suggestion\": \"call to action recommendation\",\n"
        "  \"summary\": \"2-3 sentence brief summary of the content strategy\"\n"
        "}}"
    ),
    "prompts.intent_classification": (
        "Classify the search intent of each keyword for an ecommerce tobacco/smoke shop store.\n\n"
        "Categories:\n"
        "- transactional: user wants to buy (buy, price, order, shop, deal, cheap, wholesale)\n"
        "- commercial: user is researching before buying (best, top, review, vs, compare)\n"
        "- informational: user wants to learn (how to, what is, guide, tips, meaning)\n"
        "- navigational: user looking for specific brand/site\n\n"
        "Keywords to classify:\n{keywords}\n\n"
        "Return JSON array:\n"
        "[{{\"keyword\": \"...\", \"intent\": \"transactional|commercial|informational|navigational\", \"reason\": \"brief explanation\"}}]"
    ),
    "prompts.content_gap_analysis": (
        "You are an SEO strategist analyzing content gaps for an ecommerce store.\n\n"
        "Analyze these keyword gaps and provide strategic recommendations:\n{gaps_summary}\n\n"
        "Return JSON with:\n"
        "- top_priorities: array of 5 highest-impact keywords to target first\n"
        "- content_clusters: group related keywords into topic clusters\n"
        "- quick_wins: keywords where small improvements yield big results\n"
        "- strategic_recommendations: 3-5 actionable recommendations"
    ),
    "prompts.competitor_analysis": (
        "You are an SEO competitive analyst for an ecommerce store.\n\n"
        "Analyze these competitor ranking changes:\n{changes_summary}\n\n"
        "Provide:\n"
        "- key_threats: competitor movements that threaten our rankings\n"
        "- opportunities: gaps competitors are leaving open\n"
        "- recommended_actions: specific steps to counter competitor advances\n"
        "- priority_keywords: keywords to focus defensive/offensive efforts on\n\n"
        "Return JSON only."
    ),
    "prompts.schema_breadcrumb": (
        "Generate BreadcrumbList schema for: {item_type} \"{title}\" at path {path}\n"
        "Store domain: {domain}\n"
        "Parent collection: {parent_collection}\n\n"
        "Return valid JSON-LD BreadcrumbList schema."
    ),
    "prompts.schema_collection": (
        "Generate CollectionPage + ItemList schema for collection \"{title}\" with {product_count} products.\n"
        "Store domain: {domain}\n"
        "Description: {description}\n\n"
        "Return valid JSON-LD with @type CollectionPage including ItemList of products."
    ),
    "prompts.schema_article": (
        "Generate Article/BlogPosting schema for \"{title}\" by {author}.\n"
        "Published: {published_at}\n"
        "Store domain: {domain}\n"
        "Organization: {org_name}\n\n"
        "Return valid JSON-LD BlogPosting schema."
    ),
    "prompts.schema_organization": (
        "Generate Organization schema.\n"
        "Name: {org_name}\n"
        "URL: {org_url}\n"
        "Logo: {org_logo}\n\n"
        "Return valid JSON-LD Organization schema."
    ),
    "prompts.keyword_strategy_plan": (
        "You are an expert SEO strategist for a Shopify store. You have access to the store's "
        "master keyword list, content audit results, and current rankings. Your job is to create "
        "an optimal keyword assignment plan.\n\n"
        "Rules:\n"
        "- Collections should target high-volume head terms (1-2 words, e.g. 'protein bars')\n"
        "- Products should target specific long-tail keywords with size/flavor/variant modifiers\n"
        "- Blog articles should target informational queries ('how to', 'guide', 'what is')\n"
        "- Pages (static) should target commercial/brand terms\n"
        "- For cannibalized keywords, pick the page with the best current ranking as winner\n"
        "- Consider search volume, business intent, and current performance\n\n"
        "Use the tools to fetch data in batches and submit your assignments."
    ),
    "prompts.keyword_strategy_fix": (
        "You are an SEO copywriter. Generate optimized meta titles, meta descriptions, "
        "and tag suggestions for the assigned content. Follow these rules:\n"
        "- Meta title: 50-60 characters, primary keyword in first 3 words\n"
        "- Meta description: 140-160 characters, include primary keyword, include CTA\n"
        "- Avoid keyword stuffing\n"
        "- For products: include variant/size/flavor if relevant\n"
        "- Include the store's brand name at the end of meta title\n"
        "- Secondary keywords should be woven naturally into meta description\n"
        "- Keywords to AVOID must not appear in the generated content\n\n"
        "Return JSON with keys: meta_title, meta_description, tags_add, tags_remove"
    ),
}
