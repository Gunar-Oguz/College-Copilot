"""
End-to-end answer checks: each question goes through the full pipeline (planner, guardrails,
summary) and the result is checked for the behavior a counselor would expect.

This complements evals/run_eval.py (exact answers vs. ground-truth SQL). It calls Claude, so it
takes a minute or two and costs a few cents. Nothing is written to query_logs.

Usage (from the repo root or backend/):
    python backend/evals.py               # all cases
    python backend/evals.py safety        # one category or case id
"""

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import execute_sql  # noqa: E402
from answering import answer_question  # noqa: E402

WORKERS = 4
UCLA, BERKELEY, USC, STANFORD, MIT, HARVARD, YALE, MICHIGAN = (
    110662, 110635, 123961, 243744, 166683, 166027, 130794, 170976)


# ---------- checks: each returns (passed, reason) ----------

def _num(value):
    return float(value) if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) else None


def _ids(rows):
    return [r.get("id", r.get("school_id")) for r in rows]


def _school_rows(rows):
    return [r for r in rows if r.get("role") != "reference"]


def kind(expected):
    def check(resp, rows):
        return resp.kind == expected, f"kind was {resp.kind}: {resp.response[:120]}"
    return check


def names_in_top(names, n):
    """Each name appears in some text column of the top n rows (column names may vary)."""
    def check(resp, rows):
        top = [" | ".join(str(v) for v in r.values() if isinstance(v, str)).lower() for r in rows[:n]]
        missing = [x for x in names if not any(x.lower() in t for t in top)]
        return not missing, f"{missing} not in top {n}: {top}"
    return check


def sorted_by(columns, order):
    """Rows are sorted by the first result column matching one of these names or regexes."""
    def check(resp, rows):
        keys = list(rows[0]) if rows else []
        column = next((k for c in columns for k in keys if k == c or re.fullmatch(c, k)), None)
        if not column:
            return False, f"none of {columns} in result columns {list(rows[0]) if rows else []}"
        values = [_num(r[column]) for r in _school_rows(rows) if _num(r.get(column)) is not None]
        ok = values == sorted(values, reverse=(order == "desc")) and len(values) >= 1
        return ok, f"{column} not sorted {order}: {values[:8]}"
    return check


def has_column(*columns):
    def check(resp, rows):
        keys = list(rows[0]) if rows else []
        ok = any(k == c or re.fullmatch(c, k) for c in columns for k in keys)
        return ok, f"none of {columns} in {keys}"
    return check


def row_count(minimum, maximum=None):
    def check(resp, rows):
        ok = len(rows) >= minimum and (maximum is None or len(rows) <= maximum)
        return ok, f"{len(rows)} rows, expected {minimum}-{maximum or 'any'}"
    return check


def includes_ids(*ids):
    def check(resp, rows):
        missing = set(ids) - set(_ids(rows))
        return not missing, f"missing school ids {sorted(missing)}"
    return check


def all_schools_where(condition, label):
    """Every school in the result satisfies an SQL condition on the latest schools row."""
    def check(resp, rows):
        ids = [i for i in _ids(_school_rows(rows)) if isinstance(i, int)]
        if not ids:
            return False, "no school ids in result"
        bad = execute_sql(
            f"SELECT id, name FROM schools WHERE id = ANY(%s) AND year = (SELECT max(year) FROM schools) "
            f"AND NOT ({condition})", (ids,))
        return not bad, f"not {label}: {[r['name'] for r in bad][:5]}"
    return check


def value_matches(sql):
    """Some value in the first row equals the truth (rates may be shown as percentages)."""
    def check(resp, rows):
        truth = _num(execute_sql(sql)[0]["value"])
        if not rows:
            return False, "no rows"
        targets = [truth, truth * 100] if truth is not None and 0 < abs(truth) <= 1 else [truth]
        found = [_num(v) for v in rows[0].values()]
        ok = truth is not None and any(
            f is not None and abs(f - t) <= max(abs(t) * 0.01, 0.0005) for f in found for t in targets)
        return ok, f"expected {truth}, first row {rows[0]}"
    return check


