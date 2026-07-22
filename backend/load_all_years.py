"""
Load all US colleges from College Scorecard for every available year.

Academic years 1996-97 through 2024-25 map to API prefixes 1996 … 2024.

Usage (from backend/):
    python load_all_years.py
    python load_all_years.py --start-year 2020 --end-year 2024
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
PROGRESS_EVERY = 500
REQUEST_PAUSE_SEC = 1.0
YEAR_PAUSE_SEC = 5.0
MAX_RETRIES = 12
RETRY_BASE_SEC = 15.0

FIRST_ACADEMIC_YEAR = 1996
LAST_ACADEMIC_YEAR = 2024

SCHEMA_PATH = ROOT_DIR / "data" / "schema.sql"

INSERT_SQL = """
INSERT INTO schools (
    id, year, name, city, state, zip, school_url, ownership,
    admission_rate, sat_average, act_median, cost_avg_net_price,
    cost_net_price_income_0_30k, cost_net_price_income_30_48k,
    cost_net_price_income_48_75k, cost_net_price_income_75_110k,
    cost_net_price_income_110k_plus, completion_rate,
    earnings_median_10yr, student_size, undergraduate_size,
    pell_grant_rate, federal_loan_rate, avg_loan_amount,
    retention_rate, transfer_rate,
    first_gen_rate, part_time_rate, avg_age, female_share,
    online_only, highest_degree, carnegie_class,
    earnings_6yr, earnings_8yr, loan_repayment_rate
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s
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


def api_year_range(start_year: int, end_year: int) -> list[tuple[int, str]]:
    return [
        (year, academic_year_label(year))
        for year in range(start_year, end_year + 1)
    ]


def api_prefix(api_year: int) -> str:
    """Latest year uses 'latest.' prefix in the Scorecard API; historical years use the year."""
    if api_year >= LAST_ACADEMIC_YEAR:
        return "latest"
    return str(api_year)


def fields_for_api_year(api_year: int) -> str:
    prefix = api_prefix(api_year)
    return ",".join([
        "id",
        "school.name",
        "school.city",
        "school.state",
        "school.zip",
        "school.school_url",
        "school.ownership",
        f"{prefix}.admissions.admission_rate.overall",
        f"{prefix}.admissions.sat_scores.average.overall",
        f"{prefix}.admissions.act_scores.midpoint.cumulative",
        f"{prefix}.cost.avg_net_price.overall",
        f"{prefix}.cost.net_price.consumer.by_income_level.0-30000",
        f"{prefix}.cost.net_price.consumer.by_income_level.30001-48000",
        f"{prefix}.cost.net_price.consumer.by_income_level.48001-75000",
        f"{prefix}.cost.net_price.consumer.by_income_level.75001-110000",
        f"{prefix}.cost.net_price.public.by_income_level.75001-110000",
        f"{prefix}.cost.net_price.consumer.by_income_level.110001-plus",
        f"{prefix}.completion.rate_suppressed.overall",
        f"{prefix}.earnings.10_yrs_after_entry.median",
        f"{prefix}.student.size",
        f"{prefix}.aid.pell_grant_rate",
        f"{prefix}.aid.federal_loan_rate",
        f"{prefix}.aid.median_debt.completers.overall",
        f"{prefix}.student.retention_rate.overall.full_time",
        f"{prefix}.completion.transfer_rate.4yr.full_time",
        f"{prefix}.student.share_firstgeneration",
        f"{prefix}.student.part_time_share",
        f"{prefix}.student.demographics.age_entry",
        f"{prefix}.student.demographics.female_share",
        f"{prefix}.earnings.6_yrs_after_entry.median",
        f"{prefix}.earnings.8_yrs_after_entry.median_earnings",
        f"{prefix}.repayment.3_yr_repayment.overall",
        "school.online_only",
        "school.degrees_awarded.highest",
        "school.carnegie_basic",
    ])


def _get(school: dict, api_year: int, suffix: str):
    return school.get(f"{api_prefix(api_year)}.{suffix}")


def _income_75_110k(school: dict, api_year: int):
    return (
        _get(school, api_year, "cost.net_price.consumer.by_income_level.75001-110000")
        or _get(school, api_year, "cost.net_price.public.by_income_level.75001-110000")
    )


def school_row(school: dict, year_label: str, api_year: int) -> tuple:
    size = _get(school, api_year, "student.size")
    return (
        school.get("id"),
        year_label,
        school.get("school.name"),
        school.get("school.city"),
        school.get("school.state"),
        school.get("school.zip"),
        school.get("school.school_url"),
        school.get("school.ownership"),
        _get(school, api_year, "admissions.admission_rate.overall"),
        _get(school, api_year, "admissions.sat_scores.average.overall"),
        _get(school, api_year, "admissions.act_scores.midpoint.cumulative"),
        _get(school, api_year, "cost.avg_net_price.overall"),
        _get(school, api_year, "cost.net_price.consumer.by_income_level.0-30000"),
        _get(school, api_year, "cost.net_price.consumer.by_income_level.30001-48000"),
        _get(school, api_year, "cost.net_price.consumer.by_income_level.48001-75000"),
        _income_75_110k(school, api_year),
        _get(school, api_year, "cost.net_price.consumer.by_income_level.110001-plus"),
        _get(school, api_year, "completion.rate_suppressed.overall"),
        _get(school, api_year, "earnings.10_yrs_after_entry.median"),
        size,
        size,
        _get(school, api_year, "aid.pell_grant_rate"),
        _get(school, api_year, "aid.federal_loan_rate"),
        _get(school, api_year, "aid.median_debt.completers.overall"),
        _get(school, api_year, "student.retention_rate.overall.full_time"),
        _get(school, api_year, "completion.transfer_rate.4yr.full_time"),
        _get(school, api_year, "student.share_firstgeneration"),
        _get(school, api_year, "student.part_time_share"),
        _get(school, api_year, "student.demographics.age_entry"),
        _get(school, api_year, "student.demographics.female_share"),
        school.get("school.online_only"),
        school.get("school.degrees_awarded.highest"),
        school.get("school.carnegie_basic"),
        _get(school, api_year, "earnings.6_yrs_after_entry.median"),
        _get(school, api_year, "earnings.8_yrs_after_entry.median_earnings"),
        _get(school, api_year, "repayment.3_yr_repayment.overall"),
    )


def recreate_table(conn):
    schema_sql = SCHEMA_PATH.read_text()
    with conn.cursor() as cursor:
        cursor.execute(schema_sql)
    conn.commit()
    print("Dropped and recreated schools table (multi-year schema).")


def fetch_year_schools(api_key: str, api_year: int) -> list[dict]:
    page = 0
    all_schools: list[dict] = []
    fields = fields_for_api_year(api_year)

    while True:
        for attempt in range(MAX_RETRIES):
            response = requests.get(
                API_URL,
                params={
                    "api_key": api_key,
                    "fields": fields,
                    "per_page": PER_PAGE,
                    "page": page,
                },
                timeout=60,
            )
            if response.status_code == 429:
                wait = RETRY_BASE_SEC * (2 ** attempt)
                print(f"\n  rate limited, waiting {wait:.0f}s...", end=" ", flush=True)
                time.sleep(wait)
                continue
            response.raise_for_status()
            break
        else:
            raise RuntimeError(f"Rate limit exceeded for {api_year} page {page} after {MAX_RETRIES} retries")

        data = response.json()
        results = data.get("results", [])
        if not results:
            break

        all_schools.extend(results)
        total = data.get("metadata", {}).get("total", len(all_schools))

        if len(all_schools) % PROGRESS_EVERY < PER_PAGE:
            print(f"  fetched {len(all_schools):,} / {total:,}...")

        if len(results) < PER_PAGE:
            break

        page += 1
        time.sleep(REQUEST_PAUSE_SEC)

    return all_schools


def insert_year_schools(conn, schools: list[dict], year_label: str, api_year: int) -> int:
    with conn.cursor() as cursor:
        for school in schools:
            cursor.execute(INSERT_SQL, school_row(school, year_label, api_year))
    conn.commit()
    return len(schools)


def print_year_summary(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT year, COUNT(*) FROM schools GROUP BY year ORDER BY year"
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM schools")
        total = cursor.fetchone()[0]

    print("\nSchools per year:")
    for year, count in rows:
        print(f"  {year}: {count:,}")
    print(f"\nTotal rows (all years): {total:,}")


def years_already_loaded(conn) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT DISTINCT year FROM schools")
        return {row[0] for row in cursor.fetchall()}


def load_all_years(
    conn,
    api_key: str,
    start_year: int,
    end_year: int,
    recreate: bool = True,
    skip_existing: bool = False,
) -> None:
    if recreate:
        recreate_table(conn)

    existing = years_already_loaded(conn) if skip_existing else set()
    years = api_year_range(start_year, end_year)
    print(
        f"Loading {len(years)} academic years "
        f"({academic_year_label(start_year)} to {academic_year_label(end_year)})...\n"
    )

    for index, (api_year, year_label) in enumerate(years):
        if year_label in existing:
            print(f"Loading {year_label}... skipped (already loaded)")
            continue

        print(f"Loading {year_label}...", end=" ", flush=True)
        schools = fetch_year_schools(api_key, api_year)
        inserted = insert_year_schools(conn, schools, year_label, api_year)
        print(f"done ({inserted:,} schools)")

        if index < len(years) - 1:
            time.sleep(YEAR_PAUSE_SEC)


def parse_args():
    parser = argparse.ArgumentParser(description="Load College Scorecard data for all years")
    parser.add_argument(
        "--start-year",
        type=int,
        default=FIRST_ACADEMIC_YEAR,
        help=f"First API year prefix (default: {FIRST_ACADEMIC_YEAR} = {academic_year_label(FIRST_ACADEMIC_YEAR)})",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=LAST_ACADEMIC_YEAR,
        help=f"Last API year prefix (default: {LAST_ACADEMIC_YEAR} = {academic_year_label(LAST_ACADEMIC_YEAR)})",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate the schools table before loading",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not drop/recreate the schools table before loading",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip academic years already present in the database",
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
        load_all_years(
            conn,
            api_key,
            args.start_year,
            args.end_year,
            recreate=args.recreate and not args.no_recreate,
            skip_existing=args.skip_existing,
        )
        print_year_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
