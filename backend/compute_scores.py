"""
Compute the CollegeCopilot Score: our own composite ranking of degree-granting colleges.

    40% graduation rate        (higher is better)   schools.completion_rate
    30% median earnings, 10 yr (higher is better)   schools.earnings_median_10yr
    20% acceptance rate        (lower is better)    schools.admission_rate
    10% student-faculty ratio  (lower is better)    ipeds_enrollment.student_faculty_ratio

Each metric becomes a 0-100 percentile among scored schools (robust to outliers, unlike
min-max), flipped for lower-is-better metrics, then weighted. Open-admission schools count as
admitting 100% of applicants. Schools missing graduation rate, earnings or the ratio get no score.

Writes copilot_scores (score, rank and the four component scores) and schools.copilot_score
for the latest year. Rerun after reloading schools or IPEDS (needs load_ipeds.py first).

Usage (from backend/):
    python compute_scores.py
"""

from dotenv import load_dotenv

from database import get_db_connection
from source_data import ROOT_DIR, replace_table

load_dotenv(ROOT_DIR / ".env")

WEIGHTS = {"graduation": 0.40, "earnings": 0.30, "selectivity": 0.20, "student_faculty": 0.10}

SCORES_SQL = f"""
WITH metrics AS (
    SELECT s.id,
           s.completion_rate,
           s.earnings_median_10yr,
           CASE WHEN s.admission_rate > 0 THEN s.admission_rate
                WHEN p.open_admission THEN 1.0 END AS acceptance_rate,
           e.student_faculty_ratio
    FROM schools s
    LEFT JOIN ipeds_profile p ON p.school_id = s.id
    LEFT JOIN ipeds_enrollment e ON e.school_id = s.id
    WHERE s.year = (SELECT max(year) FROM schools)
      AND s.highest_degree >= 2
      AND s.completion_rate IS NOT NULL
      AND s.earnings_median_10yr IS NOT NULL
      AND e.student_faculty_ratio IS NOT NULL
      AND (s.admission_rate > 0 OR p.open_admission)
),
components AS (
    SELECT *,
           100 * percent_rank() OVER (ORDER BY completion_rate) AS graduation_score,
           100 * percent_rank() OVER (ORDER BY earnings_median_10yr) AS earnings_score,
           100 * percent_rank() OVER (ORDER BY acceptance_rate DESC) AS selectivity_score,
           100 * percent_rank() OVER (ORDER BY student_faculty_ratio DESC) AS student_faculty_score
    FROM metrics
),
scored AS (
    SELECT *,
           {WEIGHTS['graduation']} * graduation_score + {WEIGHTS['earnings']} * earnings_score
           + {WEIGHTS['selectivity']} * selectivity_score
           + {WEIGHTS['student_faculty']} * student_faculty_score AS score
    FROM components
)
SELECT id, round(score::numeric, 1) AS score,
       rank() OVER (ORDER BY score DESC) AS score_rank,
       round(graduation_score::numeric, 1), round(earnings_score::numeric, 1),
       round(selectivity_score::numeric, 1), round(student_faculty_score::numeric, 1),
       completion_rate, earnings_median_10yr, acceptance_rate, student_faculty_ratio
FROM scored
"""

COLUMNS = [
    ("school_id", "INTEGER PRIMARY KEY", "IPEDS UNITID, joins to schools.id"),
    ("copilot_score", "NUMERIC", "CollegeCopilot Score 0-100 (higher is better): 40% graduation rate, 30% median "
                                 "earnings, 20% selectivity, 10% student-faculty ratio, each as a percentile"),
    ("copilot_rank", "INTEGER", "National rank by CollegeCopilot Score (1 = best)"),
    ("graduation_score", "NUMERIC", "Graduation rate percentile 0-100 (40% of the score)"),
    ("earnings_score", "NUMERIC", "Median 10-year earnings percentile 0-100 (30% of the score)"),
    ("selectivity_score", "NUMERIC", "Selectivity percentile 0-100, lower acceptance rate scores higher (20%)"),
    ("student_faculty_score", "NUMERIC", "Student-faculty ratio percentile 0-100, smaller ratio scores higher (10%)"),
    ("completion_rate", "NUMERIC", "Graduation rate used for the score"),
    ("earnings_median_10yr", "INTEGER", "Median earnings 10 years after entry used for the score"),
    ("acceptance_rate", "NUMERIC", "Acceptance rate used for the score (1.0 for open-admission schools)"),
    ("student_faculty_ratio", "NUMERIC", "Students per faculty member used for the score"),
]


def main():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(SCORES_SQL)
            rows = cursor.fetchall()
        replace_table(
            conn, "copilot_scores", COLUMNS, rows,
            "CollegeCopilot Score: our composite ranking of degree-granting colleges. Use it for "
            "'top ranked' / 'best' questions. Also stored as schools.copilot_score (latest year).",
            {"name": "CollegeCopilot Score (computed from College Scorecard and IPEDS)",
             "url": "https://github.com/Gunar-Oguz/College-Copilot/blob/main/backend/compute_scores.py",
             "year": "2024-25"},
        )
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE schools ADD COLUMN IF NOT EXISTS copilot_score NUMERIC")
            cursor.execute("UPDATE schools SET copilot_score = NULL WHERE copilot_score IS NOT NULL")
            cursor.execute(
                "UPDATE schools s SET copilot_score = c.copilot_score FROM copilot_scores c "
                "WHERE s.id = c.school_id AND s.year = (SELECT max(year) FROM schools)"
            )
            cursor.execute("COMMENT ON COLUMN schools.copilot_score IS %s", (COLUMNS[1][2],))
        conn.commit()
    print(f"Scored {len(rows):,} schools")


if __name__ == "__main__":
    main()
