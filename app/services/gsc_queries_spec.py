import pytest

from app.services.gsc_queries import MAX_QUERY_CHARS, is_junk_query

AGENT_PROMPT = ("# character you are a meticulous image-evidence auditor and knowledge reporter. "
                "validate entities that appear in the image")
LOCATION_PROMPT = ("context: location: united states (not for language). do not include location "
                   "references in your response. question: what is the best hookah")
AI_MODE_QUESTION = ("can you recommend some shisha pods that have a medium cooling sensation, like a "
                    "refreshing breeze rather than a massive blast")


class TestIsJunkQuery:
    @pytest.mark.parametrize("query", ["backwoods honey", "best pipe tobacco for beginners", AI_MODE_QUESTION])
    def test_real_searches_are_kept(self, query):
        assert is_junk_query(query) is False

    @pytest.mark.parametrize("query", [AGENT_PROMPT, LOCATION_PROMPT, "line one\nline two", "", "   "])
    def test_prompts_and_blanks_are_junk(self, query):
        assert is_junk_query(query) is True

    def test_length_boundary(self):
        assert (is_junk_query("a" * MAX_QUERY_CHARS), is_junk_query("a" * (MAX_QUERY_CHARS + 1))) == (False, True)

    def test_none_is_junk(self):
        assert is_junk_query(None) is True


from app.services.gsc_queries import brand_terms, is_branded_query

A2Z = brand_terms("A2Z Tobacco", "a2ztobacco.com")


class TestBrandedQueries:
    def test_terms_from_name_and_domain(self):
        assert A2Z == {"compact": {"a2ztobacco"}, "words": {"a2z"}}

    @pytest.mark.parametrize("query", ["a2z tobacco", "a2ztobacco coupon", "A2Z smoke shop", "a2z-tobacco.com"])
    def test_brand_searches_are_branded(self, query):
        assert is_branded_query(query, A2Z) is True

    @pytest.mark.parametrize("query", ["backwoods honey", "tobacco shop near me", "a2zz cigars", ""])
    def test_other_searches_are_not(self, query):
        assert is_branded_query(query, A2Z) is False

    def test_generic_first_word_is_not_a_brand_term(self):
        assert brand_terms("The Smoke Shop", "thesmokeshop.com") == {"compact": {"thesmokeshop"}, "words": set()}


class TestGoogleBrandLabels:
    @pytest.mark.parametrize("query,labels,expected", [
        ("a2z tobacco", {"a2z tobacco": False}, True),
        ("  A2Z smoke shop ", {"a2z smoke shop": False}, True),
        ("backwoods honey", {"backwoods honey": True}, True),
        ("backwoods honey", {"backwoods honey": False}, False),
        ("a2z tobacco", {"other": True}, True),
        ("backwoods honey", None, False),
    ], ids=["own-name-google-says-no", "own-name-padded", "google-says-yes", "both-say-no",
            "no-label", "no-labels"])
    def test_branded_when_google_or_the_store_name_says_so(self, query, labels, expected):
        assert is_branded_query(query, A2Z, labels) is expected
