"""
Tests for plans, limits, Stripe billing and Pro features. Stripe and Supabase are replaced by
fakes; the database is real. Test rows are removed afterwards.

Run from the repo root:
    python -m pytest tests/test_billing.py -v
"""

import hashlib
import hmac
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import billing  # noqa: E402
import main  # noqa: E402
import plans  # noqa: E402
import pro_features  # noqa: E402
import user_data  # noqa: E402
from database import UnsafeQueryError, execute_sql, get_db_connection, run_readonly_query  # noqa: E402

USERS = {"tok-free": {"id": "test-bill-free", "email": "free@example.com"},
         "tok-pro": {"id": "test-bill-pro", "email": "pro@example.com"}}
HARVARD, MIT, UCLA, BERKELEY, USC, STANFORD = 166027, 166683, 110662, 110635, 123961, 243744
WEBHOOK_SECRET = "whsec_test_secret"


def _db_ready() -> bool:
    try:
        return execute_sql("SELECT to_regclass('copilot_scores') IS NOT NULL AS ok")[0]["ok"]
    except psycopg2.Error:
        return False


pytestmark = pytest.mark.skipif(not _db_ready(), reason="Postgres with app tables not available")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _cleanup():
    with get_db_connection() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM user_plans WHERE user_id LIKE 'test-bill-%'")
        cursor.execute("DELETE FROM usage_tracking WHERE user_id LIKE 'test-bill-%' OR user_id LIKE 'pytest:%'")
        cursor.execute("DELETE FROM stripe_events WHERE id LIKE 'evt_test_%'")
        cursor.execute("DELETE FROM query_logs WHERE question LIKE '[billing-test]%'")
        for uid in ("test-bill-free", "test-bill-pro"):
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
            cursor.execute("DELETE FROM user_favorites WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM user_checklist_items WHERE user_id = %s", (uid,))
            cursor.execute("DELETE FROM user_profiles WHERE user_id = %s", (uid,))


def make_pro(user_id="test-bill-pro", status="active"):
    plans.save_subscription(user_id, f"cus_{user_id}", f"sub_{user_id}", status, None, False)


@pytest.fixture
def client(monkeypatch):
    def fake_get_user(token):
        if token not in USERS:
            raise user_data.AuthError("Session expired, please sign in again")
        return USERS[token]

    def fake_answer(question, history=None):
        return main.AskResponse(question=question, kind="result", response="ok"), [], "SELECT 1"

    monkeypatch.setattr(user_data, "get_user", fake_get_user)
    monkeypatch.setattr(main, "answer_question", fake_answer)
    monkeypatch.setattr(plans, "visitor_id", lambda ip: "pytest:visitor")
    monkeypatch.setattr("school_profile.get_photo", lambda unitid: None)
    for ensure in (user_data.ensure_user_tables, plans.ensure_plan_tables, pro_features.ensure_pro_tables):
        ensure()
    _cleanup()
    make_pro()
    yield TestClient(main.app)
    _cleanup()


# ---------- Limits ----------

