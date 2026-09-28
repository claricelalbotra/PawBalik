"""In-app notifications + push (Firebase Cloud Messaging) + match SMS dispatch (SPEC 9).

How a notification travels
--------------------------
1. A feature calls ``notify(user, event, data)``. It writes the in-app
   ``notifications`` row (title, body, link) and enqueues a
   ``dispatch_notification`` job in the caller's transaction.
2. The worker (or JOBS_EAGER inline mode) runs :func:`dispatch`, which sends
   the channels the event table below allows, honouring the user's preferences:

   ==========================  ======  ====  ===
   Event                       In-app  Push  SMS
   ==========================  ======  ====  ===
   ``possible_match``          yes     yes   yes (opt-in + verified mobile + daily cap)
   ``new_message``             yes     yes   no
   ``case_status_changed``     yes     yes   no
   ==========================  ======  ====  ===

   Every push attempt is written to ``push_log`` and every SMS attempt to
   ``sms_log`` (``sent``, ``failed`` or ``skipped`` with the reason).

Data keys each caller passes (``*`` = required):

* ``possible_match``      - {"scan_id"*, "lost_report_id"*, "sighting_id", "similarity" (int %),
                             "case_number", "link_url"}
* ``new_message``         - {"conversation_id"*, "message_id", "sender_name", "case_number",
                             "link_url"}
* ``case_status_changed`` - {"lost_report_id"*, "case_number", "from_status", "to_status",
                             "link_url"}

``title`` / ``body`` may be passed in ``data`` to override the default wording.
Wording rule: always "possible match", never "this is your pet".

Push providers (``PUSH_PROVIDER``): ``fcm`` uses firebase-admin with the service
account at ``FIREBASE_CREDENTIALS_PATH`` and turns itself off with one log line
when that file is missing; ``memory`` (tests) appends dicts to
``current_app.extensions["push_outbox"]``.
"""

from __future__ import annotations

import logging
import threading
import warnings
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from flask import current_app
from werkzeug.routing import BuildError

from app.extensions import db
from app.models import (
    DeviceToken,
    Notification,
    NotificationEvent,
    PushLog,
    ReportStatus,
    SmsLog,
    SmsPurpose,
    User,
)
from app.services.job_service import enqueue, register
from app.utils.time import utcnow

log = logging.getLogger(__name__)

# Which channels each event uses (in-app is always on). SPEC 9 table.
EVENT_CHANNELS: dict[str, dict[str, bool]] = {
    NotificationEvent.POSSIBLE_MATCH: {"push": True, "sms": True},
    NotificationEvent.NEW_MESSAGE: {"push": True, "sms": False},
    NotificationEvent.CASE_STATUS_CHANGED: {"push": True, "sms": False},
}

DEFAULT_TITLES = {
    NotificationEvent.POSSIBLE_MATCH: "New possible match for your lost pet",
    NotificationEvent.NEW_MESSAGE: "New message about a case",
    NotificationEvent.CASE_STATUS_CHANGED: "A case you follow was updated",
}

TITLE_MAX = 160
BODY_MAX = 500


# --- Building the in-app record ----------------------------------------------------------------


def _path_for(endpoint: str, **values: Any) -> str | None:
    """Relative URL for ``endpoint`` that works with or without a request (worker)."""
    try:
        return current_app.url_map.bind("localhost").build(endpoint, values)
    except BuildError:
        return None


def _case_label(data: dict[str, Any]) -> str:
    return f"case {data['case_number']}" if data.get("case_number") else "your case"


