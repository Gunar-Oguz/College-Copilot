"""
Load accreditation from the Department of Education's DAPIP database
(Database of Accredited Postsecondary Institutions and Programs, ope.ed.gov/dapip).

- accreditation: one row per school, its current institutional accreditor(s), status,
  dates and the latest accreditor action (e.g. renewal, probation, warning).
- program_accreditation: one row per currently accredited program at a school
  (e.g. law, medicine, nursing, engineering, from specialized accreditors).

DAPIP lists institutions and their additional locations; IpedsUnitIds may hold several
UNITIDs. Records of a location without its own UNITID are attributed to its parent
institution. Only current accreditations are kept (status Accredited or Preaccredited
and no end date).

Usage (from backend/):
    python load_accreditation.py
"""

from collections import defaultdict
from datetime import datetime

import requests
from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

URL = "https://ope.ed.gov/dapip/api/downloadFiles/accreditationDataFiles"
PATH = DATA_DIR / "accreditation" / "dapip.zip"
SOURCE = {"name": "DAPIP, U.S. Department of Education", "url": "https://ope.ed.gov/dapip/"}
INSTITUTIONAL = "Institutional Accreditation"
CURRENT = ("Accredited", "Preaccredited")

# Actions that are routine paperwork rather than a change in standing.
ROUTINE_ACTIONS = ("Grant Substantive Change", "Deny Substantive Change", "Additional Location",
                   "Approved for Distance Education", "Deferral of Action")
SANCTIONS = ("Probation", "Warning", "Adverse Action")  # prefixes; show cause is "Probation or ...: Show Cause"


def fetch() -> None:
    if not PATH.exists():
        PATH.parent.mkdir(parents=True, exist_ok=True)
        response = requests.post(URL, json={"CSVChecked": True, "ExcelChecked": False}, timeout=300,
                                 headers={"User-Agent": "CollegeCopilot data loader"})
        response.raise_for_status()
        PATH.write_bytes(response.content)


def parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value.split()[0], "%m/%d/%Y").date()
    except ValueError:
        return None


def unitids_by_dapip(campuses: list[dict]) -> dict[str, list[int]]:
    """UNITIDs for each DAPIP id; locations without one get their parent's."""
    own = {}
    for row in campuses:
        ids = [int(x) for x in row["IPEDSUNITIDS"].replace(";", ",").split(",") if x.strip().isdigit()]
        own[row["DAPIPID"]] = ids
    mapping = {}
    for row in campuses:
        mapping[row["DAPIPID"]] = own[row["DAPIPID"]] or own.get(row["PARENTDAPIPID"], [])
    return mapping


def is_current(record: dict) -> bool:
    return record["ACCREDITATIONSTATUS"] in CURRENT and not record["ACCREDITATIONENDDATE"]


def joined(values) -> str | None:
    values = sorted({v for v in values if v})
    return "; ".join(values) or None


def institution_rows(records, actions, campuses, by_dapip) -> list[tuple]:
    institution_ids = {r["DAPIPID"] for r in campuses if r["LOCATIONTYPE"] == "Institution"}
    accreditors = defaultdict(list)  # unitid -> current institutional records
    for record in records:
        if record["PROGRAMNAME"] == INSTITUTIONAL and is_current(record):
            for unitid in by_dapip.get(record["DAPIPID"], []):
                accreditors[unitid].append(record)

    # Latest institutional actions per (DAPIP id, agency), on the institution itself.
    actions_by_key = defaultdict(list)
    for action in actions:
        if action["PROGRAMNAME"] == INSTITUTIONAL and action["DAPIPID"] in institution_ids:
            date = parse_date(action["ACTIONDATE"])
            if date:
                actions_by_key[(action["DAPIPID"], action["AGENCYID"])].append((date, action))

    rows = []
    for unitid, recs in sorted(accreditors.items()):
        school_actions = sorted(
            {(d, a["ACTIONDESCRIPTION"]) for r in recs for d, a in actions_by_key.get((r["DAPIPID"], r["AGENCYID"]), [])},
            reverse=True,
        )
        status_actions = [(d, a) for d, a in school_actions if not a.startswith(ROUTINE_ACTIONS)]
        latest = school_actions[0] if school_actions else (None, None)
        latest_status = status_actions[0] if status_actions else (None, None)
        since = [parse_date(r["ACCREDITATIONDATE"]) for r in recs]
        reviews = [parse_date(r["REVIEWDATE"]) for r in recs]
        rows.append((
            unitid,
            joined(r["AGENCYNAME"] for r in recs),
            len({r["AGENCYID"] for r in recs}),
            "Accredited" if any(r["ACCREDITATIONSTATUS"] == "Accredited" for r in recs) else "Preaccredited",
            min((d for d in since if d), default=None),
            min((d for d in reviews if d), default=None),
            latest[1], latest[0],
            latest_status[1], latest_status[0],
            bool(latest_status[1]) and latest_status[1].startswith(SANCTIONS),
        ))
    return rows


