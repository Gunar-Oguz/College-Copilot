import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import admin_page
import app_logs
import billing
import photos
import plans
import pro_features
import stripe
import user_data
from answering import (  # noqa: F401 (re-exported: tests and evals use main.<name>)
    HISTORY_TURNS,
    MEMORY_CHARS,
    AskResponse,
    _get_client,
    answer_question,
    cannot_answer_response,
    memory_of,
    vague_response,
)
from database import execute_sql, get_latest_year
from guardrails import VAGUE_MESSAGE, is_vague  # noqa: F401
from school_profile import compare_schools, school_basics, school_profile, search_schools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        user_data.ensure_user_tables()
        app_logs.ensure_log_tables()
        photos.ensure_photo_table()
        plans.ensure_plan_tables()
        pro_features.ensure_pro_tables()
    except psycopg2.Error as e:
        logger.error("Could not create user or log tables: %s", e)
    yield


app = FastAPI(title="CollegeCopilot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Turn(BaseModel):
    question: str = Field(max_length=500)
    answer: str = Field(max_length=800)  # the "memory" text returned with that answer


class AskRequest(BaseModel):
    question: str
    history: list[Turn] = Field(default=[], max_length=10)  # this chat's earlier turns, oldest first


@app.get("/health")
def health():
    try:
        result = execute_sql(
            "SELECT COUNT(*) AS total FROM schools WHERE year = %s", (get_latest_year(),)
        )
        total = result[0]["total"] if result else 0
    except Exception:
        total = 0
    return {"status": "healthy", "schools_in_database": total}


def _log(question: str, sql: str | None, answer: str, started: float, kind: str) -> int | None:
    """Log a question; logging problems never break the answer."""
    try:
        return app_logs.log_query(question, sql, answer, round((time.monotonic() - started) * 1000), kind)
    except psycopg2.Error as e:
        logger.error("Could not log query: %s", e)
        return None


LIMIT_MESSAGE = (
    "You've used your {limit} free questions for today. They reset at midnight UTC. "
    "Upgrade to Pro for unlimited questions."
)


def _optional_user_id(authorization: str | None) -> str | None:
    """The signed-in user's id, or None for visitors and expired sessions."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        return None
    try:
        return user_data.get_user(token)["id"]
    except user_data.AuthError:
        return None


def _client_ip(http: Request) -> str | None:
    forwarded = http.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else (http.client.host if http.client else None)


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest, http: Request, authorization: str | None = Header(default=None)):
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    logger.info("Question: %s", question)
    started = time.monotonic()
    if is_vague(question):  # answered without the AI, so it doesn't count toward the daily limit
        response = vague_response(question)
        response.log_id = _log(question, None, response.response, started, "clarify")
        response.memory = response.response
        return response
    user_id = _optional_user_id(authorization)
    pro = plans.is_pro(user_id)
    limit = None if pro else plans.FREE_DAILY_QUESTIONS
    allowed, used = plans.consume_question(user_id or plans.visitor_id(_client_ip(http)), limit)
    usage = {"used": used, "limit": limit, "plan": "pro" if pro else "free", "signed_in": bool(user_id)}
    if not allowed:
        message = LIMIT_MESSAGE.format(limit=limit)
        response = AskResponse(question=question, kind="limit", response=message, usage=usage)
        response.log_id = _log(question, None, message, started, "limit")
        return response
    try:
        with plans.ai_gate.turn(pro):  # Pro requests are answered first when the AI is busy
            history = [t.model_dump() for t in request.history[-HISTORY_TURNS:]]
            response, rows, sql = answer_question(question, history)
    except psycopg2.Error as e:
        logger.error("Database error: %s", e)
        _log(question, None, f"Database error: {e}", started, "error")
        raise HTTPException(status_code=500, detail="Database error while answering the question")
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        _log(question, None, f"Claude API error: {e}", started, "error")
        raise HTTPException(status_code=502, detail="AI service error, please try again")

    text = " ".join([response.response, *response.notes])
    response.log_id = _log(question, sql, text, started, response.kind)
    response.usage = usage
    response.memory = memory_of(response, rows)
    return response


class FeedbackRequest(BaseModel):
    log_id: int
    rating: str  # "up" or "down"


@app.post("/feedback")
def feedback(request: FeedbackRequest):
    if request.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if not app_logs.save_feedback(request.log_id, request.rating):
        raise HTTPException(status_code=404, detail="Answer not found")
    return {"saved": True}


@app.get("/admin", response_class=HTMLResponse)
def admin(token: str | None = Query(default=None)):
    """Usage dashboard. Open by default; set ADMIN_TOKEN to require /admin?token=..."""
    expected = os.getenv("ADMIN_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Admin token required")
    return admin_page.render(app_logs.admin_stats())


class NoteRequest(BaseModel):
    note_text: str


def _user_id(authorization: str | None) -> str:
    token = (authorization or "").removeprefix("Bearer ").strip()
    try:
        return user_data.get_user(token)["id"]
    except user_data.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _note_text(request: NoteRequest) -> str:
    text = request.note_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note cannot be empty")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Note is too long (5,000 characters max)")
    return text


@app.get("/config")
def config():
    """Public Supabase settings for the browser (the anon key is meant to be public)."""
    return {"supabase": user_data.supabase_config()}


@app.get("/me/favorites")
def my_favorites(authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    return {"favorites": user_data.list_favorites(user_id)}


@app.get("/me/favorite-ids")
def my_favorite_ids(authorization: str | None = Header(default=None)):
    return {"ids": user_data.list_favorite_ids(_user_id(authorization))}


@app.put("/me/favorites/{unitid}")
def save_favorite(unitid: int, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    if not execute_sql("SELECT 1 FROM schools WHERE id = %s LIMIT 1", (unitid,)):
        raise HTTPException(status_code=404, detail="School not found")
    saved = user_data.list_favorite_ids(user_id)
    if unitid not in saved and len(saved) >= plans.FREE_SAVED_SCHOOLS and not plans.is_pro(user_id):
        _upgrade_required(f"Free accounts can save up to {plans.FREE_SAVED_SCHOOLS} schools. "
                          "Upgrade to Pro to save as many as you like.")
    user_data.add_favorite(user_id, unitid)
    return {"saved": True}


@app.delete("/me/favorites/{unitid}")
def unsave_favorite(unitid: int, authorization: str | None = Header(default=None)):
    user_data.remove_favorite(_user_id(authorization), unitid)
    return {"saved": False}


@app.post("/me/favorites/{unitid}/notes")
def create_note(unitid: int, request: NoteRequest, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    if unitid not in user_data.list_favorite_ids(user_id):
        raise HTTPException(status_code=400, detail="Save the school before adding notes")
    return user_data.add_note(user_id, unitid, _note_text(request))


@app.put("/me/notes/{note_id}")
def edit_note(note_id: int, request: NoteRequest, authorization: str | None = Header(default=None)):
    note = user_data.update_note(_user_id(authorization), note_id, _note_text(request))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@app.delete("/me/notes/{note_id}")
def remove_note(note_id: int, authorization: str | None = Header(default=None)):
    if not user_data.delete_note(_user_id(authorization), note_id):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"deleted": True}


class ReviewRequest(BaseModel):
    rating: int
    review_text: str = ""


@app.get("/api/school/{unitid}")
def school_api(unitid: int, authorization: str | None = Header(default=None)):
    pro = plans.is_pro(_optional_user_id(authorization))
    profile = school_profile(unitid, review_limit=50 if pro else plans.FREE_VISIBLE_REVIEWS)
    if not profile:
        raise HTTPException(status_code=404, detail="School not found")
    profile["reviews"]["locked"] = 0 if pro else max(0, profile["reviews"]["count"] - plans.FREE_VISIBLE_REVIEWS)
    return profile


@app.get("/api/schools/search")
def school_search(q: str = Query(default="", max_length=100)):
    return {"schools": search_schools(q)}


@app.get("/api/schools/basic")
def schools_basic(ids: str = Query(..., max_length=200)):
    try:
        school_ids = [int(i) for i in ids.split(",") if i.strip()][:10]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be numbers")
    return {"schools": [{k: s[k] for k in ("id", "name", "city", "state", "type", "url")}
                        for s in school_basics(school_ids)]}


@app.get("/api/compare")
def compare_api(ids: str = Query(..., description="2-3 comma-separated UNITIDs (up to 10 on Pro)"),
                authorization: str | None = Header(default=None)):
    try:
        school_ids = list(dict.fromkeys(int(i) for i in ids.split(",") if i.strip()))
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be numbers")
    if len(school_ids) > plans.FREE_COMPARE_SCHOOLS and len(school_ids) <= plans.PRO_COMPARE_SCHOOLS \
            and not plans.is_pro(_optional_user_id(authorization)):
        _upgrade_required(f"Free accounts compare up to {plans.FREE_COMPARE_SCHOOLS} schools. "
                          f"Upgrade to Pro to compare up to {plans.PRO_COMPARE_SCHOOLS}.")
    if not 2 <= len(school_ids) <= plans.PRO_COMPARE_SCHOOLS:
        raise HTTPException(status_code=400, detail=f"Pick 2 to {plans.PRO_COMPARE_SCHOOLS} schools to compare")
    result = compare_schools(school_ids)
    if len(result["schools"]) != len(school_ids):
        raise HTTPException(status_code=404, detail="School not found")
    return result


@app.get("/me/reviews/{unitid}")
def get_my_review(unitid: int, authorization: str | None = Header(default=None)):
    return {"review": user_data.my_review(_user_id(authorization), unitid)}


@app.put("/me/reviews/{unitid}")
def put_my_review(unitid: int, request: ReviewRequest, authorization: str | None = Header(default=None)):
    user_id = _user_id(authorization)
    if not 1 <= request.rating <= 5:
        raise HTTPException(status_code=400, detail="Rating must be 1 to 5 stars")
    text = request.review_text.strip()
    if len(text) > user_data.REVIEW_TEXT_MAX:
        raise HTTPException(status_code=400, detail="Review is too long (2,000 characters max)")
    if not school_basics([unitid]):
        raise HTTPException(status_code=404, detail="School not found")
    return {"review": user_data.save_review(user_id, unitid, request.rating, text)}


@app.delete("/me/reviews/{unitid}")
def delete_my_review(unitid: int, authorization: str | None = Header(default=None)):
    if not user_data.delete_review(_user_id(authorization), unitid):
        raise HTTPException(status_code=404, detail="Review not found")
    return {"deleted": True}


# ---------- Plans and billing ----------

def _upgrade_required(message: str):
    raise HTTPException(status_code=402, detail={"message": message, "upgrade": True})


def _require_pro(authorization: str | None) -> str:
    user_id = _user_id(authorization)
    if not plans.is_pro(user_id):
        _upgrade_required("This is a Pro feature. Upgrade to Pro to use it.")
    return user_id


def _origin(http: Request) -> str:
    """Base URL for Stripe redirects: APP_URL, else the browser's Origin, else this request's host."""
    configured = os.getenv("APP_URL")
    if configured:
        return configured.rstrip("/")
    origin = http.headers.get("origin")
    if origin and re.fullmatch(r"https?://[\w.-]+(:\d+)?", origin):
        return origin
    proto = http.headers.get("x-forwarded-proto", http.url.scheme)
    return f"{proto}://{http.headers.get('host', http.url.netloc)}"


def _billing_call(fn, *args):
    try:
        return fn(*args)
    except billing.BillingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.StripeError as e:
        logger.error("Stripe error: %s", e)
        raise HTTPException(status_code=502, detail="Payment service error, please try again")


@app.get("/api/subscription")
def subscription(http: Request, sync: bool = False, authorization: str | None = Header(default=None)):
    """Current plan, limits and today's question count (works signed out too)."""
    user_id = _optional_user_id(authorization)
    plan = _billing_call(billing.sync_from_stripe, user_id) if (sync and user_id) else plans.get_plan(user_id)
    limits = plans.limits_for(user_id)
    used = plans.questions_today(user_id or plans.visitor_id(_client_ip(http)))
    return {**plan, "limits": limits, "questions_today": used, "signed_in": bool(user_id),
            "payments_enabled": billing.configured()}


@app.post("/api/create-checkout-session")
def create_checkout_session(http: Request, authorization: str | None = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    user_id = _user_id(authorization)
    email = user_data.get_user(token).get("email")
    return {"url": _billing_call(billing.create_checkout_session, user_id, email, _origin(http))}


@app.post("/api/billing-portal")
def billing_portal(http: Request, authorization: str | None = Header(default=None)):
    return {"url": _billing_call(billing.create_portal_session, _user_id(authorization), _origin(http))}


@app.post("/api/cancel-subscription")
def cancel_subscription(authorization: str | None = Header(default=None)):
    return _billing_call(billing.cancel_subscription, _user_id(authorization))


@app.post("/api/webhook")
async def stripe_webhook(http: Request):
    payload = await http.body()
    try:
        result = await run_in_threadpool(billing.handle_webhook, payload, http.headers.get("stripe-signature"))
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except (billing.BillingError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("Stripe webhook: %s", result)
    return {"received": True}


# ---------- Pro features ----------

class ProfileRequest(BaseModel):
    gpa: float | None = None
    sat: int | None = None
    act: int | None = None
    family_income: int | None = None
    budget: int | None = None
    intended_major: str | None = None
    home_state: str | None = None
    preferred_size: str | None = None


class ChecklistItemRequest(BaseModel):
    item_text: str | None = None
    done: bool | None = None


def _check_profile(p: ProfileRequest) -> dict:
    data = p.model_dump()
    checks = [("gpa", 0, 5), ("sat", 400, 1600), ("act", 1, 36), ("family_income", 0, 10_000_000),
              ("budget", 0, 1_000_000)]
    for field, lo, hi in checks:
        if data[field] is not None and not lo <= data[field] <= hi:
            raise HTTPException(status_code=400, detail=f"{field.replace('_', ' ')} must be between {lo:,} and {hi:,}")
    if data["home_state"]:
        data["home_state"] = data["home_state"].strip().upper()[:2]
    if data["preferred_size"] not in (None, "small", "medium", "large", "any"):
        raise HTTPException(status_code=400, detail="preferred_size must be small, medium, large or any")
    if data["intended_major"]:
        data["intended_major"] = data["intended_major"].strip()[:100]
    return data


@app.get("/me/profile")
def my_profile(authorization: str | None = Header(default=None)):
    return {"profile": pro_features.get_profile(_user_id(authorization))}


@app.put("/me/profile")
def save_my_profile(request: ProfileRequest, authorization: str | None = Header(default=None)):
    return {"profile": pro_features.save_profile(_user_id(authorization), _check_profile(request))}


@app.get("/me/fit/{unitid}")
def my_fit(unitid: int, authorization: str | None = Header(default=None)):
    user_id = _require_pro(authorization)
    profile = pro_features.get_profile(user_id)
    with plans.ai_gate.turn(True):
        facts = pro_features.fit_facts(unitid, profile)
        if not facts:
            raise HTTPException(status_code=404, detail="School not found")
        try:
            facts["summary"] = pro_features.fit_summary(facts, _get_client())
        except anthropic.APIError as e:
            logger.error("Fit summary failed: %s", e)
            facts["summary"] = None
    facts["profile_complete"] = any(profile.get(k) for k in ("sat", "act", "family_income", "intended_major"))
    return facts


@app.get("/me/checklist/{unitid}")
def my_checklist(unitid: int, authorization: str | None = Header(default=None)):
    return {"items": pro_features.get_checklist(_require_pro(authorization), unitid)}


@app.post("/me/checklist/{unitid}")
def add_checklist_item(unitid: int, request: ChecklistItemRequest, authorization: str | None = Header(default=None)):
    user_id = _require_pro(authorization)
    text = (request.item_text or "").strip()
    if not text or len(text) > 300:
        raise HTTPException(status_code=400, detail="Item must be 1 to 300 characters")
    return pro_features.add_checklist_item(user_id, unitid, text)


@app.put("/me/checklist/items/{item_id}")
def update_checklist_item(item_id: int, request: ChecklistItemRequest, authorization: str | None = Header(default=None)):  # noqa: E501
    user_id = _require_pro(authorization)
    text = request.item_text.strip() if request.item_text is not None else None
    if text is not None and not 1 <= len(text) <= 300:
        raise HTTPException(status_code=400, detail="Item must be 1 to 300 characters")
    item = pro_features.update_checklist_item(user_id, item_id, text, request.done)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.delete("/me/checklist/items/{item_id}")
def delete_checklist_item(item_id: int, authorization: str | None = Header(default=None)):
    if not pro_features.delete_checklist_item(_require_pro(authorization), item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return {"deleted": True}


@app.get("/me/deadlines.ics")
def my_deadlines_calendar(unitid: int | None = None, authorization: str | None = Header(default=None)):
    user_id = _require_pro(authorization)
    ids = [unitid] if unitid else user_data.list_favorite_ids(user_id)
    return Response(pro_features.deadlines_ics(ids), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="college-deadlines.ics"'})


@app.get("/me/favorites.csv")
def my_favorites_csv(http: Request, authorization: str | None = Header(default=None)):
    user_id = _require_pro(authorization)
    favorites = user_data.list_favorites(user_id)
    ids = [f["school_id"] for f in favorites]
    body = pro_features.favorites_csv(favorites, pro_features._deadlines(ids),
                                      pro_features.checklist_progress(user_id, ids), _origin(http))
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="my-college-list.csv"'})


def _page(name: str) -> FileResponse:
    path = FRONTEND_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(path)


@app.get("/school/{unitid}")
def school_page(unitid: int):
    return _page("school.html")


@app.get("/compare")
def compare_page():
    return _page("compare.html")


@app.get("/pricing")
def pricing_page():
    return _page("pricing.html")


@app.get("/terms")
def terms_page():
    return _page("terms.html")


@app.get("/privacy")
def privacy_page():
    return _page("privacy.html")


@app.get("/")
def home():
    """Landing page; it forwards signed-in visitors to /chat in the browser."""
    return _page("landing.html")


@app.get("/chat")
def chat_page():
    return _page("chat.html")


@app.get("/api/stats")
def site_stats():
    """Numbers shown on the landing page."""
    counts = execute_sql(
        "SELECT (SELECT count(*) FROM schools WHERE year = (SELECT max(year) FROM schools)) AS schools, "
        "(SELECT count(*) FROM copilot_scores) AS ranked, "
        "(SELECT count(*) FROM school_deadlines) AS deadlines, "
        "(SELECT count(DISTINCT unitid) FROM school_deadlines) AS deadline_schools"
    )[0]
    return counts
