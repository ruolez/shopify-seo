"""Shared length rules for generated content.

Google has no preferred length. Articles get a target that covers the topic
the way ranking pages do, bounded so a long competitor never turns into a
padding instruction."""

ARTICLE_MIN_WORDS = 800
ARTICLE_MAX_WORDS = 1800
ARTICLE_DEFAULT_WORDS = 1200


def article_target_words(ai_recommendation=None, competitor_avg=None):
    """Word target for a new article: the research step's estimate of what a
    complete answer needs, else the competitor average, else the default —
    always within [ARTICLE_MIN_WORDS, ARTICLE_MAX_WORDS]."""
    for value in (ai_recommendation, competitor_avg):
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return max(ARTICLE_MIN_WORDS, min(n, ARTICLE_MAX_WORDS))
    return ARTICLE_DEFAULT_WORDS
