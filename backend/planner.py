"""Turn a question into data using Claude as a planner over deterministic tools.

Claude never states facts itself here. It calls tools that read the database,
then submits the id of the tool result that answers the question (or asks for
clarification, or says it cannot answer).
"""

import json
import os
from dataclasses import dataclass, field

import anthropic
import psycopg2

from database import UnsafeQueryError, get_latest_year, get_schema, run_readonly_query
from guardrails import question_hints
from locations import resolve_location
from school_lookup import resolve_school
from similar_schools import find_similar_schools

MAX_TOOL_ROUNDS = 8

PREVIEW_ROWS = 20

INCOME_BRACKETS = [
    (30_000, "cost_net_price_income_0_30k", "$0-$30,000"),
    (48_000, "cost_net_price_income_30_48k", "$30,001-$48,000"),
    (75_000, "cost_net_price_income_48_75k", "$48,001-$75,000"),
    (110_000, "cost_net_price_income_75_110k", "$75,001-$110,000"),
    (float("inf"), "cost_net_price_income_110k_plus", "$110,001 and up"),
]

COLUMN_NOTES = """
- schools has one row per (id, year). id is the IPEDS UNITID. Names are NOT unique.
- Rates (admission_rate, completion_rate, retention_rate, pell_grant_rate, etc.) are decimals 0-1, e.g. 0.25 = 25%.
- schools.admission_rate = 0 is a reporting error (no admissions reported); always add
  admission_rate > 0 when filtering, sorting or counting by acceptance rate.
- ownership: 1 = public, 2 = private nonprofit, 3 = private for-profit.
- University systems are groups, not one school; list their campuses with these name patterns
  (the only allowed name matching): UC = schools.name LIKE 'University of California-%';
  CUNY = schools.name LIKE 'CUNY %'; SUNY = schools.name LIKE 'SUNY%' OR schools.name LIKE
  'State University of New York%'.
- cost_avg_net_price is average annual net price. cost_net_price_income_* are net price by family
  income bracket; always get the right column from net_price_column.
- earnings_median_10yr is median earnings 10 years after entry, in dollars.
- state is a 2-letter code. Many columns are NULL for schools that did not report them.
- programs joins to schools on (programs.school_id = schools.id AND programs.year = schools.year).
  credential_level: 1 = undergraduate certificate, 2 = associate, 3 = bachelor's, 4 = post-bacc certificate,
  5 = master's, 6 = doctoral, 7 = first professional, 8 = graduate certificate.
  cip_code is the 4-digit CIP family as text 'NN.NN', e.g. '11.07' = Computer Science,
  '51.38' = Registered Nursing, '52.02' = Business Administration. There are no 6-digit codes.
- schools.latitude / schools.longitude locate each campus. miles_between(lat1, lon1, lat2, lon2)
  returns the distance in miles, e.g.
  miles_between(schools.latitude, schools.longitude, 30.30526, -97.76259) AS distance_miles.
- The other tables (ipeds_*, mobility, ...) come from other public sources and have one row per
  school unless their description says otherwise. Join them with t.school_id = schools.id and still
  filter schools.year = the latest year. Their rates are also decimals 0-1.
- Use schools columns for measures it has: graduation rate = schools.completion_rate, retention =
  schools.retention_rate, acceptance rate = schools.admission_rate, size = schools.student_size, cost =
  schools.cost_avg_net_price, earnings, debt. Use the other tables only for what schools lacks or for
  breakdowns (by race/gender/Pell, 4-year rates, room and board, endowment, faculty, test policy,
  crime, athletics, mobility, careers, ...).
- school_designations has one row per (school, designation); to require two designations join it twice.

VOCABULARY (how to sort when the question uses these words and names no other measure):
- "top ranked", "best", "top universities/colleges", "highest ranked": use our CollegeCopilot Score:
  ORDER BY schools.copilot_score DESC (exclude NULLs) and select copilot_score plus
  schools.completion_rate and schools.earnings_median_10yr. "Ranked" or "rank" of a named school:
  copilot_scores.copilot_rank. For "the top/best/highest ranked X" (singular), still return the top 10
  so the user sees how close the others are.
- "most selective", "hardest to get into": ORDER BY schools.admission_rate ASC.
- "cheapest", "most affordable", "low cost": ORDER BY schools.cost_avg_net_price ASC (what families
  pay after aid) and also select ipeds_costs.tuition_fees_in_state and tuition_fees_out_of_state.
- "highest paying", "best salary", "most money": ORDER BY schools.earnings_median_10yr DESC. For a
  major, use programs.earnings_median_4yr (else earnings_median_1yr) for that cip_code, bachelor's
  (credential_level = 3) unless another level is asked, programs.year = the latest year.
- "safest", "lowest crime": join campus_crime, ORDER BY criminal_offenses_per_1000_students ASC and
  only include schools with at least 1,000 students (schools.student_size >= 1000).
- "best value": schools.earnings_median_10yr / NULLIF(schools.cost_avg_net_price, 0) AS value_ratio,
  ORDER BY value_ratio DESC, selecting earnings and net price too.
""".strip()

