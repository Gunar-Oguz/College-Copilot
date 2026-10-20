import json
import os

import anthropic

from database import check_query, get_latest_year, get_schema
from school_lookup import resolve_school

CANNOT_ANSWER = "CANNOT_ANSWER"
CLARIFY_PREFIX = "CLARIFY:"
MAX_TOOL_ROUNDS = 6

COLUMN_NOTES = """
- schools has one row per (id, year). id is the IPEDS UNITID. Names are NOT unique.
- Rates (admission_rate, completion_rate, retention_rate, pell_grant_rate, etc.) are decimals 0-1, e.g. 0.25 = 25%.
- ownership: 1 = public, 2 = private nonprofit, 3 = private for-profit.
- cost_avg_net_price is average annual net price. cost_net_price_income_* are net price by family income bracket:
  0_30k ($0-30,000), 30_48k ($30,001-48,000), 48_75k ($48,001-75,000), 75_110k ($75,001-110,000), 110k_plus ($110,001+).
  Use the bracket that contains the family income; an income exactly on a boundary belongs to the lower bracket
  (e.g. $48,000 -> 30_48k, $100,000 -> 75_110k).
- earnings_median_10yr is median earnings 10 years after entry, in dollars.
- state is a 2-letter code. Many columns are NULL for schools that did not report them.
- programs joins to schools on (programs.school_id = schools.id AND programs.year = schools.year).
  credential_level: 1 = undergraduate certificate, 2 = associate, 3 = bachelor's, 4 = post-bacc certificate,
  5 = master's, 6 = doctoral, 7 = first professional, 8 = graduate certificate.
  cip_code is the 4-digit CIP family as text 'NN.NN', e.g. '11.07' = Computer Science,
  '51.38' = Registered Nursing, '52.02' = Business Administration. There are no 6-digit codes.
""".strip()

RESOLVE_SCHOOL_TOOL = {
    "name": "resolve_school",
    "description": (
        "Look up a school by name or nickname (e.g. 'UCLA', 'Penn', 'UW'). Returns candidate "
        "schools with id, name, city, state, student_size and a match score (1.0 = exact name "
        "or alias match). Call once per school mentioned in the question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "School name as the user wrote it"}},
        "required": ["name"],
    },
}

SUBMIT_ANSWER_TOOL = {
    "name": "submit_answer",
    "description": (
        "Submit the final answer. kind=sql with the query, kind=clarify with a question for "
        "the user, or kind=cannot_answer. A query that fails to plan is returned with the "
        "database error so you can fix it and submit again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["sql", "clarify", "cannot_answer"]},
            "sql": {"type": "string", "description": "A single PostgreSQL SELECT query"},
            "clarification": {"type": "string", "description": "Question asking which school was meant"},
        },
        "required": ["kind"],
    },
}

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def build_prompt(question: str, schema: str, latest_year: str) -> str:
    return f"""Write one PostgreSQL SELECT query that answers the question.

SCHEMA:
{schema}

COLUMN NOTES:
{COLUMN_NOTES}

RULES:
- Filter schools.year = '{latest_year}' unless the question asks about other years or trends.
- Always select schools.id and schools.name, plus every column used to filter or sort.
- When filtering or sorting on a column, exclude rows where it is NULL.
- Add LIMIT 50 unless the question asks for a specific number or a count.
- For every specific school named in the question, call resolve_school, then filter with
  schools.id IN (...) using the returned ids. Never match school names with ILIKE or LIKE.
- Use a candidate when it is the only one with the top score, or when the question makes clear
  which one is meant (e.g. by state). If several candidates fit equally, answer clarify
  with a short question listing them with city and state.
- If no candidate plausibly matches a named school, or the question cannot be answered from
  these tables, answer cannot_answer.

QUESTION: "{question}"

Finish by calling submit_answer."""


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(line for line in lines[1:] if not line.startswith("```"))
    return text.strip()


def _submit(tool_input: dict) -> tuple[str | None, str]:
    """Return (final answer or None to keep going, tool result text)."""
    kind = tool_input.get("kind")
    if kind == "cannot_answer":
        return CANNOT_ANSWER, "ok"
    if kind == "clarify" and tool_input.get("clarification"):
        return f"{CLARIFY_PREFIX} {tool_input['clarification'].strip()}", "ok"
    if kind == "sql" and tool_input.get("sql"):
        sql = _strip_markdown(tool_input["sql"])
        error = check_query(sql)
        if error is None:
            return sql, "ok"
        return None, f"Query failed, fix it and submit again: {error}"
    return None, "Invalid submission: provide sql, clarification, or kind=cannot_answer."


def generate_sql(question: str) -> str:
    """Return SQL, CANNOT_ANSWER, or a question starting with CLARIFY_PREFIX."""
    messages = [{"role": "user", "content": build_prompt(question, get_schema(), get_latest_year())}]
    for _ in range(MAX_TOOL_ROUNDS):
        message = _get_client().messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
            max_tokens=1500,
            tools=[RESOLVE_SCHOOL_TOOL, SUBMIT_ANSWER_TOOL],
            tool_choice={"type": "any"},
            messages=messages,
        )
        results = []
        for block in message.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_answer":
                answer, text = _submit(block.input)
                if answer is not None:
                    return answer
            elif block.name == "resolve_school":
                text = json.dumps(resolve_school(block.input.get("name", "")), default=str)
            else:
                text = f"Unknown tool {block.name}"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text})

        messages.append({"role": "assistant", "content": message.content})
        messages.append({"role": "user", "content": results})
    return CANNOT_ANSWER
