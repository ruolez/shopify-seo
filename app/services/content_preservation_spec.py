"""Pure-logic tests for handing existing content to a rewriting model (no DB, no AI).

Run with: pytest app/services/content_preservation_spec.py
"""

from app.services.content_preservation import (
    MAX_EXISTING_CHARS,
    MIN_WORDS_WORTH_PRESERVING,
    existing_description_block,
)

PRESERVE_RULE = "- PRESERVE EXISTING CONTENT: keep tables and links."
SHORT_HTML = "<p>Mild cigars.</p>"
RICH_HTML = (
    "<h2>What is it?</h2><p>"
    + " ".join(["leaf"] * MIN_WORDS_WORTH_PRESERVING)
    + '</p><table><tr><td>Count</td><td>20</td></tr></table>'
    + '<p>See <a href="/collections/cigarillos">cigarillos</a>.</p>'
)


class TestExistingDescriptionBlock:
    def test_returns_none_for_empty_description(self):
        assert existing_description_block("", PRESERVE_RULE) is None
        assert existing_description_block(None, PRESERVE_RULE) is None

    def test_returns_none_when_description_is_too_thin_to_be_worth_keeping(self):
        assert existing_description_block(SHORT_HTML, PRESERVE_RULE) is None

    def test_word_floor_counts_text_not_markup(self):
        markup_heavy = "<div class='a b c d e f g h'>" * MIN_WORDS_WORTH_PRESERVING + "<p>one two</p>"
        assert existing_description_block(markup_heavy, PRESERVE_RULE) is None

    def test_block_carries_the_full_html_and_the_preserve_rule(self):
        block = existing_description_block(RICH_HTML, PRESERVE_RULE)
        assert RICH_HTML in block
        assert block.endswith(PRESERVE_RULE)

    def test_block_tells_the_model_to_revise_not_restart(self):
        block = existing_description_block(RICH_HTML, PRESERVE_RULE)
        assert block.startswith("CURRENT DESCRIPTION (HTML)")
        assert "do not start over" in block

    def test_oversized_html_is_cut_at_the_cap(self):
        huge = "<p>" + "word " * MAX_EXISTING_CHARS + "</p>"
        block = existing_description_block(huge, PRESERVE_RULE)
        html_part = block.split("\n", 1)[1].rsplit("\n\n", 1)[0]
        assert len(html_part) == MAX_EXISTING_CHARS


from app.services.content_preservation import missing_preserved  # noqa: E402

OLD_BODY = (
    "<h2>What is a Backwoods cigar?</h2><p>Intro copy.</p>"
    "<h2>Specifications</h2><table><tr><td>Count</td><td>5</td></tr></table>"
    "<h2>Expert Verdict</h2><p>Solid.</p>"
    '<p>Browse <a href="/collections/backwoods">Backwoods</a> and '
    '<a href="https://a2ztobacco.com/products/honey-berry?ref=x">Honey Berry</a>.</p>'
    '<p><a href="https://example.com/study">a study</a></p>'
)


class TestMissingPreserved:
    def test_identical_body_loses_nothing(self):
        assert missing_preserved(OLD_BODY, OLD_BODY) == []

    def test_empty_old_body_has_nothing_to_lose(self):
        assert missing_preserved("", "<p>New.</p>") == []

    def test_reports_a_dropped_table(self):
        new = OLD_BODY.replace("<table><tr><td>Count</td><td>5</td></tr></table>", "<p>Count: 5</p>")
        assert missing_preserved(OLD_BODY, new) == ["1 table"]

    def test_reports_dropped_internal_links_by_path(self):
        new = OLD_BODY.replace('<a href="/collections/backwoods">Backwoods</a>', "Backwoods")
        assert missing_preserved(OLD_BODY, new) == ["internal link /collections/backwoods"]

    def test_internal_link_survives_a_change_of_host_or_query(self):
        new = OLD_BODY.replace("https://a2ztobacco.com/products/honey-berry?ref=x", "/products/honey-berry")
        assert missing_preserved(OLD_BODY, new) == []

    def test_external_links_are_not_protected(self):
        new = OLD_BODY.replace('<p><a href="https://example.com/study">a study</a></p>', "")
        assert missing_preserved(OLD_BODY, new) == []

    def test_reports_dropped_verdict_and_specifications_sections(self):
        new = (
            "<h2>About</h2><p>Intro copy.</p><table><tr><td>Count</td><td>5</td></tr></table>"
            '<p><a href="/collections/backwoods">Backwoods</a> '
            '<a href="/products/honey-berry">Honey Berry</a></p>'
        )
        assert missing_preserved(OLD_BODY, new) == ["Specifications section", "Expert Verdict section"]
