"""Pure-logic tests for the theme SEO snippet and the blog section patch.

Run with: pytest app/services/seo_snippets_spec.py
"""

from app.services.seo_snippets import (
    SNIPPET_VERSION,
    blog_section_patch_state,
    generate_seo_snippet,
    patch_blog_section,
    unpatch_blog_section,
)

# The live theme's sections/main-blog.liquid, trimmed to the parts the patch touches.
SECTION = """<div class="section container" data-section-type="blog-template">
  {% if section.settings.blog_show_title %}
    <div class="page-title opposing-items float-right">
      <h1 class="left">
        {{ blog.title }}
        {% for tag in current_tags %} / {{ tag }}{% endfor %}
      </h1>

      {% if section.settings.show_tags and blog.all_tags.size > 0 %}
      <ul class="right last tags plain">
        {% for tag in blog.all_tags %}<li>{{ tag | link_to_tag: tag }}</li>{% endfor %}
      </ul>
      {% endif %}
    </div>
  {% endif %}
  {% render 'social-sharing', share_title: blog.title %}
</div>"""


class TestPatchBlogSection:
    def test_both_parts_are_patched(self):
        patched, parts = patch_blog_section(SECTION)
        assert parts == ["h1", "tags"]
        assert "{{ blog.metafields.global.hub_h1 }}" in patched
        assert "blog.all_tags.size > 0 and current_tags.size > 0 %}" in patched

    def test_only_the_h1_title_is_replaced(self):
        patched, _ = patch_blog_section(SECTION)
        assert "share_title: blog.title %}" in patched
        assert patched.count("{{ blog.title }}") == 1

    def test_the_hub_h1_shows_only_on_page_one_without_tags(self):
        patched, _ = patch_blog_section(SECTION)
        assert "{% if current_tags == blank and current_page == 1 and blog.metafields.global.hub_h1 != blank %}" in patched

    def test_patching_twice_changes_nothing(self):
        once, _ = patch_blog_section(SECTION)
        assert patch_blog_section(once) == (once, [])

    def test_unpatch_restores_the_original_exactly(self):
        assert unpatch_blog_section(patch_blog_section(SECTION)[0]) == SECTION

    def test_unpatch_restores_a_whitespace_trimmed_title_exactly(self):
        section = SECTION.replace("{{ blog.title }}\n", "{{- blog.title -}}\n", 1)
        assert unpatch_blog_section(patch_blog_section(section)[0]) == section

    def test_a_section_without_a_title_h1_is_left_alone(self):
        section = "<h2>{{ blog.title }}</h2>"
        assert patch_blog_section(section) == (section, [])


class TestBlogSectionPatchState:
    def test_states(self):
        assert [blog_section_patch_state(c) for c in (SECTION, patch_blog_section(SECTION)[0], "<p></p>", None)] == [
            "missing", "installed", "unsupported", "unsupported"]


class TestGenerateSeoSnippet:
    def test_the_header_names_the_current_version(self):
        assert generate_seo_snippet().startswith(f"{{% comment %}} SEO Fixes Snippet {SNIPPET_VERSION} ")

    def test_tag_and_paginated_blog_views_are_noindexed(self):
        assert '{%- if current_tags or current_page > 1 -%}\n<meta name="robots" content="noindex, follow">' in \
            generate_seo_snippet()

    def test_the_landing_article_url_is_noindexed(self):
        snippet = generate_seo_snippet()
        guard = "{% if template contains 'article' and article.tags contains 'landing-page' %}"
        assert snippet[snippet.index(guard):].split("{% endif %}")[0] == \
            guard + '\n<meta name="robots" content="noindex, follow">\n'

    def test_liquid_blocks_are_balanced(self):
        snippet = generate_seo_snippet()
        opens = snippet.count("{% if ") + snippet.count("{%- if ")
        closes = snippet.count("{% endif %}") + snippet.count("{%- endif -%}")
        assert opens == closes


from app.services.schema_generator import strip_faq_microdata

FAQ_HTML = ('<div style="x" itemscope itemtype="https://schema.org/FAQPage"><div style="m" itemscope '
            'itemprop="mainEntity" itemtype="https://schema.org/Question"><h3 itemprop="name">Q?</h3>'
            '<div itemscope itemprop="acceptedAnswer" itemtype="https://schema.org/Answer">'
            '<p itemprop="text">A.</p></div></div></div>')


class TestStripFaqMicrodata:
    def test_removes_every_faq_attribute_and_keeps_text(self):
        assert strip_faq_microdata(FAQ_HTML) == (
            '<div style="x"><div style="m"><h3>Q?</h3><div><p>A.</p></div></div></div>')
