"""Firebase push: FCM provider (firebase-admin mocked), token cleanup, /api/devices, service worker."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from firebase_admin import exceptions as fb_exceptions
from firebase_admin import messaging

from app.extensions import db
from app.models import DeviceToken, NotificationEvent, PushLog
from app.services import notification_service as ns

TOKEN_A = "fcm-token-AAAAAAAAAAAAAAAAAAAAAAAA:APA91b"
TOKEN_B = "fcm-token-BBBBBBBBBBBBBBBBBBBBBBBB:APA91b"
TOKEN_C = "fcm-token-CCCCCCCCCCCCCCCCCCCCCCCC:APA91b"

FIREBASE_WEB = {
    "apiKey": "web-api-key",
    "authDomain": "pawbalik.firebaseapp.com",
    "projectId": "pawbalik",
    "storageBucket": "pawbalik.appspot.com",
    "messagingSenderId": "1234567890",
    "appId": "1:1234567890:web:abc",
}


def _tokens(user) -> set[str]:
    return set(
        db.session.scalars(sa.select(DeviceToken.fcm_token).where(DeviceToken.user_id == user.id))
    )


def _add_tokens(user, *tokens):
    for token in tokens:
        db.session.add(DeviceToken(user_id=user.id, fcm_token=token))
    db.session.commit()


@pytest.fixture
def fcm(app, monkeypatch):
    """PUSH_PROVIDER=fcm with firebase-admin mocked: set `fcm.responses` per token."""

    class FakeFcm:
        def __init__(self):
            self.sent: list = []
            self.responses: dict[str, messaging.SendResponse] = {}
            self.raise_exc: Exception | None = None

        def send_each(self, messages, dry_run=False, app=None):
            if self.raise_exc:
                raise self.raise_exc
            self.sent.extend(messages)
            ok = messaging.SendResponse({"name": "projects/pawbalik/messages/1"}, None)
            return messaging.BatchResponse([self.responses.get(m.token, ok) for m in messages])

    fake = FakeFcm()
    app.config["PUSH_PROVIDER"] = "fcm"
    monkeypatch.setattr(ns, "firebase_app", lambda: object())
    monkeypatch.setattr(messaging, "send_each", fake.send_each)
    return fake


def _failed(exc):
    return messaging.SendResponse(None, exc)


def test_fcm_sends_webpush_messages(app, user, fcm):
    _add_tokens(user, TOKEN_A)
    n = ns.notify(user, NotificationEvent.NEW_MESSAGE, {"conversation_id": 8, "sender_name": "Jo"})
    (message,) = fcm.sent
    assert message.token == TOKEN_A
    assert message.webpush.notification.title == n.title
    assert message.webpush.notification.body == "Jo sent you a message."
    assert message.webpush.data["link"] == "http://localhost/messages/8"
    assert message.webpush.data["notification_id"] == str(n.id)
    # http links are not allowed as FCM fcm_options.link; the service worker uses data.link.
    assert message.webpush.fcm_options is None
    (log,) = db.session.scalars(sa.select(PushLog))
    assert log.status == "sent" and log.token_id is not None


def test_fcm_uses_https_link_option(app, user, fcm):
    app.config["APP_BASE_URL"] = "https://pawbalik.example.ph"
    _add_tokens(user, TOKEN_A)
    ns.notify(
        user, NotificationEvent.CASE_STATUS_CHANGED, {"lost_report_id": 4, "to_status": "found"}
    )
    (message,) = fcm.sent
    assert message.webpush.fcm_options.link == "https://pawbalik.example.ph/cases/4"
    # The SDK validates the message when it is encoded; this must not raise.
    assert "https://pawbalik.example.ph/cases/4" in str(message)


def test_invalid_tokens_are_removed(app, user, fcm):
    _add_tokens(user, TOKEN_A, TOKEN_B, TOKEN_C)
    fcm.responses = {
        TOKEN_A: _failed(messaging.UnregisteredError("Requested entity was not found.")),
        TOKEN_B: _failed(
            fb_exceptions.InvalidArgumentError(
                "The registration token is not a valid FCM registration token"
            )
        ),
    }
    ns.notify(user, NotificationEvent.NEW_MESSAGE, {"conversation_id": 1})
    db.session.commit()
    assert _tokens(user) == {TOKEN_C}
    logs = list(db.session.scalars(sa.select(PushLog).order_by(PushLog.id)))
    assert sorted(log.status for log in logs) == ["failed", "failed", "sent"]
    removed = [log for log in logs if log.error and "token removed" in log.error]
    assert len(removed) == 2
    assert any("UnregisteredError" in log.error for log in removed)


def test_sender_id_mismatch_removes_token(app, user, fcm):
    _add_tokens(user, TOKEN_A)
    fcm.responses = {TOKEN_A: _failed(messaging.SenderIdMismatchError("wrong sender"))}
    ns.notify(user, NotificationEvent.NEW_MESSAGE, {"conversation_id": 1})
    assert _tokens(user) == set()


def test_temporary_errors_keep_tokens(app, user, fcm):
    _add_tokens(user, TOKEN_A, TOKEN_B)
    fcm.responses = {
        TOKEN_A: _failed(fb_exceptions.UnavailableError("try later")),
        TOKEN_B: _failed(fb_exceptions.InvalidArgumentError("Invalid JSON payload received.")),
    }
    ns.notify(user, NotificationEvent.NEW_MESSAGE, {"conversation_id": 1})
    assert _tokens(user) == {TOKEN_A, TOKEN_B}
    statuses = {log.status for log in db.session.scalars(sa.select(PushLog))}
    assert statuses == {"failed"}


def test_whole_batch_failure_is_logged(app, user, fcm):
    _add_tokens(user, TOKEN_A)
    fcm.raise_exc = fb_exceptions.UnauthenticatedError("bad credentials")
    ns.notify(user, NotificationEvent.NEW_MESSAGE, {"conversation_id": 1})
    (log,) = db.session.scalars(sa.select(PushLog))
    assert log.status == "failed" and "UnauthenticatedError" in log.error
    assert _tokens(user) == {TOKEN_A}


def test_fcm_disabled_without_credentials(app, user, caplog):
    app.config.update(PUSH_PROVIDER="fcm", FIREBASE_CREDENTIALS_PATH=None)
    _add_tokens(user, TOKEN_A)
    ns.notify(user, NotificationEvent.NEW_MESSAGE, {"conversation_id": 1})
    (log,) = db.session.scalars(sa.select(PushLog))
    assert log.status == "skipped" and "not configured" in log.error
    assert _tokens(user) == {TOKEN_A}


def test_firebase_app_with_bad_credentials_file(app, tmp_path):
    bad = tmp_path / "service-account.json"
    bad.write_text("{}", encoding="utf-8")
    app.config["FIREBASE_CREDENTIALS_PATH"] = str(bad)
    assert ns.firebase_app() is None


def test_firebase_app_missing_file(app, tmp_path):
    app.config["FIREBASE_CREDENTIALS_PATH"] = str(tmp_path / "missing.json")
    assert ns.firebase_app() is None


# --- /api/devices ----------------------------------------------------------------------------------


def test_devices_api_requires_login(client):
    assert client.post("/api/devices", json={"token": TOKEN_A}).status_code == 401
    assert client.delete(f"/api/devices/{TOKEN_A}").status_code == 401


def test_register_device(client, login, make_user):
    user = make_user(push_opt_in=False)
    login(client, user)
    response = client.post(
        "/api/devices", json={"token": TOKEN_A}, headers={"User-Agent": "TestBrowser/1.0"}
    )
    assert response.status_code == 201
    assert response.get_json()["ok"] is True
    device = db.session.scalar(sa.select(DeviceToken))
    assert (device.user_id, device.fcm_token, device.user_agent) == (
        user.id,
        TOKEN_A,
        "TestBrowser/1.0",
    )
    db.session.refresh(user)
    assert user.push_opt_in  # clicking "Enable match alerts" opts in
    # Registering again is an upsert, not a duplicate.
    assert client.post("/api/devices", json={"token": TOKEN_A}).status_code == 201
    assert db.session.scalar(sa.select(sa.func.count(DeviceToken.id))) == 1


def test_register_device_moves_token_to_current_user(client, login, make_user):
    first, second = make_user(), make_user()
    _add_tokens(first, TOKEN_A)
    login(client, second)
    client.post("/api/devices", json={"token": TOKEN_A})
    assert _tokens(first) == set() and _tokens(second) == {TOKEN_A}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": ""},
        {"token": 123},
        {"token": "short"},
        {"token": "has spaces in it which is not a token"},
        [],
    ],
)
def test_register_device_validation(client, login, user, payload):
    login(client, user)
    response = client.post("/api/devices", json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_delete_device_own_only(client, login, make_user):
    owner, other = make_user(), make_user()
    _add_tokens(owner, TOKEN_A)
    login(client, other)
    response = client.delete(f"/api/devices/{TOKEN_A}")
    assert response.status_code == 404
    assert _tokens(owner) == {TOKEN_A}
    login(client, owner)
    assert client.delete(f"/api/devices/{TOKEN_A}").status_code == 204
    assert _tokens(owner) == set()


def test_delete_device_with_encoded_token(client, login, user):
    _add_tokens(user, TOKEN_A)
    login(client, user)
    assert client.delete(f"/api/devices/{TOKEN_A.replace(':', '%3A')}").status_code == 204


def test_devices_api_needs_csrf(csrf_client, login, user):
    from conftest import get_csrf_token

    login(csrf_client, user)
    refused = csrf_client.post("/api/devices", json={"token": TOKEN_A})
    assert refused.status_code == 400
    assert refused.get_json()["error"]["code"] == "csrf_failed"
    token = get_csrf_token(csrf_client, "/notifications")
    ok = csrf_client.post("/api/devices", json={"token": TOKEN_A}, headers={"X-CSRFToken": token})
    assert ok.status_code == 201
    assert csrf_client.delete(f"/api/devices/{TOKEN_A}").status_code == 400
    deleted = csrf_client.delete(f"/api/devices/{TOKEN_A}", headers={"X-CSRFToken": token})
    assert deleted.status_code == 204


# --- service worker + enable-alerts partial ----------------------------------------------------------


def test_service_worker_disabled_without_config(client):
    response = client.get("/firebase-messaging-sw.js")
    assert response.status_code == 200
    assert response.mimetype == "application/javascript"
    assert response.headers["Cache-Control"] == "no-cache"
    body = response.get_data(as_text=True)
    assert "unregister()" in body
    assert "importScripts" not in body


@pytest.mark.config(
    FIREBASE_WEB_CONFIG=FIREBASE_WEB,
    FIREBASE_VAPID_KEY="vapid-public-key",
    FIREBASE_JS_VERSION="10.14.1",
)
def test_service_worker_with_config(client):
    response = client.get("/firebase-messaging-sw.js")
    assert response.mimetype == "application/javascript"
    assert response.headers["Service-Worker-Allowed"] == "/"
    body = response.get_data(as_text=True)
    assert 'const FIREBASE_VERSION = "10.14.1";' in body
    assert "firebase-messaging-compat.js" in body
    assert '"projectId": "pawbalik"' in body
    assert "notificationclick" in body
    assert "<" not in body.split("*/", 1)[1].split("importScripts")[0]  # no HTML leakage


@pytest.mark.config(FIREBASE_WEB_CONFIG=FIREBASE_WEB, FIREBASE_VAPID_KEY="vapid-public-key")
def test_enable_alerts_card_shown_when_configured(client, login, user):
    login(client, user)
    html = client.get("/notifications").get_data(as_text=True)
    assert "data-enable-alerts" in html
    assert "Enable match alerts" in html


def test_enable_alerts_card_hidden_without_config(client, login, user):
    login(client, user)
    html = client.get("/notifications").get_data(as_text=True)
    assert "data-enable-alerts" not in html
