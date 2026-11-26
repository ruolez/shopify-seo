import pytest

from app.services.content_rules import (
    ARTICLE_DEFAULT_WORDS,
    ARTICLE_MAX_WORDS,
    ARTICLE_MIN_WORDS,
    article_target_words,
)


class TestArticleTargetWords:
    def test_ai_estimate_wins_inside_bounds(self):
        assert article_target_words(1100, 2600) == 1100

    def test_competitor_average_when_no_estimate(self):
        assert article_target_words(None, 950) == 950

    @pytest.mark.parametrize("value,expected", [(3500, ARTICLE_MAX_WORDS), (300, ARTICLE_MIN_WORDS)])
    def test_clamped_to_bounds(self, value, expected):
        assert article_target_words(value) == expected

    @pytest.mark.parametrize("ai,comp", [(None, None), (0, 0), ("n/a", None)])
    def test_default_without_usable_input(self, ai, comp):
        assert article_target_words(ai, comp) == ARTICLE_DEFAULT_WORDS

    def test_never_exceeds_max_whatever_competitors_write(self):
        assert max(article_target_words(None, c) for c in range(0, 10000, 250)) == ARTICLE_MAX_WORDS
