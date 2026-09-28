"""Mobile verification codes: sending, hashing, expiry, attempts, reuse, cooldown, the page."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import sqlalchemy as sa
from freezegun import freeze_time

from app.extensions import db
from app.models import PhoneVerification, SmsLog, SmsPurpose
from app.services import sms_service
from app.services.sms_service import OtpCooldown, OtpError, otp_status, send_otp, verify_otp
from app.utils.time import utcnow


def _code(sms_outbox) -> str:
    match = re.search(r"\b(\d{6})\b", sms_outbox[-1]["message"])
    assert match, sms_outbox[-1]
    return match.group(1)


def _verifications(user) -> list[PhoneVerification]:
    return list(
        db.session.scalars(
            sa.select(PhoneVerification)
            .where(PhoneVerification.user_id == user.id)
            .order_by(PhoneVerification.id)
        )
    )


def test_send_otp_sends_six_digits_and_stores_only_a_hash(app, user, sms_outbox):
    send_otp(user)
    db.session.commit()
    assert len(sms_outbox) == 1
    assert sms_outbox[0]["to"] == user.mobile_e164
    code = _code(sms_outbox)
    (row,) = _verifications(user)
    assert row.code_hash != code and code not in row.code_hash
    assert len(row.code_hash) == 64
    assert row.mobile_e164 == user.mobile_e164
    assert timedelta(minutes=9) < row.expires_at - utcnow() <= timedelta(minutes=10)
    log = db.session.scalar(sa.select(SmsLog))
    assert log.purpose == SmsPurpose.OTP and code not in log.message


def test_verify_otp_success_sets_mobile_verified(app, user, sms_outbox):
    send_otp(user)
    assert verify_otp(user, _code(sms_outbox)) is True
    db.session.commit()
    assert user.mobile_verified_at is not None
    assert user.mobile_verified
    (row,) = _verifications(user)
    assert row.consumed_at is not None and row.attempts == 1


def test_code_with_spaces_is_accepted(app, user, sms_outbox):
    send_otp(user)
    code = _code(sms_outbox)
    assert verify_otp(user, f" {code[:3]} {code[3:]} ")


def test_wrong_code_counts_attempts_and_locks_after_max(app, user, sms_outbox):
    app.config["OTP_MAX_ATTEMPTS"] = 5
    send_otp(user)
    code = _code(sms_outbox)
    wrong = "000000" if code != "000000" else "111111"
    for i in range(5):
        assert verify_otp(user, wrong) is False
        assert otp_status(user).attempts_left == 4 - i
    status = otp_status(user)
    assert status.locked and not status.has_active_code
    # Even the right code is refused after the attempts are used up.
    assert verify_otp(user, code) is False
    assert user.mobile_verified_at is None


def test_non_numeric_code_counts_as_attempt(app, user, sms_outbox):
    send_otp(user)
    assert verify_otp(user, "abcdef") is False
    assert _verifications(user)[0].attempts == 1


def test_code_expires_after_ttl(app, user, sms_outbox):
    with freeze_time("2026-09-20 02:00:00"):
        send_otp(user)
        code = _code(sms_outbox)
    with freeze_time("2026-09-20 02:10:01"):
        assert verify_otp(user, code) is False
        assert not otp_status(user).has_active_code
    assert user.mobile_verified_at is None


def test_code_valid_just_before_expiry(app, user, sms_outbox):
    with freeze_time("2026-09-20 02:00:00"):
        send_otp(user)
        code = _code(sms_outbox)
    with freeze_time("2026-09-20 02:09:59"):
        assert verify_otp(user, code) is True


def test_code_cannot_be_reused(app, make_user, sms_outbox):
    user = make_user()
    send_otp(user)
    code = _code(sms_outbox)
    assert verify_otp(user, code)
    user.mobile_verified_at = None  # e.g. number changed back; old code must still be dead
    assert verify_otp(user, code) is False


def test_resend_cooldown(app, user, sms_outbox):
    app.config["OTP_RESEND_COOLDOWN_SECONDS"] = 60
    with freeze_time("2026-09-20 02:00:00"):
        send_otp(user)
    with freeze_time("2026-09-20 02:00:30"):
        with pytest.raises(OtpCooldown) as info:
            send_otp(user)
        assert info.value.retry_after == 30
        assert otp_status(user).resend_in == 30
    with freeze_time("2026-09-20 02:01:00"):
        send_otp(user)
    assert len(sms_outbox) == 2


def test_new_code_invalidates_older_codes(app, user, sms_outbox):
    app.config["OTP_RESEND_COOLDOWN_SECONDS"] = 0
    send_otp(user)
    first = _code(sms_outbox)
    send_otp(user)
    second = _code(sms_outbox)
    if first != second:
        assert verify_otp(user, first) is False
    assert verify_otp(user, second) is True
    rows = _verifications(user)
    assert len(rows) == 2
    assert rows[0].expires_at <= rows[1].created_at


def test_code_is_bound_to_the_number(app, user, sms_outbox):
    send_otp(user)
    code = _code(sms_outbox)
    user.set_mobile("+639991112222")
    assert verify_otp(user, code) is False


def test_send_otp_errors(app, make_user):
    with pytest.raises(OtpError) as info:
        send_otp(make_user(mobile_e164=None))
    assert info.value.code == "no_mobile"
    with pytest.raises(OtpError) as info:
        send_otp(make_user(mobile_verified_at=utcnow()))
    assert info.value.code == "already_verified"


def test_failed_send_removes_code_and_allows_retry(app, user, monkeypatch):
    calls = []

    class Failing:
        name = "failing"

        def send(self, to, message):
            calls.append(message)
            return sms_service.SmsResult(ok=False, provider="failing", error="down")

    monkeypatch.setattr(sms_service, "get_provider", lambda: Failing())
    with pytest.raises(OtpError) as info:
        send_otp(user)
    assert info.value.code == "send_failed"
    assert _verifications(user) == []
    assert otp_status(user).resend_in == 0  # no cooldown after a failed send
    assert db.session.scalar(sa.select(SmsLog.status)) == "failed"


def test_otp_is_sent_immediately_not_queued(app, user, sms_outbox):
    app.config["JOBS_EAGER"] = False
    send_otp(user)
    assert len(sms_outbox) == 1


# --- verify-phone page --------------------------------------------------------------------------


def test_verify_page_requires_login(client):
    response = client.get("/account/verify-phone")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_verify_page_flow(client, login, user, sms_outbox):
    login(client, user)
    page = client.get("/account/verify-phone")
    assert page.status_code == 200
    assert b"Send code" in page.data

    sent = client.post("/account/verify-phone", data={"action": "send"})
    assert sent.status_code == 302
    page = client.get("/account/verify-phone")
    assert b"Enter the code" in page.data and b'name="code"' in page.data

    wrong = client.post("/account/verify-phone", data={"action": "verify", "code": "12345"})
    assert wrong.status_code == 422
    assert b"6-digit code" in wrong.data

    code = _code(sms_outbox)
    bad = "000000" if code != "000000" else "111111"
    wrong = client.post("/account/verify-phone", data={"action": "verify", "code": bad})
    assert wrong.status_code == 422
    assert b"4 tries left" in wrong.data

    ok = client.post("/account/verify-phone", data={"action": "verify", "code": code})
    assert ok.status_code == 302
    db.session.refresh(user)
    assert user.mobile_verified
    page = client.get("/account/verify-phone")
    assert b"Your number is verified" in page.data


def test_verify_page_redirects_to_safe_next(client, login, user, sms_outbox):
    login(client, user)
    client.post("/account/verify-phone", data={"action": "send"})
    ok = client.post(
        "/account/verify-phone",
        data={"action": "verify", "code": _code(sms_outbox), "next": "/my-cases"},
    )
    assert ok.headers["Location"].endswith("/my-cases")


def test_verify_page_ignores_external_next(client, login, user, sms_outbox):
    login(client, user)
    client.post("/account/verify-phone", data={"action": "send"})
    ok = client.post(
        "/account/verify-phone",
        data={"action": "verify", "code": _code(sms_outbox), "next": "https://evil.example/"},
    )
    assert "evil.example" not in ok.headers["Location"]


def test_verify_page_cooldown_message(client, login, user, sms_outbox):
    login(client, user)
    client.post("/account/verify-phone", data={"action": "send"})
    again = client.post("/account/verify-phone", data={"action": "send"})
    assert again.status_code == 422
    assert b"Please wait" in again.data
    assert len(sms_outbox) == 1


def test_verify_page_without_mobile(client, login, make_user):
    login(client, make_user(mobile_e164=None))
    page = client.get("/account/verify-phone")
    assert b"Add your mobile number first" in page.data


def test_verify_page_csrf_required(csrf_client, login, user, sms_outbox):
    login(csrf_client, user)
    refused = csrf_client.post("/account/verify-phone", data={"action": "send"})
    assert refused.status_code == 400
    assert sms_outbox == []
    from conftest import get_csrf_token

    token = get_csrf_token(csrf_client, "/account/verify-phone")
    ok = csrf_client.post("/account/verify-phone", data={"action": "send", "csrf_token": token})
    assert ok.status_code == 302
    assert len(sms_outbox) == 1


@pytest.mark.config(
    RATELIMIT_ENABLED=True, RATE_LIMIT_OTP="2 per hour", OTP_RESEND_COOLDOWN_SECONDS=0
)
def test_send_code_is_rate_limited(client, login, user, sms_outbox):
    login(client, user)
    assert client.post("/account/verify-phone", data={"action": "send"}).status_code == 302
    assert client.post("/account/verify-phone", data={"action": "send"}).status_code == 302
    assert client.post("/account/verify-phone", data={"action": "send"}).status_code == 429
    assert len(sms_outbox) == 2
    # Checking a code is not counted by the OTP send limit (it has its own attempt cap).
    response = client.post("/account/verify-phone", data={"action": "verify", "code": "000001"})
    assert response.status_code in (302, 422)
