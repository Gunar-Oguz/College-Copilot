"""
Tests for query logging, feedback and the /admin page. Claude is replaced by a fixed answer;
the endpoints and database are real. Test rows are deleted afterwards.

Run from the repo root:
    python -m pytest tests/test_app_logs.py -v
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
from database import UnsafeQueryError, execute_sql, get_db_connection, run_readonly_query  # noqa: E402

MARKER = "[pytest] "


def _db_available() -> bool:
    try:
        get_db_connection().close()
        return True
    except psycopg2.Error:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


def _cleanup():
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM feedback WHERE question LIKE %s", (MARKER + "%",))
        cursor.execute("DELETE FROM query_logs WHERE question LIKE %s", (MARKER + "%",))
        cursor.execute("DELETE FROM usage_tracking WHERE user_id LIKE 'pytest:%'")


@pytest.fixture
def client(monkeypatch):
    def fake_answer(question, history=None):
        response = main.AskResponse(question=question, kind="result", response="UCLA's graduation rate is 93.0%.")
        return response, [{"id": 110662, "name": "UCLA"}], "SELECT completion_rate FROM schools"
    monkeypatch.setattr(main, "answer_question", fake_answer)
    monkeypatch.setattr(plans, "visitor_id", lambda ip: "pytest:visitor")
    app_logs.ensure_log_tables()
    plans.ensure_plan_tables()
    _cleanup()
    yield TestClient(main.app)
    _cleanup()


def test_every_question_is_logged(client):
    data = client.post("/ask", json={"question": MARKER + "Graduation rate at UCLA"}).json()
    [log] = execute_sql("SELECT * FROM query_logs WHERE id = %s", (data["log_id"],))
    assert log["question"] == MARKER + "Graduation rate at UCLA"
    assert log["sql_generated"] == "SELECT completion_rate FROM schools"
    assert log["answer"] == "UCLA's graduation rate is 93.0%."
    assert log["kind"] == "result" and log["response_time_ms"] >= 0


def test_feedback_saves_question_and_answer_and_can_change(client):
    log_id = client.post("/ask", json={"question": MARKER + "Q"}).json()["log_id"]
    assert client.post("/feedback", json={"log_id": log_id, "rating": "down"}).json() == {"saved": True}
    client.post("/feedback", json={"log_id": log_id, "rating": "up"})
    rows = execute_sql("SELECT question, answer, rating FROM feedback WHERE query_log_id = %s", (log_id,))
    assert rows == [{"question": MARKER + "Q", "answer": "UCLA's graduation rate is 93.0%.", "rating": "up"}]


def test_feedback_validation(client):
    log_id = client.post("/ask", json={"question": MARKER + "Q"}).json()["log_id"]
    assert client.post("/feedback", json={"log_id": log_id, "rating": "meh"}).status_code == 400
    assert client.post("/feedback", json={"log_id": -1, "rating": "up"}).status_code == 404


def test_admin_page_shows_stats_and_thumbs_down(client):
    log_id = client.post("/ask", json={"question": MARKER + "Which <b>dorms</b> are best?"}).json()["log_id"]
    client.post("/feedback", json={"log_id": log_id, "rating": "down"})
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Questions today" in page.text and "Recent 👎 answers" in page.text
    assert "Which &lt;b&gt;dorms&lt;/b&gt; are best?" in page.text  # escaped


def test_admin_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    assert client.get("/admin").status_code == 401
    assert client.get("/admin?token=secret").status_code == 200


def test_planner_sql_cannot_read_logs():
    for sql in ("SELECT * FROM query_logs", "SELECT question FROM feedback"):
        with pytest.raises(UnsafeQueryError):
            run_readonly_query(sql)
