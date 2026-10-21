"""
Data for school profile pages, the compare tool and "I don't know" fallbacks.

Everything is read with application-written SQL from the latest year of schools plus the
source tables (IPEDS, Clery, CollegeCopilot Score, deadlines, reviews, photos).
"""

from database import execute_sql
from photos import get_photo
from school_lookup import resolve_school
from user_data import review_summary

CREDENTIAL_LABELS = {
    1: "Certificates", 2: "Associate's", 3: "Bachelor's", 4: "Post-bacc certificates",
    5: "Master's", 6: "Doctoral", 7: "Professional", 8: "Graduate certificates",
}
OWNERSHIP = {1: "Public", 2: "Private nonprofit", 3: "Private for-profit"}

BASICS_SQL = """
SELECT s.id, s.name, s.city, s.state, s.ownership, s.student_size, s.undergraduate_size,
       s.admission_rate, s.completion_rate, s.retention_rate, s.earnings_median_10yr,
       s.cost_avg_net_price, s.sat_average, s.school_url,
       p.website, p.locale, p.carnegie_basic, p.religious_affiliation, p.hbcu,
       c.tuition_fees_in_state, c.tuition_fees_out_of_state, c.room_board_on_campus_cost,
       e.student_faculty_ratio, e.total_enrollment,
       cs.copilot_score, cs.copilot_rank, cs.graduation_score, cs.earnings_score,
       cs.selectivity_score, cs.student_faculty_score,
       (SELECT count(*) FROM copilot_scores) AS scored_schools
FROM schools s
LEFT JOIN ipeds_profile p ON p.school_id = s.id
LEFT JOIN ipeds_costs c ON c.school_id = s.id
LEFT JOIN ipeds_enrollment e ON e.school_id = s.id
LEFT JOIN copilot_scores cs ON cs.school_id = s.id
WHERE s.id = ANY(%s) AND s.year = (SELECT max(year) FROM schools)
"""

SAFETY_SQL = """
SELECT criminal_offenses_total, criminal_offenses_per_1000_students, criminal_offenses_total_2023,
       criminal_offenses_total_2022, sex_offenses_total, robbery_count, aggravated_assault_count,
       burglary_count, motor_vehicle_theft_count, stalking_count, dating_violence_count,
       hate_crimes_count, liquor_referrals_count, drug_referrals_count
FROM campus_crime WHERE school_id = %s
"""


