import json
import re
from collections import Counter
from app.settings_manager import get_setting
from app.prompt_defaults import DEFAULTS
from app.services.seo_rules import score_length, score_description_words, score_image_alt

def _get_stop_words():
    words = get_setting("lists.stop_words", DEFAULTS["lists.stop_words"])
    return frozenset(words)

STOP_WORDS = _get_stop_words()

def _get_ngram_multiplier():
    raw = get_setting("lists.ngram_multipliers", DEFAULTS["lists.ngram_multipliers"])
    return {int(k): v for k, v in raw.items()}

NGRAM_MULTIPLIER = _get_ngram_multiplier()

_PROMO_PATTERN = re.compile(
    r'\b(\d+)\s*for\s*\$?(\d+(?:\.\d{1,2})?[c¢]?)\b',
    re.IGNORECASE,
)

_PACKAGING_PATTERN = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*-?\s*'
    r'(packs?|ct|counts?|oz|lbs?|grams?|kg|ml|bags?|boxes?|cartons?|cases?|cans?|rolls?|pieces?|pcs?)'
    r'(?:\s+(bags?|box(?:es)?|cartons?|cases?|cans?|rolls?|packs?|pouch(?:es)?|tins?|tubs?|jars?|bottles?))?\b',
    re.IGNORECASE,
)


def _extract_promos(text):
    return [m.group(0).strip() for m in _PROMO_PATTERN.finditer(text)]


def _strip_promos(text, promos):
    for promo in promos:
        text = text.replace(promo, " ")
    return text


def _extract_packaging(text):
    return [m.group(0).strip() for m in _PACKAGING_PATTERN.finditer(text)]


def _strip_packaging(text, packaging):
    for pkg in packaging:
        text = text.replace(pkg, " ")
    return text


def _tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _strip_html(html):
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html)


def _is_valid_token(token):
    if token in STOP_WORDS:
        return False
    if token.isdigit():
        return True
    return len(token) > 1


def _get_variant_text(product):
    variants = product.get("variants") or []
    if isinstance(variants, str):
        try:
            variants = json.loads(variants)
        except (ValueError, TypeError):
            return ""
    titles = []
    for v in variants:
        t = v.get("title") or ""
        if t.lower() not in ("default title", "default"):
            titles.append(t)
    return " ".join(titles)


def _generate_ngrams(tokens, max_n=3):
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            gram_tokens = tokens[i:i + n]
            if n >= 2:
                meaningful = sum(1 for t in gram_tokens if t not in STOP_WORDS)
                if meaningful < 2:
                    continue
            yield (" ".join(gram_tokens), n)


def _infer_product_type(product):
    tags = product.get("tags") or ""
    for tag in tags.split(","):
        tag = tag.strip()
        if tag.lower().startswith("type_"):
            return tag[5:].strip().lower()

    title_lower = (product.get("title") or "").lower()
    for tag in tags.split(","):
        tag = tag.strip()
        tag_lower = tag.lower()
        if " " in tag_lower and not re.search(r"\d", tag_lower) and tag_lower in title_lower:
            return tag_lower

    return ""


def _get_compound_phrases(product):
    vendor = (product.get("vendor") or "").strip().lower()
    product_type = (product.get("product_type") or "").strip().lower()

    if not product_type:
        product_type = _infer_product_type(product)

    phrases = []
    if vendor:
        phrases.append((vendor, False))
    if product_type:
        phrases.append((product_type, False))
    if vendor and product_type:
        phrases.append((f"{vendor} {product_type}", True))
    return phrases


def _apply_subsumption(weighted_counts):
    phrases = sorted(weighted_counts.keys(), key=lambda k: len(k.split()), reverse=True)
    absorbed = {}

    for phrase in phrases:
        phrase_words = set(phrase.split())
        n = len(phrase.split())
        if n < 2:
            continue

        for shorter in phrases:
            shorter_words = set(shorter.split())
            shorter_n = len(shorter.split())
            if shorter_n >= n:
                continue
            if not shorter_words.issubset(phrase_words):
                continue

            if weighted_counts[phrase] >= weighted_counts[shorter] * 0.4:
                transfer = weighted_counts[shorter] * 0.5
                absorbed[shorter] = absorbed.get(shorter, 0) + transfer
                weighted_counts[phrase] += transfer

    for key, amount in absorbed.items():
        weighted_counts[key] = max(0, weighted_counts[key] - amount)

    return Counter({k: v for k, v in weighted_counts.items() if v > 0})


