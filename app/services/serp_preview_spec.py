import pytest

from app.services.serp_preview import item_path, serp_preview

HANDLE = "loose-leaf-natural"
BLOG = "news"


@pytest.mark.parametrize("item_type, expected", [
    ("product", f"/products/{HANDLE}"),
    ("collection", f"/collections/{HANDLE}"),
    ("page", f"/pages/{HANDLE}"),
    ("article", f"/blogs/{BLOG}/{HANDLE}"),
])
def test_item_path_uses_shopify_url_shape(item_type, expected):
    assert item_path(item_type, HANDLE, BLOG) == expected


@pytest.mark.parametrize("item_type, handle, blog", [
    ("product", None, None),
    ("article", HANDLE, None),
    ("brand", HANDLE, None),
])
def test_item_path_is_none_when_url_unknown(item_type, handle, blog):
    assert item_path(item_type, handle, blog) is None


def test_preview_prefers_seo_fields_and_strips_markup():
    row = {"handle": HANDLE, "title": "Shopify title", "meta_title": "SEO title",
           "meta_description": "<p>Slow  burning\nwraps</p>"}
    assert serp_preview("product", row) == {
        "path": f"/products/{HANDLE}", "title": "SEO title",
        "description": "Slow burning wraps", "own_description": True,
    }


def test_preview_falls_back_to_page_title_without_description():
    row = {"handle": HANDLE, "title": "Shopify title", "meta_title": None, "meta_description": "  "}
    assert serp_preview("collection", row) == {
        "path": f"/collections/{HANDLE}", "title": "Shopify title",
        "description": "", "own_description": False,
    }


def test_preview_reads_page_column_names():
    row = {"handle": "about", "title": "About", "seo_title": "About A2Z", "seo_description": "Who we are"}
    assert serp_preview("page", row)["title"] == "About A2Z"
