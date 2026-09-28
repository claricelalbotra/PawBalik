"""Case conversations, messages and abuse reports (SPEC 5.6)."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import Coordinate, CreatedAtMixin, enum_type, table_args
from app.models.enums import AbuseStatus, MessageType
from app.utils.time import utcnow


class Conversation(db.Model):
    """One conversation per (lost report, other user). Participants: the case
    owner and the other user. Access: app.services.policy_service.can_view_conversation."""

    __tablename__ = "conversations"
    __table_args__ = table_args(
        sa.UniqueConstraint("lost_report_id", "other_user_id"),
        sa.Index("ix_conversations_last_message_at", "last_message_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lost_report_id: Mapped[int] = mapped_column(
        sa.ForeignKey("lost_reports.id"), index=True, nullable=False
    )
    sighting_id: Mapped[int | None] = mapped_column(sa.ForeignKey("sightings.id"), index=True)
    owner_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)
    other_user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    lost_report = relationship("LostReport")
    sighting = relationship("Sighting")
    owner = relationship("User", foreign_keys=[owner_id])
    other_user = relationship("User", foreign_keys=[other_user_id])
    messages = relationship(
        "Message",
        back_populates="conversation",
        order_by="Message.id",
        cascade="all, delete-orphan",
    )

    @property
    def participant_ids(self) -> tuple[int, int]:
        return (self.owner_id, self.other_user_id)

    def is_participant(self, user) -> bool:  # noqa: ANN001
        return bool(user and getattr(user, "id", None) in self.participant_ids)

    def other_participant(self, user):  # noqa: ANN001, ANN201
        """The participant who is not ``user``."""
        return self.other_user if getattr(user, "id", None) == self.owner_id else self.owner

    def unread_count_for(self, user) -> int:  # noqa: ANN001
        return (
            db.session.scalar(
                sa.select(sa.func.count(Message.id)).where(
                    Message.conversation_id == self.id,
                    Message.read_at.is_(None),
                    sa.or_(Message.sender_id.is_(None), Message.sender_id != user.id),
                )
            )
            or 0
        )


class Message(CreatedAtMixin, db.Model):
    __tablename__ = "messages"
    __table_args__ = table_args(
        sa.Index("ix_messages_conversation_id_id", "conversation_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sender_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("users.id"), index=True
    )  # None = system
    type: Mapped[str] = mapped_column(
        enum_type(MessageType.ALL, "message_type"), default=MessageType.TEXT, nullable=False
    )
    body: Mapped[str | None] = mapped_column(sa.Text)
    lat: Mapped[float | None] = mapped_column(Coordinate)
    lng: Mapped[float | None] = mapped_column(Coordinate)
    location_label: Mapped[str | None] = mapped_column(sa.String(160))
    photo_id: Mapped[int | None] = mapped_column(sa.ForeignKey("photos.id"), index=True)
    read_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    conversation = relationship("Conversation", back_populates="messages")
    sender = relationship("User")
    photo = relationship("Photo")


class AbuseReport(CreatedAtMixin, db.Model):
    """ "Report this user" from a conversation."""

    __tablename__ = "abuse_reports"
    __table_args__ = table_args(sa.Index("ix_abuse_reports_status", "status"))

    id: Mapped[int] = mapped_column(primary_key=True)
    reporter_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)
    target_user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id"), index=True, nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("conversations.id"), index=True
    )
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_type(AbuseStatus.ALL, "abuse_status"), default=AbuseStatus.OPEN, nullable=False
    )

    reporter = relationship("User", foreign_keys=[reporter_id])
    target_user = relationship("User", foreign_keys=[target_user_id])
    conversation = relationship("Conversation")
