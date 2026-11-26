import pytest
import itertools

from app.services.impact_verdict import impact_verdict

DAYS = 14


class TestImpactVerdict:
    def test_one_extra_click_on_a_tiny_page_is_flat(self):
        assert impact_verdict(2, DAYS, 3, DAYS) == ("flat", 50.0)

    def test_first_clicks_after_none_are_flat_until_clearly_above_noise(self):
        assert impact_verdict(0, DAYS, 3, DAYS) == ("flat", None)

    def test_clear_rise_from_zero_is_improved(self):
        assert impact_verdict(0, DAYS, 12, DAYS) == ("improved", None)

    def test_doubling_on_a_busy_page_is_improved(self):
        assert impact_verdict(140, DAYS, 280, DAYS) == ("improved", 100.0)

    def test_halving_on_a_busy_page_is_declined(self):
        assert impact_verdict(280, DAYS, 140, DAYS) == ("declined", -50.0)

    def test_five_percent_on_a_huge_page_is_flat_even_if_significant(self):
        assert impact_verdict(14000, DAYS, 14700, DAYS)[0] == "flat"

    def test_unequal_windows_compare_rates_not_totals(self):
        assert impact_verdict(140, DAYS, 70, 7) == ("flat", 0.0)

    def test_empty_window_is_rejected(self):
        with pytest.raises(ValueError):
            impact_verdict(5, 0, 5, DAYS)


COUNTS = (0, 1, 3, 10, 40, 150, 900)
SPANS = (7, 14, 28)


@pytest.mark.parametrize("pre,pre_days,post,post_days", itertools.product(COUNTS, SPANS, COUNTS, SPANS))
def test_swapping_windows_never_flips_the_direction(pre, pre_days, post, post_days):
    forward, _ = impact_verdict(pre, pre_days, post, post_days)
    backward, _ = impact_verdict(post, post_days, pre, pre_days)
    assert (forward, backward) not in {("improved", "improved"), ("declined", "declined")}