def _extract_keywords(product_or_collection):
    weighted_counts = Counter()

    title = product_or_collection.get("title") or ""
    vendor = product_or_collection.get("vendor") or ""
    product_type = product_or_collection.get("product_type") or ""
    meta_title = product_or_collection.get("meta_title") or ""
    handle = product_or_collection.get("handle") or ""
    meta_desc = product_or_collection.get("meta_description") or ""
    tags = product_or_collection.get("tags") or ""
    body_html = product_or_collection.get("description") or ""
    variant_text = _get_variant_text(product_or_collection)

    body_text = _strip_html(body_html)
    body_words = body_text.split()
    first_100 = " ".join(body_words[:100])
    rest_body = " ".join(body_words[100:])

    handle_text = handle.replace("-", " ")

    sources = [
        (title, 5),
        (vendor, 5),
        (product_type, 5),
        (meta_title, 4),
        (handle_text, 3),
        (first_100, 3),
        (tags, 2),
        (meta_desc, 2),
        (variant_text, 1.5),
        (rest_body, 1),
    ]

    promos = _extract_promos(title)
    packaging = _extract_packaging(title)

    product_type_lower = product_type.strip().lower()
    product_type_phrase = product_type_lower if len(_tokenize(product_type_lower)) >= 2 else ""

    for text, weight in sources:
        clean_text = text
        if promos:
            clean_text = _strip_promos(clean_text, promos)
        if packaging:
            clean_text = _strip_packaging(clean_text, packaging)
        if product_type_phrase:
            clean_text = re.sub(re.escape(product_type_phrase), " ", clean_text, flags=re.IGNORECASE)
        tokens = _tokenize(clean_text)
        filtered = [t for t in tokens if _is_valid_token(t)]

        for ngram, n in _generate_ngrams(filtered):
            weighted_counts[ngram] += weight * NGRAM_MULTIPLIER[n]

    compound_phrases = _get_compound_phrases(product_or_collection)

    if packaging:
        title_lower = title.lower()
        vendor_lower = vendor.strip().lower()
        for pkg in packaging:
            pkg_lower = pkg.lower().strip()
            pkg_joined = " ".join(_tokenize(pkg_lower))

            pkg_start = title_lower.find(pkg_lower)
            before_word = ""
            after_word = ""
            if pkg_start >= 0:
                before_text = title_lower[:pkg_start].strip()
                after_text = title_lower[pkg_start + len(pkg_lower):].strip()
                if before_text:
                    before_tokens = _tokenize(before_text)
                    if before_tokens:
                        before_word = before_tokens[-1]
                if after_text:
                    after_tokens = _tokenize(after_text)
                    if after_tokens:
                        after_word = after_tokens[0]

            compound_phrases.append((pkg_joined, False))
            if after_word and _is_valid_token(after_word):
                compound_phrases.append((f"{pkg_joined} {after_word}", False))
            if before_word and _is_valid_token(before_word):
                compound_phrases.append((f"{before_word} {pkg_joined}", False))
            if product_type_lower:
                compound_phrases.append((f"{product_type_lower} {pkg_joined}", False))
            if vendor_lower:
                compound_phrases.append((f"{vendor_lower} {pkg_joined}", False))

    for phrase, is_primary in compound_phrases:
        phrase_tokens = set(phrase.split())
        n = len(phrase_tokens)
        multiplier = NGRAM_MULTIPLIER.get(n, 2.0)
        for text, weight in sources:
            text_lower = text.lower()
            if phrase in text_lower:
                weighted_counts[phrase] += weight * multiplier
            elif phrase_tokens.issubset(set(_tokenize(text_lower))):
                weighted_counts[phrase] += weight * multiplier * 0.7
        if is_primary:
            weighted_counts[phrase] += 20.0

    weighted_counts = _apply_subsumption(weighted_counts)

    vendor_tokens = set(_tokenize(vendor))
    if len(vendor_tokens) >= 2:
        for kw in list(weighted_counts):
            kw_tokens = set(kw.split())
            overlap = kw_tokens & vendor_tokens
            if overlap and not vendor_tokens.issubset(kw_tokens):
                del weighted_counts[kw]

    top = weighted_counts.most_common(8)
    keywords = [{"keyword": kw, "confidence": round(score, 1)} for kw, score in top]

    if packaging:
        pkg_terms = {" ".join(_tokenize(p.lower())) for p in packaging}
        has_pkg = any(any(pkg in kw["keyword"] for pkg in pkg_terms) for kw in keywords)
        if not has_pkg:
            for kw, score in weighted_counts.most_common():
                if any(pkg in kw for pkg in pkg_terms):
                    keywords[-1] = {"keyword": kw, "confidence": round(score, 1)}
                    break
        existing = {kw["keyword"] for kw in keywords}
        for pkg_term in pkg_terms:
            if pkg_term not in existing:
                keywords.append({"keyword": pkg_term, "confidence": 0, "packaging_only": True})

    return keywords


