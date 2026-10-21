"""
Load Equity in Athletics (EADA, ope.ed.gov/athletics) data for the 2024-25 school year.

- athletics: one row per school (instLevel file): association/division, team and athlete
  counts by sex, athletic scholarships, revenue and expenses, coach salaries, plus Title IX
  comparisons (women's share of athletes and of athletic aid vs. of undergraduates).
- athletics_sports: one row per school and sport (schools file): participants by sex,
  revenue and expenses.

EADA reports full-time undergraduates only (EFMaleCount/EFFemaleCount). Dollar amounts are
as reported by the school for the 2024-25 reporting year; many schools report revenue equal
to expenses, so "profit" comparisons are unreliable. Coed-team numbers are folded into the
men's/women's totals where EADA provides combined fields.

Usage (from backend/):
    python load_athletics.py
"""

import io
import zipfile
from collections import defaultdict

import openpyxl
from dotenv import load_dotenv

from database import get_db_connection
from source_data import DATA_DIR, ROOT_DIR, download, num, replace_table

load_dotenv(ROOT_DIR / ".env")

URL = "https://ope.ed.gov/athletics/api/dataFiles/file?fileName=EADA_2024-2025.zip"
ZIP_PATH = DATA_DIR / "athletics" / "EADA_2024-2025.zip"
SOURCE = {"name": "Equity in Athletics Data Analysis (EADA), U.S. Department of Education",
          "url": "https://ope.ed.gov/athletics/", "year": "2024-25"}

COLUMNS = [
    ("classification", "TEXT", "Athletic association/division, e.g. 'NCAA Division I-FBS', 'NCAA Division III with football', 'NAIA Division I', 'NJCAA Division I'"),
    ("sports_count", "INTEGER", "Number of varsity sports with at least one participant"),
    ("men_teams_count", "INTEGER", "Sports with a men's (or coed men) team"),
    ("women_teams_count", "INTEGER", "Sports with a women's (or coed women) team"),
    ("men_athletes_count", "INTEGER", "Unduplicated men varsity athletes (each person counted once even if on several teams)"),
    ("women_athletes_count", "INTEGER", "Unduplicated women varsity athletes"),
    ("athletes_total", "INTEGER", "Unduplicated varsity athletes, men + women"),
    ("fulltime_undergrad_count", "INTEGER", "Full-time undergraduates (EADA enrollment base)"),
    ("women_athletes_share", "NUMERIC", "Women's share of varsity athletes (Title IX participation comparison; decimal)"),
    ("undergrad_women_share", "NUMERIC", "Women's share of full-time undergraduates, to compare with women_athletes_share"),
    ("athletic_aid_men_amount", "BIGINT", "Athletically related student aid (scholarships) to men's teams, dollars"),
    ("athletic_aid_women_amount", "BIGINT", "Athletically related student aid to women's teams, dollars"),
    ("athletic_aid_total_amount", "BIGINT", "Total athletically related student aid incl. coed teams, dollars"),
    ("women_athletic_aid_share", "NUMERIC", "Women's share of athletic aid (women / (men + women), excluding coed teams); compare with women_athletes_share"),
    ("total_revenue_amount", "BIGINT", "Grand total athletics revenue, dollars (incl. revenue not allocated to a sport)"),
    ("total_expenses_amount", "BIGINT", "Grand total athletics expenses, dollars (incl. expenses not allocated to a sport)"),
    ("mens_teams_expenses_amount", "BIGINT", "Expenses of men's and coed-men's teams, dollars"),
    ("womens_teams_expenses_amount", "BIGINT", "Expenses of women's and coed-women's teams, dollars"),
    ("football_revenue_amount", "BIGINT", "Football revenue, dollars (null if no football team)"),
    ("football_expenses_amount", "BIGINT", "Football expenses, dollars"),
    ("mens_basketball_revenue_amount", "BIGINT", "Men's basketball revenue, dollars"),
    ("mens_basketball_expenses_amount", "BIGINT", "Men's basketball expenses, dollars"),
    ("womens_basketball_revenue_amount", "BIGINT", "Women's basketball revenue, dollars"),
    ("womens_basketball_expenses_amount", "BIGINT", "Women's basketball expenses, dollars"),
    ("recruiting_expenses_amount", "BIGINT", "Total recruiting expenses, dollars"),
    ("recruiting_men_amount", "BIGINT", "Recruiting expenses for men's teams, dollars"),
    ("recruiting_women_amount", "BIGINT", "Recruiting expenses for women's teams, dollars"),
    ("head_coach_avg_salary_mens_teams", "INTEGER", "Average annual salary per head coach of men's teams, dollars"),
    ("head_coach_avg_salary_womens_teams", "INTEGER", "Average annual salary per head coach of women's teams, dollars"),
    ("assistant_coach_avg_salary_mens_teams", "INTEGER", "Average annual salary per assistant coach of men's teams, dollars"),
    ("assistant_coach_avg_salary_womens_teams", "INTEGER", "Average annual salary per assistant coach of women's teams, dollars"),
]

