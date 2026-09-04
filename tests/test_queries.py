"""
CollegeCopilot query engine tests.

Run from backend/:
    python -m pytest ../tests/test_queries.py -v
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from database import execute_plan, fetch_meta_stats, run_query_pipeline  # noqa: E402
from query_engine import build_query_plan  # noqa: E402
from data_coverage import format_coverage_header, sparse_filter_columns  # noqa: E402
from query_parser import parse_question  # noqa: E402


def _rows(question: str) -> list[dict]:
    plan = build_query_plan(question)
    return execute_plan(plan)


def _plan(question: str):
    return build_query_plan(question)


def _names(rows: list[dict]) -> list[str]:
    return [row["name"] for row in rows]


# --- 1. Basic queries ---


class TestBasicQueries:
    def test_school_count_in_database(self):
        stats = fetch_meta_stats()
        assert stats["total_schools"] > 6000

    def test_meta_question_plan(self):
        plan = _plan("How many schools in database?")
        assert plan.kind == "meta"
        assert plan.use_claude is False

    def test_meta_question_extracts_california(self):
        parsed = parse_question("How many schools are in California?")
        assert parsed.kind == "meta"
        assert parsed.states == ["CA"]

    def test_meta_question_sql_filters_by_state(self):
        plan = _plan("How many schools are in California?")
        assert plan.kind == "meta"
        assert "state = %s" in plan.sql
        assert "CA" in plan.params
        assert "total_schools" in plan.sql
        assert "2024-25" in plan.params

    def test_meta_question_no_state_counts_all(self):
        plan = _plan("How many schools are in the database?")
        assert plan.kind == "meta"
        assert "state =" not in plan.sql
        assert "state IN" not in plan.sql

    def test_top5_lowest_acceptance_rate_limit(self):
        rows = _rows("Top 5 by lowest acceptance rate")
        assert len(rows) == 5

    def test_top5_includes_stanford_and_harvard(self):
        names = _names(_rows("Top 5 by lowest acceptance rate"))
        assert any("Stanford" in n for n in names)
        assert any("Harvard" in n for n in names)

    def test_top10_includes_mit(self):
        names = _names(_rows("Top 10 by lowest acceptance rate"))
        assert any("MIT" in n or "Massachusetts Institute of Technology" in n for n in names)


# --- 2. State filters ---


class TestStateFilters:
    @pytest.mark.parametrize(
        "question,state",
        [
            ("Schools in California", "CA"),
            ("Schools in Texas", "TX"),
        ],
    )
    def test_all_results_match_state(self, question, state):
        rows = _rows(question)
        assert len(rows) > 10
        assert all(row["state"] == state for row in rows)

    def test_california_returns_many_schools(self):
        rows = _rows("Schools in California")
        assert len(rows) > 100

    def test_no_default_limit_on_state_query(self):
        plan = _plan("Schools in California")
        assert plan.limit is None


# --- 3. Numeric filters ---


class TestNumericFilters:
    def test_acceptance_above_50_percent(self):
        rows = _rows("Acceptance rate above 50%")
        assert len(rows) > 10
        for row in rows:
            assert row["admission_rate"] is not None
            assert float(row["admission_rate"]) > 0.50

    def test_net_cost_under_20000(self):
        rows = _rows("Net cost under $20,000")
        assert len(rows) > 10
        for row in rows:
            assert row["cost_avg_net_price"] is not None
            assert float(row["cost_avg_net_price"]) < 20000


# --- 4. Income-based cost ---


class TestIncomeBasedCost:
    def test_85k_income_uses_75_110k_column(self):
        plan = _plan("Cost for family making $85,000")
        assert "cost_net_price_income_75_110k" in plan.sql

    def test_110k_income_uses_110k_plus_column(self):
        plan = _plan("My family makes $110,000")
        assert plan.parsed.cost_column == "cost_net_price_income_110k_plus"

    @pytest.mark.parametrize(
        "question,column",
        [
            ("family makes $25,000", "cost_net_price_income_0_30k"),
            ("family makes $40,000", "cost_net_price_income_30_48k"),
            ("family makes $60,000", "cost_net_price_income_48_75k"),
            ("family makes $90,000", "cost_net_price_income_75_110k"),
            ("family makes $110,000", "cost_net_price_income_110k_plus"),
            ("family makes $150,000", "cost_net_price_income_110k_plus"),
            ("income over $110,000", "cost_net_price_income_110k_plus"),
        ],
    )
    def test_income_bracket_mapping(self, question, column):
        parsed = parse_question(question)
        assert parsed.cost_column == column

    def test_110k_private_nonprofit_cost_query_returns_ten(self):
        question = (
            "My family makes $110,000. Private nonprofit, acceptance above 40%, "
            "net cost under $30,000, sort by cost, top 10."
        )
        plan = _plan(question)
        rows = _rows(question)

        assert plan.parsed.cost_column == "cost_net_price_income_110k_plus"
        assert plan.parsed.acceptance_rate_min == 0.40
        assert plan.parsed.ownership == 2
        assert plan.parsed.limit == 10
        assert "cost_net_price_income_110k_plus" in plan.sql
        assert "ORDER BY cost_net_price_income_110k_plus ASC" in plan.sql
        assert len(rows) == 10

    def test_75k_income_has_actual_cost_data(self):
        """48-75k bracket has data in College Scorecard load."""
        rows = _rows("Schools with cost data for family making $75,000")
        assert len(rows) > 0
        plan = _plan("Schools with cost data for family making $75,000")
        assert "cost_net_price_income_48_75k" in plan.sql
        assert any(row["cost_net_price_income_48_75k"] is not None for row in rows)

    def test_income_cost_values_are_numeric(self):
        rows = _rows("Schools in Washington under $15,000 for families making $75,000")
        assert len(rows) > 0
        for row in rows:
            cost = row["cost_net_price_income_48_75k"]
            assert cost is not None
            assert float(cost) < 15000


# --- 5. Comparison queries ---


class TestComparisonQueries:
    def test_compare_stanford_and_mit(self):
        rows = _rows("Compare Stanford and MIT")
        assert len(rows) >= 2
        names = " ".join(_names(rows))
        assert "Stanford" in names
        assert "MIT" in names or "Massachusetts Institute of Technology" in names

    def test_comparison_returns_same_columns(self):
        rows = _rows("Compare Stanford and MIT")
        keys = set(rows[0].keys())
        for row in rows[1:]:
            assert set(row.keys()) == keys


# --- 7. BETWEEN acceptance rate filters ---


class TestBetweenAcceptanceRate:
    FULL_QUESTION = (
        "Schools in WA or OR with acceptance rate between 40% and 70%, "
        "graduation rate above 50%, earnings above $45,000"
    )
    USER_QUESTION = (
        "Schools in WA or OR, acceptance rate between 40-70%, "
        "graduation rate above 50%, earnings above $45,000"
    )

    def test_wa_or_between_returns_exactly_three_schools(self):
        rows = _rows(self.FULL_QUESTION)
        assert len(rows) == 3

    def test_user_hyphen_question_returns_exactly_three_schools(self):
        rows = _rows(self.USER_QUESTION)
        assert len(rows) == 3

    def test_parsed_filters_match_expected_structure(self):
        parsed = parse_question(self.USER_QUESTION)
        assert parsed.states == ["WA", "OR"]
        assert parsed.acceptance_rate_min == 0.40
        assert parsed.acceptance_rate_max == 0.70
        assert parsed.graduation_rate_min == 0.50
        assert parsed.earnings_min == 45000
        assert parsed.limit is None
        assert parsed.sort_by == "earnings_median_10yr"
        assert parsed.sort_order == "DESC"

    def test_all_acceptance_rates_within_bounds(self):
        rows = _rows(self.FULL_QUESTION)
        for row in rows:
            rate = float(row["admission_rate"])
            assert 0.40 <= rate <= 0.70

    def test_sql_uses_between_clause(self):
        plan = _plan(self.FULL_QUESTION)
        assert "admission_rate BETWEEN" in plan.sql
        assert plan.use_claude is False

    def test_above_and_below_parsed_as_between(self):
        question = (
            "Schools in WA or OR with acceptance rate above 40% and below 70%, "
            "graduation rate above 50%, earnings above $45,000"
        )
        rows = _rows(question)
        assert len(rows) == 3
        for row in rows:
            rate = float(row["admission_rate"])
            assert 0.40 <= rate <= 0.70

    @pytest.mark.parametrize(
        "question",
        [
            "acceptance rate between 40% and 70%",
            "acceptance between 40% and 70%",
            "acceptance rate from 40% to 70%",
            "between 40% and 70% acceptance rate",
        ],
    )
    def test_between_parsing_applies_upper_bound(self, question):
        rows = _rows(question)
        assert len(rows) > 0
        for row in rows:
            rate = float(row["admission_rate"])
            assert 0.40 <= rate <= 0.70


class TestDataCoverage:
    def test_sparse_columns_detected_for_acceptance_filter(self):
        parsed = parse_question("Schools in CA with acceptance above 50%")
        assert "admission_rate" in sparse_filter_columns(parsed)

    def test_coverage_header_with_results(self):
        header = format_coverage_header(45, [("admission_rate", "acceptance rates", 1910)])
        assert "Found 45 schools" in header
        assert "1,910" in header
        assert "acceptance rates" in header

    def test_coverage_header_zero_results_explains_missing_data(self):
        header = format_coverage_header(0, [("admission_rate", "acceptance rates", 1910)])
        assert "No schools match" in header
        assert "1,910" in header
        assert "acceptance rates" in header

    def test_coverage_header_no_reporting_data(self):
        header = format_coverage_header(
            0, [("cost_net_price_income_75_110k", "cost data for $75k–$110k income", 0)]
        )
        assert "No schools" in header
        assert "do not report" in header or "No schools in your search area report" in header


class TestSortQueries:
    def test_sort_by_earnings_with_filters(self):
        question = (
            "Schools with acceptance rate between 40% and 70% "
            "sort by earnings highest first"
        )
        plan = _plan(question)
        rows = _rows(question)

        assert plan.parsed.sort_by == "earnings_median_10yr"
        assert plan.parsed.sort_order == "DESC"
        assert "ORDER BY earnings_median_10yr DESC" in plan.sql
        assert len(rows) > 1
        earnings = [float(r["earnings_median_10yr"]) for r in rows]
        assert earnings == sorted(earnings, reverse=True)

    def test_sort_by_earnings_standalone(self):
        plan = _plan("Sort by earnings highest first")
        rows = _rows("Sort by earnings highest first")

        assert plan.parsed.sort_by == "earnings_median_10yr"
        assert "ORDER BY earnings_median_10yr DESC" in plan.sql
        assert float(rows[0]["earnings_median_10yr"]) >= float(rows[1]["earnings_median_10yr"])

    def test_lowest_acceptance_rate_sort(self):
        plan = _plan("Top 5 by lowest acceptance rate")
        rows = _rows("Top 5 by lowest acceptance rate")

        assert plan.parsed.sort_by == "admission_rate"
        assert plan.parsed.sort_order == "ASC"
        assert len(rows) == 5
        rates = [float(r["admission_rate"]) for r in rows if r["admission_rate"]]
        assert rates == sorted(rates)


class TestComplexQueries:
    def test_ca_acceptance_and_cost_with_income(self):
        question = (
            "Schools in CA with acceptance > 30% and cost < $25,000 for $85K income"
        )
        plan = _plan(question)
        rows = _rows(question)

        assert "state = %s" in plan.sql or "state" in plan.sql
        assert "admission_rate" in plan.sql
        assert "cost_net_price_income_75_110k" in plan.sql
        assert plan.limit is None

    def test_ca_acceptance_and_cost_75k_income_returns_multiple(self):
        """Uses 48-75k bracket which has loaded data."""
        question = (
            "Schools in CA with acceptance > 30% and cost < $25,000 for $75K income"
        )
        rows = _rows(question)
        assert len(rows) > 10
        for row in rows:
            assert row["state"] == "CA"
            assert float(row["admission_rate"]) > 0.30
            assert float(row["cost_net_price_income_48_75k"]) <= 25000
