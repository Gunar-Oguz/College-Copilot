"""Tests for turning result rows into display blocks and checking summaries."""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from presentation import build_blocks, format_value, unsupported_numbers  # noqa: E402


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