def _keyword_in_text(keyword, text):
    if not keyword or not text:
        return False
    if keyword.lower() in text.lower():
        return True
    kw_tokens = set(_tokenize(keyword))
    if len(kw_tokens) >= 2:
        return kw_tokens.issubset(set(_tokenize(text)))
    return False


def _keyword_position_in_title(keyword, title, first_n=5):
    if not keyword or not title:
        return 0
    words = _tokenize(title)
    kw_words = _tokenize(keyword)
    if not kw_words:
        return 0
    first_kw_word = kw_words[0]
    for i, w in enumerate(words):
        if w == first_kw_word:
            if i < first_n:
                return 100
            return 50
    return 0


def _keyword_density(keyword, body_html):
    text = _strip_html(body_html).lower()
    tokens = _tokenize(text)
    total_words = len(tokens)
    if total_words == 0:
        return 0.0
    kw_tokens = _tokenize(keyword)
    kw_len = len(kw_tokens)
    if kw_len == 1:
        count = sum(1 for w in tokens if w == kw_tokens[0])
    else:
        count = text.count(keyword.lower())
        if count == 0:
            kw_set = set(kw_tokens)
            window = kw_len + 2
            for i in range(total_words - kw_len + 1):
                chunk = set(tokens[i:i + window])
                if kw_set.issubset(chunk):
                    count += 1
    return (count / total_words) * 100


def _count_headings(body_html):
    if not body_html:
        return 0
    return len(re.findall(r"<h[2-6][^>]*>", body_html, re.IGNORECASE))


