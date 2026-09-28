"""Registration, login, logout, profile (app/routes/auth.py)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import DEFAULT_PASSWORD

from app.extensions import db
from app.models import AuditLog, User
from app.utils.time import utcnow

VALID = {
    "full_name": "Jonalie Montera",
    "email": "Jonalie@Example.com",
    "mobile": "0917 123 4567",
    "password": "Pawbalik2026",
    "confirm_password": "Pawbalik2026",
    "consent": "y",
}


def _user_by_email(email):
    return db.session.scalar(sa.select(User).where(User.email == email))


def _audit_events():
    return [a.event for a in db.session.scalars(sa.select(AuditLog).order_by(AuditLog.id))]


def test_register_creates_user_and_logs_in(client):
    response = client.post("/register", data=VALID)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account/verify-phone")
    user = _user_by_email("jonalie@example.com")
    assert user is not None
    assert user.mobile_e164 == "+639171234567"
    assert user.mobile_verified_at is None
    assert user.privacy_consented_at is not None
    assert user.password_hash.startswith("$argon2")
    assert user.check_password("Pawbalik2026")
    assert "auth.register" in _audit_events()
    with client.session_transaction() as session:
        assert session["_user_id"] == str(user.id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consent", ""),
        ("mobile", "12345"),
        ("password", "weakpass"),
        ("email", "not-an-email"),
        ("full_name", ""),
    ],
)
def test_register_validation(client, field, value):
    data = dict(VALID, **{field: value})
    if field == "password":
        data["confirm_password"] = value
    response = client.post("/register", data=data)
    assert response.status_code == 200
    assert _user_by_email("jonalie@example.com") is None
    assert b"field__error" in response.data


def test_register_rejects_duplicate_email_and_mobile(client, make_user):
    make_user(email="jonalie@example.com", mobile_e164="+639999999999")
    response = client.post("/register", data=VALID)
    assert b"already exists" in response.data
    make_user(email="other@example.com", mobile_e164="+639181234567")
    response = client.post(
        "/register", data=dict(VALID, email="new@example.com", mobile="09181234567")
    )
    assert b"already registered" in response.data


def test_register_rejects_password_mismatch(client):
    response = client.post("/register", data=dict(VALID, confirm_password="Different2026"))
    assert b"Passwords do not match" in response.data


def test_login_success_and_safe_next(client, make_user):
    user = make_user(email="owner@example.com")
    response = client.post(
        "/login?next=/my-cases", data={"email": "OWNER@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/my-cases")
    assert db.session.get(User, user.id).last_login_at is not None
    assert "auth.login" in _audit_events()


def test_login_ignores_unsafe_next(client, make_user):
    make_user(email="owner@example.com")
    response = client.post(
        "/login?next=https://evil.example.com/",
        data={"email": "owner@example.com", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 302
    assert "evil" not in response.headers["Location"]


def test_login_failure_is_audited(client, make_user):
    make_user(email="owner@example.com")
    response = client.post("/login", data={"email": "owner@example.com", "password": "wrong-pass1"})
    assert response.status_code == 401
    assert b"Incorrect email or password" in response.data
    response = client.post(
        "/login", data={"email": "nobody@example.com", "password": "wrong-pass1"}
    )
    assert response.status_code == 401
    assert _audit_events().count("auth.login_failed") == 2


def test_inactive_user_cannot_log_in(client, make_user):
    make_user(email="banned@example.com", is_active=False)
    response = client.post(
        "/login", data={"email": "banned@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 403
    assert b"deactivated" in response.data
    with client.session_transaction() as session:
        assert "_user_id" not in session


def test_deactivated_user_is_logged_out(client, make_user, login):
    user = make_user()
    login(client, user)
    assert client.get("/profile").status_code == 200
    user.is_active = False
    db.session.commit()
    assert client.get("/profile").status_code == 302


def test_logout_requires_post(client, user, login):
    login(client, user)
    assert client.get("/logout").status_code == 405
    response = client.post("/logout")
    assert response.status_code == 302
    assert client.get("/profile").status_code == 302


def test_login_required_redirects_with_next(client):
    response = client.get("/my-cases")
    assert response.status_code == 302
    assert "/login?next=/my-cases" in response.headers["Location"]


def test_profile_update_clears_mobile_verification(client, make_user, login):
    user = make_user(mobile_e164="+639171111111", mobile_verified_at=utcnow())
    login(client, user)
    response = client.post(
        "/profile",
        data={
            "full_name": "New Name",
            "email": user.email,
            "mobile": "0918 222 3333",
            "sms_opt_in": "y",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/account/verify-phone")
    user = db.session.get(User, user.id)
    assert user.full_name == "New Name"
    assert user.mobile_e164 == "+639182223333"
    assert user.mobile_verified_at is None
    assert user.sms_opt_in is True
    assert user.push_opt_in is False


def test_profile_same_mobile_keeps_verification(client, make_user, login):
    user = make_user(mobile_e164="+639171111111", mobile_verified_at=utcnow())
    login(client, user)
    response = client.post(
        "/profile",
        data={
            "full_name": user.full_name,
            "email": user.email,
            "mobile": "09171111111",
            "push_opt_in": "y",
        },
    )
    assert response.status_code == 302
    assert db.session.get(User, user.id).mobile_verified_at is not None


def test_profile_page_renders(client, user, login):
    login(client, user)
    response = client.get("/profile")
    assert response.status_code == 200
    assert b"Verify your mobile number" in response.data


def test_change_password(client, user, login):
    login(client, user)
    response = client.post(
        "/profile/password",
        data={
            "pw-current_password": DEFAULT_PASSWORD,
            "pw-new_password": "Brandnew2027",
            "pw-confirm_new_password": "Brandnew2027",
        },
    )
    assert response.status_code == 302
    assert db.session.get(User, user.id).check_password("Brandnew2027")
    bad = client.post(
        "/profile/password",
        data={
            "pw-current_password": "wrong",
            "pw-new_password": "Another2028",
            "pw-confirm_new_password": "Another2028",
        },
    )
    assert bad.status_code == 400
    assert b"current password is incorrect" in bad.data


def test_password_hash_helpers(make_user):
    user = make_user(password="Secret2026x")
    assert user.check_password("Secret2026x")
    assert not user.check_password("secret2026x")
    assert not user.check_password("")


@pytest.mark.config(RATELIMIT_ENABLED=True, RATE_LIMIT_LOGIN="3 per minute")
def test_login_is_rate_limited(client):
    for _ in range(3):
        client.post("/login", data={"email": "x@example.com", "password": "nope-nope1"})
    response = client.post("/login", data={"email": "x@example.com", "password": "nope-nope1"})
    assert response.status_code == 429
    assert client.get("/login").status_code == 200  # GET is not limited
