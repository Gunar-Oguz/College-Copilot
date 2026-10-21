"""
Load Opportunity Insights college mobility data (opportunityinsights.org/data).

- mobility: Chetty et al. "Mobility Report Cards" (2017), one row per school. Students
  born 1980-82; parent income measured when they were teens, child earnings around 2014.
- mobility_admissions_by_income: Chetty, Deming & Friedman (2023) attendance and
  application rates by parent income for 139 highly selective colleges.

Opportunity Insights groups colleges by "super OPEID". mrc_table11 maps it to 6-digit
OPEIDs, and the IPEDS directory (HD2024, see load_ipeds.py) maps OPEIDs to UNITIDs.
Branch campuses sharing a 6-digit OPEID get their parent institution's numbers.

Usage (from backend/):
    python load_mobility.py
"""

from collections import defaultdict

from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, download, num, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

OI_DIR = DATA_DIR / "opportunity_insights"
OI_URL = "https://opportunityinsights.org/wp-content/uploads"
FILES = {
    "table2": f"{OI_URL}/2018/04/mrc_table2.csv",
    "table11": f"{OI_URL}/2018/04/mrc_table11.csv",
    "admissions": f"{OI_URL}/2023/07/CollegeAdmissions_Data.csv",
}
HD_URL = "https://nces.ed.gov/ipeds/datacenter/data/HD2024.zip"

MOBILITY_COLUMNS = [
    # (column, sql type, description, source field)
    ("oi_name", "TEXT", "College (or group of colleges) name in Opportunity Insights", "NAME"),
    ("oi_grouped", "BOOLEAN", "True when Opportunity Insights reports several colleges together (e.g. a university system), so the numbers are for the group", "MULTI"),
    ("students_in_cohort_count", "INTEGER", "Students in the 1980-82 birth cohorts used", "COUNT"),
    ("parent_median_income", "INTEGER", "Median parent household income (2015 dollars)", "PAR_MEDIAN"),
    ("parent_mean_income", "INTEGER", "Mean parent household income (2015 dollars)", "PAR_MEAN"),
    ("parents_bottom_20pct_share", "NUMERIC", "Share of students whose parents were in the bottom income quintile", "PAR_Q1"),
    ("parents_top_20pct_share", "NUMERIC", "Share of students whose parents were in the top income quintile", "PAR_Q5"),
    ("parents_top_1pct_share", "NUMERIC", "Share of students whose parents were in the top 1% of income", "PAR_TOP1PC"),
    ("child_median_earnings", "INTEGER", "Median individual earnings of students at about age 34 (2015 dollars)", "K_MEDIAN"),
    ("child_top_20pct_share", "NUMERIC", "Share of students who reached the top income quintile as adults", "K_Q5"),
    ("child_top_1pct_share", "NUMERIC", "Share of students who reached the top 1% as adults", "K_TOP1PC"),
    ("mobility_rate", "NUMERIC", "Mobility rate: share of students who came from the bottom quintile AND reached the top quintile (access x success). Main Opportunity Insights ranking measure", "MR_KQ5_PQ1"),
    ("success_rate_bottom_20pct", "NUMERIC", "Success rate: share of bottom-quintile students who reached the top quintile", "KQ5_COND_PARQ1"),
    ("upper_tail_mobility_rate", "NUMERIC", "Share of students from the bottom quintile who reached the top 1%", "MR_KTOP1_PQ1"),
    ("top_1pct_success_rate_bottom_20pct", "NUMERIC", "Share of bottom-quintile students who reached the top 1%", "KTOP1PC_COND_PARQ1"),
]

