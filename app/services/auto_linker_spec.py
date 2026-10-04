"""Pure-logic tests for placing one internal link in a page body (no DB).

Run with: pytest app/services/auto_linker_spec.py
"""

from app.services.auto_linker import RELATED_CLASS, place_link

URL = "/products/zyn-chill"
BODY = "<h2>ZYN Chill review</h2><p>We tried ZYN Chill last week. It is mild.</p><p>Also see other pouches.</p>"


class TestPlaceLink:
    def test_links_the_first_mention_in_body_text_not_in_a_heading(self):
        html, how = place_link(BODY, URL, ["ZYN Chill"], "ZYN Chill Nicotine Pouches")
        assert how == "inline"
        assert '<h2>ZYN Chill review</h2>' in html
        assert '<p>We tried <a href="/products/zyn-chill">ZYN Chill</a> last week.' in html

    def test_tries_phrases_in_order_and_uses_the_first_found(self):
        html, how = place_link(BODY, URL, ["ZYN Chill Nicotine Pouches (5 Can Pack)", "zyn chill"], "label")
        assert how == "inline"
        assert html.count('<a href="/products/zyn-chill">') == 1

    def test_single_word_phrases_are_not_used_as_anchors(self):
        html, how = place_link(BODY, URL, ["ZYN"], "ZYN Chill Nicotine Pouches")
        assert how == "related"

    def test_no_mention_adds_a_related_line_at_the_end(self):
        html, how = place_link("<p>About hookah.</p>", URL, ["zyn chill"], "ZYN Chill Nicotine Pouches")
        assert how == "related"
        assert html == (
            f'<p>About hookah.</p><p class="{RELATED_CLASS}"><strong>Related:</strong> '
            '<a href="/products/zyn-chill">ZYN Chill Nicotine Pouches</a></p>')

    def test_a_second_related_link_joins_the_existing_line(self):
        first, _ = place_link("<p>About hookah.</p>", URL, [], "ZYN Chill")
        second, how = place_link(first, "/products/zyn-citrus", [], "ZYN Citrus")
        assert how == "related"
        assert second.count(f'class="{RELATED_CLASS}"') == 1
        assert second.endswith('<a href="/products/zyn-chill">ZYN Chill</a> · <a href="/products/zyn-citrus">ZYN Citrus</a></p>')

    def test_related_line_goes_before_a_faq_section(self):
        body = '<p>Text.</p><section class="a2z-blog__faq"><h2>FAQ</h2></section>'
        html, _ = place_link(body, URL, [], "ZYN Chill")
        assert html.index(RELATED_CLASS) < html.index("a2z-blog__faq")

    def test_page_that_already_links_the_target_is_left_alone(self):
        body = '<p>See <a href="/products/zyn-chill">this</a>.</p>'
        assert place_link(body, URL, ["zyn chill"], "ZYN Chill") == (body, "already")

    def test_label_is_escaped(self):
        html, _ = place_link("<p>x</p>", URL, [], "Black & Mild <Jazz>")
        assert "Black &amp; Mild &lt;Jazz&gt;" in html

    def test_a_collection_keyword_may_be_a_single_word(self):
        html, how = place_link("<p>Our cigarillos ship fast.</p>", "/collections/cigarillos", ["cigarillos"], "Cigarillos", min_words=1)
        assert how == "inline"
        assert '<a href="/collections/cigarillos">cigarillos</a>' in html

    def test_very_short_words_are_never_anchors(self):
        assert place_link("<p>Try zyn now.</p>", "/collections/zyn", ["zyn"], "ZYN", min_words=1, allow_related=False)[1] == "none"

    def test_inline_only_mode_does_not_add_a_related_line(self):
        body = "<p>About hookah.</p>"
        assert place_link(body, URL, ["zyn chill"], "ZYN Chill", allow_related=False) == (body, "none")


from app.services.auto_linker import collection_phrases  # noqa: E402


class TestCollectionPhrases:
    def test_keyword_naming_the_collection_is_usable(self):
        assert collection_phrases({"title": "Bulk Pipe Tobacco Blends", "handle": "pipe-tobacco", "keyword": "pipe tobacco"}) == [
            "Bulk Pipe Tobacco Blends", "pipe tobacco"]

    def test_one_word_keyword_needs_a_major_collection(self):
        small = {"title": "Mellow Cigars", "handle": "mellow", "keyword": "mellow", "products_count": 4}
        big = {"title": "Cigarillos & Small Cigars", "handle": "cigarillos", "keyword": "cigarillos", "products_count": 120}
        assert collection_phrases(small) == ["Mellow Cigars"]
        assert collection_phrases(big) == ["Cigarillos & Small Cigars", "cigarillos"]

    def test_plural_and_singular_count_as_the_same_word(self):
        assert "white owls" in collection_phrases({"title": "White Owl", "handle": "white-owl", "keyword": "white owls"})

    def test_keyword_broader_than_the_collection_is_dropped(self):
        phrases = collection_phrases({"title": "Grape Flavored Cigars", "handle": "grape-flavored-cigars", "keyword": "flavored cigars"})
        assert phrases == ["Grape Flavored Cigars"]

    def test_keyword_unrelated_to_a_pack_size_collection_is_dropped(self):
        phrases = collection_phrases({"title": "Filtered Cigars (10 Packs of 20)", "handle": "10-packs-of-20", "keyword": "filtered cigars"})
        assert phrases == ["Filtered Cigars (10 Packs of 20)"]
