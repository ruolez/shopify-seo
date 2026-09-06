"""Pure-logic tests for relevance-ranked link candidates (no DB).

Run with: pytest app/services/link_candidates_spec.py
"""

from app.services.link_candidates import rank_by_relevance, text_tokens


def _titles(candidates):
    return [c["title"] for c in candidates]


class TestTextTokens:
    def test_strips_html_and_stopwords_and_lowercases(self):
        assert text_tokens("<h2>The Best Rolling Papers</h2> for you") == {"rolling", "papers"}

    def test_apostrophes_collapse_so_possessives_match(self):
        assert text_tokens("RAW’s papers") == text_tokens("RAWs papers")

    def test_empty_input_yields_empty_set(self):
        assert text_tokens(None) == set()


class TestRankByRelevance:
    ARTICLE = (
        "<p>New York regulators cracked down on hemp wraps and rolling papers "
        "sold in smoke shops across the state.</p>"
    )

    def test_matching_titles_rank_ahead_of_unrelated_ones(self):
        candidates = [
            {"title": "Ceramic Ashtray", "handle": "ceramic-ashtray"},
            {"title": "Hemp Wraps", "handle": "hemp-wraps"},
            {"title": "Glass Water Pipe", "handle": "glass-water-pipe"},
            {"title": "Rolling Papers", "handle": "rolling-papers"},
        ]
        assert _titles(rank_by_relevance(self.ARTICLE, candidates, 2)) == [
            "Hemp Wraps",
            "Rolling Papers",
        ]

    def test_equal_match_count_prefers_fully_matched_title(self):
        candidates = [
            {"title": "Hemp Wraps Ceramic Grinder Kit", "handle": "kit"},
            {"title": "Hemp Wraps", "handle": "hemp-wraps"},
        ]
        assert _titles(rank_by_relevance(self.ARTICLE, candidates, 1)) == ["Hemp Wraps"]

    def test_zero_score_candidates_fill_remaining_slots_in_original_order(self):
        candidates = [
            {"title": "Ceramic Ashtray", "handle": "a"},
            {"title": "Grinder", "handle": "b"},
            {"title": "Hemp Wraps", "handle": "c"},
        ]
        assert _titles(rank_by_relevance(self.ARTICLE, candidates, 3)) == [
            "Hemp Wraps",
            "Ceramic Ashtray",
            "Grinder",
        ]

    def test_limit_truncates_result(self):
        candidates = [{"title": f"Item {i}", "handle": str(i)} for i in range(10)]
        assert len(rank_by_relevance(self.ARTICLE, candidates, 4)) == 4

    def test_empty_article_text_keeps_original_order(self):
        candidates = [
            {"title": "Hemp Wraps", "handle": "a"},
            {"title": "Rolling Papers", "handle": "b"},
        ]
        assert rank_by_relevance("", candidates, 2) == candidates
