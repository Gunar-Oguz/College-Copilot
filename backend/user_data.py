"""
Signed-in users' saved schools and notes.

Users sign in with Supabase Auth in the browser and send their access token. The server
asks Supabase who the token belongs to (cached briefly) and only trusts that user id.

Every query filters on the user's id. As a second layer, user_favorites and user_notes use
row-level security keyed on app.user_id, which these queries set and the planner's read-only
SQL never does (validate_select also blocks these tables outright). Superusers bypass
row-level security, so the explicit filters are what count on a local database.
"""

import os
import time

import requests
from psycopg2.extras import RealDictCursor

from database import execute_sql, get_db_connection

TOKEN_CACHE_SEC = 300
_token_cache: dict[str, tuple[float, dict]] = {}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_favorites (
    user_id TEXT,
    unitid INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (user_id, unitid)
);
CREATE TABLE IF NOT EXISTS user_notes (
    id SERIAL PRIMARY KEY,
    user_id TEXT,
    unitid INTEGER,
    note_text TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS user_notes_user_school_idx ON user_notes (user_id, unitid);

CREATE TABLE IF NOT EXISTS user_reviews (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    unitid INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_text TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS user_reviews_one_per_school ON user_reviews (user_id, unitid);
CREATE INDEX IF NOT EXISTS user_reviews_school_idx ON user_reviews (unitid, created_at);

ALTER TABLE user_favorites ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_favorites FORCE ROW LEVEL SECURITY;
ALTER TABLE user_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_notes FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS own_rows ON user_favorites;
CREATE POLICY own_rows ON user_favorites
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS own_rows ON user_notes;
CREATE POLICY own_rows ON user_notes
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));

