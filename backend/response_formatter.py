from query_parser import ParsedQuery


def _format_percent(value) -> str:
    if value is None:
        return "Data not available"
    return f"{float(value) * 100:.1f}%"


def _format_money(value) -> str:
    if value is None:
        return "Data not available"
    return f"${float(value):,.0f}"


def _fields_for_question(question: str, parsed: ParsedQuery) -> dict[str, bool]:
    q = question.lower()
    return {
        "admission": parsed.acceptance_rate_min is not None
        or parsed.acceptance_rate_max is not None
        or any(kw in q for kw in ("acceptance", "admission", "selective")),
        "completion": parsed.graduation_rate_min is not None
        or parsed.graduation_rate_max is not None
        or any(kw in q for kw in ("graduation", "completion")),
        "earnings": parsed.earnings_min is not None
        or parsed.earnings_max is not None
        or any(kw in q for kw in ("earnings", "salary")),
        "cost": parsed.cost_min is not None
        or parsed.cost_max is not None
        or any(kw in q for kw in ("cost", "price", "tuition", "afford")),
        "sat": parsed.sat_min is not None
        or parsed.sat_max is not None
        or "sat" in q,
    }


def format_list_response(
    question: str,
    schools: list[dict],
    parsed: ParsedQuery,
    coverage_header: str = "",
) -> str:
    if not schools:
        return coverage_header.rstrip() or "No schools match those criteria."

    fields = _fields_for_question(question, parsed)
    lines: list[str] = []
    if coverage_header:
        lines.append(coverage_header.rstrip())
        lines.append("")

    for index, school in enumerate(schools, start=1):
        parts = [school["name"]]
        if school.get("state"):
            parts.append(school["state"])
        if fields["admission"]:
            parts.append(f"{_format_percent(school.get('admission_rate'))} acceptance")
        if fields["completion"]:
            parts.append(f"{_format_percent(school.get('completion_rate'))} graduation")
        if fields["earnings"]:
            parts.append(f"{_format_money(school.get('earnings_median_10yr'))} earnings")
        if fields["cost"]:
            cost_col = parsed.cost_column or "cost_avg_net_price"
            parts.append(f"{_format_money(school.get(cost_col))} net cost")
        if fields["sat"]:
            sat = school.get("sat_average")
            parts.append(f"SAT {int(sat)}" if sat is not None else "SAT Data not available")
        lines.append(f"{index}. {' — '.join(parts)}")

    return "\n".join(lines)


def format_direct_response(schools: list[dict], question: str) -> str:
    if not schools:
        return "Data not available"

    school = schools[0]
    q = question.lower()

    if "sat" in q:
        sat = school.get("sat_average")
        return str(int(sat)) if sat is not None else "Data not available"
    if any(kw in q for kw in ("earnings", "salary")):
        return _format_money(school.get("earnings_median_10yr"))
    if any(kw in q for kw in ("acceptance", "admission")):
        return _format_percent(school.get("admission_rate"))
    if any(kw in q for kw in ("graduation", "completion")):
        return _format_percent(school.get("completion_rate"))
    if any(kw in q for kw in ("cost", "price", "net")):
        return _format_money(school.get("cost_avg_net_price"))

    return school.get("name", "Data not available")


def slim_schools_for_claude(schools: list[dict]) -> list[dict]:
    """Send only display-relevant fields to Claude to cut latency."""
    keys = (
        "name", "state", "city", "admission_rate", "completion_rate",
        "sat_average", "act_median", "cost_avg_net_price",
        "cost_net_price_income_0_30k", "cost_net_price_income_30_48k",
        "cost_net_price_income_48_75k", "cost_net_price_income_75_110k",
        "cost_net_price_income_110k_plus", "earnings_median_10yr", "student_size",
    )
    return [{k: s[k] for k in keys if k in s} for s in schools]
