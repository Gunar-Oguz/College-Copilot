"""
Answer-quality rules decided in code (not left to the model's judgment).

Before planning: question_hints() adds scope instructions based on the question's wording.
After planning: check_answer() removes non-degree programs from college/university lists and
adds notes when a "ranking" was asked for, or when fewer results came back than requested.
"""

import re

from database import execute_sql
from presentation import column_meta

UNIVERSITY_RE = re.compile(
    r"\buniversities\b|\bfour[- ]year\b|\b4[- ]year\b"
    r"|\b(top|best|top[- ]ranked|highest[- ]ranked|ranked)\s+(\w+\s+){0,2}university\b",
    re.IGNORECASE,
)
COLLEGES_RE = re.compile(
    r"\bcolleges\b|\buniversities\b|\bfour[- ]year\b|\b4[- ]year\b"
    r"|\b(top|best|top[- ]ranked|highest[- ]ranked|ranked|cheapest|safest)\s+(\w+\s+){0,2}(college|university)\b",
    re.IGNORECASE,
)
# Counts and averages keep every institution; the scope rules are for lists and rankings.
AGGREGATE_RE = re.compile(r"\bhow many\b|\baverage\b|\bnumber of\b|\bcount\b|\bwhich states?\b|\bin total\b",
                          re.IGNORECASE)
SCHOOLS_MEASURE_RE = re.compile(
    r"\b(top|best|rank|ranked|ranking)\b|graduat|complet|retention|admi(ssion|t)|accept|selective|"
    r"hardest|cost|price|tuition|afford|cheap|expensive|value|earning|salar|paying|debt|largest|biggest|"
    r"smallest|enrollment|students|size",
    re.IGNORECASE,
)
RANKING_RE = re.compile(r"\b(top|best|rank|ranked|ranking|rankings|number one|#\s?1)\b", re.IGNORECASE)
REQUESTED_COUNT_RES = [
    re.compile(r"\b(?:top|best|first|list|show(?: me)?|find|give me|name)\s+(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,3})\s+(?:\w+\s+){0,2}(?:schools|colleges|universities|programs|majors|jobs|"
               r"occupations|options|places)\b", re.IGNORECASE),
]

UNIVERSITY_HINT = (
    "This question asks about universities or four-year colleges: when listing or ranking schools, add "
    "schools.carnegie_class BETWEEN 15 AND 23 (doctoral, master's and baccalaureate institutions)."
)
COLLEGES_HINT = (
    "This question asks about colleges: when listing or ranking schools, add schools.highest_degree >= 2 "
    "(degree-granting; leaves out adult-education and certificate-only programs)."
)
NON_DEGREE_NOTE = "Left out {n} certificate-only or adult-education program{s}, since you asked about colleges."
RANKING_NOTE = "There's no official ranking in this data, so results are ordered by {basis}."
FEWER_NOTE = "Only {count} result{s} in the data {verb}, so this shows fewer than the {requested} you asked for."


def question_hints(question: str) -> str:
    """Scope instructions for the planner, decided from the question's wording."""
    if AGGREGATE_RE.search(question):
        return ""
    hints = []
    if UNIVERSITY_RE.search(question) and SCHOOLS_MEASURE_RE.search(question):
        hints.append(UNIVERSITY_HINT)
    elif COLLEGES_RE.search(question):
        hints.append(COLLEGES_HINT)
    return "\n".join(hints)


def requested_count(question: str) -> int | None:
    for pattern in REQUESTED_COUNT_RES:
        match = pattern.search(question)
        if match and 1 < int(match.group(1)) <= 100:
            return int(match.group(1))
    return None


def _order_by_columns(sql: str) -> list[str]:
    """Column names in the query's last ORDER BY, e.g. ['completion_rate', 'earnings_median_10yr']."""
    clauses = re.findall(r"\border\s+by\s+(.+?)(?:\blimit\b|\boffset\b|\)|;|$)", sql, re.IGNORECASE | re.DOTALL)
    if not clauses:
        return []
    columns = []
    for term in clauses[-1].split(","):
        term = re.sub(r"\b(asc|desc|nulls\s+(first|last))\b", "", term, flags=re.IGNORECASE).strip()
        if re.fullmatch(r"[\w.]+", term):
            columns.append(term.split(".")[-1])
    return columns


def ranking_basis(sql: str) -> str | None:
    labels = [column_meta(c)[0].lower() for c in _order_by_columns(sql) if c not in ("name", "id")]
    if not labels:
        return None
    return labels[0] if len(labels) == 1 else f"{labels[0]}, then {', then '.join(labels[1:])}"


def _non_degree_ids(ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = execute_sql(
        "SELECT id FROM schools WHERE id = ANY(%s) AND highest_degree < 2 "
        "AND year = (SELECT max(year) FROM schools)",
        (ids,),
    )
    return {r["id"] for r in rows}


def check_answer(question: str, rows: list[dict], sql: str) -> tuple[list[dict], list[str]]:
    """Rows to show (non-degree programs removed for college questions) and notes for the user."""
    notes = []
    school_ids = [r["id"] for r in rows if isinstance(r.get("id"), int) and r.get("name")]
    if len(rows) > 1 and school_ids and COLLEGES_RE.search(question) and not AGGREGATE_RE.search(question):
        drop = _non_degree_ids(school_ids)
        if drop:
            rows = [r for r in rows if r.get("id") not in drop or r.get("role") == "reference"]
            notes.append(NON_DEGREE_NOTE.format(n=len(drop), s="" if len(drop) == 1 else "s"))

    requested = requested_count(question)
    if requested and 0 < len(rows) < requested:
        one = len(rows) == 1
        notes.append(FEWER_NOTE.format(count=len(rows), s="" if one else "s", verb="matches" if one else "match",
                                       requested=requested))

    if len(rows) > 1 and RANKING_RE.search(question):
        basis = ranking_basis(sql)
        if basis:
            notes.append(RANKING_NOTE.format(basis=basis))
    return rows, notes
