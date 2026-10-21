"""
Load IRS Form 990 finances of private nonprofit colleges into irs_990 (one row per school).

Every IPEDS school with CONTROL = 2 (private nonprofit) and a 9-digit EIN in the
directory file (HD2024, see load_ipeds.py) is looked up in the ProPublica Nonprofit
Explorer API, and the most recent filing that has extracted financial data is kept.
Many campuses of one institution share an EIN: each campus gets a row with the same
(institution-wide) numbers and shared_ein = true.

Raw API responses are cached in data/irs990/<EIN>.json, so reruns are fast and an
interrupted run resumes where it stopped. Requests are throttled to ~3 per second.

Usage (from backend/):
    python load_irs990.py
"""

import json
import time
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, download, read_csv, replace_table

load_dotenv(ROOT_DIR / ".env")

CACHE_DIR = DATA_DIR / "irs990"
API_URL = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
HD_URL = "https://nces.ed.gov/ipeds/datacenter/data/HD2024.zip"
SOURCE = {"name": "Data from ProPublica Nonprofit Explorer (IRS Form 990)",
          "url": "https://projects.propublica.org/nonprofits/api"}
MIN_INTERVAL = 0.34  # seconds between requests (~3 per second)

# IPEDS reports placeholder EINs for a few schools; these are their actual EINs.
EIN_OVERRIDES = {
    190150: "135598093",  # Columbia University (IPEDS: 603800000)
    195030: "160743209",  # University of Rochester (IPEDS: 263800000)
}

FORM_TYPES = {0: "990", 1: "990-EZ", 2: "990-PF"}

COLUMNS = [
    # (column, sql type, description, ProPublica filing field)
    ("ein", "TEXT", "IRS Employer Identification Number (9 digits) the filing belongs to", None),
    ("organization_name", "TEXT", "Organization name on the IRS filing (often the legal name, e.g. 'President And Fellows Of Harvard College')", None),
    ("shared_ein", "BOOLEAN", "True when several IPEDS campuses share this EIN, so the figures cover the whole institution or system, not just this campus", None),
    ("campuses_sharing_ein_count", "INTEGER", "Number of IPEDS campuses that share this EIN (1 = only this school)", None),
    ("tax_year", "INTEGER", "Tax year of the latest filing with financial data (mostly 2023; a few schools only have older filings, so check it). The fiscal year usually ends in this year or the next", "tax_prd_yr"),
    ("fiscal_year_end", "TEXT", "End of the fiscal year covered by the filing, as YYYY-MM", "tax_prd"),
    ("form_type", "TEXT", "IRS form filed: '990', '990-EZ' or '990-PF'", "formtype"),
    ("total_revenue_amount", "BIGINT", "Total revenue for the year in dollars (Form 990 Part I line 12)", "totrevenue"),
    ("total_expenses_amount", "BIGINT", "Total functional expenses for the year in dollars", "totfuncexpns"),
    ("total_assets_amount", "BIGINT", "Total assets at year end in dollars (includes the endowment)", "totassetsend"),
    ("total_liabilities_amount", "BIGINT", "Total liabilities at year end in dollars", "totliabend"),
    ("net_assets_amount", "BIGINT", "Net assets (fund balance) at year end in dollars", "totnetassetend"),
    ("contributions_amount", "BIGINT", "Contributions, gifts and grants received during the year in dollars", "totcntrbgfts"),
    ("program_service_revenue_amount", "BIGINT", "Program service revenue in dollars (mostly tuition and fees, plus e.g. hospital or research revenue)", "totprgmrevnue"),
    ("investment_income_amount", "BIGINT", "Investment income (interest, dividends) in dollars; excludes realized gains", "invstmntinc"),
    ("net_gain_on_assets_amount", "BIGINT", "Net gain or loss from sales of securities and other assets in dollars (can be negative)", "netgnls"),
    ("officer_compensation_amount", "BIGINT", "Compensation of current officers, directors, trustees and key employees in dollars", "compnsatncurrofcr"),
    ("other_salaries_amount", "BIGINT", "All other salaries and wages in dollars", "othrsalwages"),
    ("tax_exempt_bonds_debt_amount", "BIGINT", "Tax-exempt bond liabilities outstanding at year end in dollars", "txexmptbndsend"),
    ("filing_pdf_url", "TEXT", "Link to the filing (PDF) on ProPublica, when available", "pdf_url"),
]


