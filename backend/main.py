import json
import logging
import os
from pathlib import Path

import anthropic
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from database import execute_sql, get_latest_year
from planner import plan_answer

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
    response: str
    schools_considered: int


FORMAT_SYSTEM_PROMPT = """You are CollegeCopilot. You turn query results into a clear answer.

RULES:
- Use ONLY the data provided. Never add schools, numbers, or facts that are not in it.
- List every row provided, in the order given. Do not drop or re-filter rows.
- If a value is null, say "not reported" rather than guessing.
- If the data does not fully answer the question, say what is missing.
- Rates in the data are decimals: show 0.254 as 25.4%. Show money as $45,000.
- Rows with role "reference" are the school the user asked about: show it first as the point of
  comparison. For rows with role "similar", mention what they are similar on (similar_on).
- Do not add advice, explanations, or opinions. At most one short sentence summarizing the data.
- Use a numbered list for lists and a markdown table for comparisons. No intro or closing filler."""

NO_ANSWER_MESSAGE = (
    "I don't know. That question can't be answered from the data I have "
    "(U.S. Department of Education College Scorecard)."
)
NO_RESULTS_MESSAGE = "No schools in the data match that question."

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def format_response(question: str, data: list[dict], truncated: bool) -> str:
    note = f"(Only the first {len(data)} rows are shown.)" if truncated else ""
    message = _get_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
        max_tokens=2000,
        system=FORMAT_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f'QUESTION: "{question}"\n\n'
                f"DATA ({len(data)} rows) {note}:\n{json.dumps(data, default=str)}"
            ),
        }],
    )
    return message.content[0].text


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


FORMAT_ROW_LIMIT = 100


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
            return AskResponse(question=question, response=NO_ANSWER_MESSAGE, schools_considered=0)
        if answer.kind == "clarify":
            return AskResponse(question=question, response=answer.clarification, schools_considered=0)

        data = answer.rows
        logger.info("Results: %d from %s", len(data), answer.source)
        if not data:
            return AskResponse(question=question, response=NO_RESULTS_MESSAGE, schools_considered=0)

        response = format_response(
            question, data[:FORMAT_ROW_LIMIT], truncated=len(data) > FORMAT_ROW_LIMIT
        )
        response += f"\n\n*Source: U.S. Department of Education College Scorecard, {get_latest_year()} data.*"
        return AskResponse(question=question, response=response, schools_considered=len(data))

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