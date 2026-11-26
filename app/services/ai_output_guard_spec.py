import pytest

from app.services.ai_output_guard import drop_empty_headings, empty_sections


class TestEmptySections:
    def test_headings_with_nothing_under_them(self):
        html = ("<p>Intro</p><h2>How does it burn?</h2>\n\n<h2>Why menthol?</h2>\n"
                "<h2>Is it good for RYO?</h2><p>Yes.</p><h3>The bottom line</h3><p>Good.</p>")
        assert empty_sections(html) == ["How does it burn?", "Why menthol?"]

    def test_trailing_empty_heading(self):
        assert empty_sections("<p>Intro</p><h2>Sizes</h2>  ") == ["Sizes"]

    def test_heading_followed_by_subheading_with_content_is_not_empty(self):
        assert empty_sections("<h2>Flavors</h2><h3>Grape</h3><p>Sweet.</p>") == []

    @pytest.mark.parametrize("body", ["<ul><li>One</li></ul>", "<table><tr><td>x</td></tr></table>",
                                      '<img src="a.jpg" alt="">'])
    def test_lists_tables_and_images_count_as_content(self, body):
        assert empty_sections(f"<h2>Details</h2>{body}") == []

    def test_no_headings(self):
        assert empty_sections("<p>Just text</p>") == []


class TestDropEmptyHeadings:
    def test_removes_only_new_empty_headings(self):
        html = "<h2>Kept empty</h2><h2>Emptied</h2>\n\n<h2>Full</h2><p>Text</p>"
        assert drop_empty_headings(html, keep=["Kept empty"]) == "<h2>Kept empty</h2>\n\n<h2>Full</h2><p>Text</p>"

    def test_parent_emptied_by_dropping_its_last_subheading_goes_too(self):
        assert drop_empty_headings("<p>Intro</p><h2>Section</h2><h3>Sub</h3>") == "<p>Intro</p>"
