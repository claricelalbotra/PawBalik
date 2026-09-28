"""Inbox, conversation thread, location pins, /api/conversations (SPEC 5.6).

OWNER: messages team.

Access: participants only (``policy_service.can_view_conversation``); anyone
else - admins included - gets 404 so a conversation's existence is not
revealed. Rules for starting a conversation live in
``message_service.resolve_target``. Closed cases are read-only.

Delivery: the thread page renders the latest messages on the server (works
without JavaScript); ``static/js/messages.js`` then polls
``GET /api/conversations/<id>/messages?after=<id>`` every MESSAGE_POLL_SECONDS
while the tab is visible. The composer forms post to the JSON endpoint; a
plain HTML form post (no JS) is answered with a redirect back to the thread.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import config_limit, db, limiter
from app.models import (
    CandidateType,
    Conversation,
    LostReport,
    MessageType,
    ReportStatus,
    Scan,
    ScanResult,
)
from app.services import message_service as ms
from app.services import policy_service
from app.utils.auth import api_login_required
from app.utils.http import json_error, json_ok
from app.utils.strings import VERIFICATION_CHECKLIST
from app.utils.validators import ValidationError, clean_text, parse_int

bp = Blueprint("messages", __name__)

# Shown after the shared checklist (TODO(integrator): move into utils/strings.py).
EXTRA_CHECKLIST = (
    "Ask for details only the real owner would know (habits, name it answers to, quirks).",
    "Never hand over an animal until you have verified these details.",
)
PHONE_NOTE = "Phone numbers are never shown in chat automatically. Share yours only if you want to."
THREAD_PAGE_SIZE = 100
API_PAGE_SIZE = 200


# --- Helpers -----------------------------------------------------------------------------------


def _conversation_or_404(conversation_id: int) -> Conversation:
    """Page routes: the conversation if the current user takes part in it, else 404."""
    conv = db.session.get(Conversation, conversation_id)
    if conv is None or not policy_service.can_view_conversation(current_user, conv):
        abort(404)
    return conv


def _api_conversation(conversation_id: int) -> Conversation | None:
    conv = db.session.get(Conversation, conversation_id)
    if conv is None or not policy_service.can_view_conversation(current_user, conv):
        return None
    return conv


def _api_not_found():
    return json_error("not_found", "That conversation could not be found.", 404)


def _is_plain_form_post() -> bool:
    """A no-JavaScript HTML form submission (the browser wants a page, not JSON)."""
    if request.is_json:
        return False
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


def _validation_json(exc: ValidationError):
    details = {"field": exc.field} if exc.field else None
    return json_error("validation_error", exc.message, 400, details)


def _owner_open_reports(user) -> list[LostReport]:  # noqa: ANN001
    """The user's own reports that can still receive messages."""
    return list(
        db.session.scalars(
            sa.select(LostReport)
            .where(
                LostReport.owner_id == user.id,
                LostReport.status != ReportStatus.CLOSED,
                LostReport.is_hidden.is_(False),
            )
            .order_by(LostReport.created_at.desc())
        )
    )


def _guess_report_for_sighting(
    user, sighting_id: int | None, reports: list[LostReport]
):  # noqa: ANN001, ANN202
    """Which of the owner's reports a sighting was found for (scan source), else the only one."""
    if sighting_id is not None:
        report_id = db.session.scalar(
            sa.select(Scan.source_lost_report_id)
            .join(ScanResult, ScanResult.scan_id == Scan.id)
            .where(
                Scan.user_id == user.id,
                Scan.source_lost_report_id.is_not(None),
                ScanResult.candidate_type == CandidateType.SIGHTING,
                ScanResult.candidate_id == sighting_id,
            )
            .order_by(Scan.id.desc())
            .limit(1)
        )
        if report_id is not None and any(r.id == report_id for r in reports):
            return report_id
    if len(reports) == 1:
        return reports[0].id
    return None


def _report_card_context(report: LostReport | None) -> dict[str, Any]:
    from app.services import storage_service  # engine-owned

    photo = report.primary_photo if report is not None else None
    return {"report_photo_url": storage_service.photo_url(photo, "thumb") if photo else None}


# --- Pages ---------------------------------------------------------------------------------------


@bp.get("/messages")
@login_required
def inbox():
    """All conversations of the current user, newest activity first, with unread counts."""
    items = ms.inbox_items(current_user)
    return render_template("messages/inbox.html", conversations=items)


