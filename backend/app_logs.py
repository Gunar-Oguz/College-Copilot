"""
Query logging, answer feedback and the numbers behind the /admin page.

Every /ask is written to query_logs (question, the SQL or tool call behind the answer, the answer
text, response time). Thumbs up/down go to feedback, one vote per logged answer (a new click
replaces the old vote). The planner's SQL validator blocks both tables: they hold other people's
questions.
"""

from psycopg2.extras import RealDictCursor

from database import execute_sql, get_db_connection

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS query_logs (
    id SERIAL PRIMARY KEY,
    question TEXT,
    sql_generated TEXT,
    answer TEXT,
    response_time_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    kind TEXT
);
CREATE INDEX IF NOT EXISTS query_logs_created_idx ON query_logs (created_at);

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    question TEXT,
    answer TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    created_at TIMESTAMP DEFAULT NOW(),
    query_log_id INTEGER UNIQUE REFERENCES query_logs (id) ON DELETE SET NULL
);
"""

MAX_TEXT = 20_000


def ensure_log_tables() -> None:
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(SCHEMA_SQL)


def _write(sql: str, params: tuple) -> dict | None:
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone() if cursor.description else None
            return dict(row) if row else None
    finally:
        conn.close()


def log_query(question: str, sql_generated: str | None, answer: str, response_time_ms: int, kind: str) -> int:
    row = _write(
        "INSERT INTO query_logs (question, sql_generated, answer, response_time_ms, kind) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (question[:MAX_TEXT], (sql_generated or "")[:MAX_TEXT] or None, answer[:MAX_TEXT], response_time_ms, kind),
    )
    return row["id"]


def save_feedback(query_log_id: int, rating: str) -> bool:
    """Record a vote on a logged answer, copying its question and answer. False if no such log."""
    row = _write(
        """
        INSERT INTO feedback (question, answer, rating, query_log_id)
        SELECT question, answer, %s, id FROM query_logs WHERE id = %s
        ON CONFLICT (query_log_id) DO UPDATE SET rating = EXCLUDED.rating, created_at = NOW()
        RETURNING id
        """,
        (rating, query_log_id),
    )
    return row is not None


def admin_stats() -> dict:
    totals = execute_sql(
        """
        SELECT count(*) AS all_time,
               count(*) FILTER (WHERE created_at >= date_trunc('day', NOW())) AS today,
               round(avg(response_time_ms)) AS avg_ms,
               round(avg(response_time_ms) FILTER (WHERE created_at >= date_trunc('day', NOW()))) AS avg_ms_today,
               count(*) FILTER (WHERE kind = 'error') AS errors,
               count(*) FILTER (WHERE kind IN ('cannot_answer', 'no_results')) AS unanswered
        FROM query_logs
        """
    )[0]
    votes = execute_sql(
        "SELECT count(*) FILTER (WHERE rating = 'up') AS up, count(*) FILTER (WHERE rating = 'down') AS down "
        "FROM feedback"
    )[0]
    thumbs_down = execute_sql(
        """
        SELECT f.created_at, f.question, f.answer, q.sql_generated, q.kind
        FROM feedback f LEFT JOIN query_logs q ON q.id = f.query_log_id
        WHERE f.rating = 'down'
        ORDER BY f.created_at DESC LIMIT 25
        """
    )
    common = execute_sql(
        """
        SELECT min(question) AS question, count(*) AS times, round(avg(response_time_ms)) AS avg_ms
        FROM query_logs
        GROUP BY lower(regexp_replace(trim(question), '[[:space:]?.!]+', ' ', 'g'))
        ORDER BY times DESC, max(created_at) DESC LIMIT 15
        """
    )
    recent = execute_sql(
        "SELECT created_at, question, kind, response_time_ms FROM query_logs ORDER BY created_at DESC LIMIT 20"
    )
    return {"totals": totals, "votes": votes, "thumbs_down": thumbs_down, "common": common, "recent": recent}
