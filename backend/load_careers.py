"""
Load public career data: occupations (BLS wages and projections, O*NET descriptions), the
CIP-to-SOC crosswalk linking majors to occupations, and metro-area wages per school.

Tables:
  occupations      one row per SOC occupation (BLS OEWS May 2025 wages, BLS Employment
                   Projections 2025-35, O*NET 31.0 descriptions and job zones)
  cip_occupations  NCES CIP 2020 to SOC 2018 crosswalk (program -> occupation)
  area_wages       one row per school: all-occupation wages in the school's metro area (OEWS)

Join path from a school's programs to careers:
  programs.cip_code = cip_occupations.cip4_code AND cip_occupations.soc_code = occupations.soc_code

Files are downloaded to data/careers/ when missing. BLS requires a User-Agent with a contact.

Usage (from backend/):
    python load_careers.py
"""

import csv
import io
import zipfile

import openpyxl
import requests
from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, num, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

CAREERS_DIR = DATA_DIR / "careers"
USER_AGENT = "CollegeCopilot data loader (contact: collegecopilot@example.com)"

URLS = {
    "oews_national": "https://www.bls.gov/oes/special-requests/oesm25nat.zip",
    "oews_metro": "https://www.bls.gov/oes/special-requests/oesm25ma.zip",
    "projections": "https://www.bls.gov/emp/ind-occ-matrix/occupation.xlsx",
    "education": "https://www.bls.gov/emp/ind-occ-matrix/education.xlsx",
    "onet": "https://www.onetcenter.org/dl_files/database/db_31_0_text.zip",
    "crosswalk": "https://nces.ed.gov/ipeds/cipcode/Files/CIP2020_SOC2018_Crosswalk.xlsx",
}

OCCUPATIONS_SOURCE = {
    "name": "BLS Occupational Employment and Wage Statistics and Employment Projections; O*NET 31.0",
    "url": "https://www.bls.gov/oes/tables.htm",
    "year": "May 2025 wages; 2025-35 projections",
}
CROSSWALK_SOURCE = {
    "name": "NCES CIP 2020 to SOC 2018 Crosswalk",
    "url": "https://nces.ed.gov/ipeds/cipcode/resources.aspx?y=56",
    "year": "CIP 2020 / SOC 2018",
}
AREA_SOURCE = {
    "name": "BLS Occupational Employment and Wage Statistics (metro areas)",
    "url": "https://www.bls.gov/oes/tables.htm",
    "year": "May 2025",
}

TOP_CODED_SALARY = 239200  # older OEWS releases show '#' for wages at or above this amount

# Crosswalk SOC codes that OEWS and the projections publish only as part of a combined code.
COMBINED_SOC = {"21-1011": "21-1018", "21-1014": "21-1018", "25-2055": "25-2052", "25-2056": "25-2052",
                "25-9042": "25-9045", "25-9043": "25-9045", "25-9049": "25-9045", "51-2022": "51-2028",
                "51-2023": "51-2028", "53-1042": "53-1047", "53-1043": "53-1047", "53-1044": "53-1047",
                "53-1049": "53-1047"}


def fetch(key: str):
    """Download a source file into data/careers/ unless it is already there."""
    url = URLS[key]
    dest = CAREERS_DIR / url.rsplit("/", 1)[-1]
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading {url}")
        response = requests.get(url, timeout=300, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        dest.write_bytes(response.content)
    return dest


def xlsx_rows(source, sheet: str, header_row: int) -> list[dict]:
    """Rows of a worksheet as dicts keyed by the header on header_row (0-based)."""
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    rows = list(workbook[sheet].iter_rows(values_only=True))
    workbook.close()
    header = [str(h).strip() if h is not None else "" for h in rows[header_row]]
    return [dict(zip(header, row)) for row in rows[header_row + 1:] if any(v is not None for v in row)]


def zipped_xlsx_rows(path, member_suffix: str) -> list[dict]:
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(member_suffix))
        data = io.BytesIO(archive.read(name))
    return xlsx_rows(data, openpyxl.load_workbook(data, read_only=True).sheetnames[0], 0)


