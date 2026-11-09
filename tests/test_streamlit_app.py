"""
Smoke tests for the Streamlit demo (streamlit_app.py) with Streamlit's AppTest.

The answer pipeline is replaced by a fake, so no Claude API call is made; the database is the
tiny DuckDB fixture; email sending is captured. No network.

Run from the repo root:
    python -m pytest tests/test_streamlit_app.py -v
"""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import access
import answering

APP = str(Path(__file__).resolve().parents[1] / "streamlit_app.py")
SECRET = "k" * 48
OWNER = "owner@example.com"


@pytest.fixture
def app(duck_db, monkeypatch):
    """An AppTest with secrets set, a fake answer pipeline and captured emails."""
    for name in ("ACCESS_GATE", "ANTHROPIC_API_KEY", "DEV_PRINT_LINKS", "APP_URL"):
        monkeypatch.delenv(name, raising=False)
    st.cache_resource.clear()
    sent, questions = [], []

    def fake_answer(question, history=None):
        questions.append((question, history))
        response = answering.AskResponse(
            question=question, kind="result", response="UCLA's graduation rate is 92.6%.",
            blocks=[{"type": "stats", "title": "UCLA", "school_id": 110662,
                     "items": [{"label": "Graduation rate", "display": "92.6%"}]}],
            notes=["A guardrail note."], source="U.S. Department of Education College Scorecard, 2024-25 data")
        return response, [{"id": 110662, "name": "UCLA", "completion_rate": 0.926}], "SELECT completion_rate ..."

    monkeypatch.setattr(answering, "answer_question", fake_answer)
    monkeypatch.setattr(access, "send_email", lambda config, message: sent.append(message))

    def make(**secrets):
        at = AppTest.from_file(APP, default_timeout=30)
        values = {"ACCESS_SECRET": SECRET, "OWNER_EMAIL": OWNER, "APP_URL": "https://demo.example",
                  "SMTP_USER": OWNER, "SMTP_PASSWORD": "app-password", "QUESTIONS_PER_VISITOR": "2",
                  **secrets}
        for key, value in values.items():
            at.secrets[key] = value
        return at

    make.sent, make.questions = sent, questions
    yield make
    st.cache_resource.clear()


def access_link_token(email="visitor@example.com"):
    return access.make_token(access.ACCESS, email, SECRET, 3600, name="Visitor")


def test_gate_shows_landing_without_access(app):
    at = app().run()
    assert not at.exception
    assert "Request access" in [b.label for b in at.button]
    assert not at.chat_input  # the chat is locked
    assert any("Access is by approval" in m.value for m in at.markdown)


def test_invalid_link_shows_a_warning(app):
    at = app()
    at.query_params["access"] = "forged.token"
    at.run()
    assert not at.chat_input and any("not valid" in w.value for w in at.warning)


def test_valid_access_link_unlocks_the_chat_and_answers(app):
    at = app()
    at.query_params["access"] = access_link_token()
    at.run()
    assert not at.exception and at.chat_input
    assert any("visitor@example.com" in c.value for c in at.sidebar.caption)
    assert at.sidebar.metric[0].value == "2 of 2"

    at.chat_input[0].set_value("What is UCLA's graduation rate?").run()
    assert not at.exception
    assert [m.value for m in at.metric if m.label == "Graduation rate"] == ["92.6%"]
    assert any("graduation rate is 92.6%" in m.value for m in at.markdown)
    assert at.sidebar.metric[0].value == "1 of 2"

    at.chat_input[0].set_value("And Berkeley?").run()  # follow-ups carry the earlier turn
    question, history = app.questions[-1]
    assert question == "And Berkeley?" and history[0]["question"] == "What is UCLA's graduation rate?"

    # The limit of 2 is reached: the upgrade view replaces the chat input.
    assert at.sidebar.metric[0].value == "0 of 2"
    assert any("used today's free questions" in s.value for s in at.subheader)
    assert at.chat_input[0].disabled


