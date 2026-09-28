"""Case conversations and messages (SPEC 5.6).

OWNER: messages team. Several teams call the fixed functions below:

* ``get_or_create_conversation(lost_report, other_user, sighting=None)``
* ``post_system_message(conversation, body)`` (reports team: status changes)
* ``conversations_for_report(report)``
* ``send_message(conversation, sender, *, type, body, lat, lng, location_label, photo)``

Rules:

* A conversation belongs to one Lost Pet Report (optionally one Sighting).
  Participants are the report owner and ONE other user; one conversation per
  (report, other user). Nobody can message themselves.
* Who may start one (``start_conversation``):
    - a logged-in non-owner about a visible, not-closed report (they become
      the "other user"); they may attach a sighting only if they found it;
    - the report owner about a sighting ``policy_service.can_view_sighting``
      allows (the finder becomes the "other user").
* Closed cases are read-only; hidden (moderated) cases accept no new messages.
* Every new message notifies the recipient (``notify(..., "new_message", ...)``).
  Phone numbers are never added to messages automatically.

Service functions flush but never commit - the calling route commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from flask import current_app, has_request_context, url_for

from app.extensions import db
from app.models import (
    AbuseReport,
    AbuseStatus,
    Conversation,
    LostReport,
    Message,
    MessageType,
    Photo,
    PhotoOwner,
    ReportStatus,
    Sighting,
    User,
)
from app.services import policy_service
from app.utils.filters import format_date, format_time, timeago
from app.utils.maps import directions_url, view_url
from app.utils.time import utcnow
from app.utils.validators import ValidationError, clean_text, parse_coordinates

LOCATION_LABEL_MAX = 160
ABUSE_REASON_MAX = 1000
PREVIEW_LENGTH = 80
# Message types a user may send (system messages come from post_system_message only).
USER_MESSAGE_TYPES = (MessageType.TEXT, MessageType.LOCATION_PIN, MessageType.IMAGE)


class ConversationError(ValueError):
    """The conversation cannot be created or used.

    ``code`` / ``status`` map straight onto the SPEC JSON error shape, e.g.
    ``ConversationError("You cannot message yourself.", "self_conversation", 400)``.
    """

    def __init__(self, message: str, code: str = "conversation_error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# --- Conversations -------------------------------------------------------------------------


def thread_url(conversation_id: int) -> str:
    """Relative URL of a thread; also works outside a request (worker, seed, CLI)."""
    if has_request_context():
        return url_for("messages.thread", conversation_id=conversation_id)
    return f"/messages/{conversation_id}"


def get_or_create_conversation(
    lost_report: LostReport, other_user: User, sighting: Sighting | None = None
) -> Conversation:
    """Return the conversation for (report, other_user), creating it if needed (flushes).

    ``sighting`` (optional) must belong to one of the two participants; it is
    attached to an existing conversation that has none yet.
    Raises :class:`ConversationError` for self-conversations or a foreign sighting.
    """
    if other_user.id == lost_report.owner_id:
        raise ConversationError(
            "You cannot start a conversation about your own report.", "self_conversation"
        )
    if sighting is not None and sighting.finder_id not in (lost_report.owner_id, other_user.id):
        raise ConversationError(
            "Only the finder can attach their own sighting.", "sighting_not_allowed", 403
        )
    conv = db.session.scalar(
        sa.select(Conversation).where(
            Conversation.lost_report_id == lost_report.id,
            Conversation.other_user_id == other_user.id,
        )
    )
    if conv is None:
        conv = Conversation(
            lost_report_id=lost_report.id,
            sighting_id=sighting.id if sighting is not None else None,
            owner_id=lost_report.owner_id,
            other_user_id=other_user.id,
            created_at=utcnow(),
        )
        db.session.add(conv)
        db.session.flush()
    elif sighting is not None and conv.sighting_id is None:
        conv.sighting_id = sighting.id
        db.session.flush()
    return conv


def find_conversation(lost_report: LostReport, other_user: User) -> Conversation | None:
    """The existing conversation for (report, other_user), or None (no side effects)."""
    return db.session.scalar(
        sa.select(Conversation).where(
            Conversation.lost_report_id == lost_report.id,
            Conversation.other_user_id == other_user.id,
        )
    )


@dataclass
class ConversationTarget:
    """Who a new conversation would be with (resolved by :func:`resolve_target`)."""

    report: LostReport
    other_user: User  # the non-owner participant
    sighting: Sighting | None
    existing: Conversation | None

    def recipient_for(self, user: User) -> User:
        """The person ``user`` would be talking to."""
        return self.report.owner if user.id == self.other_user.id else self.other_user


def _load_report(lost_report_id: Any) -> LostReport | None:
    try:
        report_id = int(lost_report_id)
    except (TypeError, ValueError):
        return None
    return db.session.get(LostReport, report_id)


def _load_sighting(sighting_id: Any) -> Sighting | None:
    try:
        sid = int(sighting_id)
    except (TypeError, ValueError):
        return None
    return db.session.get(Sighting, sid)


def resolve_target(user: User, lost_report_id: Any, sighting_id: Any = None) -> ConversationTarget:
    """Check every rule for starting a conversation, without writing anything.

    Raises :class:`ConversationError` (``code``/``status`` suitable for JSON errors).
    Things the user may not see answer ``not_found`` so their existence is not revealed.
    """
    report = _load_report(lost_report_id)
    if report is None or not policy_service.can_view_report(user, report):
        raise ConversationError("That case could not be found.", "not_found", 404)
    if report.is_hidden:
        raise ConversationError(
            "This case is under review by a moderator and cannot receive messages.",
            "case_hidden",
            403,
        )
    if report.status == ReportStatus.CLOSED:
        raise ConversationError(
            "This case is closed, so new conversations cannot be started.", "case_closed", 403
        )

    sighting = None
    if sighting_id not in (None, ""):
        sighting = _load_sighting(sighting_id)
        if sighting is None or not policy_service.can_view_sighting(user, sighting):
            raise ConversationError("That sighting could not be found.", "not_found", 404)
        if sighting.is_hidden:  # only its finder (or an admin) can still see it
            raise ConversationError(
                "This sighting is under review by a moderator and cannot be shared.",
                "sighting_hidden",
                403,
            )

    if user.id == report.owner_id:
        # The owner contacts the finder of a sighting their scan (or a shared chat) surfaced.
        if sighting is None:
            raise ConversationError(
                "You cannot start a conversation about your own report.", "self_conversation"
            )
        if sighting.finder_id == user.id:
            raise ConversationError(
                "This sighting is yours - there is nobody else to message.", "self_conversation"
            )
        other_user = sighting.finder
    else:
        # A finder / citizen contacts the owner; they may attach only their own sighting.
        if sighting is not None and sighting.finder_id != user.id:
            raise ConversationError(
                "You can only attach a sighting that you submitted.", "sighting_not_allowed", 403
            )
        other_user = user

    counterpart = report.owner if other_user.id == user.id else other_user
    if counterpart is None or not counterpart.is_active:
        raise ConversationError(
            "This person's account is not active, so they cannot receive messages.",
            "recipient_inactive",
            403,
        )
    return ConversationTarget(
        report=report,
        other_user=other_user,
        sighting=sighting,
        existing=find_conversation(report, other_user),
    )


def start_conversation(
    user: User, lost_report_id: Any, sighting_id: Any = None
) -> tuple[Conversation, bool]:
    """Start or reuse the conversation ``user`` asks for. Returns (conversation, created).

    Reusing an existing conversation is allowed even if nothing new is attached.
    Raises :class:`ConversationError`. Flushes; the caller commits.
    """
    target = resolve_target(user, lost_report_id, sighting_id)
    created = target.existing is None
    conv = get_or_create_conversation(target.report, target.other_user, target.sighting)
    return conv, created


def post_system_message(conversation: Conversation, body: str) -> Message:
    """Add a ``system`` message (no sender), e.g. "Case marked as Found". Flushes."""
    now = utcnow()
    message = Message(
        conversation_id=conversation.id,
        sender_id=None,
        type=MessageType.SYSTEM,
        body=(body or "").strip()[: current_app.config.get("MESSAGE_MAX_LENGTH", 2000)],
        created_at=now,
    )
    db.session.add(message)
    conversation.last_message_at = now
    db.session.flush()
    return message


def conversations_for_report(report: LostReport) -> list[Conversation]:
    """All conversations about one report, most recent activity first."""
    return list(
        db.session.scalars(
            sa.select(Conversation)
            .where(Conversation.lost_report_id == report.id)
            .order_by(
                sa.func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
                Conversation.id.desc(),
            )
        )
    )


def conversations_for_user(user: User) -> list[Conversation]:
    """Every conversation ``user`` takes part in, most recent activity first."""
    return list(
        db.session.scalars(
            sa.select(Conversation)
            .where(sa.or_(Conversation.owner_id == user.id, Conversation.other_user_id == user.id))
            .order_by(
                sa.func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
                Conversation.id.desc(),
            )
        )
    )


def is_read_only(conversation: Conversation) -> bool:
    """Closed or hidden cases accept no new user messages."""
    report = conversation.lost_report
    return report is None or report.is_hidden or report.status == ReportStatus.CLOSED


def read_only_reason(conversation: Conversation, user: User) -> str | None:
    """Why ``user`` cannot write here (shown instead of the composer), or None."""
    report = conversation.lost_report
    if report is None or report.is_hidden:
        return "This case is under review by a moderator, so new messages are paused."
    if report.status == ReportStatus.CLOSED:
        return "This case is closed. You can still read the conversation, but not reply."
    other = conversation.other_participant(user)
    if other is None or not other.is_active:
        return "The other person's account is no longer active."
    return None


def can_send(user: User, conversation: Conversation) -> bool:
    """Participant, case open and visible, both accounts active."""
    return policy_service.can_post_in_conversation(user, conversation) and not is_read_only(
        conversation
    )


# --- Messages --------------------------------------------------------------------------------


def _max_length() -> int:
    return int(current_app.config.get("MESSAGE_MAX_LENGTH", 2000))


def _save_message_photo(conversation: Conversation, photo: Any) -> Photo:
    """Accept an existing message Photo of this conversation, or an upload to store."""
    from app.services import storage_service  # engine-owned

    if isinstance(photo, Photo):
        if photo.owner_type != PhotoOwner.MESSAGE or photo.owner_id != conversation.id:
            raise ValidationError("That photo does not belong to this conversation.", "photo")
        return photo
    if photo is None or (hasattr(photo, "filename") and not photo.filename):
        raise ValidationError("Please choose a photo to send.", "photo")
    try:
        return storage_service.save_image(
            photo,
            owner_type=PhotoOwner.MESSAGE,
            owner_id=conversation.id,
            folder=storage_service.default_folder(PhotoOwner.MESSAGE, conversation.id),
        )
    except storage_service.ImageValidationError as exc:
        raise ValidationError(exc.message, "photo") from None


def send_message(
    conversation: Conversation,
    sender: User,
    *,
    type: str = MessageType.TEXT,  # noqa: A002 - name fixed by the contract
    body: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    location_label: str | None = None,
    photo: Any = None,
) -> Message:
    """Validate and store a text / location_pin / image message, bump ``last_message_at``
    and ``notify(recipient, "new_message", ...)``. Flushes; the caller commits.

    * ``text``: ``body`` required (<= MESSAGE_MAX_LENGTH).
    * ``location_pin``: ``lat``/``lng`` required and inside the service area;
      optional ``location_label`` (<= 160) and ``body`` note.
    * ``image``: ``photo`` is an upload (FileStorage / bytes) saved with
      ``storage_service.save_image(owner_type="message", owner_id=conversation.id)``
      or an already-saved message Photo of this conversation; optional caption ``body``.

    Raises ``ValidationError`` (bad input) or :class:`ConversationError` (not allowed).
    """
    if not conversation.is_participant(sender):
        raise ConversationError("You are not part of this conversation.", "not_found", 404)
    if not can_send(sender, conversation):
        raise ConversationError(
            read_only_reason(conversation, sender) or "You cannot send messages here.",
            "read_only",
            403,
        )
    kind = (type or "").strip().lower()
    if kind not in USER_MESSAGE_TYPES:
        raise ValidationError("Unknown message type.", "type")

    message = Message(conversation_id=conversation.id, sender_id=sender.id, type=kind)
    if kind == MessageType.TEXT:
        message.body = clean_text(body, max_length=_max_length(), required=True, field="body")
    elif kind == MessageType.LOCATION_PIN:
        message.lat, message.lng = parse_coordinates(lat, lng, require_service_area=True)
        message.location_label = clean_text(
            location_label, max_length=LOCATION_LABEL_MAX, field="location_label"
        )
        message.body = clean_text(body, max_length=_max_length(), field="body")
    else:  # image
        caption = clean_text(body, max_length=_max_length(), field="body")
        saved = _save_message_photo(conversation, photo)
        message.photo_id = saved.id
        message.body = caption

    now = utcnow()
    message.created_at = now
    db.session.add(message)
    conversation.last_message_at = now
    db.session.flush()
    _notify_recipient(conversation, sender, message)
    return message


def _notify_recipient(conversation: Conversation, sender: User, message: Message) -> None:
    """In-app + push for the other participant. The notification never quotes the
    message (it may contain a location or phone number) - only who wrote about which case."""
    from app.services.notification_service import notify  # notifications-owned

    recipient = conversation.other_participant(sender)
    if recipient is None or not recipient.is_active:
        return
    report = conversation.lost_report
    about = f"{report.pet_name} ({report.case_number})" if report else "a case"
    notify(
        recipient,
        "new_message",
        {
            "conversation_id": conversation.id,
            "message_id": message.id,
            "sender_name": sender.first_name,
            "link_url": thread_url(conversation.id),
            "title": f"New message from {sender.first_name}",
            "body": f"{sender.first_name} sent you a message about {about}.",
        },
    )


def messages_after(
    conversation: Conversation, after_id: int | None = None, *, limit: int = 100
) -> list[Message]:
    """Messages with ``id > after_id`` (oldest first). Without ``after_id``: the latest ``limit``."""
    stmt = sa.select(Message).where(Message.conversation_id == conversation.id)
    if after_id is not None:
        rows = db.session.scalars(
            stmt.where(Message.id > after_id).order_by(Message.id.asc()).limit(limit)
        )
        return list(rows)
    rows = list(db.session.scalars(stmt.order_by(Message.id.desc()).limit(limit)))
    rows.reverse()
    return rows


def mark_read(conversation: Conversation, user: User) -> int:
    """Mark every unread message ``user`` did not send (system messages too) as read.

    Returns the number of messages updated. Does not commit.
    """
    result = db.session.execute(
        sa.update(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.read_at.is_(None),
            sa.or_(Message.sender_id.is_(None), Message.sender_id != user.id),
        )
        .values(read_at=utcnow())
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount or 0)


def last_read_own_message_id(conversation: Conversation, user: User) -> int | None:
    """Id of the newest message ``user`` sent that the other person has read (read receipt)."""
    return db.session.scalar(
        sa.select(sa.func.max(Message.id)).where(
            Message.conversation_id == conversation.id,
            Message.sender_id == user.id,
            Message.read_at.is_not(None),
        )
    )


def unread_count(user: User) -> int:
    """Unread messages across all of ``user``'s conversations."""
    return sum(unread_counts_by_conversation(user).values())