def salary(value):
    """OEWS annual wage: '#' is top-coded (none in May 2025), '*' and '**' are missing."""
    if isinstance(value, str) and value.strip() == "#":
        return TOP_CODED_SALARY
    return num(value)


def text(value):
    value = str(value).strip() if value is not None else ""
    return None if value in ("", "—", "-", "n/a") else value


def thousands(value):
    """Employment Projections counts are in thousands; store whole people."""
    number = num(value)
    return None if number is None else int(round(number * 1000))


def growth_share(value):
    """Percent change (may be negative) as a decimal share."""
    if value is None or value in ("", "—"):
        return None
    try:
        return round(float(value) / 100, 4)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------- occupations

OCCUPATION_COLUMNS = [
    ("soc_code", "TEXT PRIMARY KEY", "Standard Occupational Classification code 'NN-NNNN', e.g. '15-1252' = Software Developers, '29-1141' = Registered Nurses"),
    ("title", "TEXT", "Occupation title, e.g. 'Software Developers'"),
    ("major_group", "TEXT", "SOC major group title, e.g. 'Computer and Mathematical Occupations', 'Healthcare Practitioners and Technical Occupations'"),
    ("description", "TEXT", "What workers in this occupation do (O*NET 31.0)"),
    ("employment_total", "INTEGER", "U.S. wage-and-salary employment, May 2025 (BLS OEWS; excludes self-employed)"),
    ("median_annual_salary", "INTEGER", "Median annual wage in dollars, May 2025 (BLS OEWS); null for performers paid only hourly (actors, dancers, musicians)"),
    ("mean_annual_salary", "INTEGER", "Mean (average) annual wage in dollars, May 2025 (BLS OEWS)"),
    ("pct10_annual_salary", "INTEGER", "10th percentile annual wage in dollars, May 2025 (BLS OEWS)"),
    ("pct25_annual_salary", "INTEGER", "25th percentile annual wage in dollars, May 2025 (BLS OEWS)"),
    ("pct75_annual_salary", "INTEGER", "75th percentile annual wage in dollars, May 2025 (BLS OEWS)"),
    ("pct90_annual_salary", "INTEGER", "90th percentile annual wage in dollars, May 2025 (BLS OEWS)"),
    ("projected_employment_2035_total", "INTEGER", "Projected employment in 2035 including self-employed (BLS Employment Projections)"),
    ("projected_growth_rate", "NUMERIC", "Projected employment change 2025-35 as a decimal, e.g. 0.41 = +41%; negative = decline (BLS Employment Projections; all-occupation average is 0.035)"),
    ("annual_openings_count", "INTEGER", "Projected average annual job openings 2025-35, from growth plus replacement of workers who leave (BLS Employment Projections)"),
    ("self_employed_share", "NUMERIC", "Share of workers who are self-employed, 2025 (BLS Employment Projections)"),
    ("entry_education", "TEXT", "Typical education needed for entry: 'Doctoral or professional degree', \"Master's degree\", \"Bachelor's degree\", \"Associate's degree\", 'Postsecondary nondegree award', 'Some college, no degree', 'High school diploma or equivalent', 'No formal educational credential'"),
    ("work_experience", "TEXT", "Work experience in a related occupation needed: 'None', 'Less than 5 years', '5 years or more'"),
    ("on_the_job_training", "TEXT", "Typical on-the-job training needed, e.g. 'None', 'Short-term on-the-job training', 'Internship/residency', 'Apprenticeship'"),
    ("job_zone", "INTEGER", "O*NET Job Zone 1-5 = preparation needed: 1 little or none, 2 some, 3 medium, 4 considerable (usually a bachelor's), 5 extensive (usually graduate degree)"),
]


