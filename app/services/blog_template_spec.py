import pytest

from app.services.blog_template import (
    blog_section_marker,
    contrast_ratio,
    generate_blog_section,
    get_article_css,
    link_color_warnings,
    readable_color,
)


class TestFaqMarkupGate:
    def test_off_leaves_no_faq_jsonld(self):
        section = generate_blog_section(emit_faq=False)
        assert ("faq_jsonld" in section, blog_section_marker(False) in section) == (False, True)

    def test_on_prints_the_stored_faq(self):
        section = generate_blog_section(emit_faq=True)
        assert ("article.metafields.global.faq_jsonld" in section, blog_section_marker(True) in section) == (True, True)

    def test_markers_tell_the_two_apart(self):
        assert (blog_section_marker(True) in generate_blog_section(emit_faq=False),
                blog_section_marker(False) in generate_blog_section(emit_faq=True)) == (False, False)


class TestReadableColor:
    @pytest.mark.parametrize("brand", ["#0479e7", "#ff9900", "#22c55e", "#ffffff"])
    def test_result_reads_at_4_5_on_white(self, brand):
        assert contrast_ratio(readable_color(brand), "#ffffff") >= 4.5

    def test_a_passing_color_is_unchanged(self):
        assert readable_color("#1d4ed8") == "#1d4ed8"

    def test_darkening_keeps_the_hue_ratio(self):
        r, g, b = (int(readable_color("#0479e7")[i:i + 2], 16) for i in (1, 3, 5))
        assert (r < g < b, abs(g / b - 0x79 / 0xe7) < 0.02) == (True, True)

    def test_published_css_uses_the_readable_color(self):
        css = get_article_css("#0479e7")
        assert (readable_color("#0479e7") in css, "#0479e7" in css) == (True, False)


class TestLinkColorWarnings:
    def test_passing_color_has_no_note(self):
        assert link_color_warnings("#1d4ed8") == []

    def test_failing_color_names_the_darker_one_used(self):
        notes = link_color_warnings("#0479e7")
        assert len(notes) == 1 and readable_color("#0479e7") in notes[0]
