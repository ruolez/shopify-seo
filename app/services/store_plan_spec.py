"""Pure-logic tests for store goals and the weekly plan (no DB)."""

from datetime import date

import pytest

from app.services.store_plan import family_progress, goal_change, plan_lines, week_bounds


def kw(keyword, clicks, impressions, position):
    return {"keyword": keyword, "clicks": clicks, "impressions": impressions, "position": position}


class TestFamilyProgress:
    def test_position_is_weighted_by_impressions(self):
        rows = [kw("backwoods", 10, 300, 4.0), kw("backwoods honey", 2, 100, 12.0)]
        assert family_progress(rows) == {"clicks": 12, "impressions": 400, "position": 6.0, "top10": 1}

    def test_no_rows(self):
        assert family_progress([]) == {"clicks": 0, "impressions": 0, "position": None, "top10": 0}


class TestGoalChange:
    def test_lower_position_shows_as_negative_change(self):
        now = {"clicks": 20, "impressions": 500, "position": 5.0, "top10": 3}
        before = {"clicks": 12, "impressions": 450, "position": 7.5, "top10": 1}
        assert goal_change(now, before) == {"clicks": 8, "impressions": 50, "position": -2.5, "top10": 2}

    def test_unknown_position_gives_no_position_change(self):
        now = {"clicks": 1, "impressions": 10, "position": 9.0, "top10": 1}
        before = {"clicks": 0, "impressions": 0, "position": None, "top10": 0}
        assert goal_change(now, before)["position"] is None


class TestWeekBounds:
    @pytest.mark.parametrize("today", [date(2026, 11, 23), date(2026, 11, 26), date(2026, 11, 29)])
    def test_monday_to_sunday(self, today):
        assert week_bounds(today) == (date(2026, 11, 23), date(2026, 11, 29))


class TestPlanLines:
    def test_fixes_first_then_approvals_publishing_links_duplicates(self):
        plan = plan_lines([{"label": "Fix A", "href": "/a", "gain": 16}],
                          {"this_week": 2, "waiting_approval": 1}, links_pending=4, links_auto=False,
                          duplicate_pairs=3, changed_this_week=9)
        assert ([l["kind"] for l in plan["lines"]], plan["changed_this_week"]) == (
            ["fix", "approve", "publish", "links", "duplicates"], 9)

    def test_links_are_not_a_task_when_added_automatically(self):
        plan = plan_lines([], {}, links_pending=4, links_auto=True, duplicate_pairs=0, changed_this_week=0)
        assert plan["lines"] == []

    def test_singular_wording(self):
        plan = plan_lines([], {"waiting_approval": 1}, 0, False, 1, 0)
        assert [l["text"] for l in plan["lines"]] == [
            "Approve 1 scheduled post so it can go live", "Merge or keep 1 pair of duplicate collections"]
