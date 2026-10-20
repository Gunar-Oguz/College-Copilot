"""
Tests for the SQL safety layer and SQL generator helpers.

Run from the repo root:
    python -m pytest tests/test_queries.py -v

Database tests are skipped when Postgres is not reachable. Answer quality is
measured separately by evals/run_eval.py, which calls Claude.
"""

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
from sql_generator import CANNOT_ANSWER, _strip_markdown, build_prompt  # noqa: E402


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


class TestSqlGeneratorHelpers:
    def test_strips_markdown_fences(self):
        assert _strip_markdown("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_leaves_plain_sql_alone(self):
        assert _strip_markdown("  SELECT 1  ") == "SELECT 1"

    def test_prompt_pins_latest_year_and_cannot_answer(self):
        prompt = build_prompt("Top 5 schools", "Table: schools", "2024-25")
        assert "schools.year = '2024-25'" in prompt
        assert CANNOT_ANSWER in prompt


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