def build_content(event: str, data: dict[str, Any]) -> tuple[str, str, str | None]:
    """(title, body, link_url) for an event; ``data`` may override any of them."""
    link = data.get("link_url")
    if event == NotificationEvent.POSSIBLE_MATCH:
        similarity = data.get("similarity")
        pct = f" ({int(round(float(similarity)))}% visual similarity)" if similarity else ""
        body = (
            f"We found a possible match{pct} for {_case_label(data)}. Open it to review the "
            "photos and details. Similarity does not confirm it is the same animal."
        )
        if not link and data.get("scan_id"):
            link = _path_for("scanner.results", scan_id=int(data["scan_id"]))
        if not link and data.get("lost_report_id"):
            link = _path_for("cases.detail", report_id=int(data["lost_report_id"]))
    elif event == NotificationEvent.NEW_MESSAGE:
        sender = data.get("sender_name") or "Someone"
        about = f" about case {data['case_number']}" if data.get("case_number") else ""
        body = f"{sender} sent you a message{about}."
        if not link and data.get("conversation_id"):
            link = _path_for("messages.thread", conversation_id=int(data["conversation_id"]))
    elif event == NotificationEvent.CASE_STATUS_CHANGED:
        to_status = data.get("to_status")
        label = ReportStatus.label(to_status) if to_status else None
        case = f"Case {data['case_number']}" if data.get("case_number") else "A case"
        body = f"{case} is now marked {label}." if label else f"{case} was updated."
        if not link and data.get("lost_report_id"):
            link = _path_for("cases.detail", report_id=int(data["lost_report_id"]))
    else:  # pragma: no cover - guarded by notify()
        body = ""
    title = data.get("title") or DEFAULT_TITLES[event]
    body = data.get("body") or body
    return title[:TITLE_MAX], body[:BODY_MAX], (link or None)


def notify(user: User, event: str, data: dict[str, Any] | None = None) -> Notification:
    """Create the in-app notification, then queue push/SMS dispatch (flushes; caller commits)."""
    data = dict(data or {})
    if event not in NotificationEvent.ALL:
        raise ValueError(f"Unknown notification event {event!r}")
    title, body, link = build_content(event, data)
    if link and not link.startswith("/"):
        link = None  # only relative in-app links (no open redirects via notifications.open)
    notification = Notification(
        user_id=user.id, type=event, title=title, body=body, link_url=link, is_read=False
    )
    db.session.add(notification)
    db.session.flush()
    enqueue("dispatch_notification", {"notification_id": notification.id, "event": event})
    return notification


def unread_count(user: Any) -> int:
    """Unread in-app notifications for ``user`` (0 for guests)."""
    if not getattr(user, "is_authenticated", False):
        return 0
    return (
        db.session.scalar(
            sa.select(sa.func.count(Notification.id)).where(
                Notification.user_id == user.id, Notification.is_read.is_(False)
            )
        )
        or 0
    )


def mark_read(user: User, notification_id: int) -> Notification | None:
    """Mark one of ``user``'s notifications read; None if it is not theirs (flushes)."""
    notification = db.session.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        return None
    notification.is_read = True
    db.session.flush()
    return notification


def mark_all_read(user: User) -> int:
    """Mark every unread notification of ``user`` read; returns how many changed."""
    result = db.session.execute(
        sa.update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True)
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount or 0)


def absolute_url(path: str | None) -> str:
    """``APP_BASE_URL`` + relative path (for SMS text and push click links)."""
    base = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    if not path:
        return base + "/"
    return path if path.startswith(("http://", "https://")) else base + path


# --- Device tokens -------------------------------------------------------------------------------


def register_device(user: User, token: str, user_agent: str | None = None) -> DeviceToken:
    """Upsert a browser's FCM token for ``user`` (a token moves to the latest user; flushes)."""
    device = db.session.scalar(sa.select(DeviceToken).where(DeviceToken.fcm_token == token))
    now = utcnow()
    if device is None:
        device = DeviceToken(user_id=user.id, fcm_token=token)
        db.session.add(device)
    device.user_id = user.id  # a shared computer: the token follows whoever enabled alerts
    device.user_agent = (user_agent or "")[:255] or None
    device.last_seen_at = now
    db.session.flush()
    return device


