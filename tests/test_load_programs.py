"""Tests for program earnings field extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from load_programs import program_row


def test_program_row_extracts_1yr_and_4yr_earnings():
    program = {
        "code": "0403",
        "title": "Architecture.",
        "credential": {"level": 3, "title": "Bachelor's Degree"},
        "earnings": {
            "1_yr": {"overall_median_earnings": 56327},
            "4_yr": {"overall_median_earnings": 76808},
        },
        "debt": {
            "staff_grad_plus": {
                "all": {
                    "all_inst": {"median": 22000, "median_payment": 250},
                }
            }
        },
    }

    row = program_row(228778, "2024-25", program)

    assert row is not None
    assert row[6] == 56327  # earnings_median_1yr
    assert row[7] == 76808  # earnings_median_4yr


def test_program_row_returns_none_without_2yr_field():
    program = {
        "code": "0403",
        "title": "Architecture.",
        "credential": {"level": 3, "title": "Bachelor's Degree"},
        "earnings": {
            "1_yr": {"overall_median_earnings": 56327},
            "4_yr": {"overall_median_earnings": 76808},
        },
    }

    row = program_row(228778, "2024-25", program)

    assert row is not None
    assert row[7] == 76808