def test_daily_limit_is_atomic():
    plans.ensure_plan_tables()
    key = "pytest:atomic"
    results = []
    threads = [threading.Thread(target=lambda: results.append(plans.consume_question(key, 10)[0])) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 10 and plans.questions_today(key) == 10
    execute_sql_write = plans.execute_write
    execute_sql_write("DELETE FROM usage_tracking WHERE user_id = %s", (key,))


def test_signed_out_and_free_users_hit_the_daily_limit(client):
    for i in range(plans.FREE_DAILY_QUESTIONS):
        assert client.post("/ask", json={"question": f"[billing-test] q{i}"}).json()["kind"] == "result"
    blocked = client.post("/ask", json={"question": "[billing-test] one more"}).json()
    assert blocked["kind"] == "limit" and "Upgrade to Pro" in blocked["response"]
    assert blocked["usage"] == {"used": 10, "limit": 10, "plan": "free", "signed_in": False}


def test_pro_has_no_daily_limit(client):
    for i in range(plans.FREE_DAILY_QUESTIONS + 2):
        data = client.post("/ask", json={"question": f"[billing-test] pro {i}"}, headers=auth("tok-pro")).json()
        assert data["kind"] == "result"
    assert data["usage"]["limit"] is None and data["usage"]["plan"] == "pro"


def test_subscription_endpoint(client):
    free = client.get("/api/subscription", headers=auth("tok-free")).json()
    assert free["plan"] == "free" and free["limits"]["saved_schools"] == 3
    pro = client.get("/api/subscription", headers=auth("tok-pro")).json()
    assert pro["plan"] == "pro" and pro["limits"]["compare_schools"] == 10
    assert client.get("/api/subscription").json()["signed_in"] is False


def test_free_users_save_up_to_three(client):
    for school in (HARVARD, MIT, UCLA):
        assert client.put(f"/me/favorites/{school}", headers=auth("tok-free")).status_code == 200
    blocked = client.put(f"/me/favorites/{STANFORD}", headers=auth("tok-free"))
    assert blocked.status_code == 402 and blocked.json()["detail"]["upgrade"] is True
    assert client.put(f"/me/favorites/{HARVARD}", headers=auth("tok-free")).status_code == 200  # re-saving is fine
    for school in (HARVARD, MIT, UCLA, STANFORD):
        assert client.put(f"/me/favorites/{school}", headers=auth("tok-pro")).status_code == 200


def test_compare_limits(client):
    four = f"{HARVARD},{MIT},{UCLA},{STANFORD}"
    assert client.get(f"/api/compare?ids={four}", headers=auth("tok-free")).status_code == 402
    assert len(client.get(f"/api/compare?ids={four}", headers=auth("tok-pro")).json()["schools"]) == 4


def test_pro_only_endpoints_ask_free_users_to_upgrade(client):
    for path in (f"/me/checklist/{HARVARD}", "/me/deadlines.ics", "/me/favorites.csv", f"/me/fit/{MIT}"):
        response = client.get(path, headers=auth("tok-free"))
        assert response.status_code == 402 and response.json()["detail"]["upgrade"], path
        assert client.get(path).status_code == 401


def test_planner_sql_cannot_read_billing_tables():
    for table in ("user_plans", "usage_tracking", "stripe_events", "user_profiles", "user_checklist_items"):
        with pytest.raises(UnsafeQueryError):
            run_readonly_query(f"SELECT * FROM {table}")


# ---------- Stripe ----------

def _signed(event: dict) -> tuple[bytes, str]:
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return payload.encode(), f"t={ts},v1={sig}"


def _subscription_event(event_id, status, customer="cus_test-bill-free", cancel=False, event_type=None):
    return {"id": event_id, "object": "event", "type": event_type or "customer.subscription.updated",
            "api_version": "2026-09-30.endive", "data": {"object": {
                "id": "sub_free_user", "object": "subscription", "customer": customer, "status": status,
                "cancel_at_period_end": cancel, "metadata": {},
                "items": {"object": "list", "data": [{"id": "si_1", "object": "subscription_item",
                                                      "current_period_end": 1893456000}]}}}}


@pytest.fixture
def stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(billing, "_client", None)
    plans.save_customer("test-bill-free", "cus_test-bill-free")


def test_webhook_rejects_bad_signatures(client, stripe_env):
    payload, _ = _signed(_subscription_event("evt_test_bad", "active"))
    response = client.post("/api/webhook", content=payload, headers={"stripe-signature": "t=1,v1=nope"})
    assert response.status_code == 400
    assert plans.get_plan("test-bill-free")["plan"] == "free"


def test_webhook_subscription_lifecycle(client, stripe_env):
    def send(event):
        payload, sig = _signed(event)
        return client.post("/api/webhook", content=payload, headers={"stripe-signature": sig})

    assert send(_subscription_event("evt_test_1", "active", event_type="customer.subscription.created")).status_code == 200
    plan = plans.get_plan("test-bill-free")
    assert plan["plan"] == "pro" and plan["current_period_end"].year == 2030
    send(_subscription_event("evt_test_2", "past_due"))
    assert plans.get_plan("test-bill-free")["plan"] == "pro"  # Stripe is retrying the payment
    send(_subscription_event("evt_test_3", "active", cancel=True))
    assert plans.get_plan("test-bill-free")["cancel_at_period_end"] is True
    send(_subscription_event("evt_test_4", "canceled", event_type="customer.subscription.deleted"))
    assert plans.get_plan("test-bill-free")["plan"] == "free"
    # A replayed event is ignored.
    send(_subscription_event("evt_test_1", "active", event_type="customer.subscription.created"))
    assert plans.get_plan("test-bill-free")["plan"] == "free"


def test_checkout_session_parameters(client, stripe_env, monkeypatch):
    calls = {}

    def record(name, result):
        def fn(*args, **kwargs):
            calls[name] = kwargs.get("params", args[-1] if args else None)
            return result
        return fn

    fake = SimpleNamespace(v1=SimpleNamespace(
        prices=SimpleNamespace(list=record("prices.list", SimpleNamespace(data=[SimpleNamespace(id="price_123")]))),
        customers=SimpleNamespace(create=record("customers.create", SimpleNamespace(id="cus_new"))),
        checkout=SimpleNamespace(sessions=SimpleNamespace(
            create=record("checkout", SimpleNamespace(url="https://checkout.stripe.com/c/test")))),
    ))
    monkeypatch.setattr(billing, "client", lambda: fake)
    monkeypatch.setattr(billing, "_price_id", None)
    plans.execute_write("DELETE FROM user_plans WHERE user_id = 'test-bill-free'", ())
    response = client.post("/api/create-checkout-session", headers={**auth("tok-free"), "Origin": "https://app.example"})
    assert response.json() == {"url": "https://checkout.stripe.com/c/test"}
    params = calls["checkout"]
    assert params["mode"] == "subscription" and params["line_items"] == [{"price": "price_123", "quantity": 1}]
    assert "payment_method_types" not in params
    assert params["success_url"].startswith("https://app.example/pricing?success=true")
    assert params["cancel_url"] == "https://app.example/pricing?cancelled=true"
    assert params["customer"] == "cus_new" and plans.customer_id_for("test-bill-free") == "cus_new"
    # Pro users can't buy a second subscription.
    assert client.post("/api/create-checkout-session", headers=auth("tok-pro")).status_code == 400


def test_cancel_at_period_end(client, stripe_env, monkeypatch):
    updated = {}

    def update(subscription_id, params):
        updated.update(params, id=subscription_id)
        return {"id": subscription_id, "customer": "cus_test-bill-pro", "status": "active",
                "cancel_at_period_end": True, "items": {"data": [{"current_period_end": 1893456000}]}}

    monkeypatch.setattr(billing, "client", lambda: SimpleNamespace(v1=SimpleNamespace(
        subscriptions=SimpleNamespace(update=update))))
    plan = client.post("/api/cancel-subscription", headers=auth("tok-pro")).json()
    assert updated == {"cancel_at_period_end": True, "id": "sub_test-bill-pro"}
    assert plan["plan"] == "pro" and plan["cancel_at_period_end"] is True
    assert client.post("/api/cancel-subscription", headers=auth("tok-free")).status_code == 400


def test_priority_gate_admits_pro_first():
    gate = plans.PriorityGate(1)
    order = []
    with gate.turn(pro=False):
        def wait(name, pro):
            with gate.turn(pro):
                order.append(name)
        free = threading.Thread(target=wait, args=("free", False))
        free.start()
        time.sleep(0.05)
        pro = threading.Thread(target=wait, args=("pro", True))
        pro.start()
        time.sleep(0.05)
    free.join()
    pro.join()
    assert order == ["pro", "free"]


# ---------- Pro features ----------

def test_checklist_starts_from_school_requirements(client):
    items = client.get(f"/me/checklist/{MIT}", headers=auth("tok-pro")).json()["items"]
    texts = [i["item_text"] for i in items]
    assert any("SAT or ACT scores (required)" in t for t in texts)  # MIT requires test scores
    assert any("CSS Profile" in t for t in texts)  # private nonprofit
    item = client.post(f"/me/checklist/{MIT}", json={"item_text": "Visit campus"}, headers=auth("tok-pro")).json()
    done = client.put(f"/me/checklist/items/{item['id']}", json={"done": True}, headers=auth("tok-pro")).json()
    assert done["done"] is True and done["item_text"] == "Visit campus"
    assert client.delete(f"/me/checklist/items/{item['id']}", headers=auth("tok-pro")).json() == {"deleted": True}


def test_deadline_calendar(client):
    client.put(f"/me/favorites/{HARVARD}", headers=auth("tok-pro"))
    response = client.get("/me/deadlines.ics", headers=auth("tok-pro"))
    body = response.text
    assert response.headers["content-type"].startswith("text/calendar")
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.rstrip().endswith("END:VCALENDAR")
    assert "DTSTART;VALUE=DATE:20261101" in body and "SUMMARY:Harvard University: Restrictive Early Action" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_csv_export_and_injection_guard(client):
    client.put(f"/me/favorites/{HARVARD}", headers=auth("tok-pro"))
    client.post(f"/me/favorites/{HARVARD}/notes", json={"note_text": "=HYPERLINK(\"evil\")"}, headers=auth("tok-pro"))
    body = client.get("/me/favorites.csv", headers=auth("tok-pro")).text
    assert body.splitlines()[0].startswith("School,City,State")
    assert "Harvard University" in body and "'=HYPERLINK" in body


def test_fit_analysis(client, monkeypatch):
    monkeypatch.setattr(pro_features, "fit_summary", lambda facts, c: "Summary.")
    client.put("/me/profile", json={"sat": 1500, "family_income": 60000, "budget": 20000,
                                    "intended_major": "Computer Science", "home_state": "ca"}, headers=auth("tok-pro"))
    fit = client.get(f"/me/fit/{MIT}", headers=auth("tok-pro")).json()
    assert fit["chance"] == "Reach"  # under 15% acceptance
    areas = {c["area"]: c for c in fit["checks"]}
    assert areas["Your major"]["status"] == "good" and "Computer" in areas["Your major"]["detail"]
    assert areas["Cost"]["status"] in ("good", "concern") and "budget" in areas["Cost"]["detail"]
    assert client.put("/me/profile", json={"sat": 2000}, headers=auth("tok-pro")).status_code == 400


def test_reviews_are_limited_on_free(client):
    data = client.get(f"/api/school/{UCLA}").json()
    assert "locked" in data["reviews"]
