import json
import logging
import os
import re
from pathlib import Path

import anthropic
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from database import execute_sql, get_data_sources, get_latest_year
from planner import plan_answer
from presentation import build_blocks, unsupported_numbers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")

app = FastAPI(title="CollegeCopilot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    question: str
    kind: str  # "result", "clarify", "cannot_answer" or "no_results"
    response: str  # short text answer (summary, clarifying question, or "I don't know")
    blocks: list[dict] = []
    source: str | None = None
    schools_considered: int = 0


SUMMARY_SYSTEM_PROMPT = """You are CollegeCopilot. Write a one- or two-sentence summary that
answers the question from the data. Tables and charts showing the full data appear below it.

RULES:
- Use ONLY the data provided. Never add schools, numbers, or facts that are not in it.
- Copy numbers exactly from the data. Rates are decimals: show 0.2546 as 25.5% (one decimal).
  Show money as whole dollars ($45,000).
- Rows with role "reference" are the school the user asked about.
- The rows were already selected to match the question: when it asks which schools meet a condition,
  every row meets it, even if the condition's column is not shown.
- When comparing two numbers, say "up" or "higher" only if the later or named value is larger.
- If values the question needs are null, say they are not reported.
- No advice, opinions, markdown, or lists."""

NO_ANSWER_MESSAGE = (
    "I don't know. That question can't be answered from the data I have "
    "(College Scorecard, IPEDS and other public sources)."
)
NO_RESULTS_MESSAGE = "No schools in the data match that question."

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


def write_summary(question: str, rows: list[dict], context: str) -> str:
    """Claude's summary, or a plain fallback if it cites a number not in the data."""
    message = _get_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
        max_tokens=300,
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f'QUESTION: "{question}"\n\nDATA ({len(rows)} rows):\n{json.dumps(rows, default=str)}',
        }],
    )
    # Keep only the first paragraph: the data is shown below, so a table or list is redundant.
    summary = message.content[0].text.strip().split("\n\n")[0].strip()
    bad = unsupported_numbers(summary, rows, f"{question} {context}")
    if bad:
        logger.warning("Summary dropped, numbers not in data: %s | %s", bad, summary)
        return fallback_summary(rows)
    return summary


@app.get("/health")
def health():
    try:
        result = execute_sql(
            "SELECT COUNT(*) AS total FROM schools WHERE year = %s", (get_latest_year(),)
        )
        total = result[0]["total"] if result else 0
    except Exception:
        total = 0
    return {"status": "healthy", "schools_in_database": total}


SUMMARY_ROW_LIMIT = 100


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest):
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    logger.info("Question: %s", question)

    try:
        answer = plan_answer(question)
        logger.info("Plan: kind=%s trace=%s", answer.kind, answer.trace)
        if answer.kind == "cannot_answer":
            return AskResponse(question=question, kind="cannot_answer", response=NO_ANSWER_MESSAGE)
        if answer.kind == "clarify":
            return AskResponse(question=question, kind="clarify", response=answer.clarification)

        rows = answer.rows[:SUMMARY_ROW_LIMIT]
        logger.info("Results: %d from %s", len(answer.rows), answer.source)
        if not rows:
            return AskResponse(question=question, kind="no_results", response=NO_RESULTS_MESSAGE)

        source = describe_sources(answer.source)
        return AskResponse(
            question=question,
            kind="result",
            response=write_summary(question, rows, f"{source} {answer.source}"),
            blocks=build_blocks(rows, answer.highlight),
            source=source,
            schools_considered=len(answer.rows),
        )

    except psycopg2.Error as e:
        logger.error("Database error: %s", e)
        raise HTTPException(status_code=500, detail="Database error while answering the question")
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        raise HTTPException(status_code=502, detail="AI service error, please try again")


@app.get("/")
def home():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index_path)