def unregister_device(user: User, token: str) -> bool:
    """Delete one of ``user``'s tokens. False if it does not exist or is someone else's."""
    device = db.session.scalar(
        sa.select(DeviceToken).where(DeviceToken.fcm_token == token, DeviceToken.user_id == user.id)
    )
    if device is None:
        return False
    db.session.delete(device)
    db.session.flush()
    return True


# --- Push providers ------------------------------------------------------------------------------


@dataclass
class PushMessage:
    title: str
    body: str
    link: str  # absolute URL opened when the notification is clicked
    data: dict[str, str] = field(default_factory=dict)


@dataclass
class PushResult:
    token: str
    ok: bool
    error: str | None = None
    invalid_token: bool = False  # the token should be deleted (unregistered/invalid)
    message_id: str | None = None


class MemoryPushProvider:
    """Tests: append one dict per token to ``current_app.extensions["push_outbox"]``."""

    name = "memory"

    def is_enabled(self) -> bool:
        return True

    def send(self, tokens: list[str], message: PushMessage) -> list[PushResult]:
        outbox = current_app.extensions.setdefault("push_outbox", [])
        results = []
        for token in tokens:
            outbox.append(
                {
                    "token": token,
                    "title": message.title,
                    "body": message.body,
                    "link": message.link,
                    "data": dict(message.data),
                }
            )
            results.append(PushResult(token=token, ok=True, message_id=f"mem-{len(outbox)}"))
        return results


_firebase_lock = threading.Lock()
_firebase_state: dict[str, Any] = {"app": None, "path": None, "warned": False}
FIREBASE_APP_NAME = "pawbalik"


def firebase_app() -> Any | None:
    """The firebase-admin App, initialised once from ``FIREBASE_CREDENTIALS_PATH``.

    Returns None (push disabled, logged once) when the path is unset, missing or invalid.
    """
    path = current_app.config.get("FIREBASE_CREDENTIALS_PATH")
    with _firebase_lock:
        if _firebase_state["app"] is not None and _firebase_state["path"] == path:
            return _firebase_state["app"]
        if not path or not Path(path).is_file():
            if not _firebase_state["warned"]:
                log.info(
                    "Push notifications disabled: FIREBASE_CREDENTIALS_PATH is %s.",
                    "not set" if not path else "not an existing file",
                )
                _firebase_state["warned"] = True
            return None
        try:
            import firebase_admin
            from firebase_admin import credentials

            try:
                app = firebase_admin.get_app(FIREBASE_APP_NAME)
            except ValueError:
                app = firebase_admin.initialize_app(
                    credentials.Certificate(path), name=FIREBASE_APP_NAME
                )
        except Exception:  # noqa: BLE001 - bad JSON, wrong key type ...
            log.exception("Push notifications disabled: could not load Firebase credentials.")
            _firebase_state["warned"] = True
            return None
        _firebase_state.update(app=app, path=path, warned=False)
        return app


