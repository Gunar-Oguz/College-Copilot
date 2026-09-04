import os

import psycopg2
from psycopg2.extras import RealDictCursor

from query_parser import ParsedQuery, parse_question
from sql_builder import QueryPlan, build_meta_sql, build_query_plan


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "collegecopilot"),
        user=os.getenv("DB_USER", os.getenv("USER")),
        host=os.getenv("DB_HOST", "localhost"),
        password=os.getenv("DB_PASSWORD") or None,
        port=os.getenv("DB_PORT", "5432"),
    )


def fetch_meta_stats(states: list[str] | None = None) -> dict:
    parsed = ParsedQuery(kind="meta", question="", states=states or [])
    sql, params = build_meta_sql(parsed)

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            return dict(cursor.fetchone())
    finally:
        conn.close()


def _row_matches_parsed(row: dict, parsed: ParsedQuery) -> bool:
    checks = [
        ("admission_rate", parsed.acceptance_rate_min, parsed.acceptance_rate_max),
        ("completion_rate", parsed.graduation_rate_min, parsed.graduation_rate_max),
        ("sat_average", parsed.sat_min, parsed.sat_max),
        ("earnings_median_10yr", parsed.earnings_min, parsed.earnings_max),
    ]

    for column, minimum, maximum in checks:
        value = row.get(column)
        if minimum is not None or maximum is not None:
            if value is None:
                return False
            numeric = float(value)
            if minimum is not None and numeric < minimum:
                return False
            if maximum is not None and numeric > maximum:
                return False

    cost_column = parsed.cost_column or "cost_avg_net_price"
    if parsed.cost_min is not None or parsed.cost_max is not None:
        value = row.get(cost_column)
        if value is None:
            return False
        numeric = float(value)
        if parsed.cost_min is not None and numeric < parsed.cost_min:
            return False
        if parsed.cost_max is not None and numeric > parsed.cost_max:
            return False

    if parsed.states and row.get("state") not in parsed.states:
        return False

    return True


def enforce_parsed_filters(rows: list[dict], parsed: ParsedQuery) -> list[dict]:
    if parsed.kind != "list":
        return rows
    return [row for row in rows if _row_matches_parsed(row, parsed)]


def execute_plan(plan: QueryPlan) -> list[dict]:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(plan.sql, plan.params)
            rows = cursor.fetchall()
    finally:
        conn.close()

    if plan.kind == "meta":
        return [dict(row) for row in rows]

    return enforce_parsed_filters(rows, plan.parsed)


def count_schools() -> int:
    return fetch_meta_stats()["total_schools"]


def run_query_pipeline(question: str) -> tuple[list[dict], QueryPlan, ParsedQuery]:
    """Three-step pipeline: parse → build SQL → execute."""
    parsed = parse_question(question)
    plan = build_query_plan(question)
    rows = execute_plan(plan)
    return rows, plan, parsed


def fetch_schools_for_question(question: str) -> tuple[list[dict], QueryPlan]:
    rows, plan, _parsed = run_query_pipeline(question)
    return rows, plan
