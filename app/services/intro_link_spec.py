"""Pure-logic tests for the opening-paragraph collection link (no DB, no AI).

Run with: pytest app/services/intro_link_spec.py
"""

import re

from app.services.intro_link import (
    _extract_intro_html,
    anchor_matches,
    check_new_intro,
    collection_links,
    dropped_collection_links,
    generic_catalog_words,
    intro_link_status,
    locate_intro,
    pick_intro_collection,
    split_sentences,
    splice_intro,
)
from app.services.interlinking_analyzer import normalize_href_path

HOSTS = {"a2ztobacco.com", "www.a2ztobacco.com"}
CATALOG = {
    "blunt-wraps": {"title": "Blunt Wraps", "keyword": "blunt wraps"},
    "cigarillos": {"title": "Cigarillos", "keyword": "cigarillos"},
    "pipe-tobacco": {"title": "Pipe Tobacco", "keyword": "pipe tobacco"},
}
BLUNT = {"handle": "blunt-wraps", "keyword": "blunt wraps", "title": "Blunt Wraps"}
FILLER = "Rolling papers and wraps have changed a lot over the last few years for many smokers here."
ANSWER = "The best blunt wraps in 2026 are natural leaf wraps that burn slowly and evenly every time."


def _intro(link_sentence=2, anchor="blunt wraps", extra=""):
    sentences = [ANSWER, FILLER, "Each option below is ranked by burn, flavor, and price per pack."]
    link = f'browse our <a href="/collections/blunt-wraps">{anchor}</a> and skip the reading'
    sentences[link_sentence - 1] = sentences[link_sentence - 1].rstrip(".") + f", or {link}."
    return f"<p>{' '.join(sentences)}{extra}</p>"


class TestLocateIntro:
    def test_classic_body_intro_is_first_paragraph(self):
        body = "<p>Intro text.</p><h2>Next</h2><p>More.</p>"
        assert locate_intro(body) == (0, len("<p>Intro text.</p>"))

    def test_skips_leading_figure_and_empty_paragraph(self):
        lead = '<figure><img src="x.webp"></figure>\n<p> </p>\n'
        body = lead + "<p>Intro.</p><h2>A</h2>"
        assert locate_intro(body) == (len(lead), len(lead) + len("<p>Intro.</p>"))

    def test_visual_body_stops_before_takeaways(self):
        body = '<p>Intro.</p><div class="a2z-blog__takeaways"><p class="a2z-blog__label">Key</p></div>'
        assert locate_intro(body) == (0, len("<p>Intro.</p>"))

    def test_older_posts_opening_with_heading_or_wrapper_use_first_paragraph(self):
        heading, wrapper = "<h2 id='a'>Why X</h2> ", "<section> "
        assert [locate_intro(heading + "<p>x</p>"), locate_intro(wrapper + "<p>x</p></section>")] == [
            (len(heading), len(heading) + len("<p>x</p>")),
            (len(wrapper), len(wrapper) + len("<p>x</p>")),
        ]

    def test_body_opening_with_takeaways_block_has_no_intro(self):
        body = '<div class="a2z-blog__takeaways"><p class="a2z-blog__label">Key</p><ul><li>x</li></ul></div><p>x</p>'
        assert locate_intro(body) is None


class TestCollectionLinks:
    def test_finds_relative_and_absolute_collection_links_with_word_offsets(self):
        body = (
            '<p>One two three <a href="/products/x">product</a> four.</p>'
            '<p>Five <a href="https://a2ztobacco.com/collections/Blunt-Wraps/">blunt wraps</a>.</p>'
            '<p><a href="/collections/all">everything</a> <a href="https://other.com/collections/x">ext</a></p>'
        )
        assert [(l["handle"], l["anchor"], l["words_before"]) for l in collection_links(body, HOSTS)] == [
            ("blunt-wraps", "blunt wraps", 6),
        ]


