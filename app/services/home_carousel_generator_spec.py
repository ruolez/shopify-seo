"""Pure-logic tests for the home carousel section injector (no DB, no network).

Covers inject_section_into_index / remove_section_from_index and the
render_home_carousel_section heading/loading structure.
Run with: pytest app/services/home_carousel_generator_spec.py
"""

import json

from app.services.home_carousel_generator import (
    SECTION_TYPE,
    inject_section_into_index,
    remove_section_from_index,
    render_home_carousel_section,
)

KEY = "ai_home_carousel"


def _index(order, sections):
    return json.dumps({"sections": sections, "order": order})


class TestInjectSectionIntoIndex:
    def test_prepends_key_to_order_and_registers_section(self):
        raw = _index(["main"], {"main": {"type": "main-content"}})
        result = json.loads(inject_section_into_index(raw, KEY))
        assert result["order"] == [KEY, "main"]
        assert result["sections"][KEY] == {"type": SECTION_TYPE, "settings": {}}

    def test_is_idempotent_on_reinjection(self):
        raw = _index(["main"], {"main": {"type": "main-content"}})
        once = inject_section_into_index(raw, KEY)
        twice = inject_section_into_index(once, KEY)
        result = json.loads(twice)
        assert result["order"] == [KEY, "main"]
        assert result["order"].count(KEY) == 1

    def test_handles_empty_index(self):
        result = json.loads(inject_section_into_index("", KEY))
        assert result["order"] == [KEY]
        assert result["sections"][KEY]["type"] == SECTION_TYPE

    def test_preserves_other_sections(self):
        raw = _index(["a", "b"], {"a": {"type": "x"}, "b": {"type": "y"}})
        result = json.loads(inject_section_into_index(raw, KEY))
        assert result["order"] == [KEY, "a", "b"]
        assert result["sections"]["a"] == {"type": "x"}


class TestRemoveSectionFromIndex:
    def test_removes_key_from_order_and_sections(self):
        raw = inject_section_into_index(_index(["main"], {"main": {"type": "m"}}), KEY)
        result = json.loads(remove_section_from_index(raw, KEY))
        assert result["order"] == ["main"]
        assert KEY not in result["sections"]

    def test_inject_then_remove_is_round_trip(self):
        original = {"sections": {"main": {"type": "m"}}, "order": ["main"]}
        raw = json.dumps(original)
        restored = json.loads(remove_section_from_index(inject_section_into_index(raw, KEY), KEY))
        assert restored == original

    def test_remove_absent_key_is_noop(self):
        raw = _index(["main"], {"main": {"type": "m"}})
        result = json.loads(remove_section_from_index(raw, KEY))
        assert result["order"] == ["main"]


class TestRenderHomeCarouselSection:
    def test_first_slide_is_h1_rest_h2_with_lcp_priority(self):
        home = {"slides": [
            {"headline": "First", "image_url": "https://cdn/a.jpg"},
            {"headline": "Second", "image_url": "https://cdn/b.jpg"},
        ]}
        out = render_home_carousel_section(home, {"name": "Shop", "domain": "shop.com"})
        assert out.count("<h1") == 1
        assert "<h2" in out
        assert 'fetchpriority="high"' in out
        assert 'loading="lazy"' in out

    def test_wrapped_in_raw_with_section_schema_and_no_json_ld(self):
        out = render_home_carousel_section({"slides": []}, {"name": "Shop", "domain": "shop.com"})
        assert out.startswith("{% raw %}")
        assert "{% endraw %}" in out
        assert "{% schema %}" in out
        assert "application/ld+json" not in out
