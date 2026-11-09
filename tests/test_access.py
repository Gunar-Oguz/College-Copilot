"""
Tests for the stateless access gate (backend/access.py): signed tokens, emails, rate limits.

Run from the repo root:
    python -m pytest tests/test_access.py -v
"""

import re

import pytest

import access
from access import ACCESS, APPROVE, AccessConfig, RateLimiter, TokenError, make_token, verify_token

SECRET = "s" * 48
NOW = 1_800_000_000


def token(purpose=ACCESS, email="Visitor@Example.com", ttl=3600, secret=SECRET, now=NOW, name="Ada"):
    return make_token(purpose, email, secret, ttl, name=name, now=now)


class TestTokens:
    def test_round_trip(self):
        payload = verify_token(token(), ACCESS, SECRET, now=NOW + 10)
        assert payload == {"p": ACCESS, "e": "visitor@example.com", "n": "Ada", "iat": NOW, "exp": NOW + 3600}

    def test_url_safe(self):
        assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token(name="Zoë / O'Brien + co?"))

    def test_wrong_purpose(self):
        with pytest.raises(TokenError, match="purpose"):
            verify_token(token(APPROVE), ACCESS, SECRET, now=NOW)
        with pytest.raises(TokenError, match="purpose"):
            verify_token(token(ACCESS), APPROVE, SECRET, now=NOW)

    def test_expired(self):
        with pytest.raises(TokenError, match="expired"):
            verify_token(token(ttl=60), ACCESS, SECRET, now=NOW + 60)

    def test_issued_in_the_future(self):
        with pytest.raises(TokenError, match="future"):
            verify_token(token(now=NOW + 3600), ACCESS, SECRET, now=NOW)

    def test_wrong_secret(self):
        with pytest.raises(TokenError, match="signature"):
            verify_token(token(), ACCESS, "t" * 48, now=NOW)

    def test_tampered_payload(self):
        body, signature = token().split(".")
        forged = access._b64encode(access._b64decode(body).replace(b"visitor@", b"attacker@"))
        with pytest.raises(TokenError, match="signature"):
            verify_token(f"{forged}.{signature}", ACCESS, SECRET, now=NOW)

    def test_tampered_signature(self):
        good = token()
        bad = good[:-1] + ("A" if good[-1] != "A" else "B")
        with pytest.raises(TokenError):
            verify_token(bad, ACCESS, SECRET, now=NOW)

    def test_signed_by_this_scheme_but_not_json(self):
        body = access._b64encode(b"not json")
        with pytest.raises(TokenError, match="malformed"):
            verify_token(f"{body}.{access._signature(SECRET, body)}", ACCESS, SECRET, now=NOW)

    @pytest.mark.parametrize("bad", ["", "abc", "a.b.c", "é.é", "x" * 5000])
    def test_malformed(self, bad):
        with pytest.raises(TokenError):
            verify_token(bad, ACCESS, SECRET, now=NOW)

    def test_short_secret_is_refused(self):
        with pytest.raises(ValueError):
            make_token(ACCESS, "a@b.co", "short", 60)
        with pytest.raises(ValueError):
            verify_token(token(), ACCESS, "short")

    def test_invalid_email_or_purpose(self):
        with pytest.raises(ValueError):
            make_token(ACCESS, "not-an-email", SECRET, 60)
        with pytest.raises(ValueError):
            make_token("admin", "a@b.co", SECRET, 60)

    def test_link(self):
        assert access.link("https://app.example/", "access", "abc.def") == "https://app.example/?access=abc.def"


@pytest.mark.parametrize("value,expected", [
    (" Ada@Example.COM ", "ada@example.com"), ("a+tag@sub.example.org", "a+tag@sub.example.org"),
    ("no-at-sign", None), ("a@b", None), ("a@@b.com", None), ("a b@c.com", None), ("a..b@c.com", None),
    ("a@b.com\nBcc: x@y.com", None), ("", None),
])
def test_normalize_email(value, expected):
    assert access.normalize_email(value) == expected


def test_clean_text_removes_header_breaking_characters():
    assert access.clean_text("Ada\r\nBcc: evil@x.com", 100) == "Ada Bcc: evil@x.com"
    assert len(access.clean_text("x" * 500, 100)) == 100


def test_passcode():
    assert access.passcode_matches("open sesame", "open sesame")
    assert not access.passcode_matches("open", "open sesame")
    assert not access.passcode_matches("", None)


def test_rate_limiter():
    clock = [0.0]
    limiter = RateLimiter(limit=2, window_sec=60, clock=lambda: clock[0])
    assert limiter.allow() and limiter.allow() and not limiter.allow()
    clock[0] = 61
    assert limiter.allow()


class TestConfigAndEmail:
    def config(self, **overrides):
        values = {"ACCESS_SECRET": SECRET, "OWNER_EMAIL": "Owner@Example.com", "SMTP_USER": "owner@example.com",
                  "SMTP_PASSWORD": "app-password", "APP_URL": "https://demo.example/", **overrides}
        return AccessConfig.from_mapping(values.get)

    def test_from_mapping(self):
        config = self.config(SMTP_PORT="587", DEV_PRINT_LINKS="true")
        assert config.owner_email == "owner@example.com" and config.app_url == "https://demo.example"
        assert config.smtp_port == 587 and config.smtp_host == "smtp.gmail.com" and config.dev_print_links
        assert config.tokens_ready and config.smtp_ready and config.missing() == []

    def test_placeholder_password_is_not_ready(self):
        config = self.config(SMTP_PASSWORD="PASTE_GMAIL_APP_PASSWORD_HERE")
        assert not config.smtp_ready and config.missing() == ["SMTP_PASSWORD (a Gmail app password)"]

    def test_nothing_configured(self):
        config = AccessConfig.from_mapping({}.get)
        assert not config.tokens_ready and not config.smtp_ready and len(config.missing()) == 4

    def test_emails(self):
        config = self.config()
        owner = access.owner_request_email(config, "Ada", "ada@example.com", "Counselor at X", "https://a/?approve=t")
        assert owner["To"] == "owner@example.com" and owner["Reply-To"] == "ada@example.com"
        assert "https://a/?approve=t" in owner.get_content() and "Counselor at X" in owner.get_content()
        visitor = access.visitor_access_email(config, "Ada", "ada@example.com", "https://a/?access=t")
        assert visitor["To"] == "ada@example.com" and "https://a/?access=t" in visitor.get_content()

    @pytest.mark.parametrize("port,ssl_class", [(465, "SMTP_SSL"), (587, "SMTP")])
    def test_send_email_uses_tls(self, monkeypatch, port, ssl_class):
        calls = []

        class FakeSMTP:
            def __init__(self, host, port_, **kwargs):
                calls.append(("connect", host, port_))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self, context):
                calls.append(("starttls",))

            def login(self, user, password):
                calls.append(("login", user))

            def send_message(self, message):
                calls.append(("send", message["To"]))

        monkeypatch.setattr(access.smtplib, ssl_class, FakeSMTP)
        config = self.config(SMTP_PORT=str(port))
        access.send_email(config, access.visitor_access_email(config, "Ada", "ada@example.com", "https://a"))
        assert calls[0] == ("connect", "smtp.gmail.com", port)
        assert ("login", "owner@example.com") in calls and calls[-1] == ("send", "ada@example.com")
        assert (("starttls",) in calls) == (port == 587)
