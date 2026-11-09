"""
Answer one question: plan with Claude over the database tools, apply the guardrails, build the
display blocks and write the short summary.

Shared by the FastAPI app (main.py) and the Streamlit demo (streamlit_app.py); it has no web
framework, billing or account dependencies.
"""

import json
import logging
import os
import re

import anthropic
from pydantic import BaseModel

from database import execute_sql, get_data_sources, get_latest_year
from guardrails import (QUICK_REPLIES, VAGUE_MESSAGE, aid_note, check_answer, ranking_basis,
                        schools_named_in)
from planner import MODEL, plan_answer
from presentation import build_blocks, unsupported_numbers
from school_profile import school_basics

logger = logging.getLogger(__name__)

HISTORY_TURNS = 3  # earlier question/answer pairs sent to the planner for follow-ups
MEMORY_CHARS = 300  # an answer's summary is cut to about this length


class AskResponse(BaseModel):
    question: str
    kind: str  # "result", "clarify", "cannot_answer" or "no_results"
    response: str  # short text answer (summary, clarifying question, or "I don't know")
    blocks: list[dict] = []
    notes: list[str] = []  # guardrail notes, e.g. what a "ranking" is ordered by
    source: str | None = None
    schools_considered: int = 0
    log_id: int | None = None  # for thumbs up/down
    fallback: dict | None = None  # for "can't answer": what we do know, and questions we can answer
    usage: dict | None = None  # {"used", "limit", "plan"} for today's questions
    quick_replies: list[dict] = []  # [{"label", "question"}] buttons for vague questions
    memory: str | None = None  # short summary the chat sends back with follow-up questions


SUMMARY_SYSTEM_PROMPT = """You are CollegeCopilot. Write a one- or two-sentence summary that
answers the question from the data. Tables and charts showing the full data appear below it.

RULES:
- Use ONLY the data provided. Never add schools, numbers, or facts that are not in it.
- Copy numbers exactly from the data. Rates are decimals: show 0.2546 as 25.5% (one decimal).
  Show money as whole dollars ($45,000).
- Rows with role "reference" are the school the user asked about.
- The rows were already selected to match the question: when it asks which schools meet a condition,
  every row meets it, even if the condition's column is not shown.
- The rows are already in the answer's order: the first row is the top, best or cheapest result.
  Never call a different school the top one, and never use reputation or outside rankings.
- When comparing two numbers, say "up" or "higher" only if the later or named value is larger.
- If values the question needs are null, say they are not reported.
- If the question asks about financial aid, scholarships or grants and the data has net price, explain
  the link in a short clause: net price is what students pay after financial aid, so a lower net price
  means more aid. Never call net price an aid amount.
- No advice, opinions, markdown, or lists."""

NO_ANSWER_MESSAGE = (
    "I don't know. That question can't be answered from the data I have "
    "(College Scorecard, IPEDS and other public sources)."
)
NO_ANSWER_FOR_SCHOOL = "I don't have data to answer that, but here's what I do know about {name}."
GENERAL_SUGGESTIONS = [
    "Top 10 universities in California by graduation rate",
    "Which colleges have the highest economic mobility?",
    "What jobs can a computer science major get?",
    "Compare Harvard and Yale on cost and outcomes",
]
NO_RESULTS_MESSAGE = "I couldn't find data matching your question. Try rephrasing?"

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def fallback_summary(rows: list[dict]) -> str:
    reference = next((r for r in rows if r.get("role") == "reference"), None)
    if reference:
        return f"{len(rows) - 1} schools similar to {reference.get('name')}."
    if len(rows) == 1 and rows[0].get("name"):
        return f"Here is the data for {rows[0]['name']}."
    return f"Found {len(rows)} results."


def describe_sources(query: str) -> str:
    """Name the datasets behind an answer, based on the tables its query used."""
    sources = [f"U.S. Department of Education College Scorecard, {get_latest_year()} data"]
    for table in get_data_sources():
        if re.search(rf"\b{table['table_name']}\b", query):
            source = f"{table['source']} ({table['data_year']})" if table["data_year"] else table["source"]
            if source not in sources:
                sources.append(source)
    return "; ".join(sources)