def shown_values_match(db_column):
    """Some result column holds each school's db_column value (whatever it was named) and is sorted ascending."""
    def check(resp, rows):
        ids = [r["id"] for r in rows if isinstance(r.get("id"), int)]
        truth = {r["id"]: _num(r["value"]) for r in execute_sql(
            f"SELECT id, {db_column} AS value FROM schools WHERE id = ANY(%s) AND year = '2024-25'", (ids,))}
        for column in rows[0] if rows else []:
            values = [_num(r.get(column)) for r in rows]
            if all(v is not None and truth.get(r.get("id")) == v for v, r in zip(values, rows)):
                return values == sorted(values), f"{column} not sorted ascending: {values}"
        return False, f"no column matches {db_column}: {rows[:2]}"
    return check


def text_contains(*parts, where="response"):
    def check(resp, rows):
        text = resp.response if where == "response" else " ".join(resp.notes)
        missing = [p for p in parts if p.lower() not in text.lower()]
        return not missing, f"{where} missing {missing}: {text[:160]}"
    return check


def summary_names_first_row():
    """For rankings, the summary names the school in row 1 (not one from reputation)."""
    def check(resp, rows):
        first = str(rows[0].get("name", "")) if rows else ""
        short = re.split(r"-| Campus", first)[0].strip()
        return bool(short) and short.lower() in resp.response.lower(), f"summary doesn't name {short!r}: {resp.response[:160]}"  # noqa: E501
    return check


def fallback_school(school_id):
    def check(resp, rows):
        school = (resp.fallback or {}).get("school") or {}
        ok = school.get("id") == school_id and bool(resp.fallback.get("suggestions"))
        return ok, f"fallback was {resp.fallback}"
    return check


def block_type(expected):
    def check(resp, rows):
        types = [b["type"] for b in resp.blocks]
        return expected in types, f"blocks were {types}"
    return check


def program_name_contains(word, cip_prefix):
    """Rows are for this program: by program_name, or (if not selected) the query filters its CIP code."""
    def check(resp, rows):
        if rows and "program_name" in rows[0]:
            names = [str(r.get("program_name", "")) for r in rows[:10]]
            return all(word.lower() in n.lower() for n in names), f"programs: {names[:5]}"
        return cip_prefix in rows.sql, f"no program_name and query doesn't filter cip {cip_prefix}"
    return check


class Rows(list):
    """Result rows plus the SQL that produced them (for checks that need it)."""
    sql = ""


DEGREE_GRANTING = all_schools_where("highest_degree >= 2", "degree-granting")
UNIVERSITIES = all_schools_where("carnegie_class BETWEEN 15 AND 23", "a four-year university")

