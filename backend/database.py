"""
Database access for the app, on one of two backends:

- PostgreSQL (the full FastAPI app): DATABASE_URL or DB_NAME / DB_USER / DB_HOST / ...
- DuckDB (the Streamlit demo): a read-only file exported by scripts/export_duckdb.py, chosen when
  DB_BACKEND=duckdb or DUCKDB_PATH is set.

Application code writes PostgreSQL-style SQL with psycopg2 placeholders (%s, %(name)s); on DuckDB
the placeholders are translated and the session is configured to behave like PostgreSQL where it
matters for answers (integer division, NULL ordering). Untrusted SQL from the planner always goes
through validate_select() and run_readonly_query(), with a read-only connection, a timeout and a
row cap on both backends.
"""

import os
import re
import threading

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # the DuckDB demo does not need PostgreSQL
    psycopg2 = None

try:
    import duckdb
except ImportError:  # the FastAPI app does not need DuckDB
    duckdb = None

MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 5000
TIMEOUT_MESSAGE = "canceling statement due to statement timeout"  # PostgreSQL's wording, on both backends

# Defense in depth on top of the read-only transaction: block multiple statements
# and server functions that read files or reach outside the database.
_FORBIDDEN_PATTERNS = [
    r"\bpg_\w+\s*\(",
    r"\blo_\w+\s*\(",
    r"\bdblink\w*\s*\(",
    r"\bcopy\b",
    r"\binto\b",
    r"set_config",
    r"\bu&['\"]",  # unicode-escaped names could spell a blocked word
    r"\buser_\w+",  # per-user data (saved schools, notes, reviews, plans, profiles, checklists)
    r"\b(query_logs|feedback|usage_tracking|stripe_events)\b",  # logs, usage and billing records
    # DuckDB: file and network readers, SQL-in-a-string, catalog and extension commands.
    r"\bread_\w+\s*\(",
    r"\b(glob|query|query_table|sniff_csv|parquet_\w+|iceberg_\w+|delta_scan)\s*\(",
    r"\bduckdb_\w+\s*\(",
    r"\b(attach|detach|install|pragma|getvariable|setvariable)\b",
]

# The same formula and rounding as the PostgreSQL function created by load_zipcodes.py:
# haversine distance in miles, returned as a decimal rounded half away from zero to 0.1 mile.
MILES_BETWEEN_DUCKDB = """
CREATE OR REPLACE MACRO miles_between(lat1, lon1, lat2, lon2) AS
    round(CAST(3958.8 * 2 * asin(sqrt(
        pow(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * pow(sin(radians(lon2 - lon1) / 2), 2)
    )) AS DECIMAL(18, 6)), 1)
"""

# Per-cursor settings that make DuckDB answer like PostgreSQL.
DUCKDB_SESSION_SQL = (
    "SET integer_division = true",  # 7 / 2 = 3, as in PostgreSQL
    "SET default_null_order = 'nulls_last_on_asc_first_on_desc'",  # PostgreSQL's default NULL order
)


class UnsafeQueryError(ValueError):
    pass


class QueryTimeoutError(Exception):
    """An untrusted query ran longer than STATEMENT_TIMEOUT_MS (DuckDB backend)."""


# Errors from either database, for callers that turn them into messages.
DB_ERRORS: tuple[type[Exception], ...] = tuple(
    e for e in (getattr(psycopg2, "Error", None), getattr(duckdb, "Error", None), QueryTimeoutError) if e)


def backend() -> str:
    """'duckdb' or 'postgres'. Read on every call, so the demo can set DUCKDB_PATH at startup."""
    explicit = os.getenv("DB_BACKEND", "").strip().lower()
    if explicit in ("duckdb", "postgres"):
        return explicit
    return "duckdb" if os.getenv("DUCKDB_PATH") else "postgres"


# ---------- PostgreSQL ----------

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


# ---------- DuckDB ----------

_duck_state: dict = {"conn": None, "path": None}
_duck_lock = threading.Lock()


def open_duckdb(path: str):
    """A read-only DuckDB connection with file and network access turned off."""
    if duckdb is None:
        raise RuntimeError("duckdb is not installed")
    return duckdb.connect(str(path), read_only=True, config={
        "enable_external_access": False,  # no read_csv('/etc/passwd'), no httpfs, no extensions
        "threads": int(os.getenv("DUCKDB_THREADS", "2")),
        "memory_limit": os.getenv("DUCKDB_MEMORY_LIMIT", "1GB"),
    })


def use_duckdb(conn, path: str | None = None) -> None:
    """Make this connection the app's database (the Streamlit app passes its cached connection)."""
    with _duck_lock:
        _duck_state.update(conn=conn, path=path)


def duckdb_connection():
    with _duck_lock:
        path = os.getenv("DUCKDB_PATH")
        if _duck_state["conn"] is None or (path and path != _duck_state["path"]):
            if not path:
                raise RuntimeError("Set DUCKDB_PATH to the DuckDB file (see scripts/export_duckdb.py)")
            _duck_state.update(conn=open_duckdb(path), path=path)
        return _duck_state["conn"]


def database_token() -> str:
    """Identifies the current database, for caches of data loaded from it."""
    return f"duckdb:{_duck_state['path'] or id(_duck_state['conn'])}" if backend() == "duckdb" else "postgres"


def _duck_cursor():
    cursor = duckdb_connection().cursor()
    for statement in DUCKDB_SESSION_SQL:
        cursor.execute(statement)
    return cursor


_PARAM_RE = re.compile(r"%\((\w+)\)s|%s|%%")


