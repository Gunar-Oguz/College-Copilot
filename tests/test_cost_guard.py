"""
Tests for the demo's question limits (backend/cost_guard.py) and the database download check
(backend/demo_data.py). No network: downloads use a fake opener.

Run from the repo root:
    python -m pytest tests/test_cost_guard.py -v
"""

import hashlib
import io
import threading
from datetime import datetime, timedelta, timezone

import pytest

import demo_data
from cost_guard import QuestionBudget

START = datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc)


def budget(per_visitor=3, per_day=5):
    clock = {"now": START}
    return QuestionBudget(per_visitor, per_day, clock=lambda: clock["now"]), clock


class TestQuestionBudget:
    def test_per_visitor_limit(self):
        b, _ = budget()
        assert [b.consume("a").allowed for _ in range(4)] == [True, True, True, False]
        assert b.consume("a").reason == "visitor"
        assert b.consume("b").allowed  # other visitors are not affected

    def test_daily_cap_across_visitors(self):
        b, _ = budget(per_visitor=10, per_day=3)
        assert [b.consume(v).allowed for v in "abc"] == [True, True, True]
        decision = b.consume("d")
        assert not decision.allowed and decision.reason == "daily" and decision.daily_left == 0

    def test_owner_is_exempt_from_the_visitor_limit_but_not_the_daily_cap(self):
        b, _ = budget(per_visitor=1, per_day=3)
        assert [b.consume("owner", exempt=True).allowed for _ in range(4)] == [True, True, True, False]

    def test_resets_at_utc_midnight(self):
        b, clock = budget(per_visitor=1)
        assert b.consume("a").allowed and not b.consume("a").allowed
        clock["now"] = START + timedelta(hours=1)  # 00:00 UTC the next day
        assert b.consume("a").allowed

    def test_remaining_and_refund(self):
        b, _ = budget()
        b.consume("a")
        assert (b.peek("a").visitor_left, b.peek("a").daily_left) == (2, 4)
        b.refund("a")
        assert (b.peek("a").visitor_left, b.peek("a").daily_left) == (3, 5)
        b.refund("nobody")  # never goes below zero
        assert b.peek("a").daily_left == 5

    def test_thread_safe(self):
        b = QuestionBudget(per_visitor=1000, per_day=100)
        results = []
        threads = [threading.Thread(target=lambda: results.extend(b.consume("x").allowed for _ in range(50)))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == 100


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestDatabaseFile:
    DATA = b"duckdb file bytes" * 1000
    SHA = hashlib.sha256(DATA).hexdigest()

    def opener(self, calls):
        def open_url(url, timeout):
            calls.append(url)
            return FakeResponse(self.DATA)
        return open_url

    def test_local_path(self, tmp_path):
        path = tmp_path / "local.duckdb"
        path.write_bytes(b"x")
        assert demo_data.ensure_database({"DUCKDB_PATH": str(path)}.get) == path
        with pytest.raises(demo_data.DataFileError):
            demo_data.ensure_database({"DUCKDB_PATH": str(tmp_path / "missing.duckdb")}.get)

    def test_download_verified_then_cached(self, tmp_path):
        calls = []
        settings = {"DB_URL": "https://example.org/db", "DB_SHA256": self.SHA}.get
        path = demo_data.ensure_database(settings, cache_dir=tmp_path, opener=self.opener(calls))
        assert path.read_bytes() == self.DATA and calls == ["https://example.org/db"]
        assert demo_data.ensure_database(settings, cache_dir=tmp_path, opener=self.opener(calls)) == path
        assert len(calls) == 1  # the cached, verified file is reused

    def test_wrong_hash_leaves_no_file(self, tmp_path):
        settings = {"DB_SHA256": "0" * 64}.get
        with pytest.raises(demo_data.DataFileError, match="SHA-256"):
            demo_data.ensure_database(settings, cache_dir=tmp_path, opener=self.opener([]))
        assert list(tmp_path.iterdir()) == []

    def test_corrupted_cache_is_downloaded_again(self, tmp_path):
        calls = []
        settings = {"DB_SHA256": self.SHA}.get
        (tmp_path / f"collegecopilot-{self.SHA[:12]}.duckdb").write_bytes(b"truncated")
        path = demo_data.ensure_database(settings, cache_dir=tmp_path, opener=self.opener(calls))
        assert path.read_bytes() == self.DATA and calls == [demo_data.DEFAULT_DB_URL]

    def test_manifest_pins_a_sha256(self):
        assert len(demo_data.manifest()["sha256"]) == 64
