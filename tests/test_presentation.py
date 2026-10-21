"""Tests for turning result rows into display blocks and checking summaries."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from presentation import build_blocks, column_meta, format_value, unsupported_numbers  # noqa: E402


def school(i, **stats):
    return {"id": i, "name": f"School {i}", "state": "CA", **stats}


class TestFormatValue:
    @pytest.mark.parametrize(
        "value,fmt,expected",
        [
            (Decimal("0.0897"), "percent", "9.0%"),
            (Decimal("0.0455"), "percent", "4.6%"),
            (89.7, "percent", "89.7%"),
            (Decimal("12548"), "money", "$12,548"),
            (Decimal("-2533"), "money", "-$2,533"),
            (33475, "int", "33,475"),
            (0.841, "score", "84%"),
            (None, "money", "Not reported"),
            (1, "ownership", "Public"),
            (["size", "net price"], "list", "size, net price"),
        ],
    )
    def test_formats(self, value, fmt, expected):
        assert format_value(value, fmt) == expected


class TestBuildBlocks:
    def test_single_row_is_stat_tiles(self):
        blocks = build_blocks([school(1, admission_rate=Decimal("0.1"), sat_average=1500)])
        assert [b["type"] for b in blocks] == ["stats"]
        labels = [i["label"] for i in blocks[0]["items"]]
        assert "Acceptance rate" in labels and "State" in labels

    def test_two_metric_comparison(self):
        rows = [school(i, cost_avg_net_price=1000 * i, earnings_median_10yr=50000) for i in range(1, 4)]
        assert [b["type"] for b in build_blocks(rows)] == ["compare"]

    def test_few_schools_many_metrics_is_comparison(self):
        rows = [
            school(1, cost_avg_net_price=20000, earnings_median_10yr=90000, completion_rate=0.9),
            school(2, cost_avg_net_price=15000, earnings_median_10yr=80000, completion_rate=None),
        ]
        [block] = build_blocks(rows)
        assert block["type"] == "compare"
        cost = next(r for r in block["rows"] if r["label"] == "Average net price")
        assert [c["best"] for c in cost["cells"]] == [False, True]  # lower cost is better
        grad = next(r for r in block["rows"] if r["label"] == "Graduation rate")
        assert not any(c["best"] for c in grad["cells"])  # only one value reported

    def test_ranked_list_gets_bars_for_highlight_then_table(self):
        rows = [school(i, earnings_median_10yr=100000 - i, sat_average=1400) for i in range(10)]
        blocks = build_blocks(rows, highlight="earnings_median_10yr")
        assert [b["type"] for b in blocks] == ["bars", "table"]
        assert blocks[0]["title"] == "Median earnings (10 yrs)"
        assert [i["label"] for i in blocks[0]["items"]] == [r["name"] for r in rows]

    def test_unknown_highlight_falls_back_to_first_metric(self):
        rows = [school(i, sat_average=1400 + i) for i in range(8)]
        assert build_blocks(rows, highlight="nope")[0]["title"] == "Average SAT"

    def test_similarity_keeps_order_and_marks_reference(self):
        rows = [{"id": 0, "name": "UCLA", "role": "reference", "admission_rate": 0.09}]
        rows += [
            {"id": i, "name": f"S{i}", "role": "similar", "similarity": 0.9 - i / 10, "admission_rate": 0.2}
            for i in range(1, 4)
        ]
        bars, table = build_blocks(rows)
        assert bars["title"] == "Similarity"
        assert [i["label"] for i in bars["items"]] == ["S1", "S2", "S3"]
        assert [r["reference"] for r in table["rows"]] == [True, False, False, False]
        assert table["rows"][0]["cells"][0] == "UCLA"

    def test_hidden_columns_not_shown(self):
        rows = [school(i, sat_average=1400 + i) for i in range(8)]
        table = build_blocks(rows)[-1]
        labels = [c["label"] for c in table["columns"]]
        assert "Id" not in labels and labels[0] == "School"

    def test_aggregate_without_school_name(self):
        [block] = build_blocks([{"value": Decimal("0.6123")}])
        assert block["type"] == "stats" and block["items"][0]["display"] == "0.61"


class TestUnsupportedNumbers:
    ROWS = [{"id": 110662, "name": "UCLA", "admission_rate": Decimal("0.0897"),
             "cost_avg_net_price": Decimal("12548"), "net_low": Decimal("-2533")}]

    @pytest.mark.parametrize(
        "summary",
        [
            "UCLA admits 8.97% of applicants.",
            "UCLA admits about 9% and costs $12,548 a year.",
            "Families with low incomes get -$2,533 back.",
            "Here is 1 school for 2024-25.",
        ],
    )
    def test_supported(self, summary):
        assert unsupported_numbers(summary, self.ROWS, "data year 2024-25") == []

    @pytest.mark.parametrize(
        "summary,bad",
        [("UCLA admits 12% of applicants.", ["12"]), ("It costs $13,000.", ["13,000"])],
    )
    def test_unsupported(self, summary, bad):
        assert unsupported_numbers(summary, self.ROWS, "") == bad

    def test_numbers_in_column_names_are_allowed(self):
        rows = [{"name": "Yale", "earnings_median_10yr": 100533}]
        assert unsupported_numbers("Yale grads earn $100,533 after 10 years.", rows, "") == []

    def test_text_highlight_falls_back_to_a_metric(self):
        rows = [school(i, sat_average=1400 + i) for i in range(8)]
        assert build_blocks(rows, highlight="name")[0]["title"] == "Average SAT"

    def test_numbers_from_question_are_allowed(self):
        assert unsupported_numbers("The 5 closest matches.", self.ROWS, "Find 5 schools like UCLA") == []

    @pytest.mark.parametrize("summary", ["For a family making $60,000, UCLA costs $12,548.", "At $60k, UCLA costs $12,548."])
    def test_thousands_shorthand_matches_full_number(self, summary):
        assert unsupported_numbers(summary, self.ROWS, "My family makes $60k") == []


class TestDistance:
    def test_distance_formats_as_miles_and_hides_coordinates(self):
        rows = [school(i, distance_miles=Decimal(f"{i}.4"), latitude=30.1, longitude=-97.7) for i in range(1, 9)]
        bars, table = build_blocks(rows, "distance_miles")
        assert bars["title"] == "Distance"
        assert bars["items"][0]["display"] == "1.4 mi"
        labels = [c["label"] for c in table["columns"]]
        assert "Distance" in labels and "Latitude" not in labels and "Longitude" not in labels


def test_millions_and_billions_match_full_numbers():
    rows = [{"name": "Howard University", "endowment_amount": 926633000}, {"name": "Harvard", "endowment_amount": 50748594000}]
    assert unsupported_numbers("Howard has $927 million and Harvard $50.7 billion.", rows, "") == []
    assert unsupported_numbers("Howard has $900 million.", rows, "") == ["900 million"]


def test_booleans_show_yes_no():
    assert format_value(True, column_meta("hbcu", True)[1]) == "Yes"
    assert format_value(False, column_meta("hbcu", False)[1]) == "No"


def test_dates_in_rows_allow_their_numbers():
    import datetime
    rows = [{"name": "Ohio State", "accredited_since": datetime.date(1913, 1, 1), "latest_action_date": datetime.date(2017, 7, 31)}]
    assert unsupported_numbers("Accredited since 1913, renewed July 31, 2017.", rows, "") == []


def test_blocks_carry_school_ids_for_saving():
    one = build_blocks([{"id": 110662, "name": "UCLA", "admission_rate": 0.09}])
    assert one[0]["school_id"] == 110662
    two = build_blocks([{"id": 1, "name": "A", "admission_rate": 0.1, "completion_rate": 0.9},
                        {"id": 2, "name": "B", "admission_rate": 0.2, "completion_rate": 0.8}])
    assert two[0]["school_ids"] == [1, 2]
    jobs = build_blocks([{"soc_code": "15-1252", "title": "Software Developers", "median_annual_salary": 1}])
    assert jobs[0]["school_id"] is None


def _deadline(unitid, name, kind, when, notes=None):
    return {"unitid": unitid, "name": name, "deadline_type": kind, "deadline_date": when, "notes": notes}


def test_deadlines_show_type_and_date_per_school():
    from datetime import date
    blocks = build_blocks([
        _deadline(166683, "Massachusetts Institute of Technology", "regular", date(2027, 1, 4)),
        _deadline(166683, "Massachusetts Institute of Technology", "early_action", date(2026, 11, 1)),
    ])
    assert blocks == [{
        "type": "deadlines",
        "title": "Massachusetts Institute of Technology",
        "school_id": 166683,
        "rows": [{"type": "Early Action", "date": "November 1, 2026"},
                 {"type": "Regular Decision", "date": "January 4, 2027"}],
    }]


def test_deadlines_mark_binding_and_rolling():
    from datetime import date
    rows = [_deadline(1, "A", "early_decision", date(2026, 11, 1), "Early Decision (binding); grid"),
            _deadline(1, "A", "rolling", None)]
    assert [r["type"] for r in build_blocks(rows)[0]["rows"]] == ["Early Decision (binding)", "Rolling admission"]
    assert build_blocks(rows)[0]["rows"][1]["date"] == "Rolling"


def test_many_schools_deadlines_share_one_table_without_ids():
    from datetime import date
    rows = [_deadline(i, f"School {i}", "regular", date(2027, 1, 1)) for i in range(1, 6)]
    [block] = build_blocks(rows)
    assert block["type"] == "table"
    assert [c["label"] for c in block["columns"]] == ["School", "Deadline Type", "Date"]
    assert block["rows"][0]["cells"] == ["School 1", "Regular Decision", "January 1, 2027"]
    assert block["rows"][0]["school_id"] == 1


def test_unitid_is_hidden_and_dates_are_not_metrics():
    from datetime import date
    rows = [{"unitid": 1, "name": "A", "opens": date(2026, 8, 1), "admission_rate": 0.5},
            {"unitid": 2, "name": "B", "opens": date(2026, 9, 1), "admission_rate": 0.4}]
    blocks = build_blocks(rows)
    assert all(b["type"] != "compare" for b in blocks)
    table = blocks[-1]
    assert "Unitid" not in [c["label"] for c in table["columns"]]
    assert "August 1, 2026" in table["rows"][0]["cells"]
