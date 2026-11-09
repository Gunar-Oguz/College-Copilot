"""
Tests for the DuckDB backend used by the Streamlit demo: the read-only query runner, the
validator's DuckDB rules, PostgreSQL-compatible behavior, miles_between(), and the Python
pg_trgm matcher. Runs on a tiny synthetic database (tests/conftest.py); no PostgreSQL, no network.

Run from the repo root:
    python -m pytest tests/test_duckdb_backend.py -v
"""

import json
from decimal import Decimal

import duckdb
import pytest

import database
from conftest import BERKELEY, OLD_YEAR, UCLA, UKIAH, UW_MADISON, UW_SEATTLE
from database import UnsafeQueryError, run_readonly_query, to_duckdb_params, to_duckdb_sql, validate_select
from guardrails import check_answer
from locations import resolve_location
from planner import _Session
from school_lookup import resolve_school, similarity, trigrams
from school_profile import compare_schools, school_basics, school_profile
from similar_schools import find_similar_schools


class TestValidatorDuckDBRules:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('x.csv')",
        "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
        "SELECT * FROM read_text('/etc/hosts')",
        "SELECT * FROM glob('/home/*')",
        "SELECT * FROM query('DROP TABLE schools')",
        "SELECT * FROM query_table('schools')",
        "SELECT * FROM duckdb_settings()",
        "WITH x AS (SELECT 1) SELECT * FROM x; ATTACH 'other.db'",
        "SELECT 1 FROM schools WHERE 1 = 1 ATTACH",
        "SELECT * FROM 'x.csv' INTO y",
    ])
    def test_rejects(self, sql):
        with pytest.raises(UnsafeQueryError):
            validate_select(sql)

    def test_allows_ordinary_names(self):
        assert validate_select("SELECT reading_score, queryable FROM schools")


class TestDialect:
    @pytest.mark.parametrize("sql,expected", [
        ("SELECT round(avg(x)::numeric, 4)", "SELECT round(avg(x)::DOUBLE, 4)"),
        ("SELECT CAST(x AS NUMERIC) FROM t", "SELECT CAST(x AS DOUBLE) FROM t"),
        ("SELECT x::numeric(10, 2)", "SELECT x::DECIMAL(10, 2)"),
        ("SELECT x:: decimal", "SELECT x:: DOUBLE"),
        ("SELECT '1::numeric' AS s, y::numeric", "SELECT '1::numeric' AS s, y::DOUBLE"),
        ("SELECT 'it''s ::numeric' AS s", "SELECT 'it''s ::numeric' AS s"),
        ("SELECT numeric_column FROM t", "SELECT numeric_column FROM t"),
    ])
    def test_numeric_casts(self, sql, expected):
        assert to_duckdb_sql(sql) == expected

    def test_params(self):
        assert to_duckdb_params("a = %s AND b = ANY(%s) AND c LIKE 'x%%'", (1, (2, 3))) == (
            "a = ? AND b = ANY(?) AND c LIKE 'x%'", [1, [2, 3]])
        assert to_duckdb_params("q = %(q)s OR r %% %(q)s", {"q": "a", "unused": 1}) == ("q = $q OR r % $q", {"q": "a"})
        assert to_duckdb_params("SELECT '100%%'", None) == ("SELECT '100%%'", None)  # no params: left alone


@pytest.mark.usefixtures("duck_db")
class TestReadonlyQuery:
    def test_backend_switch(self):
        assert database.backend() == "duckdb"

    def test_returns_dicts(self):
        rows = run_readonly_query("SELECT id, name FROM schools WHERE year = '2024-25' ORDER BY id LIMIT 2")
        assert rows == [{"id": BERKELEY, "name": "University of California-Berkeley"},
                        {"id": UCLA, "name": "University of California-Los Angeles"}]

    def test_row_cap(self):
        assert len(run_readonly_query("SELECT id FROM schools", max_rows=3)) == 3

    def test_postgres_like_integer_division_and_null_order(self):
        [row] = run_readonly_query("SELECT 7 / 2 AS q, 7.0 / 2 AS f")
        assert row == {"q": 3, "f": 3.5}
        ids = [r["id"] for r in run_readonly_query(
            "SELECT id FROM schools WHERE year = '2024-25' ORDER BY admission_rate DESC")]
        assert ids[0] == UKIAH  # NULL first on DESC, as in PostgreSQL

    def test_numeric_cast_keeps_precision(self):
        [row] = run_readonly_query("SELECT 0.2546::numeric AS x, round(avg(admission_rate)::numeric, 4) AS a "
                                   "FROM schools WHERE year = '2024-25'")
        assert row["x"] == pytest.approx(0.2546) and row["a"] == pytest.approx(0.28)

    def test_connection_is_read_only_and_cannot_read_files(self):
        conn = database.duckdb_connection()
        with pytest.raises(duckdb.Error):
            conn.execute("CREATE TABLE hacked (a INTEGER)")
        with pytest.raises(duckdb.Error):
            conn.execute("SELECT * FROM read_csv('/etc/hosts')")

    def test_timeout(self, monkeypatch):
        monkeypatch.setattr(database, "STATEMENT_TIMEOUT_MS", 200)
        with pytest.raises(database.QueryTimeoutError, match="statement timeout"):
            run_readonly_query("SELECT sum(a.range * b.range) AS s FROM range(200000) a, range(200000) b")
        assert run_readonly_query("SELECT 1 AS ok") == [{"ok": 1}]  # the connection still works

    def test_miles_between(self):
        [row] = run_readonly_query("SELECT miles_between(34.0689, -118.4452, 37.8719, -122.2585) AS miles, "
                                   "miles_between(NULL, 0, 0, 0) AS missing")
        assert row["miles"] == Decimal("338.3")  # same value and type as the PostgreSQL function
        assert row["missing"] is None

    def test_distance_query_like_the_planner_writes(self):
        rows = run_readonly_query(
            "SELECT id, name, miles_between(latitude, longitude, 37.8738, -122.2549) AS distance_miles "
            "FROM schools WHERE year = '2024-25' AND miles_between(latitude, longitude, 37.8738, -122.2549) <= 25 "
            "ORDER BY distance_miles")
        assert [r["id"] for r in rows] == [BERKELEY] and rows[0]["distance_miles"] < 1

    def test_schema_and_helpers(self):
        schema = database.get_schema()
        assert schema.startswith("Table: schools\n- id: integer")
        assert "- admission_rate: numeric" in schema
        assert "Table: copilot_scores (CollegeCopilot Score)" in schema
        assert database.table_exists("schools") and not database.table_exists("user_notes")
        assert database.get_latest_year() == "2024-25"
        assert [s["table_name"] for s in database.get_data_sources()] == ["copilot_scores", "ipeds_costs"]


