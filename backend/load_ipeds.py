"""
Load IPEDS (nces.ed.gov) data into ipeds_* tables, one row per school (school_id = UNITID).

Tables: ipeds_profile, ipeds_admissions, ipeds_enrollment, ipeds_costs, ipeds_finance,
ipeds_faculty, ipeds_outcomes. Rates are stored as decimals (0.25 = 25%), like the
College Scorecard columns. Files are downloaded to data/ipeds/ when missing.

Usage (from backend/):
    python load_ipeds.py
"""

from dotenv import load_dotenv

import ipeds_labels as labels
from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, download, flag, num, pct, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

IPEDS_DIR = DATA_DIR / "ipeds"
BASE_URL = "https://nces.ed.gov/ipeds/datacenter/data"
SOURCE = {"name": "IPEDS, National Center for Education Statistics",
          "url": "https://nces.ed.gov/ipeds/datacenter/DataFiles.aspx"}

FILES = ["HD2024", "IC2024", "IC2023_AY", "ADM2023", "DRVADM2023", "EF2023A", "DRVEF2023",
         "DRVIC2023", "DRVF2023", "F2223_F1A", "F2223_F2", "DRVHR2023", "S2023_IS",
         "DRVGR2023", "DRVOM2023"]

ADMISSION_LABELS = {1: "Required", 3: "Not considered", 5: "Considered but not required"}


def label(codes: dict):
    return lambda value: codes.get(num(value))


def text(value):
    return value or None


def load_files() -> dict[str, list[dict]]:
    files = {}
    for name in FILES:
        path = download(f"{BASE_URL}/{name}.zip", IPEDS_DIR / f"{name}.zip")
        files[name] = read_csv(path)
        print(f"  read {name}: {len(files[name]):,} rows")
    return files


def by_unitid(rows: list[dict], **where) -> dict[int, dict]:
    return {
        int(r["UNITID"]): r for r in rows
        if all(r.get(k) == str(v) for k, v in where.items())
    }


def finance_value(row: dict, field: str):
    """DRVF fields exist as F1 (public), F2 (nonprofit) and F3 (for-profit) variants."""
    for prefix in ("F1", "F2", "F3"):
        value = num(row.get(prefix + field))
        if value is not None:
            return value
    return None


