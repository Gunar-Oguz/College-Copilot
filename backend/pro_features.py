"""
Pro features: application checklists, the student profile and "Why this school?" fit analysis,
and exports (deadline calendar .ics, saved-schools CSV).

Per-user tables (user_checklist_items, user_profiles) filter every query on the user id and use
row-level security on app.user_id as a second layer, like saved schools and notes.
"""

import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta, timezone  # noqa: F401

from psycopg2.extras import RealDictCursor

from database import execute_sql, get_db_connection
from presentation import DEADLINE_LABELS, unsupported_numbers

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_checklist_items (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    unitid INTEGER NOT NULL,
    item_text TEXT NOT NULL,
    done BOOLEAN NOT NULL DEFAULT FALSE,
    due_date DATE,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS user_checklist_user_school_idx ON user_checklist_items (user_id, unitid);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    gpa NUMERIC,
    sat INTEGER,
    act INTEGER,
    family_income INTEGER,
    budget INTEGER,
    intended_major TEXT,
    home_state TEXT,
    preferred_size TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

ALTER TABLE user_checklist_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_checklist_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON user_checklist_items;
CREATE POLICY own_rows ON user_checklist_items
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON user_profiles;
CREATE POLICY own_rows ON user_profiles
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
"""



def ensure_pro_tables() -> None:
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(SCHEMA_SQL)


def _run(user_id: str, sql: str, params: tuple = ()) -> list[dict]:
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()] if cursor.description else []
    finally:
        conn.close()


def _deadlines(unitids: list[int]) -> list[dict]:
    if not execute_sql("SELECT to_regclass('school_deadlines') IS NOT NULL AS ok")[0]["ok"]:
        return []
    return execute_sql(
        "SELECT d.unitid, s.name, d.deadline_type, d.deadline_date, d.notes, d.source_url, d.cycle "
        "FROM school_deadlines d JOIN schools s ON s.id = d.unitid AND s.year = (SELECT max(year) FROM schools) "
        "WHERE d.unitid = ANY(%s) ORDER BY d.deadline_date NULLS LAST", (unitids,))


# ---------- Application checklist ----------

def default_checklist(unitid: int) -> list[tuple[str, date | None]]:
    """Starting checklist from the school's own requirements and deadlines."""
    items: list[tuple[str, date | None]] = []
    for d in _deadlines([unitid]):
        label = DEADLINE_LABELS.get(d["deadline_type"], d["deadline_type"])
        if d["deadline_date"]:
            items.append((f"Submit application: {label} ({d['deadline_date']:%b %-d, %Y})", d["deadline_date"]))
        else:
            items.append((f"Submit application: {label} (apply early; spots fill up)", None))
    if not items:
        items.append(("Find the application deadline on the school's admissions site", None))
    items.append(("Request your high school transcript", None))

    policies = execute_sql(
        "SELECT test_score_policy, essay_policy, recommendations_policy FROM ipeds_admissions WHERE school_id = %s",
        (unitid,))
    policy = policies[0] if policies else {}
    tests = policy.get("test_score_policy")
    if tests == "Required":
        items.append(("Send SAT or ACT scores (required)", None))
    elif tests == "Considered but not required":
        items.append(("Decide whether to send SAT/ACT scores (test-optional)", None))
    if policy.get("essay_policy") in ("Required", "Considered but not required"):
        items.append((f"Write your personal essay ({policy['essay_policy'].lower()})", None))
    if policy.get("recommendations_policy") in ("Required", "Considered but not required"):
        items.append((f"Ask for recommendation letters ({policy['recommendations_policy'].lower()})", None))
    else:
        items.append(("Check whether recommendation letters are needed", None))

    items.append(("Pay the application fee or request a fee waiver", None))
    items.append(("Submit the FAFSA (check the school's financial aid priority date)", None))
    ownership = execute_sql("SELECT ownership FROM schools WHERE id = %s AND year = (SELECT max(year) FROM schools)",
                            (unitid,))
    if ownership and ownership[0]["ownership"] == 2:
        items.append(("Check whether the CSS Profile is required for financial aid", None))
    items.append(("Check your application portal for missing documents", None))
    return items


def get_checklist(user_id: str, unitid: int) -> list[dict]:
    rows = _run(user_id, "SELECT id, item_text, done, due_date, position FROM user_checklist_items "
                         "WHERE user_id = %s AND unitid = %s ORDER BY position, id", (user_id, unitid))
    if rows:
        return rows
    for position, (text, due) in enumerate(default_checklist(unitid)):
        _run(user_id, "INSERT INTO user_checklist_items (user_id, unitid, item_text, due_date, position) "
                      "VALUES (%s, %s, %s, %s, %s)", (user_id, unitid, text, due, position))
    return _run(user_id, "SELECT id, item_text, done, due_date, position FROM user_checklist_items "
                         "WHERE user_id = %s AND unitid = %s ORDER BY position, id", (user_id, unitid))


def add_checklist_item(user_id: str, unitid: int, text: str) -> dict:
    return _run(user_id, """
        INSERT INTO user_checklist_items (user_id, unitid, item_text, position)
        VALUES (%s, %s, %s, (SELECT coalesce(max(position), 0) + 1 FROM user_checklist_items
                             WHERE user_id = %s AND unitid = %s))
        RETURNING id, item_text, done, due_date, position
    """, (user_id, unitid, text, user_id, unitid))[0]


def update_checklist_item(user_id: str, item_id: int, text: str | None, done: bool | None) -> dict | None:
    rows = _run(user_id, """
        UPDATE user_checklist_items SET item_text = coalesce(%s, item_text), done = coalesce(%s, done)
        WHERE id = %s AND user_id = %s RETURNING id, item_text, done, due_date, position
    """, (text, done, item_id, user_id))
    return rows[0] if rows else None


def delete_checklist_item(user_id: str, item_id: int) -> bool:
    return bool(_run(user_id, "DELETE FROM user_checklist_items WHERE id = %s AND user_id = %s RETURNING id",
                     (item_id, user_id)))


def checklist_progress(user_id: str, unitids: list[int]) -> dict[int, dict]:
    rows = _run(user_id, "SELECT unitid, count(*) AS total, count(*) FILTER (WHERE done) AS done "
                         "FROM user_checklist_items WHERE user_id = %s AND unitid = ANY(%s) GROUP BY unitid",
                (user_id, unitids))
    return {r["unitid"]: {"total": r["total"], "done": r["done"]} for r in rows}


# ---------- Student profile ----------

PROFILE_FIELDS = ("gpa", "sat", "act", "family_income", "budget", "intended_major", "home_state", "preferred_size")


def get_profile(user_id: str) -> dict:
    rows = _run(user_id, f"SELECT {', '.join(PROFILE_FIELDS)} FROM user_profiles WHERE user_id = %s", (user_id,))
    return rows[0] if rows else {field: None for field in PROFILE_FIELDS}


def save_profile(user_id: str, profile: dict) -> dict:
    values = [profile.get(f) for f in PROFILE_FIELDS]
    _run(user_id, f"""
        INSERT INTO user_profiles (user_id, {', '.join(PROFILE_FIELDS)}, updated_at)
        VALUES (%s, {', '.join(['%s'] * len(PROFILE_FIELDS))}, NOW())
        ON CONFLICT (user_id) DO UPDATE SET {', '.join(f'{f} = EXCLUDED.{f}' for f in PROFILE_FIELDS)}, updated_at = NOW()
    """, (user_id, *values))  # noqa: E501
    return get_profile(user_id)


# ---------- "Why this school?" fit analysis ----------

INCOME_COLUMNS = [
    (30_000, "cost_net_price_income_0_30k"), (48_000, "cost_net_price_income_30_48k"),
    (75_000, "cost_net_price_income_48_75k"), (110_000, "cost_net_price_income_75_110k"),
    (float("inf"), "cost_net_price_income_110k_plus"),
]
SIZES = {"small": (0, 5000), "medium": (5000, 15000), "large": (15000, 10**7)}

FIT_SQL = """
SELECT s.id, s.name, s.state, s.ownership, s.admission_rate, s.completion_rate, s.earnings_median_10yr,
       s.cost_avg_net_price, s.student_size, s.cost_net_price_income_0_30k, s.cost_net_price_income_30_48k,
       s.cost_net_price_income_48_75k, s.cost_net_price_income_75_110k, s.cost_net_price_income_110k_plus,
       a.sat_reading_25th, a.sat_reading_75th, a.sat_math_25th, a.sat_math_75th,
       a.act_composite_25th, a.act_composite_75th, a.test_score_policy,
       c.tuition_fees_in_state, c.tuition_fees_out_of_state,
       cs.copilot_score, cs.copilot_rank
FROM schools s
LEFT JOIN ipeds_admissions a ON a.school_id = s.id
LEFT JOIN ipeds_costs c ON c.school_id = s.id
LEFT JOIN copilot_scores cs ON cs.school_id = s.id
WHERE s.id = %s AND s.year = (SELECT max(year) FROM schools)
"""


def _money(value) -> str:
    return f"${round(float(value)):,}"


def _pct(value) -> str:
    return f"{float(value) * 100:.1f}%"


def fit_facts(unitid: int, profile: dict) -> dict | None:
    """Deterministic fit checks: each is {area, status (good|okay|concern|unknown), detail}."""
    rows = execute_sql(FIT_SQL, (unitid,))
    if not rows:
        return None
    s = rows[0]
    checks = []

    # Academic fit and chances
    sat25 = (s["sat_reading_25th"] or 0) + (s["sat_math_25th"] or 0) if s["sat_math_25th"] else None
    sat75 = (s["sat_reading_75th"] or 0) + (s["sat_math_75th"] or 0) if s["sat_math_75th"] else None
    position = None
    if profile.get("sat") and sat25 and sat75:
        position = "below" if profile["sat"] < sat25 else "above" if profile["sat"] > sat75 else "within"
        detail = f"Your SAT {profile['sat']} is {position} the middle 50% of admitted students ({sat25}-{sat75})."
    elif profile.get("act") and s["act_composite_25th"] and s["act_composite_75th"]:
        lo, hi = s["act_composite_25th"], s["act_composite_75th"]
        position = "below" if profile["act"] < lo else "above" if profile["act"] > hi else "within"
        detail = f"Your ACT {profile['act']} is {position} the middle 50% of admitted students ({lo}-{hi})."
    else:
        detail = "Add an SAT or ACT score to compare with admitted students." if (sat25 or s["act_composite_25th"]) \
            else "This school doesn't report test score ranges."
    rate = float(s["admission_rate"]) if s["admission_rate"] else None
    if rate is not None and rate < 0.15:
        chance = "Reach"
        detail += f" With a {_pct(rate)} acceptance rate, it's a reach for every applicant."
    elif position == "below":
        chance = "Reach"
    elif position == "above" and (rate is None or rate >= 0.5):
        chance = "Likely"
    elif position:
        chance = "Target"
    else:
        chance = "Reach" if rate is not None and rate < 0.3 else "Target" if rate is not None else "Unknown"
        if rate is not None:
            detail += f" Its acceptance rate is {_pct(rate)}."
    checks.append({"area": "Admission chances", "status": {"Likely": "good", "Target": "okay", "Reach": "concern"}
                   .get(chance, "unknown"), "label": chance, "detail": detail})

    # Cost
    in_state = bool(profile.get("home_state")) and profile["home_state"].upper() == s["state"]
    price = None
    if profile.get("family_income") is not None:
        column = next(col for top, col in INCOME_COLUMNS if profile["family_income"] <= top)
        price = s[column]
    price = price if price is not None else s["cost_avg_net_price"]
    if price is None:
        checks.append({"area": "Cost", "status": "unknown", "detail": "This school doesn't report a net price."})
    else:
        basis = "for your family's income" if profile.get("family_income") is not None else "on average"
        detail = f"Net price {basis}: {_money(price)} a year."
        status = "okay"
        if profile.get("budget"):
            status = "good" if float(price) <= profile["budget"] else "concern"
            detail += f" Your budget is {_money(profile['budget'])}."
        if s["ownership"] == 1 and profile.get("home_state") and not in_state and s["tuition_fees_out_of_state"]:
            detail += (f" You'd likely pay out-of-state tuition ({_money(s['tuition_fees_out_of_state'])}), "
                       "so your price could be higher.")
            status = "concern" if status != "good" else "okay"
        checks.append({"area": "Cost", "status": status, "detail": detail})

    # Intended major
    major = (profile.get("intended_major") or "").strip()
    if major:
        words = [w for w in re.findall(r"[a-zA-Z]{3,}", major) if w.lower() not in {"and", "the", "studies"}]
        programs = execute_sql(
            "SELECT program_name, earnings_median_4yr FROM programs WHERE school_id = %s "
            "AND year = (SELECT max(year) FROM programs) AND credential_level = 3 AND "
            + " AND ".join(["program_name ILIKE %s"] * len(words)) + " ORDER BY earnings_median_4yr DESC NULLS LAST",
            (unitid, *[f"%{w}%" for w in words]),
        ) if words else []
        if programs:
            top = programs[0]
            detail = f"Offers a bachelor's in {top['program_name'].rstrip('.')}."
            if top["earnings_median_4yr"]:
                detail += f" Graduates earn a median {_money(top['earnings_median_4yr'])} four years later."
            checks.append({"area": "Your major", "status": "good", "detail": detail})
        else:
            checks.append({"area": "Your major", "status": "concern",
                           "detail": f"No bachelor's program matching \"{major}\" in the data. Check the school's catalog."})  # noqa: E501

    # Outcomes
    if s["completion_rate"] is not None or s["earnings_median_10yr"] is not None:
        parts = []
        if s["completion_rate"] is not None:
            parts.append(f"{_pct(s['completion_rate'])} of students graduate")
        if s["earnings_median_10yr"] is not None:
            parts.append(f"graduates earn a median {_money(s['earnings_median_10yr'])} after 10 years")
        grad = float(s["completion_rate"] or 0)
        checks.append({"area": "Outcomes", "status": "good" if grad >= 0.7 else "okay" if grad >= 0.5 else "concern",
                       "detail": "; ".join(parts).capitalize() + "."})

    # Size
    if profile.get("preferred_size") in SIZES and s["student_size"]:
        lo, hi = SIZES[profile["preferred_size"]]
        fits = lo <= s["student_size"] < hi
        checks.append({"area": "Size", "status": "good" if fits else "concern",
                       "detail": f"{s['student_size']:,} undergraduates; you prefer a {profile['preferred_size']} school."})  # noqa: E501

    return {
        "school": {"id": s["id"], "name": s["name"], "state": s["state"],
                   "copilot_score": s["copilot_score"], "copilot_rank": s["copilot_rank"]},
        "chance": chance,
        "checks": checks,
    }


FIT_PROMPT = """Write 2-3 sentences for a student explaining why this college could or couldn't be a good fit,
using ONLY the facts given. Be encouraging but honest; mention the biggest strength and the biggest concern.
Copy numbers exactly. No markdown, no lists, no outside knowledge."""


def fit_summary(facts: dict, client) -> str | None:
    """A short AI summary of the fit checks, or None if it cites numbers that aren't in them."""
    text = json.dumps(facts, default=str)
    message = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"), max_tokens=250, system=FIT_PROMPT,
        messages=[{"role": "user", "content": f"SCHOOL: {facts['school']['name']}\nFACTS: {text}"}],
    )
    summary = message.content[0].text.strip().split("\n\n")[0]
    bad = unsupported_numbers(summary, [], text)
    return None if bad else summary


