"""Fuzzy "same headline" check shared by the News and Blog duplicate guards."""

import re

_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")

_TITLE_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "with", "as",
    "at", "by", "its", "is", "are", "from", "this", "that", "up", "over", "under",
}


def title_tokens(title):
    # Apostrophes (straight or curly) are removed, not treated as separators, so
    # quoted words ('Lax') and possessives (Estate's/Estates) tokenize identically.
    # Stopwords are dropped so filler-word rewrites don't dilute the overlap.
    cleaned = (title or "").casefold().replace("'", "").replace("’", "")
    tokens = set(_TITLE_TOKEN_RE.findall(cleaned))
    content = tokens - _TITLE_STOPWORDS
    return content or tokens


def titles_match(a, b, threshold=0.8):
    """True when two headlines read as the same story.

    Fuzzy on purpose — re-importing the same source yields AI headlines that differ
    by a word or two ("Over Lax" vs "For Lax") or trim trailing detail, which exact
    matching would miss. Same story = token-set Jaccard >= threshold, or (for titles
    with enough substance) the shorter title's tokens are >= threshold contained in
    the longer one's.
    """
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    if inter / len(ta | tb) >= threshold:
        return True
    smaller = min(len(ta), len(tb))
    return smaller >= 4 and inter / smaller >= threshold
