"""Pure-logic tests for the apply decision (no DB, no Shopify).

Run with: pytest app/services/apply_pipeline_spec.py
"""

import pytest

from app.services.apply_pipeline import can_revert, plan_apply, shopify_fields

IMG = '<img src="https://cdn.shopify.com/hero.jpg" alt="Hero">'
BODY = (
    f"{IMG}<h2>Specifications</h2><table><tr><td>Count</td><td>5</td></tr></table>"
    '<p>See <a href="/collections/backwoods">Backwoods</a>.</p>'
)
LIVE = {"title": "Backwoods Honey", "body": BODY, "meta_title": "Backwoods Honey 5 Pack", "meta_description": "Old meta."}
NEW_META = "Backwoods Honey Cigars 5 Pack"


class TestPlanApply:
    def test_meta_only_change_pushes_just_that_field(self):
        plan = plan_apply(LIVE, LIVE, {"meta_title": NEW_META})
        assert plan == {"status": "ok", "pushed": {"meta_title": NEW_META}, "old": {"meta_title": LIVE["meta_title"]}}

    def test_fields_equal_to_live_are_a_noop(self):
        assert plan_apply(LIVE, LIVE, {"meta_title": LIVE["meta_title"]}) == {"status": "noop"}

    def test_none_values_are_ignored(self):
        assert plan_apply(LIVE, LIVE, {"meta_title": None, "body": None}) == {"status": "noop"}

    def test_stale_when_live_differs_from_the_mirror_on_a_written_field(self):
        mirror = {**LIVE, "meta_title": "What the app last saw"}
        assert plan_apply(mirror, LIVE, {"meta_title": NEW_META}) == {"status": "stale", "fields": ["meta_title"]}

    def test_drift_on_an_untouched_field_does_not_block(self):
        mirror = {**LIVE, "body": "<p>Older body.</p>"}
        assert plan_apply(mirror, LIVE, {"meta_title": NEW_META})["status"] == "ok"

    def test_whitespace_only_difference_is_not_drift(self):
        mirror = {**LIVE, "body": BODY.replace("><", ">\n  <")}
        new_body = BODY + "<p>More.</p>"
        assert plan_apply(mirror, LIVE, {"body": new_body})["status"] == "ok"

    def test_shopify_html_normalisation_is_not_drift(self):
        stored_by_app = "<p>Retailer&#x27;s pick: Red, White & Berry</p><img src=\"a.jpg\" />"
        stored_by_shopify = "<p>Retailer's pick: Red, White &amp; Berry</p><img src=\"a.jpg\">"
        mirror, live = {**LIVE, "body": stored_by_app}, {**LIVE, "body": stored_by_shopify}
        assert plan_apply(mirror, live, {"body": stored_by_shopify + "<p>More.</p>"})["status"] == "ok"

    def test_rewrite_that_drops_protected_content_is_blocked(self):
        plan = plan_apply(LIVE, LIVE, {"body": f"{IMG}<p>Short rewrite.</p>"})
        assert plan == {
            "status": "blocked",
            "code": "dropped_blocks",
            "missing": ["1 table", "Specifications section", "internal link /collections/backwoods"],
        }

    def test_blocked_rewrite_goes_through_when_explicitly_allowed(self):
        new_body = f"{IMG}<p>Short rewrite.</p>"
        plan = plan_apply(LIVE, LIVE, {"body": new_body}, allow_dropped_blocks=True)
        assert plan == {"status": "ok", "pushed": {"body": new_body}, "old": {"body": BODY}}

    def test_missing_images_are_restored_before_the_push(self):
        new_body = BODY.replace(IMG, "") + "<p>More.</p>"
        assert plan_apply(LIVE, LIVE, {"body": new_body})["pushed"]["body"] == IMG + new_body

    def test_manual_edits_may_remove_an_image(self):
        new_body = BODY.replace(IMG, "") + "<p>More.</p>"
        plan = plan_apply(LIVE, LIVE, {"body": new_body}, restore_media=False)
        assert plan["pushed"]["body"] == new_body

    def test_body_is_sanitized_and_metas_clamped(self):
        plan = plan_apply(LIVE, LIVE, {"body": BODY + "<p>More.</p><script>x()</script>", "meta_title": "word " * 30})
        assert "<script" not in plan["pushed"]["body"]
        assert len(plan["pushed"]["meta_title"]) <= 60

    def test_rewrite_that_drops_the_primary_keyword_is_blocked(self):
        plan = plan_apply(LIVE, LIVE, {"meta_title": "Honey Cigars 5 Pack"}, primary_keyword="backwoods honey")
        assert plan["status"] == "ok"  # keyword still present in title/body
        live = {**LIVE, "title": "Honey", "body": "<p>Cigars.</p>"}
        plan = plan_apply(live, live, {"meta_title": "Honey Cigars 5 Pack"}, primary_keyword="backwoods honey")
        assert plan == {"status": "blocked", "code": "dropped_primary", "missing": ["backwoods honey"]}


