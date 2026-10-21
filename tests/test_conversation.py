"""
Tests for follow-up questions (recent turns sent with each question) and quick replies for vague
questions. Claude is replaced by a fake planner; the endpoints and database are real.

Run from the repo root:
    python -m pytest tests/test_conversation.py -v
"""

import sys
from pathlib import Path

import psycopg2
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import app_logs  # noqa: E402
import main  # noqa: E402
import plans  # noqa: E402
from database import get_db_connection  # noqa: E402
from guardrails import QUICK_REPLIES, is_vague  # noqa: E402
from planner import Answer, history_text  # noqa: E402


@pytest.mark.parametrize("question", [
    "Help me find a good school", "help me find a good college?", "What college should I go to?",
    "Recommend a college", "Where should I apply?", "Can you help me choose a university please", "help",
])
def test_vague_questions(question):
    assert is_vague(question)


@pytest.mark.parametrize("question", [
    "Help me find a good school in Texas", "Recommend a college for engineering",
    "What college should I go to for nursing?", "Find colleges with high graduation rates",
])
def test_questions_with_criteria_are_not_vague(question):
    assert not is_vague(question)


def test_history_text_lists_turns_oldest_first():
    text = history_text([{"question": "Graduation rate at MIT?", "answer": "96.2%. Schools shown: MIT."},
                         {"question": "And Stanford?", "answer": "92.4%."}])
    assert text.index("Graduation rate at MIT?") < text.index("And Stanford?")
    assert "Schools shown: MIT." in text


def test_memory_is_short_and_names_the_schools():
    long = main.AskResponse(question="q", kind="result", response="word " * 200)
    rows = [{"id": i, "name": f"School {i}"} for i in range(8)]
    memory = main.memory_of(long, rows)
    assert len(memory) < main.MEMORY_CHARS + 120
    assert "Schools shown: School 0, School 1, School 2, School 3, School 4 and 3 more." in memory


def _db_available() -> bool:
    try:
        get_db_connection().close()
        return True
    except psycopg2.Error:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@pytest.fixture
def client(monkeypatch):
    seen = {}

    def fake_plan(question, history=None):
        seen["history"] = history
        if question == "What about its acceptance rate?":
            return Answer("result", rows=[{"id": 166683, "name": "Massachusetts Institute of Technology",
                                           "admission_rate": 0.0455}],
                          source="SELECT admission_rate FROM schools",
                          standalone_question="What is MIT's acceptance rate?")
        return Answer("clarify", clarification="Which one?", vague=question == "Which school is right for me?")

    monkeypatch.setattr(main, "plan_answer", fake_plan)
    monkeypatch.setattr(main, "write_summary", lambda asked, *a: f"summary of: {asked}")
    monkeypatch.setattr(plans, "visitor_id", lambda ip: "pytest:visitor")
    app_logs.ensure_log_tables()
    plans.ensure_plan_tables()
    log_ids = []
    test_client = TestClient(main.app)
    test_client.log_ids = log_ids
    test_client.seen = seen
    yield test_client
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM query_logs WHERE id = ANY(%s)", (log_ids,))
        cursor.execute("DELETE FROM usage_tracking WHERE user_id LIKE 'pytest:%'")


def _ask(client, question, history=()):
    data = client.post("/ask", json={"question": question, "history": list(history)}).json()
    client.log_ids.append(data["log_id"])
    return data


@needs_db
def test_vague_question_gets_quick_replies_without_using_a_question(client):
    data = _ask(client, "Help me find a good school")
    assert data["kind"] == "clarify" and data["quick_replies"] == QUICK_REPLIES
    assert len(data["response"]) < 120
    assert "history" not in client.seen  # answered without the AI
    assert plans.questions_today("pytest:visitor") == 0


@needs_db
def test_model_flagged_vague_question_gets_quick_replies(client):
    data = _ask(client, "Which school is right for me?")
    assert data["quick_replies"] == QUICK_REPLIES and data["response"] == main.VAGUE_MESSAGE


@needs_db
def test_follow_up_gets_last_three_turns_and_a_standalone_summary(client):
    history = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(5)]
    data = _ask(client, "What about its acceptance rate?", history)
    assert client.seen["history"] == history[-3:]
    assert data["response"] == "summary of: What is MIT's acceptance rate?"
    assert data["memory"].endswith("Schools shown: Massachusetts Institute of Technology.")


@needs_db
def test_history_is_validated(client):
    too_long = [{"question": "q", "answer": "a" * 801}]
    assert client.post("/ask", json={"question": "x", "history": too_long}).status_code == 422
