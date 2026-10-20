"""
Load ZIP code coordinates from data/zipcodes.csv, give every school a latitude and
longitude, and create the miles_between() SQL function.

School coordinates come from College Scorecard (location.lat/lon) when available,
otherwise from the centroid of the school's ZIP code. Many large universities have
their own ZIP codes that are not in the ZIP file, so the API matters.

Safe to re-run. Run again after reloading schools (load_all_years.py --recreate).

Usage (from backend/):
    python load_zipcodes.py
"""

import csv
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from psycopg2.extras import execute_values

from database import get_db_connection

ROOT_DIR = Path(__file__).resolve().parent.parent
ZIP_FILE = ROOT_DIR / "data" / "zipcodes.csv"
load_dotenv(ROOT_DIR / ".env")

API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
PER_PAGE = 100
REQUEST_PAUSE_SEC = 0.25

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS zip_codes (
    zip VARCHAR(5) PRIMARY KEY,
    city TEXT NOT NULL,
    state VARCHAR(2) NOT NULL,
    population INTEGER,
    latitude NUMERIC NOT NULL,
    longitude NUMERIC NOT NULL
);
CREATE INDEX IF NOT EXISTS zip_codes_city_idx ON zip_codes (lower(city), state);

ALTER TABLE schools
    ADD COLUMN IF NOT EXISTS latitude NUMERIC,
    ADD COLUMN IF NOT EXISTS longitude NUMERIC;

-- Great-circle distance in miles (haversine).
CREATE OR REPLACE FUNCTION miles_between(lat1 NUMERIC, lon1 NUMERIC, lat2 NUMERIC, lon2 NUMERIC)
RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS $$
    SELECT round((3958.8 * 2 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2)
    )))::numeric, 1)
$$;
"""

UPDATE_SCHOOLS_SQL = """
UPDATE schools s
SET latitude = z.latitude, longitude = z.longitude
FROM zip_codes z
WHERE left(s.zip, 5) = z.zip
"""

UPDATE_FROM_API_SQL = """
UPDATE schools s
SET latitude = c.latitude, longitude = c.longitude
FROM (VALUES %s) AS c (id, latitude, longitude)
WHERE s.id = c.id
"""


def read_zip_codes() -> list[tuple]:
    rows = []
    with open(ZIP_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            zip_code = row["zip_code"].strip()
            point = row["geo_point_2d"].strip()
            if len(zip_code) != 5 or "," not in point:
                continue
            lat, lng = (float(part) for part in point.split(","))
            population = int(float(row["population"])) if row["population"] else None
            rows.append((zip_code, row["usps_city"].strip(), row["stusps_code"].strip(), population, lat, lng))
    return rows


def fetch_school_coordinates(api_key: str) -> list[tuple[int, float, float]]:
    coords = []
    page = 0
    while True:
        response = requests.get(
            API_URL,
            params={
                "api_key": api_key, "fields": "id,location.lat,location.lon",
                "per_page": PER_PAGE, "page": page,
            },
            timeout=60,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        coords.extend(
            (school["id"], school["location.lat"], school["location.lon"])
            for school in results
            if school.get("location.lat") is not None and school.get("location.lon") is not None
        )
        if len(results) < PER_PAGE:
            return coords
        page += 1
        time.sleep(REQUEST_PAUSE_SEC)


def main():
    zip_rows = read_zip_codes()
    print(f"Read {len(zip_rows)} ZIP codes")
    api_key = os.getenv("COLLEGE_SCORECARD_API_KEY")
    school_coords = fetch_school_coordinates(api_key) if api_key else []
    print(f"Fetched coordinates for {len(school_coords)} schools from College Scorecard")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
            cursor.execute("TRUNCATE zip_codes")
            execute_values(
                cursor,
                "INSERT INTO zip_codes (zip, city, state, population, latitude, longitude) VALUES %s",
                zip_rows,
            )
            cursor.execute(UPDATE_SCHOOLS_SQL)
            print(f"Located {cursor.rowcount} school rows by ZIP")
            if school_coords:
                execute_values(cursor, UPDATE_FROM_API_SQL, school_coords, page_size=len(school_coords))
                print(f"Located {cursor.rowcount} school rows from College Scorecard")
            cursor.execute("SELECT count(latitude), count(*) FROM schools")
            located, total = cursor.fetchone()
        conn.commit()
    finally:
        conn.close()
    print(f"Schools with coordinates: {located} / {total} ({100 * located / total:.1f}%)")


if __name__ == "__main__":
    main()