def unread_counts_by_conversation(user: User) -> dict[int, int]:
    """{conversation_id: unread messages for ``user``} (conversations with 0 are omitted)."""
    rows = db.session.execute(
        sa.select(Message.conversation_id, sa.func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            sa.or_(Conversation.owner_id == user.id, Conversation.other_user_id == user.id),
            Message.read_at.is_(None),
            sa.or_(Message.sender_id.is_(None), Message.sender_id != user.id),
        )
        .group_by(Message.conversation_id)
    )
    return {conv_id: int(count) for conv_id, count in rows}


def last_messages(conversation_ids: list[int]) -> dict[int, Message]:
    """{conversation_id: newest message} in one query."""
    if not conversation_ids:
        return {}
    newest = (
        sa.select(sa.func.max(Message.id))
        .where(Message.conversation_id.in_(conversation_ids))
        .group_by(Message.conversation_id)
    )
    rows = db.session.scalars(sa.select(Message).where(Message.id.in_(newest)))
    return {m.conversation_id: m for m in rows}


# --- Abuse reports ----------------------------------------------------------------------------


def report_user(conversation: Conversation, reporter: User, reason: str | None) -> AbuseReport:
    """ "Report this user": an ``abuse_reports`` row against the other participant + audit log.

    If the reporter already has an open report for this conversation, that report
    is returned with the new reason appended (no duplicates for moderators).
    Raises ``ValidationError`` / :class:`ConversationError`. Does not commit.
    """
    from app.services.audit_service import log_event

    if not conversation.is_participant(reporter):
        raise ConversationError("You are not part of this conversation.", "not_found", 404)
    text = clean_text(reason, max_length=ABUSE_REASON_MAX, required=True, field="reason")
    target = conversation.other_participant(reporter)
    existing = db.session.scalar(
        sa.select(AbuseReport).where(
            AbuseReport.conversation_id == conversation.id,
            AbuseReport.reporter_id == reporter.id,
            AbuseReport.status == AbuseStatus.OPEN,
        )
    )
    if existing is not None:
        existing.reason = f"{existing.reason}\n---\n{text}"[: ABUSE_REASON_MAX * 3]
        report = existing
    else:
        report = AbuseReport(
            reporter_id=reporter.id,
            target_user_id=target.id,
            conversation_id=conversation.id,
            reason=text,
            status=AbuseStatus.OPEN,
        )
        db.session.add(report)
    db.session.flush()
    log_event(
        "abuse.reported",
        actor=reporter,
        target=report,
        details={"conversation_id": conversation.id, "target_user_id": target.id},
    )
    return report