CASES = [
    # The five cases from the spec
    ("top-ranked-wa", "rankings", "Top ranked university in Washington",
     [kind("result"), names_in_top(["University of Washington-Seattle"], 3), UNIVERSITIES,
      summary_names_first_row()]),
    ("cheapest-uc", "costs", "Cheapest UC school",
     [kind("result"), sorted_by(["cost_avg_net_price", "tuition_fees_in_state"], "asc"),
      names_in_top(["Merced"], 3), summary_names_first_row()]),
    ("highest-paid-cs", "majors", "Highest paid CS graduates",
     [kind("result"), program_name_contains("Computer", "11.07"),
      sorted_by([r"\w*earnings\w*4yr", r"\w*earnings\w*1yr"], "desc")]),
    ("compare-stanford-mit", "comparisons", "Compare Stanford and MIT",
     [kind("result"), includes_ids(STANFORD, MIT), block_type("compare")]),
    ("grad-rate-ucla", "outcomes", "Graduation rate at UCLA",
     [kind("result"), text_contains("%"),
      value_matches(f"SELECT completion_rate AS value FROM schools WHERE id = {UCLA} AND year = '2024-25'")]),

    # Rankings
    ("most-selective-universities", "rankings", "Most selective universities",
     [kind("result"), sorted_by(["admission_rate", "admit_rate"], "asc"), UNIVERSITIES]),
    ("hardest-ca", "rankings", "Hardest colleges to get into in California",
     [kind("result"), sorted_by(["admission_rate"], "asc"), all_schools_where("state = 'CA'", "in CA"),
      names_in_top(["California Institute of Technology", "Stanford"], 3)]),
    ("best-ohio", "rankings", "Best colleges in Ohio",
     [kind("result"), sorted_by(["copilot_score"], "desc"), DEGREE_GRANTING,
      all_schools_where("state = 'OH'", "in OH"), text_contains("CollegeCopilot Score", where="notes"),
      summary_names_first_row()]),
    ("best-national", "rankings", "What are the 10 best colleges in the country?",
     [kind("result"), row_count(10, 10), sorted_by(["copilot_score"], "desc"),
      names_in_top(["Massachusetts Institute of Technology", "Princeton"], 3)]),
    ("top10-ca-universities", "rankings", "Top 10 universities in California by graduation rate",
     [kind("result"), row_count(10, 10), UNIVERSITIES, sorted_by(["completion_rate"], "desc")]),
    ("top5-ca-public-colleges", "rankings", "Top 5 public colleges in California by graduation rate",
     [kind("result"), row_count(5, 5), DEGREE_GRANTING, sorted_by(["completion_rate"], "desc")]),

    # Costs
    ("affordable-private-ma", "costs", "Most affordable private colleges in Massachusetts",
     [kind("result"), sorted_by(["cost_avg_net_price"], "asc"), DEGREE_GRANTING,
      all_schools_where("state = 'MA' AND ownership = 2", "private nonprofit in MA")]),
    ("michigan-oos-tuition", "costs", "What is the out-of-state tuition at the University of Michigan?",
     [kind("result"), value_matches(
         f"SELECT tuition_fees_out_of_state AS value FROM ipeds_costs WHERE school_id = {MICHIGAN}")]),
    ("ucla-room-board", "costs", "How much is room and board at UCLA?",
     [kind("result"), value_matches(
         f"SELECT room_board_on_campus_cost AS value FROM ipeds_costs WHERE school_id = {UCLA}")]),
    ("best-value-tx", "costs", "Best value colleges in Texas",
     [kind("result"), sorted_by(["value_ratio"], "desc"), all_schools_where("state = 'TX'", "in TX")]),
    ("income-50k-fl", "costs", "My family makes $50,000. Which 5 colleges in Florida cost us the least?",
     [kind("result"), row_count(5, 5), shown_values_match("cost_net_price_income_48_75k"),
      all_schools_where("state = 'FL'", "in FL")]),

    # Outcomes
    ("highest-paying", "outcomes", "Highest paying colleges",
     [kind("result"), sorted_by(["earnings_median_10yr"], "desc"), DEGREE_GRANTING]),
    ("top-mobility", "outcomes", "Which colleges have the highest economic mobility?",
     [kind("result"), sorted_by(["mobility_rate"], "desc"), names_in_top(["Vaughn"], 1)]),
    ("harvard-4yr", "outcomes", "What is the 4-year graduation rate at Harvard?",
     [kind("result"), value_matches(
         f"SELECT bachelor_grad_rate_4yr AS value FROM ipeds_outcomes WHERE school_id = {HARVARD}")]),

    # Majors and careers
    ("nursing-earnings", "majors", "Which schools have the highest earnings for nursing graduates?",
     [kind("result"), program_name_contains("Nurs", "51.38"),
      sorted_by([r"\w*earnings\w*4yr", r"\w*earnings\w*1yr"], "desc")]),
    ("cs-jobs", "majors", "What jobs can a computer science major get?",
     [kind("result"), has_column("median_annual_salary", r"\w*salary\w*"), names_in_top(["Software Developers"], 15)]),
    ("fastest-job", "majors", "What is the fastest growing occupation?",
     [kind("result"), names_in_top(["Nurse Practitioners"], 1)]),

    # Safety
    ("safest-ca", "safety", "Safest colleges in California",
     [kind("result"), sorted_by(["criminal_offenses_per_1000_students"], "asc"),
      all_schools_where("state = 'CA' AND student_size >= 1000", "in CA with 1,000+ students")]),
    ("berkeley-crime", "safety", "Show me campus crime at UC Berkeley",
     [kind("result"), value_matches(
         f"SELECT criminal_offenses_total AS value FROM campus_crime WHERE school_id = {BERKELEY}")]),
    ("lowest-crime-ny", "safety", "Lowest crime universities in New York",
     [kind("result"), sorted_by(["criminal_offenses_per_1000_students"], "asc"),
      all_schools_where("state = 'NY'", "in NY")]),

    # Comparisons
    ("compare-harvard-yale", "comparisons", "Compare Harvard and Yale on cost and outcomes",
     [kind("result"), includes_ids(HARVARD, YALE), block_type("compare")]),
    ("compare-three", "comparisons", "Compare UCLA, UC Berkeley and USC on acceptance rate",
     [kind("result"), includes_ids(UCLA, BERKELEY, USC)]),

    # Guardrails
    ("unanswerable-dorms", "guardrails", "Which colleges have the best dorms?", [kind("cannot_answer")]),
    ("no-results", "guardrails", "Top 10 HBCUs in Nevada by graduation rate",
     [kind("no_results"), text_contains("couldn't find data matching your question")]),
    ("fewer-than-asked", "guardrails", "Top 10 public universities in Wyoming by graduation rate",
     [kind("result"), row_count(1, 9), text_contains("fewer than the 10", where="notes")]),
    ("ambiguous-columbia", "guardrails", "What is Columbia College's graduation rate?", [kind("clarify")]),
    ("fallback-ucla-food", "guardrails", "What is the food like at UCLA?",
     [kind("cannot_answer"), fallback_school(UCLA), text_contains("University of California-Los Angeles")]),

    # Deadlines and the CollegeCopilot Score
    ("harvard-early", "deadlines", "When is Harvard's early action deadline?",
     [kind("result"), text_contains("November 1")]),
    ("ucla-score", "rankings", "What is UCLA's CollegeCopilot Score?",
     [kind("result"), value_matches(f"SELECT copilot_score AS value FROM copilot_scores WHERE school_id = {UCLA}")]),
]


