"""Turn result rows into display blocks (stat tiles, bars, comparisons, tables).

Every number shown is formatted here from the rows, never written by the model.
"""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

HIDDEN_COLUMNS = {"id", "school_id", "unitid", "role", "year", "latitude", "longitude", "carnegie_class", "highest_degree"}  # noqa: E501
TEXT_COLUMNS = {"name", "title", "soc_code", "city", "state", "program_name", "credential_name", "cip_code", "school_url"}  # noqa: E501
MAX_BARS = 25
MAX_COMPARE_SCHOOLS = 6
MIN_COMPARE_METRICS = 2
MAX_DEADLINE_SCHOOLS = 3  # more schools than this share one table

YES_NO = {True: "Yes", False: "No"}
DEADLINE_LABELS = {
    "early_decision": "Early Decision", "early_decision_2": "Early Decision II", "early_action": "Early Action",
    "restrictive_early_action": "Restrictive Early Action", "regular": "Regular Decision",
    "rolling": "Rolling admission", "priority": "Priority deadline",
}
OWNERSHIP_LABELS = {1: "Public", 2: "Private nonprofit", 3: "Private for-profit"}

# column -> (label, format, better): better is "higher", "lower" or None
COLUMNS = {
    "name": ("School", "text", None),
    "city": ("City", "text", None),
    "state": ("State", "text", None),
    "ownership": ("Type", "ownership", None),
    "admission_rate": ("Acceptance rate", "percent", None),
    "sat_average": ("Average SAT", "int", None),
    "act_median": ("Median ACT", "int", None),
    "cost_avg_net_price": ("Average net price", "money", "lower"),
    "cost_net_price_income_0_30k": ("Net price, income $0-30k", "money", "lower"),
    "cost_net_price_income_30_48k": ("Net price, income $30-48k", "money", "lower"),
    "cost_net_price_income_48_75k": ("Net price, income $48-75k", "money", "lower"),
    "cost_net_price_income_75_110k": ("Net price, income $75-110k", "money", "lower"),
    "cost_net_price_income_110k_plus": ("Net price, income $110k+", "money", "lower"),
    "completion_rate": ("Graduation rate", "percent", "higher"),
    "retention_rate": ("Retention rate", "percent", "higher"),
    "transfer_rate": ("Transfer rate", "percent", None),
    "earnings_median_10yr": ("Median earnings (10 yrs)", "money", "higher"),
    "earnings_6yr": ("Median earnings (6 yrs)", "money", "higher"),
    "earnings_8yr": ("Median earnings (8 yrs)", "money", "higher"),
    "earnings_median_1yr": ("Median earnings (1 yr after)", "money", "higher"),
    "earnings_median_4yr": ("Median earnings (4 yrs after)", "money", "higher"),
    "student_size": ("Students", "int", None),
    "undergraduate_size": ("Undergraduates", "int", None),
    "pell_grant_rate": ("Pell grant recipients", "percent", None),
    "federal_loan_rate": ("Students with federal loans", "percent", None),
    "avg_loan_amount": ("Average loan", "money", "lower"),
    "debt_median": ("Median debt", "money", "lower"),
    "debt_monthly_payment": ("Monthly loan payment", "money", "lower"),
    "loan_repayment_rate": ("Loan repayment rate", "percent", "higher"),
    "first_gen_rate": ("First-generation students", "percent", None),
    "part_time_rate": ("Part-time students", "percent", None),
    "female_share": ("Women", "percent", None),
    "distance_miles": ("Distance", "miles", None),
    "value_ratio": ("Earnings per $1 of net price", "number", "higher"),
    "copilot_score": ("CollegeCopilot Score", "score100", "higher"),
    "copilot_rank": ("CollegeCopilot rank", "rank", None),
    "criminal_offenses_per_1000_students": ("Crimes per 1,000 students", "number", "lower"),
    "similarity": ("Similarity", "score", "higher"),
    "similar_on": ("Similar on", "list", None),
}


