"""Pure-logic tests for the blog hub body.

Run with: pytest app/services/blog_landing_generator_spec.py
"""

import random
import re

from app.prompt_defaults import DEFAULTS
from app.services.blog_landing_generator import (
    MORE_GUIDES,
    assign_topic,
    assign_topics,
    cdn_width,
    generate_blog_landing_html,
    hub_keyword_conflicts,
)
from app.services.keyword_identity import variant_key

TOPICS = DEFAULTS["lists.blog_hub_topics"]
BLOG = "news"


def _topic_named(name):
    return next(i for i, t in enumerate(TOPICS) if t["name"] == name)


def _article(n, title, published="2026-09-01T10:00:00", **extra):
    return {"title": title, "handle": f"guide-{n}", "published_at": published, "is_published": True,
            "tags": "", "body": "", **extra}


def _hrefs(page):
    return re.findall(r'href="([^"]+)"', page)


def _archive(page):
    return page[page.index('<div class="blog-landing__archive">'):]


class TestAssignTopic:
    def test_the_earliest_term_in_the_title_decides(self):
        article = _article(1, "The Real Difference: Filtered Cigars vs Cigarillos")
        assert assign_topic(article, TOPICS) == _topic_named("Filtered Cigars")

    def test_a_longer_term_wins_at_the_same_position(self):
        article = _article(1, "Filtered Cigars vs Cigarettes")
        assert assign_topic(article, TOPICS) == _topic_named("Filtered Cigars")

    def test_plurals_match_their_singular_term(self):
        assert assign_topic(_article(1, "2026 Guide to Zyns"), TOPICS) == _topic_named("Nicotine Pouches")

    def test_terms_match_whole_words_only(self):
        assert assign_topic(_article(1, "Cigar Wrapper Types Explained"), TOPICS) == _topic_named("Cigars")

    def test_the_title_beats_the_collection_link(self):
        article = _article(1, "Best Hookah Flavors", body='<a href="/collections/zyn">Zyn</a>')
        assert assign_topic(article, TOPICS) == _topic_named("Hookah & Shisha")

    def test_the_primary_keyword_decides_when_the_title_names_no_topic(self):
        article = _article(1, "Stop Overpaying: The Truth", primary_keyword="cherokee pipe tobacco, budget")
        assert assign_topic(article, TOPICS) == _topic_named("Pipe Tobacco")

    def test_the_first_collection_link_decides_last(self):
        article = _article(1, "Keeping It Fresh", body='<p><a href="https://x.com/collections/al-fakher">x</a></p>'
                                                         '<a href="/collections/zyn">y</a>')
        assert assign_topic(article, TOPICS) == _topic_named("Hookah & Shisha")

    def test_no_signal_gives_no_topic(self):
        assert assign_topic(_article(1, "Keeping Tobacco Fresh: A Storage Guide"), TOPICS) is None


class TestAssignTopics:
    def test_unclaimed_guides_go_to_a_trailing_more_guides_group(self):
        groups = assign_topics([_article(1, "Keeping Tobacco Fresh"), _article(2, "Swisher Flavors")], TOPICS)
        assert [(t["name"], [a["title"] for a in posts]) for t, posts in groups] == [
            ("Cigarillos", ["Swisher Flavors"]), (MORE_GUIDES["name"], ["Keeping Tobacco Fresh"])]

    def test_every_article_lands_in_exactly_one_group(self):
        rng = random.Random(7)
        words = ["swisher", "zyn", "hookah", "pipe", "blunt", "storage", "cigar", "filtered", "ryo", "fresh"]
        articles = [_article(i, " ".join(rng.sample(words, 3))) for i in range(200)]
        groups = assign_topics(articles, TOPICS)
        placed = [a["handle"] for _, posts in groups for a in posts]
        assert sorted(placed) == sorted(a["handle"] for a in articles)


def _random_articles(rng, count):
    titles = ["Swisher Sweets Ranked", "Zyn Strengths", "Hookah Setup", "Pipe Blends", "Blunt Wraps Ranked",
              "Keeping Tobacco Fresh", "Premium Cigars", "Filtered Cigars Ranked", "RYO Costs", "Fronto Leaf"]
    articles = []
    for i in range(count):
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        published = rng.choice([f"2025-{month:02d}-{day:02d}T10:00:00", None])
        articles.append(_article(i, f"{rng.choice(titles)} {i}", published=published,
                                 image_url=rng.choice(["", "https://cdn.shopify.com/s/files/a.jpg?v=1"])))
    return articles


