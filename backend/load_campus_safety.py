"""
Load Clery Act campus crime statistics (Campus Safety and Security, ope.ed.gov/campussafety)
into campus_crime, one row per school for calendar year 2024.

The 2025 release covers calendar years 2022-2024 with one row per campus (UNITID_P = UNITID
followed by a 3-digit campus number). Counts are summed over all campuses of a UNITID, so a
school's row includes branch sites, field stations, etc. (most of them report zero). The
enrollment in the file (men_total/women_total/Total) is the institution's total and is
repeated on every campus row, so it is taken once.

Only on-campus numbers (plus the residence-hall subset of them) are loaded; noncampus
buildings, public property and local police reports are left out.

The files are legacy .xls (read with xlrd) and .xlsx (openpyxl).

Usage (from backend/):
    python load_campus_safety.py
"""

import io
import zipfile
from collections import defaultdict

import openpyxl
import xlrd
from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, download, num, replace_table

load_dotenv(ROOT_DIR / ".env")

URL = "https://ope.ed.gov/campussafety/api/dataFiles/file?fileName=Crime2025EXCEL.zip"
ZIP_PATH = DATA_DIR / "campus_safety" / "Crime2025EXCEL.zip"
SOURCE = {"name": "Campus Safety and Security (Clery Act), U.S. Department of Education",
          "url": "https://ope.ed.gov/campussafety/", "year": "Calendar year 2024"}
YEAR = "24"

CRIMES = [  # (field prefix, column, description)
    ("MURD", "murder_count", "Murder/non-negligent manslaughter"),
    ("NEG_M", "negligent_manslaughter_count", "Negligent manslaughter"),
    ("RAPE", "rape_count", "Rape"),
    ("FONDL", "fondling_count", "Fondling"),
    ("INCES", "incest_count", "Incest"),
    ("STATR", "statutory_rape_count", "Statutory rape"),
    ("ROBBE", "robbery_count", "Robbery"),
    ("AGG_A", "aggravated_assault_count", "Aggravated assault"),
    ("BURGLA", "burglary_count", "Burglary"),
    ("VEHIC", "motor_vehicle_theft_count", "Motor vehicle theft"),
    ("ARSON", "arson_count", "Arson"),
]
SEXUAL = ("RAPE", "FONDL", "INCES", "STATR")
VAWA = [
    ("DOMEST", "domestic_violence_count", "Domestic violence incidents"),
    ("DATING", "dating_violence_count", "Dating violence incidents"),
    ("STALK", "stalking_count", "Stalking incidents"),
]
DRUGS = [("LIQUOR", "liquor"), ("DRUG", "drug"), ("WEAPON", "weapons")]
HATE = ["MURD", "RAPE", "FOND", "INCE", "STAT", "ROBBE", "AGG_A", "BURGLA", "VEHIC", "ARSON",
        "SIM_A", "LAR_T", "INTIM", "VANDAL"]

COLUMNS = (
    [("campus_count", "INTEGER", "Number of campuses/sites the school reports Clery data for (counts below are summed over them)"),
     ("students_total", "INTEGER", "Total enrollment used by the Clery file (institution-wide headcount)")]
    + [(col, "INTEGER", f"{label} on campus in 2024 (Clery criminal offenses)") for _, col, label in CRIMES]
    + [("criminal_offenses_total", "INTEGER", "Total on-campus Clery criminal offenses in 2024 (murder, manslaughter, sex offenses, robbery, aggravated assault, burglary, motor vehicle theft, arson)"),
       ("criminal_offenses_per_1000_students", "NUMERIC", "On-campus criminal offenses in 2024 per 1,000 enrolled students (criminal_offenses_total / students_total * 1000)"),
       ("sex_offenses_total", "INTEGER", "On-campus sex offenses in 2024 (rape + fondling + incest + statutory rape)"),
       ("criminal_offenses_total_2023", "INTEGER", "Total on-campus criminal offenses in 2023, for trend"),
       ("criminal_offenses_total_2022", "INTEGER", "Total on-campus criminal offenses in 2022, for trend")]
    + [(col, "INTEGER", f"{label} on campus in 2024 (VAWA offenses)") for _, col, label in VAWA]
    + [(f"{name}_arrests_count", "INTEGER", f"On-campus arrests for {name} law violations in 2024") for _, name in DRUGS]
    + [(f"{name}_referrals_count", "INTEGER", f"On-campus disciplinary referrals (not arrests) for {name} law violations in 2024") for _, name in DRUGS]
    + [("hate_crimes_count", "INTEGER", "On-campus hate crimes in 2024 (all offense types, including simple assault, larceny, intimidation and vandalism)"),
       ("residence_hall_offenses_total", "INTEGER", "Criminal offenses in on-campus student housing in 2024 (a subset of criminal_offenses_total)"),
       ("residence_hall_sex_offenses_total", "INTEGER", "Sex offenses (rape, fondling, incest, statutory rape) in on-campus student housing in 2024"),
       ("residence_hall_burglary_count", "INTEGER", "Burglaries in on-campus student housing in 2024")]
)


