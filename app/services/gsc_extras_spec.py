from app.services.gsc_extras import brand_daily_values, query_label_values


def _row(keys, clicks=1, impressions=10, position=3.0):
    return {"keys": keys, "clicks": clicks, "impressions": impressions, "position": position}


class TestBrandDailyValues:
    def test_maps_labels_to_booleans_and_drops_unknown(self):
        rows = [_row(["2026-11-01", "BRANDED"], 4, 40, 2.0), _row(["2026-11-01", "NON_BRANDED"], 9, 900, 18.0),
                _row(["2026-11-01", "SOMETHING_NEW"]), _row(["2026-11-01"])]
        assert brand_daily_values(rows) == [("2026-11-01", True, 4, 40, 2.0), ("2026-11-01", False, 9, 900, 18.0)]


class TestQueryLabelValues:
    def test_lowercases_and_trims_queries(self):
        assert query_label_values([_row([" A2Z Tobacco ", "BRANDED"]), _row(["red velvet backwoods", "NON_BRANDED"])]) == {
            "a2z tobacco": True, "red velvet backwoods": False}

    def test_query_seen_under_both_labels_counts_as_branded(self):
        rows = [_row(["a2z vape", "NON_BRANDED"]), _row(["a2z vape", "BRANDED"]), _row(["a2z vape", "NON_BRANDED"])]
        assert query_label_values(rows) == {"a2z vape": True}

    def test_skips_blank_queries_and_unknown_labels(self):
        assert query_label_values([_row(["", "BRANDED"]), _row(["x", "MAYBE"]), _row(["y"])]) == {}
