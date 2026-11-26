import pytest

from app.services.unindexed_links import SOURCE_CAP, phrases_for, plan_links, score_source, text_of


def _target(**kw):
    base = {"type": "product", "id": 1, "title": "Swisher Sweets Grape Cigarillos 60ct",
            "vendor": "Swisher Sweets", "phrases": ["Swisher Sweets Grape Cigarillos 60ct", "grape swisher"],
            "priority": 1}
    return {**base, **kw}


def _source(**kw):
    base = {"type": "article", "id": 10, "title": "Swisher Sweets flavors ranked",
            "text": "every swisher flavor we tried", "clicks": 40, "indexed": True, "vendor": None}
    return {**base, **kw}


class TestTextOf:
    def test_strips_tags_and_lowercases(self):
        assert text_of("<p>Try <b>Grape</b> Swisher</p>") == " try  grape  swisher "


class TestScoreSource:
    def test_category_keyword_in_the_text_is_not_a_mention(self):
        scored = score_source(_target(), _source(text="the best grape swisher around"))
        assert scored[1] == "About the same brand (swisher sweets)"

    def test_article_about_the_brand_qualifies(self):
        scored = score_source(_target(), _source())
        assert scored is not None and scored[1] == "About the same brand (swisher sweets)"

    def test_article_naming_the_product_ranks_above_brand_only(self):
        named = score_source(_target(), _source(text="our pick is the swisher sweets grape cigarillos 60ct for sure"))
        brand_only = score_source(_target(), _source())
        assert (named[1], named[0] > brand_only[0]) == ("Already names it, so those words become the link", True)

    def test_unrelated_article_is_skipped(self):
        assert score_source(_target(), _source(title="Best pipe tobacco for beginners", text="pipes")) is None

    def test_unindexed_source_is_skipped(self):
        assert score_source(_target(), _source(indexed=False)) is None

    def test_page_never_links_to_itself(self):
        assert score_source(_target(type="article", id=10), _source()) is None

    def test_product_source_needs_the_same_vendor(self):
        same = score_source(_target(), _source(type="product", id=5, title="Swisher Sweets Wine 60ct", vendor="Swisher Sweets"))
        other = score_source(_target(), _source(type="product", id=6, title="Swisher Sweets lookalike", vendor="Game"))
        assert (same is not None, other) == (True, None)

    def test_collection_target_needs_two_shared_words_without_a_mention(self):
        target = _target(type="collection", title="Grape Flavored Cigars", vendor=None, phrases=["Grape Flavored Cigars"])
        two = score_source(target, _source(title="Grape cigars worth trying", text="nothing"))
        one = score_source(target, _source(title="Cigars for beginners", text="nothing"))
        assert (two is not None, one) == (True, None)

    def test_more_clicks_score_higher(self):
        busy = score_source(_target(), _source(clicks=500))
        quiet = score_source(_target(), _source(clicks=0))
        assert busy[0] > quiet[0]


class TestPlanLinks:
    def test_each_target_gets_its_best_sources_up_to_the_limit(self):
        sources = [_source(id=10, clicks=10), _source(id=11, clicks=900), _source(id=12, clicks=100)]
        plan = plan_links([_target()], sources, per_target=2)
        assert [(p["source_id"], p["target_id"]) for p in plan] == [(11, 1), (12, 1)]

    def test_a_source_carries_at_most_the_cap(self):
        targets = [_target(id=i, priority=1) for i in range(1, SOURCE_CAP + 3)]
        plan = plan_links(targets, [_source(id=10)], per_target=1)
        assert len(plan) == SOURCE_CAP

    def test_links_already_on_a_source_count_toward_its_cap(self):
        targets = [_target(id=i) for i in range(1, 5)]
        plan = plan_links(targets, [_source(id=10)], per_target=1, used={("article", 10): SOURCE_CAP - 1})
        assert len(plan) == 1

    def test_higher_priority_targets_claim_sources_first(self):
        targets = [_target(id=1, priority=0), _target(id=2, priority=5)]
        plan = plan_links(targets, [_source(id=10)], per_target=1, used={("article", 10): SOURCE_CAP - 1})
        assert [p["target_id"] for p in plan] == [2]

    @pytest.mark.parametrize("skip", [{("article", 10, "product", 1)}])
    def test_skipped_pairs_are_not_suggested_again(self, skip):
        assert plan_links([_target()], [_source(id=10)], skip=skip) == []