class TestGenerateBlogLandingHtml:
    def test_every_guide_appears_exactly_once_in_the_archive_and_is_linked_from_the_page(self):
        rng = random.Random(11)
        for _ in range(25):
            articles = _random_articles(rng, rng.randint(1, 60))
            page = generate_blog_landing_html(articles, BLOG, topics=TOPICS)
            archive_links = _hrefs(_archive(page))
            expected = sorted(f"/blogs/{BLOG}/{a['handle']}" for a in articles)
            assert sorted(archive_links) == expected
            assert set(expected) <= set(_hrefs(page))

    def test_no_tag_archive_is_linked(self):
        articles = [_article(1, "Swisher Flavors", tags="swisher sweets, best swisher flavor")]
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS)
        assert [h for h in _hrefs(page) if "/tagged/" in h] == []

    def test_every_h2_keeps_the_class_the_theme_css_leaves_visible(self):
        articles = _random_articles(random.Random(3), 30)
        news = [_article(900, "FDA News", article_type="news")]
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS, news_articles=news, news_hub_url="/pages/news",
                                          clicks={f"/blogs/{BLOG}/guide-1": {"clicks": 5}})
        h2_tags = re.findall(r"<h2[^>]*>", page)
        assert [t for t in h2_tags if "blog-landing__section-title" not in t] == []

    def test_no_article_tag_is_used_inside_the_hub(self):
        # The theme CSS hides every <article> that is not the first one.
        page = generate_blog_landing_html(_random_articles(random.Random(5), 20), BLOG, topics=TOPICS)
        assert "<article" not in page

    def test_the_landing_article_is_never_listed(self):
        articles = [_article(1, "Swisher Flavors"), _article(2, "Hub", tags="landing-page")]
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS)
        assert f"/blogs/{BLOG}/guide-2" not in _hrefs(page)

    def test_start_here_lists_the_most_clicked_guides_first(self):
        articles = [_article(i, f"Swisher Flavor {i}") for i in range(6)]
        clicks = {f"/blogs/{BLOG}/guide-{i}": {"clicks": c} for i, c in [(2, 40), (5, 90), (0, 10)]}
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS, clicks=clicks)
        start = page[page.index("Start here"):page.index("</section>", page.index("Start here"))]
        assert _hrefs(start) == [f"/blogs/{BLOG}/guide-5", f"/blogs/{BLOG}/guide-2", f"/blogs/{BLOG}/guide-0"]

    def test_start_here_is_left_out_without_search_data(self):
        page = generate_blog_landing_html([_article(1, "Swisher Flavors")], BLOG, topics=TOPICS)
        assert "Start here" not in page

    def test_a_topic_links_its_collection_only_when_the_collection_exists(self):
        articles = [_article(1, "Swisher Flavors"), _article(2, "Hookah Setup")]
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS, collections={"cigarillos": "Cigarillos"})
        assert [h for h in _hrefs(page) if h.startswith("/collections/")] == ["/collections/cigarillos"]

    def test_news_headlines_link_to_the_news_hub(self):
        news = [_article(900 + i, f"News {i}", published=f"2026-09-0{i + 1}T10:00:00") for i in range(5)]
        page = generate_blog_landing_html([_article(1, "Swisher Flavors")], BLOG, topics=TOPICS,
                                          news_articles=news, news_hub_url="/pages/news")
        section = page[page.index("Industry news"):]
        section = section[:section.index("</section>")]
        assert _hrefs(section) == [f"/blogs/{BLOG}/guide-904", f"/blogs/{BLOG}/guide-903",
                                   f"/blogs/{BLOG}/guide-902", "/pages/news"]

    def test_intro_keeps_at_most_two_escaped_paragraphs(self):
        page = generate_blog_landing_html([_article(1, "Swisher")], BLOG, topics=TOPICS,
                                          intro_text="One <b>\n\nTwo\n\nThree")
        intro = page[page.index('<div class="blog-landing__intro">'):]
        intro = intro[:intro.index("</div>") + 6]
        assert intro == '<div class="blog-landing__intro"><p>One &lt;b&gt;</p><p>Two</p></div>'

    def test_start_here_guides_are_not_repeated_as_topic_cards(self):
        articles = [_article(i, f"Swisher Flavor {i}") for i in range(8)]
        clicks = {f"/blogs/{BLOG}/guide-{i}": {"clicks": 100 - i} for i in range(8)}
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS, clicks=clicks)
        topic = page[page.index('<section class="blog-landing__section blog-landing__topic">'):]
        grid = topic[topic.index('<div class="blog-landing__grid">'):topic.index('<div class="blog-landing__more">')]
        assert _hrefs(grid) == [f"/blogs/{BLOG}/guide-{i}" for i in (4, 5, 6)]

    def test_a_long_topic_keeps_every_guide_linked_with_the_overflow_collapsed(self):
        articles = [_article(i, f"Swisher Flavor {i}") for i in range(14)]
        page = generate_blog_landing_html(articles, BLOG, topics=TOPICS)
        topic = page[page.index('<section class="blog-landing__section blog-landing__topic">'):]
        topic = topic[:topic.index("</section>")]
        hidden = topic[topic.index('<details class="blog-landing__reveal">'):]
        assert (len(_hrefs(topic)), len(_hrefs(hidden))) == (14, 14 - 3 - 6)

    def test_entities_in_a_summary_are_not_escaped_twice(self):
        article = _article(1, "Black and Mild Flavors", summary="Black &amp; Mild <b>ranked</b>")
        page = generate_blog_landing_html([article], BLOG, topics=TOPICS, clicks={f"/blogs/{BLOG}/guide-1": {"clicks": 3}})
        assert ("Black &amp; Mild ranked" in page, "&amp;amp;" in page) == (True, False)

    def test_dates_are_machine_readable(self):
        page = generate_blog_landing_html([_article(1, "Swisher", published="2026-03-05T10:00:00")], BLOG, topics=TOPICS)
        assert '<time datetime="2026-03-05">Mar 5, 2026</time>' in page

    def test_an_empty_blog_says_so(self):
        assert "No articles published yet" in generate_blog_landing_html([], BLOG, topics=TOPICS)


