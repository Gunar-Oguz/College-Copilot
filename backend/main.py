import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import admin_page
import app_logs
import user_data
from database import execute_sql, get_data_sources, get_latest_year
from guardrails import check_answer, ranking_basis
from planner import plan_answer
from presentation import build_blocks, unsupported_numbers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        user_data.ensure_user_tables()
        app_logs.ensure_log_tables()
    except psycopg2.Error as e:
        logger.error("Could not create user or log tables: %s", e)
    yield


app = FastAPI(title="CollegeCopilot", lifespan=lifespan)

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
    notes: list[str] = []  # guardrail notes, e.g. what a "ranking" is ordered by
    source: str | None = None
    schools_considered: int = 0
    log_id: int | None = None  # for thumbs up/down


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
- No advice, opinions, markdown, or lists."""

NO_ANSWER_MESSAGE = (
    "I don't know. That question can't be answered from the data I have "
    "(College Scorecard, IPEDS and other public sources)."
)
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
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
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


def answer_question(question: str) -> tuple[AskResponse, list[dict], str]:
    """The answer, the rows behind it, and the SQL or tool call that produced them."""
    answer = plan_answer(question)
    logger.info("Plan: kind=%s trace=%s", answer.kind, answer.trace)
    trace = "\n".join(answer.trace)
    if answer.kind == "cannot_answer":
        return AskResponse(question=question, kind="cannot_answer", response=NO_ANSWER_MESSAGE), [], trace
    if answer.kind == "clarify":
        return AskResponse(question=question, kind="clarify", response=answer.clarification), [], trace

    rows, notes = check_answer(question, answer.rows, answer.source)
    rows = rows[:SUMMARY_ROW_LIMIT]
    logger.info("Results: %d from %s", len(rows), answer.source)
    if not rows:
        return AskResponse(question=question, kind="no_results", response=NO_RESULTS_MESSAGE), [], answer.source

    source = describe_sources(answer.source)
    response = AskResponse(
        question=question,
        kind="result",
        response=write_summary(question, rows, f"{source} {answer.source}", ranking_basis(answer.source)),
        blocks=build_blocks(rows, answer.highlight),
        notes=notes,
        source=source,
        schools_considered=len(rows),
    )
    return response, rows, answer.source


def _log(question: str, sql: str | None, answer: str, started: float, kind: str) -> int | None:
    """Log a question; logging problems never break the answer."""
    try:
        return app_logs.log_query(question, sql, answer, round((time.monotonic() - started) * 1000), kind)
    except psycopg2.Error as e:
        logger.error("Could not log query: %s", e)
        return None


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest):
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    logger.info("Question: %s", question)
    started = time.monotonic()
    try:
        response, _rows, sql = answer_question(question)
    except psycopg2.Error as e:
        logger.error("Database error: %s", e)
        _log(question, None, f"Database error: {e}", started, "error")
        raise HTTPException(status_code=500, detail="Database error while answering the question")
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        _log(question, None, f"Claude API error: {e}", started, "error")
        raise HTTPException(status_code=502, detail="AI service error, please try again")

    text = " ".join([response.response, *response.notes])
    response.log_id = _log(question, sql, text, started, response.kind)
    return response


class FeedbackRequest(BaseModel):
    log_id: int
    rating: str  # "up" or "down"


@app.post("/feedback")
def feedback(request: FeedbackRequest):
    if request.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if not app_logs.save_feedback(request.log_id, request.rating):
        raise HTTPException(status_code=404, detail="Answer not found")
    return {"saved": True}


@app.get("/admin", response_class=HTMLResponse)
def admin(token: str | None = Query(default=None)):
    """Usage dashboard. Open by default; set ADMIN_TOKEN to require /admin?token=..."""
    expected = os.getenv("ADMIN_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Admin token required")
    return admin_page.render(app_logs.admin_stats())


class NoteRequest(BaseModel):
    note_text: str


def _user_id(authorization: str | None) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    try:
        return user_data.get_user(token)["id"]
    except user_data.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _note_text(request: NoteRequest) -> str:
    text = request.note_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note cannot be empty")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Note is too long (5,000 characters max)")
    return text


@app.get("/config")
def config():
    """Public Supabase settings for the browser (the anon key is meant to be public)."""
    return {"supabase": user_data.supabase_config()}


@app.get("/me/favorites")
def my_favorites(authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    return {"favorites": user_data.list_favorites(user_id)}


@app.get("/me/favorite-ids")
def my_favorite_ids(authorization: str | None = Header(default=None)):
    return {"ids": user_data.list_favorite_ids(_user_id(authorization))}


@app.put("/me/favorites/{unitid}")
def save_favorite(unitid: int, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    if not execute_sql("SELECT 1 FROM schools WHERE id = %s LIMIT 1", (unitid,)):
        raise HTTPException(status_code=404, detail="School not found")
    user_data.add_favorite(user_id, unitid)
    return {"saved": True}


@app.delete("/me/favorites/{unitid}")
def unsave_favorite(unitid: int, authorization: str | None = Header(default=None)):
    user_data.remove_favorite(_user_id(authorization), unitid)
    return {"saved": False}


@app.post("/me/favorites/{unitid}/notes")
def create_note(unitid: int, request: NoteRequest, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    if unitid not in user_data.list_favorite_ids(user_id):
        raise HTTPException(status_code=400, detail="Save the school before adding notes")
    return user_data.add_note(user_id, unitid, _note_text(request))


@app.put("/me/notes/{note_id}")
def edit_note(note_id: int, request: NoteRequest, authorization: str | None = Header(default=None)):
    note = user_data.update_note(_user_id(authorization), note_id, _note_text(request))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.delete("/me/notes/{note_id}")
def remove_note(note_id: int, authorization: str | None = Header(default=None)):
    if not user_data.delete_note(_user_id(authorization), note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"deleted": True}


@app.get("/")
def home():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(index_path)