class TestTrigrams:
    def test_padding_like_pg_trgm(self):
        assert trigrams("ab") == {"  a", " ab", "ab "}
        assert trigrams("a b") == trigrams("b a")

    @pytest.mark.parametrize("a,b,expected", [  # values from PostgreSQL's similarity()
        ("word", "two words", 0.36363637),
        ("stanfrod", "stanford university", 0.20833333),
        ("ucla", "university of california los angeles", 0.024390243),
        ("abc", "", 0.0),
    ])
    def test_similarity_matches_postgres(self, a, b, expected):
        assert similarity(trigrams(a), trigrams(b)) == pytest.approx(expected, rel=1e-6)


@pytest.mark.usefixtures("duck_db")
class TestLookupsOnDuckDB:
    def test_alias_exact_match(self):
        [best, *_] = resolve_school("UCLA")
        assert best["id"] == UCLA and best["score"] == 1.0 and best["match_type"] == "alias"

    def test_ambiguous_alias_has_tied_candidates(self):
        candidates = resolve_school("UW")
        assert {c["id"] for c in candidates[:2]} == {UW_SEATTLE, UW_MADISON}
        assert candidates[0]["score"] == candidates[1]["score"] == 1.0

    def test_misspelling(self):
        assert resolve_school("University of California Berkly")[0]["id"] == BERKELEY

    def test_nothing_found(self):
        assert resolve_school("zzqx") == []

    def test_locations(self):
        [berkeley] = resolve_location("94720")
        assert berkeley["label"] == "94720 (Berkeley, CA)"
        [los_angeles] = resolve_location("Los Angeles, CA")
        assert los_angeles["population"] == 80000
        assert los_angeles["latitude"] == pytest.approx((34.0633 * 5 + 34.0614 * 3) / 8, abs=1e-5)

    def test_similar_schools(self):
        rows = find_similar_schools(UCLA, limit=2)
        assert rows[0]["id"] == UCLA and rows[0]["role"] == "reference"
        assert rows[1]["id"] == BERKELEY

    def test_guardrails_drop_non_degree_programs(self):
        rows = [{"id": UKIAH, "name": "Ukiah Adult School"}, {"id": UCLA, "name": "UCLA"}]
        kept, notes = check_answer("Top colleges in California by graduation rate", rows, "")
        assert [r["id"] for r in kept] == [UCLA] and "Left out 1" in notes[0]

    def test_school_profile_without_reviews_or_photos(self):
        profile = school_profile(UCLA)
        assert profile["name"] == "University of California-Los Angeles"
        assert profile["score"]["rank"] == 2 and profile["score"]["out_of"] == 4
        assert profile["reviews"] == {"average": None, "count": 0, "recent": []} and profile["photo"] is None
        assert profile["majors"] == [{"level": "Bachelor's",
                                      "programs": ["Business Administration.", "Computer Science."]}]
        assert profile["safety"]["criminal_offenses_total"] == 460
        assert [s["id"] for s in school_basics([BERKELEY, UCLA])] == [BERKELEY, UCLA]
        assert compare_schools([UCLA, BERKELEY])["schools"][0]["id"] == UCLA

    def test_planner_session_runs_queries(self):
        session = _Session()
        reply = json.loads(session.call("run_query", {"sql": f"SELECT id, name, admission_rate FROM schools "
                                                             f"WHERE id = {UCLA} AND year = '{OLD_YEAR}'"}))
        assert reply["row_count"] == 1 and reply["preview"][0]["admission_rate"] == 0.18
        error = json.loads(session.call("run_query", {"sql": "SELECT nope FROM schools"}))
        assert "nope" in error["error"]
        assert json.loads(session.call("run_query", {"sql": "SELECT * FROM read_csv('/etc/hosts')"}))["error"] == \
            "Query uses a forbidden construct"