@bp.route("/messages/new", methods=["GET", "POST"])
@limiter.limit(config_limit("RATE_LIMIT_MESSAGES"), methods=["POST"])
@login_required
def new():
    """Start (or reopen) the conversation about ?lost_report_id= (optional &sighting_id=).
    GET shows a confirm/first-message form; POST creates it and redirects to the thread."""
    lost_report_id = parse_int(request.values.get("lost_report_id"))
    sighting_id = parse_int(request.values.get("sighting_id"))

    # An owner coming from a sighting result may not have said which case it is about.
    choices: list[LostReport] = []
    if lost_report_id is None:
        choices = _owner_open_reports(current_user) if sighting_id is not None else []
        lost_report_id = _guess_report_for_sighting(current_user, sighting_id, choices)
        if lost_report_id is None:
            if not choices:
                flash("Open a lost pet case first, then use its Message button.", "warning")
                return redirect(url_for("cases.dashboard"))
            return render_template(
                "messages/new.html", target=None, choices=choices, sighting_id=sighting_id
            )

    try:
        target = ms.resolve_target(current_user, lost_report_id, sighting_id)
    except ms.ConversationError as exc:
        if exc.status == 404:
            abort(404)
        flash(exc.message, "error")
        return redirect(url_for("cases.detail", report_id=lost_report_id))

    attaches_new_sighting = target.sighting is not None and (
        target.existing is None or target.existing.sighting_id is None
    )
    if target.existing is not None and not attaches_new_sighting and request.method == "GET":
        return redirect(url_for("messages.thread", conversation_id=target.existing.id))

    recipient = target.recipient_for(current_user)
    error = None
    body_value = request.form.get("body") if request.method == "POST" else None
    if request.method == "POST":
        try:
            body = clean_text(
                body_value,
                max_length=current_app.config["MESSAGE_MAX_LENGTH"],
                required=target.existing is None,
                field="body",
            )
            conv, created = ms.start_conversation(current_user, lost_report_id, sighting_id)
            if body:
                ms.send_message(conv, current_user, type=MessageType.TEXT, body=body)
            db.session.commit()
        except ValidationError as exc:
            db.session.rollback()
            error = exc.message
        except ms.ConversationError as exc:
            db.session.rollback()
            flash(exc.message, "error")
            return redirect(url_for("cases.detail", report_id=lost_report_id))
        else:
            if created:
                flash(f"Conversation started with {recipient.first_name}.", "success")
            return redirect(url_for("messages.thread", conversation_id=conv.id))

    return render_template(
        "messages/new.html",
        target=target,
        recipient=recipient,
        choices=choices,
        sighting_id=sighting_id,
        body_value=body_value,
        error=error,
        is_owner=current_user.id == target.report.owner_id,
        phone_note=PHONE_NOTE,
        **_report_card_context(target.report),
    )


@bp.get("/messages/<int:conversation_id>")
@login_required
def thread(conversation_id: int):
    """One conversation (participants only) with the verification checklist panel."""
    conv = _conversation_or_404(conversation_id)
    if ms.mark_read(conv, current_user):
        db.session.commit()
    report = conv.lost_report
    other = conv.other_participant(current_user)
    messages = [
        ms.serialize_message(m, current_user)
        for m in ms.messages_after(conv, None, limit=THREAD_PAGE_SIZE)
    ]
    sighting = conv.sighting
    show_sighting = sighting is not None and policy_service.can_view_sighting(
        current_user, sighting
    )
    return render_template(
        "messages/thread.html",
        conv=conv,
        report=report,
        report_visible=report is not None and policy_service.can_view_report(current_user, report),
        other=other,
        is_owner=current_user.id == conv.owner_id,
        sighting=sighting if show_sighting else None,
        messages=messages,
        last_id=messages[-1]["id"] if messages else 0,
        last_read_mine_id=ms.last_read_own_message_id(conv, current_user) or 0,
        can_send=ms.can_send(current_user, conv),
        read_only_reason=ms.read_only_reason(conv, current_user),
        compose=request.args.get("compose", ""),
        checklist=tuple(VERIFICATION_CHECKLIST) + EXTRA_CHECKLIST,
        phone_note=PHONE_NOTE,
        max_length=current_app.config["MESSAGE_MAX_LENGTH"],
        **_report_card_context(report),
    )


@bp.post("/messages/<int:conversation_id>/report")
@limiter.limit(config_limit("RATE_LIMIT_ABUSE_REPORTS"))
@login_required
def report_user(conversation_id: int):
    """ "Report this user" (field: reason) -> abuse_reports row."""
    conv = _conversation_or_404(conversation_id)
    try:
        ms.report_user(conv, current_user, request.form.get("reason"))
        db.session.commit()
    except ValidationError as exc:
        db.session.rollback()
        flash(f"Please tell us what happened: {exc.message}", "error")
    else:
        flash(
            "Thank you. An administrator will review this conversation. "
            "If you feel unsafe, stop replying and do not meet this person.",
            "success",
        )
    return redirect(url_for("messages.thread", conversation_id=conv.id))


# --- JSON API ------------------------------------------------------------------------------


@bp.get("/api/conversations")
@api_login_required
def api_list():
    """GET /api/conversations -> {"conversations": [...], "unread_total": n}."""
    items = ms.inbox_items(current_user)
    return json_ok({"conversations": items, "unread_total": sum(item["unread"] for item in items)})