class TestIntroLinkStatus:
    def test_ok_when_first_paragraph_links_collection_with_keyword_anchor(self):
        body = _intro() + "<h2>Next</h2><p>x</p>"
        assert intro_link_status(body, HOSTS, CATALOG)["status"] == "ok"

    def test_weak_anchor_when_first_paragraph_link_does_not_name_collection(self):
        body = _intro(anchor="these") + "<h2>Next</h2>"
        assert intro_link_status(body, HOSTS, CATALOG)["status"] == "weak_anchor"

    def test_needs_fix_reports_depth_and_audit_paragraph_flags(self):
        body = f"<p>{FILLER}</p><p>Second <a href='/collections/cigarillos'>cigarillos</a>.</p>"
        status = intro_link_status(body, HOSTS, CATALOG)
        assert {k: status[k] for k in ("status", "in_p1", "in_first_two", "words_before_first", "linked_handles")} == {
            "status": "needs_fix",
            "in_p1": False,
            "in_first_two": True,
            "words_before_first": len(FILLER.split()) + 1,
            "linked_handles": ["cigarillos"],
        }

    def test_no_collection_and_no_intro_statuses(self):
        assert [
            intro_link_status(f"<p>{FILLER}</p>", HOSTS, CATALOG)["status"],
            intro_link_status("<div class='a2z-blog__takeaways'></div><p>x</p>", HOSTS, CATALOG)["status"],
        ] == ["no_collection", "no_intro"]


class TestAnchorMatches:
    def test_plural_and_extra_words_still_match_keyword(self):
        assert [anchor_matches("our Blunt Wrap selection", "blunt wraps"), anchor_matches("wraps", "blunt wraps")] == [True, False]

    def test_two_thirds_keyword_coverage_matches(self):
        assert [anchor_matches("tobacco leaf wraps", "natural tobacco wraps"), anchor_matches("Hand-Rolled Tobacco", "hand rolled tobacco")] == [True, True]

    def test_descriptive_words_drawn_from_keyword_or_title_match(self):
        title = "Tobacco Blunt Wraps & Natural Leaf Wraps"
        assert [
            anchor_matches("best blunt wraps", "tobacco leaf blunt wraps", title),
            anchor_matches("Browse our full collection of Filtered Cigars", "little cigar", "Filtered Cigar Collection"),
        ] == [True, True]

    def test_anchor_naming_something_else_does_not_match(self):
        assert [
            anchor_matches("Drew Estate", "acid cigars", "Acid Premium Cigars"),
            anchor_matches("shisha shelf", "hookah tobacco flavors", "Hookah Tobacco & Shisha Flavors"),
        ] == [False, False]


