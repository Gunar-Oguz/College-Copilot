import re
from dataclasses import asdict, dataclass, field

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

STATE_ABBRS = set(STATE_NAMES.values())

SCHOOL_ALIASES: list[tuple[str, str]] = [
    (r"\bmit\b", "Massachusetts Institute of Technology%"),
    (r"\bstanford\b", "Stanford University%"),
    (r"\bharvard\b", "Harvard University%"),
    (r"\byale\b", "Yale University%"),
    (r"\bprinceton\b", "Princeton University%"),
    (r"\bcolumbia\b", "Columbia University%"),
    (r"\bucla\b", "University of California-Los Angeles%"),
    (r"\buc berkeley\b", "University of California-Berkeley%"),
    (r"\bberkeley\b", "University of California-Berkeley%"),
    (r"\busc\b", "University of Southern California%"),
    (r"\buw\b", "University of Washington%"),
    (r"\bwsu\b", "Washington State University%"),
    (r"\bgonzaga\b", "Gonzaga University%"),
    (r"\bwwu\b", "Western Washington University%"),
    (r"\bcwu\b", "Central Washington University%"),
    (r"\bewu\b", "Eastern Washington University%"),
]

METRIC_KEYWORDS = {
    "admission_rate": ("acceptance rate", "acceptance", "admission rate", "admission"),
    "completion_rate": ("graduation rate", "graduation", "completion rate", "completion"),
    "sat_average": ("sat",),
    "act_median": ("act",),
    "cost_avg_net_price": ("net price", "net cost", "tuition", "cost", "price"),
    "earnings_median_10yr": ("earnings", "salary"),
    "student_size": ("enrollment", "student size", "size"),
}

INCOME_COST_COLUMNS = {
    "0_30k": "cost_net_price_income_0_30k",
    "30_48k": "cost_net_price_income_30_48k",
    "48_75k": "cost_net_price_income_48_75k",
    "75_110k": "cost_net_price_income_75_110k",
    "110k_plus": "cost_net_price_income_110k_plus",
}


@dataclass
class ParsedQuery:
    """Structured filters extracted from a natural-language question."""

    kind: str
    question: str
    states: list[str] = field(default_factory=list)
    school_names: list[str] = field(default_factory=list)
    acceptance_rate_min: float | None = None
    acceptance_rate_max: float | None = None
    graduation_rate_min: float | None = None
    graduation_rate_max: float | None = None
    sat_min: float | None = None
    sat_max: float | None = None
    act_min: float | None = None
    act_max: float | None = None
    earnings_min: float | None = None
    earnings_max: float | None = None
    cost_min: float | None = None
    cost_max: float | None = None
    cost_column: str | None = None
    ownership: int | None = None
    limit: int | None = None
    sort_by: str | None = None
    sort_order: str = "DESC"
    is_comparison: bool = False
    is_single_stat: bool = False

    def to_filters_dict(self) -> dict:
        data = asdict(self)
        data.pop("kind")
        data.pop("question")
        data.pop("is_comparison")
        data.pop("is_single_stat")
        return data


def _parse_percent(value: str) -> float:
    num = float(value.replace(",", ""))
    return num / 100 if num > 1 else num


def _parse_money(value: str, context: str) -> float:
    num = float(value.replace(",", ""))
    if num < 1000 or "k" in context.lower():
        num *= 1000
    return num


def _extract_limit(question: str) -> int | None:
    q = question.lower()
    match = re.search(r"\b(top|bottom|first|list)\s+(\d+)\b", q)
    if match:
        return int(match.group(2))
    match = re.search(r"\b(\d+)\s+schools?\b", q)
    if match:
        return int(match.group(1))
    return None


