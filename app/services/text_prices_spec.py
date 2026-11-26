"""Pure-logic tests for refreshing store prices in text (no DB).

Run with: pytest app/services/text_prices_spec.py
"""

import pytest

from app.services.text_prices import pair_variants, plan_price_text, prices_changed, refresh_text_prices

CARTON = "Cheyenne Cherry Filtered Cigars - 10 Packs of 20"
ONE_VARIANT = [{"title": "Default Title", "old": "17.15", "new": "15.99"}]
TWO_VARIANTS = [
    {"title": "6oz Bag", "old": "6.25", "new": "5.99"},
    {"title": "16oz Bag", "old": "12.99", "new": "11.49"},
]


def refreshed(text, title=CARTON, variants=ONE_VARIANT, keep=()):
    return refresh_text_prices(text, title, variants, keep=keep)


def result(text, changes=(), flags=()):
    return {"text": text, "changes": list(changes), "flags": list(flags)}


class TestFullPrice:
    def test_single_variant_price_takes_the_new_price(self):
        assert refreshed("Only $17.15. In stock now.") == result(
            "Only $15.99. In stock now.", changes=[("$17.15", "$15.99")])

    def test_price_inside_html_keeps_the_markup(self):
        assert refreshed("<p>Priced at <strong>$17.15</strong> per carton.</p>") == result(
            "<p>Priced at <strong>$15.99</strong> per carton.</p>", changes=[("$17.15", "$15.99")])

    def test_old_price_of_a_variant_maps_to_that_variants_new_price(self):
        text = "The 16oz bag is $12.99."
        assert refreshed(text, variants=TWO_VARIANTS) == result(
            "The 16oz bag is $11.49.", changes=[("$12.99", "$11.49")])

    def test_two_number_range_maps_each_end(self):
        text = "prices ranging from $6.25 to $12.99."
        assert refreshed(text, variants=TWO_VARIANTS) == result(
            "prices ranging from $5.99 to $11.49.", changes=[("$6.25", "$5.99"), ("$12.99", "$11.49")])

    def test_unknown_two_number_range_takes_min_and_max(self):
        text = "Priced between $6.75 and $14.50, a bargain."
        assert refreshed(text, variants=TWO_VARIANTS) == result(
            "Priced between $5.99 and $11.49, a bargain.", changes=[("$6.75", "$5.99"), ("$14.50", "$11.49")])

    def test_dash_range_becomes_new_min_to_max(self):
        text = "<td>$6.25–$12.99</td>"
        assert refreshed(text, variants=TWO_VARIANTS) == result(
            "<td>$5.99–$11.49</td>", changes=[("$6.25–$12.99", "$5.99–$11.49")])

    def test_dash_range_collapses_when_variants_now_share_a_price(self):
        same = [{"title": "A", "old": "1.00", "new": "9.00"}, {"title": "B", "old": "2.00", "new": "9.00"}]
        assert refreshed("$1.00–$2.00", variants=same) == result("$9.00", changes=[("$1.00–$2.00", "$9.00")])

    def test_lone_from_price_becomes_the_new_lowest_price(self):
        assert refreshed("In stock from $1.90.", variants=TWO_VARIANTS) == result(
            "In stock from $5.99.", changes=[("$1.90", "$5.99")])

    def test_old_price_shared_by_variants_that_now_differ_is_flagged(self):
        split = [{"title": "$1.29", "old": "16.50", "new": "16.00"}, {"title": "$1.49", "old": "16.50", "new": "16.50"}]
        text = "30 smokes for a price of $16.50."
        assert refreshed(text, variants=split) == result(text, flags=[("$16.50", "no_variant")])

    def test_unmatched_price_on_a_multi_variant_product_is_flagged(self):
        text = "A steal at $9.99."
        assert refreshed(text, variants=TWO_VARIANTS) == result(text, flags=[("$9.99", "no_variant")])

    def test_price_that_is_already_current_is_left_alone(self):
        assert refreshed("Only $15.99.") == result("Only $15.99.")

    def test_wrong_price_on_a_single_variant_product_is_corrected(self):
        assert refreshed("At $16.50 MSRP.") == result("At $15.99 MSRP.", changes=[("$16.50", "$15.99")])


