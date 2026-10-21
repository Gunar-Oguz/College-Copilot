"""
Load NSF NCSES Higher Education Research and Development (HERD) Survey FY2024 data into
research_spending, one row per school.

The HERD file is long format (one row per institution x question x row x column) with
values in thousands of dollars; they are converted to dollars here. Totals by funding
source come from question 1, totals by field from questions 9 (federal) + 11 (nonfederal).

Some HERD respondents have no IPEDS UNITID in the file (mostly multi-campus reporters such
as "Ohio State University" or "Johns Hopkins University"). UNITID_OVERRIDES maps them to the
main campus; where HERD reports a combined unit (e.g. University of Maryland = College Park
+ Baltimore, Vanderbilt + its medical center) the whole amount goes to the named campus,
and units mapping to the same UNITID are summed. Research institutes without a UNITID
(Scripps Research, Van Andel, ...) are skipped but still count for the national rank.

Usage (from backend/):
    python load_research.py
"""

from collections import defaultdict

from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, download, num, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

URL = "https://ncses.nsf.gov/821/assets/0/files/higher_education_r_and_d_2024.zip"
ZIP_PATH = DATA_DIR / "research" / "higher_education_r_and_d_2024.zip"
SOURCE = {"name": "NSF NCSES Higher Education Research and Development (HERD) Survey",
          "url": "https://ncses.nsf.gov/surveys/higher-education-research-development", "year": "FY2024"}

UNITID_OVERRIDES = {
    "Johns Hopkins University": 162928,
    "Ohio State University, The": 204796,
    "University of Maryland": 163286,
    "Texas A&M University, College Station and Health Science Center": 228723,
    "Vanderbilt University and Vanderbilt University Medical Center": 221999,
    "University of Colorado Anschutz Medical Campus": 126562,
    "University of Colorado Denver": 126562,
    "University of Cincinnati": 201885,
    "University of Nebraska, Lincoln and Medical Center": 181464,
    "University of Connecticut": 129020,
    "Oregon State University": 209542,
    "University of New Hampshire": 183044,
    "University of Mississippi": 176017,
    "William & Mary": 231624,
    "Ohio University": 204857,
    "Western Michigan University and Homer Stryker M.D. School of Medicine": 172699,
    "Kent State University": 203517,
    "Embry-Riddle Aeronautical University": 133553,
    "Miami University": 204024,
    "University of Akron, The": 200800,
    "Wright State University": 206604,
    "Bowling Green State University": 201441,
    "Midwestern University": 143853,
    "City University of New York, The, Advanced Science Research Center": 190576,
}

SOURCES = [  # (question 1 item, column, description)
    ("01.a", "rd_federal_amount", "R&D expenditures funded by the federal government, dollars"),
    ("01.b", "rd_state_local_amount", "R&D expenditures funded by state and local government, dollars"),
    ("01.c", "rd_business_amount", "R&D expenditures funded by business, dollars"),
    ("01.d", "rd_nonprofit_amount", "R&D expenditures funded by nonprofit organizations, dollars"),
    ("01.e", "rd_institution_amount", "R&D expenditures funded by the institution's own funds, dollars"),
    ("01.f", "rd_other_sources_amount", "R&D expenditures funded by all other sources, dollars"),
]
FIELDS = [  # (HERD field label, column suffix)
    ("Computer and information sciences", "computer_sciences"),
    ("Engineering", "engineering"),
    ("Geosciences, atmospheric sciences, and ocean sciences", "geosciences"),
    ("Life sciences", "life_sciences"),
    ("Mathematics and statistics", "math"),
    ("Physical sciences", "physical_sciences"),
    ("Psychology", "psychology"),
    ("Social sciences", "social_sciences"),
    ("Other sciences", "other_sciences"),
    ("Non-S&E", "non_science"),
]
FIELD_NOTES = {
    "life_sciences": " (biological, biomedical, health, agricultural sciences)",
    "geosciences": " (incl. atmospheric and ocean sciences)",
    "non_science": " (non-science & engineering: business, education, humanities, law, arts, etc.)",
}

