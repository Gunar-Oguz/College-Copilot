"""
Tests for school profiles, the compare tool, reviews, the CollegeCopilot Score and the
"I don't know" fallback. Supabase is replaced by a fake token lookup; the database is real.

Run from the repo root:
    python -m pytest tests/test_school_pages.py -v
"""

import sys
from pathlib import Path

import psycopg2
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import main  # noqa: E402
import user_data  # noqa: E402
from database import UnsafeQueryError, execute_sql, get_db_connection, run_readonly_query  # noqa: E402

UCLA, BERKELEY, USC, MIT = 110662, 110635, 123961, 166683
USERS = {"token-a": {"id": "test-review-a"}, "token-b": {"id": "test-review-b"}}


def _db_ready() -> bool:
    try:
        rows = execute_sql("SELECT to_regclass('copilot_scores') IS NOT NULL AS ok")
        return rows[0]["ok"]
    except psycopg2.Error:
        return False


pytestmark = pytest.mark.skipif(not _db_ready(), reason="Postgres with copilot_scores not available")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _cleanup():
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT set_config('app.user_id', 'test-review-a', true)")
        cursor.execute("DELETE FROM user_reviews WHERE user_id = 'test-review-a'")
        cursor.execute("SELECT set_config('app.user_id', 'test-review-b', true)")
        cursor.execute("DELETE FROM user_reviews WHERE user_id = 'test-review-b'")


@pytest.fixture
def client(monkeypatch):
    def fake_get_user(token):
        if token not in USERS:
            raise user_data.AuthError("Session expired, please sign in again")
        return USERS[token]
    monkeypatch.setattr(user_data, "get_user", fake_get_user)
    monkeypatch.setattr("photos.get_photo", lambda unitid: None)  # no network in tests
    monkeypatch.setattr("school_profile.get_photo", lambda unitid: None)
    user_data.ensure_user_tables()
    _cleanup()
    yield TestClient(main.app)
    _cleanup()


def test_profile(client):
    p = client.get(f"/api/school/{UCLA}").json()
    assert p["name"] == "University of California-Los Angeles" and p["type"] == "Public"
    assert p["website"].startswith("https://")
    assert 0 < float(p["score"]["score"]) <= 100 and len(p["score"]["components"]) == 4
    assert any(m["level"] == "Bachelor's" and m["programs"] for m in p["majors"])
    assert p["safety"]["criminal_offenses_total"] > 0
    assert p["reviews"]["count"] == 0


def test_profile_404(client):
    assert client.get("/api/school/999999999").status_code == 404


def test_pages_are_served(client):
    for path in (f"/school/{UCLA}", "/compare", "/terms", "/privacy", "/static/auth.js", "/static/common.css"):
        assert client.get(path).status_code == 200


def test_search_and_basic(client):
    assert client.get("/api/schools/search?q=stanfrod").json()["schools"][0]["name"] == "Stanford University"
    names = [s["name"] for s in client.get(f"/api/schools/basic?ids={UCLA},{MIT}").json()["schools"]]
    assert names == ["University of California-Los Angeles", "Massachusetts Institute of Technology"]


def test_compare_marks_best_and_worst(client):
    data = client.get(f"/api/compare?ids={UCLA},{BERKELEY},{USC}").json()
    assert [s["id"] for s in data["schools"]] == [UCLA, BERKELEY, USC]
    acceptance = next(r for r in data["rows"] if r["key"] == "acceptance_rate")
    values = [float(c["value"]) for c in acceptance["cells"]]
    best = [c["best"] for c in acceptance["cells"]]
    assert best[values.index(min(values))]  # lower acceptance rate is better


@pytest.mark.parametrize("ids,status", [(f"{UCLA}", 400), (f"{UCLA},{MIT},{USC},{BERKELEY}", 400), ("a,b", 400)])
def test_compare_validation(client, ids, status):
    assert client.get(f"/api/compare?ids={ids}").status_code == status


def test_reviews(client):
    assert client.put(f"/me/reviews/{UCLA}", json={"rating": 5}).status_code == 401
    assert client.put(f"/me/reviews/{UCLA}", json={"rating": 6}, headers=auth("token-a")).status_code == 400
    client.put(f"/me/reviews/{UCLA}", json={"rating": 4, "review_text": "Great"}, headers=auth("token-a"))
    client.put(f"/me/reviews/{UCLA}", json={"rating": 5, "review_text": "Even better"}, headers=auth("token-a"))
    client.put(f"/me/reviews/{UCLA}", json={"rating": 2}, headers=auth("token-b"))
    reviews = client.get(f"/api/school/{UCLA}").json()["reviews"]
    assert reviews["count"] == 2 and float(reviews["average"]) == 3.5  # one review per user
    assert "user_id" not in reviews["recent"][0]
    assert client.get(f"/me/reviews/{UCLA}", headers=auth("token-a")).json()["review"]["review_text"] == "Even better"
    # B can't delete A's review: deleting only removes B's own.
    client.delete(f"/me/reviews/{UCLA}", headers=auth("token-b"))
    assert client.delete(f"/me/reviews/{UCLA}", headers=auth("token-b")).status_code == 404
    assert client.get(f"/api/school/{UCLA}").json()["reviews"]["count"] == 1


def test_planner_sql_cannot_read_reviews():
    with pytest.raises(UnsafeQueryError):
        run_readonly_query("SELECT * FROM user_reviews")


def test_copilot_score():
    top = execute_sql("SELECT school_id FROM copilot_scores ORDER BY copilot_rank LIMIT 10")
    assert MIT in [r["school_id"] for r in top]
    [row] = execute_sql(f"SELECT copilot_score FROM schools WHERE id = {MIT} AND year = '2024-25'")
    assert row["copilot_score"] is not None


def test_cannot_answer_fallback():
    with_school = main.cannot_answer_response("What is the food like at UCLA?", [UCLA])
    assert "University of California-Los Angeles" in with_school.response
    assert with_school.fallback["school"]["website"] and len(with_school.fallback["suggestions"]) == 4
    general = main.cannot_answer_response("Which dorms are best?", [])
    assert general.fallback["school"] is None and general.fallback["suggestions"]


def test_deadlines_loaded():
    if not execute_sql("SELECT to_regclass('school_deadlines') IS NOT NULL AS ok")[0]["ok"]:
        pytest.skip("school_deadlines not loaded")
    rows = execute_sql("SELECT deadline_type, deadline_date FROM school_deadlines WHERE unitid = 166027")
    assert {"restrictive_early_action", "regular"} <= {r["deadline_type"] for r in rows}