ADMISSIONS_COLUMNS = [
    ("parent_income_bin", "TEXT", "Parent income percentile range, e.g. '0-20', '99-99.9', 'Top 0.1'", "PAR_INCOME_LAB"),
    ("parent_income_order", "NUMERIC", "Sort key for parent_income_bin (low to high)", "PAR_INCOME_BIN"),
    ("attend_rate", "NUMERIC", "Share of all U.S. students in this parent income group who attended the college", "ATTEND"),
    ("relative_apply", "NUMERIC", "Application rate relative to the college's average (1.0 = average)", "REL_APPLY"),
    ("relative_attend", "NUMERIC", "Attendance rate relative to the college's average (1.0 = average)", "REL_ATTEND"),
    ("relative_attend_same_sat", "NUMERIC", "Relative attendance rate among students with the same SAT/ACT score", "REL_ATTEND_SAT"),
    ("relative_admit_attend_same_sat", "NUMERIC", "Attendance rate among applicants with the same test score, relative to average (captures admission preferences)", "REL_ATT_COND_APP_SAT"),
    ("college_group", "TEXT", "Opportunity Insights college group, e.g. 'Ivy Plus', 'Other Top 100 Private'", "TEST_BAND_TIER"),
]

SOURCE = {"name": "Opportunity Insights", "url": "https://opportunityinsights.org/data/"}


def opeid6_to_unitids() -> dict[int, list[int]]:
    """Main campus (8-digit OPEID ending in 00) for each 6-digit OPEID, else all campuses."""
    hd = read_csv(download(HD_URL, DATA_DIR / "ipeds" / "HD2024.zip"))
    main, branches = defaultdict(list), defaultdict(list)
    for row in hd:
        opeid = row["OPEID"]
        if opeid[:6].isdigit():
            (main if opeid.endswith("00") else branches)[int(opeid[:6])].append(int(row["UNITID"]))
    return {opeid6: main.get(opeid6) or branches[opeid6] for opeid6 in main.keys() | branches.keys()}


def super_opeid_to_unitids(table11: list[dict], by_opeid: dict[int, list[int]]) -> dict[int, list[int]]:
    mapping = defaultdict(set)
    for row in table11:
        super_opeid = int(row["SUPER_OPEID"])
        if super_opeid > 0:
            mapping[super_opeid].update(by_opeid.get(int(row["OPEID"]), []))
    return {k: sorted(v) for k, v in mapping.items()}


def value(field: str, raw: str):
    if field in ("NAME", "PAR_INCOME_LAB", "TEST_BAND_TIER"):
        return raw or None
    if field == "MULTI":
        return raw == "1"
    number = num(raw)
    if isinstance(number, float) and field in ("PAR_MEDIAN", "PAR_MEAN", "K_MEDIAN", "COUNT"):
        return round(number)
    return number


def main():
    paths = {key: download(url, OI_DIR / url.rsplit("/", 1)[1]) for key, url in FILES.items()}
    unitids = super_opeid_to_unitids(read_csv(paths["table11"]), opeid6_to_unitids())

    rows, unmatched = [], 0
    for row in read_csv(paths["table2"]):
        targets = unitids.get(int(row["SUPER_OPEID"]), [])
        unmatched += not targets
        for unitid in targets:
            rows.append((unitid, *(value(field, row[field]) for _, _, _, field in MOBILITY_COLUMNS)))
    print(f"  {unmatched} Opportunity Insights colleges had no current UNITID (closed or merged)")

    admissions = []
    for row in read_csv(paths["admissions"]):
        for unitid in unitids.get(int(row["SUPER_OPEID"]), []):
            admissions.append((unitid, *(value(field, row[field]) for _, _, _, field in ADMISSIONS_COLUMNS)))

    with get_db_connection() as conn:
        replace_table(
            conn, "mobility",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")]
            + [c[:3] for c in MOBILITY_COLUMNS],
            rows,
            "Opportunity Insights Mobility Report Cards: parent income and students' later earnings "
            "(students born 1980-82, earnings measured in 2014). Rates are decimals.",
            {**SOURCE, "year": "1980-82 birth cohorts"},
        )
        replace_table(
            conn, "mobility_admissions_by_income",
            [("school_id", "INTEGER NOT NULL", "IPEDS UNITID, joins to schools.id")]
            + [c[:3] for c in ADMISSIONS_COLUMNS],
            admissions,
            "Opportunity Insights (2023): application and attendance rates by parent income at "
            "139 highly selective colleges, students entering 2010-2015. Several rows per school.",
            {**SOURCE, "year": "2010-2015 entering classes"},
        )


if __name__ == "__main__":
    main()
