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
│   └── load_*.py              # Data loaders
├── data/                      # Schema SQL and curated aliases
├── evals/                     # Question set with known answers + scorer
├── frontend/index.html        # Chat interface
└── tests/
```

## Setup

### 1. Environment variables (`.env`)

```
ANTHROPIC_API_KEY=your_key
COLLEGE_SCORECARD_API_KEY=your_key
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

### 4. Run the app

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** — chat page and API on one URL.

## Testing

```bash
python -m pytest tests/              # unit + database tests
python evals/run_eval.py             # answer accuracy (calls Claude, ~1-2 min)
```

Try questions like:

1. "Find 5 schools similar to UCLA but easier to get into"
2. "Compare Harvard, Yale, and Princeton on cost, acceptance rate, and earnings"
3. "My family makes $85,000. Which 5 schools in California have the lowest net price for us?"
4. "What's the acceptance rate at UW?" (asks which UW)
5. "Which 5 schools have the highest earnings 4 years after a computer science bachelor's?"
6. "Public colleges within 40 miles of 94720 with a graduation rate above 70%"

## Deploy

See `render.yaml` for Render.com deployment config.