def analyze_product_technical_seo(product, primary_keyword=None):
    keywords = _extract_keywords(product)
    primary_kw = primary_keyword or (keywords[0]["keyword"] if keywords else None)
    if not primary_kw:
        return {
            "score": 0,
            "detected_keywords": [],
            "checks": {},
            "issues_count": 0,
            "fixable_count": 0,
        }

    title = product.get("title") or ""
    meta_title = product.get("meta_title") or ""
    meta_desc = product.get("meta_description") or ""
    handle = product.get("handle") or ""
    body_html = product.get("description") or ""
    images = product.get("images") or []
    if isinstance(images, str):
        import json
        try:
            images = json.loads(images)
        except (ValueError, TypeError):
            images = []

    body_text = _strip_html(body_html)
    body_words = body_text.split()
    first_100_text = " ".join(body_words[:100])
    word_count = len(body_words)

    rules = get_setting("rules.technical_product", DEFAULTS["rules.technical_product"])
    tech_thresholds = rules.get("thresholds", {})

    checks = {}

    # 1. Title contains primary keyword
    has_kw_in_title = _keyword_in_text(primary_kw, title)
    checks["title_has_keyword"] = {
        "score": 100 if has_kw_in_title else 0,
        "detail": f"Title contains '{primary_kw}'" if has_kw_in_title else f"Title missing keyword '{primary_kw}'",
        "issue": None if has_kw_in_title else f"Add '{primary_kw}' to product title",
        "fix_type": "title",
    }

    # 2. Keyword position in title
    title_first_n = tech_thresholds.get("keyword_at_title_start", 5)
    pos_score = _keyword_position_in_title(primary_kw, title, title_first_n)
    if pos_score == 100:
        pos_detail = f"Keyword '{primary_kw}' appears in first {title_first_n} words of title"
    elif pos_score == 50:
        pos_detail = f"Keyword '{primary_kw}' found in title but not in first {title_first_n} words"
    else:
        pos_detail = f"Keyword '{primary_kw}' not found in title"
    checks["keyword_at_title_start"] = {
        "score": pos_score,
        "detail": pos_detail,
        "issue": None if pos_score == 100 else f"Move the primary keyword into the first {title_first_n} words of the title",
        "fix_type": "title",
    }

    # 3. Meta title contains keyword
    effective_meta = meta_title or title
    has_kw_meta = _keyword_in_text(primary_kw, effective_meta)
    checks["meta_title_has_keyword"] = {
        "score": 100 if has_kw_meta else 0,
        "detail": f"Meta title contains '{primary_kw}'" if has_kw_meta else f"Meta title missing keyword '{primary_kw}'",
        "issue": None if has_kw_meta else f"Add '{primary_kw}' to meta title",
        "fix_type": "meta_title",
    }

    # Shared completeness thresholds — same source of truth as seo_analyzer
    content_thresholds = get_setting("rules.product_seo", DEFAULTS["rules.product_seo"])["thresholds"]

    # 4. Meta title length
    mt_len = len(effective_meta)
    mt_good = content_thresholds["meta_title"]["good"]
    mt_score, mt_detail = score_length(mt_len, content_thresholds["meta_title"], "meta title")
    checks["meta_title_length"] = {
        "score": mt_score,
        "detail": mt_detail,
        "issue": None if mt_score == 100 else f"Adjust meta title to {mt_good[0]}-{mt_good[1]} characters (currently {mt_len})",
        "fix_type": "meta_title",
    }

    # 5. Meta description contains keyword
    has_kw_md = _keyword_in_text(primary_kw, meta_desc)
    checks["meta_desc_has_keyword"] = {
        "score": 100 if has_kw_md else 0,
        "detail": f"Meta description contains '{primary_kw}'" if has_kw_md else f"Meta description missing keyword '{primary_kw}'",
        "issue": None if has_kw_md else f"Add '{primary_kw}' to meta description",
        "fix_type": "meta_description",
    }

    # 6. Meta description length
    md_len = len(meta_desc)
    md_good = content_thresholds["meta_description"]["good"]
    md_score, md_detail = score_length(md_len, content_thresholds["meta_description"], "meta description")
    checks["meta_desc_length"] = {
        "score": md_score,
        "detail": md_detail,
        "issue": None if md_score == 100 else f"Adjust meta description to {md_good[0]}-{md_good[1]} characters (currently {md_len})",
        "fix_type": "meta_description",
    }

    # 7. URL/handle contains keyword
    handle_text = handle.replace("-", " ").lower()
    has_kw_url = _keyword_in_text(primary_kw, handle_text)
    checks["url_has_keyword"] = {
        "score": 100 if has_kw_url else 0,
        "detail": f"URL handle contains '{primary_kw}'" if has_kw_url else f"URL handle missing keyword '{primary_kw}'",
        "issue": None if has_kw_url else f"URL handle does not contain '{primary_kw}' (informational — Shopify creates an automatic redirect if you change the handle with 'Create URL redirect' enabled)",
        "fix_type": None,
    }

    # 8. Keyword in first 100 words of body
    has_kw_body = _keyword_in_text(primary_kw, first_100_text)
    checks["body_first_100_has_keyword"] = {
        "score": 100 if has_kw_body else 0,
        "detail": f"Keyword '{primary_kw}' found in first 100 words" if has_kw_body else f"Keyword '{primary_kw}' not in first 100 words",
        "issue": None if has_kw_body else f"Include '{primary_kw}' in the first 100 words of description",
        "fix_type": "description",
    }

    # 9. Keyword stuffing guard — density is not a ranking factor; only
    # excessive exact-match repetition is penalized.
    density = _keyword_density(primary_kw, body_html)
    stuffing_max = tech_thresholds.get("keyword_stuffing_max", 4.0)
    if density <= stuffing_max:
        dens_score = 100
        dens_detail = f"No stuffing risk — keyword density {density:.1f}%"
        dens_issue = None
    else:
        dens_score = 0
        dens_detail = f"Keyword '{primary_kw}' appears excessively ({density:.1f}%) — reads as stuffing"
        dens_issue = f"Keyword '{primary_kw}' appears excessively ({density:.1f}%) — reduce exact-match repetition, use synonyms"
    checks["keyword_density"] = {
        "score": dens_score,
        "detail": dens_detail,
        "issue": dens_issue,
        "fix_type": "description",
    }

    # 10. Description word count
    dw = content_thresholds["description_words"]
    wc_score, wc_detail = score_description_words(word_count, dw, "product")
    checks["description_word_count"] = {
        "score": wc_score,
        "detail": wc_detail,
        "issue": None if wc_score == 100 else f"Expand description to {dw['good']}+ words (currently {word_count})",
        "fix_type": "description",
    }

    # 11. Heading structure (H2/H3 in description)
    heading_count = _count_headings(body_html)
    if heading_count >= 2:
        h_score = 100
        h_detail = f"Good heading structure ({heading_count} headings found)"
    elif heading_count == 1:
        h_score = 60
        h_detail = "Only 1 heading found, aim for 2+ subheadings"
    else:
        h_score = 0
        h_detail = "No headings (H2-H6) in description"
    checks["heading_structure"] = {
        "score": h_score,
        "detail": h_detail,
        "issue": None if h_score == 100 else "Add H2/H3 subheadings to structure your description",
        "fix_type": "description",
    }

    # 12. Image alt text coverage
    img_alt_score, img_alt_detail, _, _ = score_image_alt(images)
    checks["image_alt_coverage"] = {
        "score": img_alt_score,
        "detail": img_alt_detail,
        "issue": None if img_alt_score == 100 else "Add alt text to all product images",
        "fix_type": "image_alt",
    }

    # 13. Alt text keyword balance — the keyword belongs in 1-2 of the most
    # relevant alts; putting it in every alt reads as stuffing (Google spam
    # guidance), and none at all misses an image-search signal.
    n_imgs = len(images)
    k_with_kw = sum(
        1 for img in images
        if img.get("alt") and _keyword_in_text(primary_kw, img["alt"])
    )
    if n_imgs == 0:
        img_kw_score = 0
        img_kw_detail = "No images to check"
        img_kw_issue = None  # coverage check owns the no-images problem
    elif n_imgs == 1:
        if k_with_kw == 1:
            img_kw_score = 100
            img_kw_detail = f"Image alt contains '{primary_kw}'"
            img_kw_issue = None
        else:
            img_kw_score = 60
            img_kw_detail = f"Image alt does not mention '{primary_kw}'"
            img_kw_issue = f"Include '{primary_kw}' in the image alt text"
    elif k_with_kw == 0:
        img_kw_score = 60
        img_kw_detail = f"0/{n_imgs} alts contain '{primary_kw}'"
        img_kw_issue = f"Include '{primary_kw}' in one or two of the most relevant image alts"
    elif k_with_kw == n_imgs:
        img_kw_score = 0
        img_kw_detail = f"All {n_imgs} image alts contain '{primary_kw}'"
        img_kw_issue = "Keyword in every image alt reads as stuffing — vary the descriptions"
    else:
        img_kw_score = 100
        img_kw_detail = f"{k_with_kw}/{n_imgs} alts contain '{primary_kw}' — good balance (target 1-2)"
        img_kw_issue = None
    checks["image_alt_has_keyword"] = {
        "score": img_kw_score,
        "detail": img_kw_detail,
        "issue": img_kw_issue,
        "fix_type": "image_alt",
    }

    weights = rules.get("weights", DEFAULTS["rules.technical_product"]["weights"])

    overall = sum(checks[k]["score"] * weights.get(k, 0) for k in weights)
    issues_count = sum(1 for c in checks.values() if c["issue"])
    fixable_count = sum(1 for c in checks.values() if c["issue"] and c["fix_type"])

    return {
        "score": round(overall),
        "detected_keywords": keywords,
        "checks": checks,
        "issues_count": issues_count,
        "fixable_count": fixable_count,
    }


