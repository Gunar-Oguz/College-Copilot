"""
Tests for the summary attribution check: each number must belong to the school the sentence names,
superlatives must be true, and "higher/lower ... A vs B" must match A and B. The failing summaries
are real ones the model wrote during testing. No database or API calls.

Run from the repo root:
    python -m pytest tests/test_summary_attribution.py -v
"""

from types import SimpleNamespace

import pytest

import answering
from presentation import attribution_problems


def school(i, name, price, admit, grad, earnings):
    return {"id": i, "name": name, "cost_avg_net_price": price, "admission_rate": admit, "completion_rate": grad,
            "earnings_median_10yr": earnings}


UCLA = school(110662, "University of California-Los Angeles", 12548.0, 0.0897, 0.9264, 82511.0)
BERKELEY = school(110635, "University of California-Berkeley", 13481.0, 0.1098, 0.9286, 92446.0)
MICHIGAN = school(170976, "University of Michigan-Ann Arbor", 13138.0, 0.1564, 0.9321, 83648.0)
HARVARD = school(166027, "Harvard University", 19066.0, 0.0365, 0.9745, 101817.0)
YALE = school(130794, "Yale University", 23777.0, 0.0387, 0.9603, 100533.0)
PRINCETON = school(186131, "Princeton University", 6128.0, 0.0462, 0.9735, 110066.0)
MIT = school(166683, "Massachusetts Institute of Technology", 20111.0, 0.0455, 0.9624, 143372.0)
STANFORD = school(243744, "Stanford University", 13807.0, 0.0361, 0.9237, 124080.0)
ALIASES = {110635: ["Berkeley", "UC Berkeley", "Cal"], 110662: ["UCLA"], 170976: ["Michigan"], 166683: ["MIT"],
           243744: ["Stanford"], 123961: ["USC"]}

MISATTRIBUTED = [
    # 93.2% is Michigan's graduation rate, not Berkeley's.
    ([UCLA, BERKELEY, MICHIGAN],
     "UCLA has the lowest net price at $12,548 and the lowest acceptance rate at 9.0%, while UC Berkeley has the "
     "highest median earnings at $92,446 and the highest graduation rate at 93.2%, and University of Michigan has "
     "the highest acceptance rate at 15.6% with median earnings of $83,648.", "93.2"),
    # 3.6% is not Princeton's rate (4.6%), and Princeton is not the most selective.
    ([HARVARD, PRINCETON, YALE],
     "Harvard has a 3.7% acceptance rate and average net price of $19,066, with median 10-year earnings of "
     "$101,817, while Princeton is the most selective at 3.6% acceptance with the lowest net price at $6,128 and "
     "highest earnings at $110,066, and Yale has a 3.9% acceptance rate, highest net price at $23,777, and median "
     "earnings of $100,533.", "3.6"),
    # Stanford's earnings are lower than MIT's, not higher.
    ([MIT, STANFORD],
     "Stanford has a lower net price ($13,807 vs $20,111, meaning students pay less after financial aid), a lower "
     "acceptance rate (3.6% vs 4.6%), and a higher median 10-year earnings ($124,080 vs $143,372); however, MIT has "
     "a higher graduation rate (96.2% vs 92.4%).", "higher"),
    # A true number, but a false superlative: Yale's 96.0% is the lowest of the three.
    ([HARVARD, PRINCETON, YALE], "Yale has the highest graduation rate at 96.0%.", "highest"),
    # "More selective" with a higher acceptance rate.
    ([HARVARD, YALE], "Yale is more selective, admitting 3.9% versus Harvard's 3.7%.", "more selective"),
    # Superlative without a number.
    ([HARVARD, PRINCETON, YALE], "Princeton is the most selective of the three.", "most selective"),
    # A number from another school in a clause that only names one.
    ([HARVARD, YALE], "Yale costs $19,066 a year after aid.", "19,066"),
]

