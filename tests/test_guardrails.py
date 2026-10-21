"""
Tests for the answer guardrails (scope hints, non-degree filtering, notes).

Run from the repo root:
    python -m pytest tests/test_guardrails.py -v
"""

import sys
from pathlib import Path

import psycopg2
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from database import get_db_connection  # noqa: E402
from guardrails import (  # noqa: E402
    COLLEGES_HINT,
    UNIVERSITY_HINT,
    check_answer,
    question_hints,
    ranking_basis,
    requested_count,
)


def _db_available() -> bool:
    try:
        get_db_connection().close()
        return True
    except psycopg2.Error:
        return False


@pytest.mark.parametrize("question,hint", [
    ("Top 10 universities in California by graduation rate", UNIVERSITY_HINT),
    ("Top ranked university in Washington", UNIVERSITY_HINT),
    ("Cheapest four-year colleges in Ohio", UNIVERSITY_HINT),
    ("Top 5 public colleges in California by graduation rate", COLLEGES_HINT),
    ("Which 5 universities spend the most on research?", COLLEGES_HINT),
    ("Which colleges are within 30 miles of Austin?", COLLEGES_HINT),
    ("How many for-profit colleges are there?", ""),
    ("What is the average acceptance rate of public colleges in Ohio?", ""),
    ("Which schools have the most students?", ""),
    ("What is the graduation rate at Columbia College in Missouri?", ""),
])
def test_question_hints(question, hint):
    assert question_hints(question) == hint


@pytest.mark.parametrize("question,count", [
    ("Top 10 schools in California", 10),
    ("Which 5 schools have the lowest acceptance rate?", 5),
    ("Show me 3 colleges like MIT", 3),
    ("Colleges within 30 miles of Austin", None),
    ("My family makes $85,000. Which schools cost the least?", None),
    ("Top ranked university in Washington", None),
])
def test_requested_count(question, count):
    assert requested_count(question) == count


def test_ranking_basis_reads_order_by():
    sql = ("SELECT id, name FROM schools WHERE year = '2024-25' "
           "ORDER BY schools.completion_rate DESC NULLS LAST, s.earnings_median_10yr DESC LIMIT 10")
    assert ranking_basis(sql) == "graduation rate, then median earnings (10 yrs)"
    assert ranking_basis("SELECT 1") is None


def test_notes_for_rankings_and_short_lists():
    rows = [{"name": "A", "completion_rate": 0.9}, {"name": "B", "completion_rate": 0.8}]
    _, notes = check_answer("Top 10 schools in Ohio", rows, "SELECT ... ORDER BY completion_rate DESC")
    assert any("fewer than the 10" in n for n in notes)
    assert any("ordered by graduation rate" in n for n in notes)


@pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")
def test_college_lists_drop_non_degree_programs():
    ukiah, ucla = 481182, 110662  # Ukiah Adult School (non-degree), UCLA
    rows = [{"id": ukiah, "name": "Ukiah Adult School"}, {"id": ucla, "name": "UCLA"}]
    kept, notes = check_answer("Top colleges in California by graduation rate", rows, "")
    assert [r["id"] for r in kept] == [ucla]
    assert "Left out 1" in notes[0]
    kept, _ = check_answer("Top schools in California by graduation rate", rows, "")
    assert len(kept) == 2  # "schools" keeps every institution
