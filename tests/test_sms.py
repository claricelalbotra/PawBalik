"""sms_service: providers, provider selection, message fitting, send_sms logging, bulksms adapter."""

from __future__ import annotations

import json

import pytest
import requests
import responses
import sqlalchemy as sa

from app.extensions import db
from app.models import SmsLog, SmsPurpose
from app.services import sms_service
from app.services.sms_service import (
    BulkSmsPhProvider,
    ConsoleSmsProvider,
    MemorySmsProvider,
    SmsResult,
    fit_sms,
    get_provider,
    send_sms,
    to_gsm7,
)

BULK_URL = "https://sms.example.test/isms_send_json.php"


def _logs() -> list[SmsLog]:
    return list(db.session.scalars(sa.select(SmsLog).order_by(SmsLog.id)))


# --- provider selection ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "cls"),
    [
        ("console", ConsoleSmsProvider),
        ("memory", MemorySmsProvider),
        ("bulksms", BulkSmsPhProvider),
        ("BulkSMS ", BulkSmsPhProvider),
        ("nonsense", ConsoleSmsProvider),
        (None, ConsoleSmsProvider),
    ],
)
def test_get_provider_by_config(app, name, cls):
    app.config["SMS_PROVIDER"] = name
    assert isinstance(get_provider(), cls)


def test_console_provider_logs_and_succeeds(app, caplog):
    result = ConsoleSmsProvider().send("+639171234567", "hello")
    assert result == SmsResult(ok=True, provider="console", message_id="console")
    assert "hello" in caplog.text


def test_memory_provider_appends_to_outbox(app, sms_outbox):
    MemorySmsProvider().send("+639171234567", "hi")
    assert sms_outbox == [{"to": "+639171234567", "message": "hi"}]


# --- message text ----------------------------------------------------------------------------


def test_to_gsm7_replaces_non_gsm_characters():
    text = to_gsm7("Case — “Bantay” costs ₱500… \U0001f436 cafê")
    assert text == 'Case - "Bantay" costs PHP 500... cafe'
    assert all(c in sms_service.GSM7_CHARS for c in text)


def test_fit_sms_limits_to_160_chars(app):
    long = "x" * 400
    fitted = fit_sms(long)
    assert len(fitted) == 160
    assert fitted.endswith("...")
    assert fit_sms("short") == "short"


# --- send_sms ---------------------------------------------------------------------------------


def test_send_sms_sends_and_logs(app, user, sms_outbox):
    result = send_sms(user, "Hello from PawBalik", SmsPurpose.POSSIBLE_MATCH)
    db.session.commit()
    assert result.ok and result.provider == "memory"
    assert sms_outbox == [{"to": user.mobile_e164, "message": "Hello from PawBalik"}]
    (row,) = _logs()
    assert row.status == "sent"
    assert row.user_id == user.id
    assert row.to_number == user.mobile_e164
    assert row.purpose == SmsPurpose.POSSIBLE_MATCH
    assert row.provider == "memory"
    assert row.provider_message_id == "mem-1"


def test_send_sms_without_mobile_is_skipped(app, make_user, sms_outbox):
    user = make_user(mobile_e164=None)
    result = send_sms(user, "hi", SmsPurpose.POSSIBLE_MATCH)
    assert not result.ok
    assert sms_outbox == []
    (row,) = _logs()
    assert row.status == "skipped"
    assert "no mobile" in row.error


def test_send_sms_rejects_unknown_purpose(app, user):
    with pytest.raises(ValueError):
        send_sms(user, "hi", "marketing")


def test_send_sms_logs_provider_failure(app, user, monkeypatch):
    class Failing:
        name = "failing"

        def send(self, to, message):
            return SmsResult(ok=False, provider="failing", error="boom")

    monkeypatch.setattr(sms_service, "get_provider", lambda: Failing())
    result = send_sms(user, "hi", SmsPurpose.POSSIBLE_MATCH)
    assert not result.ok
    (row,) = _logs()
    assert (row.status, row.error, row.provider) == ("failed", "boom", "failing")


def test_send_sms_survives_provider_crash(app, user, monkeypatch):
    class Crashing:
        name = "crashing"

        def send(self, to, message):
            raise RuntimeError("secret-ish detail")

    monkeypatch.setattr(sms_service, "get_provider", lambda: Crashing())
    result = send_sms(user, "hi", SmsPurpose.POSSIBLE_MATCH)
    assert not result.ok
    (row,) = _logs()
    assert row.status == "failed"
    assert row.error == "RuntimeError"