# --- Presentation helpers (JSON + templates) -----------------------------------------------


def preview(message: Message | None, viewer: User | None = None) -> str:
    """One-line summary for the inbox ("You: On my way", "Shared a location pin", ...)."""
    if message is None:
        return "No messages yet"
    if message.type == MessageType.LOCATION_PIN:
        text = "Shared a location pin" + (
            f": {message.location_label}" if message.location_label else ""
        )
    elif message.type == MessageType.IMAGE:
        text = "Sent a photo"
    else:
        text = " ".join((message.body or "").split())
    if len(text) > PREVIEW_LENGTH:
        text = text[: PREVIEW_LENGTH - 1].rstrip() + "…"
    if viewer is not None and message.sender_id == viewer.id:
        text = f"You: {text}"
    return text


def _iso_utc(value: Any) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value is not None else None


def serialize_message(message: Message, viewer: User) -> dict[str, Any]:
    """JSON shape used by the API and by static/js/messages.js."""
    from app.services import storage_service  # engine-owned

    data: dict[str, Any] = {
        "id": message.id,
        "type": message.type,
        "body": message.body,
        "mine": message.sender_id is not None and message.sender_id == viewer.id,
        "sender_id": message.sender_id,
        "sender_name": message.sender.first_name if message.sender is not None else None,
        "created_at": _iso_utc(message.created_at),
        "time_label": format_time(message.created_at),
        "date_label": format_date(message.created_at),
        "read": message.read_at is not None,
    }
    if message.type == MessageType.LOCATION_PIN and message.lat is not None:
        data["location"] = {
            "lat": float(message.lat),
            "lng": float(message.lng),
            "label": message.location_label,
            "view_url": view_url(message.lat, message.lng),
            "directions_url": directions_url(message.lat, message.lng),
        }
    if message.type == MessageType.IMAGE and message.photo is not None:
        data["photo"] = {
            "thumb_url": storage_service.photo_url(message.photo, "thumb"),
            "full_url": storage_service.photo_url(message.photo, "full"),
            "width": message.photo.width,
            "height": message.photo.height,
        }
    return data


