"""
Tests for saved schools and notes. Supabase is replaced by a fake token lookup; the
endpoints and database are real. Skipped when Postgres is not reachable.

Run from the repo root:
    python -m pytest tests/test_user_data.py -v
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
from database import UnsafeQueryError, get_db_connection, run_readonly_query  # noqa: E402

USERS = {"token-a": {"id": "test-user-a"}, "token-b": {"id": "test-user-b"}}
UCLA, MIT = 110662, 166683


def _db_available() -> bool:
    try:
        get_db_connection().close()
        return True
    except psycopg2.Error:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


def _fake_get_user(token):
    if token not in USERS:
        raise user_data.AuthError("Session expired, please sign in again")
    return USERS[token]


def _cleanup():
    for user in USERS.values():
        for unitid in (UCLA, MIT):
            user_data.remove_favorite(user["id"], unitid)
        for note in user_data._run(user["id"], "SELECT id FROM user_notes WHERE user_id = %s", (user["id"],)):
            user_data.delete_note(user["id"], note["id"])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(user_data, "get_user", _fake_get_user)
    user_data.ensure_user_tables()
    _cleanup()
    yield TestClient(main.app)
    _cleanup()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_requires_sign_in(client):
    assert client.get("/me/favorites").status_code == 401
    assert client.put(f"/me/favorites/{UCLA}", headers=auth("bad-token")).status_code == 401


def test_save_list_and_unsave(client):
    assert client.put(f"/me/favorites/{UCLA}", headers=auth("token-a")).json() == {"saved": True}
    client.put(f"/me/favorites/{UCLA}", headers=auth("token-a"))  # saving twice is fine
    favorites = client.get("/me/favorites", headers=auth("token-a")).json()["favorites"]
    assert [f["school_id"] for f in favorites] == [UCLA]
    assert favorites[0]["name"] == "University of California-Los Angeles"
    client.delete(f"/me/favorites/{UCLA}", headers=auth("token-a"))
    assert client.get("/me/favorite-ids", headers=auth("token-a")).json() == {"ids": []}


def test_unknown_school(client):
    assert client.put("/me/favorites/999999999", headers=auth("token-a")).status_code == 404


def test_notes_add_edit_delete(client):
    assert client.post(f"/me/favorites/{MIT}/notes", json={"note_text": "x"},
                       headers=auth("token-a")).status_code == 400  # must save first
    client.put(f"/me/favorites/{MIT}", headers=auth("token-a"))
    note = client.post(f"/me/favorites/{MIT}/notes", json={"note_text": "Visit in April"},
                       headers=auth("token-a")).json()
    edited = client.put(f"/me/notes/{note['id']}", json={"note_text": "Visit in May"}, headers=auth("token-a"))
    assert edited.json()["note_text"] == "Visit in May"
    favorites = client.get("/me/favorites", headers=auth("token-a")).json()["favorites"]
    assert [n["note_text"] for n in favorites[0]["notes"]] == ["Visit in May"]
    assert client.put(f"/me/notes/{note['id']}", json={"note_text": " "}, headers=auth("token-a")).status_code == 400
    assert client.delete(f"/me/notes/{note['id']}", headers=auth("token-a")).json() == {"deleted": True}


def test_users_cannot_see_or_change_each_others_data(client):
    client.put(f"/me/favorites/{UCLA}", headers=auth("token-a"))
    note = client.post(f"/me/favorites/{UCLA}/notes", json={"note_text": "private"}, headers=auth("token-a")).json()
    assert client.get("/me/favorites", headers=auth("token-b")).json()["favorites"] == []
    assert client.put(f"/me/notes/{note['id']}", json={"note_text": "hacked"}, headers=auth("token-b")).status_code == 404
    assert client.delete(f"/me/notes/{note['id']}", headers=auth("token-b")).status_code == 404
    client.delete(f"/me/favorites/{UCLA}", headers=auth("token-b"))
    assert client.get("/me/favorite-ids", headers=auth("token-a")).json() == {"ids": [UCLA]}


def test_planner_sql_cannot_read_user_tables(client):
    with pytest.raises(UnsafeQueryError):
        run_readonly_query("SELECT * FROM user_notes")


def _is_superuser() -> bool:
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        return cursor.fetchone()[0]


@pytest.mark.skipif(_db_available() and _is_superuser(), reason="superusers bypass row-level security")
def test_row_level_security_hides_rows_without_a_user(client):
    client.put(f"/me/favorites/{UCLA}", headers=auth("token-a"))
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM user_favorites")
        assert cursor.fetchone()[0] == 0
