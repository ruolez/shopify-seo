"""Pure-logic tests for the monthly report (no DB).

Run with: pytest app/services/monthly_report_spec.py
"""

from app.services.monthly_report import change_pct, headline, win_rates


class TestChangePct:
    def test_growth_and_decline(self):
        assert change_pct(150, 100) == 50.0
        assert change_pct(80, 100) == -20.0

    def test_no_baseline_gives_none_not_infinity(self):
        assert change_pct(40, 0) is None
        assert change_pct(0, 0) is None


class TestWinRates:
    def test_counts_only_changes_with_a_verdict(self):
        items = [
            {"change_type": "article_refresh", "status": "improved"},
            {"change_type": "article_refresh", "status": "declined"},
            {"change_type": "article_refresh", "status": "collecting"},
            {"change_type": "technical_fix", "status": "flat"},
        ]
        assert win_rates(items) == [
            {"change_type": "article_refresh", "measured": 2, "improved": 1, "declined": 1},
            {"change_type": "technical_fix", "measured": 1, "improved": 0, "declined": 0},
        ]

    def test_no_measured_changes_gives_an_empty_list(self):
        assert win_rates([{"change_type": "x", "status": "collecting"}]) == []


class TestHeadline:
    def test_names_the_click_change_and_orders(self):
        text = headline({"clicks": 300, "clicks_prev": 200, "orders": 12, "revenue": 1500.0, "changes": 9})
        assert text == "Google sent 300 clicks, up 50% on the month before, and 12 organic orders worth $1,500. The app made 9 changes."

    def test_flat_month_with_no_orders_or_changes(self):
        text = headline({"clicks": 100, "clicks_prev": 100, "orders": 0, "revenue": 0, "changes": 0})
        assert text == "Google sent 100 clicks, level with the month before. The app made no changes."