def read_sheet(archive: zipfile.ZipFile, name: str) -> list[dict]:
    """Rows of the first sheet of an .xls/.xlsx member as dicts keyed by upper-case header."""
    raw = archive.read(name)
    if name.endswith(".xls"):
        sheet = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
        rows = (sheet.row_values(i) for i in range(sheet.nrows))
    else:
        rows = openpyxl.load_workbook(io.BytesIO(raw), read_only=True).worksheets[0].iter_rows(values_only=True)
    header = [str(h).strip().upper() for h in next(rows)]
    return [dict(zip(header, row)) for row in rows if row and row[0] not in (None, "")]


def by_school(rows: list[dict]) -> dict[int, list[dict]]:
    schools = defaultdict(list)
    for row in rows:
        schools[int(row["UNITID_P"]) // 1000].append(row)
    return schools


def total(rows: list[dict], fields) -> int | None:
    values = [num(r.get(f)) for r in rows for f in fields]
    values = [v for v in values if v is not None]
    return int(sum(values)) if values else None


def main():
    download(URL, ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH) as archive:
        files = {key: by_school(read_sheet(archive, name)) for key, name in {
            "crime": "Oncampuscrime222324.xls",
            "vawa": "Oncampusvawa222324.xls",
            "arrest": "Oncampusarrest222324.xls",
            "discipline": "Oncampusdiscipline222324.xls",
            "hate": "Oncampushate222324.xlsx",
            "hall": "Residencehallcrime222324.xls",
        }.items()}

    rows = []
    for unitid, campuses in sorted(files["crime"].items()):
        students = num(campuses[0].get("TOTAL"))
        crimes = [total(campuses, [prefix + YEAR]) for prefix, _, _ in CRIMES]
        offenses = total(campuses, [prefix + YEAR for prefix, _, _ in CRIMES])
        per_1000 = round(offenses / students * 1000, 2) if offenses is not None and students else None
        hall = files["hall"].get(unitid, [])
        rows.append((
            unitid, len(campuses), students, *crimes, offenses, per_1000,
            total(campuses, [p + YEAR for p in SEXUAL]),
            total(campuses, [prefix + "23" for prefix, _, _ in CRIMES]),
            total(campuses, [prefix + "22" for prefix, _, _ in CRIMES]),
            *(total(files["vawa"].get(unitid, []), [prefix + YEAR]) for prefix, _, _ in VAWA),
            *(total(files["arrest"].get(unitid, []), [prefix + YEAR]) for prefix, _ in DRUGS),
            *(total(files["discipline"].get(unitid, []), [prefix + YEAR]) for prefix, _ in DRUGS),
            total(files["hate"].get(unitid, []), [p + YEAR for p in HATE]),
            total(hall, [prefix + YEAR for prefix, _, _ in CRIMES]),
            total(hall, [p + YEAR for p in SEXUAL]),
            total(hall, ["BURGLA" + YEAR]),
        ))

    with get_db_connection() as conn:
        replace_table(
            conn, "campus_crime",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")] + COLUMNS,
            rows,
            "Clery Act campus crime statistics for calendar year 2024 (U.S. Department of Education "
            "Campus Safety and Security survey). Counts are incidents reported ON CAMPUS, summed over "
            "all of a school's campuses; residence_hall_* columns are the on-campus student housing "
            "subset. Counts are reported crimes, not convictions, and depend on campus size and "
            "reporting practices; compare schools with criminal_offenses_per_1000_students.",
            SOURCE,
        )


if __name__ == "__main__":
    main()
