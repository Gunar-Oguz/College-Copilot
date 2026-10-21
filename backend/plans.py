"""
Plans (Free / Pro), daily question limits and the AI priority queue.

- user_plans: one row per user who has started checkout or subscribed (Stripe ids, status,
  current_period_end). Everyone else is on Free.
- usage_tracking: questions asked per user (or per signed-out visitor, by hashed IP) per UTC day.
  The limit check and the increment are one atomic statement, so parallel requests can't exceed it.
- AI requests take turns through a priority queue (at most AI_CONCURRENCY at once); Pro goes first.
"""

import hashlib
import heapq
import itertools
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from database import execute_sql, get_db_connection

FREE_DAILY_QUESTIONS = 10
FREE_SAVED_SCHOOLS = 3
FREE_COMPARE_SCHOOLS = 3
PRO_COMPARE_SCHOOLS = 10
FREE_VISIBLE_REVIEWS = 2
PRO_STATUSES = {"active", "trialing", "past_due"}  # past_due: Stripe is still retrying the payment

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_plans (
    user_id TEXT PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT UNIQUE,
    stripe_subscription_id TEXT,
    current_period_end TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    status TEXT,
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_tracking (
    user_id TEXT NOT NULL,
    date DATE NOT NULL,
    question_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

-- Stripe event ids already handled (webhooks can arrive more than once).
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    type TEXT,
    received_at TIMESTAMP DEFAULT NOW()
);
"""


def ensure_plan_tables() -> None:
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute(SCHEMA_SQL)


def utc_today():
    return datetime.now(timezone.utc).date()


# ---------- Plans ----------

def get_plan(user_id: str | None) -> dict:
    """{'plan': 'free'|'pro', 'status', 'current_period_end', 'cancel_at_period_end', 'has_billing'}."""
    row = None
    if user_id:
        rows = execute_sql("SELECT * FROM user_plans WHERE user_id = %s", (user_id,))
        row = rows[0] if rows else None
    is_pro = bool(row and row["plan"] == "pro" and row["status"] in PRO_STATUSES)
    return {
        "plan": "pro" if is_pro else "free",
        "status": row["status"] if row else None,
        "current_period_end": row["current_period_end"] if row else None,
        "cancel_at_period_end": bool(row and row["cancel_at_period_end"]),
        "has_billing": bool(row and row["stripe_customer_id"]),
    }


def is_pro(user_id: str | None) -> bool:
    return get_plan(user_id)["plan"] == "pro"


def limits_for(user_id: str | None) -> dict:
    pro = is_pro(user_id)
    return {
        "plan": "pro" if pro else "free",
        "daily_questions": None if pro else FREE_DAILY_QUESTIONS,
        "saved_schools": None if pro else FREE_SAVED_SCHOOLS,
        "compare_schools": PRO_COMPARE_SCHOOLS if pro else FREE_COMPARE_SCHOOLS,
    }


def save_customer(user_id: str, customer_id: str) -> None:
    execute_write(
        """
        INSERT INTO user_plans (user_id, stripe_customer_id) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET stripe_customer_id = EXCLUDED.stripe_customer_id, updated_at = NOW()
        """,
        (user_id, customer_id),
    )


def customer_id_for(user_id: str) -> str | None:
    rows = execute_sql("SELECT stripe_customer_id FROM user_plans WHERE user_id = %s", (user_id,))
    return rows[0]["stripe_customer_id"] if rows else None


def user_for_customer(customer_id: str) -> str | None:
    rows = execute_sql("SELECT user_id FROM user_plans WHERE stripe_customer_id = %s", (customer_id,))
    return rows[0]["user_id"] if rows else None


def save_subscription(user_id: str, customer_id: str | None, subscription_id: str, status: str,
                      period_end: datetime | None, cancel_at_period_end: bool) -> None:
    plan = "pro" if status in PRO_STATUSES else "free"
    execute_write(
        """
        INSERT INTO user_plans (user_id, plan, stripe_customer_id, stripe_subscription_id, current_period_end,
                                status, cancel_at_period_end)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            plan = EXCLUDED.plan,
            stripe_customer_id = COALESCE(EXCLUDED.stripe_customer_id, user_plans.stripe_customer_id),
            stripe_subscription_id = EXCLUDED.stripe_subscription_id,
            current_period_end = EXCLUDED.current_period_end,
            status = EXCLUDED.status,
            cancel_at_period_end = EXCLUDED.cancel_at_period_end,
            updated_at = NOW()
        """,
        (user_id, plan, customer_id, subscription_id, period_end, status, cancel_at_period_end),
    )


def first_time_event(event_id: str, event_type: str) -> bool:
    """Record a Stripe event; False if it was already handled."""
    rows = execute_write(
        "INSERT INTO stripe_events (id, type) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
        (event_id, event_type),
    )
    return bool(rows)


def execute_write(sql: str, params: tuple) -> list[dict]:
    conn = get_db_connection()
    try:
        with conn, conn.cursor() as cursor:
            cursor.execute(sql, params)
            if cursor.description:
                names = [d.name for d in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
            return []
    finally:
        conn.close()


# ---------- Daily question limits ----------

def visitor_id(ip: str | None) -> str:
    """A signed-out visitor's usage key: a salted hash of their IP address (the IP isn't stored)."""
    salt = os.getenv("USAGE_SALT") or os.getenv("SUPABASE_ANON_KEY") or "collegecopilot"
    digest = hashlib.sha256(f"{salt}:{ip or 'unknown'}".encode()).hexdigest()[:32]
    return f"anon:{digest}"


def consume_question(usage_key: str, limit: int | None) -> tuple[bool, int]:
    """Count a question if under the limit (None = unlimited). Returns (allowed, count today)."""
    today = utc_today()
    if limit is None:
        rows = execute_write(
            """
            INSERT INTO usage_tracking (user_id, date, question_count) VALUES (%s, %s, 1)
            ON CONFLICT (user_id, date) DO UPDATE SET question_count = usage_tracking.question_count + 1
            RETURNING question_count
            """,
            (usage_key, today),
        )
        return True, rows[0]["question_count"]
    rows = execute_write(
        """
        INSERT INTO usage_tracking (user_id, date, question_count) VALUES (%s, %s, 1)
        ON CONFLICT (user_id, date) DO UPDATE SET question_count = usage_tracking.question_count + 1
            WHERE usage_tracking.question_count < %s
        RETURNING question_count
        """,
        (usage_key, today, limit),
    )
    if rows:
        return True, rows[0]["question_count"]
    return False, questions_today(usage_key)


def questions_today(usage_key: str) -> int:
    rows = execute_sql("SELECT question_count FROM usage_tracking WHERE user_id = %s AND date = %s",
                       (usage_key, utc_today()))
    return rows[0]["question_count"] if rows else 0


# ---------- Priority queue for AI requests ----------

class PriorityGate:
    """At most `slots` holders at once; waiting Pro requests are admitted before Free ones."""

    def __init__(self, slots: int):
        self._slots = slots
        self._busy = 0
        self._waiting: list[tuple[int, int]] = []
        self._order = itertools.count()
        self._cond = threading.Condition()

    @contextmanager
    def turn(self, pro: bool):
        ticket = (0 if pro else 1, next(self._order))
        with self._cond:
            heapq.heappush(self._waiting, ticket)
            while self._busy >= self._slots or self._waiting[0] != ticket:
                self._cond.wait()
            heapq.heappop(self._waiting)
            self._busy += 1
        try:
            yield
        finally:
            with self._cond:
                self._busy -= 1
                self._cond.notify_all()


ai_gate = PriorityGate(int(os.getenv("AI_CONCURRENCY", "4")))