# Each table: (name, description, columns). A column is
# (name, sql type, description, file, field, transform) or (name, sql type, description, function).
TABLES = [
    ("ipeds_profile", "IPEDS institutional characteristics (2024-25 directory and offerings)", [
        ("website", "TEXT", "Institution website", "HD2024", "WEBADDR", text),
        ("opeid", "TEXT", "8-digit Office of Postsecondary Education ID", "HD2024", "OPEID", text),
        ("county", "TEXT", "County name", "HD2024", "COUNTYNM", text),
        ("locale", "TEXT", "Urbanicity, e.g. 'City: Large', 'Suburb: Midsize', 'Rural: Remote'", "HD2024", "LOCALE", label(labels.LOCALE)),
        ("carnegie_basic", "TEXT", "Carnegie Classification 2021 basic, e.g. 'Doctoral Universities: Very High Research Activity'", "HD2024", "C21BASIC", label(labels.C21BASIC)),
        ("carnegie_research", "TEXT", "Carnegie 2025 research designation, e.g. 'Research 1: ...'", "HD2024", "CARNEGIERSCH", label(labels.CARNEGIERSCH)),
        ("institution_size", "TEXT", "Enrollment size category, e.g. '20,000 and above'", "HD2024", "INSTSIZE", label(labels.INSTSIZE)),
        ("hbcu", "BOOLEAN", "Historically Black College or University", "HD2024", "HBCU", flag),
        ("tribal_college", "BOOLEAN", "Tribal college", "HD2024", "TRIBAL", flag),
        ("land_grant", "BOOLEAN", "Land-grant institution", "HD2024", "LANDGRNT", flag),
        ("has_hospital", "BOOLEAN", "Institution has a hospital", "HD2024", "HOSPITAL", flag),
        ("grants_medical_degree", "BOOLEAN", "Grants a medical degree (MD or DO)", "HD2024", "MEDICAL", flag),
        ("religious_affiliation", "TEXT", "Religious affiliation, e.g. 'Roman Catholic'; null if none", "IC2024", "RELAFFIL", label(labels.RELAFFIL)),
        ("calendar_system", "TEXT", "Academic calendar, e.g. 'Semester', 'Quarter'", "IC2024", "CALSYS", label(labels.CALSYS)),
        ("open_admission", "BOOLEAN", "Open admission policy (admits all applicants)", "IC2024", "OPENADMP", flag),
        ("ncaa_member", "BOOLEAN", "Member of the NCAA", "IC2024", "ASSOC1", flag),
        ("naia_member", "BOOLEAN", "Member of the NAIA", "IC2024", "ASSOC2", flag),
        ("njcaa_member", "BOOLEAN", "Member of the NJCAA (junior colleges)", "IC2024", "ASSOC3", flag),
        ("rotc", "BOOLEAN", "Offers ROTC", "IC2024", "SLO5", flag),
        ("rotc_army", "BOOLEAN", "Offers Army ROTC", "IC2024", "SLO51", flag),
        ("rotc_navy", "BOOLEAN", "Offers Navy ROTC", "IC2024", "SLO52", flag),
        ("rotc_air_force", "BOOLEAN", "Offers Air Force ROTC", "IC2024", "SLO53", flag),
        ("study_abroad", "BOOLEAN", "Offers study abroad", "IC2024", "SLO6", flag),
        ("undergrad_research", "BOOLEAN", "Offers co-curricular undergraduate research", "IC2024", "SLOA", flag),
        ("weekend_evening_college", "BOOLEAN", "Offers weekend/evening college", "IC2024", "SLO7", flag),
        ("on_campus_daycare", "BOOLEAN", "On-campus day care for students' children", "IC2024", "STUSRV8", flag),
        ("yellow_ribbon", "BOOLEAN", "Participates in the GI Bill Yellow Ribbon Program", "IC2024", "VET1", flag),
        ("credit_for_military_training", "BOOLEAN", "Gives credit for military training", "IC2024", "VET2", flag),
        ("veteran_support_contact", "BOOLEAN", "Dedicated point of contact for veterans and military families", "IC2024", "VET3", flag),
        ("student_veteran_org", "BOOLEAN", "Recognized student veteran organization", "IC2024", "VET4", flag),
        ("fully_online", "BOOLEAN", "All programs offered completely via distance education", "IC2024", "DISTNCED", flag),
        ("disability_registered_share", "NUMERIC", "Share of undergraduates registered with disability services (reported only when above 3%)", "IC2024", "DISABPCT", pct),
        ("net_price_calculator_url", "TEXT", "Net price calculator web address", "HD2024", "NPRICURL", text),
        ("veterans_url", "TEXT", "Veterans tuition policies web address", "HD2024", "VETURL", text),
        ("athlete_grad_rate_url", "TEXT", "Student-athlete graduation rate web address", "HD2024", "ATHURL", text),
    ]),
    ("ipeds_admissions", "IPEDS fall 2023 admissions: applicants, admits, test scores and requirements", [
        ("applicants_total", "INTEGER", "Applicants", "ADM2023", "APPLCN", num),
        ("applicants_men", "INTEGER", "Applicants who are men", "ADM2023", "APPLCNM", num),
        ("applicants_women", "INTEGER", "Applicants who are women", "ADM2023", "APPLCNW", num),
        ("admitted_total", "INTEGER", "Admitted applicants", "ADM2023", "ADMSSN", num),
        ("enrolled_first_year_total", "INTEGER", "Admitted students who enrolled", "ADM2023", "ENRLT", num),
        ("admit_rate", "NUMERIC", "Share of applicants admitted", "DRVADM2023", "DVADM01", pct),
        ("admit_rate_men", "NUMERIC", "Admit rate for men", "DRVADM2023", "DVADM02", pct),
        ("admit_rate_women", "NUMERIC", "Admit rate for women", "DRVADM2023", "DVADM03", pct),
        ("yield_rate", "NUMERIC", "Share of admitted students who enrolled", "DRVADM2023", "DVADM04", pct),
        ("sat_submit_rate", "NUMERIC", "Share of first-year students who submitted SAT scores", "ADM2023", "SATPCT", pct),
        ("act_submit_rate", "NUMERIC", "Share of first-year students who submitted ACT scores", "ADM2023", "ACTPCT", pct),
        ("sat_reading_25th", "INTEGER", "SAT reading/writing 25th percentile", "ADM2023", "SATVR25", num),
        ("sat_reading_75th", "INTEGER", "SAT reading/writing 75th percentile", "ADM2023", "SATVR75", num),
        ("sat_math_25th", "INTEGER", "SAT math 25th percentile", "ADM2023", "SATMT25", num),
        ("sat_math_75th", "INTEGER", "SAT math 75th percentile", "ADM2023", "SATMT75", num),
        ("act_composite_25th", "INTEGER", "ACT composite 25th percentile", "ADM2023", "ACTCM25", num),
        ("act_composite_75th", "INTEGER", "ACT composite 75th percentile", "ADM2023", "ACTCM75", num),
        ("test_score_policy", "TEXT", "Admission test scores: 'Required', 'Considered but not required' (test-optional) or 'Not considered' (test-blind)", "ADM2023", "ADMCON7", label(ADMISSION_LABELS)),
        ("hs_gpa_policy", "TEXT", "High school GPA: Required / Considered but not required / Not considered", "ADM2023", "ADMCON1", label(ADMISSION_LABELS)),
        ("class_rank_policy", "TEXT", "High school class rank: Required / Considered but not required / Not considered", "ADM2023", "ADMCON2", label(ADMISSION_LABELS)),
        ("recommendations_policy", "TEXT", "Recommendations: Required / Considered but not required / Not considered", "ADM2023", "ADMCON5", label(ADMISSION_LABELS)),
        ("essay_policy", "TEXT", "Personal statement or essay: Required / Considered but not required / Not considered", "ADM2023", "ADMCON11", label(ADMISSION_LABELS)),
        ("legacy_policy", "TEXT", "Legacy status: 'Considered but not required' means legacy is considered; 'Not considered' means no legacy preference", "ADM2023", "ADMCON12", label(ADMISSION_LABELS)),
    ]),
    ("ipeds_enrollment", "IPEDS fall 2023 enrollment by level, race/ethnicity, gender and residence", [
        ("total_enrollment", "INTEGER", "All students", "DRVEF2023", "ENRTOT", num),
        ("fte_enrollment", "INTEGER", "Full-time equivalent enrollment", "DRVEF2023", "FTE", num),
        ("undergrad_total", "INTEGER", "Undergraduate students", "DRVEF2023", "EFUG", num),
        ("grad_total", "INTEGER", "Graduate students", "DRVEF2023", "EFGRAD", num),
        ("first_time_undergrad_total", "INTEGER", "First-time undergraduates (new freshmen)", "DRVEF2023", "EFUG1ST", num),
        ("transfer_in_total", "INTEGER", "Transfer-in undergraduates", "DRVEF2023", "EFUGTRN", num),
        ("men_total", "INTEGER", "Students who are men", "EF2023A:1", "EFTOTLM", num),
        ("women_total", "INTEGER", "Students who are women", "EF2023A:1", "EFTOTLW", num),
        ("undergrad_men_total", "INTEGER", "Undergraduates who are men", "EF2023A:2", "EFTOTLM", num),
        ("undergrad_women_total", "INTEGER", "Undergraduates who are women", "EF2023A:2", "EFTOTLW", num),
        ("women_share", "NUMERIC", "Share of all students who are women", "DRVEF2023", "PCTENRW", pct),
        ("undergrad_women_share", "NUMERIC", "Share of undergraduates who are women", "DRVEF2023", "PCUENRW", pct),
        ("grad_women_share", "NUMERIC", "Share of graduate students who are women", "DRVEF2023", "PCGENRW", pct),
        ("undergrad_white_share", "NUMERIC", "Share of undergraduates who are White", "DRVEF2023", "PCUENRWH", pct),
        ("undergrad_black_share", "NUMERIC", "Share of undergraduates who are Black or African American", "DRVEF2023", "PCUENRBK", pct),
        ("undergrad_hispanic_share", "NUMERIC", "Share of undergraduates who are Hispanic/Latino", "DRVEF2023", "PCUENRHS", pct),
        ("undergrad_asian_share", "NUMERIC", "Share of undergraduates who are Asian", "DRVEF2023", "PCUENRAS", pct),
        ("undergrad_pacific_islander_share", "NUMERIC", "Share of undergraduates who are Native Hawaiian or Pacific Islander", "DRVEF2023", "PCUENRNH", pct),
        ("undergrad_native_american_share", "NUMERIC", "Share of undergraduates who are American Indian or Alaska Native", "DRVEF2023", "PCUENRAN", pct),
        ("undergrad_multiracial_share", "NUMERIC", "Share of undergraduates of two or more races", "DRVEF2023", "PCUENR2M", pct),
        ("undergrad_international_share", "NUMERIC", "Share of undergraduates who are U.S. nonresidents (international)", "DRVEF2023", "PCUENRNR", pct),
        ("undergrad_race_unknown_share", "NUMERIC", "Share of undergraduates with race/ethnicity unknown", "DRVEF2023", "PCUENRUN", pct),
        ("grad_white_share", "NUMERIC", "Share of graduate students who are White", "DRVEF2023", "PCGENRWH", pct),
        ("grad_black_share", "NUMERIC", "Share of graduate students who are Black", "DRVEF2023", "PCGENRBK", pct),
        ("grad_hispanic_share", "NUMERIC", "Share of graduate students who are Hispanic/Latino", "DRVEF2023", "PCGENRHS", pct),
        ("grad_asian_share", "NUMERIC", "Share of graduate students who are Asian", "DRVEF2023", "PCGENRAS", pct),
        ("grad_international_share", "NUMERIC", "Share of graduate students who are international", "DRVEF2023", "PCGENRNR", pct),
        ("first_year_in_state_share", "NUMERIC", "Share of first-time undergraduates from the same state", "DRVEF2023", "RMINSTTP", pct),
        ("first_year_out_of_state_share", "NUMERIC", "Share of first-time undergraduates from other states", "DRVEF2023", "RMOUSTTP", pct),
        ("first_year_foreign_share", "NUMERIC", "Share of first-time undergraduates from foreign countries", "DRVEF2023", "RMFRGNCP", pct),
        ("undergrad_age_25_plus_share", "NUMERIC", "Share of undergraduates aged 25-64", "DRVEF2023", "DVEF15", pct),
        ("online_only_share", "NUMERIC", "Share of students enrolled exclusively in distance education", "DRVEF2023", "PCTDEEXC", pct),
        ("full_time_retention_rate", "NUMERIC", "Full-time first-year retention rate (fall 2022 to fall 2023)", "DRVEF2023", "RET_PCF", pct),
        ("part_time_retention_rate", "NUMERIC", "Part-time first-year retention rate", "DRVEF2023", "RET_PCP", pct),
        ("student_faculty_ratio", "NUMERIC", "Students per faculty member, e.g. 18 means 18:1", "DRVEF2023", "STUFACR", num),
    ]),
    ("ipeds_costs", "IPEDS published prices for 2023-24 (sticker prices before aid)", [
        ("tuition_fees_in_state", "INTEGER", "In-state tuition and fees, 2023-24", "IC2023_AY", "CHG2AY3", num),
        ("tuition_fees_out_of_state", "INTEGER", "Out-of-state tuition and fees, 2023-24", "IC2023_AY", "CHG3AY3", num),
        ("tuition_fees_in_district", "INTEGER", "In-district tuition and fees (community colleges), 2023-24", "IC2023_AY", "CHG1AY3", num),
        ("tuition_fees_2020_21", "INTEGER", "Tuition and fees (in-state for publics), 2020-21", "DRVIC2023", "TUFEYR0", num),
        ("tuition_fees_2022_23", "INTEGER", "Tuition and fees (in-state for publics), 2022-23", "DRVIC2023", "TUFEYR2", num),
        ("books_supplies_cost", "INTEGER", "Books and supplies, 2023-24", "IC2023_AY", "CHG4AY3", num),
        ("room_board_on_campus_cost", "INTEGER", "On-campus food and housing (room and board), 2023-24", "IC2023_AY", "CHG5AY3", num),
        ("room_board_off_campus_cost", "INTEGER", "Off-campus (not with family) food and housing, 2023-24", "IC2023_AY", "CHG7AY3", num),
        ("other_expenses_on_campus_cost", "INTEGER", "Other expenses for on-campus students, 2023-24", "IC2023_AY", "CHG6AY3", num),
        ("total_price_in_state_on_campus", "INTEGER", "Total cost of attendance, in-state, living on campus, 2023-24", "DRVIC2023", "CINSON", num),
        ("total_price_out_of_state_on_campus", "INTEGER", "Total cost of attendance, out-of-state, living on campus, 2023-24", "DRVIC2023", "COTSON", num),
        ("total_price_in_state_off_campus", "INTEGER", "Total cost of attendance, in-state, off campus not with family", "DRVIC2023", "CINSOFF", num),
        ("total_price_in_state_with_family", "INTEGER", "Total cost of attendance, in-state, living with family", "DRVIC2023", "CINSFAM", num),
        ("grad_tuition_in_state", "INTEGER", "Graduate tuition, in-state, full-time, 2023-24", "IC2023_AY", "TUITION6", num),
        ("grad_tuition_out_of_state", "INTEGER", "Graduate tuition, out-of-state, full-time, 2023-24", "IC2023_AY", "TUITION7", num),
        ("medical_school_tuition_in_state", "INTEGER", "Medicine (MD) tuition, in-state", "IC2023_AY", "ISPROF3", num),
        ("medical_school_tuition_out_of_state", "INTEGER", "Medicine (MD) tuition, out-of-state", "IC2023_AY", "OSPROF3", num),
        ("law_school_tuition_in_state", "INTEGER", "Law (JD) tuition, in-state", "IC2023_AY", "ISPROF9", num),
        ("law_school_tuition_out_of_state", "INTEGER", "Law (JD) tuition, out-of-state", "IC2023_AY", "OSPROF9", num),
        ("dental_school_tuition_in_state", "INTEGER", "Dentistry tuition, in-state", "IC2023_AY", "ISPROF2", num),
        ("pharmacy_school_tuition_in_state", "INTEGER", "Pharmacy tuition, in-state", "IC2023_AY", "ISPROF6", num),
        ("vet_school_tuition_in_state", "INTEGER", "Veterinary medicine tuition, in-state", "IC2023_AY", "ISPROF8", num),
    ]),
    ("ipeds_finance", "IPEDS finances for fiscal year 2023 (public: GASB, private: FASB)", [
        ("endowment_amount", "BIGINT", "Endowment assets at end of fiscal year 2023, dollars", lambda f, u: num(f["F2223_F1A"].get(u, {}).get("F1H02")) or num(f["F2223_F2"].get(u, {}).get("F2H02"))),
        ("endowment_per_student_amount", "INTEGER", "Endowment assets per full-time-equivalent student", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "ENDMFT")),
        ("core_revenue_amount", "BIGINT", "Core revenues, dollars", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "CORREV")),
        ("core_expenses_amount", "BIGINT", "Core expenses, dollars", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "COREXP")),
        ("tuition_revenue_share", "NUMERIC", "Tuition and fees as a share of core revenues", lambda f, u: pct(finance_value(f["DRVF2023"].get(u, {}), "TUFEPC"))),
        ("state_appropriations_share", "NUMERIC", "State appropriations as a share of core revenues (public schools)", "DRVF2023", "F1STAPPC", pct),
        ("government_grants_share", "NUMERIC", "Government grants and contracts as a share of core revenues", lambda f, u: pct(finance_value(f["DRVF2023"].get(u, {}), "GVGCPC"))),
        ("private_gifts_share", "NUMERIC", "Private gifts, grants and contracts as a share of core revenues", lambda f, u: pct(finance_value(f["DRVF2023"].get(u, {}), "PGGCPC"))),
        ("instruction_spending_per_student_amount", "INTEGER", "Instruction expenses per FTE student", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "INSTFT")),
        ("research_spending_per_student_amount", "INTEGER", "Research expenses per FTE student", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "RSRCFT")),
        ("student_services_spending_per_student_amount", "INTEGER", "Student services expenses per FTE student", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "STSVFT")),
        ("academic_support_spending_per_student_amount", "INTEGER", "Academic support expenses per FTE student", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "ACSPFT")),
        ("admin_spending_per_student_amount", "INTEGER", "Institutional support (administration) expenses per FTE student", lambda f, u: finance_value(f["DRVF2023"].get(u, {}), "INSUFT")),
        ("instruction_spending_share", "NUMERIC", "Instruction as a share of core expenses", lambda f, u: pct(finance_value(f["DRVF2023"].get(u, {}), "INSTPC"))),
    ]),
    ("ipeds_faculty", "IPEDS fall 2023 instructional staff, salaries and staffing", [
        ("full_time_faculty_total", "INTEGER", "Full-time instructional staff", "S2023_IS:1", "HRTOTLT", num),
        ("full_time_faculty_women_total", "INTEGER", "Full-time instructional staff who are women", "S2023_IS:1", "HRTOTLW", num),
        ("tenured_faculty_total", "INTEGER", "Full-time instructional staff with tenure", "S2023_IS:200", "HRTOTLT", num),
        ("tenure_track_faculty_total", "INTEGER", "Full-time instructional staff on the tenure track", "S2023_IS:300", "HRTOTLT", num),
        ("avg_salary_all_ranks", "INTEGER", "Average 9-month salary, full-time instructional staff", "DRVHR2023", "SALTOTL", num),
        ("avg_salary_professor", "INTEGER", "Average 9-month salary, professors", "DRVHR2023", "SALPROF", num),
        ("avg_salary_associate_professor", "INTEGER", "Average 9-month salary, associate professors", "DRVHR2023", "SALASSC", num),
        ("avg_salary_assistant_professor", "INTEGER", "Average 9-month salary, assistant professors", "DRVHR2023", "SALASST", num),
        ("avg_salary_lecturer", "INTEGER", "Average 9-month salary, lecturers", "DRVHR2023", "SALLECT", num),
        ("staff_fte_total", "INTEGER", "All staff, full-time equivalent", "DRVHR2023", "SFTETOTL", num),
        ("instructional_staff_fte_total", "INTEGER", "Instructional staff, full-time equivalent", "DRVHR2023", "SFTEINST", num),
        ("research_staff_fte_total", "INTEGER", "Research staff, full-time equivalent", "DRVHR2023", "SFTERSRC", num),
        ("management_staff_fte_total", "INTEGER", "Management staff, full-time equivalent", "DRVHR2023", "SFTEMNGM", num),
    ]),
    ("ipeds_outcomes", "IPEDS graduation rates (2017 cohort, 150% of normal time) and 8-year outcome measures", [
        ("grad_rate_total", "NUMERIC", "Graduation rate within 150% of normal time, all students", "DRVGR2023", "GRRTTOT", pct),
        ("grad_rate_men", "NUMERIC", "Graduation rate, men", "DRVGR2023", "GRRTM", pct),
        ("grad_rate_women", "NUMERIC", "Graduation rate, women", "DRVGR2023", "GRRTW", pct),
        ("grad_rate_white", "NUMERIC", "Graduation rate, White students", "DRVGR2023", "GRRTWH", pct),
        ("grad_rate_black", "NUMERIC", "Graduation rate, Black students", "DRVGR2023", "GRRTBK", pct),
        ("grad_rate_hispanic", "NUMERIC", "Graduation rate, Hispanic students", "DRVGR2023", "GRRTHS", pct),
        ("grad_rate_asian", "NUMERIC", "Graduation rate, Asian students", "DRVGR2023", "GRRTAS", pct),
        ("grad_rate_native_american", "NUMERIC", "Graduation rate, American Indian or Alaska Native students", "DRVGR2023", "GRRTAN", pct),
        ("grad_rate_multiracial", "NUMERIC", "Graduation rate, students of two or more races", "DRVGR2023", "GRRT2M", pct),
        ("grad_rate_international", "NUMERIC", "Graduation rate, international students", "DRVGR2023", "GRRTNR", pct),
        ("grad_rate_pell", "NUMERIC", "Graduation rate, Pell Grant recipients", "DRVGR2023", "PGGRRTT", pct),
        ("grad_rate_no_pell_no_loan", "NUMERIC", "Graduation rate, students with neither Pell Grants nor subsidized loans", "DRVGR2023", "NRGRRTT", pct),
        ("transfer_out_rate", "NUMERIC", "Share of the cohort that transferred out", "DRVGR2023", "TRRTTOT", pct),
        ("bachelor_grad_rate_4yr", "NUMERIC", "Bachelor's degree within 4 years", "DRVGR2023", "GBA4RTT", pct),
        ("bachelor_grad_rate_5yr", "NUMERIC", "Bachelor's degree within 5 years", "DRVGR2023", "GBA5RTT", pct),
        ("bachelor_grad_rate_6yr", "NUMERIC", "Bachelor's degree within 6 years", "DRVGR2023", "GBA6RTT", pct),
        ("award_rate_8yr", "NUMERIC", "Full-time first-time students who earned any award within 8 years", "DRVOM2023", "OM1TOTLAWDP8", pct),
        ("award_rate_8yr_pell", "NUMERIC", "Full-time first-time Pell recipients who earned an award within 8 years", "DRVOM2023", "OM1PELLAWDP8", pct),
        ("award_rate_8yr_part_time", "NUMERIC", "Part-time first-time students who earned an award within 8 years", "DRVOM2023", "OM2TOTLAWDP8", pct),
        ("award_rate_8yr_transfer_in", "NUMERIC", "Full-time transfer-in students who earned an award within 8 years", "DRVOM2023", "OM3TOTLAWDP8", pct),
    ]),
]


