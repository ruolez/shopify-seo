"""Score composition (settings stubbed to defaults; no DB).

Run with: pytest app/services/seo_analyzer_spec.py
"""

import pytest

from app.services import seo_analyzer
from app.services.seo_analyzer import DEFAULT_UNIFIED_SCORING, compute_unified_score

CONTENT_100 = {"score": 100, "breakdown": {}}
WEAK_SEARCH = {"search_score": 20, "breakdown": {}}


@pytest.fixture(autouse=True)
def default_rules(monkeypatch):
    monkeypatch.setattr(seo_analyzer, "get_setting", lambda key, default=None: DEFAULT_UNIFIED_SCORING)


class TestComputeUnifiedScore:
    def test_headline_score_is_on_page_only(self):
        result = compute_unified_score(CONTENT_100, 100, WEAK_SEARCH)
        assert result["seo_score"] == 100

    def test_search_performance_is_reported_beside_it_not_blended_in(self):
        result = compute_unified_score(CONTENT_100, 100, WEAK_SEARCH)
        assert result["search_score"] == 20
        assert result["unified_score"] == 68

    def test_first_impressions_do_not_lower_the_headline_score(self):
        without_search = compute_unified_score(CONTENT_100, 100, None)
        with_search = compute_unified_score(CONTENT_100, 100, WEAK_SEARCH)
        assert without_search["seo_score"] == with_search["seo_score"]

    def test_on_page_blends_content_and_keyword_equally_by_default(self):
        assert compute_unified_score({"score": 60, "breakdown": {}}, 100, None)["seo_score"] == 80

    def test_on_page_falls_back_to_content_without_keyword_analysis(self):
        assert compute_unified_score({"score": 60, "breakdown": {}}, None, WEAK_SEARCH)["seo_score"] == 60