def _table_exists(name: str) -> bool:
    return execute_sql("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))[0]["ok"]


def _website(row: dict) -> str | None:
    url = row.get("website") or row.get("school_url")
    if not url:
        return None
    return url if url.startswith("http") else f"https://{url.rstrip('/')}"


def _shape(row: dict) -> dict:
    """Basics in display-ready groups."""
    score = None
    if row["copilot_score"] is not None:
        score = {
            "score": row["copilot_score"], "rank": row["copilot_rank"], "out_of": row["scored_schools"],
            "components": [
                {"label": "Graduation rate", "weight": 40, "score": row["graduation_score"]},
                {"label": "Median earnings", "weight": 30, "score": row["earnings_score"]},
                {"label": "Selectivity", "weight": 20, "score": row["selectivity_score"]},
                {"label": "Student-faculty ratio", "weight": 10, "score": row["student_faculty_score"]},
            ],
        }
    return {
        "id": row["id"], "name": row["name"], "city": row["city"], "state": row["state"],
        "type": OWNERSHIP.get(row["ownership"]), "website": _website(row), "url": f"/school/{row['id']}",
        "locale": row["locale"], "carnegie": row["carnegie_basic"], "religious_affiliation": row["religious_affiliation"],
        "hbcu": row["hbcu"], "score": score,
        "stats": {
            "graduation_rate": row["completion_rate"], "acceptance_rate": row["admission_rate"],
            "retention_rate": row["retention_rate"], "median_earnings": row["earnings_median_10yr"],
            "net_price": row["cost_avg_net_price"], "tuition_in_state": row["tuition_fees_in_state"],
            "tuition_out_of_state": row["tuition_fees_out_of_state"], "room_board": row["room_board_on_campus_cost"],
            "students": row["total_enrollment"] or row["student_size"], "undergraduates": row["undergraduate_size"],
            "student_faculty_ratio": row["student_faculty_ratio"], "sat_average": row["sat_average"],
        },
    }


def school_basics(ids: list[int]) -> list[dict]:
    rows = {r["id"]: _shape(r) for r in execute_sql(BASICS_SQL, (ids,))}
    return [rows[i] for i in ids if i in rows]


def school_profile(unitid: int, review_limit: int = 10) -> dict | None:
    found = school_basics([unitid])
    if not found:
        return None
    profile = found[0]
    programs = execute_sql(
        "SELECT credential_level, array_agg(DISTINCT program_name ORDER BY program_name) AS names "
        "FROM programs WHERE school_id = %s AND year = (SELECT max(year) FROM programs) "
        "AND program_name IS NOT NULL GROUP BY credential_level ORDER BY credential_level",
        (unitid,),
    )
    profile["majors"] = [
        {"level": CREDENTIAL_LABELS.get(p["credential_level"], "Other"), "programs": p["names"]}
        for p in programs
    ]
    safety = execute_sql(SAFETY_SQL, (unitid,))
    profile["safety"] = safety[0] if safety else None
    profile["deadlines"] = execute_sql(
        "SELECT deadline_type, deadline_date, notes, source_url, cycle FROM school_deadlines "
        "WHERE unitid = %s ORDER BY deadline_date NULLS LAST", (unitid,),
    ) if _table_exists("school_deadlines") else []
    profile["designations"] = [
        r["designation"] for r in execute_sql(
            "SELECT DISTINCT designation FROM school_designations WHERE school_id = %s ORDER BY 1", (unitid,))
    ] if _table_exists("school_designations") else []
    profile["reviews"] = review_summary(unitid, limit=review_limit)
    profile["photo"] = get_photo(unitid)
    return profile


def search_schools(query: str, limit: int = 8) -> list[dict]:
    """Name search for the compare tool (same matching as the planner)."""
    if len(query.strip()) < 2:
        return []
    return [
        {"id": c["id"], "name": c["name"], "city": c["city"], "state": c["state"]}
        for c in resolve_school(query, limit=limit)
    ]


# Compare tool: (stat key, label, format, better)
COMPARE_METRICS = [
    ("copilot_score", "CollegeCopilot Score", "score", "higher"),
    ("graduation_rate", "Graduation rate", "percent", "higher"),
    ("acceptance_rate", "Acceptance rate", "percent", "lower"),
    ("median_earnings", "Median earnings (10 yrs)", "money", "higher"),
    ("net_price", "Average net price", "money", "lower"),
    ("tuition_in_state", "In-state tuition", "money", "lower"),
    ("tuition_out_of_state", "Out-of-state tuition", "money", "lower"),
    ("room_board", "Room and board", "money", "lower"),
    ("retention_rate", "Retention rate", "percent", "higher"),
    ("student_faculty_ratio", "Student-faculty ratio", "ratio", "lower"),
    ("students", "Students", "int", None),
    ("crimes_per_1000", "Crimes per 1,000 students", "number", "lower"),
    ("rating", "Average review", "rating", "higher"),
]


def compare_schools(ids: list[int]) -> dict:
    schools = school_basics(ids)
    crime = {r["school_id"]: r["criminal_offenses_per_1000_students"] for r in execute_sql(
        "SELECT school_id, criminal_offenses_per_1000_students FROM campus_crime WHERE school_id = ANY(%s)", (ids,))}
    for school in schools:
        school["stats"]["copilot_score"] = school["score"]["score"] if school["score"] else None
        school["stats"]["crimes_per_1000"] = crime.get(school["id"])
        school["stats"]["rating"] = review_summary(school["id"], limit=0)["average"]
    rows = []
    for key, label, fmt, better in COMPARE_METRICS:
        values = [s["stats"].get(key) for s in schools]
        present = [float(v) for v in values if v is not None]
        best = worst = None
        if better and len(present) >= 2 and len(set(present)) > 1:
            best = max(present) if better == "higher" else min(present)
            worst = min(present) if better == "higher" else max(present)
        rows.append({
            "key": key, "label": label, "format": fmt, "better": better,
            "cells": [{"value": v, "best": v is not None and float(v) == best,
                       "worst": v is not None and float(v) == worst} for v in values],
        })
    return {"schools": [{k: s[k] for k in ("id", "name", "city", "state", "type", "url")} for s in schools],
            "rows": rows}