def test_limit_reached_shows_the_upgrade_view_without_calling_the_planner(app):
    at = app(QUESTIONS_PER_VISITOR="1")
    at.query_params["access"] = access_link_token()
    at.run()
    at.chat_input[0].set_value("What is UCLA's graduation rate?").run()
    assert len(app.questions) == 1
    assert any("used today's free questions" in s.value for s in at.subheader)
    upgrade = next(b for b in at.button if b.label == "Upgrade to Pro — coming soon")
    assert any("$9" in m.value for m in at.markdown)
    upgrade.click().run()
    assert any("Payments aren't connected" in i.value for i in at.info)  # no checkout is pretended
    [link] = at.get("link_button")
    assert link.proto.label == "Request Pro access" and link.proto.url.startswith(f"mailto:{OWNER}?")
    assert len(app.questions) == 1  # the planner was never called again


def test_plans_link_opens_the_plans_view(app):
    at = app()
    at.query_params["access"] = access_link_token()
    at.run()
    next(b for b in at.sidebar.button if b.label == "Plans").click().run()
    assert [s.value for s in at.subheader] == ["Plans"]
    assert any(b.label == "Upgrade to Pro — coming soon" for b in at.button)


def test_vague_question_gets_quick_replies_without_using_the_ai(app):
    at = app()
    at.query_params["access"] = access_link_token()
    at.run()
    at.chat_input[0].set_value("Help me find a good school").run()
    assert not app.questions and "📍" not in "".join(b.label for b in at.button)
    assert any("specific location" in b.label for b in at.button)
    assert at.sidebar.metric[0].value == "2 of 2"


def test_request_access_emails_the_owner(app):
    at = app().run()
    at.text_input[0].set_value("Ada Lovelace")
    at.text_input[1].set_value("not-an-email")
    at.button[0].click().run()
    assert any("valid email" in e.value for e in at.error) and not app.sent

    at.text_input[1].set_value("Ada@Example.com")
    at.text_area[0].set_value("Counselor at a high school")
    at.button[0].click().run()
    assert any("request was sent" in s.value for s in at.success)
    [message] = app.sent
    assert message["To"] == OWNER and "https://demo.example/?approve=" in message.get_content()


def test_request_without_smtp_says_it_is_not_open(app):
    at = app(SMTP_PASSWORD="PASTE_GMAIL_APP_PASSWORD_HERE").run()
    at.text_input[0].set_value("Ada")
    at.text_input[1].set_value("ada@example.com")
    at.button[0].click().run()
    assert not app.sent and any("not open" in w.value for w in at.warning)


def test_approve_link_sends_the_visitor_a_personal_link(app):
    approve = access.make_token(access.APPROVE, "ada@example.com", SECRET, 3600, name="Ada")
    at = app()
    at.query_params["approve"] = approve
    at.run()
    assert any("ada@example.com" in m.value for m in at.markdown) and not app.sent  # opening alone sends nothing
    at.button[0].click().run()
    assert any("Approved ✓" in s.value for s in at.success)
    [message] = app.sent
    assert message["To"] == "ada@example.com"
    link = next(line for line in message.get_content().splitlines() if "?access=" in line)
    payload = access.verify_token(link.split("?access=")[1], access.ACCESS, SECRET)
    assert payload["e"] == "ada@example.com"


def test_access_token_is_not_an_approve_token(app):
    at = app()
    at.query_params["approve"] = access_link_token()
    at.run()
    assert any("not valid" in e.value for e in at.error)


def test_owner_passcode(app):
    at = app(OWNER_PASSCODE="correct horse battery staple").run()
    at.text_input[-1].set_value("wrong").run()
    at.button[-1].click().run()
    assert any("not right" in e.value for e in at.error) and not at.chat_input
    at.text_input[-1].set_value("correct horse battery staple")
    at.button[-1].click().run()
    assert at.chat_input and any(OWNER in c.value for c in at.sidebar.caption)


def test_school_profile_view(app):
    at = app()
    at.query_params["access"] = access_link_token()
    at.run()
    at.sidebar.segmented_control[0].set_value("School profile").run()
    at.text_input[0].set_value("UCLA").run()
    assert not at.exception
    assert [s.value for s in at.subheader] == ["University of California-Los Angeles"]
    assert ("Graduation rate", "92.6%") in [(m.label, m.value) for m in at.metric]
