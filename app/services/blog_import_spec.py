"""Pure-logic tests for blog brief import (no DB).

Run with: pytest app/services/blog_import_spec.py
"""

import pytest

from app.services.ai_content_validator import validate_named_fields
from app.services.blog_import import (
    ARTICLE_KEYS,
    OUTLINE_KEYS,
    apply_locked_fields,
    check_brief,
    locked_article_keys,
    locked_fields_of,
    normalize_handle,
    parse_blog_briefs,
)

CHEAP = {
    "topic": "Cheap tobacco, ranked by price per smoke",
    "title": "Cheap Tobacco Online: Every 16 oz Bag Ranked by Price per Cigarette (2026)",
    "meta_title": "Cheap Tobacco: 16 oz Bags Ranked by Price per Smoke (2026)",
    "meta_description": (
        "Looking for cheap tobacco? We ranked every 16 oz bag, from OHM and Good Stuff to Gambler "
        "and Golden Harvest, by real cost per cigarette. See who wins."
    ),
    "keywords": ["cheap tobacco", "cheap pipe tobacco", "cheapest tobacco online"],
    "handle": "cheap-tobacco-price-per-cigarette",
}
TUBE = {
    "topic": "How to tube cigarettes",
    "title": "How to Tube Cigarettes: Step-by-Step Guide to Making Your Own with Filter Tubes",
    "meta_title": "How to Tube Cigarettes: Step-by-Step Beginner's Guide",
    "meta_description": (
        "Learn how to tube cigarettes with an injector, filter tubes and pipe tobacco: setup, the "
        "right cut, king size vs 100mm tubes, and how much you'll save."
    ),
    "keywords": ["how to tube cigarettes", "cigarette tubes"],
    "handle": "how-to-tube-cigarettes",
}


def _render(brief, number, label_fmt="{}: {}"):
    return "\n".join([
        f"{number}. {brief['topic']}",
        "",
        label_fmt.format("H1", brief["title"]),
        label_fmt.format("SEO Title", brief["meta_title"]),
        label_fmt.format("Meta Description", brief["meta_description"]),
        label_fmt.format("Keywords", ", ".join(brief["keywords"])),
        label_fmt.format("URL Slug", brief["handle"]),
    ])


SAMPLE = "\n\n\n" + _render(CHEAP, 3) + "\n\n" + _render(TUBE, 4) + "\n\n"


class TestParseBlogBriefs:
    def test_reads_every_field_of_the_users_file_format_verbatim(self):
        assert parse_blog_briefs(SAMPLE) == [CHEAP, TUBE]

    def test_crlf_line_endings_parse_the_same(self):
        assert parse_blog_briefs(SAMPLE.replace("\n", "\r\n")) == [CHEAP, TUBE]

    @pytest.mark.parametrize("label_fmt", ["**{}:** {}", "**{}**: {}", "- {}: {}", "- **{}:** {}"])
    def test_markdown_emphasis_and_bullets_around_labels_are_ignored(self, label_fmt):
        assert parse_blog_briefs(_render(CHEAP, 1, label_fmt)) == [CHEAP]

    def test_markdown_heading_topic_lines_start_briefs(self):
        text = _render(CHEAP, 1).replace("1. ", "## 1. ", 1) + "\n" + _render(TUBE, 2).replace("2. ", "### ", 1)
        assert parse_blog_briefs(text) == [CHEAP, TUBE]

    def test_a_second_h1_starts_a_new_brief_when_there_are_no_topic_lines(self):
        text = "\n".join(_render(b, 1).split("\n", 2)[2] for b in (CHEAP, TUBE))
        assert parse_blog_briefs(text) == [{**CHEAP, "topic": ""}, {**TUBE, "topic": ""}]

    def test_keywords_are_deduped_case_insensitively_keeping_first_order(self):
        text = "1. T\nH1: Title\nKeywords: Cheap Tobacco, cheap tobacco , pipe tobacco,, CHEAP TOBACCO"
        assert parse_blog_briefs(text)[0]["keywords"] == ["Cheap Tobacco", "pipe tobacco"]

    def test_bulleted_keywords_under_an_empty_keywords_label(self):
        text = "1. T\nH1: Title\nKeywords:\n- cheap tobacco\n- pipe tobacco\nURL Slug: x"
        brief = parse_blog_briefs(text)[0]
        assert (brief["keywords"], brief["handle"]) == (["cheap tobacco", "pipe tobacco"], "x")

    def test_missing_optional_fields_stay_empty(self):
        text = "1. Topic only\nH1: Just a title\nKeywords: kw"
        assert parse_blog_briefs(text) == [{
            "topic": "Topic only", "title": "Just a title", "meta_title": "",
            "meta_description": "", "handle": "", "keywords": ["kw"],
        }]

    def test_topic_lines_without_fields_are_dropped(self):
        assert parse_blog_briefs("Ideas for later\n1. Something\n2. Other") == []


