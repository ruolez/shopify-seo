from datetime import date

import pytest

from app.services.gsc_page_totals import aggregate_by_path, totals_window


def _row(page, clicks, impressions, position):
    return {"page": page, "clicks": clicks, "impressions": impressions, "position": position}


class TestAggregateByPath:
    def test_url_variants_of_one_page_merge_into_its_path(self):
        rows = [
            _row("https://shop.com/blogs/news/post", 10, 100, 2.0),
            _row("https://shop.com/blogs/news/post/", 5, 100, 4.0),
            _row("https://shop.com/blogs/news/post?utm=x", 1, 0, 0),
        ]
        assert aggregate_by_path(rows) == {
            "/blogs/news/post": {"clicks": 16, "impressions": 200, "position": 3.0}
        }

    def test_position_is_impression_weighted_and_absent_without_impressions(self):
        rows = [
            _row("https://shop.com/a", 0, 300, 1.0),
            _row("https://shop.com/A", 0, 100, 9.0),
            _row("https://shop.com/b", 2, 0, 0),
        ]
        assert aggregate_by_path(rows) == {
            "/a": {"clicks": 0, "impressions": 400, "position": 3.0},
            "/b": {"clicks": 2, "impressions": 0, "position": None},
        }

    def test_fractional_values_are_rounded_to_counts(self):
        rows = [_row("https://shop.com/p", 2.4, 99.6, 5.26), _row("https://shop.com/p/", 0.3, 0, 0)]
        assert aggregate_by_path(rows) == {"/p": {"clicks": 3, "impressions": 100, "position": 5.3}}

    @pytest.mark.parametrize("page", ["", None, "https://shop.com", "https://shop.com/"])
    def test_rows_without_a_path_are_dropped(self, page):
        assert aggregate_by_path([_row(page, 5, 5, 1.0)]) == {}


class TestTotalsWindow:
    def test_window_ends_on_the_latest_available_day_and_spans_days(self):
        assert totals_window(date(2026, 10, 8), days=30) == {
            "from": "2026-09-09",
            "to": "2026-10-08",
            "days": 30,
        }

    def test_no_data_means_no_window(self):
        assert totals_window(None, days=30) is None