class TestPickIntroCollection:
    def test_prefers_most_relevant_already_linked_collection(self):
        pick = pick_intro_collection("best blunt wraps", "Blunt Wraps Ranked", ["cigarillos", "blunt-wraps"], CATALOG)
        assert (pick["handle"], pick["mode"]) == ("blunt-wraps", "moved")

    def test_adds_collection_naming_the_topic_when_only_a_sub_brand_is_linked(self):
        catalog = {
            **CATALOG,
            "pipe-tobacco": {"title": "Bulk Pipe Tobacco Blends", "keyword": "tobacco bulk"},
            "arrowhead-pipe-tobacco": {"title": "Arrowhead Pipe Tobacco", "keyword": "arrowhead"},
        }
        pick = pick_intro_collection("top pipe tobacco brands", "Best Pipe Tobacco Brands", ["arrowhead-pipe-tobacco"], catalog)
        assert (pick["handle"], pick["mode"]) == ("pipe-tobacco", "added")

    def test_keeps_linked_collection_when_it_already_names_the_topic(self):
        catalog = {**CATALOG, "white-owl": {"title": "White Owl", "keyword": "white owl cigarillos"}}
        pick = pick_intro_collection("white owl flavors", "White Owl Cigarillos Ranked", ["white-owl"], catalog)
        assert (pick["handle"], pick["mode"]) == ("white-owl", "moved")

    def test_linked_collection_missing_only_a_category_word_still_counts_as_named(self):
        filler = {f"brand-{i}-cigarillos": {"title": f"Brand{i} Cigarillos", "keyword": ""} for i in range(5)}
        catalog = {
            **filler,
            "swisher-sweets-cigarillos": {"title": "Swisher Sweets Cigarillos", "keyword": "swisher cigarillos"},
            "swisher-blk": {"title": "Swisher BLK", "keyword": "swisher sweets"},
        }
        pick = pick_intro_collection("swisher sweets flavors", "Swisher Sweets Flavors Ranked", ["swisher-sweets-cigarillos"], catalog)
        assert (pick["handle"], pick["mode"]) == ("swisher-sweets-cigarillos", "moved")

    def test_prefers_most_general_named_collection_among_linked(self):
        catalog = {
            "backwoods-select-cigars": {"title": "Backwoods Select Cigars", "keyword": "backwoods flavors"},
            "backwoods-cigars": {"title": "Backwoods Cigars", "keyword": "backwoods"},
        }
        pick = pick_intro_collection("backwoods flavors", "Backwoods Flavors Ranked", list(catalog), catalog)
        assert (pick["handle"], pick["mode"]) == ("backwoods-cigars", "moved")

    def test_brand_collection_beats_generic_category_collection(self):
        filler = {f"x{i}-hookah-tobacco": {"title": f"X{i} Hookah Tobacco", "keyword": ""} for i in range(5)}
        catalog = {
            **filler,
            "hookah-tobacco": {"title": "Hookah Tobacco", "keyword": "hookah tobacco"},
            "fumari": {"title": "Fumari", "keyword": "fumari"},
        }
        pick = pick_intro_collection("", "Fumari Adds Three Hookah Tobacco Flavors", ["hookah-tobacco", "fumari"], catalog)
        assert (pick["handle"], pick["mode"]) == ("fumari", "moved")

    def test_adds_best_catalog_collection_when_nothing_linked_is_relevant(self):
        pick = pick_intro_collection("pipe tobacco storage", "Keep Pipe Tobacco Fresh", ["cigarillos"], CATALOG)
        assert (pick["handle"], pick["mode"]) == ("pipe-tobacco", "added")

    def test_falls_back_to_first_linked_when_nothing_is_relevant(self):
        pick = pick_intro_collection("zyn pouches", "Do You Spit With ZYN", ["cigarillos"], CATALOG)
        assert (pick["handle"], pick["mode"]) == ("cigarillos", "moved")

    def test_returns_none_without_links_or_relevant_catalog(self):
        assert pick_intro_collection("zyn pouches", "ZYN", [], CATALOG) is None


class TestGenericCatalogWords:
    def test_words_in_many_collection_names_are_generic(self):
        catalog = {f"brand{i}-pipe-tobacco": {"title": f"Brand{i} Pipe Tobacco", "keyword": ""} for i in range(5)}
        assert generic_catalog_words(catalog) == {"pipe", "tobacco"}


class TestSplitSentences:
    def test_keeps_abbreviations_and_quoted_endings_together(self):
        text = 'Sales in the U.S. rose. He said "stop." Then 3 more came.'
        assert split_sentences(text) == ["Sales in the U.S. rose.", 'He said "stop."', "Then 3 more came."]


class TestExtractIntroHtml:
    def test_collapses_nested_paragraphs_and_unbolds_links(self):
        raw = '```html\n<p><p>Hi <strong><a href="/collections/x">x y</a></strong>.</p></p>\n```'
        assert _extract_intro_html(raw) == '<p>Hi <a href="/collections/x">x y</a>.</p>'