def _humanize(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def column_meta(key: str, sample=None) -> tuple[str, str, str | None]:
    if key in COLUMNS:
        return COLUMNS[key]
    if isinstance(sample, bool):
        return _humanize(key), "yesno", None
    if isinstance(sample, date):
        return _humanize(key), "date", None
    if key in TEXT_COLUMNS or isinstance(sample, str):
        return _humanize(key), "text", None
    if isinstance(sample, list):
        return _humanize(key), "list", None
    if re.search(r"rate|share|pct|percent", key):
        return _humanize(key), "percent", None
    if re.search(r"price|cost|earning|salary|debt|loan|amount|payment|tuition", key):
        return _humanize(key), "money", None
    if re.search(r"count|number|total|size", key):
        return _humanize(key), "int", None
    return _humanize(key), "number", None


def _to_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def format_value(value, fmt: str) -> str:
    if value is None:
        return "Not reported"
    if fmt == "list":
        return ", ".join(str(v) for v in value) if value else "-"
    if fmt == "ownership":
        return OWNERSHIP_LABELS.get(value, str(value))
    if fmt == "yesno":
        return YES_NO.get(value, str(value))
    if fmt == "date" and isinstance(value, date):
        return f"{value:%B} {value.day}, {value.year}"
    number = _to_float(value)
    if fmt == "text" or number is None:
        return str(value)
    if fmt == "percent":
        # Values above 1 are already percentages (e.g. a model computed rate * 100).
        pct = Decimal(str(value)) * (100 if abs(number) <= 1 else 1)
        return f"{pct.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
    if fmt == "money":
        sign = "-" if number < 0 else ""
        return f"{sign}${abs(number):,.0f}"
    if fmt == "int":
        return f"{number:,.0f}"
    if fmt == "miles":
        return f"{number:,.1f} mi"
    if fmt == "score":
        return f"{number * 100:.0f}%"
    if fmt == "score100":
        return f"{number:.1f}"
    if fmt == "rank":
        return f"#{number:,.0f}"
    return f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"


def _all_keys(rows: list[dict]) -> list[str]:
    return list(dict.fromkeys(k for row in rows for k in row))


def _visible_columns(rows: list[dict], highlight: str | None) -> list[str]:
    keys = [k for k in _all_keys(rows) if k not in HIDDEN_COLUMNS]
    if len({r.get("year") for r in rows}) > 1:
        keys.insert(0, "year")
    ordered = [k for k in ("name", "title", "state") if k in keys]
    if highlight in keys and highlight not in ordered:
        ordered.append(highlight)
    return ordered + [k for k in keys if k not in ordered]


def _metric_columns(rows: list[dict], columns: list[str]) -> list[str]:
    metrics = []
    for key in columns:
        sample = next((r[key] for r in rows if r.get(key) is not None), None)
        _, fmt, _ = column_meta(key, sample)
        if fmt not in ("text", "list", "ownership", "yesno", "date") and sample is not None:
            metrics.append(key)
    return metrics


def _school_id(row: dict) -> int | None:
    """The school a row describes (for the Save button), or None for non-school rows."""
    value = row.get("id", row.get("school_id", row.get("unitid")))
    return value if isinstance(value, int) and row.get("name") else None


def _label_for_row(row: dict) -> str:
    return str(row.get("name") or row.get("program_name") or row.get("title") or row.get("school_type") or row.get("state") or "Result")  # noqa: E501


def _stats_block(row: dict, columns: list[str]) -> dict:
    items = []
    for key in columns:
        if key in ("name",):
            continue
        label, fmt, _ = column_meta(key, row.get(key))
        items.append({"label": label, "display": format_value(row.get(key), fmt)})
    return {"type": "stats", "title": row.get("name"), "school_id": _school_id(row), "items": items}


def _bars_block(rows: list[dict], metric: str) -> dict | None:
    values = [r for r in rows if _to_float(r.get(metric)) is not None]
    if len(values) < 2 or len(values) > MAX_BARS:
        return None
    label, fmt, _ = column_meta(metric, values[0][metric])
    numbers = [_to_float(r[metric]) for r in values]
    if min(numbers) < 0:
        return None
    return {
        "type": "bars",
        "title": label,
        "max": max(numbers) or 1,
        "items": [
            {"label": _label_for_row(r), "school_id": _school_id(r), "value": _to_float(r[metric]),
             "display": format_value(r[metric], fmt)}
            for r in values
        ],
    }


def _compare_block(rows: list[dict], metrics: list[str]) -> dict:
    out_rows = []
    for key in metrics:
        sample = next((r[key] for r in rows if r.get(key) is not None), None)
        label, fmt, better = column_meta(key, sample)
        numbers = [_to_float(r.get(key)) for r in rows]
        present = [n for n in numbers if n is not None]
        best = None
        if better and len(present) >= 2 and len(set(present)) > 1:
            best = max(present) if better == "higher" else min(present)
        out_rows.append({
            "label": label,
            "cells": [
                {"display": format_value(r.get(key), fmt), "best": best is not None and n == best}
                for r, n in zip(rows, numbers)
            ],
        })
    return {
        "type": "compare",
        "schools": [_label_for_row(r) for r in rows],
        "school_ids": [_school_id(r) for r in rows],
        "rows": out_rows,
    }


def _table_block(rows: list[dict], columns: list[str]) -> dict:
    metas = []
    for key in columns:
        sample = next((r[key] for r in rows if r.get(key) is not None), None)
        metas.append((key, *column_meta(key, sample)))
    return {
        "type": "table",
        "columns": [{"label": label, "numeric": fmt not in ("text", "list", "ownership", "yesno", "date")}
                    for _, label, fmt, _ in metas],
        "rows": [
            {
                "reference": r.get("role") == "reference",
                "school_id": _school_id(r),
                "cells": [format_value(r.get(key), fmt) for key, _, fmt, _ in metas],
            }
            for r in rows
        ],
    }


def _deadline_type(row: dict) -> str:
    kind = row.get("deadline_type")
    label = DEADLINE_LABELS.get(kind, _humanize(str(kind or "Deadline")))
    notes = str(row.get("notes") or "").lower()
    return f"{label} (binding)" if "binding" in notes and "non-binding" not in notes else label


def _deadline_date(row: dict) -> str:
    value = row.get("deadline_date")
    if value is None:
        return "Rolling" if row.get("deadline_type") == "rolling" else "Not reported"
    return format_value(value, "date")


def _by_date(row: dict) -> tuple:
    return row.get("deadline_date") is None, row.get("deadline_date") or date.min


def _deadline_blocks(rows: list[dict]) -> list[dict]:
    """Application deadlines: a "Deadline Type | Date" table per school, or one table for many schools."""
    groups: dict = {}
    for row in rows:
        key = row.get("id", row.get("school_id", row.get("unitid"))) or row.get("name")
        groups.setdefault(key, []).append(row)
    if len(groups) > MAX_DEADLINE_SCHOOLS:
        return [{
            "type": "table",
            "columns": [{"label": label, "numeric": False} for label in ("School", "Deadline Type", "Date")],
            "rows": [{"reference": False, "school_id": _school_id(r),
                      "cells": [str(r.get("name") or "Not reported"), _deadline_type(r), _deadline_date(r)]}
                     for r in rows],
        }]
    return [
        {
            "type": "deadlines",
            "title": school_rows[0].get("name"),
            "school_id": _school_id(school_rows[0]),
            "rows": [{"type": _deadline_type(r), "date": _deadline_date(r)} for r in sorted(school_rows, key=_by_date)],
        }
        for school_rows in groups.values()
    ]


def pick_highlight(rows: list[dict], requested: str | None) -> str | None:
    if not rows:
        return None
    metrics = _metric_columns(rows, [k for k in _all_keys(rows) if k not in HIDDEN_COLUMNS])
    if requested in metrics:
        return requested
    if "similarity" in metrics:
        return "similarity"
    return metrics[0] if metrics else None


def build_blocks(rows: list[dict], highlight: str | None = None) -> list[dict]:
    if not rows:
        return []
    if all({"deadline_type", "deadline_date"} <= r.keys() for r in rows):
        return _deadline_blocks(rows)
    highlight = pick_highlight(rows, highlight)
    columns = _visible_columns(rows, highlight)
    metrics = _metric_columns(rows, columns)

    if len(rows) == 1:
        return [_stats_block(rows[0], columns)]

    is_similarity = any(r.get("role") == "reference" for r in rows)
    if not is_similarity and len(rows) <= MAX_COMPARE_SCHOOLS and len(metrics) >= MIN_COMPARE_METRICS:
        return [_compare_block(rows, metrics)]

    blocks = []
    if highlight in metrics:
        bars = _bars_block(rows, highlight)
        if bars:
            blocks.append(bars)
    blocks.append(_table_block(rows, columns))
    return blocks


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?:k\b|\s(?:million|billion)\b)?", re.IGNORECASE)
_MULTIPLIERS = {"k": 1e3, "million": 1e6, "billion": 1e9}


