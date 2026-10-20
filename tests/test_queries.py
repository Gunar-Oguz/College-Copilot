"""
Tests for the SQL safety layer and SQL generator helpers.

Run from the repo root:
    python -m pytest tests/test_queries.py -v

Database tests are skipped when Postgres is not reachable. Answer quality is
measured separately by evals/run_eval.py, which calls Claude.
"""

import json
import sys
from pathlib import Path

import psycopg2
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from database import (  # noqa: E402
    UnsafeQueryError,
    get_db_connection,
    get_latest_year,
    get_schema,
    run_readonly_query,
    validate_select,
)
from school_lookup import normalize, resolve_school  # noqa: E402
from planner import _Session, build_prompt, net_price_column  # noqa: E402
from similar_schools import find_similar_schools  # noqa: E402


def _db_available() -> bool:
    try:
        get_db_connection().close()
        return True
    except psycopg2.Error:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


class TestValidateSelect:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, name FROM schools LIMIT 5",
            "select id from schools;",
            "WITH ca AS (SELECT * FROM schools WHERE state = 'CA') SELECT id FROM ca",
            "-- top schools\nSELECT id FROM schools",
        ],
    )
    def test_allows_plain_selects(self, sql):
        assert validate_select(sql).lower().lstrip().startswith(("select", "with"))

    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "DELETE FROM schools",
            "DROP TABLE schools",
            "UPDATE schools SET name = 'x'",
            "SELECT 1; DROP TABLE schools",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_sleep(60)",
            "SELECT * INTO copy_of_schools FROM schools",
            "SELECT set_config('statement_timeout', '0', false)",
            "COPY schools TO '/tmp/out.csv'",
            "/* hi */ INSERT INTO schools (id) VALUES (1)",
        ],
    )
    def test_rejects_unsafe_sql(self, sql):
        with pytest.raises(UnsafeQueryError):
            validate_select(sql)


class TestPlannerHelpers:
    def test_prompt_pins_latest_year_and_requires_submit(self):
        prompt = build_prompt("Top 5 schools", "Table: schools", "2024-25")
        assert "schools.year = '2024-25'" in prompt
        assert "submit_answer" in prompt

    @pytest.mark.parametrize(
        "income,column",
        [
            (25_000, "cost_net_price_income_0_30k"),
            (30_000, "cost_net_price_income_0_30k"),
            (30_001, "cost_net_price_income_30_48k"),
            (48_000, "cost_net_price_income_30_48k"),
            (60_000, "cost_net_price_income_48_75k"),
            (75_000, "cost_net_price_income_48_75k"),
            (85_000, "cost_net_price_income_75_110k"),
            (110_000, "cost_net_price_income_75_110k"),
            (150_000, "cost_net_price_income_110k_plus"),
        ],
    )
    def test_net_price_column(self, income, column):
        assert net_price_column(income)["column"] == column

    def test_submit_cannot_answer(self):
        assert _Session().submit({"kind": "cannot_answer"}).kind == "cannot_answer"

    def test_submit_clarify(self):
        answer = _Session().submit({"kind": "clarify", "clarification": "Which UW?"})
        assert answer.kind == "clarify" and answer.clarification == "Which UW?"

    def test_submit_unknown_result_id_is_rejected(self):
        assert isinstance(_Session().submit({"kind": "result", "result_id": "r9"}), str)

    def test_normalize(self):
        assert normalize("  Texas A&M University-College Station ") == "texas a and m university college station"


@requires_db
class TestReadonlyQuery:
    def test_returns_rows(self):
        rows = run_readonly_query("SELECT id, name FROM schools LIMIT 3")
        assert len(rows) == 3
        assert {"id", "name"} <= rows[0].keys()

    def test_caps_row_count(self):
        rows = run_readonly_query("SELECT id FROM schools", max_rows=10)
        assert len(rows) == 10

    def test_postgres_blocks_writes_hidden_in_cte(self):
        sql = "WITH d AS (DELETE FROM schools WHERE id = -1 RETURNING id) SELECT * FROM d"
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            run_readonly_query(sql)

    def test_statement_timeout(self):
        sql = "SELECT count(*) FROM generate_series(1, 1000000000)"
        with pytest.raises(psycopg2.errors.QueryCanceled):
            run_readonly_query(sql)


@requires_db
class TestSchemaHelpers:
    def test_latest_year(self):
        assert get_latest_year() == "2024-25"

    def test_schema_includes_both_tables(self):
        schema = get_schema()
        assert "Table: schools" in schema
        assert "Table: programs" in schema
        assert "- admission_rate: numeric" in schema


@requires_db
class TestPlannerSession:
    def test_run_query_result_can_be_submitted(self):
        session = _Session()
        reply = json.loads(session.call("run_query", {"sql": "SELECT id, name FROM schools LIMIT 2"}))
        answer = session.submit({"kind": "result", "result_id": reply["result_id"]})
        assert answer.kind == "result" and len(answer.rows) == 2

    def test_run_query_errors_go_back_to_claude(self):
        reply = json.loads(_Session().call("run_query", {"sql": "SELECT nope FROM schools"}))
        assert "nope" in reply["error"]

    def test_unsafe_sql_errors_go_back_to_claude(self):
        reply = json.loads(_Session().call("run_query", {"sql": "DELETE FROM schools"}))
        assert reply["error"] == "Only SELECT queries are allowed"


@requires_db
class TestSimilarSchools:
    UCLA = 110662

    def test_reference_first_then_matches(self):
        rows = find_similar_schools(self.UCLA, limit=5)
        assert rows[0]["id"] == self.UCLA and rows[0]["role"] == "reference"
        assert [r["role"] for r in rows[1:]] == ["similar"] * 5
        scores = [r["similarity"] for r in rows[1:]]
        assert scores == sorted(scores, reverse=True)

    def test_easier_to_get_into(self):
        rows = find_similar_schools(self.UCLA, limit=10, admission="easier")
        target_rate = rows[0]["admission_rate"]
        assert all(r["admission_rate"] > target_rate for r in rows[1:])

    def test_cheaper_and_state_filter(self):
        rows = find_similar_schools(166683, limit=5, cheaper=True, states=["ca"])
        assert all(r["state"] == "CA" for r in rows[1:])
        assert all(r["cost_avg_net_price"] < rows[0]["cost_avg_net_price"] for r in rows[1:])

    def test_unknown_school(self):
        with pytest.raises(ValueError):
            find_similar_schools(-1)


def _alias_table_loaded() -> bool:
    if not _db_available():
        return False
    try:
        with get_db_connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('school_aliases') IS NOT NULL")
            return cursor.fetchone()[0]
    except psycopg2.Error:
        return False


@pytest.mark.skipif(not _alias_table_loaded(), reason="school_aliases not loaded")
class TestResolveSchool:
    @pytest.mark.parametrize(
        "name,school_id",
        [
            ("MIT", 166683),
            ("UCLA", 110662),
            ("Stanford", 243744),
            ("University of Michigan", 170976),
            ("Wash U", 179867),
            ("Univ of Texas Austin", 228778),
        ],
    )
    def test_best_match(self, name, school_id):
        assert resolve_school(name)[0]["id"] == school_id

    @pytest.mark.parametrize("name", ["UW", "Columbia College", "Miami"])
    def test_ambiguous_names_have_tied_candidates(self, name):
        candidates = resolve_school(name)
        assert candidates[0]["score"] == candidates[1]["score"] == 1.0

    def test_misspelling_still_finds_school(self):
        assert resolve_school("stanfrod")[0]["id"] == 243744

    def test_nonsense_returns_nothing(self):
        assert resolve_school("zzqx") == []
