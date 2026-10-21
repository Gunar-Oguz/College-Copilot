"""
Load application deadlines from data/school_deadlines.csv into school_deadlines.

The CSV was researched for the Fall 2027 cycle from the Common App's 2026-27 first-year deadline
grid (https://content.commonapp.org/Files/ReqGrid.pdf) and official admissions pages; every row
cites its source_url. Update the CSV each admissions cycle and rerun.

Usage (from backend/):
    python load_deadlines.py
"""

import csv
from datetime import date

from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, replace_table

load_dotenv(ROOT_DIR / ".env")

CSV_PATH = DATA_DIR / "school_deadlines.csv"
TYPES = {"early_decision", "early_decision_2", "early_action", "restrictive_early_action", "regular",
         "rolling", "priority"}

COLUMNS = [
    ("unitid", "INTEGER NOT NULL", "IPEDS UNITID, joins to schools.id"),
    ("deadline_type", "TEXT NOT NULL", "early_decision, early_decision_2, early_action, restrictive_early_action, "
                                       "regular, rolling or priority"),
    ("deadline_date", "DATE", "Application deadline for the cycle; NULL for rolling admission with no date"),
    ("notes", "TEXT", "Details, e.g. 'Early Action II' or 'UC application'"),
    ("source_url", "TEXT", "Official page or Common App grid where the date was confirmed"),
    ("cycle", "TEXT", "Entry term the deadline is for, e.g. 'Fall 2027'"),
]


def read_rows() -> list[tuple]:
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["deadline_type"] not in TYPES:
                raise ValueError(f"Unknown deadline_type {row['deadline_type']!r} for {row['school_name']}")
            day = date.fromisoformat(row["deadline_date"]) if row["deadline_date"] else None
            rows.append((int(row["unitid"]), row["deadline_type"], day, row["notes"] or None,
                         row["source_url"] or None, row["cycle"] or None))
    return rows


def main():
    rows = read_rows()
    with get_db_connection() as conn:
        replace_table(
            conn, "school_deadlines", COLUMNS, rows,
            "Application deadlines for about 115 colleges (Ivy League, flagships, largest schools), "
            "Fall 2027 cycle. Several rows per school, one per deadline type.",
            {"name": "Common App 2026-27 deadline grid and official admissions pages",
             "url": "https://content.commonapp.org/Files/ReqGrid.pdf", "year": "Fall 2027 entry"},
        )
        with conn.cursor() as cursor:
            cursor.execute("CREATE INDEX ON school_deadlines (unitid)")
        conn.commit()
    print(f"Loaded {len(rows)} deadlines for {len({r[0] for r in rows})} schools")


if __name__ == "__main__":
    main()