def _canonical(number: float) -> str:
    return f"{abs(number):.2f}".rstrip("0").rstrip(".")


def _half_up(value, scale: int, digits: int) -> float:
    exponent = Decimal(1).scaleb(-digits)
    return float((Decimal(str(value)) * scale).quantize(exponent, rounding=ROUND_HALF_UP))


def _number_forms(value) -> set[str]:
    """Ways a number may appear in prose: raw, rounded, and as a percentage."""
    number = _to_float(value)
    if number is None:
        return set()
    candidates = [number, round(number), round(number, 1), _half_up(value, 1, 0), _half_up(value, 1, 1)]
    if abs(number) <= 1:
        pct = number * 100
        # Half-up like format_value: 0.0455 shows as 4.6%, which float round() would make 4.5.
        candidates += [pct, round(pct), round(pct, 1), _half_up(value, 100, 0), _half_up(value, 100, 1)]
    for scale in (1e6, 1e9):
        if abs(number) >= scale:
            candidates += [round(number / scale, digits) * scale for digits in (0, 1, 2)]
    return {_canonical(c) for c in candidates}


def _tokens(text: str) -> list[tuple[str, str]]:
    """(token, normalized value) for each number in text; "60k" is 60,000, "$2.5 billion" 2.5e9."""
    tokens = []
    for token in _NUMBER_RE.findall(text):
        digits, suffix = re.match(r"([\d,.]+)\s?([a-z]*)", token, re.IGNORECASE).group(1, 2)
        number = float(digits.replace(",", "")) * _MULTIPLIERS.get(suffix.lower(), 1)
        tokens.append((token, _canonical(number)))
    return tokens


