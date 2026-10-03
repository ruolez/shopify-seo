"""Pure-logic tests for reading URL Inspection results (no API, no DB).

Run with: pytest app/services/index_coverage_spec.py
"""

from app.services.index_coverage import explain_state, parse_inspection

INDEXED = {"inspectionResult": {"indexStatusResult": {
    "verdict": "PASS", "coverageState": "Submitted and indexed",
    "lastCrawlTime": "2026-09-30T04:12:09Z", "googleCanonical": "https://a2ztobacco.com/products/x",
}}}
CRAWLED_NOT_INDEXED = {"inspectionResult": {"indexStatusResult": {
    "verdict": "NEUTRAL", "coverageState": "Crawled - currently not indexed",
    "lastCrawlTime": "2026-08-01T10:00:00Z",
}}}


class TestParseInspection:
    def test_indexed_page(self):
        assert parse_inspection(INDEXED) == {
            "indexed": True, "coverage_state": "Submitted and indexed",
            "last_crawl_time": "2026-09-30T04:12:09Z",
            "google_canonical": "https://a2ztobacco.com/products/x",
        }

    def test_crawled_but_not_indexed_page(self):
        parsed = parse_inspection(CRAWLED_NOT_INDEXED)
        assert parsed["indexed"] is False
        assert parsed["coverage_state"] == "Crawled - currently not indexed"
        assert parsed["google_canonical"] is None

    def test_empty_result_is_unknown_and_not_indexed(self):
        assert parse_inspection({}) == {
            "indexed": False, "coverage_state": "Unknown", "last_crawl_time": None, "google_canonical": None,
        }


class TestExplainState:
    def test_known_states_get_plain_language_and_an_action(self):
        why, action = explain_state("Crawled - currently not indexed")
        assert "chose not to index" in why
        assert action

    def test_unknown_state_falls_back_to_a_generic_explanation(self):
        why, action = explain_state("Some new state Google invented")
        assert why and action
