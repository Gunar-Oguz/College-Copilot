"""
Answer-quality rules decided in code (not left to the model's judgment).

Before planning: question_hints() adds scope instructions based on the question's wording.
After planning: check_answer() removes non-degree programs from college/university lists and
adds notes when a "ranking" was asked for, or when fewer results came back than requested.
"""

import re

from database import execute_sql
from presentation import column_meta
from school_lookup import resolve_school

UNIVERSITY_RE = re.compile(
    r"\buniversities\b|\bfour[- ]year\b|\b4[- ]year\b"
    r"|\b(top|best|top[- ]ranked|highest[- ]ranked|ranked)\s+(\w+\s+){0,2}university\b",
    re.IGNORECASE,
)
COLLEGES_RE = re.compile(
    r"\bcolleges\b|\buniversities\b|\bschools\b|\bfour[- ]year\b|\b4[- ]year\b"
    r"|\b(top|best|top[- ]ranked|highest[- ]ranked|ranked|cheapest|safest)\s+(\w+\s+){0,2}(college|university|school)\b",
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

# Questions with no criteria to search by ("Help me find a good school").
_SCHOOL = r"(?:school|college|university|schools|colleges|universities)"
VAGUE_RES = [
    re.compile(rf"^(?:hi|hello|hey)?[\s,!.]*(?:please\s+)?(?:can|could|would)?\s*(?:you\s+)?(?:please\s+)?"
               rf"(?:help\s+me\s+)?(?:to\s+)?(?:find|pick|choose|select|recommend|suggest)\s+(?:me\s+)?"
               rf"(?:a|an|the|some|my)?\s*(?:good|great|right|best|perfect|nice)?\s*{_SCHOOL}"
               rf"(?:\s+for\s+me)?[\s?!.]*(?:please)?[\s?!.]*$", re.IGNORECASE),
    re.compile(rf"^(?:what|which)\s+{_SCHOOL}\s+(?:should|could|can)\s+i\s+(?:go\s+to|attend|apply\s+to|"
               rf"choose|pick)[\s?!.]*$", re.IGNORECASE),
    re.compile(rf"^(?:what|which)\s+(?:is|are)\s+(?:the\s+)?(?:right|best)\s+{_SCHOOL}\s+for\s+me[\s?!.]*$",
               re.IGNORECASE),
    re.compile(r"^(?:where\s+should\s+i\s+(?:apply|go)(?:\s+to\s+college)?|i\s+need\s+help\s+"
               rf"(?:finding|choosing|picking)\s+(?:a\s+)?{_SCHOOL}|help)[\s?!.]*$", re.IGNORECASE),
]
VAGUE_MESSAGE = "Happy to help! What matters most to you? Pick one below, or tell me in your own words."
QUICK_REPLIES = [
    {"label": "📍 Location", "question": "Help me find good colleges in a specific location"},
    {"label": "💰 Affordable", "question": "What are the most affordable colleges with strong graduation rates?"},
    {"label": "🎓 Top ranked", "question": "What are the top ranked colleges?"},
    {"label": "💻 Specific major", "question": "Help me find the best colleges for a specific major"},
]

AID_RE = re.compile(r"\b(?:financial\s+aid|aid|scholarships?|grants?|need[- ]based|merit|generous)\b",
                    re.IGNORECASE)
AID_HINT = (
    "This question asks about financial aid, scholarships or grants. Answer it now (do not submit clarify): "
    "aid shows up as a lower net price, so ORDER BY schools.cost_avg_net_price ASC (or the net_price_column "
    "for a family income given in the question) and select the in-state and out-of-state tuition too."
)

UNIVERSITY_HINT = (
    "This question asks about universities or four-year colleges: when listing or ranking schools, add "
    "schools.carnegie_class BETWEEN 15 AND 23 (doctoral, master's and baccalaureate institutions)."
)
COLLEGES_HINT = (
    "This question asks about colleges: when listing or ranking schools, add schools.highest_degree >= 3 "
    "(bachelor's-granting; leaves out vocational, certificate-only and community college programs)."
)
NON_DEGREE_NOTE = "Left out {n} vocational or community college program{s}, since you asked about colleges."
RANKING_NOTE = "There's no official ranking in this data, so results are ordered by {basis}."
SCORE_NOTE = ("Ranked by the CollegeCopilot Score: 40% graduation rate, 30% median earnings, "
              "20% selectivity and 10% student-faculty ratio, each compared with other colleges.")
FEWER_NOTE = "Only {count} result{s} in the data {verb}, so this shows fewer than the {requested} you asked for."


def is_vague(question: str) -> bool:
    """True for questions that give nothing to search by, like "Help me find a good school"."""
    return any(pattern.search(question.strip()) for pattern in VAGUE_RES)


def question_hints(question: str) -> str:
    """Scope instructions for the planner, decided from the question's wording."""
    hints = [AID_HINT] if _asks_about_aid(question) else []
    if AGGREGATE_RE.search(question):
        return "\n".join(hints)
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
        "SELECT id FROM schools WHERE id = ANY(%s) AND highest_degree < 3 "
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
        if basis and basis.startswith("collegecopilot score"):
            notes.append(SCORE_NOTE)
        elif basis:
            notes.append(RANKING_NOTE.format(basis=basis))
    return rows, notes


AID_NOTE = ("Net price is what you pay after financial aid (grants and scholarships). "
            "A lower net price means more aid is given.")


def _asks_about_aid(question: str) -> bool:
    """Aid, scholarship or grant questions (not land-grant status or Pell grant rates)."""
    return bool(AID_RE.search(question)) and not re.search(r"land[- ]grant|\bpell\b|athletic", question, re.IGNORECASE)


def aid_note(question: str, rows: list[dict], summary: str) -> str | None:
    """Explain net price for aid questions, unless the summary already did."""
    if not _asks_about_aid(question):
        return None
    if not any(key.startswith("cost_") and "net_price" in key for row in rows[:1] for key in row):
        return None
    if re.search(r"after (?:financial )?aid|after grants", summary, re.IGNORECASE):
        return None
    return AID_NOTE


QUESTION_WORDS = {"what", "which", "who", "where", "when", "why", "how", "is", "are", "does", "do", "can",
                  "tell", "show", "find", "list", "compare", "i", "my", "the", "a", "an", "best", "top"}
PHRASE_RE = re.compile(r"\b[A-Z][\w&'.-]*(?:\s+(?:of|at|in|the|and|&|[A-Z][\w&'.-]*))*")


def schools_named_in(question: str) -> list[int]:
    """Schools named exactly (full name or known nickname) in a question, e.g. 'UCLA', 'Ohio State'."""
    found = []
    for phrase in PHRASE_RE.findall(question):
        words = phrase.split()
        while words and words[0].lower() in QUESTION_WORDS:
            words = words[1:]
        while words and words[-1].lower() in {"of", "at", "in", "the", "and", "&"}:
            words = words[:-1]
        if not words:
            continue
        candidates = resolve_school(" ".join(words).strip(" .'"), limit=2)
        exact = [c for c in candidates if c["score"] == 1.0]
        if len(exact) == 1 and exact[0]["id"] not in found:
            found.append(exact[0]["id"])
    return found
