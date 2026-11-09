"""
Access by approval for the demo, with no database: everything is in signed links.

1. A visitor asks for access (name, email, why). The owner gets an email with an Approve link,
   <APP_URL>?approve=<token>, where the token is signed for the purpose "approve" (valid 7 days).
2. The owner opens it and confirms. The app emails the visitor a personal link,
   <APP_URL>?access=<token>, signed for the purpose "access" and bound to their email (30 days).
3. A valid access token unlocks the app for that browser session.

A token is base64url(JSON payload) + "." + base64url(HMAC-SHA256(secret, "v1." + payload)).
It is signed, not encrypted: it carries only the visitor's own email and name. Changing
ACCESS_SECRET revokes every link at once.

Everything here is pure Python (no Streamlit), so it is unit-tested directly.
"""

import base64
import hashlib
import hmac
import json
import re
import smtplib
import ssl
import time
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import urlencode

TOKEN_VERSION = "v1"
APPROVE, ACCESS = "approve", "access"
PURPOSES = (APPROVE, ACCESS)
APPROVE_TTL_SEC = 7 * 24 * 3600
ACCESS_TTL_SEC = 30 * 24 * 3600
CLOCK_SKEW_SEC = 300
MIN_SECRET_CHARS = 32
MAX_TOKEN_CHARS = 2000
MAX_NAME_CHARS = 100
MAX_NOTE_CHARS = 1000

# Practical email check (one @, a dot in the domain, no spaces or control characters).
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class TokenError(ValueError):
    """The token is malformed, tampered with, expired, or for another purpose."""


def normalize_email(value: str) -> str | None:
    email = (value or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email) or ".." in email:
        return None
    return email


def clean_text(value: str, limit: int) -> str:
    """One line of plain text: no control characters (they could break email headers), capped."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "").strip()
    return re.sub(r"\s+", " ", text)[:limit]


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, body: str) -> str:
    message = f"{TOKEN_VERSION}.{body}".encode("ascii")
    return _b64encode(hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest())


def check_secret(secret: str | None) -> str:
    if not secret or len(secret) < MIN_SECRET_CHARS:
        raise ValueError(f"ACCESS_SECRET must be at least {MIN_SECRET_CHARS} characters")
    return secret


def make_token(purpose: str, email: str, secret: str, ttl_sec: int, name: str = "",
               now: float | None = None) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("invalid email")
    issued = int(now if now is not None else time.time())
    payload = {"p": purpose, "e": normalized, "n": clean_text(name, MAX_NAME_CHARS), "iat": issued,
               "exp": issued + int(ttl_sec)}
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_signature(check_secret(secret), body)}"


def verify_token(token: str, purpose: str, secret: str, now: float | None = None) -> dict:
    """The token's payload ({p, e, n, iat, exp}), or TokenError. The signature is checked first."""
    if not token or len(token) > MAX_TOKEN_CHARS or not token.isascii() or token.count(".") != 1:
        raise TokenError("malformed token")
    body, signature = token.split(".")
    if not hmac.compare_digest(_signature(check_secret(secret), body).encode(), signature.encode()):
        raise TokenError("bad signature")
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("malformed payload") from exc
    if not isinstance(payload, dict):
        raise TokenError("malformed payload")
    if payload.get("p") != purpose:
        raise TokenError("wrong purpose")
    current = now if now is not None else time.time()
    issued, expires = payload.get("iat"), payload.get("exp")
    if not isinstance(issued, int) or not isinstance(expires, int):
        raise TokenError("malformed payload")
    if issued > current + CLOCK_SKEW_SEC:
        raise TokenError("issued in the future")
    if current >= expires:
        raise TokenError("expired")
    if normalize_email(payload.get("e", "")) != payload.get("e"):
        raise TokenError("malformed payload")
    return payload


def link(app_url: str, param: str, token: str) -> str:
    return f"{app_url.rstrip('/')}/?{urlencode({param: token})}"


def passcode_matches(given: str, expected: str | None) -> bool:
    """Constant-time comparison; an unset passcode never matches."""
    if not expected:
        return False
    return hmac.compare_digest(hashlib.sha256((given or "").encode()).digest(),
                               hashlib.sha256(expected.encode()).digest())


