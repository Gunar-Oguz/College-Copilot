# CollegeCopilot

AI-powered college search tool for admissions counselors.

## How it answers a question

1. **Planner** (`planner.py`): Claude reads the question and calls tools. It never states facts itself.
   - `resolve_school`: name or nickname → school ids and coordinates (asks which one if ambiguous)
   - `resolve_location`: ZIP code or city → coordinates, used with the `miles_between()` SQL function
   - `net_price_column`: family income → the right net-price bracket
   - `find_similar_schools`: "schools like X" with optional easier / cheaper / state filters
   - `run_query`: read-only SQL (validated, 5s timeout, row cap)
   - `submit_answer`: a tool result, a clarifying question, or "can't answer"
2. **Presentation** (`presentation.py`): the rows become stat tiles, a comparison table, ranked bars
   or a table. Every number shown is formatted from the data. Claude writes only a one- or
   two-sentence summary, which is replaced by a plain one if it cites a number not in the data.

## Project structure

```
college_cop/
├── backend/
│   ├── main.py                # FastAPI app, /ask endpoint, answer summary
│   ├── planner.py             # Claude tool-use planner
│   ├── presentation.py        # Result rows → display blocks
│   ├── school_lookup.py       # School name/alias resolution
│   ├── locations.py           # ZIP/city → coordinates
│   ├── similar_schools.py     # Similarity search
│   ├── database.py            # Connections and the read-only query runner
│   ├── source_data.py         # Shared helpers for the extra-source loaders
│   └── load_*.py              # Data loaders
├── data/                      # Schema SQL, curated aliases; downloads (gitignored)
├── docs/DATA_SOURCES.md       # Every source table and column
├── evals/                     # Question set with known answers + scorer
├── frontend/index.html        # Chat interface
└── tests/
```

## Setup

### 1. Environment variables (`.env`)

```
ANTHROPIC_API_KEY=your_key
COLLEGE_SCORECARD_API_KEY=your_key
SUPABASE_URL=https://your-project.supabase.co      # optional: accounts and saved schools
SUPABASE_ANON_KEY=your_publishable_key
```

### 2. Install dependencies

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Load data (from `backend/`)

```bash
python load_all_years.py --recreate --start-year 2015 --end-year 2024
python load_programs.py --recreate
python load_aliases.py        # school nicknames; run after schools load
python load_zipcodes.py       # school coordinates + distance search; rerun after reloading schools
```

Extra public sources (optional; `pip install -r requirements-data.txt` first). Each loader downloads
its files to `data/<source>/`, replaces its tables and records itself in `data_sources`. Run
`load_ipeds.py` first: the others use its directory file to map OPEIDs and ZIPs to schools.

```bash
python load_ipeds.py          # IPEDS: admissions, enrollment by race/gender, costs, finance, faculty, outcomes
python load_mobility.py       # Opportunity Insights economic mobility
python load_campus_safety.py  # Clery Act crime statistics
python load_default_rates.py  # Federal student loan cohort default rates
python load_athletics.py      # EADA / Title IX athletics
python load_research.py       # NSF HERD research spending
python load_gi_bill.py        # VA GI Bill Comparison Tool
python load_accreditation.py  # DAPIP institutional and program accreditation
python load_irs990.py         # IRS Form 990 via ProPublica (private nonprofits; ~10 min first run)
python load_careers.py        # BLS wages and projections, O*NET, CIP-to-occupation crosswalk, metro wages
python load_census.py         # Census ACS demographics of each campus ZIP code
python load_designations.py   # AACSB, Phi Beta Kappa, CTCL, QuestBridge, Posse lists
python compute_scores.py      # CollegeCopilot Score (needs load_ipeds.py); rerun after reloading data
python load_deadlines.py      # application deadlines from data/school_deadlines.csv
python document_sources.py    # regenerate docs/DATA_SOURCES.md
```

Every table and column is described in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md). The planner
sees all loaded tables with those descriptions; answers name the sources their query used.

### 4. Run the app

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** — chat page and API on one URL.

## Testing

```bash
python -m pytest tests/              # unit + database tests
python backend/evals.py              # 30 end-to-end behavior checks (calls Claude, ~1 min)
python evals/run_eval.py             # 70 exact answers vs. ground-truth SQL (calls Claude, ~2 min)
```

`backend/evals.py` runs questions through the whole pipeline (planner, guardrails, summary) and checks
behavior: "Top ranked university in Washington" has UW-Seattle in the top 3, "Cheapest UC school" is
sorted by net price, college lists contain no adult-education programs, empty results say so, and so on.
Run one category with `python backend/evals.py safety`. Both suites exit non-zero on any failure.

## Answer guardrails (`guardrails.py`)

- **Wording-based scope, decided in code:** "universities" / "four-year" lists use Carnegie classes 15-23;
  "colleges" lists use degree-granting schools only (no adult-education or certificate-only programs).
  Counts and averages keep every institution. Any non-degree school that slips into a college list is
  removed, with a note.