def test_otp_codes_are_redacted_in_sms_log(app, user, sms_outbox):
    send_sms(user, "PawBalik verification code: 123456. It expires in 10 minutes.", SmsPurpose.OTP)
    assert "123456" in sms_outbox[0]["message"]  # the user gets the real code
    (row,) = _logs()
    assert "123456" not in row.message
    assert "******" in row.message
    assert "10 minutes" in row.message


# --- BulkSmsPhProvider (requests mocked with `responses`) ------------------------------------


@pytest.fixture
def bulksms(app):
    app.config.update(
        SMS_PROVIDER="bulksms",
        BULKSMS_API_URL=BULK_URL,
        BULKSMS_USERNAME="pawbalik-user",
        BULKSMS_API_KEY="very-secret-key",
        BULKSMS_SENDER_NAME="PawBalikAlerts",  # 14 chars -> trimmed to 11
        BULKSMS_TIMEOUT_SECONDS=7,
    )
    return BulkSmsPhProvider()


def test_bulksms_number_format():
    assert BulkSmsPhProvider.to_provider_number("+639171234567") == "639171234567"
    assert BulkSmsPhProvider.to_provider_number("09171234567") == "639171234567"


def test_bulksms_payload(bulksms):
    payload = bulksms.build_payload("+639171234567", "Hello")
    assert payload == {
        "un": "pawbalik-user",
        "pwd": "very-secret-key",
        "type": "1",
        "agreedterm": "YES",
        "sendid": "PawBalikAle",
        "messages": [{"dstno": "639171234567", "msg": "Hello"}],
    }


@responses.activate
def test_bulksms_send_success(bulksms):
    responses.post(
        BULK_URL,
        json={
            "status": "success",
            "code": 2000,
            "results": [{"dstno": "639171234567", "code": 2000, "sms_id": "1143007207"}],
        },
    )
    result = bulksms.send("+639171234567", "Hello")
    assert result == SmsResult(ok=True, provider="bulksms", message_id="1143007207")
    call = responses.calls[0]
    body = json.loads(call.request.body)
    assert body["messages"] == [{"dstno": "639171234567", "msg": "Hello"}]
    assert call.request.headers["Content-Type"] == "application/json"
    assert "very-secret-key" not in call.request.url  # credentials only in the POST body


@responses.activate
def test_bulksms_passes_timeout(bulksms, monkeypatch):
    seen = {}
    real_post = requests.post

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_post(*args, **kwargs)

    monkeypatch.setattr(sms_service.requests, "post", spy)
    responses.post(BULK_URL, json={"code": 2000, "results": [{"code": 2000, "sms_id": "9"}]})
    bulksms.send("+639171234567", "Hello")
    assert seen["timeout"] == 7.0


@responses.activate
def test_bulksms_error_code(bulksms):
    responses.post(BULK_URL, json={"status": "error", "code": -1004, "results": [{"code": -1004}]})
    result = bulksms.send("+639171234567", "Hello")
    assert not result.ok
    assert "-1004" in result.error and "credits" in result.error


@responses.activate
def test_bulksms_plain_text_response(bulksms):
    responses.post(BULK_URL, body="2000 = SUCCESS:555")
    assert bulksms.send("+639171234567", "Hello").message_id == "555"


@responses.activate
def test_bulksms_http_error(bulksms):
    responses.post(BULK_URL, status=503)
    result = bulksms.send("+639171234567", "Hello")
    assert (result.ok, result.error) == (False, "HTTP 503")


@responses.activate
def test_bulksms_network_error_does_not_leak_key(bulksms):
    responses.post(BULK_URL, body=requests.ConnectionError("pwd=very-secret-key"))
    result = bulksms.send("+639171234567", "Hello")
    assert not result.ok
    assert "very-secret-key" not in result.error
    assert result.error == "network error: ConnectionError"


def test_bulksms_not_configured(app):
    app.config.update(BULKSMS_USERNAME=None, BULKSMS_API_KEY=None)
    result = BulkSmsPhProvider(username="", api_key="").send("+639171234567", "Hello")
    assert not result.ok and "not configured" in result.error


@responses.activate
def test_send_sms_through_bulksms_logs_message_id(bulksms, user, caplog):
    responses.post(BULK_URL, json={"code": 2000, "results": [{"code": 2000, "sms_id": "77"}]})
    result = send_sms(user, "Hello", SmsPurpose.POSSIBLE_MATCH)
    assert result.ok
    (row,) = _logs()
    assert (row.provider, row.provider_message_id, row.status) == ("bulksms", "77", "sent")
    assert "very-secret-key" not in caplog.text
