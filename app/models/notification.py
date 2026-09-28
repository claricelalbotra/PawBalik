"""In-app notifications and SMS / push delivery logs (SPEC 9)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import CreatedAtMixin, table_args


class Notification(CreatedAtMixin, db.Model):
    __tablename__ = "notifications"
    __table_args__ = table_args(sa.Index("ix_notifications_user_id_is_read", "user_id", "is_read"))

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(sa.String(40), nullable=False)  # NotificationEvent value
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    body: Mapped[str | None] = mapped_column(sa.String(500))
    link_url: Mapped[str | None] = mapped_column(sa.String(512))  # relative URL inside the app
    is_read: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    user = relationship("User")


class SmsLog(CreatedAtMixin, db.Model):
    __tablename__ = "sms_log"
    __table_args__ = table_args(sa.Index("ix_sms_log_created_at", "created_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    to_number: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    message: Mapped[str] = mapped_column(sa.String(640), nullable=False)
    purpose: Mapped[str] = mapped_column(sa.String(30), nullable=False)  # SmsPurpose value
    provider: Mapped[str] = mapped_column(sa.String(30), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(100))
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False)  # sent | failed | skipped
    error: Mapped[str | None] = mapped_column(sa.Text)

    user = relationship("User")


class PushLog(CreatedAtMixin, db.Model):
    __tablename__ = "push_log"
    __table_args__ = table_args(sa.Index("ix_push_log_created_at", "created_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    token_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("device_tokens.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False)  # sent | failed | skipped
    error: Mapped[str | None] = mapped_column(sa.Text)

    user = relationship("User")
