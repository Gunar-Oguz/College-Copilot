import json
import logging
import os
import time
from pathlib import Path

import anthropic
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from database import count_schools, run_query_pipeline
from data_coverage import fetch_coverage_stats, format_coverage_header
from response_formatter import (
    format_direct_response,
    format_list_response,
    slim_schools_for_claude,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")

# Haiku is ~3-5x faster than Sonnet for simple formatting; override via env if needed.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

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


SYSTEM_PROMPT = """You are CollegeCopilot, a formatting assistant for college data.

CRITICAL: You are NOT a database. All filtering has already been done in SQL.
The school data you receive is the complete, final result set.

YOUR ONLY JOB:
1. Format the provided schools into a clear, readable answer
2. Include helpful context ONLY when the user explicitly asks for it
3. Use ONLY the data provided — never invent schools or statistics

STRICT RULES — NEVER BREAK THESE:
- If exactly N schools are provided, your response MUST list exactly N schools
- NEVER exclude, skip, or re-filter any school from the provided data
- NEVER add schools that are not in the provided data
- NEVER apply acceptance rate, earnings, graduation, or any other filters yourself

FORMATTING:
- Percentages as "64.4%", money as "$45,000"
- Lists: clean numbered format; comparisons: markdown table
- No intro or closing phrases — just the direct formatted answer"""


def format_meta_answer(stats: dict) -> str:
    return (
        f"{stats['total_schools']:,} schools across "
        f"{stats['unique_states']} states/territories"
    )


def ask_claude(question: str, schools: list[dict], plan, sql_row_count: int) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    slim = slim_schools_for_claude(schools)
    schools_data = json.dumps(slim, default=str)

    logger.info(
        "Claude format-only: model=%s sql_rows=%d schools_sent=%d query_type=%s",
        CLAUDE_MODEL,
        sql_row_count,
        len(slim),
        plan.kind,
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f'Question: "{question}"\n\n'
                    f"Total matching schools: {len(schools)}\n"
                    f"FORMAT ONLY. List all {len(schools)} schools. "
                    f"Do not add or remove any.\n\n"
                    f"School data:\n{schools_data}"
                ),
            }
        ],
    )
    return message.content[0].text


@app.get("/health")
def health():
    try:
        total = count_schools()
    except psycopg2.Error:
        total = 0
    return {"status": "healthy", "schools_in_database": total}


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest):
    t0 = time.perf_counter()
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    try:
        schools, plan, parsed = run_query_pipeline(question)
    except psycopg2.Error as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc

    t_sql = time.perf_counter()

    if not schools and plan.kind not in ("list", "meta"):
        raise HTTPException(status_code=500, detail="No schools found in database")

    if plan.kind == "meta":
        return AskResponse(
            question=question,
            response=format_meta_answer(schools[0]),
            schools_considered=schools[0]["total_schools"],
        )

    sql_row_count = len(schools)
    coverage = fetch_coverage_stats(parsed) if plan.kind == "list" else []
    coverage_header = format_coverage_header(sql_row_count, coverage)

    logger.info(
        "Pipeline: parse+sql=%dms rows=%d kind=%s coverage=%s",
        int((t_sql - t0) * 1000),
        sql_row_count,
        plan.kind,
        coverage,
    )

    if sql_row_count == 0 and plan.kind == "list":
        return AskResponse(
            question=question,
            response=coverage_header.rstrip(),
            schools_considered=0,
        )

    # List/direct queries: format in Python (~instant). SQL already did all filtering.
    if plan.kind == "list":
        response = format_list_response(question, schools, parsed, coverage_header)
        logger.info("Response: local format in %dms total", int((time.perf_counter() - t0) * 1000))
        return AskResponse(
            question=question,
            response=response,
            schools_considered=sql_row_count,
        )

    if plan.kind == "direct":
        response = format_direct_response(schools, question)
        logger.info("Response: local format in %dms total", int((time.perf_counter() - t0) * 1000))
        return AskResponse(
            question=question,
            response=response,
            schools_considered=sql_row_count,
        )

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    try:
        response = ask_claude(question, schools, plan, sql_row_count)
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc}") from exc

    logger.info(
        "Response: claude in %dms (sql %dms) total %dms",
        int((time.perf_counter() - t_sql) * 1000),
        int((t_sql - t0) * 1000),
        int((time.perf_counter() - t0) * 1000),
    )

    return AskResponse(
        question=question,
        response=response,
        schools_considered=sql_row_count,
    )


@app.get("/")
def home():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(
        index_path,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )
