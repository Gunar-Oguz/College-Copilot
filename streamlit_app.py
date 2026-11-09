"""
CollegeCopilot demo on Streamlit Community Cloud.

The same planner, guardrails and presentation code as the FastAPI app, called in-process, over a
read-only DuckDB copy of the database. Access is by approval (signed links sent by email, see
backend/access.py), and question limits protect the paid AI API (backend/cost_guard.py).
Accounts, saved schools and Stripe billing belong to the full app and are off here.

Run locally:
    DUCKDB_PATH=/path/to/collegecopilot.duckdb streamlit run streamlit_app.py
"""

import dataclasses
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import altair as alt
import anthropic
import pandas as pd
import streamlit as st

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "backend"))

import access  # noqa: E402
import answering  # noqa: E402
import cost_guard  # noqa: E402
import database  # noqa: E402
import demo_data  # noqa: E402
from guardrails import is_vague  # noqa: E402
from school_profile import school_profile, search_schools  # noqa: E402

logger = logging.getLogger("collegecopilot.demo")
logging.basicConfig(level=logging.INFO)

SCREENSHOTS_DIR = ROOT_DIR / "docs" / "screenshots"
EXAMPLES = [
    "Compare Harvard, Yale, and Princeton on cost, acceptance rate, and earnings",
    "Top 10 universities in California by graduation rate",
    "My family makes $85,000. Which 5 schools in California have the lowest net price for us?",
    "Find 5 schools similar to UCLA but easier to get into",
    "Public colleges within 40 miles of 94720 with a graduation rate above 70%",
    "Which 5 schools have the highest earnings 4 years after a computer science bachelor's?",
]
DATA_SOURCES = (
    "U.S. Department of Education **College Scorecard** (2015-16 to 2024-25) · **IPEDS** (NCES) · "
    "**Opportunity Insights** · Clery Act **campus crime** · **EADA** athletics · **NSF HERD** research · "
    "**VA GI Bill** Comparison Tool · **DAPIP** accreditation · **IRS Form 990** via ProPublica · "
    "**BLS** wages and projections, **O*NET** · **Census ACS** · Common App deadlines"
)
ACCENT = "#1f6feb"

st.set_page_config(page_title="CollegeCopilot", page_icon="🎓", layout="wide")


# ---------- settings and shared resources ----------

def setting(name: str, default: str | None = None) -> str | None:
    """Streamlit secrets first, then the environment."""
    try:
        value = st.secrets.get(name)
    except Exception:  # no secrets.toml (local development)
        value = None
    if value is None or value == "":
        value = os.getenv(name)
    return default if value is None or value == "" else str(value)


def access_config() -> access.AccessConfig:
    config = access.AccessConfig.from_mapping(setting)
    if config.app_url == access.AccessConfig.app_url and not setting("APP_URL"):
        try:
            url = st.context.url
            if url:
                config = dataclasses.replace(config, app_url=url.split("?")[0].rstrip("/"))
        except Exception:
            pass
    return config


@st.cache_resource(show_spinner="Loading the college database...")
def data_file() -> str:
    return str(demo_data.ensure_database(setting))


@st.cache_resource
def duckdb_connection(path: str):
    return database.open_duckdb(path)


@st.cache_resource
def question_budget() -> cost_guard.QuestionBudget:
    return cost_guard.QuestionBudget(
        per_visitor=int(setting("QUESTIONS_PER_VISITOR", str(cost_guard.DEFAULT_PER_VISITOR))),
        per_day=int(setting("QUESTIONS_PER_DAY", str(cost_guard.DEFAULT_PER_DAY))),
    )


@st.cache_resource
def request_limiter() -> access.RateLimiter:
    """All access requests together, so a flood can't fill the owner's inbox."""
    return access.RateLimiter(limit=int(setting("ACCESS_REQUESTS_PER_HOUR", "30")), window_sec=3600)


@st.cache_resource
def approvals_sent() -> set:
    return set()


def session_limiter(key: str, limit: int, window_sec: float) -> access.RateLimiter:
    if key not in st.session_state:
        st.session_state[key] = access.RateLimiter(limit, window_sec)
    return st.session_state[key]


