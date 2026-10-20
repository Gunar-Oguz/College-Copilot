"""
Score the question -> SQL -> rows pipeline against known-correct answers.

Each case in questions.json has a truth_sql that computes the correct answer
and a check type:
  value         first generated row contains the truth value (numbers within 1%)
  top_n         generated ids are the correct top n (ties at the cutoff allowed)
  ids_set       generated ids are exactly the truth ids
  cannot_answer generator returns CANNOT_ANSWER
  clarify       generator asks which school was meant

Usage (from the repo root, needs ANTHROPIC_API_KEY and Postgres):
    python evals/run_eval.py                 # run all cases
    python evals/run_eval.py --only lookup   # one category or case id
    python evals/run_eval.py --truth-only    # just check the truth SQL runs
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env")

from database import execute_sql, run_readonly_query  # noqa: E402
from sql_generator import CANNOT_ANSWER, CLARIFY_PREFIX, generate_sql  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
ID_KEYS = ("id", "school_id")


def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _matches(truth, candidate) -> bool:
    t, c = _numeric(truth), _numeric(candidate)
    if t is not None and c is not None:
        targets = [t, t * 100] if 0 < abs(t) <= 1 else [t]
        return any(abs(c - x) <= max(abs(x) * 0.01, 0.0005) for x in targets)
    if isinstance(truth, str) and isinstance(candidate, str):
        return truth.strip().lower() == candidate.strip().lower()
    return False


def _row_ids(rows: list[dict]) -> list | None:
    if not rows:
        return []
    for key in ID_KEYS:
        if rows and key in rows[0]:
            return [row[key] for row in rows]
    return None


def check_value(case, truth_rows, rows):
    truth = truth_rows[0]["value"]
    if not rows:
        return False, "no rows returned"
    candidates = {k: v for k, v in rows[0].items() if k not in ID_KEYS and k != "year"}
    if any(_matches(truth, v) for v in candidates.values()):
        return True, ""
    return False, f"expected {truth}, first row was {candidates}"


def check_top_n(case, truth_rows, rows):
    n = case["n"]
    cutoff = truth_rows[n - 1]["rank_value"]
    required = {r["id"] for r in truth_rows[:n] if r["rank_value"] != cutoff}
    eligible = required | {r["id"] for r in truth_rows if r["rank_value"] == cutoff}
    ids = _row_ids(rows)
    if ids is None:
        return False, "result has no id column"
    got = set(ids)
    if len(got) != n:
        return False, f"expected {n} schools, got {len(got)}"
    if not required <= got:
        return False, f"missing {sorted(required - got)}"
    if not got <= eligible:
        return False, f"unexpected {sorted(got - eligible)}"
    return True, ""


def check_ids_set(case, truth_rows, rows):
    ids = _row_ids(rows)
    if ids is None:
        return False, "result has no id column"
    expected = {r["id"] for r in truth_rows}
    got = set(ids)
    if got == expected:
        return True, ""
    return False, f"missing {sorted(expected - got)}, unexpected {sorted(got - expected)}"


CHECKS = {"value": check_value, "top_n": check_top_n, "ids_set": check_ids_set}


def load_truth(case) -> list[dict]:
    if case["check"] in ("cannot_answer", "clarify"):
        return []
    rows = execute_sql(case["truth_sql"])
    if not rows:
        raise ValueError("truth_sql returned no rows")
    if case["check"] == "value" and rows[0]["value"] is None:
        raise ValueError("truth value is NULL")
    if case["check"] == "top_n" and len(rows) < case["n"]:
        raise ValueError(f"truth has fewer than {case['n']} rows")
    return rows


def run_case(case) -> dict:
    result = {"id": case["id"], "category": case["category"], "question": case["question"]}
    start = time.perf_counter()
    try:
        truth_rows = load_truth(case)
        sql = generate_sql(case["question"])
        result["sql"] = sql
        if case["check"] == "cannot_answer":
            passed = sql == CANNOT_ANSWER
            reason = "" if passed else "answered instead of saying it can't"
        elif case["check"] == "clarify":
            passed = sql.startswith(CLARIFY_PREFIX)
            reason = "" if passed else "guessed instead of asking which school"
        elif sql.startswith(CLARIFY_PREFIX):
            passed, reason = False, f"asked unnecessarily: {sql}"
        elif sql == CANNOT_ANSWER:
            passed, reason = False, "said it can't answer"
        else:
            rows = run_readonly_query(sql)
            result["row_count"] = len(rows)
            passed, reason = CHECKS[case["check"]](case, truth_rows, rows)
    except Exception as exc:  # report every failure as a result, keep going
        passed, reason = False, f"{type(exc).__name__}: {exc}".strip()
    result.update(passed=passed, reason=reason, seconds=round(time.perf_counter() - start, 2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="category or case id to run")
    parser.add_argument("--truth-only", action="store_true", help="only validate truth_sql")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    cases = json.loads((EVAL_DIR / "questions.json").read_text())
    if args.only:
        cases = [c for c in cases if args.only in (c["id"], c["category"])]

    if args.truth_only:
        bad = 0
        for case in cases:
            try:
                load_truth(case)
            except Exception as exc:
                bad += 1
                print(f"BAD  {case['id']}: {exc}")
        print(f"{len(cases) - bad}/{len(cases)} truth queries OK")
        sys.exit(1 if bad else 0)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_case, cases))

    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark}  {r['id']:<28} {r['reason']}")

    print("\nBy category:")
    categories = sorted({r["category"] for r in results})
    for category in categories:
        group = [r for r in results if r["category"] == category]
        print(f"  {category:<14} {sum(r['passed'] for r in group)}/{len(group)}")
    total = sum(r["passed"] for r in results)
    print(f"\nTOTAL {total}/{len(results)} ({100 * total / len(results):.0f}%)")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"Details: {out}")


if __name__ == "__main__":
    main()