def load_occupations():
    oews = zipped_xlsx_rows(fetch("oews_national"), "national_M2025_dl.xlsx")
    oews = {r["OCC_CODE"]: r for r in oews if str(r.get("OWN_CODE")) == "1235" and r.get("I_GROUP") == "cross-industry"}
    major_titles = {code[:2]: r["OCC_TITLE"] for code, r in oews.items() if r["O_GROUP"] == "major"}

    projections = {r["2025 National Employment Matrix code"]: r
                   for r in xlsx_rows(fetch("projections"), "Table 1.2", 1)
                   if r.get("Occupation type") == "Line item"}
    training = {r["2025 National Employment Matrix code"]: r
                for r in xlsx_rows(fetch("education"), "Table 5.4", 1)}

    with zipfile.ZipFile(fetch("onet")) as archive:
        def onet(member):
            raw = archive.read(f"db_31_0_text/{member}").decode("utf-8")
            return list(csv.DictReader(io.StringIO(raw), delimiter="\t"))
        onet_data, job_zones = onet("Occupation Data.txt"), onet("Job Zones.txt")
    # Map O*NET-SOC 'XX-XXXX.NN' to SOC 'XX-XXXX', preferring the base '.00' occupation.
    descriptions, zones = {}, {}
    for row in sorted(onet_data, key=lambda r: r["O*NET-SOC Code"]):
        descriptions.setdefault(row["O*NET-SOC Code"][:7], row["Description"])
    for row in sorted(job_zones, key=lambda r: r["O*NET-SOC Code"]):
        zones.setdefault(row["O*NET-SOC Code"][:7], num(row["Job Zone"]))

    # Detailed OEWS occupations plus projection line items OEWS publishes only at a broader level.
    codes = sorted({c for c, r in oews.items() if r["O_GROUP"] == "detailed"} | set(projections))
    rows = []
    for code in codes:
        wage = oews.get(code, {})
        proj = projections.get(code, {})
        train = training.get(code, {}) or proj
        title = wage.get("OCC_TITLE") or text(proj.get("2025 National Employment Matrix title"))
        wages = [salary(wage.get(f)) for f in ("A_MEDIAN", "A_MEAN", "A_PCT10", "A_PCT25", "A_PCT75", "A_PCT90")]
        if not wage:  # fall back to the projections' median wage
            wages[0] = num(proj.get("Median annual wage, dollars, 2025"))
        self_employed = num(proj.get("Percent self-employed, 2025"))
        rows.append((
            code, title, major_titles.get(code[:2]), descriptions.get(code),
            num(wage.get("TOT_EMP")), *wages,
            thousands(proj.get("Employment, 2035")),
            growth_share(proj.get("Employment change, percent, 2025–35")),
            thousands(proj.get("Occupational openings, 2025–35 annual average")),
            None if self_employed is None else round(self_employed / 100, 4),
            text(train.get("Typical education needed for entry")),
            text(train.get("Work experience in a related occupation")),
            text(train.get("Typical on-the-job training needed to attain competency in the occupation")),
            zones.get(code),
        ))
    return rows


# ------------------------------------------------------------------------- crosswalk

CROSSWALK_COLUMNS = [
    ("soc_code", "TEXT", "Occupation code 'NN-NNNN'; join to occupations.soc_code. Where BLS publishes only a combined code (e.g. 13-1020 Buyers and Purchasing Agents) this is the combined code"),
    ("cip_code", "TEXT", "6-digit CIP 2020 program code 'NN.NNNN', e.g. '11.0701' = Computer Science"),
    ("cip4_code", "TEXT", "4-digit CIP family 'NN.NN'; join to programs.cip_code, e.g. '11.07' = Computer Science"),
    ("cip_title", "TEXT", "CIP 2020 program title, e.g. 'Computer Science.'"),
    ("soc_title", "TEXT", "SOC 2018 detailed occupation title from the crosswalk (may be narrower than occupations.title)"),
]


def load_crosswalk(occupation_codes: set[str]):
    rows = set()
    for row in xlsx_rows(fetch("crosswalk"), "CIP-SOC", 0):
        cip, soc = text(row["CIP2020Code"]), text(row["SOC2018Code"])
        if not cip or not soc or row["SOC2018Title"] == "NO MATCH" or soc == "99-9999":
            continue
        broad = soc[:6] + "0"
        if soc not in occupation_codes:
            soc = COMBINED_SOC.get(soc) or (broad if broad in occupation_codes else soc)
        rows.add((soc, cip, cip[:5], text(row["CIP2020Title"]), text(row["SOC2018Title"])))
    return sorted(rows)


