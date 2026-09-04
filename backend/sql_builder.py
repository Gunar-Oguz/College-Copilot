from dataclasses import dataclass, field

from query_parser import ParsedQuery

# Default to most recent loaded academic year until year selection is added to the parser.
DEFAULT_ACADEMIC_YEAR = "2024-25"

SELECT_COLUMNS = """
    id, name, city, state, zip, school_url, ownership,
    admission_rate, sat_average, act_median, cost_avg_net_price,
    cost_net_price_income_0_30k, cost_net_price_income_30_48k,
    cost_net_price_income_48_75k, cost_net_price_income_75_110k,
    cost_net_price_income_110k_plus,
    completion_rate, earnings_median_10yr, student_size, undergraduate_size
"""


@dataclass
class QueryPlan:
    kind: str
    question: str
    parsed: ParsedQuery
    sql: str = ""
    params: list = field(default_factory=list)
    limit: int | None = None
    use_claude: bool = True


def _append_range(
    where_clauses: list[str],
    params: list,
    column: str,
    minimum: float | None,
    maximum: float | None,
) -> None:
    if minimum is None and maximum is None:
        return

    where_clauses.append(f"{column} IS NOT NULL")
    if minimum is not None and maximum is not None:
        where_clauses.append(f"{column} BETWEEN %s AND %s")
        params.extend([minimum, maximum])
    elif minimum is not None:
        where_clauses.append(f"{column} > %s")
        params.append(minimum)
    elif maximum is not None:
        where_clauses.append(f"{column} < %s")
        params.append(maximum)


def build_where_clauses(
    parsed: ParsedQuery,
    skip_columns: set[str] | None = None,
) -> tuple[list[str], list]:
    skip = skip_columns or set()
    where_clauses: list[str] = []
    params: list = []

    if parsed.kind in ("schools", "direct"):
        where = " OR ".join(["name ILIKE %s"] * len(parsed.school_names))
        where_clauses.append(f"({where})")
        params.extend(parsed.school_names)
        where_clauses.append("year = %s")
        params.append(DEFAULT_ACADEMIC_YEAR)
        return where_clauses, params

    where_clauses.append("year = %s")
    params.append(DEFAULT_ACADEMIC_YEAR)

    if len(parsed.states) == 1:
        where_clauses.append("state = %s")
        params.append(parsed.states[0])
    elif len(parsed.states) > 1:
        placeholders = ", ".join(["%s"] * len(parsed.states))
        where_clauses.append(f"state IN ({placeholders})")
        params.extend(parsed.states)

    if parsed.ownership is not None:
        where_clauses.append("ownership = %s")
        params.append(parsed.ownership)

    if "admission_rate" not in skip:
        _append_range(
            where_clauses,
            params,
            "admission_rate",
            parsed.acceptance_rate_min,
            parsed.acceptance_rate_max,
        )
    if "completion_rate" not in skip:
        _append_range(
            where_clauses,
            params,
            "completion_rate",
            parsed.graduation_rate_min,
            parsed.graduation_rate_max,
        )
    if "sat_average" not in skip:
        _append_range(
            where_clauses,
            params,
            "sat_average",
            parsed.sat_min,
            parsed.sat_max,
        )
    if "earnings_median_10yr" not in skip:
        _append_range(
            where_clauses,
            params,
            "earnings_median_10yr",
            parsed.earnings_min,
            parsed.earnings_max,
        )

    cost_column = parsed.cost_column or "cost_avg_net_price"
    if cost_column not in skip:
        if parsed.cost_min is not None or parsed.cost_max is not None:
            where_clauses.append(f"{cost_column} IS NOT NULL")
            if parsed.cost_min is not None:
                where_clauses.append(f"{cost_column} >= %s")
                params.append(parsed.cost_min)
            if parsed.cost_max is not None:
                where_clauses.append(f"{cost_column} <= %s")
                params.append(parsed.cost_max)
        elif cost_column != "cost_avg_net_price":
            where_clauses.append(f"{cost_column} IS NOT NULL")

    return where_clauses, params


