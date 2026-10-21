"""
Load official federal student loan cohort default rates (FSA, fsapartners.ed.gov) into
cohort_default_rates, one row per school.

A cohort default rate is the share of a school's federal student loan borrowers who entered
repayment in a fiscal year and defaulted by the end of the second following fiscal year.
FY2021-FY2023 rates are far below historical levels (national FY2023 average about 0.5%)
because of the 2020-2023 payment pause and the 2023-24 repayment "on-ramp". The source
rounds rates to 0.1 percentage point, so very small rates show as 0.

Rates are published per 6-digit OPEID. The main campus UNITID comes from
load_mobility.opeid6_to_unitids(); branch campuses sharing the OPEID get the same numbers
with shared_with_main_campus = true.

Program length codes 13 and 14 are not in FSA's published code list; comparing them with the
IPEDS highest degree offered shows 13 = master's and 14 = doctoral institutions.

Usage (from backend/):
    python load_default_rates.py
"""

import openpyxl
from dotenv import load_dotenv

from database import get_db_connection
from load_mobility import HD_URL, opeid6_to_unitids
from source_data import DATA_DIR, ROOT_DIR, download, num, pct, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

URL = "https://fsapartners.ed.gov/sites/default/files/2026-09/EligibleSchoolFileFY2023OfficialFinal.xlsx"
PATH = DATA_DIR / "default_rates" / "EligibleSchoolFileFY2023OfficialFinal.xlsx"
SOURCE = {"name": "Official Cohort Default Rates, Federal Student Aid",
          "url": "https://fsapartners.ed.gov/knowledge-center/topics/default-management/official-cohort-default-rates-schools",
          "year": "FY2023 (with FY2022, FY2021)"}

PROGRAM_LENGTH = {
    0: "Short-term (300-599 hours)", 1: "Graduate/professional only", 2: "Non-degree, under 1 year",
    3: "Non-degree, 1 year", 4: "Non-degree, 2 years", 5: "Associate's degree",
    6: "Bachelor's degree", 7: "First professional degree", 8: "Master's or doctor's degree",
    9: "Professional certification", 10: "Undergraduate (previous degree required)",
    11: "Non-degree, 3+ years", 12: "Two-year transfer", 13: "Master's degree", 14: "Doctoral degree",
}
SCHOOL_TYPE = {1: "Public", 2: "Private nonprofit", 3: "Proprietary (for-profit)",
               5: "Foreign public", 6: "Foreign private", 7: "Foreign for-profit"}

COLUMNS = [
    ("opeid6", "TEXT", "6-digit OPEID the rates are published for"),
    ("cdr_school_name", "TEXT", "School name in the FSA file"),
    ("shared_with_main_campus", "BOOLEAN", "True for a branch campus: the rate covers the whole institution (all campuses sharing the OPEID), not this campus alone"),
    ("default_rate_fy2023", "NUMERIC", "Official 3-year cohort default rate for borrowers entering repayment in FY2023 (decimal, 0.05 = 5%). Most recent year"),
    ("default_rate_fy2022", "NUMERIC", "Official cohort default rate, FY2022 cohort (near zero nationally due to the loan payment pause)"),
    ("default_rate_fy2021", "NUMERIC", "Official cohort default rate, FY2021 cohort (near zero nationally due to the loan payment pause)"),
    ("borrowers_in_repayment_count", "INTEGER", "Borrowers who entered repayment in FY2023 (denominator of default_rate_fy2023)"),
    ("defaulted_borrowers_count", "INTEGER", "FY2023-cohort borrowers who defaulted (numerator of default_rate_fy2023)"),
    ("uses_average_formula", "BOOLEAN", "True when fewer than 30 borrowers entered repayment, so FSA averaged three cohorts"),
    ("program_length", "TEXT", "Longest program the school offers, e.g. \"Bachelor's degree\", 'Doctoral degree', \"Associate's degree\""),
    ("school_type", "TEXT", "Control per FSA: 'Public', 'Private nonprofit', 'Proprietary (for-profit)', or foreign"),
]


def read_rows() -> list[dict]:
    sheet = openpyxl.load_workbook(download(URL, PATH), read_only=True).worksheets[0]
    rows = sheet.iter_rows(min_row=2, values_only=True)  # row 1 is a title
    header = [str(h).strip() for h in next(rows)]
    out = []
    for row in rows:
        if row[0] is None:
            break
        out.append(dict(zip(header, row)))
    return out


def branch_unitids(main: dict[int, list[int]]) -> dict[int, list[int]]:
    """UNITIDs sharing a 6-digit OPEID with a main campus, excluding the main campus itself."""
    branches = {}
    for row in read_csv(download(HD_URL, DATA_DIR / "ipeds" / "HD2024.zip")):
        opeid = row["OPEID"]
        if opeid[:6].isdigit():
            unitid, opeid6 = int(row["UNITID"]), int(opeid[:6])
            if unitid not in main.get(opeid6, []):
                branches.setdefault(opeid6, []).append(unitid)
    return branches


def main():
    main_campuses = opeid6_to_unitids()
    branches = branch_unitids(main_campuses)
    best: dict[int, tuple] = {}
    for row in read_rows():
        code = str(row["School Code"]).strip()
        if not code.isdigit():
            continue
        opeid6 = int(code)
        record = (
            code.zfill(6), str(row["Name"]).strip(), pct(row["Default Rate 1"]), pct(row["Default Rate 2"]),
            pct(row["Default Rate 3"]), num(row["Denom 1"]), num(row["Num 1"]),
            num(row["Average or Greater than 30"]) == 1,
            PROGRAM_LENGTH.get(num(row["Prog Length"])), SCHOOL_TYPE.get(num(row["School Type"])),
        )
        targets = [(u, False) for u in main_campuses.get(opeid6, [])]
        targets += [(u, True) for u in branches.get(opeid6, [])]
        for unitid, shared in targets:
            # A UNITID can be reached twice (rare OPEID oddities): keep the main-campus match,
            # then the larger borrower cohort.
            key = (not shared, record[5] or 0)
            if unitid not in best or key > best[unitid][0]:
                best[unitid] = (key, (unitid, record[0], record[1], shared, *record[2:]))

    rows = [value for _, value in best.values()]
    with get_db_connection() as conn:
        replace_table(
            conn, "cohort_default_rates",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")] + COLUMNS,
            rows,
            "Official federal student loan cohort default rates (Federal Student Aid, released 2026): "
            "share of borrowers entering repayment in a fiscal year who defaulted within about three "
            "years. Rates are decimals rounded to 0.001. All three cohorts are far below historical levels "
            "(FY2021-FY2022 near zero) because of the 2020-2023 payment pause and repayment on-ramp; "
            "default_rate_fy2023 is the most recent.",
            SOURCE,
        )


if __name__ == "__main__":
    main()
