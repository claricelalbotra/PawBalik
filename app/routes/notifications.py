"""Notification list, phone OTP verification, /api/devices, FCM service worker (SPEC 9).

Pages
  GET  /notifications                  notifications.index (unread first styling, paging)
  GET  /notifications/<id>             notifications.open (mark read + go to its link)
  POST /notifications/read-all         notifications.mark_all_read
  GET/POST /account/verify-phone       notifications.verify_phone (send code / enter code)
  GET  /firebase-messaging-sw.js       notifications.service_worker (site root, JavaScript)

JSON API
  GET    /api/notifications            {"notifications": [...], "unread": int}
  POST   /api/notifications/<id>/read  {"ok": true, "unread": int}
  POST   /api/notifications/read-all   {"ok": true, "updated": int, "unread": 0}
  POST   /api/devices {"token"}        201 {"ok": true, "id": int}
  DELETE /api/devices/<token>          204

Only the owner of a notification or device token can read or change it.
"""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import config_limit, db, limiter
from app.models import Notification
from app.services import notification_service as ns
from app.services import sms_service
from app.utils.auth import api_login_required
from app.utils.http import is_safe_redirect_url, json_error, json_ok, safe_next_url

bp = Blueprint("notifications", __name__)

PER_PAGE = 20
API_MAX_LIMIT = 50
# FCM registration tokens are long URL-safe strings (letters, digits, - _ : .).
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{20,512}$")


def _page_arg() -> int:
    try:
        return max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        return 1


def _own_notifications_query():
    return (
        sa.select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )


def _safe_link(link: str | None) -> str | None:
    """Only relative in-app links (``/...``, never ``//host``) are followed."""
    if link and link.startswith("/") and not link.startswith("//") and is_safe_redirect_url(link):
        return link
    return None


# --- Pages -----------------------------------------------------------------------------------


@bp.get("/notifications")
@login_required
def index():
    """The user's notifications, newest first (20 per page)."""
    page = _page_arg()
    total = (
        db.session.scalar(
            sa.select(sa.func.count(Notification.id)).where(Notification.user_id == current_user.id)
        )
        or 0
    )
    items = list(
        db.session.scalars(_own_notifications_query().limit(PER_PAGE).offset((page - 1) * PER_PAGE))
    )
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return render_template(
        "notifications/list.html",
        notifications=items,
        page=page,
        pages=pages,
        total=total,
        unread=ns.unread_count(current_user),
    )


@bp.get("/notifications/<int:notification_id>")
@login_required
def open(notification_id: int):  # noqa: A001 - endpoint name "notifications.open"
    """Mark one notification read and redirect to its link_url (only relative app URLs)."""
    notification = ns.mark_read(current_user, notification_id)
    if notification is None:
        abort(404)
    db.session.commit()
    return redirect(_safe_link(notification.link_url) or url_for("notifications.index"))


@bp.post("/notifications/read-all")
@login_required
def mark_all_read():
    """Mark all of the user's notifications read, then redirect back."""
    updated = ns.mark_all_read(current_user)
    db.session.commit()
    if updated:
        flash("All notifications marked as read.", "success")
    return redirect(safe_next_url(url_for("notifications.index")))


def _not_sending_code() -> bool:
    """Only "send a code" requests count towards RATE_LIMIT_OTP (code checks have their own cap)."""
    return request.method != "POST" or request.form.get("action") != "send"