@bp.post("/api/conversations")
@limiter.limit(config_limit("RATE_LIMIT_MESSAGES"))
@api_login_required
def api_create():
    """POST /api/conversations {"lost_report_id", "sighting_id"?, "body"?} ->
    201 {"id", "url", "created": true} (200 with "created": false when reused)."""
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    if parse_int(data.get("lost_report_id")) is None:
        return json_error(
            "validation_error", "lost_report_id is required.", 400, {"field": "lost_report_id"}
        )
    try:
        body = clean_text(
            data.get("body"), max_length=current_app.config["MESSAGE_MAX_LENGTH"], field="body"
        )
        conv, created = ms.start_conversation(
            current_user, data.get("lost_report_id"), data.get("sighting_id")
        )
        if body:
            ms.send_message(conv, current_user, type=MessageType.TEXT, body=body)
        db.session.commit()
    except ValidationError as exc:
        db.session.rollback()
        return _validation_json(exc)
    except ms.ConversationError as exc:
        db.session.rollback()
        return json_error(exc.code, exc.message, exc.status)
    url = url_for("messages.thread", conversation_id=conv.id)
    return json_ok({"id": conv.id, "url": url, "created": created}, 201 if created else 200)


@bp.get("/api/conversations/<int:conversation_id>/messages")
@api_login_required
def api_messages(conversation_id: int):
    """GET /api/conversations/<id>/messages?after=<message_id> -> {"messages": [...]}.

    Without ``after`` the latest messages are returned. The response also says
    whether the viewer may still write (``can_send``) and the newest own message
    the other person has read (``last_read_mine_id``, for "Seen" receipts).
    """
    conv = _api_conversation(conversation_id)
    if conv is None:
        return _api_not_found()
    after_raw = request.args.get("after")
    after = None
    if after_raw not in (None, ""):
        after = parse_int(after_raw)
        if after is None or after < 0:
            return json_error(
                "validation_error", "after must be a message id.", 400, {"field": "after"}
            )
    rows = ms.messages_after(conv, after, limit=API_PAGE_SIZE if after is not None else 100)
    return json_ok(
        {
            "messages": [ms.serialize_message(m, current_user) for m in rows],
            "last_read_mine_id": ms.last_read_own_message_id(conv, current_user) or 0,
            "can_send": ms.can_send(current_user, conv),
            "read_only_reason": ms.read_only_reason(conv, current_user),
        }
    )


@bp.post("/api/conversations/<int:conversation_id>/messages")
@limiter.limit(config_limit("RATE_LIMIT_MESSAGES"))
@api_login_required
def api_send(conversation_id: int):
    """POST /api/conversations/<id>/messages - JSON {"type": "text"|"location_pin", "body",
    "lat", "lng", "location_label"} or multipart with "photo" for images -> 201 message."""
    conv = _api_conversation(conversation_id)
    plain_form = _is_plain_form_post()
    if conv is None:
        if plain_form:
            abort(404)
        return _api_not_found()

    if request.is_json:
        data = request.get_json(silent=True) or {}
        photo = None
    else:
        data = request.form
        photo = request.files.get("photo")
    if photo is not None and not photo.filename:
        photo = None
    kind = data.get("type") or (MessageType.IMAGE if photo is not None else MessageType.TEXT)
    label = data.get("location_label")
    if label is None:
        label = data.get("place")  # the shared location_picker macro names it "place"

    try:
        message = ms.send_message(
            conv,
            current_user,
            type=str(kind),
            body=data.get("body"),
            lat=data.get("lat"),
            lng=data.get("lng"),
            location_label=label,
            photo=photo,
        )
        db.session.commit()
    except ValidationError as exc:
        db.session.rollback()
        if plain_form:
            flash(exc.message, "error")
            return redirect(url_for("messages.thread", conversation_id=conv.id))
        return _validation_json(exc)
    except ms.ConversationError as exc:
        db.session.rollback()
        if plain_form:
            flash(exc.message, "error")
            return redirect(url_for("messages.thread", conversation_id=conv.id))
        return json_error(exc.code, exc.message, exc.status)

    if plain_form:
        return redirect(url_for("messages.thread", conversation_id=conv.id) + "#latest")
    return json_ok({"message": ms.serialize_message(message, current_user)}, 201)


@bp.post("/api/conversations/<int:conversation_id>/read")
@api_login_required
def api_mark_read(conversation_id: int):
    """POST /api/conversations/<id>/read - mark the other participant's messages as read."""
    conv = _api_conversation(conversation_id)
    if conv is None:
        return _api_not_found()
    marked = ms.mark_read(conv, current_user)
    db.session.commit()
    return json_ok({"marked": marked, "unread_total": ms.unread_count(current_user)})
