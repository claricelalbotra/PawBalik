"""SMS via bulksms.com.ph (+ console/memory providers) and mobile OTP (SPEC 4, 9).

``SMS_PROVIDER`` selects the provider:

* ``console`` (default in development): logs the message, sends nothing. The
  OTP code therefore appears in the server log, which is how you verify a
  number on a laptop without an SMS account.
* ``memory`` (tests): appends ``{"to", "message"}`` to
  ``current_app.extensions["sms_outbox"]`` (the ``sms_outbox`` fixture).
* ``bulksms``: :class:`BulkSmsPhProvider`, an HTTP adapter for
  bulksms.com.ph (iSMS Philippines). Its endpoint and field names come from the
  public iSMS documentation and are marked ``TODO(verify)``; confirm them with
  your bulksms.com.ph account before going live.

Rules
-----
* Every attempt (sent, failed or skipped) is written to ``sms_log``.
  OTP codes are *redacted* in the log so admins never see a usable code.
* Messages are converted to GSM-7 friendly text and kept to ``SMS_MAX_CHARS``
  (160) characters so each alert costs one SMS credit.
* OTP: 6 digits, stored as an HMAC (never in plain text), valid for
  ``OTP_TTL_MINUTES``, at most ``OTP_MAX_ATTEMPTS`` wrong tries, one new code per
  ``OTP_RESEND_COOLDOWN_SECONDS``; sending a new code invalidates older ones.
  OTPs are sent immediately (not through the job queue) because the user is
  waiting for them.

Opt-in and the daily match-SMS cap are checked by the caller
(``notification_service``); :func:`send_sms` itself sends to whatever user it is
given, because an OTP must reach a number that is not verified yet.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import requests
import sqlalchemy as sa
from flask import current_app

from app.extensions import db
from app.models import PhoneVerification, SmsLog, SmsPurpose, User
from app.utils.time import utcnow

log = logging.getLogger(__name__)


# --- Results and providers ------------------------------------------------------------------


@dataclass
class SmsResult:
    """Outcome of one send attempt."""

    ok: bool
    provider: str
    message_id: str | None = None
    error: str | None = None


class SmsProvider(Protocol):
    """Anything with a ``name`` and ``send(to_e164, message) -> SmsResult``."""

    name: str

    def send(self, to_e164: str, message: str) -> SmsResult: ...


class ConsoleSmsProvider:
    """Development: write the SMS to the server log instead of sending it."""

    name = "console"

    def send(self, to_e164: str, message: str) -> SmsResult:
        log.warning("[console SMS] to %s: %s", to_e164, message)
        return SmsResult(ok=True, provider=self.name, message_id="console")


class MemorySmsProvider:
    """Tests: record messages in ``current_app.extensions["sms_outbox"]``."""

    name = "memory"

    def send(self, to_e164: str, message: str) -> SmsResult:
        outbox = current_app.extensions.setdefault("sms_outbox", [])
        outbox.append({"to": to_e164, "message": message})
        return SmsResult(ok=True, provider=self.name, message_id=f"mem-{len(outbox)}")


# TODO(verify): default endpoint of the iSMS JSON API that bulksms.com.ph accounts use
# (documented at https://www.bulksms.com.ph/sms-api-documentation.php). Mirrors listed
# there: ww3.isms.com.my, smtpapi.vocotext.com, smtpapi2.vocotext.com. Override with
# BULKSMS_API_URL once your account manager confirms the URL for Philippine accounts.
BULKSMS_DEFAULT_URL = "https://www.isms.com.my/isms_send_json.php"
BULKSMS_SUCCESS_CODE = 2000  # TODO(verify): "2000 = SUCCESS" per the iSMS response code table

# Documented iSMS error codes (TODO(verify)), used only to write a readable sms_log.error.
BULKSMS_ERRORS = {
    -1000: "unknown server error",
    -1001: "authentication failed (check BULKSMS_USERNAME / BULKSMS_API_KEY)",
    -1002: "account suspended or expired",
    -1003: "server IP address is not whitelisted",
    -1004: "insufficient SMS credits",
    -1005: "invalid message type",
    -1006: "message too long",
    -1008: "missing parameter or invalid destination number",
    -1009: "message blocked by content filter",
    -1012: "characters not allowed for the message type",
    -1013: "terms not accepted (agreedterm)",
    -1014: "request body is not valid JSON",
    -1015: "JSON endpoint requires POST",
}


class BulkSmsPhProvider:
    """bulksms.com.ph (iSMS Philippines) HTTP adapter.

    Everything provider-specific lives in this class. Request shape per the public
    iSMS JSON API docs - TODO(verify) every field against your account's docs:

        POST <BULKSMS_API_URL>   Content-Type: application/json
        {"un": <username>, "pwd": <password/API key>, "type": "1",  # 1 = plain text (GSM)
         "sendid": <sender name, max 11 chars>, "agreedterm": "YES",
         "messages": [{"dstno": "639171234567", "msg": "..."}]}

        -> {"code": 2000, "results": [{"dstno": ..., "code": 2000, "sms_id": "1143007207"}], ...}

    Credentials travel in the POST body, never in the URL, so they cannot leak into
    proxy or error logs. The key is never logged.
    """

    name = "bulksms"

    def __init__(
        self,
        *,
        api_url: str | None = None,
        username: str | None = None,
        api_key: str | None = None,
        sender_name: str | None = None,
        timeout: float | None = None,
    ) -> None:
        cfg = current_app.config
        self.api_url = api_url or cfg.get("BULKSMS_API_URL") or BULKSMS_DEFAULT_URL
        # BULKSMS_USERNAME is read from config, falling back to the environment until the
        # key is added to app/config.py.
        self.username = (
            username or cfg.get("BULKSMS_USERNAME") or os.environ.get("BULKSMS_USERNAME") or ""
        )
        self.api_key = api_key or cfg.get("BULKSMS_API_KEY") or ""
        self.sender_name = (sender_name or cfg.get("BULKSMS_SENDER_NAME") or "")[:11]
        self.timeout = float(timeout or cfg.get("BULKSMS_TIMEOUT_SECONDS") or 15)

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.api_key)

    @staticmethod
    def to_provider_number(to_e164: str) -> str:
        """E.164 ``+639171234567`` -> ``639171234567`` (country code, no plus). TODO(verify)."""
        digits = re.sub(r"\D", "", to_e164 or "")
        if digits.startswith("09") and len(digits) == 11:  # tolerate a local 09xx number
            digits = "63" + digits[1:]
        return digits

    def build_payload(self, to_e164: str, message: str) -> dict[str, Any]:
        """The JSON body for one message (kept separate so tests can check it)."""
        payload: dict[str, Any] = {
            "un": self.username,
            "pwd": self.api_key,
            "type": "1",  # TODO(verify): "1" = ASCII/GSM text, "2" = Unicode
            "agreedterm": "YES",  # TODO(verify): required by the iSMS API
            "messages": [{"dstno": self.to_provider_number(to_e164), "msg": message}],
        }
        if self.sender_name:
            payload["sendid"] = self.sender_name  # TODO(verify): sender ID must be registered
        return payload

    def send(self, to_e164: str, message: str) -> SmsResult:
        if not self.is_configured:
            return SmsResult(
                ok=False,
                provider=self.name,
                error="bulksms is not configured (set BULKSMS_USERNAME and BULKSMS_API_KEY)",
            )
        try:
            response = requests.post(
                self.api_url,
                json=self.build_payload(to_e164, message),
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            # Only the exception type: messages of some exceptions can include request data.
            return SmsResult(
                ok=False, provider=self.name, error=f"network error: {type(exc).__name__}"
            )
        return self.parse_response(response)

    def parse_response(self, response: requests.Response) -> SmsResult:
        """Read the provider answer. TODO(verify): response shape per the iSMS docs."""
        if response.status_code >= 400:
            return SmsResult(ok=False, provider=self.name, error=f"HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict):
            results = data.get("results") or []
            first = results[0] if results and isinstance(results[0], dict) else {}
            code = _as_int(first.get("code", data.get("code")))
            if code == BULKSMS_SUCCESS_CODE:
                message_id = first.get("sms_id") or _trx_id(str(first.get("status") or ""))
                return SmsResult(ok=True, provider=self.name, message_id=message_id or None)
            return SmsResult(ok=False, provider=self.name, error=_describe_code(code))

        # Classic endpoints answer plain text such as "2000 = SUCCESS:1143007207" or "-1001".
        text = (response.text or "").strip()
        code = _as_int(text.split("=")[0].strip()) if text else None
        if code == BULKSMS_SUCCESS_CODE:
            return SmsResult(ok=True, provider=self.name, message_id=_trx_id(text))
        return SmsResult(ok=False, provider=self.name, error=_describe_code(code))


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _trx_id(status_text: str) -> str | None:
    match = re.search(r"SUCCESS:\s*(\w+)", status_text)
    return match.group(1) if match else None


def _describe_code(code: int | None) -> str:
    if code is None:
        return "unexpected response from bulksms"
    return f"bulksms error {code}: {BULKSMS_ERRORS.get(code, 'unknown error code')}"


PROVIDERS: dict[str, type] = {
    "console": ConsoleSmsProvider,
    "memory": MemorySmsProvider,
    "bulksms": BulkSmsPhProvider,
}


def get_provider() -> SmsProvider:
    """Provider selected by ``SMS_PROVIDER`` (console | memory | bulksms); console if unknown."""
    name = (current_app.config.get("SMS_PROVIDER") or "console").strip().lower()
    if name not in PROVIDERS:
        log.warning("Unknown SMS_PROVIDER %r - using the console provider", name)
    return PROVIDERS.get(name, ConsoleSmsProvider)()


# --- Message text ---------------------------------------------------------------------------

# Characters of the GSM 03.38 basic set (one SMS = 160 of these).
GSM7_CHARS = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_REPLACEMENTS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "…": "...",
    " ": " ",
    "₱": "PHP ",  # peso sign is not GSM-7
    "·": "-",
    "•": "-",
}


def to_gsm7(text: str) -> str:
    """Replace characters outside GSM-7 so the SMS is not sent as (70-char) Unicode."""
    out: list[str] = []
    for char in text or "":
        char = _REPLACEMENTS.get(char, char)
        if all(c in GSM7_CHARS for c in char):
            out.append(char)
            continue
        # Strip accents (e.g. "ñ" is GSM, but "õ" is not -> "o"); drop anything else.
        plain = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode("ascii")
        out.append("".join(c for c in plain if c in GSM7_CHARS))
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def fit_sms(text: str, max_chars: int | None = None) -> str:
    """GSM-7 text of at most ``max_chars`` (default ``SMS_MAX_CHARS``) characters."""
    limit = int(max_chars or current_app.config.get("SMS_MAX_CHARS") or 160)
    text = to_gsm7(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _redact_codes(message: str) -> str:
    """Hide OTP digits before a message is stored in sms_log."""
    return re.sub(r"\d{4,8}", lambda m: "*" * len(m.group(0)), message)


# --- Sending --------------------------------------------------------------------------------


def _log_sms(
    user: User | None,
    to_number: str,
    message: str,
    purpose: str,
    provider: str,
    status: str,
    *,
    message_id: str | None = None,
    error: str | None = None,
) -> SmsLog:
    logged = _redact_codes(message) if purpose == SmsPurpose.OTP else message
    row = SmsLog(
        user_id=getattr(user, "id", None),
        to_number=(to_number or "")[:16],
        message=logged[:640],
        purpose=purpose,
        provider=provider[:30],
        provider_message_id=(message_id or None) and str(message_id)[:100],
        status=status,
        error=error,
    )
    db.session.add(row)
    db.session.flush()
    return row


def log_skipped_sms(user: User, message: str, purpose: str, reason: str) -> SmsLog:
    """Record an SMS that was deliberately not sent (opt-out, unverified, daily cap)."""
    return _log_sms(
        user,
        user.mobile_e164 or "",
        fit_sms(message),
        purpose,
        current_app.config.get("SMS_PROVIDER") or "console",
        "skipped",
        error=reason,
    )


def send_sms(user: User, message: str, purpose: str) -> SmsResult:
    """Send one SMS to ``user.mobile_e164`` and log it in ``sms_log`` (flushes; caller commits).

    ``purpose`` is an :class:`~app.models.SmsPurpose` value. Never raises for provider
    problems: a failure comes back as ``SmsResult(ok=False, error=...)`` and is logged.
    """
    if purpose not in SmsPurpose.ALL:
        raise ValueError(f"Unknown SMS purpose {purpose!r}")
    text = fit_sms(message)
    provider = get_provider()
    if not user.mobile_e164:
        result = SmsResult(ok=False, provider=provider.name, error="user has no mobile number")
        _log_sms(user, "", text, purpose, provider.name, "skipped", error=result.error)
        return result

    try:
        result = provider.send(user.mobile_e164, text)
    except Exception as exc:  # noqa: BLE001 - a broken provider must not break the caller
        log.exception("SMS provider %s crashed", provider.name)
        result = SmsResult(ok=False, provider=provider.name, error=f"{type(exc).__name__}")

    _log_sms(
        user,
        user.mobile_e164,
        text,
        purpose,
        result.provider or provider.name,
        "sent" if result.ok else "failed",
        message_id=result.message_id,
        error=result.error,
    )
    if not result.ok:
        log.warning("SMS to user %s failed: %s", user.id, result.error)
    return result


def sent_match_sms_today(user: User, now: datetime | None = None) -> int:
    """How many possible-match SMS were sent to ``user`` since local midnight (Manila)."""
    from app.utils.time import start_of_local_day_utc

    since = start_of_local_day_utc(now)
    return (
        db.session.scalar(
            sa.select(sa.func.count(SmsLog.id)).where(
                SmsLog.user_id == user.id,
                SmsLog.purpose == SmsPurpose.POSSIBLE_MATCH,
                SmsLog.status == "sent",
                SmsLog.created_at >= since,
            )
        )
        or 0
    )


# --- One-time codes (OTP) ---------------------------------------------------------------------


class OtpError(Exception):
    """A code could not be sent. ``code`` is machine readable, ``message`` user friendly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OtpCooldown(OtpError):
    """A code was sent moments ago; ``retry_after`` seconds until the next one."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "cooldown",
            f"A code was just sent. Please wait {retry_after} seconds before asking for a new one.",
        )
        self.retry_after = retry_after


@dataclass
class OtpStatus:
    """What the verify-phone page needs to know about the user's latest code."""

    has_active_code: bool
    attempts_left: int
    expires_at: datetime | None
    resend_in: int  # seconds until a new code may be requested (0 = now)
    locked: bool  # the active code has used up its attempts


