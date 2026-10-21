"""
Shared helpers for loading extra public data sources (IPEDS, Opportunity Insights, ...).

Every source table is keyed by school_id (the IPEDS UNITID, same as schools.id) and
replaced on each load. Column descriptions are stored as Postgres comments so the
schema shown to the planner explains them. Each load is recorded in data_sources.
"""

import csv
import io
import zipfile
from pathlib import Path

import requests
from psycopg2.extras import execute_values

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

SOURCES_SQL = """
CREATE TABLE IF NOT EXISTS data_sources (
    table_name TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    data_year TEXT,
    row_count INTEGER,
    matched_schools INTEGER,
    loaded_at TIMESTAMPTZ DEFAULT now()
)
"""


def download(url: str, dest: Path) -> Path:
    """Download url to dest unless it is already there."""
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(url, timeout=120, headers={"User-Agent": "CollegeCopilot data loader"})
        response.raise_for_status()
        dest.write_bytes(response.content)
    return dest


def read_csv(path: Path, member: str | None = None, delimiter: str = ",") -> list[dict]:
    """Rows of a CSV (or a CSV inside a zip), with upper-cased, trimmed headers."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            # IPEDS zips may hold an original and a revised (_rv) file; prefer the revision.
            name = member or next((n for n in names if "_rv" in n.lower()), names[0])
            raw = archive.read(name)
    else:
        raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [{(k or "").strip().upper(): (v or "").strip() for k, v in row.items()} for row in reader]


def num(value):
    """A number, or None for blanks and IPEDS missing codes (negative values)."""
    if value in (None, "", ".", "NA", "PrivacySuppressed", "NULL"):
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None
    if number < 0:
        return None
    return int(number) if number.is_integer() else number


def pct(value):
    """IPEDS percentages (0-100) as decimals (0-1), like the Scorecard rates."""
    number = num(value)
    return None if number is None else round(number / 100, 4)


def flag(value):
    """IPEDS yes/no codes: 1 = yes, 0 or 2 = no, negative = not reported."""
    number = num(value)
    if number is None:
        return None
    return number == 1


def replace_table(conn, table: str, columns: list[tuple[str, str, str]], rows: list[tuple],
                  description: str, source: dict) -> None:
    """Recreate table from (name, sql type, description) columns and load rows."""
    column_sql = ",\n    ".join(f"{name} {sql_type}" for name, sql_type, _ in columns)
    with conn.cursor() as cursor:
        cursor.execute(SOURCES_SQL)
        cursor.execute(f"DROP TABLE IF EXISTS {table}")
        cursor.execute(f"CREATE TABLE {table} (\n    {column_sql}\n)")
        cursor.execute(f"COMMENT ON TABLE {table} IS %s", (description,))
        for name, _, comment in columns:
            if comment:
                cursor.execute(f"COMMENT ON COLUMN {table}.{name} IS %s", (comment,))
        names = ", ".join(name for name, _, _ in columns)
        execute_values(cursor, f"INSERT INTO {table} ({names}) VALUES %s", rows, page_size=1000)
        if columns[0][0] == "school_id":
            if "PRIMARY KEY" not in columns[0][1]:
                cursor.execute(f"CREATE INDEX ON {table} (school_id)")
            cursor.execute(
                f"SELECT count(DISTINCT t.school_id) FROM {table} t JOIN schools s ON s.id = t.school_id"
            )
            matched = cursor.fetchone()[0]
        else:
            matched = None
        cursor.execute(
            """
            INSERT INTO data_sources (table_name, source, url, data_year, row_count, matched_schools, loaded_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (table_name) DO UPDATE SET source = EXCLUDED.source, url = EXCLUDED.url,
                data_year = EXCLUDED.data_year, row_count = EXCLUDED.row_count,
                matched_schools = EXCLUDED.matched_schools, loaded_at = now()
            """,
            (table, source["name"], source["url"], source.get("year"), len(rows), matched),
        )
    conn.commit()
    print(f"  {table}: {len(rows):,} rows, {matched if matched is not None else '-'} match schools")
