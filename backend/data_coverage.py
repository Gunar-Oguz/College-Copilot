from query_parser import ParsedQuery
from sql_builder import build_count_sql

# Columns with low fill rates — show reporting context when used in filters.
SPARSE_COLUMN_LABELS: dict[str, str] = {
    "admission_rate": "acceptance rates",
    "sat_average": "SAT scores",
    "act_median": "ACT scores",
    "completion_rate": "graduation rates",
    "earnings_median_10yr": "earnings data",
    "cost_avg_net_price": "net cost data",
    "cost_net_price_income_0_30k": "cost data for $0–$30k income",
    "cost_net_price_income_30_48k": "cost data for $30k–$48k income",
    "cost_net_price_income_48_75k": "cost data for $48k–$75k income",
    "cost_net_price_income_75_110k": "cost data for $75k–$110k income",
    "cost_net_price_income_110k_plus": "cost data for $110k+ income",
}


def sparse_filter_columns(parsed: ParsedQuery) -> list[str]:
    columns: list[str] = []

    if parsed.acceptance_rate_min is not None or parsed.acceptance_rate_max is not None:
        columns.append("admission_rate")
    if parsed.graduation_rate_min is not None or parsed.graduation_rate_max is not None:
        columns.append("completion_rate")
    if parsed.sat_min is not None or parsed.sat_max is not None:
        columns.append("sat_average")
    if parsed.act_min is not None or parsed.act_max is not None:
        columns.append("act_median")
    if parsed.earnings_min is not None or parsed.earnings_max is not None:
        columns.append("earnings_median_10yr")

    cost_column = parsed.cost_column or "cost_avg_net_price"
    if parsed.cost_min is not None or parsed.cost_max is not None:
        columns.append(cost_column)

    return [col for col in columns if col in SPARSE_COLUMN_LABELS]


def count_reporting_schools(conn, parsed: ParsedQuery, column: str) -> int:
    sql, params = build_count_sql(
        parsed,
        require_column=column,
        skip_columns={column},
    )
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def fetch_coverage_stats(parsed: ParsedQuery) -> list[tuple[str, str, int]]:
    if parsed.kind != "list":
        return []

    columns = sparse_filter_columns(parsed)
    if not columns:
        return []

    from database import get_db_connection

    conn = get_db_connection()
    try:
        return [
            (column, SPARSE_COLUMN_LABELS[column], count_reporting_schools(conn, parsed, column))
            for column in columns
        ]
    finally:
        conn.close()


def format_coverage_header(result_count: int, coverage: list[tuple[str, str, int]]) -> str:
    if not coverage:
        if result_count:
            return f"Found {result_count} schools matching your criteria.\n\n"
        return "No schools match those criteria."

    if len(coverage) == 1:
        _column, label, reporting = coverage[0]
        if result_count:
            return (
                f"Found {result_count} schools matching your criteria "
                f"(out of {reporting:,} that report {label}).\n\n"
            )
        if reporting == 0:
            return (
                f"No schools match your criteria. "
                f"No schools in your search area report {label}.\n\n"
            )
        return (
            f"No schools match your criteria "
            f"(out of {reporting:,} in your search area that report {label}).\n\n"
        )

    labels = ", ".join(label for _col, label, _cnt in coverage)
    reporting_parts = [f"{cnt:,} report {label}" for _col, label, cnt in coverage]
    reporting_text = "; ".join(reporting_parts)

    if result_count:
        return (
            f"Found {result_count} schools matching your criteria. "
            f"Data coverage in your search area: {reporting_text}.\n\n"
        )

    if all(cnt == 0 for _col, _label, cnt in coverage):
        return (
            f"No schools match your criteria. "
            f"No schools in your search area report {labels}.\n\n"
        )

    return (
        f"No schools match your criteria. "
        f"Data coverage in your search area: {reporting_text}.\n\n"
    )