-- Reviews are public to read; only their author can add, change or delete them.
ALTER TABLE user_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_reviews FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS read_all ON user_reviews;
CREATE POLICY read_all ON user_reviews FOR SELECT USING (true);
DROP POLICY IF EXISTS own_insert ON user_reviews;
CREATE POLICY own_insert ON user_reviews FOR INSERT
    WITH CHECK (user_id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS own_update ON user_reviews;
CREATE POLICY own_update ON user_reviews FOR UPDATE
    USING (user_id = current_setting('app.user_id', true))
    WITH CHECK (user_id = current_setting('app.user_id', true));
DROP POLICY IF EXISTS own_delete ON user_reviews;
CREATE POLICY own_delete ON user_reviews FOR DELETE
    USING (user_id = current_setting('app.user_id', true));
"""

REVIEW_TEXT_MAX = 2000

FAVORITES_SQL = """
SELECT f.unitid AS school_id, s.name, s.city, s.state, s.ownership, s.admission_rate,
       s.cost_avg_net_price, s.completion_rate, s.earnings_median_10yr, s.student_size,
       f.created_at
FROM user_favorites f
LEFT JOIN schools s ON s.id = f.unitid AND s.year = (SELECT max(year) FROM schools)
WHERE f.user_id = %s
ORDER BY f.created_at DESC
"""


class AuthError(Exception):
    pass


def ensure_user_tables() -> None:
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(SCHEMA_SQL)


def supabase_config() -> dict:
    return {"url": os.getenv("SUPABASE_URL"), "anon_key": os.getenv("SUPABASE_ANON_KEY")}


def get_user(token: str | None) -> dict:
    """The Supabase user for an access token, or AuthError."""
    if not token:
        raise AuthError("Sign in required")
    cached = _token_cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1]
    config = supabase_config()
    if not config["url"] or not config["anon_key"]:
        raise AuthError("Sign-in is not configured on this server")
    try:
        response = requests.get(
            f"{config['url']}/auth/v1/user",
            headers={"apikey": config["anon_key"], "Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise AuthError("Could not verify sign-in") from exc
    if response.status_code != 200:
        raise AuthError("Session expired, please sign in again")
    user = response.json()
    if not user.get("id"):
        raise AuthError("Session expired, please sign in again")
    if len(_token_cache) > 1000:
        _token_cache.clear()
    _token_cache[token] = (time.time() + TOKEN_CACHE_SEC, user)
    return user


def _run(user_id: str, sql: str, params: tuple = ()) -> list[dict]:
    """Run SQL as the given user (row-level security sees only their rows)."""
    conn = get_db_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            cursor.execute(sql, params)
            return [dict(r) for r in cursor.fetchall()] if cursor.description else []
    finally:
        conn.close()


def list_favorite_ids(user_id: str) -> list[int]:
    rows = _run(user_id, "SELECT unitid FROM user_favorites WHERE user_id = %s ORDER BY created_at", (user_id,))
    return [r["unitid"] for r in rows]


def list_favorites(user_id: str) -> list[dict]:
    favorites = _run(user_id, FAVORITES_SQL, (user_id,))
    notes = _run(user_id, "SELECT id, unitid, note_text, created_at FROM user_notes WHERE user_id = %s "
                          "ORDER BY created_at", (user_id,))
    for favorite in favorites:
        favorite["notes"] = [n for n in notes if n["unitid"] == favorite["school_id"]]
    return favorites


def add_favorite(user_id: str, unitid: int) -> None:
    _run(user_id, "INSERT INTO user_favorites (user_id, unitid) VALUES (%s, %s) ON CONFLICT DO NOTHING",
         (user_id, unitid))


def remove_favorite(user_id: str, unitid: int) -> None:
    _run(user_id, "DELETE FROM user_favorites WHERE user_id = %s AND unitid = %s", (user_id, unitid))


def add_note(user_id: str, unitid: int, text: str) -> dict:
    rows = _run(user_id, "INSERT INTO user_notes (user_id, unitid, note_text) VALUES (%s, %s, %s) "
                         "RETURNING id, unitid, note_text, created_at", (user_id, unitid, text))
    return rows[0]


def update_note(user_id: str, note_id: int, text: str) -> dict | None:
    rows = _run(user_id, "UPDATE user_notes SET note_text = %s WHERE id = %s AND user_id = %s "
                         "RETURNING id, unitid, note_text, created_at", (text, note_id, user_id))
    return rows[0] if rows else None


def delete_note(user_id: str, note_id: int) -> bool:
    return bool(_run(user_id, "DELETE FROM user_notes WHERE id = %s AND user_id = %s RETURNING id",
                     (note_id, user_id)))


def review_summary(unitid: int, limit: int = 10) -> dict:
    """Average rating, count and recent reviews for a school (public; no user ids)."""
    totals = execute_sql(
        "SELECT round(avg(rating), 1) AS average, count(*) AS count FROM user_reviews WHERE unitid = %s",
        (unitid,),
    )[0]
    recent = execute_sql(
        "SELECT rating, review_text, created_at FROM user_reviews WHERE unitid = %s "
        "ORDER BY created_at DESC LIMIT %s",
        (unitid, limit),
    )
    return {"average": totals["average"], "count": totals["count"], "recent": recent}


def my_review(user_id: str, unitid: int) -> dict | None:
    rows = _run(user_id, "SELECT rating, review_text, created_at FROM user_reviews "
                         "WHERE user_id = %s AND unitid = %s", (user_id, unitid))
    return rows[0] if rows else None


def save_review(user_id: str, unitid: int, rating: int, text: str) -> dict:
    """Add or replace the user's review of a school."""
    rows = _run(
        user_id,
        """
        INSERT INTO user_reviews (user_id, unitid, rating, review_text) VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, unitid) DO UPDATE
            SET rating = EXCLUDED.rating, review_text = EXCLUDED.review_text, created_at = NOW()
        RETURNING rating, review_text, created_at
        """,
        (user_id, unitid, rating, text or None),
    )
    return rows[0]


def delete_review(user_id: str, unitid: int) -> bool:
    return bool(_run(user_id, "DELETE FROM user_reviews WHERE user_id = %s AND unitid = %s RETURNING id",
                     (user_id, unitid)))