# ---------- Exports ----------

def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545)."""
    out, current = [], ""
    for char in line:
        if len((current + char).encode()) > 75:
            out.append(current)
            current = " " + char
        else:
            current += char
    out.append(current)
    return "\r\n".join(out)


def deadlines_ics(unitids: list[int]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CollegeCopilot//Deadlines//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:College application deadlines"]
    for d in _deadlines(unitids):
        if not d["deadline_date"]:
            continue
        label = DEADLINE_LABELS.get(d["deadline_type"], d["deadline_type"])
        day = d["deadline_date"]
        description = f"{label} deadline for {d['name']}. {d['notes'] or ''} Always confirm on the school's site."
        if d["source_url"]:
            description += f" Source: {d['source_url']}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{d['unitid']}-{d['deadline_type']}-{day:%Y%m%d}@collegecopilot",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_ics_escape(d['name'] + ': ' + label + ' deadline')}",
            f"DESCRIPTION:{_ics_escape(description.strip())}",
        ]
        if d["source_url"]:
            lines.append(f"URL:{d['source_url']}")
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(d['name'] + ' deadline in one week')}",
                  "TRIGGER:-P7D", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"


def _csv_safe(value):
    """Stop spreadsheet formula injection from user-written notes."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def favorites_csv(favorites: list[dict], deadlines: list[dict], progress: dict[int, dict], origin: str) -> str:
    by_school: dict[int, list[str]] = {}
    for d in deadlines:
        label = DEADLINE_LABELS.get(d["deadline_type"], d["deadline_type"])
        by_school.setdefault(d["unitid"], []).append(
            f"{label}: {d['deadline_date']:%Y-%m-%d}" if d["deadline_date"] else label)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["School", "City", "State", "Acceptance rate", "Graduation rate", "Average net price",
                     "Median earnings (10 yrs)", "Students", "Deadlines", "Checklist", "Notes", "Profile"])
    for f in favorites:
        prog = progress.get(f["school_id"])
        writer.writerow([_csv_safe(v) for v in [
            f["name"], f["city"], f["state"],
            _pct(f["admission_rate"]) if f["admission_rate"] is not None else "",
            _pct(f["completion_rate"]) if f["completion_rate"] is not None else "",
            round(float(f["cost_avg_net_price"])) if f["cost_avg_net_price"] is not None else "",
            f["earnings_median_10yr"] or "",
            f["student_size"] or "",
            "; ".join(by_school.get(f["school_id"], [])),
            f"{prog['done']}/{prog['total']} done" if prog else "",
            " | ".join(n["note_text"] for n in f.get("notes", [])),
            f"{origin}/school/{f['school_id']}",
        ]])
    return out.getvalue()