class TestPerUnit:
    @pytest.mark.parametrize("unit", ["cigar", "stick", "individual cigar", "smoke"])
    def test_per_stick_price_is_new_price_over_the_count_in_the_title(self, unit):
        text = f"approximately $0.08 per {unit}"
        assert refreshed(text) == result(f"approximately $0.08 per {unit}")

    def test_per_stick_price_is_recomputed_when_it_changes(self):
        title = "Good Days Robusto Cigars (Box of 50)"
        variants = [{"title": "Default Title", "old": "43.99", "new": "40.00"}]
        assert refreshed("approximately $0.88 per stick", title, variants) == result(
            "approximately $0.80 per stick", changes=[("$0.88", "$0.80")])

    def test_per_pack_price_uses_the_pack_count(self):
        assert refreshed("about $1.72 per pack") == result("about $1.60 per pack", changes=[("$1.72", "$1.60")])

    def test_count_comes_from_the_variant_title_first(self):
        title = "Game Cigarillos"
        variants = [{"title": "15 Pouches of 2", "old": "16.50", "new": "15.00"}]
        assert refreshed("approximately $0.55 per cigarillo", title, variants) == result(
            "approximately $0.50 per cigarillo", changes=[("$0.55", "$0.50")])

    def test_without_a_count_the_unit_price_scales_with_the_variant(self):
        title = "Fronto Leaf"
        variants = [{"title": "Default Title", "old": "20.00", "new": "15.00"}]
        assert refreshed("roughly $1.00 per leaf", title, variants) == result(
            "roughly $0.75 per leaf", changes=[("$1.00", "$0.75")])

    def test_unit_price_based_on_a_named_total_uses_that_variant(self):
        text = "approximately $0.52 per oz based on the $12.99 price"
        assert refreshed(text, "752 Pipe Tobacco", TWO_VARIANTS) == result(
            "approximately $0.46 per oz based on the $11.49 price",
            changes=[("$0.52", "$0.46"), ("$12.99", "$11.49")])

    @pytest.mark.parametrize("text, expected", [
        ("the cost-per-stick is approximately $0.09,", "the cost-per-stick is approximately $0.08,"),
        ("the cost per cigar is reduced to approximately $0.09.", "the cost per cigar is reduced to approximately $0.08."),
        ("price-per-pack of about $1.72", "price-per-pack of about $1.60"),
    ])
    def test_unit_named_before_the_amount_is_a_unit_price(self, text, expected):
        assert refreshed(text)["text"] == expected

    def test_small_amount_without_a_unit_is_flagged_not_replaced_by_the_full_price(self):
        text = "a bargain at approximately $0.09 today"
        assert refreshed(text) == result(text, flags=[("$0.09", "no_unit")])

    def test_unit_price_is_kept_when_its_variant_price_did_not_change(self):
        unchanged = [{"title": "Default Title", "old": "17.15", "new": "17.15"}]
        assert refreshed("approximately $0.08 per cigar", variants=unchanged) == result(
            "approximately $0.08 per cigar")

    @pytest.mark.parametrize("text, expected", [
        ("a price of $17.15 per box", "a price of $15.99 per box"),
        ("a price point of $17.15 per unit", "a price point of $15.99 per unit"),
        ("approximately $16.99 per 200-count carton", "approximately $15.99 per 200-count carton"),
    ])
    def test_per_box_amount_is_the_full_price(self, text, expected):
        assert refreshed(text)["text"] == expected

    def test_per_box_range_maps_each_end(self):
        text = "prices ranging from $6.25 to $12.99 per bag"
        assert refreshed(text, variants=TWO_VARIANTS)["text"] == "prices ranging from $5.99 to $11.49 per bag"

    def test_unit_price_on_a_multi_variant_product_without_a_base_is_flagged(self):
        text = "approximately $0.52 per oz"
        assert refreshed(text, "752 Pipe Tobacco", TWO_VARIANTS) == result(text, flags=[("$0.52", "no_variant")])