def program_rows(records, by_dapip) -> list[tuple]:
    programs = {}
    for record in records:
        if record["PROGRAMNAME"] == INSTITUTIONAL or not is_current(record):
            continue
        for unitid in by_dapip.get(record["DAPIPID"], []):
            key = (unitid, record["AGENCYNAME"], record["PROGRAMNAME"], record["DEPARTMENTDESCRIPTION"] or None)
            since = parse_date(record["ACCREDITATIONDATE"])
            review = parse_date(record["REVIEWDATE"])
            previous = programs.get(key)
            if previous:  # same program listed twice (e.g. at two locations): keep earliest/soonest dates
                since = min(filter(None, (since, previous[1])), default=None)
                review = min(filter(None, (review, previous[2])), default=None)
            programs[key] = (record["ACCREDITATIONSTATUS"], since, review)
    return [(*key, *value) for key, value in sorted(programs.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2], kv[0][3] or ""))]


def main():
    fetch()
    campuses = read_csv(PATH, "InstitutionCampus.csv")
    records = read_csv(PATH, "AccreditationRecords.csv")
    actions = read_csv(PATH, "AccreditationActions.csv")
    by_dapip = unitids_by_dapip(campuses)
    year = f"as of {max(filter(None, (parse_date(a['ACTIONDATE']) for a in actions))):%b %Y}"

    with get_db_connection() as conn:
        replace_table(
            conn, "accreditation",
            [
                ("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id"),
                ("institutional_accreditor", "TEXT", "Current institutional accreditor(s), e.g. 'WASC Senior College and University Commission', 'Higher Learning Commission'; several are joined with '; '"),
                ("institutional_accreditors_count", "INTEGER", "Number of current institutional accreditors (usually 1)"),
                ("accreditation_status", "TEXT", "'Accredited' or 'Preaccredited' (candidate for accreditation)"),
                ("accredited_since", "DATE", "Date of the earliest current institutional accreditation (some old dates are estimates)"),
                ("next_review_date", "DATE", "Next scheduled comprehensive review by the institutional accreditor (may be in the past if not updated)"),
                ("latest_action", "TEXT", "Most recent action by the institutional accreditor, e.g. 'Renewal of Accreditation', 'Grant Substantive Change: Program', 'Probation or Equivalent or a More Severe Status: Probation'"),
                ("latest_action_date", "DATE", "Date of latest_action"),
                ("latest_standing_action", "TEXT", "Most recent action that affects standing (ignores routine substantive-change and location approvals), e.g. 'Renewal of Accreditation', 'Removal of Monitoring Status', 'Probation or Equivalent or a More Severe Status: Warning'"),
                ("latest_standing_action_date", "DATE", "Date of latest_standing_action"),
                ("on_probation_or_warning", "BOOLEAN", "True when latest_standing_action is a sanction (probation, warning, show cause or adverse action) not yet followed by a renewal or removal. 'Heightened Monitoring or Focused Review' alone does not count"),
            ],
            institution_rows(records, actions, campuses, by_dapip),
            "Institutional accreditation from the Department of Education's DAPIP database: current "
            "accreditor, status, next review and latest accreditor actions. Only schools with a current "
            "institutional accreditation are included.",
            {**SOURCE, "year": year},
        )
        replace_table(
            conn, "program_accreditation",
            [
                ("school_id", "INTEGER NOT NULL", "IPEDS UNITID, joins to schools.id"),
                ("accreditor", "TEXT", "Specialized accrediting agency, e.g. 'American Bar Association, Council of the Section of Legal Education and Admissions to the Bar', 'Liaison Committee on Medical Education', 'Commission on Collegiate Nursing Education'"),
                ("program_name", "TEXT", "Accredited program type, e.g. 'Law (LAW) - Professional schools', 'Medicine (MED) - Programs leading to the M.D. degree', 'Nursing (CNURED) - Nursing education programs at the baccalaureate degree levels'. Use ILIKE to search"),
                ("department", "TEXT", "School or department offering the program, when given (e.g. 'UCLA School of Dentistry Predoctoral Dental Education')"),
                ("accreditation_status", "TEXT", "'Accredited' or 'Preaccredited'"),
                ("accredited_since", "DATE", "Date the program was first accredited (some old dates are estimates)"),
                ("next_review_date", "DATE", "Next scheduled review of the program (may be in the past if not updated)"),
            ],
            program_rows(records, by_dapip),
            "Currently accredited programs at each school from specialized accreditors in the "
            "Department of Education's DAPIP database (law, medicine, nursing, dentistry, psychology, "
            "teacher education, health professions, ...; ABET engineering and AACSB business are not in DAPIP). Several rows per school; programs at a school's branch locations are "
            "listed under the parent school.",
            {**SOURCE, "year": year},
        )


if __name__ == "__main__":
    main()
