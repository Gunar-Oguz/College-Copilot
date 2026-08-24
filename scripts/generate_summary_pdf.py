"""Generate CollegeCopilot project summary PDF for download."""

from fpdf import FPDF

OUTPUT = "/workspace/docs/CollegeCopilot_Summary.pdf"

CONTENT = [
    ("title", "CollegeCopilot - Complete Project Summary"),
    ("subtitle", "For context handoff | GitHub: Gunar-Oguz/College-Copilot"),
    ("section", "WHAT THE PROJECT IS"),
    ("text", "CollegeCopilot is an AI-powered college search tool for admissions counselors."),
    ("bullet", "Backend: FastAPI (backend/main.py) - /ask endpoint turns plain English into SQL"),
    ("bullet", "Database: PostgreSQL with College Scorecard schools + programs data"),
    ("bullet", "Frontend: Chat UI at frontend/index.html"),
    ("bullet", "Repo: https://github.com/Gunar-Oguz/College-Copilot"),
    ("bullet", "Local URL: http://localhost:8000 (after starting uvicorn)"),
    ("bullet", "Deploy: render.yaml for Render.com (no Dockerfile)"),
    ("section", "PROJECT FILE TREE"),
    ("code", """college_cop/
  .env                          API keys (local only, gitignored)
  backend/
    main.py                       FastAPI + /ask endpoint
    database.py                   PostgreSQL connection
    query_parser.py               Natural language -> filters
    sql_builder.py                Filters -> SQL
    query_engine.py               Full query pipeline
    response_formatter.py         Format chat responses
    data_coverage.py              Data coverage stats
    load_all_schools.py           Load all schools (single year)
    load_all_years.py             Load schools 1996-2024
    load_recent_years.py          Load schools 2015-2024
    load_programs.py              Load programs/majors data
    requirements.txt
  data/
    schema.sql                    Schools table (37 columns)
    programs_schema.sql           Programs table schema
  frontend/
    index.html                    Chat UI
  tests/
    test_queries.py               44 query engine tests
    test_load_programs.py         Programs loader tests
    test_load_all_years.py        Schools earnings loader tests
  render.yaml                     Deploy config
  README.md"""),
    ("section", "PHASE 1: CORE APP (DONE)"),
    ("bullet", "Built FastAPI backend with /ask endpoint"),
    ("bullet", "Query engine: natural language -> SQL (query_parser.py, sql_builder.py)"),
    ("bullet", "Built chat frontend (HTML)"),
    ("bullet", "44 tests in tests/test_queries.py"),
    ("section", "PHASE 2: SCHOOLS DATA (DONE)"),
    ("bullet", "Created data/schema.sql - schools table with 37 columns"),
    ("bullet", "load_all_years.py - load 1996-2024 (hit rate limits on early years)"),
    ("bullet", "load_recent_years.py - load 2015-2024 only (~39 min, more reliable)"),
    ("bullet", "Multiple years of school data loaded into PostgreSQL"),
    ("section", "PHASE 3: PROGRAMS DATA (DONE + FIXED)"),
    ("bullet", "Created data/programs_schema.sql - programs table with FK to schools"),
    ("bullet", "Created load_programs.py - Field of Study data 2015-2024"),
    ("bullet", "BUG FOUND: All earnings columns were NULL"),
    ("bullet", "CAUSE: Loader used earnings.2_yr but API has 1_yr, 4_yr, 5_yr (not 2_yr)"),
    ("bullet", "FIX: Changed to earnings_median_1yr and earnings_median_4yr columns"),
    ("bullet", "FIX: Extract earnings.1_yr.overall_median_earnings and earnings.4_yr.overall_median_earnings"),
    ("bullet", "RESULT: ~1,840,795 programs loaded across 9 years"),
    ("bullet", "Years: 2015-16 through 2024-25 (2023-24 skipped - no API data)"),
    ("bullet", "Added retry logic for ChunkedEncodingError and ReadTimeout"),
    ("bullet", "Added --schema-only flag to recreate table without API key"),
    ("section", "PHASE 4: SCHOOLS EARNINGS FIX (DONE)"),
    ("bullet", "BUG: earnings_median_10yr was NULL for all years except 2020-21"),
    ("bullet", "CAUSE: For 2024-25 API uses latest.earnings.10_yrs_after_entry.median"),
    ("bullet", "Historical years use {year}.earnings.10_yrs_after_entry.median"),
    ("bullet", "FIX: Added api_prefix() - returns 'latest' for 2024, year string for historical"),
    ("bullet", "FIX: Updated fields_for_api_year() and _get() to use correct prefix"),
    ("bullet", "BUG: online_only column is boolean but API returns 0 or 1"),
    ("bullet", "FIX: Added _as_bool() to convert 0/1 to True/False before insert"),
    ("bullet", "Added --recreate flag to load_all_years.py"),
    ("bullet", "Test plan: reload 2024-25 only first, verify COUNT, then reload all years"),
    ("section", "GIT / GITHUB"),
    ("bullet", "All fixes committed and pushed to main branch"),
    ("bullet", "Repo: https://github.com/Gunar-Oguz/College-Copilot"),
    ("bullet", "Local Mac synced with origin/main"),
    ("section", "ENVIRONMENT VARIABLES (.env)"),
    ("code", """ANTHROPIC_API_KEY=your_key
COLLEGE_SCORECARD_API_KEY=your_key
DATABASE_URL=your_postgres_url  (optional, defaults to local postgres)"""),
    ("section", "KEY COMMANDS"),
    ("code", """# Run the app
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
# Open http://localhost:8000

# Load schools - test one year
python load_all_years.py --start-year 2024 --end-year 2024 --recreate

# Load schools - recent years with full schema
python load_recent_years.py --recreate

# Load programs
python load_programs.py --recreate

# Run tests
python -m pytest ../tests/test_queries.py -v

# If port 8000 busy:
lsof -ti :8000 | xargs kill -9"""),
    ("section", "VERIFY QUERIES (SQL)"),
    ("code", """-- Schools earnings test (2024-25)
SELECT COUNT(earnings_median_10yr)
FROM schools WHERE year = '2024-25';
-- Expected: ~5000+ (not 0)

-- Programs earnings by year
SELECT year,
       COUNT(earnings_median_1yr) AS has_1yr,
       COUNT(earnings_median_4yr) AS has_4yr
FROM programs
GROUP BY year ORDER BY year;
-- Expected: non-zero counts per year

-- Example program query
SELECT s.name, p.program_name,
       p.earnings_median_1yr, p.earnings_median_4yr
FROM schools s
JOIN programs p ON s.id = p.school_id AND s.year = p.year
WHERE p.program_name LIKE '%Computer Science%'
  AND s.year = '2024-25'
ORDER BY p.earnings_median_4yr DESC
LIMIT 10;"""),
    ("section", "PROGRAMS DATA BY YEAR"),
    ("code", """Year       Programs
2024-25    216,684
2022-23    216,551
2021-22    197,613
2020-21    211,491
2019-20    212,119
2018-19    205,163
2017-18    198,844
2016-17    194,012
2015-16    188,318
Total:     1,840,795 programs"""),
    ("section", "NOT DONE YET"),
    ("bullet", "Full schools reload with all 37 columns for all years"),
    ("bullet", "Year selection in query parser (currently hardcoded to 2024-25)"),
    ("bullet", "Wire programs into /ask for program-level queries (e.g. CS earnings)"),
    ("bullet", "Dockerfile not created"),
    ("section", "CURRENT STATUS"),
    ("code", """Item                          Status
------------------------------------------
App + query engine              DONE
Programs data (~1.84M rows)     DONE (earnings fixed)
Schools earnings fix (code)     DONE, on GitHub
Schools 2024-25 test reload     User ran - verify COUNT
Full multi-year schools reload  PENDING
Program queries in chat         NOT WIRED
Dockerfile                      NO"""),
]


class PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def build():
    pdf = PDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    for kind, text in CONTENT:
        pdf.set_x(pdf.l_margin)
        w = pdf.epw

        if kind == "title":
            pdf.set_font("Helvetica", "B", 18)
            pdf.multi_cell(w, 10, text, align="C")
            pdf.ln(4)
        elif kind == "subtitle":
            pdf.set_font("Helvetica", "I", 10)
            pdf.multi_cell(w, 6, text, align="C")
            pdf.ln(6)
        elif kind == "section":
            pdf.ln(5)
            pdf.set_font("Helvetica", "B", 12)
            pdf.set_fill_color(230, 230, 230)
            pdf.multi_cell(w, 8, f"  {text}", fill=True)
            pdf.ln(2)
        elif kind == "text":
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(w, 5, text)
            pdf.ln(2)
        elif kind == "bullet":
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(w, 5, f"  * {text}")
        elif kind == "code":
            pdf.set_font("Courier", "", 8)
            pdf.multi_cell(w, 4, text)
            pdf.ln(3)

    pdf.output(OUTPUT)


if __name__ == "__main__":
    build()
    print(OUTPUT)
