"""
Load the VA GI Bill Comparison Tool data into gi_bill (one row per school).

The VA publishes one row per "facility code" (a school's VA-approved location or
program, e.g. main campus, extension school, study abroad). Column `cross` is the
IPEDS UNITID. When several facility codes share a UNITID, the main one is the
VA-approved facility with the most GI Bill students; its flags and descriptive fields
are used. Student counts and Post-9/11 payments are also summed over all facility
codes of the school (the *_all_facilities columns and the post911 columns).

Usage (from backend/):
    python load_gi_bill.py
"""

from collections import defaultdict

import openpyxl
import requests
from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, num, replace_table

load_dotenv(ROOT_DIR / ".env")

URL = "https://www.benefits.va.gov/GIBILL/docs/job_aids/ComparisonToolData.xlsx"
PATH = DATA_DIR / "gi_bill" / "ComparisonToolData.xlsx"
SHEET = "Comparison_Tool_Data_Full"
SOURCE = {"name": "VA GI Bill Comparison Tool", "url": "https://www.va.gov/education/gi-bill-comparison-tool/"}
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def text(value):
    value = str(value).strip() if value is not None else ""
    return value or None


def boolean(value):
    """VA flags are True/False, 'Yes'/'No' or blank (blank = not reported)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "y", "1")


def flag_or_false(value):
    """Flags where blank means 'not a participant'."""
    return bool(boolean(value))


def integer(value):
    number = num(value)
    return None if number is None else round(number)


def money(value):
    number = num(value)
    return None if number is None else round(number, 2)


def rate(value):
    number = num(value)
    return None if number is None or number > 1 else round(number, 4)


def date(value):
    return value.date().isoformat() if hasattr(value, "date") else text(value)


# Fields taken from the main facility: (column, sql type, description, VA field, transform)
MAIN_COLUMNS = [
    ("facility_code", "TEXT", "VA facility code of the main facility used for this row", "facility code", text),
    ("va_institution_name", "TEXT", "Institution name in VA records (upper case)", "institution", text),
    ("va_school_type", "TEXT", "VA school type: 'PUBLIC', 'PRIVATE', 'FOR PROFIT', 'FOREIGN', ...", "type", text),
    ("va_approved", "BOOLEAN", "Main facility is currently approved by VA for GI Bill benefits", "approved", flag_or_false),
    ("approval_status", "TEXT", "VA approval status code: 'APRVD' approved, 'WTHDRN' withdrawn, 'SUSP' suspended", "poo status", text),
    ("gi_bill_students_count", "INTEGER", "GI Bill beneficiaries at the main facility in the last calendar year", "gibill", integer),
    ("yellow_ribbon", "BOOLEAN", "School is a Yellow Ribbon Program participant (covers tuition above the Post-9/11 GI Bill cap together with VA)", "yr", flag_or_false),
    ("monthly_housing_allowance_amount", "INTEGER", "Post-9/11 GI Bill monthly housing allowance in dollars for students attending in person at the main campus (based on military BAH for its ZIP code)", "bah", integer),
    ("student_veteran_group", "BOOLEAN", "Has a student veterans group (per VA)", "student veteran", flag_or_false),
    ("student_veteran_group_ipeds", "BOOLEAN", "Reports a student veterans organization to IPEDS", "student vet grp ipeds", boolean),
    ("principles_of_excellence", "BOOLEAN", "Signed the VA Principles of Excellence (consumer protections for veterans)", "poe", flag_or_false),
    ("eight_keys_to_veteran_success", "BOOLEAN", "Department of Education 'Eight Keys to Veteran Success' site", "eight keys", flag_or_false),
    ("dod_tuition_assistance_mou", "BOOLEAN", "Signed the DoD Tuition Assistance memorandum of understanding (active-duty members can use TA)", "dodmou", flag_or_false),
    ("in_state_tuition_for_veterans", "BOOLEAN", "Complies with Section 702 of the Veterans Choice Act: charges veterans and covered dependents in-state tuition (public schools)", "sec 702", boolean),
    ("credit_for_military_training", "BOOLEAN", "Awards academic credit for military training", "credit for mil training", boolean),
    ("veteran_point_of_contact", "BOOLEAN", "Has a dedicated point of contact for veterans", "vet poc", boolean),
    ("vetsuccess_on_campus", "BOOLEAN", "Has a VA VetSuccess on Campus counselor", "vetsuccess name", lambda v: v is not None and str(v).strip().lower() not in ("", "vacant")),
    ("soc_member", "BOOLEAN", "Member of Servicemembers Opportunity Colleges", "soc member", boolean),
    ("caution_flag", "BOOLEAN", "VA caution flag: something students should look into before enrolling (e.g. Heightened Cash Monitoring, accreditation action, government settlement)", "caution flag", flag_or_false),
    ("caution_flag_reason", "TEXT", "Reason(s) for the VA caution flag", "caution flag reason", text),
    ("accredited", "BOOLEAN", "Accredited by an accreditor recognized by the Department of Education (per VA)", "accredited", flag_or_false),
    ("accreditation_type", "TEXT", "Type of institutional accreditor: 'regional', 'national' or 'hybrid'", "accreditation type", text),
    ("accreditation_status", "TEXT", "Negative accreditation status if any (e.g. probation, warning, show cause); blank when in good standing", "accreditation status", text),
    ("school_closing", "BOOLEAN", "School is closing or has announced closure", "school closing", flag_or_false),
    ("school_closing_date", "TEXT", "Announced closing date (YYYY-MM-DD) when school_closing is true", "school closing on", date),
    ("complaints_count", "INTEGER", "Closed GI Bill feedback (complaints) cases against the main facility", "complaints facility code", integer),
    ("veteran_retention_rate", "NUMERIC", "Retention rate of student veterans in bachelor's programs (decimal)", "retention rate veteran ba", rate),
    ("veteran_graduation_rate", "NUMERIC", "Graduation rate of student veterans (decimal)", "graduation rate veteran", rate),
    ("veteran_transfer_out_rate", "NUMERIC", "Transfer-out rate of student veterans (decimal)", "transfer out rate veteran", rate),
    ("tuition_policy_url", "TEXT", "School web page with its veteran tuition policy", "vet tuition policy url", text),
]

# Summed over all facility codes of the school: (column, sql type, description, VA field)
SUM_COLUMNS = [
    ("gi_bill_students_all_facilities_count", "INTEGER", "GI Bill beneficiaries in the last calendar year across all of the school's VA facility codes (main campus, extension, online, ...)", "gibill"),
    ("post911_recipients_count", "INTEGER", "Students who received Post-9/11 GI Bill tuition payments in the last calendar year (all facilities)", "p911 recipients"),
    ("post911_tuition_fees_amount", "NUMERIC", "Post-9/11 GI Bill tuition and fees VA paid to the school in the last calendar year, in dollars (all facilities)", "p911 tuition fees"),
    ("yellow_ribbon_recipients_count", "INTEGER", "Students who received Yellow Ribbon funds in the last calendar year (all facilities)", "p911 yr recipients"),
    ("yellow_ribbon_payment_amount", "NUMERIC", "Yellow Ribbon payments VA paid to the school in the last calendar year, in dollars (VA's share; the school matches it) (all facilities)", "p911 yellow ribbon"),
]


def fetch() -> None:
    if not PATH.exists():
        PATH.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(URL, timeout=300, headers={"User-Agent": BROWSER_UA})
        response.raise_for_status()
        PATH.write_bytes(response.content)


def read_facilities() -> dict[int, list[dict]]:
    workbook = openpyxl.load_workbook(PATH, read_only=True)
    rows = workbook[SHEET].iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    by_unitid = defaultdict(list)
    for values in rows:
        if values[0] is None:
            continue
        row = dict(zip(header, values))
        unitid = integer(row.get("cross"))
        if unitid:
            by_unitid[unitid].append(row)
    workbook.close()
    return by_unitid


def main_facility(facilities: list[dict]) -> dict:
    return max(facilities, key=lambda r: (flag_or_false(r["approved"]), integer(r["gibill"]) or 0,
                                          -len(str(r["facility code"]))))


def total(facilities: list[dict], field: str, kind):
    values = [num(r.get(field)) for r in facilities]
    values = [v for v in values if v is not None]
    return kind(sum(values)) if values else None


def main():
    fetch()
    by_unitid = read_facilities()
    print(f"  {sum(map(len, by_unitid.values())):,} facility codes with a UNITID, {len(by_unitid):,} schools")

    rows = []
    for unitid, facilities in sorted(by_unitid.items()):
        main_row = main_facility(facilities)
        values = [transform(main_row.get(field)) for _, _, _, field, transform in MAIN_COLUMNS]
        values.append(len(facilities))
        values += [total(facilities, field, integer if sql == "INTEGER" else money)
                   for _, sql, _, field in SUM_COLUMNS]
        rows.append((unitid, *values))

    with get_db_connection() as conn:
        replace_table(
            conn, "gi_bill",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")]
            + [c[:3] for c in MAIN_COLUMNS]
            + [("va_facilities_count", "INTEGER", "Number of VA facility codes (campuses, extension or online divisions, programs) linked to this school")]
            + [c[:3] for c in SUM_COLUMNS],
            rows,
            "VA GI Bill Comparison Tool: GI Bill students, Yellow Ribbon participation and payments, "
            "housing allowance, veteran support programs and VA caution flags. Flags and descriptive "
            "fields come from the school's main VA facility (approved, most GI Bill students); "
            "student and payment totals cover all its facilities. Counts and payments are for "
            "calendar year 2025; rates are decimals.",
            {**SOURCE, "year": "CY 2025 counts and payments; flags as of Oct 2026"},
        )


if __name__ == "__main__":
    main()
