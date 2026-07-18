"""
Load College Scorecard data for recent years only (2015-2024).

Loads newest year first (2024-25), then works backwards to 2015-16.
Uses a fixed 2s pause before every API request to avoid rate limits.

Usage (from backend/):
    python load_recent_years.py
    python load_recent_years.py --recreate
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


def fields_for_api_year(api_year: int) -> str:
    prefix = str(api_year)
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
    return school.get(f"{api_year}.{suffix}")


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
    print("Dropped and recreated schools table.")


def api_request(api_key: str, fields: str, page: int) -> dict:
    for attempt in range(MAX_RETRIES):
        time.sleep(REQUEST_DELAY_SEC)
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
            print(f"\n  rate limited, waiting {RETRY_WAIT_SEC:.0f}s...", flush=True)
            time.sleep(RETRY_WAIT_SEC)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Rate limit exceeded on page {page} after {MAX_RETRIES} retries")


def fetch_year_schools(api_key: str, api_year: int) -> list[dict]:
    page = 0
    all_schools: list[dict] = []
    fields = fields_for_api_year(api_year)

    while True:
        data = api_request(api_key, fields, page)
        results = data.get("results", [])
        if not results:
            break

        all_schools.extend(results)
        total = data.get("metadata", {}).get("total", len(all_schools))
        print(f"  page {page + 1}: {len(all_schools):,} / {total:,} schools", flush=True)

        if len(results) < PER_PAGE:
            break
        page += 1

    return all_schools


def insert_year_schools(conn, schools: list[dict], year_label: str, api_year: int) -> int:
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM schools WHERE year = %s", (year_label,))
        for school in schools:
            cursor.execute(INSERT_SQL, school_row(school, year_label, api_year))
    conn.commit()
    return len(schools)


def print_row_counts(conn, year_label: str):
    with conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM schools WHERE year = %s", (year_label,))
        year_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM schools")
        total_count = cursor.fetchone()[0]
    print(f"  committed {year_label}: {year_count:,} rows (total in DB: {total_count:,})")


def load_recent_years(conn, api_key: str, recreate: bool = False) -> None:
    if recreate:
        recreate_table(conn)

    years = [
        (year, academic_year_label(year))
        for year in range(LAST_YEAR, FIRST_YEAR - 1, -1)
    ]
    print(f"Loading {len(years)} years ({academic_year_label(LAST_YEAR)} → {academic_year_label(FIRST_YEAR)})...\n")

    for api_year, year_label in years:
        print(f"Loading {year_label}...")
        schools = fetch_year_schools(api_key, api_year)
        inserted = insert_year_schools(conn, schools, year_label, api_year)
        print(f"  done — inserted {inserted:,} schools")
        print_row_counts(conn, year_label)
        print()


def parse_args():
    parser = argparse.ArgumentParser(description="Load College Scorecard data for 2015-2024")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate the schools table before loading",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    api_key = os.getenv("COLLEGE_SCORECARD_API_KEY")
    if not api_key:
        raise SystemExit("COLLEGE_SCORECARD_API_KEY not set in .env")

    conn = get_db_connection()
    try:
        load_recent_years(conn, api_key, recreate=args.recreate)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
