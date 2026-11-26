import pytest

from app import nav


@pytest.mark.parametrize("path, key, group", [
    ("/", "dashboard", "Today"),
    ("/seo", "seo", "Fix"),
    ("/content/calendar", "content/calendar", "Create"),
    ("/content-gaps", "content-gaps", "Create"),
    ("/keyword-strategy", "keyword-strategy", "Research"),
    ("/settings", "settings", "Setup"),
])
def test_match_finds_entry_and_group(path, key, group):
    item, label = nav.match(path)
    assert (item["key"], label) == (key, group)


@pytest.mark.parametrize("path", ["/login", "/seo-old", "/technical-seo"])
def test_match_ignores_paths_off_the_map(path):
    assert nav.match(path) == (None, None)


def test_related_links_point_at_known_pages():
    every = [i for g in nav.GROUPS for i in g["items"]] + nav.SETUP
    known = {i["key"] for i in every}
    assert all(set(i.get("related", [])) <= known for i in every)


def test_palette_lists_every_page_once():
    hrefs = [e["href"] for e in nav.palette_entries()]
    assert sorted(hrefs) == sorted(set(hrefs)) and len(hrefs) == 19
