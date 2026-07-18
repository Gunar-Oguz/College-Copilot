"""
Load College Scorecard Field of Study (program) data for 2015-2024.

Programs are nested under each school in the /v1/schools API response.
One school has many programs; rows link to schools via (school_id, year).

Usage (from backend/):
    python load_programs.py
    python load_programs.py --recreate
    python load_programs.py --start-year 2020 --end-year 2024
"""

import argparse
import os
import time
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
PER_PAGE = 100
REQUEST_DELAY_SEC = 2.0
MAX_RETRIES = 8
RETRY_WAIT_SEC = 30.0

FIRST_YEAR = 2015
LAST_YEAR = 2024

SCHEMA_PATH = ROOT_DIR / "data" / "programs_schema.sql"

INSERT_SQL = """
INSERT INTO programs (
    school_id, year, cip_code, program_name, credential_level, credential_name,
    earnings_median_1yr, earnings_median_2yr, debt_median, debt_monthly_payment,
    completion_rate
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s
)
"""


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


def academic_year_label(start_year: int) -> str:
    end_short = str(start_year + 1)[-2:]
    return f"{start_year}-{end_short}"


def format_cip_code(code: str | None) -> str | None:
    if not code:
        return None
    code = str(code).strip()
    if len(code) == 4 and code.isdigit():
        return f"{code[:2]}.{code[2:]}"
    return code


def _nested(data: dict | None, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def programs_response_key(api_year: int) -> str | None:
    """API key for nested program arrays. FOS data for 2023+ uses latest only for 2024."""
    if api_year == LAST_YEAR:
        return "latest.programs.cip_4_digit"
    if api_year >= 2023:
        return None
    return f"{api_year}.programs.cip_4_digit"


def program_row(school_id: int, year_label: str, program: dict) -> tuple | None:
    code = program.get("code")
    credential = program.get("credential") or {}
    credential_level = credential.get("level")
    if not code or credential_level is None:
        return None

    debt = _nested(program, "debt", "staff_grad_plus", "all", "all_inst") or {}

    return (
        school_id,
        year_label,
        format_cip_code(code),
        program.get("title"),
        credential_level,
        credential.get("title"),
        _nested(program, "earnings", "1_yr", "overall_median_earnings"),
        _nested(program, "earnings", "2_yr", "overall_median_earnings"),
        debt.get("median"),
        debt.get("median_payment"),
        None,  # completion_rate not published at program level in Scorecard API
    )


def recreate_table(conn):
    schema_sql = SCHEMA_PATH.read_text()
    with conn.cursor() as cursor:
        cursor.execute(schema_sql)
    conn.commit()
    print("Dropped and recreated programs table.")


def api_request(api_key: str, params: dict) -> dict:
    for attempt in range(MAX_RETRIES):
        time.sleep(REQUEST_DELAY_SEC)
        try:
            response = requests.get(API_URL, params=params, timeout=120)
        except requests.RequestException as exc:
            wait = RETRY_WAIT_SEC * (attempt + 1)
            print(
                f"\n  request failed ({exc.__class__.__name__}), "
                f"retry {attempt + 1}/{MAX_RETRIES} in {wait:.0f}s...",
                flush=True,
            )
            time.sleep(wait)
            continue
        if response.status_code == 429:
            print(f"\n  rate limited, waiting {RETRY_WAIT_SEC:.0f}s...", flush=True)
            time.sleep(RETRY_WAIT_SEC)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"API request failed after {MAX_RETRIES} retries")


def schools_for_year(conn, year_label: str) -> set[int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM schools WHERE year = %s", (year_label,))
        return {row[0] for row in cursor.fetchall()}


def fetch_year_programs(api_key: str, api_year: int) -> list[tuple[int, dict]]:
    response_key = programs_response_key(api_year)
    if not response_key:
        return []

    page = 0
    results: list[tuple[int, dict]] = []

    while True:
        data = api_request(
            api_key,
            {
                "api_key": api_key,
                "fields": f"id,{response_key}",
                "all_programs_nested": "true",
                "per_page": PER_PAGE,
                "page": page,
            },
        )
        schools = data.get("results", [])
        if not schools:
            break

        for school in schools:
            school_id = school.get("id")
            if school_id is None:
                continue
            for program in school.get(response_key, []) or []:
                results.append((school_id, program))

        total = data.get("metadata", {}).get("total", len(schools))
        print(
            f"  page {page + 1}: {len(results):,} programs from schools "
            f"(up to {min((page + 1) * PER_PAGE, total):,} / {total:,})",
            flush=True,
        )

        if len(schools) < PER_PAGE:
            break
        page += 1

    return results


def insert_year_programs(
    conn,
    year_label: str,
    school_ids: set[int],
    programs: list[tuple[int, dict]],
) -> int:
    rows: list[tuple] = []
    for school_id, program in programs:
        if school_id not in school_ids:
            continue
        row = program_row(school_id, year_label, program)
        if row:
            rows.append(row)

    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM programs WHERE year = %s", (year_label,))
        if rows:
            cursor.executemany(INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def print_row_counts(conn, year_label: str):
    with conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM programs WHERE year = %s", (year_label,))
        year_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM programs")
        total_count = cursor.fetchone()[0]
    print(f"  committed {year_label}: {year_count:,} programs (total in DB: {total_count:,})")


def load_programs(
    conn,
    api_key: str,
    start_year: int,
    end_year: int,
    recreate: bool = False,
) -> None:
    if recreate:
        recreate_table(conn)

    years = [
        (year, academic_year_label(year))
        for year in range(end_year, start_year - 1, -1)
    ]
    print(
        f"Loading programs for {len(years)} years "
        f"({academic_year_label(end_year)} → {academic_year_label(start_year)})...\n"
    )

    for api_year, year_label in years:
        school_ids = schools_for_year(conn, year_label)
        if not school_ids:
            print(f"Loading {year_label}... skipped (no schools in DB for this year)")
            continue

        print(f"Loading {year_label}...")
        response_key = programs_response_key(api_year)
        if response_key == "latest.programs.cip_4_digit":
            print("  using latest.programs (most recent FOS snapshot)")
        elif response_key is None:
            print("  skipped (FOS data not yet available for this year)")
            continue

        programs = fetch_year_programs(api_key, api_year)
        if not programs:
            print(f"  no program data returned for {year_label}")
            continue

        inserted = insert_year_programs(conn, year_label, school_ids, programs)
        print(f"  done — inserted {inserted:,} programs")
        print_row_counts(conn, year_label)
        print()


def parse_args():
    parser = argparse.ArgumentParser(description="Load College Scorecard Field of Study data")
    parser.add_argument(
        "--start-year",
        type=int,
        default=FIRST_YEAR,
        help=f"First API year (default: {FIRST_YEAR})",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=LAST_YEAR,
        help=f"Last API year (default: {LAST_YEAR})",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate the programs table before loading",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.start_year > args.end_year:
        raise SystemExit("--start-year must be <= --end-year")

    api_key = os.getenv("COLLEGE_SCORECARD_API_KEY")
    if not api_key:
        raise SystemExit("COLLEGE_SCORECARD_API_KEY not set in .env")

    conn = get_db_connection()
    try:
        load_programs(
            conn,
            api_key,
            args.start_year,
            args.end_year,
            recreate=args.recreate,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