class RateLimiter:
    """At most `limit` events per `window_sec` (e.g. access requests from one browser session)."""

    def __init__(self, limit: int, window_sec: float, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window_sec, clock
        self.events: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self.events and now - self.events[0] >= self.window:
            self.events.popleft()
        if len(self.events) >= self.limit:
            return False
        self.events.append(now)
        return True


# ---------- configuration and email ----------

@dataclass(frozen=True)
class AccessConfig:
    access_secret: str | None = None
    owner_email: str | None = None
    owner_passcode: str | None = None
    app_url: str = "http://localhost:8501"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: str | None = None
    dev_print_links: bool = False  # local development: write the links to the server log

    @classmethod
    def from_mapping(cls, get) -> "AccessConfig":
        """Build from a lookup function, e.g. one that reads Streamlit secrets, then the environment."""
        def flag(name):
            return str(get(name) or "").strip().lower() in ("1", "true", "yes", "on")
        return cls(
            access_secret=get("ACCESS_SECRET") or None,
            owner_email=normalize_email(get("OWNER_EMAIL") or ""),
            owner_passcode=get("OWNER_PASSCODE") or None,
            app_url=(get("APP_URL") or cls.app_url).rstrip("/"),
            smtp_host=get("SMTP_HOST") or cls.smtp_host,
            smtp_port=int(get("SMTP_PORT") or cls.smtp_port),
            smtp_user=get("SMTP_USER") or None,
            smtp_password=get("SMTP_PASSWORD") or None,
            dev_print_links=flag("DEV_PRINT_LINKS"),
        )

    @property
    def tokens_ready(self) -> bool:
        return bool(self.access_secret and len(self.access_secret) >= MIN_SECRET_CHARS)

    @property
    def smtp_ready(self) -> bool:
        placeholder = (self.smtp_password or "").startswith("PASTE_")
        return bool(self.smtp_user and self.smtp_password and self.owner_email and not placeholder)

    def missing(self) -> list[str]:
        """Secrets the owner still has to set for the email approval flow."""
        names = []
        if not self.tokens_ready:
            names.append(f"ACCESS_SECRET (at least {MIN_SECRET_CHARS} characters)")
        if not self.owner_email:
            names.append("OWNER_EMAIL")
        if not self.smtp_user:
            names.append("SMTP_USER")
        if not self.smtp_password or self.smtp_password.startswith("PASTE_"):
            names.append("SMTP_PASSWORD (a Gmail app password)")
        return names


def owner_request_email(config: AccessConfig, name: str, email: str, note: str, approve_url: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"CollegeCopilot access request: {clean_text(name, MAX_NAME_CHARS)}"
    message["From"] = config.smtp_user
    message["To"] = config.owner_email
    message["Reply-To"] = email
    message.set_content(
        f"{name} <{email}> asked for access to the CollegeCopilot demo.\n\n"
        f"Organisation / reason:\n{note or '(none given)'}\n\n"
        f"Approve (sends them a personal link valid for {ACCESS_TTL_SEC // 86400} days):\n{approve_url}\n\n"
        f"This approve link expires in {APPROVE_TTL_SEC // 86400} days. Ignore this email to decline.\n"
    )
    return message


def visitor_access_email(config: AccessConfig, name: str, email: str, access_url: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Your CollegeCopilot demo access link"
    message["From"] = config.smtp_user
    message["To"] = email
    if config.owner_email:
        message["Reply-To"] = config.owner_email
    greeting = f"Hi {name}," if name else "Hi,"
    message.set_content(
        f"{greeting}\n\nYour request to try CollegeCopilot was approved. Open the app with your personal link:\n"
        f"{access_url}\n\nThe link works for {ACCESS_TTL_SEC // 86400} days. Please don't share it: it is tied "
        f"to your email address.\n\nGulnar Albushova\n"
    )
    return message


def send_email(config: AccessConfig, message: EmailMessage, timeout: float = 15) -> None:
    """SMTP over TLS: implicit TLS on port 465, STARTTLS otherwise."""
    context = ssl.create_default_context()
    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=timeout, context=context) as smtp:
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=timeout) as smtp:
            smtp.starttls(context=context)
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(message)