SPORT_COLUMNS = [
    ("sport", "TEXT", "Sport name as in EADA, e.g. 'Football', 'Basketball', 'Soccer', 'Track and Field (Outdoor)'"),
    ("men_participants_count", "INTEGER", "Men participants (men's + coed men)"),
    ("women_participants_count", "INTEGER", "Women participants (women's + coed women)"),
    ("men_revenue_amount", "BIGINT", "Revenue of the men's (and coed men's) team, dollars"),
    ("women_revenue_amount", "BIGINT", "Revenue of the women's (and coed women's) team, dollars"),
    ("revenue_amount", "BIGINT", "Total revenue of the sport, dollars"),
    ("men_expenses_amount", "BIGINT", "Expenses of the men's (and coed men's) team, dollars"),
    ("women_expenses_amount", "BIGINT", "Expenses of the women's (and coed women's) team, dollars"),
    ("expenses_amount", "BIGINT", "Total expenses of the sport, dollars"),
]


def read_sheet(archive: zipfile.ZipFile, name: str) -> list[dict]:
    rows = openpyxl.load_workbook(io.BytesIO(archive.read(name)), read_only=True).worksheets[0].iter_rows(values_only=True)
    header = [str(h).strip().upper() for h in next(rows)]
    return [dict(zip(header, row)) for row in rows if row and row[0] is not None]


def share(part, whole):
    return round(part / whole, 4) if part is not None and whole else None


def money(value):
    number = num(value)
    return None if number is None else round(number)


def main():
    download(URL, ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH) as archive:
        institutions = read_sheet(archive, "instLevel.xlsx")
        sports = read_sheet(archive, "schools.xlsx")

    sport_rows, by_school = [], defaultdict(dict)
    for row in sports:
        men, women = num(row["SUM_PARTIC_MEN"]) or 0, num(row["SUM_PARTIC_WOMEN"]) or 0
        if not men and not women:
            continue
        unitid, sport = int(row["UNITID"]), row["SPORTS"].strip()
        by_school[unitid][sport] = row
        sport_rows.append((unitid, sport, men, women, money(row["REVENUE_MENALL"]), money(row["REVENUE_WOMENALL"]),
                           money(row["TOTAL_REVENUE_ALL"]), money(row["EXPENSE_MENALL"]),
                           money(row["EXPENSE_WOMENALL"]), money(row["TOTAL_EXPENSE_ALL"])))

    rows = []
    for row in institutions:
        unitid = int(row["UNITID"])
        teams = by_school.get(unitid, {})
        football = teams.get("Football", {})
        basketball = teams.get("Basketball", {})
        classification = (row["CLASSIFICATION_NAME"] or "").strip()
        if classification in ("Other", "") and row.get("CLASSIFICATIONOTHER"):
            classification = row["CLASSIFICATIONOTHER"].strip()
        men, women = num(row["UNDUP_CT_PARTIC_MEN"]), num(row["UNDUP_CT_PARTIC_WOMEN"])
        athletes = (men or 0) + (women or 0) if men is not None or women is not None else None
        aid_men, aid_women = money(row["STUDENTAID_MEN"]), money(row["STUDENTAID_WOMEN"])
        aid_both = (aid_men or 0) + (aid_women or 0)
        rows.append((
            unitid, classification or None, len(teams),
            sum(1 for t in teams.values() if num(t["SUM_PARTIC_MEN"])),
            sum(1 for t in teams.values() if num(t["SUM_PARTIC_WOMEN"])),
            men, women, athletes, num(row["EFTOTALCOUNT"]),
            share(women, athletes), share(num(row["EFFEMALECOUNT"]), num(row["EFTOTALCOUNT"])),
            aid_men, aid_women, money(row["STUDENTAID_TOTAL"]), share(aid_women, aid_both),
            money(row["GRND_TOTAL_REVENUE"]), money(row["GRND_TOTAL_EXPENSE"]),
            money(row["IL_EXPENSE_MENALL"]), money(row["IL_EXPENSE_WOMENALL"]),
            money(football.get("TOTAL_REVENUE_ALL")), money(football.get("TOTAL_EXPENSE_ALL")),
            money(basketball.get("REVENUE_MENALL")), money(basketball.get("EXPENSE_MENALL")),
            money(basketball.get("REVENUE_WOMENALL")), money(basketball.get("EXPENSE_WOMENALL")),
            money(row["RECRUITEXP_TOTAL"]), money(row["RECRUITEXP_MEN"]), money(row["RECRUITEXP_WOMEN"]),
            money(row["HDCOACH_SALARY_MEN"]), money(row["HDCOACH_SALARY_WOMEN"]),
            money(row["ASCOACH_SALARY_MEN"]), money(row["ASCOACH_SALARY_WOMEN"]),
        ))

    with get_db_connection() as conn:
        replace_table(
            conn, "athletics",
            [("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id")] + COLUMNS,
            rows,
            "Equity in Athletics (EADA) 2024-25 intercollegiate athletics data, one row per school: "
            "division, athletes and teams by sex, athletic scholarships, revenue/expenses and coach "
            "salaries. Shares are decimals. For Title IX comparisons use women_athletes_share and "
            "women_athletic_aid_share vs undergrad_women_share. Many schools report revenue equal to "
            "expenses (institutional support counts as revenue), so do not read their difference as profit.",
            SOURCE,
        )
        replace_table(
            conn, "athletics_sports",
            [("school_id", "INTEGER NOT NULL", "IPEDS UNITID, joins to schools.id")] + SPORT_COLUMNS,
            sport_rows,
            "Equity in Athletics (EADA) 2024-25, one row per school and varsity sport: participants, "
            "revenue and expenses by men's/women's team. Football and men's basketball usually carry "
            "most revenue.",
            SOURCE,
        )


if __name__ == "__main__":
    main()
