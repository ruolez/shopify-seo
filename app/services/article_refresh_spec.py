"""Pure-logic tests for the article refresh (no DB, no AI).

Run with: pytest app/services/article_refresh_spec.py
"""

from app.services.article_refresh import parse_refresh_reply, pick_link_targets

REPLY = """===META_TITLE===
Best Blunt Wraps Ranked (2026)
===META_DESCRIPTION===
Every major blunt wrap brand compared by leaf, burn and price.
===SUMMARY===
A ranked guide to blunt wraps.
===CHANGES===
- Rewrote the intro around "best blunt wraps"
- Added a section on hemp wraps
===BODY===
<h2>Top picks</h2>
<p>Body text.</p>
"""


class TestParseRefreshReply:
    def test_reads_every_section(self):
        assert parse_refresh_reply(REPLY) == {
            "meta_title": "Best Blunt Wraps Ranked (2026)",
            "meta_description": "Every major blunt wrap brand compared by leaf, burn and price.",
            "summary": "A ranked guide to blunt wraps.",
            "changes": ['Rewrote the intro around "best blunt wraps"', "Added a section on hemp wraps"],
            "body": "<h2>Top picks</h2>\n<p>Body text.</p>",
        }

    def test_strips_a_code_fence_around_the_body(self):
        reply = "===BODY===\n```html\n<p>Body.</p>\n```"
        assert parse_refresh_reply(reply)["body"] == "<p>Body.</p>"

    def test_missing_sections_are_empty_not_errors(self):
        assert parse_refresh_reply("===BODY===\n<p>Only body.</p>") == {
            "meta_title": "", "meta_description": "", "summary": "", "changes": [], "body": "<p>Only body.</p>",
        }

    def test_reply_without_a_body_is_unusable(self):
        assert parse_refresh_reply("Sorry, I cannot help with that.") is None


COLLECTIONS = [
    {"title": "Blunt Wraps", "handle": "blunt-wraps"},
    {"title": "Hemp Wraps", "handle": "hemp-wraps"},
    {"title": "Pipe Tobacco", "handle": "pipe-tobacco"},
    {"title": "Hookah", "handle": "hookah"},
]


class TestPickLinkTargets:
    def test_ranks_by_shared_words_with_the_article_topic(self):
        picked = pick_link_targets("best blunt wraps ranked hemp", COLLECTIONS, limit=2)
        assert [c["handle"] for c in picked] == ["blunt-wraps", "hemp-wraps"]

    def test_unrelated_items_are_left_out(self):
        assert pick_link_targets("zyn nicotine pouches", COLLECTIONS, limit=5) == []

    def test_plural_and_singular_match(self):
        picked = pick_link_targets("which wrap burns slowest", COLLECTIONS, limit=5)
        assert [c["handle"] for c in picked] == ["blunt-wraps", "hemp-wraps"]