def to_duckdb_params(sql: str, params) -> tuple[str, list | dict | None]:
    """psycopg2 placeholders -> DuckDB ($name, ?); '%%' -> '%'. Only when params are given, like psycopg2."""
    if params is None:
        return sql, None
    names: list[str] = []

    def replace(match: re.Match) -> str:
        if match.group(0) == "%%":
            return "%"
        if match.group(1):
            names.append(match.group(1))
            return f"${match.group(1)}"
        return "?"

    converted = _PARAM_RE.sub(replace, sql)
    if isinstance(params, dict):
        return converted, {k: (list(v) if isinstance(v, tuple) else v) for k, v in params.items() if k in names}
    return converted, [list(v) if isinstance(v, tuple) else v for v in params]


_STRING_RE = re.compile(r"('(?:[^']|'')*')")
_NUMERIC_CAST_RE = re.compile(r"(::\s*|\bas\s+)(numeric|decimal)\b(\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?", re.IGNORECASE)


def to_duckdb_sql(sql: str) -> str:
    """Planner SQL written for PostgreSQL, made to mean the same on DuckDB.

    PostgreSQL's unconstrained numeric is exact with any precision, but DuckDB's NUMERIC is
    DECIMAL(18,3), which would round 0.2546 to 0.255; such casts become DOUBLE. String literals
    are left alone.
    """
    def fix(part: str) -> str:
        def cast(match: re.Match) -> str:
            if match.group(3):  # numeric(p, s) has a fixed precision on both backends
                return f"{match.group(1)}DECIMAL{match.group(3)}"
            return f"{match.group(1)}DOUBLE"
        return _NUMERIC_CAST_RE.sub(cast, part)

    return "".join(part if i % 2 else fix(part) for i, part in enumerate(_STRING_RE.split(sql)))


def _rows(cursor, limit: int | None = None) -> list[dict]:
    if cursor.description is None:
        return []
    names = [d[0] for d in cursor.description]
    rows = cursor.fetchmany(limit) if limit is not None else cursor.fetchall()
    return [dict(zip(names, row)) for row in rows]


# ---------- shared ----------

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
    if backend() == "duckdb":
        return _run_readonly_duckdb(to_duckdb_sql(cleaned), max_rows)
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


def _run_readonly_duckdb(sql: str, max_rows: int) -> list[dict]:
    """The file is opened read-only with external access off; a watchdog interrupts slow queries."""
    cursor = _duck_cursor()
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        cursor.interrupt()

    watchdog = threading.Timer(STATEMENT_TIMEOUT_MS / 1000, stop)
    watchdog.start()
    try:
        cursor.execute(sql)
        return _rows(cursor, max_rows)
    except duckdb.Error as exc:
        if timed_out.is_set():
            raise QueryTimeoutError(TIMEOUT_MESSAGE) from exc
        raise
    finally:
        watchdog.cancel()
        cursor.close()


def execute_sql(sql: str, params: tuple | list | dict | None = None) -> list[dict]:
    """Run trusted, application-written SQL."""
    if backend() == "duckdb":
        cursor = _duck_cursor()
        try:
            cursor.execute(*[p for p in to_duckdb_params(sql, params) if p is not None])
            return _rows(cursor)
        finally:
            cursor.close()
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def table_exists(name: str) -> bool:
    if backend() == "duckdb":
        return bool(execute_sql("SELECT 1 AS ok FROM information_schema.tables WHERE table_name = %s", (name,)))
    return execute_sql("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))[0]["ok"]


def get_latest_year() -> str:
    rows = execute_sql("SELECT MAX(year) AS year FROM schools")
    return rows[0]["year"] if rows and rows[0]["year"] else "2024-25"


def get_data_sources() -> list[dict]:
    """Extra source tables (IPEDS, Opportunity Insights, ...) recorded by the loaders."""
    if not table_exists("data_sources"):
        return []
    return execute_sql("SELECT table_name, source, data_year FROM data_sources ORDER BY table_name")


SCHEMA_SQL = {
    "postgres": """
        SELECT c.table_name, c.column_name, c.data_type,
               col_description(format('%%I', c.table_name)::regclass, c.ordinal_position) AS note,
               obj_description(format('%%I', c.table_name)::regclass, 'pg_class') AS table_note
        FROM information_schema.columns c
        WHERE c.table_schema = 'public' AND c.table_name = ANY(%s)
        ORDER BY c.table_name, c.ordinal_position
        """,
    # The export stored each column's PostgreSQL type and description, so the prompt is the same.
    "duckdb": """
        SELECT table_name, column_name, data_type, note, table_note
        FROM app_schema WHERE table_name = ANY(%s)
        ORDER BY table_name, ordinal_position
        """,
}


def get_schema(tables: tuple[str, ...] | None = None) -> str:
    """Columns of the core tables plus every loaded source table, with their descriptions."""
    if tables is None:
        tables = ("schools", "programs", *(s["table_name"] for s in get_data_sources()))
    rows = execute_sql(SCHEMA_SQL[backend()], (list(tables),))
    lines = []
    for table in tables:
        columns = [r for r in rows if r["table_name"] == table]
        table_note = columns[0]["table_note"] if columns else None
        lines.append(f"Table: {table}" + (f" ({table_note})" if table_note else ""))
        lines.extend(
            f"- {r['column_name']}: {r['data_type']}" + (f" -- {r['note']}" if r["note"] else "")
            for r in columns
        )
        lines.append("")
    return "\n".join(lines).strip()
