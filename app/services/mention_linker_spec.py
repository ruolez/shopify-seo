"""Pure-logic tests for the exact-mention guarantee pass (no DB, no AI).

Run with: pytest app/services/mention_linker_spec.py
"""

from app.services.mention_linker import find_verbatim_mentions, link_unlinked_mentions

CAPTAIN = {"title": "Captain Black", "url": "/collections/captain-black"}
BORKUM = {"title": "Borkum Riff", "url": "/collections/borkum-riff"}


class TestFindVerbatimMentions:
    def test_returns_only_candidates_present_in_visible_text(self):
        html = "<p>Sales of Captain Black pipe tobacco rose this quarter.</p>"
        assert find_verbatim_mentions(html, [CAPTAIN, BORKUM]) == [CAPTAIN]

    def test_matches_case_insensitively_and_across_whitespace(self):
        html = "<p>demand for CAPTAIN\n  BLACK products</p>"
        assert find_verbatim_mentions(html, [CAPTAIN]) == [CAPTAIN]

    def test_does_not_match_across_tag_boundaries(self):
        html = "<p>Captain</p><p>Black</p>"
        assert find_verbatim_mentions(html, [CAPTAIN]) == []


class TestLinkUnlinkedMentions:
    def test_links_first_body_mention_of_unlinked_candidate(self):
        html = "<h2>Captain Black Results</h2><p>Captain Black grew 5%. Captain Black remains core.</p>"
        out, added = link_unlinked_mentions(html, [CAPTAIN])
        assert out == (
            '<h2>Captain Black Results</h2><p><a href="/collections/captain-black">'
            "Captain Black</a> grew 5%. Captain Black remains core.</p>"
        )
        assert added == [CAPTAIN]

    def test_skips_mentions_inside_existing_links_headings_and_figcaptions(self):
        html = (
            '<h3>Captain Black</h3><a href="/x">Captain Black</a>'
            "<figcaption>Captain Black tins</figcaption>"
        )
        out, added = link_unlinked_mentions(html, [CAPTAIN])
        assert (out, added) == (html, [])

    def test_skips_candidate_whose_url_is_already_linked(self):
        html = '<p><a href="/collections/captain-black">the brand</a> and Captain Black.</p>'
        out, added = link_unlinked_mentions(html, [CAPTAIN])
        assert (out, added) == (html, [])

    def test_respects_total_internal_link_cap(self):
        existing = '<a href="/products/a">x</a>' * 12
        html = f"<p>{existing} Captain Black</p>"
        out, added = link_unlinked_mentions(html, [CAPTAIN], max_total_links=12)
        assert (out, added) == (html, [])

    def test_skips_single_word_titles_by_default(self):
        html = "<p>Fine cigarillos on sale.</p>"
        out, added = link_unlinked_mentions(html, [{"title": "Cigarillos", "url": "/collections/cigarillos"}])
        assert (out, added) == (html, [])

    def test_word_boundaries_prevent_partial_word_matches(self):
        html = "<p>The Captain Blackbeard story.</p>"
        out, added = link_unlinked_mentions(html, [CAPTAIN])
        assert (out, added) == (html, [])

    def test_matches_entity_encoded_ampersand_titles(self):
        cand = {"title": "Papers & Wraps", "url": "/collections/papers-wraps"}
        html = "<p>Our Papers &amp; Wraps range expanded.</p>"
        out, added = link_unlinked_mentions(html, [cand])
        assert added == [cand]
        assert '<a href="/collections/papers-wraps">Papers &amp; Wraps</a>' in out

    def test_earlier_candidates_take_priority_under_the_cap(self):
        existing = '<a href="/products/a">x</a>' * 11
        html = f"<p>{existing} Captain Black and Borkum Riff</p>"
        out, added = link_unlinked_mentions(html, [CAPTAIN, BORKUM], max_total_links=12)
        assert added == [CAPTAIN]

    def test_preserves_inline_image_tokens(self):
        html = "<p>Captain Black news.</p>[[INLINE_IMAGE_1]]<p>More text.</p>"
        out, _ = link_unlinked_mentions(html, [CAPTAIN])
        assert "[[INLINE_IMAGE_1]]" in out