def write_summary(question: str, rows: list[dict], context: str, order: str | None = None) -> str:
    """Claude's summary, or a plain fallback if it cites a number not in the data."""
    order_line = (
        f"\n\nROW ORDER: the rows are ranked by {order}; row 1 is the top result. If the question asks "
        f"for the top, best or a ranking, name row 1 as the top by {order} (there is no official ranking)."
        if order and len(rows) > 1 else ""
    )
    message = _get_client().messages.create(
        model=MODEL,
        max_tokens=300,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f'QUESTION: "{question}"{order_line}\n\nDATA ({len(rows)} rows):\n'
                       f"{json.dumps(rows, default=str)}",
        }],
    )
    # Keep only the first paragraph: the data is shown below, so a table or list is redundant.
    summary = message.content[0].text.strip().split("\n\n")[0].strip()
    bad = unsupported_numbers(summary, rows, f"{question} {context}")
    if bad:
        logger.warning("Summary dropped, numbers not in data: %s | %s", bad, summary)
        return fallback_summary(rows)
    return summary



SUMMARY_ROW_LIMIT = 100


def cannot_answer_response(question: str, school_ids: list[int]) -> AskResponse:
    """'I don't know', plus what we do know about the school and questions we can answer."""
    school = school_basics(school_ids[:1])
    if not school:
        return AskResponse(question=question, kind="cannot_answer", response=NO_ANSWER_MESSAGE,
                           fallback={"school": None, "suggestions": GENERAL_SUGGESTIONS})
    name = school[0]["name"]
    suggestions = [
        f"What is the graduation rate at {name}?",
        f"How much does {name} cost for a family making $75,000?",
        f"Find 5 schools similar to {name}",
        f"Show me campus crime at {name}",
    ]
    return AskResponse(question=question, kind="cannot_answer", response=NO_ANSWER_FOR_SCHOOL.format(name=name),
                       fallback={"school": school[0], "suggestions": suggestions})


def with_school_names(rows: list[dict]) -> list[dict]:
    """Add the school name to rows that only carry its id (e.g. deadline rows keyed by unitid)."""
    ids = {r.get("unitid", r.get("school_id")) for r in rows if not r.get("name")} - {None}
    ids = [i for i in ids if isinstance(i, int)]
    if not ids:
        return rows
    names = {r["id"]: r["name"] for r in execute_sql("SELECT id, name FROM schools WHERE id = ANY(%s)", (ids,))}
    return [{**r, "name": names.get(r.get("unitid", r.get("school_id")))} if not r.get("name") else r
            for r in rows]


def vague_response(question: str) -> AskResponse:
    return AskResponse(question=question, kind="clarify", response=VAGUE_MESSAGE, quick_replies=QUICK_REPLIES)


def memory_of(response: AskResponse, rows: list[dict]) -> str:
    """A short version of an answer for follow-up questions: its summary and the schools it showed."""
    text = response.response
    if len(text) > MEMORY_CHARS:
        text = text[:MEMORY_CHARS].rsplit(" ", 1)[0] + "..."
    names = list(dict.fromkeys(str(r["name"]) for r in rows if r.get("name")))
    if response.fallback and response.fallback.get("school"):
        names = names or [response.fallback["school"]["name"]]
    if names:
        more = f" and {len(names) - 5} more" if len(names) > 5 else ""
        text += f" Schools shown: {', '.join(names[:5])}{more}."
    return text


def answer_question(question: str, history: list[dict] | None = None) -> tuple[AskResponse, list[dict], str]:
    """The answer, the rows behind it, and the SQL or tool call that produced them."""
    answer = plan_answer(question, history)
    logger.info("Plan: kind=%s trace=%s", answer.kind, answer.trace)
    trace = "\n".join(answer.trace)
    if answer.kind == "cannot_answer":
        school_ids = answer.school_ids or schools_named_in(question)
        return cannot_answer_response(question, school_ids), [], trace
    if answer.kind == "clarify":
        if answer.vague:
            return vague_response(question), [], trace
        return AskResponse(question=question, kind="clarify", response=answer.clarification), [], trace

    asked = answer.standalone_question or question  # what a follow-up means on its own
    rows, notes = check_answer(asked, answer.rows, answer.source)
    rows = with_school_names(rows[:SUMMARY_ROW_LIMIT])
    logger.info("Results: %d from %s", len(rows), answer.source)
    if not rows:
        return AskResponse(question=question, kind="no_results", response=NO_RESULTS_MESSAGE), [], answer.source

    source = describe_sources(answer.source)
    summary = write_summary(asked, rows, f"{source} {answer.source}", ranking_basis(answer.source))
    if note := aid_note(asked, rows, summary):
        notes.append(note)
    response = AskResponse(
        question=question,
        kind="result",
        response=summary,
        blocks=build_blocks(rows, answer.highlight),
        notes=notes,
        source=source,
        schools_considered=len(rows),
    )
    return response, rows, answer.source
