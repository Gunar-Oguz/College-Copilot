"""
Limits that protect the paid AI API in the public demo (in memory, no database).

- Per visitor: QUESTIONS_PER_VISITOR questions per UTC day, keyed by the visitor's access email
  (so opening a new tab does not reset it; the default is 5).
- Whole app: QUESTIONS_PER_DAY questions per UTC day across all visitors (default 200).

Counts reset at midnight UTC and when the process restarts. Questions answered without the AI
(e.g. "help me find a school", which gets quick-reply buttons) are not counted.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_PER_VISITOR = 5
DEFAULT_PER_DAY = 200


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""  # "visitor" or "daily" when not allowed
    visitor_left: int = 0
    daily_left: int = 0


class QuestionBudget:
    """Thread-safe counters for one process; Streamlit shares one instance across sessions."""

    def __init__(self, per_visitor: int = DEFAULT_PER_VISITOR, per_day: int = DEFAULT_PER_DAY, clock=None):
        self.per_visitor, self.per_day = per_visitor, per_day
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._day = ""
        self._total = 0
        self._by_visitor: dict[str, int] = {}

    def _roll(self) -> None:
        today = utc_day(self.clock())
        if today != self._day:
            self._day, self._total, self._by_visitor = today, 0, {}

    def _decision(self, visitor: str, allowed: bool = True, reason: str = "") -> Decision:
        return Decision(allowed, reason, max(0, self.per_visitor - self._by_visitor.get(visitor, 0)),
                        max(0, self.per_day - self._total))

    def peek(self, visitor: str) -> Decision:
        with self._lock:
            self._roll()
            return self._decision(visitor)

    def consume(self, visitor: str, exempt: bool = False) -> Decision:
        """Count one question if both limits allow it; the check and the count are one step.

        exempt skips the per-visitor limit (the owner), never the daily cap.
        """
        with self._lock:
            self._roll()
            if not exempt and self._by_visitor.get(visitor, 0) >= self.per_visitor:
                return self._decision(visitor, False, "visitor")
            if self._total >= self.per_day:
                return self._decision(visitor, False, "daily")
            self._by_visitor[visitor] = self._by_visitor.get(visitor, 0) + 1
            self._total += 1
            return self._decision(visitor)

    def refund(self, visitor: str) -> None:
        """Give a question back when the AI call failed before answering."""
        with self._lock:
            if self._by_visitor.get(visitor, 0) > 0:
                self._by_visitor[visitor] -= 1
                self._total = max(0, self._total - 1)


LIMIT_MESSAGES = {
    "visitor": "You've used all {limit} questions for today. They reset at midnight UTC. Thanks for trying "
               "CollegeCopilot!",
    "daily": "The demo has reached its daily limit of questions (it protects the paid AI service). "
             "Please come back after midnight UTC.",
}