# ------------------------------------------------------------------------- area wages

AREA_COLUMNS = [
    ("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID; join to schools.id"),
    ("metro_code", "TEXT", "Core Based Statistical Area (metro area) code"),
    ("metro_name", "TEXT", "Metropolitan statistical area containing the school, e.g. 'San Jose-Sunnyvale-Santa Clara, CA'"),
    ("employment_total", "INTEGER", "Wage-and-salary jobs in the metro area, all occupations, May 2025"),
    ("median_annual_salary", "INTEGER", "Median annual wage in dollars for all occupations in the metro area, May 2025 (U.S. median is 50980)"),
    ("mean_annual_salary", "INTEGER", "Mean annual wage in dollars for all occupations in the metro area, May 2025 (U.S. mean is 69770)"),
]


def load_area_wages():
    with zipfile.ZipFile(fetch("oews_metro")) as archive:
        name = next(n for n in archive.namelist() if n.endswith("MSA_M2025_dl.xlsx"))
        data = io.BytesIO(archive.read(name))
    workbook = openpyxl.load_workbook(data, read_only=True, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    metros, header = {}, None
    for row in sheet.iter_rows(values_only=True):
        if header is None:
            header = [str(h).strip() for h in row]
            occ = header.index("OCC_CODE")
            continue
        if row[occ] == "00-0000":
            r = dict(zip(header, row))
            metros[str(r["AREA"]).strip()] = r
    workbook.close()

    hd = read_csv(DATA_DIR / "ipeds" / "HD2024.zip")
    rows = []
    for school in hd:
        metro = metros.get(school.get("CBSA", ""))
        if metro:
            rows.append((int(school["UNITID"]), school["CBSA"], metro["AREA_TITLE"], num(metro["TOT_EMP"]),
                         salary(metro["A_MEDIAN"]), salary(metro["A_MEAN"])))
    print(f"  {len(metros)} metro areas")
    return rows


def main():
    conn = get_db_connection()
    try:
        occupations = load_occupations()
        replace_table(
            conn, "occupations", OCCUPATION_COLUMNS, occupations,
            "Careers: one row per occupation (NOT per school). Wages from BLS OEWS May 2025 (national, all "
            "industries), projections from BLS Employment Projections 2025-35. Link programs to occupations "
            "with programs.cip_code = cip_occupations.cip4_code AND cip_occupations.soc_code = occupations.soc_code. "
            "Includes information from O*NET 31.0 by the U.S. Department of Labor, Employment and Training "
            "Administration (USDOL/ETA), used under the CC BY 4.0 license.",
            OCCUPATIONS_SOURCE,
        )
        replace_table(
            conn, "cip_occupations", CROSSWALK_COLUMNS, load_crosswalk({row[0] for row in occupations}),
            "Crosswalk from majors (CIP 2020) to the occupations they prepare for (SOC 2018), many-to-many; "
            "NOT one row per school. Join: programs.cip_code = cip_occupations.cip4_code AND "
            "cip_occupations.soc_code = occupations.soc_code. A 4-digit CIP family maps to several 6-digit "
            "CIPs, so use SELECT DISTINCT soc_code (or GROUP BY) to avoid duplicate occupations.",
            CROSSWALK_SOURCE,
        )
        with conn.cursor() as cursor:
            cursor.execute("CREATE INDEX ON cip_occupations (cip4_code)")
            cursor.execute("CREATE INDEX ON cip_occupations (soc_code)")
        conn.commit()
        replace_table(
            conn, "area_wages", AREA_COLUMNS, load_area_wages(),
            "Local job market: all-occupation wages in the metropolitan area around each school (BLS OEWS "
            "May 2025). One row per school; schools outside a metro area are missing.",
            AREA_SOURCE,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