def _owned(*pairs):
    return {variant_key(kw): {"keyword": kw, "item_type": t, "item_id": i} for kw, t, i in pairs}


OWNED = _owned(("pipe tobacco", "collection", 1), ("cigarillos", "collection", 2), ("zyn", "collection", 3),
               ("swisher sweets flavors", "article", 4))


class TestHubKeywordConflicts:
    def test_the_proposed_hub_h1_is_clear(self):
        assert hub_keyword_conflicts("Tobacco Guides, Reviews & Flavor Rankings", OWNED) == []

    def test_an_owned_head_term_inside_the_text_is_flagged(self):
        conflicts = hub_keyword_conflicts("Tobacco Guide: Pipe Tobacco, Wraps, Cigarillos & Pouches", OWNED)
        assert conflicts == [
            {"phrase": "cigarillos", "keyword": "cigarillos", "item_type": "collection", "item_id": 2},
            {"phrase": "pipe tobacco", "keyword": "pipe tobacco", "item_type": "collection", "item_id": 1},
        ]

    def test_word_order_and_plural_variants_count_as_the_same_keyword(self):
        assert [c["item_id"] for c in hub_keyword_conflicts("Swisher Sweet Flavor Guides", OWNED)] == [4]

    def test_a_brand_suffix_after_a_pipe_is_ignored(self):
        assert hub_keyword_conflicts("Tobacco Guides & Reviews | Zyn", OWNED) == []

    def test_empty_text_has_no_conflicts(self):
        assert hub_keyword_conflicts("", OWNED) == []


class TestCdnWidth:
    def test_a_shopify_cdn_url_gets_the_width_and_keeps_its_version(self):
        assert cdn_width("https://cdn.shopify.com/s/files/a.jpg?v=12&width=100", 600) == \
            "https://cdn.shopify.com/s/files/a.jpg?v=12&width=600"

    def test_other_urls_are_left_alone(self):
        assert cdn_width("https://example.com/a.jpg", 600) == "https://example.com/a.jpg"
