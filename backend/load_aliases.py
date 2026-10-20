"""
Load school aliases from College Scorecard (school.alias) plus the curated
list in data/school_aliases_curated.csv.

Usage (from backend/):
    python load_aliases.py
"""

import csv
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from database import get_db_connection, get_latest_year
from school_lookup import normalize

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
load_dotenv(ROOT_DIR / ".env")

API_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"
PER_PAGE = 100
REQUEST_PAUSE_SEC = 0.25

INSERT_SQL = """
INSERT INTO school_aliases (school_id, alias, alias_norm, source)
VALUES (%s, %s, %s, %s)
ON CONFLICT (school_id, alias_norm) DO NOTHING
"""


# Scorecard aliases made only of these words ("Tech", "State", "Inc", "N/A")
# match too many schools to be useful.
GENERIC_WORDS = {
    "a", "n", "na", "none", "inc", "llc", "the", "of", "at", "and", "in", "univ",
    "university", "college", "state", "tech", "institute", "school", "community",
    "campus", "main", "online", "north", "south", "east", "west", "northern",
    "southern", "eastern", "western", "central",
}


def is_useful_alias(alias: str) -> bool:
    words = normalize(alias).split()
    return bool(words) and not set(words) <= GENERIC_WORDS


def split_aliases(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = (part.strip() for part in re.split(r"[,|;]", raw))
    return [part for part in parts if is_useful_alias(part)]


def fetch_scorecard_aliases(api_key: str) -> list[tuple[int, str]]:
    pairs: list[tuple[int, str]] = []
    page = 0
    while True:
        response = requests.get(
            API_URL,
            params={"api_key": api_key, "fields": "id,school.alias", "per_page": PER_PAGE, "page": page},
            timeout=60,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        for school in results:
            pairs.extend((school["id"], alias) for alias in split_aliases(school.get("school.alias")))
        if len(results) < PER_PAGE:
            return pairs
        page += 1
        time.sleep(REQUEST_PAUSE_SEC)


def load_curated(cursor, year: str) -> list[tuple[int, str]]:
    pairs: list[tuple[int, str]] = []
    missing: list[str] = []
    with open(DATA_DIR / "school_aliases_curated.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cursor.execute(
                "SELECT id FROM schools WHERE year = %s AND name = %s",
                (year, row["official_name"]),
            )
            ids = [r[0] for r in cursor.fetchall()]
            if len(ids) != 1:
                missing.append(f"{row['alias']} -> {row['official_name']} ({len(ids)} matches)")
                continue
            pairs.append((ids[0], row["alias"]))
    if missing:
        raise SystemExit("Curated aliases need fixing:\n  " + "\n  ".join(missing))
    return pairs


def main():
    api_key = os.getenv("COLLEGE_SCORECARD_API_KEY")
    if not api_key:
        raise SystemExit("COLLEGE_SCORECARD_API_KEY not set in .env")

    year = get_latest_year()
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute((DATA_DIR / "school_aliases.sql").read_text())
            cursor.execute("SELECT id FROM schools WHERE year = %s", (year,))
            known_ids = {r[0] for r in cursor.fetchall()}

            curated = load_curated(cursor, year)
            print("Fetching aliases from College Scorecard API...")
            scorecard = [(sid, alias) for sid, alias in fetch_scorecard_aliases(api_key) if sid in known_ids]

            for source, pairs in (("curated", curated), ("scorecard", scorecard)):
                for school_id, alias in pairs:
                    cursor.execute(INSERT_SQL, (school_id, alias, normalize(alias), source))
                print(f"{source}: {len(pairs)} aliases")

            cursor.execute("SELECT COUNT(*), COUNT(DISTINCT school_id) FROM school_aliases")
            total, schools = cursor.fetchone()
        conn.commit()
        print(f"Done. {total} aliases covering {schools} schools.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