class TestNormalizeHandle:
    @pytest.mark.parametrize("raw,expected", [
        ("how-to-tube-cigarettes", "how-to-tube-cigarettes"),
        ("How To Tube Cigarettes", "how-to-tube-cigarettes"),
        ("beginner's--guide__2026", "beginners-guide-2026"),
        ("/blogs/news/dutch-masters-flavors-ranked/", "dutch-masters-flavors-ranked"),
        ("https://a2ztobacco.com/blogs/news/best-wood-tip-cigars", "best-wood-tip-cigars"),
        ("!!!", ""),
    ])
    def test_normalizes_to_shopify_handle(self, raw, expected):
        assert normalize_handle(raw) == expected


class TestCheckBrief:
    def test_clean_brief_has_no_errors_or_warnings(self):
        checked = check_brief(CHEAP)
        assert (checked["errors"], checked["warnings"]) == ([], [])

    def test_missing_h1_and_keywords_are_errors(self):
        checked = check_brief({**CHEAP, "title": "", "keywords": []})
        assert checked["errors"] == ["Missing H1", "Missing keywords"]

    def test_title_over_shopify_limit_is_an_error(self):
        assert check_brief({**CHEAP, "title": "x" * 256})["errors"] == [
            "H1 is 256 characters; Shopify allows 255"
        ]

    def test_long_meta_and_missing_keyword_are_warnings_not_rewrites(self):
        long_meta = "Cheap tobacco " + "y" * 60
        checked = check_brief({**CHEAP, "meta_title": long_meta, "title": "Rolling Your Own on a Budget"})
        assert (checked["meta_title"], checked["title"], checked["warnings"]) == (
            long_meta,
            "Rolling Your Own on a Budget",
            [
                f"SEO title is {len(long_meta)} characters (over 60)",
                "H1 doesn't contain the primary keyword “cheap tobacco”",
            ],
        )

    def test_slug_is_normalized_with_a_warning(self):
        checked = check_brief({**CHEAP, "handle": "Cheap Tobacco Price"})
        assert (checked["handle"], checked["warnings"]) == (
            "cheap-tobacco-price", ["URL slug normalized to 'cheap-tobacco-price'"]
        )

    def test_unusable_slug_is_an_error(self):
        assert check_brief({**CHEAP, "handle": "???"})["errors"] == ["URL slug '???' has no usable characters"]


AI_ARTICLE = {
    "title": "AI Title", "meta_title": "AI meta", "meta_description": "AI desc",
    "handle": "", "body_html": "<p>x</p>", "tags": ["a"],
}


class TestApplyLockedFields:
    def test_locked_values_replace_ai_values_and_other_keys_pass_through(self):
        locked = locked_fields_of(CHEAP)
        assert apply_locked_fields(AI_ARTICLE, locked) == {
            **AI_ARTICLE,
            "title": CHEAP["title"],
            "meta_title": CHEAP["meta_title"],
            "meta_description": CHEAP["meta_description"],
            "handle": CHEAP["handle"],
        }

    @pytest.mark.parametrize("locked", [
        locked_fields_of(CHEAP),
        {"title": "Only the title"},
        {"handle": "only-slug"},
        {},
    ])
    def test_applying_twice_equals_applying_once(self, locked):
        once = apply_locked_fields(AI_ARTICLE, locked)
        assert apply_locked_fields(once, locked) == once

    def test_fields_the_file_left_empty_keep_the_ai_value(self):
        locked = locked_fields_of({**CHEAP, "meta_description": "", "handle": ""})
        result = apply_locked_fields(AI_ARTICLE, locked)
        assert (result["meta_description"], result["handle"]) == ("AI desc", "")

    def test_outline_keys_lock_the_suggested_title(self):
        outline = {"suggested_title": "AI", "meta_title": "AI", "meta_description": "AI", "sections": [1]}
        assert apply_locked_fields(outline, {"title": "T", "meta_title": "M", "handle": "h"}, OUTLINE_KEYS) == {
            "suggested_title": "T", "meta_title": "M", "meta_description": "AI", "sections": [1],
        }

    def test_does_not_mutate_its_input(self):
        article = dict(AI_ARTICLE)
        apply_locked_fields(article, locked_fields_of(CHEAP), ARTICLE_KEYS)
        assert article == AI_ARTICLE


class _ExplodingCompletions:
    def create(self, **_):
        raise AssertionError("locked fields must never be sent for an LLM rewrite")


class _ExplodingClient:
    class chat:
        completions = _ExplodingCompletions()


class TestQualityGateLeavesLockedFieldsAlone:
    def test_violating_locked_fields_are_reported_not_rewritten(self):
        long_meta = "M" * 70
        result = {"title": "No keyword here", "meta_title": long_meta, "meta_description": "short"}
        locked = {"title": "No keyword here", "meta_title": long_meta, "meta_description": "short"}
        out = validate_named_fields(
            dict(result),
            {"meta_title": "meta_title", "meta_description": "meta_description", "title": "title"},
            "cheap tobacco", _ExplodingClient(), {"model": "m"},
            locked=locked_article_keys(locked),
        )
        assert (
            {k: out[k] for k in result},
            sorted(v["field"] + ":" + v["kind"] for v in out["_qa"]["locked_warnings"]),
        ) == (
            result,
            [
                "meta_description:meta_keyword_missing",
                "meta_title:length",
                "meta_title:meta_keyword_missing",
                "title:title_keyword",
            ],
        )