class TestComparative:
    def test_under_claim_that_is_still_true_is_left_alone(self):
        assert refreshed("200 cigars for under $17.50.") == result("200 cigars for under $17.50.")

    def test_under_claim_that_became_false_is_flagged(self):
        variants = [{"title": "Default Title", "old": "17.15", "new": "18.25"}]
        text = "200 cigars for under $17.50."
        assert refreshed(text, variants=variants) == result(text, flags=[("$17.50", "comparative_false")])

    def test_per_unit_under_claim_is_checked_against_the_unit_price(self):
        assert refreshed("less than $0.10 per cigar") == result("less than $0.10 per cigar")


class TestPackagingPrices:
    @pytest.mark.parametrize("text", [
        "Each pouch is pre-priced at $1.49 per pack.",
        "15 Packs of 5 ($1.49 Pre-Priced)",
        'their iconic "3 for $1.29" packaging',
        "often marked at 2-for-$1.49 price points",
        "Each pack is clearly marked with the $1.19 label.",
        "the swisher sweets 99 cents or $1.29 cigars",
    ])
    def test_prices_printed_on_the_pack_stay(self, text):
        assert refreshed(text) == result(text)

    def test_price_that_is_part_of_the_product_name_stays(self):
        title = "Swisher Sweets BLK Cherry $1.69 Tip Cigarillos"
        text = "Experience Swisher Sweets BLK Cherry $1.69 Tip Cigarillos today."
        assert refreshed(text, title) == result(text)

    def test_store_price_next_to_a_pre_price_is_still_refreshed(self):
        title = "4 Kings Cigarillos - 15 Packs of 4"
        variants = [{"title": "Default Title", "old": "13.25", "new": "12.25"}]
        text = "pre-priced MSRP of $1.39 per pack. At a total price of $13.25 for 60 units"
        assert refreshed(text, title, variants) == result(
            "pre-priced MSRP of $1.39 per pack. At a total price of $12.25 for 60 units",
            changes=[("$13.25", "$12.25")])

    def test_keep_list_protects_individual_amounts(self):
        text = "these pouches moved from $0.99 to $1.49 over the years"
        assert refreshed(text, keep={"$0.99", "$1.49"}) == result(text)


class TestUntouched:
    @pytest.mark.parametrize("text", [None, "", "<p>No prices here.</p>"])
    def test_text_without_amounts_comes_back_unchanged(self, text):
        assert refreshed(text) == result(text or "")

    def test_is_idempotent(self):
        once = refreshed("Only $17.15, about $0.09 per cigar.")["text"]
        assert refreshed(once) == result(once)


def _v(vid, price, title="Default Title"):
    return {"id": vid, "title": title, "price": price}


class TestPricesChanged:
    @pytest.mark.parametrize("old,new,expected", [
        ([_v("a", "17.15")], [_v("a", "15.99")], True),
        ([_v("a", "17.15")], [_v("a", "17.150")], False),
        ([_v("a", "17.15")], [_v("a", "17.15"), _v("b", "30.00")], False),
        ([_v("a", "17.15"), _v("b", "30.00")], [_v("a", "17.15")], False),
        ([], [_v("a", "17.15")], False),
        ([_v("a", None)], [_v("a", "17.15")], False),
    ], ids=["repriced", "same-price-other-format", "variant-added", "variant-removed", "new-product", "unknown-old"])
    def test_only_a_known_variant_with_a_new_price_counts(self, old, new, expected):
        assert prices_changed(old, new) is expected


class TestPlanPriceText:
    def test_body_and_meta_both_take_the_new_price(self):
        row = {"title": CARTON, "description": "<p>Only $17.15 a carton.</p>", "meta_description": "Buy for $17.15."}
        variants = pair_variants([_v("a", "17.15")], [_v("a", "15.99")])
        assert plan_price_text(row, variants) == (
            {"body": "<p>Only $15.99 a carton.</p>", "meta_description": "Buy for $15.99."},
            [("body", "$17.15", "$15.99"), ("meta_description", "$17.15", "$15.99")],
            [],
        )

    def test_text_without_prices_plans_nothing(self):
        row = {"title": CARTON, "description": "<p>Smooth.</p>", "meta_description": None}
        assert plan_price_text(row, pair_variants([_v("a", "1")], [_v("a", "2")])) == ({}, [], [])
