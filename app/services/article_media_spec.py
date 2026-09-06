"""Pure-logic tests for inline-image token/figure round-tripping (no DB, no AI).

Run with: pytest app/services/article_media_spec.py
"""

from app.services.article_media import figure_html, insert_inline_images, tokenize_figures

READY = {"token": "[[INLINE_IMAGE_1]]", "cdn_url": "https://cdn.shopify.com/a.webp?v=1&x=2", "alt": "A", "caption": "One"}
READY_2 = {"token": "[[INLINE_IMAGE_2]]", "cdn_url": "https://cdn.shopify.com/b.webp", "alt": "B", "caption": ""}
FAILED = {"token": "[[INLINE_IMAGE_3]]", "cdn_url": "", "status": "failed"}
TEMPLATE = "<p>intro</p>[[INLINE_IMAGE_1]]<h2>Next</h2><p>more</p>[[INLINE_IMAGE_2]]<p>end</p>"


class TestTokenizeFigures:
    def test_round_trips_a_composed_body_back_to_its_template(self):
        composed = insert_inline_images(TEMPLATE, [READY, READY_2])
        assert tokenize_figures(composed, [READY, READY_2]) == TEMPLATE

    def test_leaves_foreign_figures_and_unready_entries_alone(self):
        kept = '<figure class="x"><img src="https://cdn.shopify.com/old.jpg"></figure>'
        body = f"<p>intro</p>{kept}{figure_html(READY)}<p>end</p>"
        assert tokenize_figures(body, [READY, FAILED]) == f"<p>intro</p>{kept}[[INLINE_IMAGE_1]]<p>end</p>"

    def test_body_without_the_figure_is_unchanged(self):
        assert tokenize_figures("<p>only text</p>", [READY]) == "<p>only text</p>"


from app.services.article_media import parse_restyle_response

BODY = "<p>Intro</p>\n[[INLINE_IMAGE_1]]\n<h2>More</h2><p>Text with \"quotes\" and a\nnewline.</p>"
PLAN = {"hero": {"prompt": "", "alt": ""}, "inline": [{"token": "[[INLINE_IMAGE_1]]", "prompt": "A scene"}]}


class TestParseRestyleResponse:
    def test_splits_delimited_html_and_media_plan(self):
        content = f"```html\n{BODY}\n```\n===MEDIA_PLAN===\n```json\n{__import__('json').dumps(PLAN)}\n```"
        assert parse_restyle_response(content) == (BODY, PLAN)

    def test_accepts_legacy_json_object_output(self):
        content = "Here you go:\n" + __import__("json").dumps({"body_html": BODY, "media_plan": PLAN})
        assert parse_restyle_response(content) == (BODY, PLAN)

    def test_bare_html_without_a_plan_yields_empty_plan(self):
        assert parse_restyle_response(f"\n{BODY}\n") == (BODY, {})

    def test_broken_json_around_html_still_recovers_the_body(self):
        content = '{"body_html": "' + BODY + '", "media_plan": {"inline": [}}'
        assert parse_restyle_response(content) == (BODY, {})

    def test_non_html_text_returns_no_body(self):
        assert parse_restyle_response("I cannot help with that request.") == ("", {})