def serialize_conversation(
    conversation: Conversation,
    viewer: User,
    *,
    last_message: Message | None = None,
    unread: int | None = None,
) -> dict[str, Any]:
    """Inbox row / API item for one conversation, from ``viewer``'s point of view."""
    from app.services import storage_service  # engine-owned

    report = conversation.lost_report
    other = conversation.other_participant(viewer)
    photo = report.primary_photo if report is not None else None
    activity = conversation.last_message_at or conversation.created_at
    return {
        "id": conversation.id,
        "url": thread_url(conversation.id),
        "lost_report_id": conversation.lost_report_id,
        "sighting_id": conversation.sighting_id,
        "case_number": report.case_number if report else None,
        "pet_name": report.pet_name if report else None,
        "status": report.status if report else None,
        "photo_url": storage_service.photo_url(photo, "thumb") if photo else None,
        "role": "owner" if viewer.id == conversation.owner_id else "other",
        "other_name": other.full_name if other is not None else "Former user",
        "other_initial": other.initial if other is not None else "?",
        "other_avatar_url": other.avatar_url if other is not None else None,
        "last_message_preview": preview(last_message, viewer),
        "last_message_at": _iso_utc(conversation.last_message_at),
        "activity_label": timeago(activity),
        "unread": int(unread if unread is not None else conversation.unread_count_for(viewer)),
        "read_only": is_read_only(conversation),
    }


def inbox_items(user: User) -> list[dict[str, Any]]:
    """Serialized conversations for the inbox page / GET /api/conversations (3 queries + rows)."""
    conversations = conversations_for_user(user)
    ids = [c.id for c in conversations]
    lasts = last_messages(ids)
    unread = unread_counts_by_conversation(user)
    return [
        serialize_conversation(c, user, last_message=lasts.get(c.id), unread=unread.get(c.id, 0))
        for c in conversations
    ]
