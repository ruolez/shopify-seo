"""Pure-logic tests for the published Sources section (no DB, no AI).

Run with: pytest app/services/news_body_spec.py
"""

from app.services.news_body import clean_sources, render_sources_html

STORE = {"domain": "a2z-tobacco.myshopify.com", "primary_domain": "https://a2ztobacco.com/"}
LOOSELEAF_A = {"url": "https://looseleaf.com/blogs/news/a", "title": ""}
LOOSELEAF_B = {"url": "https://looseleaf.com/blogs/news/b", "title": "Looseleaf launch story"}
ELEMENT = {"url": "https://www.elementvape.com/x", "title": ""}


class TestCleanSources:
    def test_collapses_untitled_repeats_of_one_host_to_the_first(self):
        second = {"url": "https://looseleaf.com/other", "title": ""}
        assert clean_sources([LOOSELEAF_A, second, ELEMENT]) == [LOOSELEAF_A, ELEMENT]

    def test_keeps_titled_sources_on_one_host_when_titles_differ(self):
        other = {"url": "https://looseleaf.com/blogs/news/c", "title": "Where to buy wraps"}
        assert clean_sources([LOOSELEAF_A, LOOSELEAF_B, other]) == [LOOSELEAF_A, LOOSELEAF_B, other]

    def test_collapses_same_title_on_one_host_ignoring_case(self):
        repeat = {"url": "https://looseleaf.com/blogs/news/b?utm=x", "title": "LOOSELEAF LAUNCH STORY"}
        assert clean_sources([LOOSELEAF_B, repeat]) == [LOOSELEAF_B]

    def test_treats_www_and_bare_host_as_the_same_site(self):
        assert clean_sources(["https://www.elementvape.com/a", "elementvape.com/b"]) == [
            {"url": "https://www.elementvape.com/a", "title": ""}
        ]

    def test_drops_the_stores_own_domains_and_their_subdomains(self):
        own = [
            "https://a2ztobacco.com/products/x",
            "https://www.a2ztobacco.com/collections/y",
            "https://shop.a2ztobacco.com/",
            "https://a2z-tobacco.myshopify.com/blogs/news/z",
        ]
        assert clean_sources(own + [ELEMENT], STORE) == [ELEMENT]

    def test_does_not_drop_lookalike_domains(self):
        lookalike = {"url": "https://nota2ztobacco.com/", "title": ""}
        assert clean_sources([lookalike], STORE) == [lookalike]

    def test_accepts_legacy_url_strings_and_skips_blanks(self):
        assert clean_sources(["", None, LOOSELEAF_A["url"], {"url": "  "}]) == [LOOSELEAF_A]


class TestRenderSourcesHtml:
    def test_renders_one_link_per_host_without_own_site(self):
        untitled_repeat = {"url": "https://looseleaf.com/collections/slugs", "title": ""}
        html = render_sources_html([LOOSELEAF_A, untitled_repeat, "https://a2ztobacco.com/p"], STORE)
        assert html == (
            '<h2 class="a2z-news__sources-title">Sources</h2>\n<ul class="a2z-news__sources">'
            '<li><a href="https://looseleaf.com/blogs/news/a" target="_blank" '
            'rel="nofollow noopener">looseleaf.com</a></li></ul>'
        )

    def test_returns_empty_when_only_own_site_remains(self):
        assert render_sources_html(["https://a2ztobacco.com/p"], STORE) == ""


from app.services.news_body import outbound_sources, with_sources_block

A2Z_STORE = {"primary_domain": "a2ztobacco.com", "domain": "tobacco-nation.myshopify.com"}
CITING = ('<p>Per the <a href="https://www.fda.gov/tobacco-products/rules">FDA rules</a> and '
          '<a href="https://a2ztobacco.com/collections/cigars">our cigars</a>, see '
          '<a href="/products/x">this</a>.</p>')


class TestBlogSources:
    def test_only_outside_links_become_sources(self):
        assert outbound_sources(CITING, A2Z_STORE) == [{"url": "https://www.fda.gov/tobacco-products/rules", "title": "FDA rules"}]

    def test_block_lists_them_once_even_when_run_twice(self):
        once = with_sources_block(CITING, A2Z_STORE)
        assert (with_sources_block(once, A2Z_STORE) == once, once.count('class="a2z-blog__sources"')) == (True, 1)

    def test_no_outside_links_leaves_the_body_alone(self):
        body = '<p>See <a href="https://a2ztobacco.com/x">x</a>.</p>'
        assert with_sources_block(body, A2Z_STORE) == body
