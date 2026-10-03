"""Pure-logic tests for reading a session's primary keyword (no DB).

Run with: pytest app/services/article_keywords_spec.py
"""

from app.services.article_keywords import primary_of

KEYWORD = "best blunt wraps"


class TestPrimaryOf:
    def test_first_of_a_plain_list(self):
        assert primary_of([KEYWORD, "wraps"]) == KEYWORD

    def test_first_of_a_json_string(self):
        assert primary_of('["best blunt wraps", "wraps"]') == KEYWORD

    def test_first_of_a_list_of_keyword_dicts(self):
        assert primary_of([{"keyword": KEYWORD, "volume": 400}]) == KEYWORD

    def test_empty_missing_or_unparseable_gives_none(self):
        assert primary_of([]) is None
        assert primary_of(None) is None
        assert primary_of("not json") is None
        assert primary_of(["   "]) is None