class TestCheckNewIntro:
    def _codes(self, new, old="<p>Old intro.</p>", query="best blunt wraps"):
        return sorted({v["code"] for v in check_new_intro(old, new, BLUNT, query, HOSTS)})

    def test_valid_intro_passes(self):
        assert self._codes(_intro(2)) == []

    def test_link_in_first_or_fourth_sentence_is_misplaced(self):
        first = _intro(1)
        fourth = "<p>" + " ".join([ANSWER, FILLER, FILLER, 'Or browse our <a href="/collections/blunt-wraps">blunt wraps</a>.']) + "</p>"
        assert [self._codes(first), self._codes(fourth)] == [["placement"], ["placement"]]

    def test_missing_or_duplicated_target_link(self):
        twice = _intro(2, extra=' Also <a href="/collections/blunt-wraps">blunt wraps</a> again.')
        assert [self._codes(f"<p>{ANSWER} {FILLER} {FILLER}</p>"), self._codes(twice)] == [["target_count"], ["target_count"]]

    def test_anchor_must_name_the_collection(self):
        assert self._codes(_intro(2, anchor="the good stuff")) == ["anchor"]

    def test_target_must_be_first_collection_link_but_old_collection_links_may_go(self):
        old = '<p>Old <a href="/collections/cigarillos">cigarillos</a>.</p>'
        other_first = _intro(2).replace("<p>" + ANSWER, '<p>' + ANSWER.replace("natural leaf wraps", '<a href="/collections/cigarillos">cigarillos</a> wraps'))
        assert [self._codes(_intro(2), old=old), self._codes(other_first, old=old)] == [[], ["target_not_first"]]

    def test_existing_intro_links_must_survive_and_no_new_links_added(self):
        old = '<p>Old <a href="/products/zig-zag">Zig-Zag papers</a>.</p>'
        new_link = _intro(2, extra=' See <a href="/products/other">other</a> today.')
        assert [self._codes(_intro(2), old=old), self._codes(new_link)] == [["dropped_link"], ["new_link"]]

    def test_back_to_back_links_rejected(self):
        extra = ' Try <a href="/products/a">wrap a</a>, <a href="/products/b">wrap b</a> now.'
        old = '<p><a href="/products/a">a</a> x <a href="/products/b">b</a></p>'
        assert self._codes(_intro(2, extra=extra), old=old) == ["adjacent_links"]

    def test_length_and_query_presence(self):
        short = '<p>Wraps. Browse our <a href="/collections/blunt-wraps">blunt wraps</a>.</p>'
        assert [self._codes(short, query=""), self._codes(_intro(2), query="humidor maintenance schedule")] == [
            ["length"], ["query_missing"],
        ]

    def test_non_paragraph_markup_rejected(self):
        assert self._codes("<h2>Hi</h2>" + _intro(2)) == ["format"]


class TestDroppedCollectionLinks:
    def test_reports_old_collection_links_missing_from_rewrite(self):
        old = '<p><a href="/collections/cigarillos">c</a> <a href="/products/x">p</a> <a href="/collections/blunt-wraps">b</a></p>'
        assert dropped_collection_links(old, _intro(2), "blunt-wraps", HOSTS) == ["/collections/cigarillos"]


class TestSpliceIntro:
    BODY = (
        "<p>Old intro.</p><h2>Section</h2>"
        '<p>Text <a href="/collections/blunt-wraps">wraps here</a> and '
        '<a href="/products/x">product</a>.</p>'
        '<p>End <a href="https://a2ztobacco.com/collections/blunt-wraps/">again</a>.</p>'
    )

    def test_moves_link_by_unwrapping_later_copies_only(self):
        new_intro = _intro(2)
        span = locate_intro(self.BODY)
        out, unwrapped = splice_intro(self.BODY, span, new_intro, "blunt-wraps", HOSTS)
        assert (out, unwrapped) == (
            new_intro + "<h2>Section</h2>"
            '<p>Text wraps here and <a href="/products/x">product</a>.</p><p>End again.</p>',
            2,
        )

    def test_target_linked_once_and_other_links_untouched(self):
        span = locate_intro(self.BODY)
        out, _ = splice_intro(self.BODY, span, _intro(2), "blunt-wraps", HOSTS)

        def others(html):
            return [normalize_href_path(h) for h in re.findall(r'href="([^"]+)"', html) if "blunt-wraps" not in h]

        assert ([l["handle"] for l in collection_links(out, HOSTS)], others(out)) == (["blunt-wraps"], others(self.BODY))
