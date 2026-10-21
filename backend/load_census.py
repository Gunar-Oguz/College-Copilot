"""
Load Census ACS 2020-2024 5-year estimates for each campus ZIP code into area_demographics.

One row per school (school_id = UNITID), using the ZIP Code Tabulation Area (ZCTA) that
matches the first five digits of schools.zip (latest year). Values come from the keyless
ACS table-based summary files (pipe-delimited, one file per table); only ZCTA rows for
campus ZIPs are kept. Files are downloaded to data/census/ when missing (~400 MB total).

Caveats: ZCTAs approximate ZIP codes, and PO-box or campus-only ZIPs may have no ZCTA.
Campus ZIPs are often dominated by students in dorms; dorm residents (group quarters) are
excluded from household income and poverty figures, so those describe the surrounding
households, which in student areas skew young and low-income.

Usage (from backend/):
    python load_census.py
"""

from dotenv import load_dotenv

from database import execute_sql, get_db_connection, get_latest_year
from source_data import DATA_DIR, ROOT_DIR, download, num, replace_table

load_dotenv(ROOT_DIR / ".env")

CENSUS_DIR = DATA_DIR / "census"
BASE_URL = "https://www2.census.gov/programs-surveys/acs/summary_file/2024/table-based-SF/data/5YRData"
SOURCE = {"name": "U.S. Census Bureau, American Community Survey 2020-2024 5-year estimates",
          "url": "https://www.census.gov/programs-surveys/acs/data/summary-file.html",
          "year": "2020-2024"}
ZCTA_PREFIX = "860Z200US"
TABLES = ["b01003", "b01002", "b19013", "b25064", "b25077", "b17001", "b15003", "b03002"]


def value(row: dict, field: str):
    return num(row.get(field))  # ACS uses large negative codes (e.g. -666666666) for missing


def share(row: dict, fields: list[str], total: str):
    denominator = value(row, total)
    parts = [value(row, f) for f in fields]
    if not denominator or any(p is None for p in parts):
        return None
    return round(sum(parts) / denominator, 4)


# (column, sql type, description, function of the merged ACS row)
COLUMNS = [
    ("population_total", "INTEGER", "Total population of the ZIP code area (includes students living in dorms)",
     lambda r: value(r, "B01003_E001")),
    ("median_age", "NUMERIC", "Median age of residents, in years",
     lambda r: value(r, "B01002_E001")),
    ("median_household_income_amount", "INTEGER",
     "Median household income in 2024 dollars (households only, excludes dorm residents; 250001 means $250,000 or more)",
     lambda r: value(r, "B19013_E001")),
    ("median_gross_rent_cost", "INTEGER", "Median monthly gross rent (rent plus utilities) in dollars; 3501 means $3,500 or more",
     lambda r: value(r, "B25064_E001")),
    ("median_home_value_amount", "INTEGER", "Median value of owner-occupied homes in dollars; 2000001 means $2,000,000 or more",
     lambda r: value(r, "B25077_E001")),
    ("poverty_share", "NUMERIC", "Share of residents (poverty status determined, excludes dorm residents) with income below the poverty line",
     lambda r: share(r, ["B17001_E002"], "B17001_E001")),
    ("bachelors_or_higher_share", "NUMERIC", "Share of adults 25 and older with a bachelor's degree or higher",
     lambda r: share(r, ["B15003_E022", "B15003_E023", "B15003_E024", "B15003_E025"], "B15003_E001")),
    ("graduate_degree_share", "NUMERIC", "Share of adults 25 and older with a master's, professional or doctoral degree",
     lambda r: share(r, ["B15003_E023", "B15003_E024", "B15003_E025"], "B15003_E001")),
    ("white_share", "NUMERIC", "Share of residents who are White, not Hispanic",
     lambda r: share(r, ["B03002_E003"], "B03002_E001")),
    ("black_share", "NUMERIC", "Share of residents who are Black, not Hispanic",
     lambda r: share(r, ["B03002_E004"], "B03002_E001")),
    ("asian_share", "NUMERIC", "Share of residents who are Asian, not Hispanic",
     lambda r: share(r, ["B03002_E006"], "B03002_E001")),
    ("hispanic_share", "NUMERIC", "Share of residents who are Hispanic or Latino (any race)",
     lambda r: share(r, ["B03002_E012"], "B03002_E001")),
]


def read_zcta_rows(table: str, zips: set[str]) -> dict[str, dict]:
    """ZCTA rows of one ACS summary file, keyed by 5-digit ZIP, for the given ZIPs only."""
    path = download(f"{BASE_URL}/acsdt5y2024-{table}.dat", CENSUS_DIR / f"acsdt5y2024-{table}.dat")
    rows = {}
    with path.open(encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split("|")
        for line in handle:
            if not line.startswith(ZCTA_PREFIX) or line[9:14] not in zips:
                continue
            rows[line[9:14]] = dict(zip(header, line.rstrip("\n").split("|")))
    print(f"  read {table}: {len(rows):,} campus ZCTAs")
    return rows


def main():
    schools = execute_sql(
        "SELECT id, left(zip, 5) AS zip FROM schools WHERE year = %s AND zip IS NOT NULL",
        (get_latest_year(),),
    )
    zips = {s["zip"] for s in schools}
    print(f"Reading ACS files for {len(zips):,} campus ZIP codes...")
    merged: dict[str, dict] = {}
    for table in TABLES:
        for zcta, row in read_zcta_rows(table, zips).items():
            merged.setdefault(zcta, {}).update(row)

    rows = []
    for school in sorted(schools, key=lambda s: s["id"]):
        acs = merged.get(school["zip"])
        if acs is None:
            continue
        rows.append((school["id"], school["zip"], *(fn(acs) for _, _, _, fn in COLUMNS)))

    with get_db_connection() as conn:
        replace_table(
            conn, "area_demographics",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id"),
             ("zcta", "TEXT", "5-digit ZIP Code Tabulation Area of the campus (from schools.zip)")]
            + [(name, sql_type, description) for name, sql_type, description, _ in COLUMNS],
            rows,
            "Census ACS 2020-2024 5-year estimates for the ZIP code (ZCTA) where the campus is located. "
            "Describes the surrounding neighborhood, not the student body; campus ZIPs are often dominated "
            "by students, so incomes there can look low",
            SOURCE,
        )


if __name__ == "__main__":
    main()
