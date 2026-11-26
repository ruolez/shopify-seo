"""Search Console now reports AI-agent prompts as "queries" (pasted
instructions, up to ~1,000 characters). They are not keywords: they broke
keyword consolidation (varchar 500) and would pollute every keyword list.
Long natural questions from AI Mode are real searches and are kept."""
import re

MAX_QUERY_CHARS = 200

_PROMPT_MARKERS = re.compile(
    r"\b(you are an? |your (response|approach|task|role)\b|do not include|"
    r"respond (with|in|only)\b|as an ai\b|output format|step[- ]by[- ]step instructions)",
    re.IGNORECASE,
)


def is_junk_query(query):
    text = (query or "").strip()
    if not text:
        return True
    if len(text) > MAX_QUERY_CHARS or "\n" in text or text.startswith("#"):
        return True
    return bool(_PROMPT_MARKERS.search(text))


_GENERIC_NAME_WORDS = {"the", "shop", "store", "best", "online", "co", "inc", "llc", "company", "outlet"}


def brand_terms(shop_name, domain):
    """Terms that make a search navigational for this store: the shop name
    and domain label run together ("a2ztobacco"), and the shop name's first
    word when distinctive ("a2z" from "A2Z Tobacco")."""
    compact = set()
    words = set()
    name_words = re.findall(r"[a-z0-9]+", (shop_name or "").lower())
    if name_words:
        compact.add("".join(name_words))
        if len(name_words) > 1 and len(name_words[0]) >= 3 and name_words[0] not in _GENERIC_NAME_WORDS:
            words.add(name_words[0])
    label = (domain or "").lower().replace("https://", "").replace("http://", "").replace("www.", "").split(".")[0]
    if len(label) >= 3:
        compact.add(re.sub(r"[^a-z0-9]", "", label))
    return {"compact": {c for c in compact if len(c) >= 3}, "words": words}


def is_branded_query(query, terms, labels=None):
    """Branded when the query names the store, or when Google's own label
    (Search Console brandedClassification) says so. Google calls searches
    like "a2z smoke shop" non-branded; they still find the store by name, so
    the store-name match wins. Pure."""
    text = (query or "").lower()
    if any(c in re.sub(r"[^a-z0-9]", "", text) for c in terms["compact"]):
        return True
    if terms["words"] & set(re.findall(r"[a-z0-9]+", text)):
        return True
    return bool(labels and labels.get(text.strip()))