@bp.route("/account/verify-phone", methods=["GET", "POST"])
@login_required
@limiter.limit(config_limit("RATE_LIMIT_OTP"), methods=["POST"], exempt_when=_not_sending_code)
def verify_phone():
    """Send an OTP to the user's mobile (sms_service.send_otp) and check the code."""
    user = current_user._get_current_object()
    next_url = request.values.get("next")
    next_url = next_url if is_safe_redirect_url(next_url) else None
    error = None

    if request.method == "POST" and user.mobile_e164 and not user.mobile_verified:
        action = request.form.get("action")
        if action == "send":
            try:
                sms_service.send_otp(user)
            except sms_service.OtpError as exc:
                error = exc.message
            else:
                flash(f"We sent a 6-digit code to {_display_phone(user.mobile_e164)}.", "success")
            db.session.commit()  # keep the code (or the failed attempt in sms_log)
            if error is None:
                return redirect(url_for("notifications.verify_phone", next=next_url))
        elif action == "verify":
            code = (request.form.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", re.sub(r"\s|-", "", code)):
                error = "Enter the 6-digit code from the SMS."
            else:
                ok = sms_service.verify_otp(user, code)
                db.session.commit()  # attempts are counted even when the code is wrong
                if ok:
                    flash(
                        "Your mobile number is verified. You can now get SMS match alerts.",
                        "success",
                    )
                    return redirect(next_url or url_for("notifications.verify_phone"))
                error = _verify_error_message(user)
        else:
            error = "Please choose an action."

    status = sms_service.otp_status(user) if user.mobile_e164 else None
    response = render_template(
        "notifications/verify_phone.html",
        status=status,
        error=error,
        next_url=next_url,
        code_length=current_app.config.get("OTP_LENGTH", 6),
        ttl_minutes=current_app.config.get("OTP_TTL_MINUTES", 10),
    )
    return response, (422 if error else 200)


def _display_phone(e164: str) -> str:
    from app.utils.phone import format_phone

    return format_phone(e164)


def _verify_error_message(user) -> str:
    status = sms_service.otp_status(user)
    if status.locked:
        return "Too many wrong tries. Please request a new code."
    if not status.has_active_code:
        return "This code has expired or was already used. Please request a new code."
    left = status.attempts_left
    return f"That code is not correct. {left} {'try' if left == 1 else 'tries'} left."


@bp.get("/firebase-messaging-sw.js")
def service_worker():
    """FCM service worker at the site root (JavaScript, Firebase web config injected)."""
    firebase = (current_app.jinja_env.globals.get("public_config") or {}).get("firebase") or {}
    response = make_response(
        render_template(
            "notifications/firebase-messaging-sw.js",
            enabled=bool(firebase.get("enabled")),
            firebase_config=firebase.get("config") or {},
            js_version=firebase.get("jsVersion") or current_app.config.get("FIREBASE_JS_VERSION"),
        )
    )
    response.mimetype = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


# --- JSON API ------------------------------------------------------------------------------


def _serialize(notification: Notification) -> dict[str, Any]:
    from app.utils.time import to_local

    created = to_local(notification.created_at)
    return {
        "id": notification.id,
        "type": notification.type,
        "title": notification.title,
        "body": notification.body,
        "link_url": _safe_link(notification.link_url),
        "open_url": url_for("notifications.open", notification_id=notification.id),
        "is_read": bool(notification.is_read),
        "created_at": created.isoformat() if created else None,
    }


@bp.get("/api/notifications")
@api_login_required
def api_list():
    """GET /api/notifications?limit=20&before_id= -> {"notifications": [...], "unread": int}."""
    try:
        limit = min(API_MAX_LIMIT, max(1, int(request.args.get("limit", PER_PAGE))))
    except (TypeError, ValueError):
        return json_error("validation_error", "limit must be a number.", 400)
    stmt = _own_notifications_query()
    before_id = request.args.get("before_id")
    if before_id:
        if not before_id.isdigit():
            return json_error("validation_error", "before_id must be a number.", 400)
        stmt = stmt.where(Notification.id < int(before_id))
    if request.args.get("unread") in ("1", "true"):
        stmt = stmt.where(Notification.is_read.is_(False))
    items = list(db.session.scalars(stmt.limit(limit)))
    return json_ok(
        {"notifications": [_serialize(n) for n in items], "unread": ns.unread_count(current_user)}
    )


@bp.post("/api/notifications/<int:notification_id>/read")
@api_login_required
def api_mark_read(notification_id: int):
    """POST /api/notifications/<id>/read -> {"ok": true, "unread": int}."""
    if ns.mark_read(current_user, notification_id) is None:
        return json_error("not_found", "Notification not found.", 404)
    db.session.commit()
    return json_ok({"ok": True, "unread": ns.unread_count(current_user)})


@bp.post("/api/notifications/read-all")
@api_login_required
def api_mark_all_read():
    """POST /api/notifications/read-all -> {"ok": true, "updated": int, "unread": 0}."""
    updated = ns.mark_all_read(current_user)
    db.session.commit()
    return json_ok({"ok": True, "updated": updated, "unread": ns.unread_count(current_user)})


@bp.post("/api/devices")
@api_login_required
def api_register_device():
    """POST /api/devices {"token": "<fcm token>"} -> 201 (upsert device_tokens).

    Clicking "Enable match alerts" is an explicit opt-in, so push_opt_in is switched on.
    """
    data = request.get_json(silent=True) or {}
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token.strip()):
        return json_error("validation_error", "A valid push token is required.", 400)
    device = ns.register_device(
        current_user, token.strip(), user_agent=request.headers.get("User-Agent")
    )
    current_user.push_opt_in = True
    db.session.commit()
    return json_ok({"ok": True, "id": device.id}, 201)


@bp.delete("/api/devices/<path:token>")
@api_login_required
def api_delete_device(token: str):
    """DELETE /api/devices/<token> -> 204 (own tokens only)."""
    if not ns.unregister_device(current_user, token):
        return json_error("not_found", "This device is not registered.", 404)
    db.session.commit()
    return "", 204
