"""Pure-logic tests for splitting a post listing into recent + monthly archive.

Run with: pytest app/services/listing_archive_spec.py
"""

from app.services.listing_archive import archive_html, split_archive


def _post(n, published):
    return {"title": f"Post {n}", "published_at": published}


def _key(a):
    return a.get("published_at") or ""


POSTS = [
    _post(1, "2026-09-20T10:00:00"), _post(2, "2026-09-02T10:00:00"),
    _post(3, "2026-08-15T10:00:00"), _post(4, "2026-08-01T10:00:00"), _post(5, "2026-07-30T10:00:00"),
]


class TestSplitArchive:
    def test_everything_fits_in_recent(self):
        assert split_archive(POSTS, 10, _key) == (POSTS, [])

    def test_older_posts_are_grouped_by_month_newest_first(self):
        recent, months = split_archive(POSTS, 2, _key)
        assert [p["title"] for p in recent] == ["Post 1", "Post 2"]
        assert [(label, [p["title"] for p in posts]) for label, posts in months] == [
            ("August 2026", ["Post 3", "Post 4"]), ("July 2026", ["Post 5"])]

    def test_no_post_is_dropped(self):
        recent, months = split_archive(POSTS, 1, _key)
        assert len(recent) + sum(len(p) for _, p in months) == len(POSTS)

    def test_a_month_split_by_the_cut_keeps_its_remainder_in_the_archive(self):
        _, months = split_archive(POSTS, 1, _key)
        assert months[0] == ("September 2026", [POSTS[1]])

    def test_posts_without_a_date_go_to_an_earlier_group(self):
        _, months = split_archive(POSTS + [_post(6, None)], 5, _key)
        assert months == [("Earlier", [_post(6, None)])]


class TestArchiveHtml:
    def test_each_month_is_a_collapsed_section_with_its_count(self):
        out = archive_html([("August 2026", [1, 2]), ("July 2026", [3])], lambda posts: f"<grid n={len(posts)}>", "x")
        assert out.count("<details") == 2
        assert "August 2026 <span>(2)</span>" in out
        assert "<grid n=1>" in out
        assert " open" not in out

    def test_no_archive_gives_empty_string(self):
        assert archive_html([], lambda posts: "", "x") == ""
