"""Pure-logic tests for duplicate-collection detection (no DB)."""

import pytest

from app.services.collection_duplicates import find_pairs, keeper_rank, pair_reason, title_core


def coll(cid, title, products, **kw):
    return {"id": cid, "title": title, "products": set(products), "collection_type": "manual", **kw}


class TestTitleCore:
    @pytest.mark.parametrize("a,b", [
        ("Half & Half Tobacco", "Half & Half Pipe Tobacco"),
        ("Swisher Sweets Cigarillos", "Swisher Sweet Cigarillo"),
        ("Zig-Zag Papers", "Zig Zag Rolling Paper Collection"),
    ])
    def test_category_words_and_plurals_do_not_separate_names(self, a, b):
        assert title_core(a) == title_core(b)

    def test_a_pure_category_title_has_no_core(self):
        assert title_core("Pipe Tobacco") == frozenset()


class TestPairReason:
    def test_same_name_with_shared_products_is_a_duplicate(self):
        assert pair_reason(coll(1, "Half & Half Tobacco", "abcd"), coll(2, "Half & Half Pipe Tobacco", "ab")) == "same_name"

    def test_same_name_without_shared_products_is_not(self):
        assert pair_reason(coll(1, "Half & Half Tobacco", "abcd"), coll(2, "Half & Half Pipe Tobacco", "xy")) is None

    def test_nearly_the_same_products_and_a_shared_word_is_a_duplicate(self):
        assert pair_reason(coll(1, "Filter Tubes", "abcde"), coll(2, "Wholesale Cigarette Filter Tubes", "abcd")) == "same_products"

    def test_same_products_under_unrelated_names_is_not(self):
        assert pair_reason(coll(1, "Smoking Accessories", "abcde"), coll(2, "Flavor Profiles", "abcd")) is None

    def test_same_name_but_one_is_much_broader_is_not(self):
        narrow = coll(1, "Top Filter Tubes", "abcd")
        broad = coll(2, "Top Filter Tubes & Tobacco Products", "abcdefghijklmnop")
        assert pair_reason(narrow, broad) is None

    @pytest.mark.parametrize("small", ["ab", "a"])
    def test_a_brand_inside_a_category_is_not_a_duplicate(self, small):
        assert pair_reason(coll(1, "Cigars", "abcdefghij"), coll(2, "Swisher Sweets Cigars", small)) is None

    def test_two_category_collections_with_no_core_need_matching_products(self):
        assert pair_reason(coll(1, "Pipe Tobacco", "abcdef"), coll(2, "Hookah Tobacco", "ab")) is None

    def test_empty_collection_is_never_a_duplicate(self):
        assert pair_reason(coll(1, "Half & Half Tobacco", ""), coll(2, "Half & Half", "ab")) is None


class TestKeeper:
    def test_a_click_or_two_is_a_tie_so_store_made_wins(self):
        assert keeper_rank(coll(1, "A", "ab", clicks=3, generated=False)) > keeper_rank(coll(2, "A", "ab", clicks=4, generated=True))

    def test_indexed_beats_more_clicks_and_products(self):
        indexed = coll(1, "A", "ab", indexed=True, clicks=0)
        busy = coll(2, "A", "abcdef", indexed=False, clicks=50)
        assert keeper_rank(indexed) > keeper_rank(busy)

    def test_store_made_beats_app_generated_when_otherwise_equal(self):
        assert keeper_rank(coll(1, "A", "ab", generated=False)) > keeper_rank(coll(2, "A", "ab", generated=True))


class TestFindPairs:
    def test_half_and_half_keeps_the_indexed_one(self):
        pairs = find_pairs([
            coll(10197, "Half & Half Tobacco", "abcd", indexed=False, generated=True, clicks=0),
            coll(6991, "Half & Half Pipe Tobacco", "ab", indexed=True, generated=False, clicks=3),
        ])
        assert [(p["keep"]["id"], p["drop"]["id"], p["reason"], p["shared"]) for p in pairs] == [(6991, 10197, "same_name", 2)]

    def test_a_collection_joins_only_its_strongest_pair(self):
        pairs = find_pairs([
            coll(1, "Game Cigarillos", "abcd"),
            coll(2, "Game Leaf Cigarillos", "abcd"),
            coll(3, "Game", "abc"),
        ])
        ids = [sorted((p["keep"]["id"], p["drop"]["id"])) for p in pairs]
        assert ids == [[1, 2]]