TOOLS = [
    {
        "name": "resolve_school",
        "description": (
            "Look up a school by name or nickname (e.g. 'UCLA', 'Penn', 'UW'). Returns candidates "
            "with id, name, city, state, student_size, latitude, longitude and a match score "
            "(1.0 = exact name or alias)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "resolve_location",
        "description": (
            "Look up a ZIP code or city (e.g. '94720', 'Austin, TX', 'Springfield') for distance "
            "searches. Returns candidates with label, latitude, longitude and population, largest first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "5-digit ZIP code or city name"},
                "state": {"type": "string", "description": "Optional 2-letter state code"},
            },
            "required": ["place"],
        },
    },
    {
        "name": "net_price_column",
        "description": "Return the schools column holding net price for a given annual family income.",
        "input_schema": {
            "type": "object",
            "properties": {"family_income": {"type": "number", "description": "Annual income in dollars"}},
            "required": ["family_income"],
        },
    },
    {
        "name": "find_similar_schools",
        "description": (
            "Find schools most similar to a school (selectivity, size, cost, outcomes, public/private, "
            "type). Returns the reference school first, then matches with a similarity score and the "
            "stats they are similar on. Use for 'schools like X' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "school_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "Number of matches, default 10, max 25"},
                "admission": {
                    "type": "string", "enum": ["any", "easier", "harder"],
                    "description": "easier = higher acceptance rate than the reference school",
                },
                "cheaper": {"type": "boolean", "description": "Only schools with a lower average net price"},
                "states": {"type": "array", "items": {"type": "string"}, "description": "2-letter codes"},
                "ownership": {"type": "integer", "description": "1 public, 2 private nonprofit, 3 for-profit"},
            },
            "required": ["school_id"],
        },
    },
    {
        "name": "run_query",
        "description": (
            "Run one read-only PostgreSQL SELECT and get a result id, the row count and a preview. "
            "Errors are returned so you can fix the query and try again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "submit_answer",
        "description": (
            "Finish. kind=result with the result_id that answers the question; kind=clarify with a "
            "question for the user; or kind=cannot_answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["result", "clarify", "cannot_answer"]},
                "result_id": {"type": "string"},
                "highlight_column": {
                    "type": "string",
                    "description": "The result column that most directly answers the question (charted)",
                },
                "clarification": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
]


@dataclass
class Answer:
    kind: str  # "result", "clarify" or "cannot_answer"
    rows: list[dict] = field(default_factory=list)
    clarification: str = ""
    source: str = ""  # tool and input that produced the rows
    highlight: str | None = None  # column that answers the question
    trace: list[str] = field(default_factory=list)
    school_ids: list[int] = field(default_factory=list)  # schools named in the question, if resolved


_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def net_price_column(family_income: float) -> dict:
    for upper, column, label in INCOME_BRACKETS:
        if family_income <= upper:
            return {"column": column, "bracket": label}
    raise AssertionError("unreachable")


def build_prompt(schema: str, latest_year: str) -> str:
    """System prompt: the same for every question, so it is cached."""
    return f"""Find the data that answers a question about U.S. colleges.

SCHEMA:
{schema}

COLUMN NOTES:
{COLUMN_NOTES}

RULES:
- For every specific school named in the question, call resolve_school and use the returned ids
  (also when you will submit cannot_answer).
  Never match school names with ILIKE or LIKE.
- Use a candidate when it is the only one with the top score, or when the question makes clear
  which one is meant (e.g. by state). If several candidates fit equally, submit clarify with a
  short question listing them with city and state.
- For any family income, call net_price_column to get the cost column.
- For "schools like X" questions, use find_similar_schools.
- For "near", "within N miles of" or "close to" a city or ZIP, call resolve_location and use its
  coordinates with miles_between in run_query (never write your own distance formula). Use the first candidate when it is much larger than
  the rest or the state is given; if several similar-sized places fit, submit clarify. Near a
  school, use the latitude/longitude that resolve_school returned for it. Never guess coordinates.
  Select the distance AS distance_miles and always add WHERE miles_between(...) <= the radius
  (25 miles if none is given; skip it only for "the N closest"). Sort by distance unless asked
  otherwise.
- In run_query: filter schools.year = '{latest_year}' unless the question asks about other years;
  add only the filters the question asks for (no minimum size or extra conditions), and submit that
  result even if surprising schools (trade, beauty, adult schools) rank high;
  select schools.id and schools.name plus every column used to filter or sort, keeping the schema's
  column names (use AS only for computed values); exclude NULLs in
  filtered or sorted columns; add LIMIT 50 unless a number or count is asked for.
- If a named school can't be found or the data can't answer the question, submit cannot_answer.
  Subjective quality (best dorms, food, social life, prestige) is not in the data: cannot_answer.

Finish by calling submit_answer."""


class _Session:
    """Runs tool calls for one question and keeps their results by id."""

    def __init__(self):
        self.results: dict[str, tuple[str, list[dict]]] = {}
        self.trace: list[str] = []
        self.school_ids: list[int] = []

    def _store(self, source: str, rows: list[dict]) -> str:
        result_id = f"r{len(self.results) + 1}"
        self.results[result_id] = (source, rows)
        return json.dumps(
            {"result_id": result_id, "row_count": len(rows), "preview": rows[:PREVIEW_ROWS]},
            default=str,
        )

    def call(self, name: str, args: dict) -> str:
        self.trace.append(f"{name}({json.dumps(args, default=str)})")
        try:
            if name == "resolve_school":
                candidates = resolve_school(args.get("name", ""))
                unique = len(candidates) == 1 or (
                    len(candidates) > 1 and candidates[0]["score"] > candidates[1]["score"])
                if candidates and unique and candidates[0]["id"] not in self.school_ids:
                    self.school_ids.append(candidates[0]["id"])
                return json.dumps(candidates, default=str)
            if name == "resolve_location":
                return json.dumps(resolve_location(args.get("place", ""), args.get("state")))
            if name == "net_price_column":
                return json.dumps(net_price_column(float(args["family_income"])))
            if name == "find_similar_schools":
                rows = find_similar_schools(**args)
                return self._store(f"find_similar_schools({json.dumps(args)})", rows)
            if name == "run_query":
                rows = run_readonly_query(args["sql"])
                # Schools need their id (for Save buttons and source checks).
                if rows and "name" in rows[0] and not {"id", "school_id"} & rows[0].keys():
                    return json.dumps({"error": "Add schools.id to the SELECT and run the query again."})
                return self._store(args["sql"], rows)
        except (UnsafeQueryError, psycopg2.Error, ValueError, TypeError, KeyError) as exc:
            message = getattr(exc, "pgerror", None) or str(exc)
            return json.dumps({"error": message.strip()})
        return json.dumps({"error": f"unknown tool {name}"})

    def submit(self, args: dict) -> Answer | str:
        """Return an Answer, or an error message to send back to Claude."""
        kind = args.get("kind")
        if kind == "cannot_answer":
            # Queries that ran fine but matched nothing mean "no results", not "can't answer".
            if self.results and all(not rows for _, rows in self.results.values()):
                source, rows = list(self.results.values())[-1]
                return Answer("result", rows=rows, source=source, trace=self.trace)
            return Answer("cannot_answer", trace=self.trace, school_ids=self.school_ids)
        if kind == "clarify" and args.get("clarification"):
            return Answer("clarify", clarification=args["clarification"].strip(), trace=self.trace)
        if kind == "result" and args.get("result_id") in self.results:
            source, rows = self.results[args["result_id"]]
            return Answer(
                "result", rows=rows, source=source,
                highlight=args.get("highlight_column"), trace=self.trace,
            )
        return "Invalid submission: give a valid result_id, a clarification, or kind=cannot_answer."


def plan_answer(question: str) -> Answer:
    session = _Session()
    system = [{
        "type": "text",
        "text": build_prompt(get_schema(), get_latest_year()),
        "cache_control": {"type": "ephemeral"},
    }]
    content = f'QUESTION: "{question}"'
    if hints := question_hints(question):
        content += f"\n\n{hints}"
    messages = [{"role": "user", "content": content}]
    for _ in range(MAX_TOOL_ROUNDS):
        message = _get_client().messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
            max_tokens=1500,
            system=system,
            tools=TOOLS,
            tool_choice={"type": "any"},
            messages=messages,
        )
        results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_answer":
                outcome = session.submit(block.input)
                if isinstance(outcome, Answer):
                    return outcome
                text = outcome
            else:
                text = session.call(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text})

        messages.append({"role": "assistant", "content": message.content})
        messages.append({"role": "user", "content": results})
    return Answer("cannot_answer", trace=session.trace)