def unsupported_numbers(summary: str, rows: list[dict], context: str) -> list[str]:
    """Numbers in the summary that don't appear in the rows or the context text."""
    # Numbers inside column names and labels ("10 yrs", "income $48-75k") are allowed too.
    keys = _all_keys(rows)
    labels = " ".join(keys + [column_meta(k)[0] for k in keys])
    allowed = {str(len(rows))} | {norm for _, norm in _tokens(f"{context} {labels}")}
    for row in rows:
        for value in row.values():
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, str) or hasattr(v, "isoformat"):  # text, dates
                    allowed |= {norm for _, norm in _tokens(str(v))}
                else:
                    allowed |= _number_forms(v)
    return [token for token, norm in _tokens(summary) if norm not in allowed]


# ---------- attribution: is each number credited to the right school? ----------

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_CLAUSE_RE = re.compile(r";\s*(?:however,?\s*)?|,\s*(?:while|whereas|but|and|followed by)\s+|\s+while\s+|\s+whereas\s+",
                        re.IGNORECASE)
_COMPARISON_RE = re.compile(r"\b(vs\.?|versus|compared (?:to|with)|than|relative to)\b", re.IGNORECASE)
_SUPERLATIVE_RE = re.compile(
    r"\b(most expensive|least expensive|most affordable|most selective|least selective|"
    r"highest|lowest|most|least|cheapest|largest|smallest|biggest|fewest|greatest)\b", re.IGNORECASE)
