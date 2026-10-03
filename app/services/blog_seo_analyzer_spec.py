"""Article scoring rules (settings stubbed to defaults; no DB).

Run with: pytest app/services/blog_seo_analyzer_spec.py
"""

import pytest

from app.prompt_defaults import DEFAULTS
from app.services import blog_seo_analyzer
from app.services.blog_seo_analyzer import analyze_article_seo

WORDS_300 = " ".join(["leaf"] * 320)
BODY = (
    f"<h2>One</h2><p>{WORDS_300}</p><h2>Two</h2><h3>Detail</h3><p>{WORDS_300}</p>"
    '<a href="/collections/a">a</a><a href="/products/b">b</a><a href="/blogs/news/c">c</a>'
    '<figure><img src="x.jpg" alt="x"></figure><figure><img src="y.jpg" alt="y"></figure>'
)
PERFECT = {
    "meta_title": "M" * 42, "meta_description": "D" * 150, "body": BODY, "summary": "S" * 100,
    "image_url": "https://cdn/x.jpg", "image_alt": "Hero",
}


@pytest.fixture(autouse=True)
def default_rules(monkeypatch):
    monkeypatch.setattr(blog_seo_analyzer, "get_setting", lambda key, default=None: DEFAULTS[key])


class TestAnalyzeArticleSeo:
    def test_a_600_word_article_with_no_table_or_faq_can_score_100(self):
        assert analyze_article_seo({**PERFECT, "article_type": "blog"})["score"] == 100

    def test_table_and_faq_are_not_scored(self):
        assert "structure" not in analyze_article_seo({**PERFECT, "article_type": "blog"})["breakdown"]

    def test_thin_blog_post_loses_points_for_length(self):
        thin = {**PERFECT, "article_type": "blog", "body": BODY.replace(WORDS_300, "short")}
        result = analyze_article_seo(thin)
        assert result["breakdown"]["word_count"]["score"] < 100
        assert result["score"] < 100

    def test_news_is_not_scored_on_length(self):
        short_news = {**PERFECT, "article_type": "news", "body": BODY.replace(WORDS_300, "short")}
        result = analyze_article_seo(short_news)
        assert "word_count" not in result["breakdown"]
        assert result["score"] == 100
