# CollegeCopilot

AI-powered college search tool for admissions counselors.

## Project structure

```
college_cop/
├── .env                          # API keys
├── backend/
│   ├── main.py                   # FastAPI app + /ask endpoint
│   ├── load_all_schools.py       # Load all US colleges from Scorecard
│   ├── query_engine.py           # Parses questions → SQL queries
│   ├── database.py               # PostgreSQL connection + execution
│   └── requirements.txt
├── frontend/
│   └── index.html                # Chat interface
├── data/
│   └── schema.sql                # Schools table schema
└── render.yaml                   # Deploy config
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

### 3. Load all US schools (~6,000+, takes ~2 min)

```bash
cd backend
python load_all_schools.py
```

### 4. Run the app

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** — chat page and API on one URL.

## Test queries

1. "List 10 schools with lowest acceptance rate"
2. "Top 5 schools in California by graduation rate"
3. "What is the median earnings for Stanford graduates?"
4. "Compare Harvard, Yale, Princeton on cost, acceptance rate, and earnings"
5. "Schools in Washington under $15,000 for families making $75,000"
6. "How many schools are in the database?"

## Deploy

See `render.yaml` for Render.com deployment config.
