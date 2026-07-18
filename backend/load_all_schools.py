"""
Load all US colleges from College Scorecard into PostgreSQL.

Usage (from backend/):
    python load_all_schools.py
"""

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
REQUEST_PAUSE_SEC = 0.25

FIELDS = ",".join([
    "id",
    "school.name",
    "school.city",
    "school.state",
    "school.zip",
    "school.school_url",
    "school.ownership",
    "latest.admissions.admission_rate.overall",
    "latest.admissions.sat_scores.average.overall",
    "latest.admissions.act_scores.midpoint.cumulative",
    "latest.cost.avg_net_price.overall",
    "latest.cost.net_price.consumer.by_income_level.0-30000",
    "latest.cost.net_price.consumer.by_income_level.30001-48000",
    "latest.cost.net_price.consumer.by_income_level.48001-75000",
    "latest.cost.net_price.consumer.by_income_level.75001-110000",
    "latest.cost.net_price.public.by_income_level.75001-110000",
    "latest.cost.net_price.consumer.by_income_level.110001-plus",
    "latest.completion.rate_suppressed.overall",
    "latest.earnings.10_yrs_after_entry.median",
    "latest.student.size",
])

SCHEMA_PATH = ROOT_DIR / "data" / "schema.sql"

INSERT_SQL = """
INSERT INTO schools (
    id, name, city, state, zip, school_url, ownership,
    admission_rate, sat_average, act_median, cost_avg_net_price,
    cost_net_price_income_0_30k, cost_net_price_income_30_48k,
    cost_net_price_income_48_75k, cost_net_price_income_75_110k,
    cost_net_price_income_110k_plus, completion_rate,
    earnings_median_10yr, student_size, undergraduate_size
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
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


def recreate_table(conn):
    schema_sql = SCHEMA_PATH.read_text()
    with conn.cursor() as cursor:
        cursor.execute(schema_sql)
    conn.commit()
    print("Dropped and recreated schools table.")


def _income_75_110k(school: dict):
    """Scorecard publishes the $75k–$110k bracket under public, not consumer."""
    return (
        school.get("latest.cost.net_price.consumer.by_income_level.75001-110000")
        or school.get("latest.cost.net_price.public.by_income_level.75001-110000")
    )


def school_row(school: dict) -> tuple:
    size = school.get("latest.student.size")
    return (
        school.get("id"),
        school.get("school.name"),
        school.get("school.city"),
        school.get("school.state"),
        school.get("school.zip"),
        school.get("school.school_url"),
        school.get("school.ownership"),
        school.get("latest.admissions.admission_rate.overall"),
        school.get("latest.admissions.sat_scores.average.overall"),
        school.get("latest.admissions.act_scores.midpoint.cumulative"),
        school.get("latest.cost.avg_net_price.overall"),
        school.get("latest.cost.net_price.consumer.by_income_level.0-30000"),
        school.get("latest.cost.net_price.consumer.by_income_level.30001-48000"),
        school.get("latest.cost.net_price.consumer.by_income_level.48001-75000"),
        _income_75_110k(school),
        school.get("latest.cost.net_price.consumer.by_income_level.110001-plus"),
        school.get("latest.completion.rate_suppressed.overall"),
        school.get("latest.earnings.10_yrs_after_entry.median"),
        size,
        size,
    )


def fetch_all_schools(api_key: str) -> list[dict]:
    page = 0
    all_schools: list[dict] = []

    while True:
        response = requests.get(
            API_URL,
            params={
                "api_key": api_key,
                "fields": FIELDS,
                "per_page": PER_PAGE,
                "page": page,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        if not results:
            break

        all_schools.extend(results)
        total = data.get("metadata", {}).get("total", len(all_schools))

        if len(all_schools) % PROGRESS_EVERY < PER_PAGE:
            print(f"Fetched {len(all_schools)} / {total} schools...")

        if len(results) < PER_PAGE:
            break

        page += 1
        time.sleep(REQUEST_PAUSE_SEC)

    return all_schools


def insert_schools(conn, schools: list[dict]) -> int:
    count = 0
    with conn.cursor() as cursor:
        for school in schools:
            cursor.execute(INSERT_SQL, school_row(school))
            count += 1
            if count % PROGRESS_EVERY == 0:
                conn.commit()
                print(f"Inserted {count} schools...")
        conn.commit()
    return count


def main():
    api_key = os.getenv("COLLEGE_SCORECARD_API_KEY")
    if not api_key:
        raise SystemExit("COLLEGE_SCORECARD_API_KEY not set in .env")

    conn = get_db_connection()
    try:
        recreate_table(conn)
        print("Fetching all schools from College Scorecard API...")
        schools = fetch_all_schools(api_key)
        print(f"Fetched {len(schools)} schools total.")

        print("Inserting into database...")
        inserted = insert_schools(conn, schools)

        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), COUNT(DISTINCT state) FROM schools")
            total, states = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) FROM schools WHERE cost_net_price_income_75_110k IS NOT NULL"
            )
            income_75_110k = cursor.fetchone()[0]

        print(f"Done. Inserted {inserted} schools.")
        print(f"Final count: {total} schools across {states} states/territories.")
        print(f"Schools with $75k–$110k income cost data: {income_75_110k}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
