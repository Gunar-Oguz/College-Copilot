"""Resolve a school name or nickname (e.g. "UCLA", "UW") to candidate school ids."""

import re

from database import execute_sql, get_latest_year

MIN_SIMILARITY = 0.35


def normalize(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


RESOLVE_SQL = """
WITH names AS (
    SELECT s.id AS school_id, s.name AS matched, 'name' AS match_type,
           regexp_replace(lower(s.name), '[^a-z0-9]+', ' ', 'g') AS norm
    FROM schools s WHERE s.year = %(year)s
    UNION ALL
    SELECT a.school_id, a.alias, 'alias', a.alias_norm FROM school_aliases a
),
scored AS (
    SELECT school_id, matched, match_type,
           CASE WHEN trim(norm) = %(q)s THEN 1.0 ELSE similarity(norm, %(q)s) END AS score
    FROM names
    WHERE trim(norm) = %(q)s OR norm %% %(q)s
),
best AS (
    SELECT DISTINCT ON (school_id) school_id, matched, match_type, score
    FROM scored ORDER BY school_id, score DESC
)
SELECT s.id, s.name, s.city, s.state, s.student_size, s.latitude, s.longitude,
       b.matched, b.match_type, round(b.score::numeric, 2) AS score
FROM best b
JOIN schools s ON s.id = b.school_id AND s.year = %(year)s
WHERE b.score >= %(min_score)s
ORDER BY b.score DESC, s.student_size DESC NULLS LAST
LIMIT %(limit)s
"""


def resolve_school(name: str, limit: int = 5) -> list[dict]:
    """Return up to `limit` candidate schools, best match first.

    score is 1.0 for an exact name/alias match, otherwise trigram similarity.
    """
    query = normalize(name)
    if not query:
        return []
    rows = execute_sql(
        RESOLVE_SQL,
        {"q": query, "year": get_latest_year(), "min_score": MIN_SIMILARITY, "limit": limit},
    )
    for row in rows:
        row["score"] = float(row["score"])
        for key in ("latitude", "longitude"):
            row[key] = float(row[key]) if row[key] is not None else None
    return rows