def analyze_collection_technical_seo(collection, primary_keyword=None):
    keywords = _extract_keywords(collection)
    primary_kw = primary_keyword or (keywords[0]["keyword"] if keywords else None)
    if not primary_kw:
        return {
            "score": 0,
            "detected_keywords": [],
            "checks": {},
            "issues_count": 0,
            "fixable_count": 0,
        }

    title = collection.get("title") or ""
    meta_title = collection.get("meta_title") or ""
    meta_desc = collection.get("meta_description") or ""
    handle = collection.get("handle") or ""
    body_html = collection.get("description") or ""

    body_text = _strip_html(body_html)
    body_words = body_text.split()
    first_100_text = " ".join(body_words[:100])
    word_count = len(body_words)

    rules = get_setting("rules.technical_collection", DEFAULTS["rules.technical_collection"])
    tech_thresholds = rules.get("thresholds", {})

    checks = {}

    # 1. Title contains primary keyword
    has_kw_in_title = _keyword_in_text(primary_kw, title)
    checks["title_has_keyword"] = {
        "score": 100 if has_kw_in_title else 0,
        "detail": f"Title contains '{primary_kw}'" if has_kw_in_title else f"Title missing keyword '{primary_kw}'",
        "issue": None if has_kw_in_title else f"Add '{primary_kw}' to collection title",
        "fix_type": "title",
    }

    # 2. Keyword position in title
    title_first_n = tech_thresholds.get("keyword_at_title_start", 5)
    pos_score = _keyword_position_in_title(primary_kw, title, title_first_n)
    if pos_score == 100:
        pos_detail = f"Keyword '{primary_kw}' appears in first {title_first_n} words of title"
    elif pos_score == 50:
        pos_detail = f"Keyword '{primary_kw}' found in title but not in first {title_first_n} words"
    else:
        pos_detail = f"Keyword '{primary_kw}' not found in title"
    checks["keyword_at_title_start"] = {
        "score": pos_score,
        "detail": pos_detail,
        "issue": None if pos_score == 100 else f"Move the primary keyword into the first {title_first_n} words of the title",
        "fix_type": "title",
    }

    # 3. Meta title contains keyword
    effective_meta = meta_title or title
    has_kw_meta = _keyword_in_text(primary_kw, effective_meta)
    checks["meta_title_has_keyword"] = {
        "score": 100 if has_kw_meta else 0,
        "detail": f"Meta title contains '{primary_kw}'" if has_kw_meta else f"Meta title missing keyword '{primary_kw}'",
        "issue": None if has_kw_meta else f"Add '{primary_kw}' to meta title",
        "fix_type": "meta_title",
    }

    # Shared completeness thresholds — same source of truth as seo_analyzer
    content_thresholds = get_setting("rules.collection_seo", DEFAULTS["rules.collection_seo"])["thresholds"]

    # 4. Meta title length
    mt_len = len(effective_meta)
    mt_good = content_thresholds["meta_title"]["good"]
    mt_score, mt_detail = score_length(mt_len, content_thresholds["meta_title"], "meta title")
    checks["meta_title_length"] = {
        "score": mt_score,
        "detail": mt_detail,
        "issue": None if mt_score == 100 else f"Adjust meta title to {mt_good[0]}-{mt_good[1]} characters (currently {mt_len})",
        "fix_type": "meta_title",
    }

    # 5. Meta description contains keyword
    has_kw_md = _keyword_in_text(primary_kw, meta_desc)
    checks["meta_desc_has_keyword"] = {
        "score": 100 if has_kw_md else 0,
        "detail": f"Meta description contains '{primary_kw}'" if has_kw_md else f"Meta description missing keyword '{primary_kw}'",
        "issue": None if has_kw_md else f"Add '{primary_kw}' to meta description",
        "fix_type": "meta_description",
    }

    # 6. Meta description length
    md_len = len(meta_desc)
    md_good = content_thresholds["meta_description"]["good"]
    md_score, md_detail = score_length(md_len, content_thresholds["meta_description"], "meta description")
    checks["meta_desc_length"] = {
        "score": md_score,
        "detail": md_detail,
        "issue": None if md_score == 100 else f"Adjust meta description to {md_good[0]}-{md_good[1]} characters (currently {md_len})",
        "fix_type": "meta_description",
    }

    # 7. URL/handle contains keyword
    handle_text = handle.replace("-", " ").lower()
    has_kw_url = _keyword_in_text(primary_kw, handle_text)
    checks["url_has_keyword"] = {
        "score": 100 if has_kw_url else 0,
        "detail": f"URL handle contains '{primary_kw}'" if has_kw_url else f"URL handle missing keyword '{primary_kw}'",
        "issue": None if has_kw_url else f"URL handle does not contain '{primary_kw}' (informational — Shopify creates an automatic redirect if you change the handle with 'Create URL redirect' enabled)",
        "fix_type": None,
    }

    # 8. Keyword in first 100 words of body
    has_kw_body = _keyword_in_text(primary_kw, first_100_text)
    checks["body_first_100_has_keyword"] = {
        "score": 100 if has_kw_body else 0,
        "detail": f"Keyword '{primary_kw}' found in first 100 words" if has_kw_body else f"Keyword '{primary_kw}' not in first 100 words",
        "issue": None if has_kw_body else f"Include '{primary_kw}' in the first 100 words of description",
        "fix_type": "description",
    }

    # 9. Keyword stuffing guard — density is not a ranking factor; only
    # excessive exact-match repetition is penalized.
    density = _keyword_density(primary_kw, body_html)
    stuffing_max = tech_thresholds.get("keyword_stuffing_max", 4.0)
    if density <= stuffing_max:
        dens_score = 100
        dens_detail = f"No stuffing risk — keyword density {density:.1f}%"
        dens_issue = None
    else:
        dens_score = 0
        dens_detail = f"Keyword '{primary_kw}' appears excessively ({density:.1f}%) — reads as stuffing"
        dens_issue = f"Keyword '{primary_kw}' appears excessively ({density:.1f}%) — reduce exact-match repetition, use synonyms"
    checks["keyword_density"] = {
        "score": dens_score,
        "detail": dens_detail,
        "issue": dens_issue,
        "fix_type": "description",
    }

    # 10. Description word count (150+ for collections)
    dw = content_thresholds["description_words"]
    wc_score, wc_detail = score_description_words(word_count, dw, "collection")
    checks["description_word_count"] = {
        "score": wc_score,
        "detail": wc_detail,
        "issue": None if wc_score == 100 else f"Expand description to {dw['good']}+ words (currently {word_count})",
        "fix_type": "description",
    }

    # 11. Heading structure
    heading_count = _count_headings(body_html)
    if heading_count >= 2:
        h_score = 100
        h_detail = f"Good heading structure ({heading_count} headings found)"
    elif heading_count == 1:
        h_score = 60
        h_detail = "Only 1 heading found, aim for 2+ subheadings"
    else:
        h_score = 0
        h_detail = "No headings (H2-H6) in description"
    checks["heading_structure"] = {
        "score": h_score,
        "detail": h_detail,
        "issue": None if h_score == 100 else "Add H2/H3 subheadings to structure your description",
        "fix_type": "description",
    }

    # 12. Image alt text coverage (single image for collections)
    image_url = collection.get("image_url")
    if image_url:
        img_alt_score = 100
        img_alt_detail = "Collection has an image"
    else:
        img_alt_score = 0
        img_alt_detail = "No collection image set"
    checks["image_alt_coverage"] = {
        "score": img_alt_score,
        "detail": img_alt_detail,
        "issue": None if img_alt_score == 100 else "Add a collection image",
        "fix_type": None,
    }

    # Collections: rebalanced weights (no image_alt_has_keyword)
    weights = rules.get("weights", DEFAULTS["rules.technical_collection"]["weights"])

    overall = sum(checks[k]["score"] * weights.get(k, 0) for k in checks)
    issues_count = sum(1 for c in checks.values() if c["issue"])
    fixable_count = sum(1 for c in checks.values() if c["issue"] and c["fix_type"])

    return {
        "score": round(overall),
        "detected_keywords": keywords,
        "checks": checks,
        "issues_count": issues_count,
        "fixable_count": fixable_count,
    }
