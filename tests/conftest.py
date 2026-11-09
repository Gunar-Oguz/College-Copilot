"""
Shared fixtures. `duck_db` builds a tiny synthetic DuckDB database (a handful of real-looking
schools, nicknames and ZIP codes) in a temporary directory and points the app at it, so the
DuckDB backend is tested without the real data file, PostgreSQL or the network.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import database  # noqa: E402

UCLA, BERKELEY, UW_SEATTLE, UW_MADISON, UKIAH = 110662, 110635, 236948, 240444, 481182
YEAR, OLD_YEAR = "2024-25", "2015-16"

SCHOOL_COLUMNS = {
    "id": "INTEGER", "year": "VARCHAR", "name": "VARCHAR", "city": "VARCHAR", "state": "VARCHAR",
    "ownership": "INTEGER", "carnegie_class": "INTEGER", "highest_degree": "INTEGER",
    "admission_rate": "DOUBLE", "sat_average": "DOUBLE", "student_size": "INTEGER", "undergraduate_size": "INTEGER",
    "cost_avg_net_price": "DOUBLE", "cost_net_price_income_48_75k": "DOUBLE", "completion_rate": "DOUBLE",
    "retention_rate": "DOUBLE", "earnings_median_10yr": "DOUBLE", "pell_grant_rate": "DOUBLE",
    "latitude": "DOUBLE", "longitude": "DOUBLE", "school_url": "VARCHAR", "copilot_score": "DOUBLE",
}
SCHOOLS = [
    (UCLA, "University of California-Los Angeles", "Los Angeles", "CA", 1, 15, 4, 0.09, 1405, 33040, 32000,
     12548, 9000, 0.926, 0.97, 82511, 0.28, 34.0689, -118.4452, "www.ucla.edu", 90.1),
    (BERKELEY, "University of California-Berkeley", "Berkeley", "CA", 1, 15, 4, 0.11, 1415, 32000, 31000,
     14000, 10000, 0.94, 0.96, 90000, 0.27, 37.8719, -122.2585, "www.berkeley.edu", 91.5),
    (UW_SEATTLE, "University of Washington-Seattle Campus", "Seattle", "WA", 1, 15, 4, 0.43, 1330, 36000, 35000,
     11000, 8000, 0.84, 0.94, 76000, 0.22, 47.6553, -122.3035, "www.washington.edu", 85.0),
    (UW_MADISON, "University of Wisconsin-Madison", "Madison", "WI", 1, 15, 4, 0.49, 1400, 36000, 35000,
     16000, 12000, 0.89, 0.95, 70000, 0.14, 43.0766, -89.4125, "www.wisc.edu", 86.0),
    (UKIAH, "Ukiah Adult School", "Ukiah", "CA", 1, None, 1, None, None, 300, 300,
     None, None, 1.0, None, None, None, 39.15, -123.2, None, None),
]


def build_tiny_db(path: Path) -> Path:
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE schools (" + ", ".join(f"{c} {t}" for c, t in SCHOOL_COLUMNS.items()) + ")")
    for row in SCHOOLS:
        for year in (YEAR, OLD_YEAR):
            values = (row[0], year, *row[1:])
            conn.execute(f"INSERT INTO schools VALUES ({', '.join('?' * len(values))})", values)
    conn.execute("UPDATE schools SET admission_rate = 0.18 WHERE id = ? AND year = ?", (UCLA, OLD_YEAR))
    conn.execute("CREATE TABLE school_aliases (school_id INTEGER, alias VARCHAR, alias_norm VARCHAR, source VARCHAR)")
    conn.executemany("INSERT INTO school_aliases VALUES (?, ?, ?, 'curated')", [
        (UCLA, "UCLA", "ucla"), (BERKELEY, "Cal", "cal"), (BERKELEY, "UC Berkeley", "uc berkeley"),
        (UW_SEATTLE, "UW", "uw"), (UW_MADISON, "UW", "uw"),
    ])
    conn.execute("CREATE TABLE zip_codes (zip VARCHAR, city VARCHAR, state VARCHAR, population INTEGER, "
                 "latitude DOUBLE, longitude DOUBLE)")
    conn.executemany("INSERT INTO zip_codes VALUES (?, ?, ?, ?, ?, ?)", [
        ("94720", "Berkeley", "CA", 3000, 37.8738, -122.2549),
        ("90024", "Los Angeles", "CA", 50000, 34.0633, -118.4357),
        ("90012", "Los Angeles", "CA", 30000, 34.0614, -118.2385),
        ("78701", "Austin", "TX", 10000, 30.2713, -97.7426),
    ])
    conn.execute("CREATE TABLE programs (school_id INTEGER, year VARCHAR, cip_code VARCHAR, program_name VARCHAR, "
                 "credential_level INTEGER, credential_name VARCHAR, earnings_median_1yr INTEGER, "
                 "earnings_median_4yr INTEGER, debt_median DOUBLE, debt_monthly_payment DOUBLE, "
                 "completion_rate DOUBLE)")
    conn.executemany("INSERT INTO programs VALUES (?, ?, ?, ?, 3, 'Bachelor''s Degree', ?, ?, NULL, NULL, NULL)", [
        (UCLA, YEAR, "11.07", "Computer Science.", 110000, 140000),
        (BERKELEY, YEAR, "11.07", "Computer Science.", 120000, 160000),
        (UCLA, YEAR, "52.02", "Business Administration.", 60000, 80000),
    ])
    conn.execute("CREATE TABLE copilot_scores (school_id INTEGER, copilot_score DOUBLE, copilot_rank INTEGER, "
                 "graduation_score DOUBLE, earnings_score DOUBLE, selectivity_score DOUBLE, "
                 "student_faculty_score DOUBLE)")
    conn.executemany("INSERT INTO copilot_scores VALUES (?, ?, ?, 90, 90, 90, 80)",
                     [(UCLA, 90.1, 2), (BERKELEY, 91.5, 1), (UW_SEATTLE, 85.0, 4), (UW_MADISON, 86.0, 3)])
    conn.execute("CREATE TABLE ipeds_profile (school_id INTEGER, website VARCHAR, locale VARCHAR, "
                 "carnegie_basic VARCHAR, religious_affiliation VARCHAR, hbcu BOOLEAN)")
    conn.execute("INSERT INTO ipeds_profile VALUES (?, 'www.ucla.edu', 'City: Large', "
                 "'Doctoral: Very High Research', NULL, false)", (UCLA,))
    conn.execute("CREATE TABLE ipeds_costs (school_id INTEGER, tuition_fees_in_state DOUBLE, "
                 "tuition_fees_out_of_state DOUBLE, room_board_on_campus_cost DOUBLE)")
    conn.execute("INSERT INTO ipeds_costs VALUES (?, 13747, 44524, 17000)", (UCLA,))
    conn.execute("CREATE TABLE ipeds_enrollment (school_id INTEGER, student_faculty_ratio DOUBLE, "
                 "total_enrollment INTEGER)")
    conn.execute("INSERT INTO ipeds_enrollment VALUES (?, 18, 46000)", (UCLA,))
    conn.execute("CREATE TABLE campus_crime (school_id INTEGER, criminal_offenses_total INTEGER, "
                 "criminal_offenses_per_1000_students DOUBLE, criminal_offenses_total_2023 INTEGER, "
                 "criminal_offenses_total_2022 INTEGER, sex_offenses_total INTEGER, robbery_count INTEGER, "
                 "aggravated_assault_count INTEGER, burglary_count INTEGER, motor_vehicle_theft_count INTEGER, "
                 "stalking_count INTEGER, dating_violence_count INTEGER, hate_crimes_count INTEGER, "
                 "liquor_referrals_count INTEGER, drug_referrals_count INTEGER)")
    conn.execute("INSERT INTO campus_crime VALUES (?, 460, 10.0, 400, 380, 20, 5, 10, 100, 20, 30, 15, 2, 50, 10)",
                 (UCLA,))
    conn.execute("CREATE TABLE data_sources (table_name VARCHAR, source VARCHAR, url VARCHAR, data_year VARCHAR, "
                 "row_count INTEGER, matched_schools INTEGER)")
    conn.executemany("INSERT INTO data_sources VALUES (?, ?, 'https://example.org', ?, 4, 4)", [
        ("copilot_scores", "CollegeCopilot Score", "2024-25"), ("ipeds_costs", "IPEDS, NCES", "2023-24"),
    ])
    conn.execute("CREATE TABLE app_schema (table_name VARCHAR, column_name VARCHAR, data_type VARCHAR, "
                 "ordinal_position INTEGER, note VARCHAR, table_note VARCHAR)")
    schema = [("schools", c, "numeric" if t == "DOUBLE" else t.lower(), i, None, None)
              for i, (c, t) in enumerate(SCHOOL_COLUMNS.items(), 1)]
    schema += [("programs", "school_id", "integer", 1, None, None), ("programs", "cip_code", "character varying", 3,
                                                                     "4-digit CIP family", None),
               ("copilot_scores", "copilot_score", "numeric", 2, "0-100", "CollegeCopilot Score")]
    conn.executemany("INSERT INTO app_schema VALUES (?, ?, ?, ?, ?, ?)", schema)
    conn.execute(database.MILES_BETWEEN_DUCKDB)
    conn.close()
    return path


@pytest.fixture(scope="session")
def tiny_db_file(tmp_path_factory) -> Path:
    return build_tiny_db(tmp_path_factory.mktemp("duckdb") / "tiny.duckdb")


@pytest.fixture
def duck_db(tiny_db_file, monkeypatch):
    """Point the app at the tiny DuckDB file for one test, then restore PostgreSQL settings."""
    monkeypatch.setenv("DB_BACKEND", "duckdb")
    monkeypatch.setenv("DUCKDB_PATH", str(tiny_db_file))
    database.use_duckdb(None, None)
    yield tiny_db_file
    database.use_duckdb(None, None)
