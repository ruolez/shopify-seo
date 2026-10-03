"""One keyword identity for the whole app.

Every module that compares, dedupes, or claims keywords must use these
primitives. Four incompatible normalizations used to coexist (the resolver's
punctuation-stripping _norm, plural-folded variant_key, the analyzer's
[a-z0-9]+ tokenizer, and plain lower()), so the same keyword could read as
"owned" in one feature and "unowned" in another.

- norm(): display-level dedup — "loose leaf." merges with "loose leaf".
- variant_key(): THE ownership identity — plural-folded, order-insensitive.
  Singular/plural and word-order variants share a SERP; two pages holding
  them as primaries cannibalize exactly like an exact-string duplicate.

master_keywords storage stays plain-lowercased (consolidation dedups by
LOWER(keyword)); rows are bridged to resolver primaries via variant_key at
read time, never by rewriting stored keywords.
"""

import re


def norm(kw):
    """Lowercase, strip punctuation, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s'&-]", " ", (kw or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def fold_token(t):
    """Plural-fold one token: 'masters' -> 'master'. The len > 3 guard keeps
    short words like 'gas' and 'its' intact."""
    return t[:-1] if t.endswith("s") and len(t) > 3 else t


def variant_key(kw):
    """Cluster key for ownership claims: plural-folded, order-insensitive
    tokens, so 'dutch masters', 'dutch master' and 'masters dutch' all claim
    one slot."""
    return " ".join(sorted(fold_token(t) for t in norm(kw).split()))


def tokens(kw):
    """norm()-consistent token list."""
    return norm(kw).split()


# Semrush exports carry column artifacts — bare numbers ("0", "10", "2024") and
# stray single letters ("a") — that behave like wildcards downstream: a
# one-character keyword substring-matches nearly every page, so it wins any
# relevance contest and becomes the "primary keyword" of content it has nothing
# to do with. Reject them at every entry point instead.
MIN_KEYWORD_LENGTH = 3
MIN_KEYWORD_LETTERS = 2


def is_valid_keyword(kw):
    """Whether a string is usable as a tracked keyword.

    Deliberately permissive about real short keywords — "zyn" (3 letters) and
    "3mg" (2 letters) pass — while rejecting pure numbers and single letters.
    """
    cleaned = norm(kw)
    if len(cleaned) < MIN_KEYWORD_LENGTH:
        return False
    return sum(1 for c in cleaned if c.isalpha()) >= MIN_KEYWORD_LETTERS
