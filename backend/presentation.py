"""Turn result rows into display blocks (stat tiles, bars, comparisons, tables).

Every number shown is formatted here from the rows, never written by the model.
"""

import re
from decimal import ROUND_HALF_UP, Decimal

HIDDEN_COLUMNS = {"id", "school_id", "role", "year", "latitude", "longitude", "carnegie_class", "highest_degree"}
TEXT_COLUMNS = {"name", "title", "soc_code", "city", "state", "program_name", "credential_name", "cip_code", "school_url"}
MAX_BARS = 25
MAX_COMPARE_SCHOOLS = 6
MIN_COMPARE_METRICS = 2

YES_NO = {True: "Yes", False: "No"}
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
        if fmt not in ("text", "list", "ownership", "yesno") and sample is not None:
            metrics.append(key)
    return metrics


def _school_id(row: dict) -> int | None:
    """The school a row describes (for the Save button), or None for non-school rows."""
    value = row.get("id", row.get("school_id"))
    return value if isinstance(value, int) and row.get("name") else None


def _label_for_row(row: dict) -> str:
    return str(row.get("name") or row.get("program_name") or row.get("title") or row.get("state") or "Result")


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
        "columns": [{"label": label, "numeric": fmt not in ("text", "list", "ownership", "yesno")}
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


def _number_forms(value) -> set[str]:
    """Ways a number may appear in prose: raw, rounded, and as a percentage."""
    number = _to_float(value)
    if number is None:
        return set()
    candidates = [number, round(number), round(number, 1)]
    if abs(number) <= 1:
        pct = number * 100
        candidates += [pct, round(pct), round(pct, 1)]
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