def connect_database() -> None:
    if api_key := setting("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = api_key
    os.environ["DB_BACKEND"] = "duckdb"
    path = data_file()
    database.use_duckdb(duckdb_connection(path), path)


def style() -> None:
    st.html(f"""<style>
      .block-container {{ padding-top: 2rem; max-width: 1100px; }}
      .cc-hero h1 {{ font-size: 2.4rem; margin-bottom: .2rem; }}
      .cc-hero p {{ font-size: 1.1rem; color: #4b5563; }}
      .cc-feature {{ border: 1px solid #e5e7eb; border-radius: 12px; padding: 1rem; height: 100%; background: #fff; }}
      .cc-feature b {{ color: {ACCENT}; }}
      .cc-footer {{ color: #6b7280; font-size: .85rem; border-top: 1px solid #e5e7eb; margin-top: 2rem;
                    padding-top: .8rem; }}
    </style>""")


def footer() -> None:
    st.html(f"<div class='cc-footer'>Data: {DATA_SOURCES.replace('**', '')}. Numbers are formatted from the "
            "data in code; the AI plans the queries and writes a one-sentence summary that is checked against "
            "the data. Built by Gulnar Albushova.</div>")


# ---------- access gate ----------

def current_access() -> dict | None:
    grant = st.session_state.get("access")
    if grant and grant["exp"] > datetime.now(timezone.utc).timestamp():
        return grant
    st.session_state.pop("access", None)
    return None


def grant_from_link(config: access.AccessConfig) -> str | None:
    """Unlock the session from ?access=<token>; returns an error message for a bad link."""
    token = st.query_params.get("access")
    if not token or current_access():
        return None
    if not config.tokens_ready:
        return "Access links are not set up on this deployment yet."
    try:
        payload = access.verify_token(token, access.ACCESS, config.access_secret)
    except access.TokenError:
        return "This access link is not valid or has expired. You can ask for a new one below."
    st.session_state["access"] = {"email": payload["e"], "name": payload["n"], "exp": payload["exp"],
                                  "owner": payload["e"] == config.owner_email}
    return None


def request_access(config: access.AccessConfig, name: str, email: str, note: str) -> tuple[str, str]:
    """(level, message) for the request form; the same reply whatever happens to the email."""
    normalized = access.normalize_email(email)
    name, note = access.clean_text(name, access.MAX_NAME_CHARS), access.clean_text(note, access.MAX_NOTE_CHARS)
    if not name:
        return "error", "Please enter your name."
    if not normalized:
        return "error", "Please enter a valid email address."
    if not session_limiter("request_limit", 3, 3600).allow() or not request_limiter().allow():
        return "error", "Too many requests from here. Please try again in an hour."
    if not config.tokens_ready or not (config.smtp_ready or config.dev_print_links):
        return "warning", "Access requests are not open on this deployment yet. Please try again later."
    token = access.make_token(access.APPROVE, normalized, config.access_secret, access.APPROVE_TTL_SEC, name=name)
    approve_url = access.link(config.app_url, "approve", token)
    if config.smtp_ready:
        try:
            access.send_email(config, access.owner_request_email(config, name, normalized, note, approve_url))
        except Exception as exc:  # SMTP problems are logged, never shown with details
            logger.error("Could not email the access request: %s", type(exc).__name__)
            return "error", "Sorry, the request could not be sent right now. Please try again later."
    else:
        logger.warning("DEV_PRINT_LINKS: approve link for %s: %s", normalized, approve_url)
    return "success", ("Thanks! Your request was sent. If it is approved, you'll get a personal link by email "
                       "(usually within a day).")


def approve_view(config: access.AccessConfig) -> None:
    st.title("Approve access")
    if not config.tokens_ready:
        st.error("ACCESS_SECRET is not set on this deployment.")
        return
    token = st.query_params.get("approve", "")
    try:
        payload = access.verify_token(token, access.APPROVE, config.access_secret)
    except access.TokenError:
        st.error("This approve link is not valid or has expired.")
        return
    email, name = payload["e"], payload["n"]
    signature = token.rsplit(".", 1)[-1]
    if signature in approvals_sent() or st.session_state.get("approved") == signature:
        st.success(f"Approved ✓ The access link was sent to {email}.")
        return
    st.write(f"**{name or 'A visitor'}** ({email}) asked for access.")
    # A button, not the link itself, approves: email scanners that open links can't approve by accident.
    if not st.button("Approve and email the access link", type="primary"):
        return
    access_token = access.make_token(access.ACCESS, email, config.access_secret, access.ACCESS_TTL_SEC, name=name)
    access_url = access.link(config.app_url, "access", access_token)
    if config.smtp_ready:
        try:
            access.send_email(config, access.visitor_access_email(config, name, email, access_url))
        except Exception as exc:
            logger.error("Could not email the access link: %s", type(exc).__name__)
            st.error("The email could not be sent. Check the SMTP secrets and try again.")
            return
    elif config.dev_print_links:
        logger.warning("DEV_PRINT_LINKS: access link for %s: %s", email, access_url)
    else:
        st.error("SMTP is not configured, so the access link can't be emailed. Missing: "
                 + ", ".join(config.missing()))
        return
    approvals_sent().add(signature)
    st.session_state["approved"] = signature
    st.success(f"Approved ✓ The access link was sent to {email}.")


def landing_view(config: access.AccessConfig, link_error: str | None) -> None:
    st.html("<div class='cc-hero'><h1>🎓 CollegeCopilot</h1><p>An AI college-search assistant for admissions "
            "counselors. Ask about 6,000+ U.S. colleges in plain English; every number in the answer comes "
            "from federal data, not from the AI.</p></div>")
    if link_error:
        st.warning(link_error)
    features = [
        ("Plain-English questions", "Costs by family income, acceptance and graduation rates, earnings by major, "
                                    "distance from a ZIP code, campus safety, deadlines."),
        ("Numbers you can trust", "Claude plans read-only SQL with tools; tables and charts are built in code, "
                                  "and a summary that cites a number not in the data is replaced."),
        ("Compare and rank", "Side-by-side comparisons, ranked charts, and 'schools like X but cheaper' "
                             "similarity search."),
        ("25 public sources", "College Scorecard and IPEDS, plus mobility, crime, athletics, research, "
                              "GI Bill, accreditation and career data."),
    ]
    for column, (title, text) in zip(st.columns(len(features)), features):
        column.html(f"<div class='cc-feature'><b>{title}</b><br><span>{text}</span></div>")

    shots = sorted(p for p in SCREENSHOTS_DIR.glob("*.png") if not any(x in p.name for x in ("access", "plans")))
    if shots:
        st.subheader("What it looks like")
        for tab, shot in zip(st.tabs([p.stem.split("-", 1)[-1].replace("-", " ").capitalize() for p in shots]), shots):
            tab.image(str(shot), width="stretch")

    st.subheader("Request access")
    st.write("Access is by approval, because every answer uses a paid AI API. Enter your email: I receive "
             "your request, approve it, and you get a personal link by email.")
    if not (config.tokens_ready and (config.smtp_ready or config.dev_print_links)):
        st.info("Access requests are not open on this deployment yet.")
    with st.form("request_access", clear_on_submit=False):
        name = st.text_input("Name", max_chars=access.MAX_NAME_CHARS)
        email = st.text_input("Email", max_chars=254)
        note = st.text_area("Organisation, or why you'd like to try it", max_chars=access.MAX_NOTE_CHARS)
        submitted = st.form_submit_button("Request access", type="primary")
    if submitted:
        level, message = request_access(config, name, email, note)
        getattr(st, level)(message)
    st.caption("Already approved? Open the personal link from your email.")

    with st.expander("Owner sign-in"):
        with st.form("owner_sign_in"):
            passcode = st.text_input("Passcode", type="password")
            signed_in = st.form_submit_button("Sign in")
        if signed_in:
            if not session_limiter("passcode_limit", 5, 900).allow():
                st.error("Too many attempts. Try again in 15 minutes.")
            elif access.passcode_matches(passcode, config.owner_passcode):
                st.session_state["access"] = {"email": config.owner_email or "owner", "name": "Owner", "owner": True,
                                              "exp": datetime.now(timezone.utc).timestamp() + 12 * 3600}
                st.rerun()
            else:
                st.error("That passcode is not right.")
    footer()


# ---------- answers ----------

def _unique(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for label in labels:
        seen[label] = seen.get(label, 0) + 1
        out.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    return out


def render_stats(block: dict) -> None:
    if block.get("title"):
        st.markdown(f"**{block['title']}**")
    items = block["items"]
    for start in range(0, len(items), 4):
        for column, item in zip(st.columns(4), items[start:start + 4]):
            column.metric(item["label"], item["display"], border=True)


def render_compare(block: dict) -> None:
    schools = _unique(block["schools"])
    frame = pd.DataFrame([[c["display"] for c in row["cells"]] for row in block["rows"]],
                         index=[row["label"] for row in block["rows"]], columns=schools)
    best = pd.DataFrame([[c["best"] for c in row["cells"]] for row in block["rows"]], index=frame.index,
                        columns=schools)
    styled = frame.style.apply(lambda _: best.map(
        lambda b: "background-color: #e7f5ec; color: #11633a; font-weight: 600" if b else ""), axis=None)
    st.dataframe(styled, width="stretch")
    st.caption("Green marks the better value in each row.")


def render_bars(block: dict) -> None:
    items = block["items"]
    data = pd.DataFrame({"School": _unique([i["label"] for i in items]), "value": [i["value"] for i in items],
                         "display": [i["display"] for i in items]})
    order = list(data["School"])
    chart = alt.Chart(data).mark_bar(color=ACCENT, cornerRadiusEnd=3).encode(
        x=alt.X("value:Q", title=block["title"], axis=alt.Axis(labels=False, ticks=False, grid=False)),
        y=alt.Y("School:N", sort=order, title=None, axis=alt.Axis(labelLimit=320)),
        tooltip=[alt.Tooltip("School:N"), alt.Tooltip("display:N", title=block["title"])],
    )
    labels = chart.mark_text(align="left", dx=4, color="#374151").encode(text="display:N")
    st.altair_chart((chart + labels).properties(height=28 * len(items) + 30), width="stretch")


def render_table(block: dict) -> None:
    columns = _unique([c["label"] for c in block["columns"]])
    frame = pd.DataFrame([r["cells"] for r in block["rows"]], columns=columns)
    if any(r["reference"] for r in block["rows"]):
        reference = [r["reference"] for r in block["rows"]]
        frame = frame.style.apply(lambda row: ["background-color: #eef4ff; font-weight: 600"
                                               if reference[row.name] else "" for _ in row], axis=1)
    st.dataframe(frame, width="stretch", hide_index=True)


def render_deadlines(block: dict) -> None:
    st.markdown(f"**{block.get('title') or 'Deadlines'}**")
    st.dataframe(pd.DataFrame([{"Deadline type": r["type"], "Date": r["date"]} for r in block["rows"]]),
                 hide_index=True, width="stretch")


RENDERERS = {"stats": render_stats, "compare": render_compare, "bars": render_bars, "table": render_table,
             "deadlines": render_deadlines}


def ask_buttons(questions: list[str], key: str) -> None:
    with st.container(horizontal=True):
        for i, question in enumerate(questions):
            if st.button(question, key=f"{key}-{i}"):
                st.session_state["pending_question"] = question
                st.rerun()


def render_fallback_school(school: dict) -> None:
    stats = school["stats"]
    st.markdown(f"**{school['name']}** · {school['city']}, {school['state']} · {school.get('type') or ''}")
    shown = [("Graduation rate", stats.get("graduation_rate"), "percent"),
             ("Acceptance rate", stats.get("acceptance_rate"), "percent"),
             ("Average net price", stats.get("net_price"), "money"),
             ("Median earnings (10 yrs)", stats.get("median_earnings"), "money")]
    from presentation import format_value
    for column, (label, value, fmt) in zip(st.columns(4), shown):
        column.metric(label, format_value(value, fmt), border=True)
    if school.get("website"):
        st.markdown(f"[Website]({school['website']})")


def render_response(message: dict, index: int) -> None:
    response = message["response"]
    kind = response["kind"]
    if kind == "result":
        st.markdown(response["response"])
        for block in response.get("blocks", []):
            RENDERERS.get(block["type"], lambda b: st.json(b))(block)
        for note in response.get("notes", []):
            st.caption(f"ℹ️ {note}")
        if response.get("source"):
            st.caption(f"Source: {response['source']}")
        if message.get("sql"):
            with st.expander("How this was answered"):
                st.code(message["sql"], language="sql")
    elif kind == "limit":
        st.warning(response["response"])
    elif kind == "error":
        st.error(response["response"])
    else:
        st.markdown(response["response"])
        fallback = response.get("fallback") or {}
        if fallback.get("school"):
            render_fallback_school(fallback["school"])
        if fallback.get("suggestions"):
            st.caption("Questions I can answer:")
            ask_buttons(fallback["suggestions"], key=f"suggest-{index}")
        if response.get("quick_replies"):
            ask_buttons([q["question"] for q in response["quick_replies"]], key=f"quick-{index}")


def visitor_key(grant: dict) -> str:
    return f"email:{grant['email']}"


def answer(question: str, grant: dict) -> dict:
    """Run one question through the shared pipeline, within the question limits."""
    history = [{"question": m["question"], "answer": m["memory"]} for m in st.session_state["messages"]
               if m.get("memory")][-answering.HISTORY_TURNS:]
    if is_vague(question):  # answered without the AI, so it isn't counted
        response = answering.vague_response(question)
        return {"question": question, "response": response.model_dump(), "memory": response.response}
    budget = question_budget()
    decision = budget.consume(visitor_key(grant), exempt=bool(grant.get("owner")))
    if not decision.allowed:
        text = cost_guard.LIMIT_MESSAGES[decision.reason].format(limit=budget.per_visitor)
        return {"question": question, "response": {"kind": "limit", "response": text}}
    try:
        response, rows, sql = answering.answer_question(question, history)
    except anthropic.APIError as exc:
        budget.refund(visitor_key(grant))
        logger.error("Claude API error: %s", type(exc).__name__)
        return {"question": question, "response": {"kind": "error",
                                                   "response": "The AI service had a problem. Please try again."}}
    except database.DB_ERRORS as exc:
        budget.refund(visitor_key(grant))
        logger.error("Database error: %s", exc)
        return {"question": question, "response": {"kind": "error",
                                                   "response": "Database error while answering. Please try again."}}
    return {"question": question, "response": response.model_dump(), "sql": sql,
            "memory": answering.memory_of(response, rows)}


# Plans as on the full app's pricing page (frontend/pricing.html). The demo has no payments.
PRO_FEATURES = [
    "Unlimited AI questions",
    "Save unlimited schools to your portfolio",
    "Compare up to 10 schools side by side",
    "Deadline tracker with calendar export (.ics)",
    "Access all student reviews",
    "\"Why this school?\" personalized fit analysis",
    "Export school lists to CSV",
    "Application checklist for every school",
    "Priority response queue",
    "Early access to new features",
]
PLAN_ROWS = [
    ("AI questions", "{limit} / day in this demo (10 / day in the full app)", "Unlimited"),
    ("School search and profiles", "✓", "✓"),
    ("Saved schools", "3", "Unlimited"),
    ("Side-by-side compare", "3 schools", "10 schools"),
    ("Student reviews", "Latest 2 per school", "All"),
    ("Deadlines, CSV export, checklists, fit", "—", "✓"),
]


def plans_view(grant: dict, reason: str | None = None) -> None:
    """Free vs Pro. Payments are not connected in the demo, so Pro is requested by email."""
    budget = question_budget()
    if reason == "visitor":
        st.subheader("You've used today's free questions")
        st.write(f"Approved visitors get {budget.per_visitor} questions a day; they reset at midnight UTC. "
                 "Pro has no daily limit.")
    elif reason == "daily":
        st.subheader("The demo has reached today's limit")
        st.write("The demo shares a daily budget for the paid AI service. Please come back after midnight UTC.")
    else:
        st.subheader("Plans")
    free, pro = st.columns(2)
    with free.container(border=True):
        st.markdown("#### Free\n**$0** forever")
        st.caption("Everything you need to start exploring colleges.")
        st.markdown(f"- {budget.per_visitor} AI questions per day (demo)\n- School search and profiles\n"
                    "- Save up to 3 schools\n- Compare up to 3 schools")
    with pro.container(border=True):
        st.markdown("#### Pro &nbsp; :orange-badge[MOST POPULAR]\n**$9** / month")
        st.caption("For serious applicants building and tracking their college list.")
        st.markdown("\n".join(f"- {feature}" for feature in PRO_FEATURES))
        if st.button("Upgrade to Pro — coming soon", type="primary", width="stretch", key="upgrade"):
            st.session_state["upgrade_note"] = True
        if st.session_state.get("upgrade_note"):
            st.info("Payments aren't connected in this demo, so nothing is charged here. The full CollegeCopilot "
                    "app handles Pro subscriptions with Stripe. To try Pro, ask me by email.")
            owner = access_config().owner_email
            if owner:
                subject = quote("CollegeCopilot Pro access")
                body = quote(f"Hi Gulnar, I'd like Pro access to the CollegeCopilot demo. My email: {grant['email']}")
                st.link_button("Request Pro access", f"mailto:{owner}?subject={subject}&body={body}",
                               width="stretch")
    st.markdown("**Compare plans**")
    st.dataframe(pd.DataFrame([(feature, free_value.format(limit=budget.per_visitor), pro_value)
                               for feature, free_value, pro_value in PLAN_ROWS],
                              columns=["Feature", "Free", "Pro ($9/month)"]), hide_index=True, width="stretch")


def chat_view(grant: dict) -> None:
    st.session_state.setdefault("messages", [])
    st.title("🎓 CollegeCopilot")
    st.caption("Ask about U.S. colleges: costs, admissions, outcomes, majors, distance, safety. "
               "Follow-up questions work too (\"only the public ones\").")
    if not st.session_state["messages"]:
        st.markdown("**Try one of these:**")
        ask_buttons(EXAMPLES, key="example")
    for index, message in enumerate(st.session_state["messages"]):
        with st.chat_message("user"):
            st.markdown(message["question"])
        with st.chat_message("assistant", avatar="🎓"):
            render_response(message, index)

    left = question_budget().peek(visitor_key(grant))
    out_of_questions = left.daily_left == 0 or (left.visitor_left == 0 and not grant.get("owner"))
    if out_of_questions:  # the upgrade view replaces the input; no AI call is possible
        plans_view(grant, "daily" if left.daily_left == 0 else "visitor")
    typed = st.chat_input("Ask about colleges, e.g. \"Cheapest UC school for a family making $60,000\"",
                          max_chars=500, disabled=out_of_questions)
    question = (typed or st.session_state.pop("pending_question", None) or "").strip()
    if question and not out_of_questions:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant", avatar="🎓"):
            with st.spinner("Looking it up..."):
                message = answer(question, grant)
            st.session_state["messages"].append(message)
        st.rerun()


def profile_view() -> None:
    from presentation import format_value
    st.title("🏫 School profile")
    query = st.text_input("Search for a school", placeholder="e.g. UCLA, Ohio State, Wash U")
    matches = search_schools(query) if query else []
    if query and not matches:
        st.info("No school found with that name.")
    if not matches:
        return
    choice = st.selectbox("School", matches, format_func=lambda s: f"{s['name']} ({s['city']}, {s['state']})")
    profile = school_profile(choice["id"])
    if not profile:
        st.info("No data for this school.")
        return
    st.subheader(profile["name"])
    meta = " · ".join(x for x in [f"{profile['city']}, {profile['state']}", profile.get("type"),
                                  profile.get("carnegie")] if x)
    st.caption(meta)
    if profile.get("website"):
        st.markdown(f"[{profile['website']}]({profile['website']})")
    s = profile["stats"]
    stats = [("Graduation rate", s["graduation_rate"], "percent"),
             ("Acceptance rate", s["acceptance_rate"], "percent"),
             ("Average net price", s["net_price"], "money"),
             ("Median earnings (10 yrs)", s["median_earnings"], "money"),
             ("In-state tuition", s["tuition_in_state"], "money"),
             ("Out-of-state tuition", s["tuition_out_of_state"], "money"),
             ("Students", s["students"], "int"), ("Student-faculty ratio", s["student_faculty_ratio"], "number")]
    for start in range(0, len(stats), 4):
        for column, (label, value, fmt) in zip(st.columns(4), stats[start:start + 4]):
            column.metric(label, format_value(value, fmt), border=True)
    if profile.get("score"):
        score = profile["score"]
        st.markdown(f"**CollegeCopilot Score: {score['score']:.1f}** (rank #{score['rank']:,} of "
                    f"{score['out_of']:,} colleges)")
        for part in score["components"]:
            if part["score"] is not None:
                st.progress(min(1.0, float(part["score"]) / 100),
                            text=f"{part['label']} ({part['weight']}%): {float(part['score']):.0f}")
    if profile.get("designations"):
        st.markdown("**Recognitions:** " + ", ".join(profile["designations"]))
    if profile.get("deadlines"):
        st.markdown("**Application deadlines**")
        st.dataframe(pd.DataFrame([{"Type": d["deadline_type"].replace("_", " ").capitalize(),
                                    "Date": format_value(d["deadline_date"], "date") if d["deadline_date"] else "",
                                    "Notes": d.get("notes") or ""} for d in profile["deadlines"]]),
                     hide_index=True, width="stretch")
    if profile.get("safety"):
        safety = profile["safety"]
        st.markdown(f"**Campus safety (2024):** {format_value(safety['criminal_offenses_total'], 'int')} reported "
                    f"crimes on campus, {format_value(safety['criminal_offenses_per_1000_students'], 'number')} per "
                    "1,000 students (Clery Act).")
    if profile.get("majors"):
        st.markdown("**Majors by degree level**")
        for group in profile["majors"]:
            with st.expander(f"{group['level']} ({len(group['programs'])})"):
                st.write(", ".join(p.rstrip(".") for p in group["programs"]))


def sidebar(grant: dict) -> str:
    with st.sidebar:
        st.markdown("### 🎓 CollegeCopilot")
        view = st.segmented_control("View", ["Ask", "School profile", "Plans"], default="Ask", key="view",
                                    label_visibility="collapsed")
        st.caption(f"Signed in as {grant['email']}")
        budget = question_budget()
        left = budget.peek(visitor_key(grant))
        if grant.get("owner"):
            st.metric("Questions left today (whole demo)", left.daily_left)
        else:
            remaining = min(left.visitor_left, left.daily_left)
            st.metric("Questions left today", f"{remaining} of {budget.per_visitor}")
            st.progress(remaining / budget.per_visitor if budget.per_visitor else 0.0)
            st.caption("Resets at midnight UTC.")
            st.button("Plans", type="tertiary", icon=":material/workspace_premium:",
                      on_click=lambda: st.session_state.update(view="Plans"))
        if st.button("New chat", width="stretch"):
            st.session_state["messages"] = []
            st.rerun()
        with st.expander("Data sources"):
            st.markdown(DATA_SOURCES)
            st.caption("Every table and column: docs/DATA_SOURCES.md in the repository.")
        st.caption("Demo build: read-only data, no accounts or payments.")
    return view or "Ask"


def main() -> None:
    style()
    config = access_config()
    if "approve" in st.query_params:
        approve_view(config)
        return
    link_error = grant_from_link(config)
    if setting("ACCESS_GATE", "on").lower() == "off" and not current_access():  # local development only
        st.session_state["access"] = {"email": "developer@localhost", "name": "Developer", "owner": True,
                                      "exp": datetime.now(timezone.utc).timestamp() + 3600}
    grant = current_access()
    if not grant:
        landing_view(config, link_error)
        return
    try:
        connect_database()
    except demo_data.DataFileError as exc:
        st.error(f"The database could not be loaded: {exc}")
        return
    view = sidebar(grant)
    if view == "School profile":
        profile_view()
    elif view == "Plans":
        plans_view(grant)
    else:
        chat_view(grant)
    footer()


main()
