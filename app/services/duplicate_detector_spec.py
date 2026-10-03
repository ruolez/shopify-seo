"""Pure-logic tests for near-duplicate body detection (no DB).

Run with: pytest app/services/duplicate_detector_spec.py
"""

from app.services.duplicate_detector import MIN_BODY_WORDS, find_similar_bodies

BASE = " ".join(f"word{i}" for i in range(120))
UNRELATED = " ".join(f"other{i}" for i in range(120))


def _item(item_id, body, item_type="product"):
    return {"id": item_id, "type": item_type, "title": f"Item {item_id}", "body": f"<p>{body}</p>"}


class TestFindSimilarBodies:
    def test_groups_bodies_that_differ_by_a_few_words(self):
        twin = BASE.replace("word5 ", "Swisher ").replace("word60 ", "Grape ")
        groups = find_similar_bodies([_item(1, BASE), _item(2, twin), _item(3, UNRELATED)])
        assert [[i["id"] for i in g["items"]] for g in groups] == [[1, 2]]
        assert groups[0]["similarity"] >= 80

    def test_identical_bodies_are_100_percent_similar(self):
        assert find_similar_bodies([_item(1, BASE), _item(2, BASE)])[0]["similarity"] == 100

    def test_different_bodies_form_no_group(self):
        assert find_similar_bodies([_item(1, BASE), _item(2, UNRELATED)]) == []

    def test_short_bodies_are_ignored(self):
        short = " ".join(["same"] * (MIN_BODY_WORDS - 1))
        assert find_similar_bodies([_item(1, short), _item(2, short)]) == []

    def test_only_compares_within_one_item_type(self):
        assert find_similar_bodies([_item(1, BASE, "product"), _item(2, BASE, "collection")]) == []

    def test_chains_join_into_one_group(self):
        a = BASE
        b = BASE.replace("word5 ", "x5 ").replace("word6 ", "x6 ")
        c = b.replace("word100 ", "y100 ").replace("word101 ", "y101 ")
        groups = find_similar_bodies([_item(1, a), _item(2, b), _item(3, c)])
        assert [i["id"] for i in groups[0]["items"]] == [1, 2, 3]

    def test_body_text_is_not_returned(self):
        group = find_similar_bodies([_item(1, BASE), _item(2, BASE)])[0]
        assert "body" not in group["items"][0]