CORRECT = [
    ([UCLA, BERKELEY, MICHIGAN],
     "UCLA has the lowest net price at $12,548 and the lowest acceptance rate at 9.0%, while UC Berkeley has the "
     "highest median earnings at $92,446, and the University of Michigan has the highest graduation rate at 93.2%."),
    ([HARVARD, YALE],
     "Harvard University has a lower net price at $19,066 compared to Yale University's $23,777, a higher "
     "graduation rate of 97.5% versus 96.0%, and higher median 10-year earnings of $101,817 compared to "
     "Yale's $100,533."),
    ([MIT, STANFORD],
     "Stanford has a lower net price ($13,807 vs $20,111) and a lower acceptance rate (3.6% vs 4.6%), while MIT has "
     "higher median earnings ($143,372 vs $124,080) and a higher graduation rate (96.2% vs 92.4%)."),
    ([HARVARD, PRINCETON, YALE],
     "Harvard is the most selective at 3.7%, Princeton has the lowest net price at $6,128 and the highest earnings "
     "at $110,066, and Yale has the highest net price at $23,777."),
    ([HARVARD, PRINCETON, YALE], "Harvard is the most selective of the three."),
    ([UCLA, BERKELEY], "For a family making $85,000, UCLA's net price is $12,548 and Berkeley's is $13,481."),
    ([UCLA, BERKELEY, MICHIGAN], "All 3 schools graduate more than 92% of students."),
    ([UCLA], "UCLA's graduation rate is 92.6%."),  # one school: nothing to confuse
    # Real summaries that an earlier version of the check rejected by mistake:
    ([UCLA, BERKELEY, MICHIGAN],
     "UC Berkeley leads in median 10-year earnings at $92,446 and has the second-highest graduation rate at 92.9%, "
     "and University of Michigan-Ann Arbor has the highest graduation rate at 93.2%."),
    ([UCLA, school(123961, "University of Southern California", 32740.0, 0.0981, None, 92498.0)],
     "UCLA has a lower net price at $12,548 compared to USC's $32,740, and a more selective admission rate of "
     "9.0% versus USC's 9.8%."),
]


@pytest.mark.parametrize("rows,summary,expected", MISATTRIBUTED)
def test_misattributed_summaries_are_rejected(rows, summary, expected):
    problems = attribution_problems(summary, rows, "", ALIASES)
    assert problems and any(expected in p for p in problems), problems


@pytest.mark.parametrize("rows,summary", CORRECT)
def test_correct_summaries_pass(rows, summary):
    assert attribution_problems(summary, rows, "My family makes $85,000", ALIASES) == []


def test_ambiguous_short_names_are_not_used():
    # "University of California" names both rows, so it can't attribute a number to either.
    assert attribution_problems("The University of California campuses cost $12,548 and $13,481.",
                                [UCLA, BERKELEY], "", {}) == []


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append([dict(m) for m in kwargs["messages"]])
        return SimpleNamespace(content=[SimpleNamespace(text=self.replies.pop(0))])


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(answering, "school_aliases", lambda rows: ALIASES)

    def install(*replies):
        client = FakeClient(replies)
        monkeypatch.setattr(answering, "_get_client", lambda: client)
        return client
    return install


def test_write_summary_retries_once_then_accepts(fake_claude):
    client = fake_claude(MISATTRIBUTED[0][1], CORRECT[0][1])
    assert answering.write_summary("Compare UCLA, Berkeley and Michigan", [UCLA, BERKELEY, MICHIGAN], "") == \
        CORRECT[0][1]
    assert len(client.calls) == 2 and "93.2" in client.calls[1][-1]["content"]


def test_write_summary_falls_back_to_plain_text(fake_claude):
    fake_claude(MISATTRIBUTED[0][1], MISATTRIBUTED[0][1])
    summary = answering.write_summary("Compare UCLA, Berkeley and Michigan", [UCLA, BERKELEY, MICHIGAN], "")
    assert summary == ("Here is how University of California-Los Angeles, University of California-Berkeley "
                       "and University of Michigan-Ann Arbor compare.")


def test_write_summary_accepts_a_good_first_answer(fake_claude):
    client = fake_claude(CORRECT[1][1])
    assert answering.write_summary("Harvard vs Yale", [HARVARD, YALE], "") == CORRECT[1][1]
    assert len(client.calls) == 1