_MIN_WORDS = {"lowest", "least", "cheapest", "smallest", "fewest", "least expensive", "most affordable",
              "most selective"}
_MAX_WORDS = {"highest", "most", "largest", "biggest", "greatest", "most expensive", "least selective"}
# "More selective" means a lower acceptance rate; multi-word phrases are matched before single words.
_HIGHER_PHRASES = ["more expensive", "less affordable", "less selective"]
_LOWER_PHRASES = ["more affordable", "less expensive", "more selective"]
_HIGHER_WORDS = ["higher", "more", "greater", "larger", "bigger", "pricier"]
_LOWER_WORDS = ["lower", "less", "fewer", "smaller", "cheaper"]
_HIGHER_RE = re.compile("|".join(_HIGHER_PHRASES + _HIGHER_WORDS), re.IGNORECASE)
_COMPARATIVE_RE = re.compile(r"\b(" + "|".join(_HIGHER_PHRASES + _LOWER_PHRASES + _HIGHER_WORDS + _LOWER_WORDS)
                             + r")\b", re.IGNORECASE)
_ORDINAL_BEFORE_RE = re.compile(r"\b(second|third|fourth|fifth|next)[\s-]*$", re.IGNORECASE)
_NUMBER_TEXT = r"\$?\d[\d,]*(?:\.\d+)?%?(?:k\b|\s(?:million|billion)\b)?"
_PAIR_RE = re.compile(rf"({_NUMBER_TEXT})\)?,?\s*(?:vs\.?|versus|compared (?:to|with)|than)\s+"
                      rf"[^\d$]{{0,40}}?({_NUMBER_TEXT})", re.IGNORECASE)
_NAME_SUFFIXES = (" University", " College", " Institute of Technology")
_NAME_PREFIXES = ("University of ", "The ")


def _name_variants(name: str) -> set[str]:
    """Ways a summary may name a school: full name, campus, short name ("Harvard", "Berkeley", "UC Berkeley")."""
    variants = {name}
    parts = [p.strip() for p in name.split("-") if p.strip()]
    if len(parts) > 1:
        variants |= {parts[0], parts[-1]}
        if parts[0] == "University of California":
            variants.add(f"UC {parts[-1]}")
    for base in list(variants):
        for prefix in _NAME_PREFIXES:
            if base.startswith(prefix) and len(base) > len(prefix) + 2:
                variants.add(base[len(prefix):])
        for suffix in _NAME_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix) + 2:
                variants.add(base[: -len(suffix)])
    return {v for v in variants if len(v) >= 3}


def _entities(rows: list[dict], aliases: dict[int, list[str]] | None) -> list[tuple[int, re.Pattern]]:
    """(row index, pattern matching any unambiguous name of that row's school)."""
    names: dict[int, set[str]] = {}
    for i, row in enumerate(rows):
        if not row.get("name"):
            continue
        variants = _name_variants(str(row["name"]))
        school_id = row.get("id", row.get("school_id"))
        variants |= {a for a in (aliases or {}).get(school_id, []) if len(a) >= 2}
        names[i] = variants
    counts: dict[str, int] = {}
    for variants in names.values():
        for v in {x.lower() for x in variants}:
            counts[v] = counts.get(v, 0) + 1
    entities = []
    for i, variants in names.items():
        unique = sorted((v for v in variants if counts[v.lower()] == 1), key=len, reverse=True)
        if unique:
            entities.append((i, re.compile(r"(?<![\w])(" + "|".join(map(re.escape, unique)) + r")(?![\w])",
                                           re.IGNORECASE)))
    return entities


def _row_forms(row: dict) -> dict[str, set[str]]:
    """Each numeric column of a row -> the ways its value may be written."""
    return {k: _number_forms(v) for k, v in row.items() if _to_float(v) is not None}