def index_files(raw: dict[str, list[dict]]) -> dict[str, dict[int, dict]]:
    indexed = {name: by_unitid(rows) for name, rows in raw.items() if name not in ("EF2023A", "S2023_IS")}
    for level in (1, 2):
        indexed[f"EF2023A:{level}"] = by_unitid(raw["EF2023A"], EFALEVEL=level)
    for category in (1, 200, 300):
        indexed[f"S2023_IS:{category}"] = by_unitid(raw["S2023_IS"], SISCAT=category)
    return indexed


def build_rows(files: dict[str, dict[int, dict]], columns: list[tuple]) -> list[tuple]:
    unitids = set()
    for column in columns:
        if len(column) == 6:
            unitids |= files[column[3]].keys()
        else:
            unitids |= files["DRVF2023"].keys() | files["F2223_F1A"].keys() | files["F2223_F2"].keys()
    rows = []
    for unitid in sorted(unitids):
        values = []
        for column in columns:
            if len(column) == 6:
                _, _, _, file, field, transform = column
                values.append(transform(files[file].get(unitid, {}).get(field)))
            else:
                values.append(column[3](files, unitid))
        if any(v is not None for v in values):
            rows.append((unitid, *values))
    return rows


def main():
    print("Reading IPEDS files...")
    files = index_files(load_files())
    with get_db_connection() as conn:
        for table, description, columns in TABLES:
            rows = build_rows(files, columns)
            replace_table(
                conn, table,
                [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")]
                + [(c[0], c[1], c[2]) for c in columns],
                rows, description, {**SOURCE, "year": "2023-24"},
            )


if __name__ == "__main__":
    main()