class TestPhrasesFor:
    @pytest.mark.parametrize("title,expected", [
        ("Game Leaf Black Russian Cigarillos - 15 Pouches of 2",
         ["Game Leaf Black Russian Cigarillos - 15 Pouches of 2", "Game Leaf Black Russian Cigarillos"]),
        ("4 Aces Turkish B1G1 Pipe Tobacco Pouches, 6ct (1.2oz)",
         ["4 Aces Turkish B1G1 Pipe Tobacco Pouches, 6ct (1.2oz)", "4 Aces Turkish B1G1 Pipe Tobacco Pouches"]),
        ("Buoy Natural (Yellow) Pipe Tobacco Bag", ["Buoy Natural (Yellow) Pipe Tobacco Bag"]),
        ("Fumari Hookah Tobacco - Watermelon (100g Pouch)",
         ["Fumari Hookah Tobacco - Watermelon (100g Pouch)", "Fumari Hookah Tobacco - Watermelon"]),
        ("Al Fakher Gum (Spearmint) Hookah Shisha Tobacco", ["Al Fakher Gum (Spearmint) Hookah Shisha Tobacco"]),
        ("Backwoods Honey Bourbon Cigarillos (8 Packs of 5)",
         ["Backwoods Honey Bourbon Cigarillos (8 Packs of 5)", "Backwoods Honey Bourbon Cigarillos"]),
        ("Zig-Zag Kutpack", ["Zig-Zag Kutpack"]),
    ])
    def test_product_is_linked_only_by_its_own_name(self, title, expected):
        target = _target(title=title, phrases=[title, "shisha tobacco"])
        assert phrases_for(target, _source()) == expected

    def test_collection_phrases_pass_through(self):
        target = _target(type="collection", vendor=None, phrases=["Pipe Tobacco", "pipe tobacco"])
        assert phrases_for(target, _source(title="Pipe tobacco guide")) == ["Pipe Tobacco", "pipe tobacco"]


class TestDuplicates:
    def test_same_titled_page_is_not_a_source(self):
        twin = _source(type="product", id=5, title="Swisher Sweets Grape Cigarillos 60ct", vendor="Swisher Sweets",
                       text="swisher sweets grape cigarillos 60ct")
        assert score_source(_target(), twin) is None

    def test_generic_keyword_shared_with_source_is_not_a_mention(self):
        target = _target(title="Game Vega Cigarillos Red Ruby", vendor="Game",
                         phrases=["Game Vega Cigarillos Red Ruby", "game vega cigarillos"])
        source = _source(type="product", id=5, title="Game Vega Cigarillos Iced Donut", vendor="Game",
                         text="game vega cigarillos in iced donut")
        assert score_source(target, source)[1] == "About the same brand (game)"


class TestWeakOverlap:
    @pytest.mark.parametrize("target_title,source_title", [
        ("Premium Cigars for Beginners 2026", "Cigar Tariffs in 2026: The Court Ruling"),
        ("King Size Tobacco Collection", "4 Kings Cigars Collection"),
        ("Tyson Ranch Smoking Products", "Tobacco Products and Pipes"),
    ])
    def test_years_and_filler_words_do_not_make_a_topic(self, target_title, source_title):
        target = _target(type="article", vendor=None, title=target_title, phrases=[target_title])
        assert score_source(target, _source(title=source_title, text="")) is None