def is_invalid_token_error(exc: BaseException | None) -> bool:
    """True when FCM says the token is dead and should be deleted.

    From firebase_admin.messaging (verified in the installed 7.7.0 source):
    ``UnregisteredError`` (FCM error UNREGISTERED: app uninstalled / token expired),
    ``SenderIdMismatchError`` (token belongs to another Firebase project), and
    ``InvalidArgumentError`` whose message is about the registration token (the same
    error class is also used for bad payloads, which must not delete tokens).
    """
    if exc is None:
        return False
    from firebase_admin import exceptions, messaging

    if isinstance(exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
        return True
    if isinstance(exc, exceptions.InvalidArgumentError):
        return "registration token" in str(exc).lower()
    return False


class FcmPushProvider:
    """Firebase Cloud Messaging via the official firebase-admin SDK."""

    name = "fcm"

    def is_enabled(self) -> bool:
        return firebase_app() is not None

    def build_messages(self, tokens: list[str], message: PushMessage) -> list[Any]:
        from firebase_admin import messaging

        webpush_options = None
        # FCM only accepts an HTTPS click link (WebpushFCMOptions.link, checked by the SDK).
        # On http://localhost the service worker opens data["link"] itself instead.
        if message.link.startswith("https://"):
            webpush_options = messaging.WebpushFCMOptions(link=message.link)
        data = {"link": message.link, **message.data}
        webpush = messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                title=message.title,
                body=message.body,
                icon="/static/images/logo.svg",
                data={"link": message.link},
            ),
            data=data,
            fcm_options=webpush_options,
        )
        with warnings.catch_warnings():
            # firebase-admin 7.x marks Message.token as deprecated in favour of `fid`, but the
            # Firebase JS SDK getToken() returns a registration *token*, which is what we store.
            # TODO(verify): switch to fid= if the web SDK moves to installation IDs.
            warnings.simplefilter("ignore", DeprecationWarning)
            return [messaging.Message(token=token, data=data, webpush=webpush) for token in tokens]

    def send(self, tokens: list[str], message: PushMessage) -> list[PushResult]:
        from firebase_admin import exceptions, messaging

        app = firebase_app()
        if app is None:
            return [PushResult(token=t, ok=False, error="push disabled") for t in tokens]
        try:
            batch = messaging.send_each(self.build_messages(tokens, message), app=app)
        except (exceptions.FirebaseError, ValueError) as exc:
            log.warning("FCM send failed: %s", exc)
            return [PushResult(token=t, ok=False, error=_short_error(exc)) for t in tokens]

        results = []
        for token, response in zip(tokens, batch.responses, strict=False):
            if response.success:
                results.append(PushResult(token=token, ok=True, message_id=response.message_id))
            else:
                exc = response.exception
                results.append(
                    PushResult(
                        token=token,
                        ok=False,
                        error=_short_error(exc),
                        invalid_token=is_invalid_token_error(exc),
                    )
                )
        return results


