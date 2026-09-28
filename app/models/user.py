"""Users, phone verification codes and FCM device tokens."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask_login import UserMixin
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import CreatedAtMixin, TimestampMixin, enum_type, table_args
from app.models.enums import UserRole

_hasher = PasswordHasher()  # argon2id with library defaults


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = table_args(sa.Index("ix_users_role", "role"))

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True, nullable=False)  # lower-case
    mobile_e164: Mapped[str | None] = mapped_column(sa.String(16), unique=True)
    mobile_verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        enum_type(UserRole.ALL, "user_role"), default=UserRole.USER, nullable=False
    )
    # Overrides UserMixin.is_active: Flask-Login refuses inactive users.
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    sms_opt_in: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    push_opt_in: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    # When the user accepted the Privacy Notice at registration (RA 10173 spirit).
    privacy_consented_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    avatar_photo_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("photos.id", ondelete="SET NULL"), index=True
    )
    avatar_photo = relationship("Photo", foreign_keys=[avatar_photo_id])

    lost_reports = relationship(
        "LostReport", back_populates="owner", foreign_keys="LostReport.owner_id"
    )
    sightings = relationship("Sighting", back_populates="finder", foreign_keys="Sighting.finder_id")
    device_tokens = relationship("DeviceToken", back_populates="user", cascade="all, delete-orphan")

    # --- passwords ---------------------------------------------------------
    def set_password(self, password: str) -> None:
        self.password_hash = _hasher.hash(password)

    def check_password(self, password: str) -> bool:
        """Verify ``password``; transparently upgrades outdated argon2 parameters."""
        if not self.password_hash:
            return False
        try:
            _hasher.verify(self.password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if _hasher.check_needs_rehash(self.password_hash):
            self.password_hash = _hasher.hash(password)
        return True

    # --- convenience -------------------------------------------------------
    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def first_name(self) -> str:
        return (self.full_name or "").split(" ")[0]

    @property
    def initial(self) -> str:
        """First letter of the name, shown in the avatar circle when there is no picture."""
        return (self.full_name or "?").strip()[:1].upper() or "?"

    @property
    def avatar_url(self) -> str | None:
        """URL of the 400x400 profile picture, or None to show the initial instead."""
        if self.avatar_photo is None:
            return None
        from app.services.storage_service import photo_url  # imported lazily to avoid a cycle

        return photo_url(self.avatar_photo, "thumb")

    @property
    def mobile_verified(self) -> bool:
        return self.mobile_e164 is not None and self.mobile_verified_at is not None

    @property
    def can_receive_sms(self) -> bool:
        """SMS alerts need opt-in AND a verified mobile number (SPEC 4, 9)."""
        return bool(self.is_active and self.sms_opt_in and self.mobile_verified)

    def set_mobile(self, e164: str | None) -> None:
        """Change the mobile number; a new number must be verified again."""
        if e164 != self.mobile_e164:
            self.mobile_e164 = e164
            self.mobile_verified_at = None

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email}>"


def dummy_password_check(password: str) -> None:
    """Spend the same time as a real check (login timing for unknown emails)."""
    try:
        _hasher.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


_DUMMY_HASH = _hasher.hash("pawbalik-dummy-password")


class PhoneVerification(CreatedAtMixin, db.Model):
    """One-time SMS codes (stored hashed)."""

    __tablename__ = "phone_verifications"
    __table_args__ = table_args()

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    mobile_e164: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    code_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    user = relationship("User")


class DeviceToken(CreatedAtMixin, db.Model):
    """A browser's FCM registration token."""

    __tablename__ = "device_tokens"
    __table_args__ = table_args()

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fcm_token: Mapped[str] = mapped_column(sa.String(512), unique=True, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    user = relationship("User", back_populates="device_tokens")