def eins_to_unitids() -> dict[str, list[int]]:
    hd = read_csv(download(HD_URL, DATA_DIR / "ipeds" / "HD2024.zip"))
    by_ein = defaultdict(list)
    for row in hd:
        ein = EIN_OVERRIDES.get(int(row["UNITID"]), row["EIN"])
        if row["CONTROL"] == "2" and len(ein) == 9 and ein.isdigit() and int(ein) > 0:
            by_ein[ein].append(int(row["UNITID"]))
    return by_ein


_last_request = 0.0


def fetch(session: requests.Session, ein: str) -> dict | None:
    """The API response for ein (cached), or None when ProPublica has no such organization."""
    global _last_request
    path = CACHE_DIR / f"{ein}.json"
    if path.exists():
        data = json.loads(path.read_text())
        return data or None
    for attempt in range(6):
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            response = session.get(API_URL.format(ein=ein), timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 404:
            path.write_text("{}")  # cache misses too, so reruns skip them
            return None
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(int(response.headers.get("Retry-After", 0) or 0) or 2 ** (attempt + 1))
            continue
        response.raise_for_status()
        path.write_text(response.text)
        return response.json()
    print(f"  giving up on EIN {ein}")
    return None


def latest_filing(data: dict) -> dict | None:
    filings = [f for f in data.get("filings_with_data") or [] if f.get("tax_prd")]
    return max(filings, key=lambda f: f["tax_prd"]) if filings else None


def field_value(filing: dict, field: str):
    raw = filing.get(field)
    if field == "tax_prd":
        return f"{str(raw)[:4]}-{str(raw)[4:6]}" if raw else None
    if field == "formtype":
        return FORM_TYPES.get(raw)
    if field == "pdf_url":
        return raw or None
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    by_ein = eins_to_unitids()
    print(f"  {len(by_ein):,} EINs for {sum(map(len, by_ein.values())):,} private nonprofit schools")

    session = requests.Session()
    session.headers["User-Agent"] = "CollegeCopilot data loader"
    rows, missing, no_data = [], 0, 0
    for i, (ein, unitids) in enumerate(sorted(by_ein.items()), 1):
        data = fetch(session, ein)
        if i % 100 == 0:
            print(f"  {i:,}/{len(by_ein):,} EINs fetched", flush=True)
        if not data:
            missing += 1
            continue
        filing = latest_filing(data)
        if not filing:
            no_data += 1
            continue
        name = (data.get("organization") or {}).get("name")
        for unitid in unitids:
            values = [ein, name, len(unitids) > 1, len(unitids)]
            values += [field_value(filing, field) for _, _, _, field in COLUMNS[4:]]
            rows.append((unitid, *values))
    print(f"  {missing} EINs not found on ProPublica, {no_data} found without financial data")

    years = Counter(r[5] for r in rows if r[5])
    year = f"mostly tax year {years.most_common(1)[0][0]} (latest filing per school)" if years else None
    with get_db_connection() as conn:
        replace_table(
            conn, "irs_990",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")]
            + [c[:3] for c in COLUMNS],
            rows,
            "IRS Form 990 finances of private nonprofit colleges, latest filing with data per EIN. "
            "Data from ProPublica Nonprofit Explorer (IRS Form 990). All amounts in dollars. When "
            "shared_ein is true the figures are for the whole multi-campus institution.",
            {**SOURCE, "year": year},
        )


if __name__ == "__main__":
    main()