def run_case(case):
    case_id, category, question, checks = case
    started = time.monotonic()
    try:
        resp, rows, sql = answer_question(question)
        rows = Rows(rows)
        rows.sql = sql or ""
    except Exception as exc:  # report and keep going
        return case_id, category, False, f"error: {exc}", time.monotonic() - started
    failures = []
    for check in checks:
        ok, reason = check(resp, rows)
        if not ok:
            failures.append(reason)
    return case_id, category, not failures, "; ".join(failures), time.monotonic() - started


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in CASES if only in (None, c[0], c[1])]
    if not cases:
        sys.exit(f"No case or category named {only!r}")
    print(f"Running {len(cases)} cases...\n")
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(run_case, cases))

    by_category = {}
    for case_id, category, passed, reason, seconds in results:
        by_category.setdefault(category, []).append(passed)
        status = "PASS" if passed else "FAIL"
        print(f"{status}  {case_id:30} {seconds:5.1f}s  {'' if passed else reason[:300]}")
    print("\nBy category:")
    for category, outcomes in by_category.items():
        print(f"  {category:12} {sum(outcomes)}/{len(outcomes)}")
    passed = sum(r[2] for r in results)
    print(f"\nTOTAL {passed}/{len(results)} ({passed / len(results):.0%})")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
