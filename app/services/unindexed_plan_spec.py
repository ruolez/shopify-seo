"""Pure-logic tests for triaging pages Google has not indexed (no DB).

Run with: pytest app/services/unindexed_plan_spec.py
"""

from app.services.unindexed_plan import diagnose, pick_link_sources, rank

CRAWLED = "Crawled - currently not indexed"
UNKNOWN = "URL is unknown to Google"


def _page(**kw):
    base = {"item_type": "product", "item_id": 1, "title": "Item", "coverage_state": CRAWLED,
            "volume": 0, "impressions": 0, "incoming_links": 5, "in_sitemap": True}
    return {**base, **kw}


class TestDiagnose:
    def test_page_google_never_saw_and_missing_from_sitemap(self):
        reason, action = diagnose(_page(coverage_state=UNKNOWN, in_sitemap=False))
        assert "not in your sitemap" in reason

    def test_page_nothing_links_to(self):
        reason, action = diagnose(_page(incoming_links=0))
        assert reason == "No article or description links to it"
        assert "link" in action.lower()

    def test_page_with_too_few_links(self):
        assert diagnose(_page(incoming_links=2))[0] == "Only 2 articles or descriptions link to it"
        assert diagnose(_page(incoming_links=1))[0] == "Only 1 article or description links to it"

    def test_well_linked_page_with_demand_needs_more_specific_content(self):
        reason, action = diagnose(_page(volume=300))
        assert "little that is new" in reason
        assert "specific" in action

    def test_well_linked_page_nobody_searches_for_can_be_left(self):
        reason, action = diagnose(_page())
        assert "little search demand" in reason
        assert "leave" in action.lower()


class TestRank:
    def test_pages_people_search_for_come_first_by_volume(self):
        pages = [_page(item_id=1), _page(item_id=2, volume=90), _page(item_id=3, volume=900)]
        assert [p["item_id"] for p in rank(pages)] == [3, 2, 1]

    def test_past_impressions_count_as_demand(self):
        pages = [_page(item_id=1), _page(item_id=2, impressions=40)]
        ranked = rank(pages)
        assert [p["item_id"] for p in ranked] == [2, 1]
        assert [p["worth_indexing"] for p in ranked] == [True, False]

    def test_each_page_carries_its_reason_and_action(self):
        page = rank([_page(incoming_links=0, volume=100)])[0]
        assert page["reason"] == "No article or description links to it"
        assert page["action"]


ARTICLES = [
    {"id": 10, "title": "Swisher Sweets Flavors Ranked", "clicks": 30},
    {"id": 11, "title": "Best Hookah Flavors for Beginners", "clicks": 50},
    {"id": 12, "title": "Swisher vs Backwoods", "clicks": 5},
]


class TestPickLinkSources:
    def test_articles_about_the_brand_fullest_match_first(self):
        picked = pick_link_sources({"vendor": "Swisher Sweets", "product_type": "Cigarillos"}, ARTICLES, limit=2)
        assert [a["id"] for a in picked] == [10, 12]

    def test_equal_brand_matches_rank_by_clicks(self):
        articles = [{"id": 1, "title": "Swisher guide", "clicks": 2}, {"id": 2, "title": "Swisher review", "clicks": 9}]
        assert [a["id"] for a in pick_link_sources({"vendor": "Swisher"}, articles)] == [2, 1]

    def test_product_type_alone_is_not_enough(self):
        assert pick_link_sources({"vendor": "Zig-Zag", "product_type": "Hookah"}, ARTICLES) == []

    def test_no_brand_gives_no_source(self):
        assert pick_link_sources({"vendor": "", "product_type": "Hookah"}, ARTICLES) == []
