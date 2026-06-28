"""Pure-logic tests for the interlinking link extractor/classifier.

These cover the link-identification engine only (no DB, no network):
build_store_hosts, classify_href, extract_links_from_html.
Run with: pytest app/services/interlinking_analyzer_spec.py
"""

import pytest

from app.services.interlinking_analyzer import (
    build_store_hosts,
    classify_href,
    extract_links_from_html,
)

MYSHOPIFY = "test-shop.myshopify.com"
CUSTOM = "shop.example.com"
HOSTS = build_store_hosts(MYSHOPIFY, CUSTOM)


class TestBuildStoreHosts:
    def test_includes_both_domains_with_and_without_www(self):
        assert build_store_hosts(MYSHOPIFY, CUSTOM) == {
            MYSHOPIFY,
            "www." + MYSHOPIFY,
            CUSTOM,
            "www." + CUSTOM,
        }

    def test_strips_scheme_path_and_www_prefix(self):
        assert build_store_hosts("https://www.shop.example.com/path") == {
            "shop.example.com",
            "www.shop.example.com",
        }

    def test_ignores_empty_domains(self):
        assert build_store_hosts(None, "") == set()


class TestClassifyHref:
    @pytest.mark.parametrize(
        "href",
        [
            "https://competitor.com/products/widget",
            "https://www.amazon.com/collections/all",
            "https://other.myshopify.com/products/x",
        ],
    )
    def test_external_links_return_none(self, href):
        assert classify_href(href, HOSTS) is None

    @pytest.mark.parametrize("href", ["#section", "mailto:a@b.com", "tel:+15551234", "javascript:void(0)"])
    def test_non_navigational_links_return_none(self, href):
        assert classify_href(href, HOSTS) is None

    def test_relative_product_is_canonical(self):
        assert classify_href("/products/blue-shirt", HOSTS) == {
            "link_type": "product",
            "handle": "blue-shirt",
            "non_canonical": False,
        }

    def test_absolute_internal_product_on_custom_domain(self):
        assert classify_href("https://shop.example.com/products/blue-shirt", HOSTS) == {
            "link_type": "product",
            "handle": "blue-shirt",
            "non_canonical": False,
        }

    def test_absolute_internal_product_on_myshopify_domain(self):
        assert classify_href(f"https://{MYSHOPIFY}/products/blue-shirt", HOSTS) == {
            "link_type": "product",
            "handle": "blue-shirt",
            "non_canonical": False,
        }

    def test_collection_scoped_product_is_non_canonical(self):
        assert classify_href("/collections/summer/products/blue-shirt", HOSTS) == {
            "link_type": "product",
            "handle": "blue-shirt",
            "non_canonical": True,
        }

    def test_locale_prefix_is_stripped(self):
        assert classify_href("/en-ca/products/blue-shirt", HOSTS) == {
            "link_type": "product",
            "handle": "blue-shirt",
            "non_canonical": False,
        }

    def test_query_and_trailing_slash_ignored(self):
        assert classify_href("/products/blue-shirt/?variant=42#desc", HOSTS) == {
            "link_type": "product",
            "handle": "blue-shirt",
            "non_canonical": False,
        }

    def test_article(self):
        assert classify_href("/blogs/news/spring-launch", HOSTS) == {
            "link_type": "article",
            "handle": "spring-launch",
            "blog_handle": "news",
            "non_canonical": False,
        }

    def test_blog_index(self):
        assert classify_href("/blogs/news", HOSTS) == {
            "link_type": "blog_index",
            "handle": "news",
            "non_canonical": False,
        }

    def test_collection(self):
        assert classify_href("/collections/summer", HOSTS) == {
            "link_type": "collection",
            "handle": "summer",
            "non_canonical": False,
        }

    def test_reserved_collection_all(self):
        # /collections/all is classified normally; the analyzer treats it as valid.
        assert classify_href("/collections/all", HOSTS) == {
            "link_type": "collection",
            "handle": "all",
            "non_canonical": False,
        }

    def test_page(self):
        assert classify_href("/pages/about-us", HOSTS) == {
            "link_type": "page",
            "handle": "about-us",
            "non_canonical": False,
        }

    def test_home(self):
        assert classify_href("/", HOSTS) == {
            "link_type": "home",
            "handle": "",
            "non_canonical": False,
        }

    def test_other_internal_path(self):
        assert classify_href("/policies/refund-policy", HOSTS) == {
            "link_type": "other_internal",
            "handle": "/policies/refund-policy",
            "non_canonical": False,
        }


class TestExtractLinksFromHtml:
    def test_empty_html_returns_empty(self):
        assert extract_links_from_html("", HOSTS) == []

    def test_external_links_are_excluded(self):
        html = (
            '<a href="https://competitor.com/products/x">x</a>'
            '<a href="/products/internal">internal</a>'
        )
        result = extract_links_from_html(html, HOSTS)
        assert result == [
            {
                "href": "/products/internal",
                "anchor_text": "internal",
                "link_type": "product",
                "handle": "internal",
                "non_canonical": False,
            }
        ]

    def test_anchor_text_strips_nested_tags(self):
        html = '<a href="/products/x"><strong>Buy</strong> now</a>'
        assert extract_links_from_html(html, HOSTS)[0]["anchor_text"] == "Buy now"

    def test_href_html_entities_are_decoded(self):
        html = '<a href="/products/x?a=1&amp;b=2">link</a>'
        assert extract_links_from_html(html, HOSTS)[0] == {
            "href": "/products/x?a=1&b=2",
            "anchor_text": "link",
            "link_type": "product",
            "handle": "x",
            "non_canonical": False,
        }

    def test_duplicate_links_are_each_returned(self):
        # Dedup of distinct sources happens in the analyzer; extraction is literal.
        html = '<a href="/products/x">one</a><a href="/products/x">two</a>'
        result = extract_links_from_html(html, HOSTS)
        assert [l["anchor_text"] for l in result] == ["one", "two"]

    def test_non_canonical_flag_propagates(self):
        html = '<a href="/collections/c/products/x">x</a>'
        assert extract_links_from_html(html, HOSTS)[0]["non_canonical"] is True

    def test_blog_handle_included_for_articles(self):
        html = '<a href="/blogs/news/post">post</a>'
        link = extract_links_from_html(html, HOSTS)[0]
        assert link["link_type"] == "article" and link["blog_handle"] == "news"
