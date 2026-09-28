"""Lost Pet Reports, their status history and the case-number counter."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import (
    Coordinate,
    TimestampMixin,
    enum_type,
    table_args,
)
from app.models.enums import PetSex, PetSize, PhotoOwner, ReportStatus, Species
from app.models.photo import Photo
from app.utils.time import utcnow


class InvalidStatusTransition(ValueError):
    """Raised when a Lost Pet Report status change is not allowed."""


def allowed_transitions(from_status: str) -> tuple[str, ...]:
    """Statuses reachable from ``from_status`` (pure function, SPEC 5.1)."""
    return ReportStatus.TRANSITIONS.get(from_status, ())


def can_transition(from_status: str, to_status: str) -> bool:
    """``lost -> found -> closed`` and ``lost -> closed`` only."""
    return to_status in allowed_transitions(from_status)


class CaseCounter(db.Model):
    """One row per year; ``last_value`` is the last issued case sequence."""

    __tablename__ = "case_counters"
    __table_args__ = table_args()

    year: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=False)
    last_value: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)


class LostReport(TimestampMixin, db.Model):
    __tablename__ = "lost_reports"
    __table_args__ = table_args(
        sa.Index("ix_lost_reports_status", "status"),
        sa.Index("ix_lost_reports_species", "species"),
        sa.Index("ix_lost_reports_last_seen_lat_lng", "last_seen_lat", "last_seen_lng"),
        sa.Index("ix_lost_reports_pool", "status", "is_hidden", "species"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_number: Mapped[str] = mapped_column(sa.String(20), unique=True, nullable=False)
    owner_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)

    pet_name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    species: Mapped[str] = mapped_column(enum_type(Species.ALL, "species"), nullable=False)
    breed: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    colors: Mapped[str] = mapped_column(sa.String(120), nullable=False)  # colour(s)/pattern text
    size: Mapped[str] = mapped_column(enum_type(PetSize.ALL, "pet_size"), nullable=False)
    sex: Mapped[str] = mapped_column(
        enum_type(PetSex.ALL, "pet_sex"), default=PetSex.UNKNOWN, nullable=False
    )
    age_text: Mapped[str | None] = mapped_column(sa.String(40))
    identifying_features: Mapped[str | None] = mapped_column(sa.Text)

    last_seen_lat: Mapped[float] = mapped_column(Coordinate, nullable=False)
    last_seen_lng: Mapped[float] = mapped_column(Coordinate, nullable=False)
    last_seen_place: Mapped[str | None] = mapped_column(sa.String(160))  # barangay / landmark
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime, nullable=False)  # naive UTC

    contact_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(sa.String(255))
    contact_phone: Mapped[str | None] = mapped_column(sa.String(16))  # E.164
    show_phone: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    reward_amount: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2))  # PHP
    reward_text: Mapped[str | None] = mapped_column(sa.String(160))

    status: Mapped[str] = mapped_column(
        enum_type(ReportStatus.ALL, "report_status"), default=ReportStatus.LOST, nullable=False
    )
    is_hidden: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    hidden_reason: Mapped[str | None] = mapped_column(sa.String(255))
    hidden_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    hidden_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    ai_attributes: Mapped[dict | None] = mapped_column(sa.JSON)  # SPEC 6.3 of the primary photo

    owner = relationship("User", back_populates="lost_reports", foreign_keys=[owner_id])
    hidden_by_user = relationship("User", foreign_keys=[hidden_by])
    status_history = relationship(
        "CaseStatusHistory",
        back_populates="lost_report",
        order_by="CaseStatusHistory.changed_at, CaseStatusHistory.id",
        cascade="all, delete-orphan",
    )

    # --- photos ------------------------------------------------------------
    @property
    def photos(self) -> list[Photo]:
        """All photos, primary first (queried fresh each call)."""
        return Photo.for_owner(PhotoOwner.LOST_REPORT, self.id)

    @property
    def primary_photo(self) -> Photo | None:
        return Photo.primary_for(PhotoOwner.LOST_REPORT, self.id)

    # --- status ------------------------------------------------------------
    @property
    def is_public(self) -> bool:
        """Listed publicly (any status) unless hidden by a moderator."""
        return not self.is_hidden

    @property
    def is_searchable(self) -> bool:
        """In the Found Pet Scanner pool (SPEC 6.4)."""
        return self.status == ReportStatus.LOST and not self.is_hidden

    def can_transition_to(self, to_status: str) -> bool:
        return can_transition(self.status, to_status)

    def change_status(self, to_status: str, *, actor, note: str | None = None):  # noqa: ANN001
        """Change status and append a ``CaseStatusHistory`` row (caller commits).

        Raises ``InvalidStatusTransition`` if the move is not allowed.
        """
        if not self.can_transition_to(to_status):
            raise InvalidStatusTransition(
                f"Cannot change status from {self.status} to {to_status}."
            )
        entry = CaseStatusHistory(
            from_status=self.status,
            to_status=to_status,
            changed_by=getattr(actor, "id", actor),
            note=note,
            changed_at=utcnow(),
        )
        self.status = to_status
        self.status_history.append(entry)
        return entry

    def record_created(self, *, actor) -> CaseStatusHistory:  # noqa: ANN001
        """Add the initial ``None -> lost`` history row when a report is filed."""
        entry = CaseStatusHistory(
            from_status=None,
            to_status=self.status,
            changed_by=getattr(actor, "id", actor),
            note="Report filed",
            changed_at=utcnow(),
        )
        self.status_history.append(entry)
        return entry

    @property
    def has_location(self) -> bool:
        return self.last_seen_lat is not None and self.last_seen_lng is not None

    def __repr__(self) -> str:
        return f"<LostReport {self.case_number} {self.status}>"


class CaseStatusHistory(db.Model):
    __tablename__ = "case_status_history"
    __table_args__ = table_args()

    id: Mapped[int] = mapped_column(primary_key=True)
    lost_report_id: Mapped[int] = mapped_column(
        sa.ForeignKey("lost_reports.id", ondelete="CASCADE"), index=True, nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(enum_type(ReportStatus.ALL, "report_status"))
    to_status: Mapped[str] = mapped_column(
        enum_type(ReportStatus.ALL, "report_status"), nullable=False
    )
    changed_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    note: Mapped[str | None] = mapped_column(sa.String(255))
    changed_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow, nullable=False)

    lost_report = relationship("LostReport", back_populates="status_history")
    actor = relationship("User")