def _extreme_ok(rows: list[dict], row: dict, column: str, word: str) -> bool:
    values = [_to_float(r.get(column)) for r in rows]
    values = [v for v in values if v is not None]
    mine = _to_float(row.get(column))
    if mine is None or len(values) < 2:
        return True
    return mine == (min(values) if word in _MIN_WORDS else max(values))


def attribution_problems(summary: str, rows: list[dict], context: str = "",
                         aliases: dict[int, list[str]] | None = None) -> list[str]:
    """Problems with who the summary credits each number to; empty when it is consistent with the rows.

    The summary is split into sentences and clauses. A clause that names schools may only cite numbers
    from those schools' rows (a clause naming none continues the previous clause's subject; a "vs" clause
    may also use the sentence's other schools). Superlatives ("highest graduation rate at 93.2%") must be
    true for the school holding that value, and "higher/lower ... A vs B" must match the order of A and B.
    Numbers from the question, column labels and small counts are always allowed.
    """
    entities = _entities(rows, aliases)
    if len(entities) < 2:
        return []  # nothing to confuse
    keys = _all_keys(rows)
    free = {norm for _, norm in _tokens(f"{context} {' '.join(keys + [column_meta(k)[0] for k in keys])}")}
    free |= {str(n) for n in range(0, max(len(rows), 10) + 1)}
    forms = [_row_forms(r) for r in rows]
    problems = []
    for sentence in _SENTENCE_RE.split(summary.strip()):
        clauses = [c for c in _CLAUSE_RE.split(sentence) if c and c.strip()]
        in_sentence = {i for i, pattern in entities if pattern.search(sentence)}
        subject: set[int] = set()
        for clause in clauses:
            named = {i for i, pattern in entities if pattern.search(clause)}
            subject = named or subject
            allowed = subject | (in_sentence if _COMPARISON_RE.search(clause) else set())
            if not allowed:
                continue
            numbers = [(token, norm) for token, norm in _tokens(clause) if norm not in free]
            for token, norm in numbers:
                if not any(norm in f for i in allowed for f in forms[i].values()):
                    problems.append(f"{token} is not a value of the school(s) named in: {clause.strip()!r}")
            # Superlatives: the first number after "highest"/"lowest"/... must be the extreme of its column.
            for match in _SUPERLATIVE_RE.finditer(clause):
                if _ORDINAL_BEFORE_RE.search(clause[:match.start()]):
                    continue  # "second-highest" ranks, it doesn't claim the top
                word = match.group(1).lower()
                after = [norm for _, norm in _tokens(clause[match.end():]) if norm not in free][:1]
                if not after:
                    # "X is the most selective" with no number: check the acceptance rate itself.
                    if word.endswith("selective") and named and "admission_rate" in keys and \
                            not any(_extreme_ok(rows, rows[i], "admission_rate", word) for i in named):
                        problems.append(f"'{word}' is not true for the school named in: {clause.strip()!r}")
                    continue
                holders = [(i, col) for i in allowed for col, f in forms[i].items() if after[0] in f]
                if holders and not any(_extreme_ok(rows, rows[i], col, word) for i, col in holders):
                    problems.append(f"'{word}' is not true for {after[0]} in: {clause.strip()!r}")
            # "higher ... A vs B": A must be larger than B (and the reverse for "lower").
            for pair in _PAIR_RE.finditer(clause):
                words = list(_COMPARATIVE_RE.finditer(clause[:pair.start()]))
                a, b = (_tokens(pair.group(g)) for g in (1, 2))
                if not words or not a or not b or a[0][1] == b[0][1]:
                    continue
                says_higher = bool(_HIGHER_RE.fullmatch(words[-1].group(1)))
                if (float(a[0][1]) > float(b[0][1])) != says_higher:
                    problems.append(f"'{words[-1].group(1)}' contradicts {pair.group(0)!r} in: {clause.strip()!r}")
    return problems
