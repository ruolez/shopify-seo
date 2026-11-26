from app.services.blog_template import blog_section_marker, generate_blog_section


class TestFaqMarkupGate:
    def test_off_leaves_no_faq_jsonld(self):
        section = generate_blog_section(emit_faq=False)
        assert ("faq_jsonld" in section, blog_section_marker(False) in section) == (False, True)

    def test_on_prints_the_stored_faq(self):
        section = generate_blog_section(emit_faq=True)
        assert ("article.metafields.global.faq_jsonld" in section, blog_section_marker(True) in section) == (True, True)

    def test_markers_tell_the_two_apart(self):
        assert (blog_section_marker(True) in generate_blog_section(emit_faq=False),
                blog_section_marker(False) in generate_blog_section(emit_faq=True)) == (False, False)
