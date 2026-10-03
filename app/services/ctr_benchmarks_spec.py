"""Pure-logic tests for click-gain estimates (explicit curve; no DB).

Run with: pytest app/services/ctr_benchmarks_spec.py
"""

from app.services.ctr_benchmarks import estimate_click_gain, target_position

CURVE = {1: 0.20, 3: 0.10, 5: 0.05, 10: 0.02, 20: 0.01, 999: 0.004}


class TestTargetPosition:
    def test_page_one_pages_aim_for_the_top_three(self):
        assert target_position(7.4) == 3

    def test_page_two_pages_aim_for_page_one(self):
        assert target_position(14) == 8

    def test_unranked_pages_aim_for_page_one(self):
        assert target_position(None) == 8
        assert target_position(0) == 8


class TestEstimateClickGain:
    def test_gain_is_the_ctr_difference_between_current_and_target_position(self):
        # pos 8 (2%) -> pos 3 (10%) on 1,000 monthly impressions
        assert estimate_click_gain(1000, 8, CURVE) == 80

    def test_actual_ctr_replaces_the_expected_one_when_known(self):
        # already earning 5% at pos 8; target 10%
        assert estimate_click_gain(1000, 8, CURVE, actual_ctr=0.05) == 50

    def test_a_page_already_at_or_above_target_ctr_has_no_gain(self):
        assert estimate_click_gain(1000, 2, CURVE) == 0
        assert estimate_click_gain(1000, 8, CURVE, actual_ctr=0.30) == 0

    def test_no_volume_no_gain(self):
        assert estimate_click_gain(0, 8, CURVE) == 0
        assert estimate_click_gain(None, 8, CURVE) == 0
