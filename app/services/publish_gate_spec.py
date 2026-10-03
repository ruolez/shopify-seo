"""Pure-logic tests for the shared publish cadence (no DB).

Run with: pytest app/services/publish_gate_spec.py
"""

from datetime import datetime, time

from app.services.publish_gate import pick_slot, pick_slots

NINE = time(9, 0)
GAP = 2
MORNING = datetime(2026, 10, 5, 7, 0)
AFTERNOON = datetime(2026, 10, 5, 15, 0)


class TestPickSlot:
    def test_empty_queue_before_publish_time_uses_today(self):
        assert pick_slot(MORNING, NINE, GAP, []) == datetime(2026, 10, 5, 9, 0)

    def test_empty_queue_after_publish_time_uses_tomorrow(self):
        assert pick_slot(AFTERNOON, NINE, GAP, []) == datetime(2026, 10, 6, 9, 0)

    def test_keeps_the_gap_after_the_most_recent_publish(self):
        published_yesterday = datetime(2026, 10, 4, 16, 57)
        assert pick_slot(MORNING, NINE, GAP, [published_yesterday]) == datetime(2026, 10, 6, 9, 0)

    def test_appends_after_a_contiguous_queue(self):
        queue = [datetime(2026, 10, 5, 9, 0), datetime(2026, 10, 7, 9, 0)]
        assert pick_slot(MORNING, NINE, GAP, queue) == datetime(2026, 10, 9, 9, 0)

    def test_fills_the_first_hole_that_respects_the_gap_on_both_sides(self):
        queue = [datetime(2026, 10, 5, 9, 0), datetime(2026, 10, 9, 9, 0)]
        assert pick_slot(MORNING, NINE, GAP, queue) == datetime(2026, 10, 7, 9, 0)

    def test_far_future_parked_posts_do_not_push_new_slots_past_them(self):
        parked = [datetime(2027, 8, 26, 13, 1), datetime(2027, 8, 27, 13, 1)]
        assert pick_slot(MORNING, NINE, GAP, parked) == datetime(2026, 10, 5, 9, 0)

    def test_gap_of_one_day_allows_consecutive_days(self):
        queue = [datetime(2026, 10, 5, 9, 0)]
        assert pick_slot(MORNING, NINE, 1, queue) == datetime(2026, 10, 6, 9, 0)


class TestPickSlots:
    def test_batch_slots_are_spaced_by_the_gap(self):
        assert pick_slots(MORNING, NINE, GAP, [], 3) == [
            datetime(2026, 10, 5, 9, 0),
            datetime(2026, 10, 7, 9, 0),
            datetime(2026, 10, 9, 9, 0),
        ]

    def test_zero_count_returns_no_slots(self):
        assert pick_slots(MORNING, NINE, GAP, [], 0) == []