- **Rankings:** the data has no official rankings, so "top" / "best" answers say what they are ordered by
  (e.g. "graduation rate, then median earnings"), and the summary names row 1 as the top by that measure.
- **Fewer results than asked:** "Top 10 ... in Wyoming" explains that only 1 school matches.
- **Nothing found:** "I couldn't find data matching your question. Try rephrasing?"
- **Vocabulary** (in the planner prompt): best/top ranked → graduation rate then earnings; most selective →
  acceptance rate (0% rates are reporting errors and skipped); cheapest → net price (tuition shown too);
  highest paying → 10-year earnings (program earnings for a major); safest → crimes per 1,000 students
  at schools with 1,000+ students; best value → earnings per $1 of net price.

## CollegeCopilot Score

Our own ranking of about 3,100 degree-granting colleges (`compute_scores.py`): 40% graduation rate,
30% median earnings after 10 years, 20% selectivity (lower acceptance rate is better) and 10%
student-faculty ratio (lower is better). Each metric becomes a 0-100 percentile among scored colleges, which
is robust to outliers, unlike min-max scaling; open-admission schools count as admitting 100%. Stored in
`copilot_scores` (score, national rank, the four component scores) and `schools.copilot_score`. "Best" and
"top ranked" questions sort by it, and answers say how it is built.

## School pages, compare, deadlines, reviews

- **`/school/<unitid>`**: photo, CollegeCopilot Score with its breakdown, key stats, application deadlines,
  majors by degree level, campus safety, reviews, and Save / Compare buttons (`school.html`, data from
  `GET /api/school/<unitid>`).
- **`/compare`**: pick 2-3 schools from your saved list or by search; the better value in each row is green
  and the worse red. The selection is in the URL (`/compare?ids=110662,110635`), so comparisons can be shared.
- **Deadlines:** `school_deadlines` (116 schools, Fall 2027 cycle) from the Common App 2026-27 deadline grid
  and official admissions pages, each with its source URL. Update `data/school_deadlines.csv` each cycle.
- **Reviews:** `user_reviews` (1-5 stars plus text, one per user per school; writing again edits it). Anyone can
  read them; only signed-in users can write, and row-level security limits changes to the author.
- **Photos:** `photos.py` finds a school's image through Wikidata (IPEDS id P1771, image P18) and Wikimedia
  Commons on first view, caches it in `school_photos` (including "no photo"), and shows the author and license.
- **"I don't know" answers** show what we do know about the school (planner lookup, or an exact name or
  nickname found in the question), its website and profile, and questions we can answer.
- Shared front-end code: `frontend/static/auth.js` (sign-in, Save hearts) and `common.css`.

## Logging, feedback and admin

- Every question is logged in `query_logs` (question, SQL or tool call, answer, response time, outcome).
- 👍 / 👎 under each answer saves to `feedback` with the question and answer (one vote per answer).
- **`/admin`**: questions today / all time, average response time, recent 👎 answers with their SQL,
  most common questions, latest questions. It is open by default; set `ADMIN_TOKEN` in the environment
  to require `/admin?token=...`.
- The planner's SQL validator blocks `query_logs` and `feedback`, so answers can't expose other people's
  questions.

Try questions like:

1. "Find 5 schools similar to UCLA but easier to get into"
2. "Compare Harvard, Yale, and Princeton on cost, acceptance rate, and earnings"
3. "My family makes $85,000. Which 5 schools in California have the lowest net price for us?"
4. "What's the acceptance rate at UW?" (asks which UW)
5. "Which 5 schools have the highest earnings 4 years after a computer science bachelor's?"
6. "Public colleges within 40 miles of 94720 with a graduation rate above 70%"

## Accounts, saved schools and notes

Sign-in uses [Supabase Auth](https://supabase.com/docs/guides/auth) (email/password and Google).
Without `SUPABASE_URL` / `SUPABASE_ANON_KEY` the app works as before, with no sign-in buttons.

- The browser signs in with supabase-js and sends its access token; `user_data.py` asks Supabase
  who the token belongs to and only trusts that user id.
- `user_favorites` and `user_notes` are created at startup. Every query filters on the user id, and
  row-level security on `app.user_id` is a second layer. The planner's SQL validator blocks both
  tables, so answers can never read anyone's saved schools or notes.
- Endpoints: `GET /me/favorites`, `GET /me/favorite-ids`, `PUT|DELETE /me/favorites/{unitid}`,
  `POST /me/favorites/{unitid}/notes`, `PUT|DELETE /me/notes/{id}`.

Supabase dashboard setup:
1. Authentication → URL Configuration: set the Site URL to the deployed URL and add
   `http://localhost:8000` and the deployed URL to Redirect URLs.
2. Google: create an OAuth client in Google Cloud (authorized redirect URI
   `https://<project>.supabase.co/auth/v1/callback`), then enable Authentication → Providers →
   Google with its client ID and secret. Until then the Google button says it isn't turned on.

## Deploy

See `render.yaml` for Render.com deployment config.