def _hash_code(user_id: int, mobile: str, code: str) -> str:
    """HMAC-SHA256 of the code, bound to the user and number (keyed with SECRET_KEY)."""
    key = str(current_app.config["SECRET_KEY"]).encode()
    return hmac.new(key, f"{user_id}:{mobile}:{code}".encode(), hashlib.sha256).hexdigest()


def _generate_code() -> str:
    length = int(current_app.config.get("OTP_LENGTH") or 6)
    return f"{secrets.randbelow(10**length):0{length}d}"


def _latest_verification(user: User) -> PhoneVerification | None:
    return db.session.scalar(
        sa.select(PhoneVerification)
        .where(PhoneVerification.user_id == user.id)
        .order_by(PhoneVerification.created_at.desc(), PhoneVerification.id.desc())
        .limit(1)
    )


def _active_verification(user: User, now: datetime) -> PhoneVerification | None:
    """The newest unused, unexpired code for the user's *current* number."""
    return db.session.scalar(
        sa.select(PhoneVerification)
        .where(
            PhoneVerification.user_id == user.id,
            PhoneVerification.mobile_e164 == (user.mobile_e164 or ""),
            PhoneVerification.consumed_at.is_(None),
            PhoneVerification.expires_at > now,
        )
        .order_by(PhoneVerification.created_at.desc(), PhoneVerification.id.desc())
        .limit(1)
    )


