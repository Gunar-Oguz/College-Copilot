import os
import re

import psycopg2
from psycopg2.extras import RealDictCursor

MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 5000

# Defense in depth on top of the read-only transaction: block multiple statements
# and server functions that read files or reach outside the database.
_FORBIDDEN_PATTERNS = [
    r"\bpg_\w+\s*\(",
    r"\blo_\w+\s*\(",
    r"\bdblink\w*\s*\(",
    r"\bcopy\b",
    r"\binto\b",
    r"\bset_config\s*\(",
]


class UnsafeQueryError(ValueError):
    pass


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


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)


def validate_select(sql: str) -> str:
    """Return a cleaned single SELECT statement, or raise UnsafeQueryError."""
    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQueryError("Empty query")
    if ";" in cleaned:
        raise UnsafeQueryError("Only one statement is allowed")
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise UnsafeQueryError("Only SELECT queries are allowed")
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            raise UnsafeQueryError("Query uses a forbidden construct")
    return cleaned


def run_readonly_query(sql: str, max_rows: int = MAX_ROWS) -> list[dict]:
    """Run an untrusted SELECT in a read-only transaction with a timeout and row cap."""
    cleaned = validate_select(sql)
    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
            cursor.execute(cleaned)
            rows = cursor.fetchmany(max_rows)
            return [dict(row) for row in rows]
    finally:
        conn.rollback()
        conn.close()


def execute_sql(sql: str, params: tuple | list | None = None) -> list[dict]:
    """Run trusted, application-written SQL."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_latest_year() -> str:
    rows = execute_sql("SELECT MAX(year) AS year FROM schools")
    return rows[0]["year"] if rows and rows[0]["year"] else "2024-25"


def get_schema(tables: tuple[str, ...] = ("schools", "programs")) -> str:
    rows = execute_sql(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_name = ANY(%s)
        ORDER BY table_name, ordinal_position
        """,
        (list(tables),),
    )
    lines = []
    for table in tables:
        lines.append(f"Table: {table}")
        lines.extend(
            f"- {r['column_name']}: {r['data_type']}" for r in rows if r["table_name"] == table
        )
        lines.append("")
    return "\n".join(lines).strip()
