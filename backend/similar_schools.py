"""Find schools similar to a given school, with optional constraints.

Similarity is a weighted distance over standardized school stats, compared only
among schools offering the same highest degree, with penalties when ownership
(public/private) or Carnegie classification differ.
"""

import math
from functools import lru_cache

from database import execute_sql, get_latest_year

# column -> (weight, label)
FEATURES = {
    "admission_rate": (1.5, "acceptance rate"),
    "sat_average": (1.0, "SAT average"),
    "log_size": (1.0, "size"),
    "cost_avg_net_price": (0.75, "net price"),
    "completion_rate": (1.25, "graduation rate"),
    "retention_rate": (0.75, "retention rate"),
    "earnings_median_10yr": (1.0, "earnings"),
    "pell_grant_rate": (0.5, "share of Pell students"),
}
OWNERSHIP_PENALTY = 0.4
CARNEGIE_PENALTY = 0.3
MIN_SHARED_FEATURES = 4
CLOSE_Z = 0.5
MAX_LIMIT = 25

OUTPUT_COLUMNS = (
    "id", "name", "city", "state", "ownership", "admission_rate", "sat_average",
    "student_size", "cost_avg_net_price", "completion_rate", "earnings_median_10yr",
)


@lru_cache(maxsize=2)
def _load_pool(year: str) -> tuple[dict[int, dict], dict[str, tuple[float, float]]]:
    rows = execute_sql(
        """
        SELECT id, name, city, state, ownership, carnegie_class, highest_degree,
               admission_rate, sat_average, student_size, cost_avg_net_price,
               completion_rate, retention_rate, earnings_median_10yr, pell_grant_rate
        FROM schools WHERE year = %s AND student_size > 0
        """,
        (year,),
    )
    schools = {}
    for row in rows:
        row["log_size"] = math.log10(row["student_size"])
        for column in FEATURES:
            if row[column] is not None:
                row[column] = float(row[column])
        schools[row["id"]] = row

    stats = {}
    for column in FEATURES:
        values = [s[column] for s in schools.values() if s[column] is not None]
        mean = sum(values) / len(values)
        std = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) or 1.0
        stats[column] = (mean, std)
    return schools, stats


def _distance(target: dict, other: dict, stats) -> tuple[float, list[str]] | None:
    total, weights, shared, close = 0.0, 0.0, 0, []
    for column, (weight, label) in FEATURES.items():
        a, b = target[column], other[column]
        if a is None or b is None:
            continue
        dz = (a - b) / stats[column][1]
        total += weight * dz * dz
        weights += weight
        shared += 1
        if abs(dz) <= CLOSE_Z:
            close.append(label)
    if shared < MIN_SHARED_FEATURES:
        return None
    distance = math.sqrt(total / weights)
    if target["ownership"] != other["ownership"]:
        distance += OWNERSHIP_PENALTY
    if target["carnegie_class"] and target["carnegie_class"] != other["carnegie_class"]:
        distance += CARNEGIE_PENALTY
    return distance, close


def find_similar_schools(
    school_id: int,
    limit: int = 10,
    admission: str = "any",
    cheaper: bool = False,
    states: list[str] | None = None,
    ownership: int | None = None,
) -> list[dict]:
    """Return the reference school first (role='reference'), then matches (role='similar').

    admission: 'easier' (higher acceptance rate than the reference), 'harder', or 'any'.
    cheaper: only schools with a lower average net price than the reference.
    """
    schools, stats = _load_pool(get_latest_year())
    target = schools.get(school_id)
    if target is None:
        raise ValueError(f"No school with id {school_id} in the latest year")
    if admission != "any" and target["admission_rate"] is None:
        raise ValueError(f"{target['name']} does not report an acceptance rate")
    if cheaper and target["cost_avg_net_price"] is None:
        raise ValueError(f"{target['name']} does not report a net price")
    wanted_states = {s.upper() for s in states} if states else None

    matches = []
    for other in schools.values():
        if other["id"] == school_id or other["highest_degree"] != target["highest_degree"]:
            continue
        if wanted_states and other["state"] not in wanted_states:
            continue
        if ownership is not None and other["ownership"] != ownership:
            continue
        if admission != "any":
            rate = other["admission_rate"]
            if rate is None:
                continue
            if admission == "easier" and rate <= target["admission_rate"]:
                continue
            if admission == "harder" and rate >= target["admission_rate"]:
                continue
        if cheaper and (other["cost_avg_net_price"] is None
                        or other["cost_avg_net_price"] >= target["cost_avg_net_price"]):
            continue
        scored = _distance(target, other, stats)
        if scored:
            matches.append((scored[0], scored[1], other))

    matches.sort(key=lambda m: m[0])
    limit = max(1, min(limit, MAX_LIMIT))

    def output(row: dict, **extra) -> dict:
        return {**{c: row[c] for c in OUTPUT_COLUMNS}, **extra}

    results = [output(target, role="reference")]
    for distance, close, other in matches[:limit]:
        results.append(output(
            other, role="similar", similarity=round(1 / (1 + distance), 3), similar_on=close
        ))
    return results