class TestShopifyFields:
    def test_product_and_collection_use_description_html(self):
        assert shopify_fields("product", {"body": "<p>x</p>", "meta_title": "t"}) == {"description_html": "<p>x</p>", "meta_title": "t"}

    def test_article_keeps_body(self):
        assert shopify_fields("article", {"body": "<p>x</p>", "meta_description": "d"}) == {"body": "<p>x</p>", "meta_description": "d"}

    def test_page_uses_seo_prefixed_metas(self):
        assert shopify_fields("page", {"body": "<p>x</p>", "meta_title": "t", "meta_description": "d"}) == {
            "body": "<p>x</p>", "seo_title": "t", "seo_description": "d"}


class TestCanRevert:
    def test_revertible_while_live_still_holds_what_the_change_wrote(self):
        assert can_revert({"meta_title": NEW_META}, {**LIVE, "meta_title": NEW_META}) == []

    def test_names_fields_changed_since(self):
        assert can_revert({"meta_title": NEW_META, "title": LIVE["title"]}, LIVE) == ["meta_title"]


from app.services.apply_pipeline import visible_faq  # noqa: E402

FAQ_BODY = "<h2>Are Backwoods cigars machine made?</h2><p>Yes, they are.</p><h3>How many come in a pack?</h3><p>Five.</p>"


def _faq(*questions):
    return {
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": "A."}} for q in questions],
    }


class TestVisibleFaq:
    def test_keeps_questions_that_appear_on_the_page(self):
        faq = _faq("Are Backwoods cigars machine made?", "How many come in a pack?")
        assert visible_faq(faq, FAQ_BODY) == faq

    def test_drops_questions_the_page_does_not_show(self):
        faq = _faq("Are Backwoods cigars machine made?", "Do you ship to Canada?")
        assert visible_faq(faq, FAQ_BODY) == _faq("Are Backwoods cigars machine made?")

    def test_matching_ignores_case_markup_and_spacing(self):
        body = "<h2>Are <strong>Backwoods</strong>  cigars machine made?</h2>"
        assert visible_faq(_faq("are backwoods cigars machine made?"), body) == _faq("are backwoods cigars machine made?")

    def test_returns_none_when_no_question_is_visible(self):
        assert visible_faq(_faq("Do you ship to Canada?"), FAQ_BODY) is None


STYLED = '<style>.spec{color:red}</style>' + BODY
LONG_META = "Backwoods Honey Bourbon Cigars 5 Pack Natural Leaf Wrapper Sweet"


class TestGuardScope:
    def test_guarded_write_strips_style_blocks(self):
        live = {**LIVE, "body": STYLED}
        plan = plan_apply(live, live, {"body": STYLED + "<p>Added.</p>"})
        assert plan["pushed"]["body"] == BODY + "<p>Added.</p>"

    def test_verbatim_write_keeps_style_blocks(self):
        live = {**LIVE, "body": STYLED}
        new_body = STYLED + "<p>Added.</p>"
        plan = plan_apply(live, live, {"body": new_body}, guard=False)
        assert plan == {"status": "ok", "pushed": {"body": new_body}, "old": {"body": STYLED}}

    def test_verbatim_write_keeps_a_meta_title_over_sixty_characters(self):
        plan = plan_apply(LIVE, LIVE, {"meta_title": LONG_META}, guard=False)
        assert plan["pushed"] == {"meta_title": LONG_META}

    def test_guarded_write_clamps_a_meta_title_over_sixty_characters(self):
        plan = plan_apply(LIVE, LIVE, {"meta_title": LONG_META})
        assert len(plan["pushed"]["meta_title"]) <= 60


from app.services.apply_pipeline import is_content_update


class TestIsContentUpdate:
    def test_ai_rewrite_of_the_body_is_a_content_update(self):
        assert is_content_update("blog", "article_refresh", {"body": "x"}) is True

    @pytest.mark.parametrize("source,change_type", [
        ("auto_links", "link_added"), ("price_refresh", "price_refresh"),
        ("hero_image", "inline_image_regen"), ("blog", "restyle"), ("undo", "revert"),
    ])
    def test_wiring_edits_are_not(self, source, change_type):
        assert is_content_update(source, change_type, {"body": "x"}) is False

    def test_meta_only_change_is_not(self):
        assert is_content_update("blog", "article_edit", {"meta_title": "x"}) is False