COLUMNS = (
    [("herd_name", "TEXT", "Institution name in HERD (may be a combined unit, e.g. a university plus its medical center)"),
     ("rd_total_amount", "BIGINT", "Total research & development expenditures in FY2024, all sources, dollars"),
     ("rd_national_rank", "INTEGER", "National rank by total R&D expenditures among all HERD institutions (1 = largest)")]
    + [(col, "BIGINT", desc) for _, col, desc in SOURCES]
    + [("rd_federal_share", "NUMERIC", "Share of R&D funded by the federal government (decimal)")]
    + [(f"rd_{suffix}_amount", "BIGINT", f"R&D expenditures in {label.lower().replace('non-s&e', 'non-S&E fields')}{FIELD_NOTES.get(suffix, '')}, all sources, dollars")
       for label, suffix in FIELDS]
    + [("rd_medical_school_amount", "BIGINT", "R&D expenditures at the medical school, dollars (null if none reported)"),
       ("rd_basic_research_amount", "BIGINT", "R&D expenditures on basic research, dollars"),
       ("rd_applied_research_amount", "BIGINT", "R&D expenditures on applied research, dollars"),
       ("rd_personnel_count", "INTEGER", "Headcount of R&D personnel paid from R&D funds (researchers, technicians, support staff)"),
       ("rd_researchers_count", "INTEGER", "Headcount of researchers paid from R&D funds")]
)


def main():
    download(URL, ZIP_PATH)
    units = defaultdict(lambda: {"values": defaultdict(float)})
    for row in read_csv(ZIP_PATH):
        unit = units[row["INST_ID"]]
        unit["name"] = row["INST_NAME_LONG"]
        unit["unitid"] = int(row["IPEDS_UNITID"]) if row["IPEDS_UNITID"].isdigit() else UNITID_OVERRIDES.get(row["INST_NAME_LONG"])
        value = num(row["DATA"])
        if value is None:
            continue
        q, label, column = row["QUESTIONNAIRE_NO"], row["ROW"], row["COLUMN"]
        values = unit["values"]
        if q.startswith("01.") and q != "01.1":
            values[q] += value * 1000
        elif q[:2] in ("09", "11") and label.endswith(", all") and column == "Total":
            values["field:" + label[:-len(", all")]] += value * 1000
        elif q == "04" and label == "Total":
            values["medical"] += value * 1000
        elif q in ("06.a", "06.b") and column == "Total":
            values[q] += value * 1000
        elif q == "15" and label == "Total" and column in ("Total", "Researchers"):
            values["people:" + column] += value

    # Combine units mapping to the same UNITID; rank everything (including unmapped units) by total.
    schools = defaultdict(lambda: {"names": [], "values": defaultdict(float)})
    for inst_id, unit in units.items():
        key = unit["unitid"] or f"herd:{inst_id}"
        schools[key]["names"].append(unit["name"])
        for k, v in unit["values"].items():
            schools[key]["values"][k] += v
    ranked = sorted(schools, key=lambda k: -schools[k]["values"].get("01.g", 0))
    rank = {key: i + 1 for i, key in enumerate(ranked)}

    def get(values, key):
        return round(values[key]) if key in values else None

    rows, skipped = [], []
    for key, school in schools.items():
        if isinstance(key, str):
            skipped.append(school["names"][0])
            continue
        values = school["values"]
        total = get(values, "01.g")
        federal = get(values, "01.a")
        rows.append((
            key, " + ".join(school["names"]), total, rank[key],
            *(get(values, q) for q, _, _ in SOURCES),
            round(federal / total, 4) if federal is not None and total else None,
            *(get(values, "field:" + label) for label, _ in FIELDS),
            get(values, "medical"), get(values, "06.a"), get(values, "06.b"),
            get(values, "people:Total"), get(values, "people:Researchers"),
        ))
    print(f"  {len(skipped)} HERD units without a UNITID skipped: {', '.join(sorted(skipped))}")

    with get_db_connection() as conn:
        replace_table(
            conn, "research_spending",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")] + COLUMNS,
            rows,
            "University research & development spending, FY2024 (NSF HERD Survey), one row per school; "
            "covers about 680 research-active institutions with at least $1M of R&D. Amounts are in "
            "dollars (converted from the survey's thousands). Some rows combine a university with its "
            "medical center or sister campus; see herd_name.",
            SOURCE,
        )


if __name__ == "__main__":
    main()
