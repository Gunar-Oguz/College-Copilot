"""Resolve a school name or nickname (e.g. "UCLA", "UW") to candidate school ids.

On PostgreSQL the matching runs in SQL with pg_trgm. DuckDB has no pg_trgm, so on DuckDB the same
trigram similarity is computed in Python over the latest year's names and aliases (cached), with
the same thresholds, float4 rounding and ordering.
"""

import re
import struct
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

from database import backend, database_token, execute_sql, get_latest_year

MIN_SIMILARITY = 0.35
TRGM_THRESHOLD = 0.3  # pg_trgm.similarity_threshold default, used by the % operator


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


# ---------- pg_trgm in Python (DuckDB backend) ----------

def trigrams(text: str) -> frozenset[str]:
    """pg_trgm's trigrams: each lower-cased alphanumeric word padded with two spaces before, one after."""
    grams = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        padded = f"  {word} "
        grams.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return frozenset(grams)


def _float4(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """pg_trgm similarity(): shared trigrams / all distinct trigrams, as a float4."""
    shared = len(a & b)
    union = len(a) + len(b) - shared
    return _float4(shared / union) if union else 0.0


def _round_score(score: float) -> float:
    """round(score::numeric, 2): float4 -> numeric keeps 6 significant digits, then rounds half up."""
    return float(Decimal(f"{score:.6g}").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@lru_cache(maxsize=4)
def _name_index(_db: str, year: str) -> tuple[list[tuple], dict[int, dict]]:
    schools = {r["id"]: r for r in execute_sql(
        "SELECT id, name, city, state, student_size, latitude, longitude FROM schools WHERE year = %s", (year,))}
    names = [(r["id"], r["name"], "name", re.sub(r"[^a-z0-9]+", " ", r["name"].lower())) for r in schools.values()]
    names += [(r["school_id"], r["alias"], "alias", r["alias_norm"])
              for r in execute_sql("SELECT school_id, alias, alias_norm FROM school_aliases")]
    return [(sid, matched, kind, norm.strip(), trigrams(norm)) for sid, matched, kind, norm in names], schools


def _resolve_in_python(query: str, year: str, limit: int) -> list[dict]:
    names, schools = _name_index(database_token(), year)
    query_grams = trigrams(query)
    best: dict[int, tuple] = {}
    for school_id, matched, kind, norm, grams in names:
        sim = similarity(grams, query_grams)
        if norm != query and sim < TRGM_THRESHOLD:
            continue
        score = 1.0 if norm == query else sim
        if school_id not in best or score > best[school_id][0]:
            best[school_id] = (score, matched, kind)
    found = [(score, schools[sid], matched, kind) for sid, (score, matched, kind) in best.items()
             if sid in schools and score >= MIN_SIMILARITY]
    found.sort(key=lambda f: (-f[0], -(f[1]["student_size"] if f[1]["student_size"] is not None else -1), f[1]["id"]))
    return [{**school, "matched": matched, "match_type": kind, "score": _round_score(score)}
            for score, school, matched, kind in found[:limit]]


def resolve_school(name: str, limit: int = 5) -> list[dict]:
    """Return up to `limit` candidate schools, best match first.

    score is 1.0 for an exact name/alias match, otherwise trigram similarity.
    """
    query = normalize(name)
    if not query:
        return []
    if backend() == "duckdb":
        rows = _resolve_in_python(query, get_latest_year(), limit)
    else:
        rows = execute_sql(
            RESOLVE_SQL,
            {"q": query, "year": get_latest_year(), "min_score": MIN_SIMILARITY, "limit": limit},
        )
    for row in rows:
        row["score"] = float(row["score"])
        for key in ("latitude", "longitude"):
            row[key] = float(row[key]) if row[key] is not None else None
    return rows
