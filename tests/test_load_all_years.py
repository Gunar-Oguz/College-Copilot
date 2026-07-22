"""Tests for load_all_years earnings field extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from load_all_years import (
    LAST_ACADEMIC_YEAR,
    api_prefix,
    fields_for_api_year,
    school_row,
)


def test_api_prefix_uses_latest_for_current_year():
    assert api_prefix(LAST_ACADEMIC_YEAR) == "latest"
    assert api_prefix(2020) == "2020"


def test_fields_for_latest_year_request_latest_earnings():
    fields = fields_for_api_year(LAST_ACADEMIC_YEAR)
    assert "latest.earnings.10_yrs_after_entry.median" in fields
    assert "2024.earnings.10_yrs_after_entry.median" not in fields


def test_fields_for_historical_year_request_year_prefixed_earnings():
    fields = fields_for_api_year(2020)
    assert "2020.earnings.10_yrs_after_entry.median" in fields
    assert "latest.earnings.10_yrs_after_entry.median" not in fields


def test_school_row_extracts_latest_earnings_for_2024():
    school = {
        "id": 243744,
        "school.name": "Stanford University",
        "school.city": "Stanford",
        "school.state": "CA",
        "latest.earnings.10_yrs_after_entry.median": 124080,
        "latest.student.size": 17249,
    }

    row = school_row(school, "2024-25", LAST_ACADEMIC_YEAR)

    assert row[18] == 124080  # earnings_median_10yr


def test_school_row_converts_online_only_to_bool():
    school = {
        "id": 1,
        "school.name": "Test U",
        "latest.student.size": 100,
        "school.online_only": 0,
    }

    row = school_row(school, "2024-25", LAST_ACADEMIC_YEAR)

    assert row[30] is False


def test_school_row_keeps_none_online_only():
    school = {
        "id": 1,
        "school.name": "Test U",
        "latest.student.size": 100,
    }

    row = school_row(school, "2024-25", LAST_ACADEMIC_YEAR)

    assert row[30] is None