def resend_wait_seconds(user: User, now: datetime | None = None) -> int:
    """Seconds until ``send_otp`` may be called again for this user (0 = allowed)."""
    now = now or utcnow()
    latest = _latest_verification(user)
    if latest is None or latest.mobile_e164 != user.mobile_e164:
        return 0
    cooldown = int(current_app.config.get("OTP_RESEND_COOLDOWN_SECONDS") or 0)
    ready_at = latest.created_at + timedelta(seconds=cooldown)
    return max(0, int((ready_at - now).total_seconds() + 0.999)) if ready_at > now else 0


def otp_status(user: User, now: datetime | None = None) -> OtpStatus:
    now = now or utcnow()
    active = _active_verification(user, now)
    max_attempts = int(current_app.config.get("OTP_MAX_ATTEMPTS") or 5)
    attempts_left = max(0, max_attempts - active.attempts) if active else 0
    return OtpStatus(
        has_active_code=active is not None and attempts_left > 0,
        attempts_left=attempts_left,
        expires_at=active.expires_at if active else None,
        resend_in=resend_wait_seconds(user, now),
        locked=active is not None and attempts_left == 0,
    )


def send_otp(user: User) -> None:
    """Create a new hashed code and SMS it to the user's mobile right away (flushes).

    Older unused codes are invalidated. Raises :class:`OtpError` (``no_mobile``,
    ``already_verified``, ``send_failed``) or :class:`OtpCooldown`. The caller commits
    (also after an error, so the failed attempt stays in ``sms_log``).
    """
    if not user.mobile_e164:
        raise OtpError("no_mobile", "Add a mobile number to your profile first.")
    if user.mobile_verified:
        raise OtpError("already_verified", "Your mobile number is already verified.")
    now = utcnow()
    wait = resend_wait_seconds(user, now)
    if wait:
        raise OtpCooldown(wait)

    # Invalidate every older unused code (expire it now; consumed_at means "used").
    db.session.execute(
        sa.update(PhoneVerification)
        .where(
            PhoneVerification.user_id == user.id,
            PhoneVerification.consumed_at.is_(None),
            PhoneVerification.expires_at > now,
        )
        .values(expires_at=now)
        .execution_options(synchronize_session="fetch")
    )

    code = _generate_code()
    ttl = int(current_app.config.get("OTP_TTL_MINUTES") or 10)
    verification = PhoneVerification(
        user_id=user.id,
        mobile_e164=user.mobile_e164,
        code_hash=_hash_code(user.id, user.mobile_e164, code),
        expires_at=now + timedelta(minutes=ttl),
        attempts=0,
        created_at=now,
    )
    db.session.add(verification)
    db.session.flush()

    message = (
        f"PawBalik verification code: {code}. It expires in {ttl} minutes. "
        "Never share this code with anyone."
    )
    result = send_sms(user, message, SmsPurpose.OTP)
    if not result.ok:
        # Drop the unsendable code so the resend cooldown does not block a retry.
        db.session.delete(verification)
        db.session.flush()
        raise OtpError(
            "send_failed", "We could not send the SMS right now. Please try again in a moment."
        )


def verify_otp(user: User, code: str) -> bool:
    """Check ``code`` against the user's active code (flushes; the caller commits).

    Every try counts towards ``OTP_MAX_ATTEMPTS``. On success the code is consumed and
    ``user.mobile_verified_at`` is set. Returns False for a wrong, expired, used or
    locked code - use :func:`otp_status` to tell the user which.
    """
    now = utcnow()
    active = _active_verification(user, now)
    if active is None:
        return False
    max_attempts = int(current_app.config.get("OTP_MAX_ATTEMPTS") or 5)
    if active.attempts >= max_attempts:
        return False

    active.attempts += 1
    cleaned = re.sub(r"\s|-", "", code or "")
    expected = active.code_hash
    ok = cleaned.isdigit() and hmac.compare_digest(
        _hash_code(user.id, active.mobile_e164, cleaned), expected
    )
    if ok:
        active.consumed_at = now
        user.mobile_verified_at = now
    db.session.flush()
    return bool(ok)
