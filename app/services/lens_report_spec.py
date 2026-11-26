"""Pure-logic tests for the Google Lens image checks (no DB)."""

import pytest

from app.services.lens_report import image_issues

TITLE = "ZYN Wintergreen Nicotine Pouches - 5 Cans"


def img(alt):
    return {"url": "https://cdn/x.jpg", "alt": alt}


class TestImageIssues:
    def test_good_images_have_no_issues(self):
        assert image_issues(TITLE, [img("ZYN Wintergreen nicotine pouches 5-can roll"), img("Close-up of a can")]) == []

    def test_no_images(self):
        assert image_issues(TITLE, []) == ["no_images"]

    @pytest.mark.parametrize("alt", ["", "   ", None])
    def test_blank_alt_counts_as_missing(self, alt):
        assert image_issues(TITLE, [img("ZYN Wintergreen pouches"), img(alt)]) == ["missing_alt"]

    def test_first_alt_that_does_not_name_the_product(self):
        assert image_issues(TITLE, [img("Close-up detail of tobacco-free cans"), img("ZYN Wintergreen")]) == ["first_alt_off_name"]

    def test_single_image_is_one_angle(self):
        assert image_issues(TITLE, [img("ZYN Wintergreen nicotine pouches")]) == ["one_image"]

    def test_missing_first_alt_is_reported_once(self):
        assert image_issues(TITLE, [img(""), img("ZYN Wintergreen")]) == ["missing_alt"]