def _extract_states(question: str) -> list[str]:
    q = question.lower()
    states: list[str] = []

    for name, abbr in sorted(STATE_NAMES.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{re.escape(name)}\b", q) and abbr not in states:
            states.append(abbr)

    for match in re.finditer(r"\b([A-Z]{2})\b", question):
        abbr = match.group(1)
        if abbr in STATE_ABBRS and abbr not in states:
            states.append(abbr)

    if not states:
        context_match = re.search(r"\b(?:in|from)\s+([A-Za-z]{2})\b", question, re.IGNORECASE)
        if context_match:
            abbr = context_match.group(1).upper()
            if abbr in STATE_ABBRS:
                states.append(abbr)

    return states


def _extract_school_patterns(question: str) -> list[str]:
    patterns: list[str] = []
    for pattern, name_like in SCHOOL_ALIASES:
        if re.search(pattern, question, re.IGNORECASE):
            patterns.append(name_like)
    return list(dict.fromkeys(patterns))


def _detect_metric(question: str) -> str | None:
    q = question.lower()
    for column, keywords in METRIC_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return column
    return None


INCOME_BRACKET_BOUNDS = {
    "0_30k": (0, 30_000),
    "30_48k": (30_001, 48_000),
    "48_75k": (48_001, 75_000),
    "75_110k": (75_001, 109_999),
    "110k_plus": (110_000, None),
}


def _income_to_cost_column(income: float) -> str:
    if income <= 30_000:
        return INCOME_COST_COLUMNS["0_30k"]
    if income <= 48_000:
        return INCOME_COST_COLUMNS["30_48k"]
    if income <= 75_000:
        return INCOME_COST_COLUMNS["48_75k"]
    if income < 110_000:
        return INCOME_COST_COLUMNS["75_110k"]
    return INCOME_COST_COLUMNS["110k_plus"]


def _extract_income_amount(question: str) -> float | None:
    q = question.lower()

    income_range = re.search(
        r"income\s+\$?\s*([\d,]+)\s*k?\s*[-–to]+\s*\$?\s*([\d,]+)\s*k?",
        q,
    )
    if income_range:
        low = float(income_range.group(1).replace(",", ""))
        high = float(income_range.group(2).replace(",", ""))
        if low < 1000:
            low *= 1000
        if high < 1000:
            high *= 1000
        return (low + high) / 2

    patterns = [
        r"(?:family|household|families)\s+makes?\s+\$?\s*([\d,]+)\s*k?",
        r"(?:income|making|makes?)\s*(?:of|is|:)?\s*\$?\s*([\d,]+)\s*k?",
        r"(?:income|making|family|household|families)[^$]{0,40}\$\s*([\d,]+)\s*k?",
        r"\$\s*([\d,]+)\s*k?\s*income",
        r"making\s+\$?\s*([\d,]+)\s*k?",
    ]
    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            income = float(match.group(1).replace(",", ""))
            if income < 1000:
                income *= 1000
            return income

    return None


def _extract_income_cost_column(question: str) -> str | None:
    q = question.lower()

    if re.search(r"income\s+(?:over|above|greater than)\s+\$?\s*110", q):
        return INCOME_COST_COLUMNS["110k_plus"]
    if re.search(r"income\s+\$?\s*0\s*[-–to]+\s*\$?\s*30", q):
        return INCOME_COST_COLUMNS["0_30k"]
    if re.search(r"income\s+\$?\s*30[,000]*\s*[-–to]+\s*\$?\s*48", q):
        return INCOME_COST_COLUMNS["30_48k"]
    if re.search(r"income\s+\$?\s*48[,000]*\s*[-–to]+\s*\$?\s*75", q):
        return INCOME_COST_COLUMNS["48_75k"]
    if re.search(r"income\s+\$?\s*75[,000]*\s*[-–to]+\s*\$?\s*110", q):
        return INCOME_COST_COLUMNS["75_110k"]

    has_income_context = any(
        w in q for w in ("income", "making", "makes", "family", "household", "families")
    )
    if not has_income_context and not re.search(r"\$\s*[\d,]+\s*k?\s*income", q):
        return None

    income = _extract_income_amount(question)
    if income is None:
        return None

    return _income_to_cost_column(income)


def _set_acceptance_range(parsed: ParsedQuery, low: float, high: float) -> None:
    parsed.acceptance_rate_min = low
    parsed.acceptance_rate_max = high


def _apply_acceptance_filters(parsed: ParsedQuery, question: str) -> None:
    q = question.lower()
    has_admission_context = any(kw in q for kw in ("acceptance", "admission"))
    has_range = parsed.acceptance_rate_min is not None or parsed.acceptance_rate_max is not None

    op_map = {
        "above": ">", "over": ">", "greater than": ">", ">": ">",
        "below": "<", "under": "<", "less than": "<", "<": "<",
    }

    combo_match = re.search(
        r"(?:acceptance|admission)\s+rate\s+(?:above|over|greater than|>)\s*(\d+(?:\.\d+)?)\s*%?\s+and\s+(?:below|under|less than|<)\s*(\d+(?:\.\d+)?)\s*%?",
        q,
    )
    if combo_match:
        _set_acceptance_range(parsed, _parse_percent(combo_match.group(1)), _parse_percent(combo_match.group(2)))
        return

    between_patterns = [
        r"(?:acceptance|admission)\s+(?:rate\s+)?between\s+(\d+(?:\.\d+)?)\s*%?\s+and\s+(\d+(?:\.\d+)?)\s*%?",
        r"(?:acceptance|admission)\s+(?:rate\s+)?between\s+(\d+(?:\.\d+)?)\s*%\s*-\s*(\d+(?:\.\d+)?)\s*%?",
        r"(?:acceptance|admission)\s+(?:rate\s+)?between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*%?",
    ]
    for pattern in between_patterns:
        match = re.search(pattern, q)
        if match:
            _set_acceptance_range(parsed, _parse_percent(match.group(1)), _parse_percent(match.group(2)))
            return

    if has_admission_context:
        generic_between = re.search(
            r"\bbetween\s+(\d+(?:\.\d+)?)\s*%?\s+and\s+(\d+(?:\.\d+)?)\s*%?",
            q,
        )
        if generic_between:
            _set_acceptance_range(
                parsed,
                _parse_percent(generic_between.group(1)),
                _parse_percent(generic_between.group(2)),
            )
            return

        generic_hyphen = re.search(
            r"\bbetween\s+(\d+(?:\.\d+)?)\s*%\s*-\s*(\d+(?:\.\d+)?)\s*%?",
            q,
        )
        if generic_hyphen:
            _set_acceptance_range(
                parsed,
                _parse_percent(generic_hyphen.group(1)),
                _parse_percent(generic_hyphen.group(2)),
            )
            return

    range_match = re.search(
        r"(?:acceptance|admission)\s+(?:rate\s+)?(?:from\s+)?(\d+(?:\.\d+)?)\s*%?\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*%?",
        q,
    )
    if range_match and not has_range:
        _set_acceptance_range(parsed, _parse_percent(range_match.group(1)), _parse_percent(range_match.group(2)))
        return

    if has_range:
        return

    metric_patterns: list[tuple] = [
        (r"(?:acceptance|admission)\s+rate\s+(above|over|greater than|>)\s*(\d+(?:\.\d+)?)\s*%?", "admission_rate"),
        (r"(?:acceptance|admission)\s+rate\s+(below|under|less than|<)\s*(\d+(?:\.\d+)?)\s*%?", "admission_rate"),
        (r"(?:acceptance|admission)\s+(?:above|over|greater than|>)\s*(\d+(?:\.\d+)?)\s*%?", "admission_rate", ">"),
        (r"(?:acceptance|admission)\s+(?:rate\s+)?(?:below|under|less than|<)\s*(\d+(?:\.\d+)?)\s*%?", "admission_rate", "<"),
        (r"(?:acceptance|admission)\s*>\s*(\d+(?:\.\d+)?)\s*%?", "admission_rate", ">"),
        (r"(?:acceptance|admission)\s*<\s*(\d+(?:\.\d+)?)\s*%?", "admission_rate", "<"),
    ]

    for entry in metric_patterns:
        if len(entry) == 3:
            pattern, _column, op = entry
            for match in re.finditer(pattern, q):
                value = _parse_percent(match.group(1))
                if op == ">":
                    parsed.acceptance_rate_min = value
                else:
                    parsed.acceptance_rate_max = value
        else:
            pattern, _column = entry
            for match in re.finditer(pattern, q):
                op = op_map[match.group(1)]
                value = _parse_percent(match.group(2))
                if op == ">":
                    parsed.acceptance_rate_min = value
                else:
                    parsed.acceptance_rate_max = value


def _apply_graduation_filters(parsed: ParsedQuery, question: str) -> None:
    q = question.lower()
    op_map = {
        "above": ">", "over": ">", "greater than": ">", ">": ">",
        "below": "<", "under": "<", "less than": "<", "<": "<",
    }

    patterns = [
        (r"(?:graduation|completion)\s+rate\s+(above|over|greater than|>)\s*(\d+(?:\.\d+)?)\s*%?", "rate"),
        (r"(?:graduation|completion)\s+rate\s+(below|under|less than|<)\s*(\d+(?:\.\d+)?)\s*%?", "rate"),
        (r"(?:graduation|completion)\s+(?:rate\s+)?(?:above|over|>|greater than)\s*(\d+(?:\.\d+)?)\s*%?", "short"),
    ]

    for pattern, style in patterns:
        for match in re.finditer(pattern, q):
            if style == "short":
                parsed.graduation_rate_min = _parse_percent(match.group(1))
            else:
                op = op_map[match.group(1)]
                value = _parse_percent(match.group(2))
                if op == ">":
                    parsed.graduation_rate_min = value
                else:
                    parsed.graduation_rate_max = value


def _apply_earnings_filters(parsed: ParsedQuery, question: str) -> None:
    q = question.lower()

    above_match = re.search(
        r"(?:earnings|salary)\s*(?:above|over|greater than|>)\s*\$?\s*([\d,]+)\s*k?",
        q,
    )
    if above_match:
        parsed.earnings_min = _parse_money(above_match.group(1), above_match.group(0))

    below_match = re.search(
        r"(?:earnings|salary)\s+(below|under|less than|<)\s*\$?\s*([\d,]+)\s*k?",
        q,
    )
    if below_match:
        parsed.earnings_max = _parse_money(below_match.group(2), below_match.group(0))


def _apply_sat_filters(parsed: ParsedQuery, question: str) -> None:
    q = question.lower()
    op_map = {
        "above": ">", "over": ">", "greater than": ">", ">": ">",
        "below": "<", "under": "<", "less than": "<", "<": "<",
    }

    for pattern in (
        r"\bsat\s+(above|over|greater than|>)\s*(\d+)",
        r"\bsat\s+(below|under|less than|<)\s*(\d+)",
    ):
        for match in re.finditer(pattern, q):
            op = op_map[match.group(1)]
            value = float(match.group(2))
            if op == ">":
                parsed.sat_min = value
            else:
                parsed.sat_max = value


def _apply_cost_filters(parsed: ParsedQuery, question: str) -> None:
    q = question.lower()
    parsed.cost_column = _extract_income_cost_column(question) or "cost_avg_net_price"
    cost_col = parsed.cost_column

    cost_lt = re.search(r"cost\s*<\s*\$?\s*([\d,]+)\s*k?", q)
    if cost_lt:
        parsed.cost_max = _parse_money(cost_lt.group(1), cost_lt.group(0))
        return

    if any(w in q for w in ("cost", "price", "tuition", "afford", "$", "income", "making")):
        cost_match = re.search(r"(?:under|below|less than)\s*\$?\s*([\d,]+)\s*k?", q)
        if cost_match:
            parsed.cost_max = _parse_money(cost_match.group(1), cost_match.group(0))

        cost_above = re.search(
            r"(?:cost|price|tuition)\s+(?:above|over|greater than)\s*\$?\s*([\d,]+)\s*k?",
            q,
        )
        if cost_above:
            parsed.cost_min = _parse_money(cost_above.group(1), cost_above.group(0))


def _extract_ownership(question: str) -> int | None:
    q = question.lower()
    if re.search(r"\bpublic\s+(universit|college|school)", q) or (
        "public" in q and "private" not in q and ("universit" in q or "college" in q)
    ):
        return 1
    if "for-profit" in q or "for profit" in q:
        return 3
    if "private" in q and "public" not in q:
        return 2
    return None


def _detect_sort_column(text: str, cost_column: str | None = None) -> str | None:
    """Detect sort column from a sort clause (earnings checked before acceptance)."""
    q = text.lower()
    if any(kw in q for kw in ("cost", "price", "tuition", "afford")):
        if cost_column and cost_column != "cost_avg_net_price":
            return cost_column
        return "cost_avg_net_price"

    sort_metrics = [
        ("earnings_median_10yr", ("earnings", "salary")),
        ("completion_rate", ("graduation rate", "graduation", "completion rate", "completion")),
        ("admission_rate", ("acceptance rate", "acceptance", "admission rate", "admission")),
        ("sat_average", ("sat",)),
        ("act_median", ("act",)),
        ("student_size", ("enrollment", "student size", "size")),
    ]
    for column, keywords in sort_metrics:
        if any(kw in q for kw in keywords):
            return column
    return None


def _extract_sort_intent(question: str, cost_column: str | None = None) -> tuple[str | None, str | None]:
    """Extract explicit sort column and direction from phrases like 'sort by earnings highest first'."""
    q = question.lower()

    sort_clause_patterns = [
        r"(?:sort(?:ed)?|order(?:ed)?|rank(?:ed)?)\s+by\s+(.+?)(?:\s*,|\s*$|\.)",
        r"\b(?:top|bottom|best|worst|rank(?:ing)?)\s+(?:\d+\s+)?(?:schools?\s+)?by\s+(.+?)(?:\s*,|\s*$|\.)",
    ]
    for pattern in sort_clause_patterns:
        match = re.search(pattern, q)
        if match:
            clause = match.group(1).strip()
            column = _detect_sort_column(clause, cost_column)
            if column:
                return column, _ranking_direction(clause, column)

    by_metric = re.search(
        r"\bby\s+(?:(?:highest|lowest|best|worst|most|least)\s+)?"
        r"(earnings|salary|acceptance(?:\s+rate)?|admission(?:\s+rate)?|"
        r"graduation(?:\s+rate)?|completion(?:\s+rate)?|sat|act|"
        r"cost|net\s+price|net\s+cost|tuition|enrollment)"
        r"(?:\s+(?:first|last))?",
        q,
    )
    if by_metric:
        clause = by_metric.group(0)
        column = _detect_sort_column(clause, cost_column)
        if column:
            return column, _ranking_direction(clause, column)

    return None, None


def _infer_sort_from_filters(parsed: ParsedQuery) -> tuple[str | None, str]:
    if parsed.earnings_min is not None:
        return "earnings_median_10yr", "DESC"
    if parsed.graduation_rate_min is not None:
        return "completion_rate", "DESC"
    if parsed.acceptance_rate_min is not None or parsed.acceptance_rate_max is not None:
        return "admission_rate", "DESC"
    return None, "DESC"


def _wants_ranking(question: str) -> bool:
    q = question.lower()
    return any(
        w in q
        for w in (
            "top", "bottom", "highest", "lowest", "best", "worst",
            "most", "least", "rank", "ranking", "selective", "cheapest",
            "affordable", "competitive", "sort", "sorted", "order", "ordered",
        )
    )


def _ranking_direction(question: str, metric: str) -> str:
    q = question.lower()
    if any(w in q for w in ("lowest", "cheapest", "most affordable", "bottom", "minimum")):
        return "ASC"
    if metric == "admission_rate" and any(w in q for w in ("selective", "hardest", "toughest", "competitive")):
        return "ASC"
    is_cost = "cost" in metric
    if is_cost:
        if any(w in q for w in ("highest", "most expensive", "priciest")):
            return "DESC"
        return "ASC"
    if any(w in q for w in ("under", "below", "less than")) and is_cost:
        return "ASC"
    return "DESC"


def _is_meta_question(question: str) -> bool:
    q = question.lower()
    return any(
        phrase in q
        for phrase in (
            "how many schools",
            "how many colleges",
            "total schools",
            "total colleges",
            "count of schools",
            "schools in the database",
            "colleges in the database",
        )
    )


def _is_single_stat_question(question: str, school_patterns: list[str]) -> bool:
    q = question.lower()
    if len(school_patterns) != 1 or "compare" in q:
        return False
    return any(
        phrase in q
        for phrase in (
            "what is the", "what's the", "median earnings", "average sat",
            "acceptance rate", "admission rate", "graduation rate", "net cost", "net price",
        )
    )


def _apply_sort(parsed: ParsedQuery, question: str) -> None:
    cost_col = parsed.cost_column
    sort_col, sort_dir = _extract_sort_intent(question, cost_col)
    if sort_col:
        parsed.sort_by = sort_col
        parsed.sort_order = sort_dir
        return

    if parsed.limit is not None or _wants_ranking(question):
        inferred_col, inferred_dir = _infer_sort_from_filters(parsed)
        if inferred_col and not _detect_sort_column(question, cost_col):
            parsed.sort_by = inferred_col
            parsed.sort_order = inferred_dir
        else:
            parsed.sort_by = _detect_sort_column(question, cost_col) or "admission_rate"
            parsed.sort_order = _ranking_direction(question, parsed.sort_by)
    elif parsed.earnings_min is not None:
        parsed.sort_by = "earnings_median_10yr"
        parsed.sort_order = "DESC"


def parse_question(question: str) -> ParsedQuery:
    """Step 1: Parse natural language into structured filters."""
    q = question.lower()
    states = _extract_states(question)
    school_patterns = _extract_school_patterns(question)
    limit = _extract_limit(question)

    if _is_meta_question(question):
        return ParsedQuery(kind="meta", question=question, states=states)

    if school_patterns and ("compare" in q or len(school_patterns) > 1):
        return ParsedQuery(
            kind="schools",
            question=question,
            school_names=school_patterns,
            is_comparison=True,
        )

    if _is_single_stat_question(question, school_patterns):
        return ParsedQuery(
            kind="direct",
            question=question,
            school_names=school_patterns,
            is_single_stat=True,
            limit=1,
        )

    if school_patterns:
        return ParsedQuery(
            kind="schools",
            question=question,
            school_names=school_patterns,
        )

    parsed = ParsedQuery(
        kind="list",
        question=question,
        states=states,
        ownership=_extract_ownership(question),
        limit=limit,
    )

    _apply_acceptance_filters(parsed, question)
    _apply_graduation_filters(parsed, question)
    _apply_earnings_filters(parsed, question)
    _apply_sat_filters(parsed, question)
    _apply_cost_filters(parsed, question)
    _apply_sort(parsed, question)

    return parsed
