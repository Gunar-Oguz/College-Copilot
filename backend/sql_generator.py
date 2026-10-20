import os

import anthropic

from database import get_latest_year, get_schema

CANNOT_ANSWER = "CANNOT_ANSWER"

COLUMN_NOTES = """
- schools has one row per (id, year). id is the IPEDS UNITID. Names are NOT unique.
- Rates (admission_rate, completion_rate, retention_rate, pell_grant_rate, etc.) are decimals 0-1, e.g. 0.25 = 25%.
- ownership: 1 = public, 2 = private nonprofit, 3 = private for-profit.
- cost_avg_net_price is average annual net price. cost_net_price_income_* are net price by family income bracket:
  0_30k ($0-30,000), 30_48k ($30,001-48,000), 48_75k ($48,001-75,000), 75_110k ($75,001-110,000), 110k_plus ($110,001+).
- earnings_median_10yr is median earnings 10 years after entry, in dollars.
- state is a 2-letter code. Many columns are NULL for schools that did not report them.
- programs joins to schools on (programs.school_id = schools.id AND programs.year = schools.year).
  credential_level: 1 = undergraduate certificate, 2 = associate, 3 = bachelor's, 4 = post-bacc certificate,
  5 = master's, 6 = doctoral, 7 = first professional, 8 = graduate certificate.
""".strip()

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def build_prompt(question: str, schema: str, latest_year: str) -> str:
    return f"""Write one PostgreSQL SELECT query that answers the question.

SCHEMA:
{schema}

COLUMN NOTES:
{COLUMN_NOTES}

RULES:
- Filter schools.year = '{latest_year}' unless the question asks about other years or trends.
- Always select schools.id and schools.name, plus every column used to filter or sort.
- When filtering or sorting on a column, exclude rows where it is NULL.
- Add LIMIT 50 unless the question asks for a specific number or a count.
- Match school names with ILIKE using the official name (e.g. 'University of California-Los Angeles' for UCLA).
- If the question cannot be answered from these tables, return exactly {CANNOT_ANSWER}.

QUESTION: "{question}"

Return ONLY the SQL query or {CANNOT_ANSWER}. No explanation, no markdown."""


def _strip_markdown(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(line for line in lines[1:] if not line.startswith("```"))
    return text.strip()


def generate_sql(question: str) -> str:
    """Return a SQL string, or CANNOT_ANSWER if the data can't answer the question."""
    prompt = build_prompt(question, get_schema(), get_latest_year())
    message = _get_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _strip_markdown(message.content[0].text)
