"""The GEO quote guard keeps real quotes and links (no DB)."""

from app.ai_agent import _strip_unverifiable_quotes

SOURCE = 'Supplier note: "With an easy pull and an even burn your expectations will be transcended every time."'


class TestQuoteGuard:
    def test_paragraph_with_a_link_and_no_quote_stays(self):
        html = '<p>Shop our <a href="/collections/filter-tubes-and-more">filter tubes collection</a> today.</p>'
        assert _strip_unverifiable_quotes(html, SOURCE) == (html, [])

    def test_invented_quote_takes_its_paragraph(self):
        html = '<p>Experts say "these are the finest tubes ever produced anywhere".</p><p>Keep.</p>'
        assert _strip_unverifiable_quotes(html, SOURCE) == ("<p>Keep.</p>", ["these are the finest tubes ever produced anywhere"])

    def test_quote_found_in_the_source_stays(self):
        html = '<p>"With an easy pull and an even burn your expectations will be transcended every time."</p>'
        assert _strip_unverifiable_quotes(html, SOURCE) == (html, [])