def _short_error(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown error"
    code = getattr(exc, "code", None)
    text = f"{type(exc).__name__}" + (f" ({code})" if code else "") + f": {exc}"
    return text[:500]


PUSH_PROVIDERS: dict[str, type] = {"fcm": FcmPushProvider, "memory": MemoryPushProvider}


def get_push_provider() -> FcmPushProvider | MemoryPushProvider:
    name = (current_app.config.get("PUSH_PROVIDER") or "fcm").strip().lower()
    return PUSH_PROVIDERS.get(name, FcmPushProvider)()


# --- Dispatch --------------------------------------------------------------------------------------


def _log_push(user: User, title: str, status: str, error: str | None = None, token_id=None) -> None:
    db.session.add(
        PushLog(user_id=user.id, token_id=token_id, title=title[:160], status=status, error=error)
    )


def send_push(user: User, notification: Notification) -> dict[str, int]:
    """Push ``notification`` to every device of ``user``; dead tokens are removed.

    Returns counts ``{"sent", "failed", "skipped", "removed"}``. Never raises for FCM
    problems (they are logged in push_log instead).
    """
    counts = {"sent": 0, "failed": 0, "skipped": 0, "removed": 0}
    title = notification.title
    if not user.push_opt_in:
        _log_push(user, title, "skipped", "push alerts are turned off in the profile")
        counts["skipped"] += 1
        return counts
    devices = list(db.session.scalars(sa.select(DeviceToken).where(DeviceToken.user_id == user.id)))
    if not devices:
        _log_push(user, title, "skipped", "no registered devices")
        counts["skipped"] += 1
        return counts
    provider = get_push_provider()
    if not provider.is_enabled():
        _log_push(user, title, "skipped", "push is not configured (FIREBASE_CREDENTIALS_PATH)")
        counts["skipped"] += 1
        return counts

    message = PushMessage(
        title=title,
        body=notification.body or "",
        link=absolute_url(notification.link_url),
        data={"notification_id": str(notification.id), "event": notification.type},
    )
    by_token = {d.fcm_token: d for d in devices}
    try:
        results = provider.send(list(by_token), message)
    except Exception as exc:  # noqa: BLE001 - never let push break the job
        log.exception("Push provider %s crashed", provider.name)
        results = [PushResult(token=t, ok=False, error=_short_error(exc)) for t in by_token]

    for result in results:
        device = by_token.get(result.token)
        if result.ok:
            _log_push(user, title, "sent", token_id=device.id if device else None)
            counts["sent"] += 1
            if device is not None:
                device.last_seen_at = utcnow()
        elif result.invalid_token and device is not None:
            _log_push(user, title, "failed", f"{result.error} - token removed")
            db.session.delete(device)
            counts["failed"] += 1
            counts["removed"] += 1
        else:
            _log_push(user, title, "failed", result.error, token_id=device.id if device else None)
            counts["failed"] += 1
    db.session.flush()
    return counts


def match_sms_text(notification: Notification) -> str:
    """Short GSM-7 SMS for a possible match (<= 160 chars with a typical base URL)."""
    return (
        "PawBalik: a possible match was found for your lost pet. "
        f"Review it: {absolute_url(notification.link_url)}"
    )


def send_match_sms(user: User, notification: Notification) -> str:
    """SMS for a possible match if the rules allow; returns sent|failed|skipped|duplicate."""
    from app.services import sms_service

    text = match_sms_text(notification)
    if not user.sms_opt_in:
        sms_service.log_skipped_sms(user, text, SmsPurpose.POSSIBLE_MATCH, "SMS alerts turned off")
        return "skipped"
    if not user.mobile_verified:
        sms_service.log_skipped_sms(
            user, text, SmsPurpose.POSSIBLE_MATCH, "mobile number not verified"
        )
        return "skipped"
    # A re-run job (crash after sending) must not send the same alert twice.
    fitted = sms_service.fit_sms(text)
    duplicate = db.session.scalar(
        sa.select(SmsLog.id)
        .where(
            SmsLog.user_id == user.id,
            SmsLog.purpose == SmsPurpose.POSSIBLE_MATCH,
            SmsLog.status == "sent",
            SmsLog.message == fitted,
            SmsLog.created_at >= utcnow() - timedelta(days=1),
        )
        .limit(1)
    )
    if duplicate is not None:
        return "duplicate"
    limit = int(current_app.config.get("SMS_DAILY_MATCH_LIMIT") or 0)
    if sms_service.sent_match_sms_today(user) >= limit:
        sms_service.log_skipped_sms(
            user, text, SmsPurpose.POSSIBLE_MATCH, f"daily limit of {limit} match SMS reached"
        )
        return "skipped"
    result = sms_service.send_sms(user, text, SmsPurpose.POSSIBLE_MATCH)
    return "sent" if result.ok else "failed"


def dispatch(notification_id: int, event: str | None = None) -> dict[str, Any]:
    """Send push (and SMS for possible_match) for one notification (flushes).

    Safe to run twice. Returns a small summary (handy in tests and logs).
    """
    notification = db.session.get(Notification, notification_id)
    if notification is None:
        log.info("dispatch_notification: notification %s no longer exists", notification_id)
        return {"status": "missing"}
    event = notification.type or event
    user = db.session.get(User, notification.user_id)
    if user is None or not user.is_active:
        return {"status": "inactive_user"}
    channels = EVENT_CHANNELS.get(event, {})
    summary: dict[str, Any] = {"status": "done", "push": None, "sms": None}
    if channels.get("push"):
        summary["push"] = send_push(user, notification)
    if channels.get("sms"):
        summary["sms"] = send_match_sms(user, notification)
    db.session.flush()  # the worker (or the eager caller) commits
    return summary


@register("dispatch_notification")
def _job_dispatch_notification(payload: dict) -> None:
    """payload: {"notification_id": int, "event": str}"""
    dispatch(int(payload["notification_id"]), payload.get("event"))