def build_count_sql(
    parsed: ParsedQuery,
    require_column: str | None = None,
    skip_columns: set[str] | None = None,
) -> tuple[str, list]:
    where_clauses, params = build_where_clauses(parsed, skip_columns=skip_columns)
    if require_column:
        if f"{require_column} IS NOT NULL" not in where_clauses:
            where_clauses.append(f"{require_column} IS NOT NULL")
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return f"SELECT COUNT(*) FROM schools {where_sql}", params


def build_meta_sql(parsed: ParsedQuery) -> tuple[str, list]:
    """Build COUNT query for meta questions, honoring state filters."""
    where_clauses = ["year = %s"]
    params: list = [DEFAULT_ACADEMIC_YEAR]

    if len(parsed.states) == 1:
        where_clauses.append("state = %s")
        params.append(parsed.states[0])
    elif len(parsed.states) > 1:
        placeholders = ", ".join(["%s"] * len(parsed.states))
        where_clauses.append(f"state IN ({placeholders})")
        params.extend(parsed.states)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    sql = (
        "SELECT COUNT(*) AS total_schools, COUNT(DISTINCT state) AS unique_states "
        f"FROM schools {where_sql}"
    )
    return sql, params


def build_sql(parsed: ParsedQuery) -> tuple[str, list]:
    """Step 2: Build precise SQL from structured filters."""
    if parsed.kind == "meta":
        return build_meta_sql(parsed)

    where_clauses, params = build_where_clauses(parsed)

    if parsed.kind in ("schools", "direct"):
        order_by = "student_size DESC NULLS LAST" if parsed.kind == "direct" else "name ASC"
        limit = parsed.limit if parsed.kind == "direct" else None
        return _finalize_sql(where_clauses, params, order_by, limit)

    order_by = None
    if parsed.sort_by:
        if parsed.sort_by == "admission_rate" and parsed.sort_order == "ASC":
            where_clauses.append("admission_rate > 0")
        if not _column_filtered(parsed, parsed.sort_by):
            where_clauses.append(f"{parsed.sort_by} IS NOT NULL")
        order_by = f"{parsed.sort_by} {parsed.sort_order} NULLS LAST"
    else:
        order_by = "name ASC"

    return _finalize_sql(where_clauses, params, order_by, parsed.limit)


def _column_filtered(parsed: ParsedQuery, column: str) -> bool:
    mapping = {
        "admission_rate": (parsed.acceptance_rate_min, parsed.acceptance_rate_max),
        "completion_rate": (parsed.graduation_rate_min, parsed.graduation_rate_max),
        "sat_average": (parsed.sat_min, parsed.sat_max),
        "earnings_median_10yr": (parsed.earnings_min, parsed.earnings_max),
    }
    bounds = mapping.get(column)
    if not bounds:
        return False
    return bounds[0] is not None or bounds[1] is not None


def _finalize_sql(
    where_clauses: list[str],
    params: list,
    order_by: str | None,
    limit: int | None,
) -> tuple[str, list]:
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    order_sql = f"ORDER BY {order_by}" if order_by else "ORDER BY name ASC"
    limit_sql = "LIMIT %s" if limit is not None else ""
    sql = f"SELECT {SELECT_COLUMNS} FROM schools {where_sql} {order_sql} {limit_sql}".strip()
    if limit is not None:
        params = [*params, limit]
    return sql, params


def build_query_plan(question: str) -> QueryPlan:
    from query_parser import parse_question

    parsed = parse_question(question)
    if parsed.kind == "meta":
        sql, params = build_meta_sql(parsed)
        return QueryPlan(
            kind="meta",
            question=question,
            parsed=parsed,
            sql=sql,
            params=params,
            use_claude=False,
        )

    sql, params = build_sql(parsed)
    return QueryPlan(
        kind=parsed.kind,
        question=question,
        parsed=parsed,
        sql=sql,
        params=params,
        limit=parsed.limit,
        use_claude=parsed.kind == "schools",
    )
