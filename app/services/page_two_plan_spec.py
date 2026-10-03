"""Pure-logic tests for the page-two plan (explicit curve; no DB).

Run with: pytest app/services/page_two_plan_spec.py
"""

from app.services.page_two_plan import build_plan

CURVE = {1: 0.20, 3: 0.10, 5: 0.05, 10: 0.02, 20: 0.01, 999: 0.004}


def _row(path, query, impressions, clicks, position):
    return {"path": path, "query": query, "impressions": impressions, "clicks": clicks, "position": position}


PAGES = {
    "collections/blunt-wraps": {"item_type": "collection", "id": 1, "title": "Blunt Wraps"},
    "products/zig-zag": {"item_type": "product", "id": 2, "title": "Zig-Zag Wraps"},
    "blogs/news/best-wraps": {"item_type": "article", "id": 3, "title": "Best Wraps"},
}


class TestBuildPlan:
    def test_groups_queries_under_their_page_with_a_summed_gain(self):
        rows = [
            _row("collections/blunt-wraps", "blunt wraps", 1000, 20, 8),     # 2% -> 10% = +80
            _row("collections/blunt-wraps", "buy blunt wraps", 500, 5, 14),  # 1% -> 2% (page one) = +5
        ]
        plan = build_plan(rows, PAGES, CURVE)
        assert len(plan) == 1
        assert plan[0]["title"] == "Blunt Wraps"
        assert plan[0]["est_clicks_gain"] == 85
        assert [q["query"] for q in plan[0]["queries"]] == ["blunt wraps", "buy blunt wraps"]

    def test_collections_come_before_products_and_articles(self):
        rows = [
            _row("blogs/news/best-wraps", "best wraps", 5000, 50, 8),
            _row("products/zig-zag", "zig zag wraps", 2000, 20, 8),
            _row("collections/blunt-wraps", "blunt wraps", 1000, 20, 8),
        ]
        assert [p["item_type"] for p in build_plan(rows, PAGES, CURVE)] == ["collection", "product", "article"]

    def test_within_a_type_the_bigger_gain_leads(self):
        pages = {**PAGES, "collections/hemp": {"item_type": "collection", "id": 9, "title": "Hemp Wraps"}}
        rows = [
            _row("collections/blunt-wraps", "blunt wraps", 1000, 20, 8),
            _row("collections/hemp", "hemp wraps", 4000, 80, 8),
        ]
        assert [p["title"] for p in build_plan(rows, pages, CURVE)] == ["Hemp Wraps", "Blunt Wraps"]

    def test_queries_with_nothing_to_gain_and_unknown_pages_are_left_out(self):
        rows = [
            _row("collections/blunt-wraps", "blunt wraps", 1000, 300, 8),   # already above target CTR
            _row("pages/unknown", "something", 1000, 0, 8),
        ]
        assert build_plan(rows, PAGES, CURVE) == []


from app.services.page_two_plan import _merge_url_variants  # noqa: E402


class TestMergeUrlVariants:
    def test_same_path_and_query_become_one_row_with_weighted_position(self):
        rows = [_row("blogs/news/x", "white owl flavors", 300, 3, 6), _row("blogs/news/x", "white owl flavors", 100, 1, 10)]
        assert _merge_url_variants(rows) == [
            {"path": "blogs/news/x", "query": "white owl flavors", "impressions": 400, "clicks": 4, "position": 7.0}]
