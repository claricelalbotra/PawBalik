"""Private Pet Sightings (SPEC 5.4) and their access log."""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import Coordinate, TimestampMixin, enum_type, table_args
from app.models.enums import (
    AnimalCondition,
    ContactPref,
    PetSize,
    PhotoOwner,
    SightingStatus,
    Species,
)
from app.models.photo import Photo
from app.utils.time import utcnow


class Sighting(TimestampMixin, db.Model):
    """NEVER list publicly. Visibility: app.services.policy_service.can_view_sighting."""

    __tablename__ = "sightings"
    __table_args__ = table_args(
        sa.Index("ix_sightings_status", "status"),
        sa.Index("ix_sightings_species", "species"),
        sa.Index("ix_sightings_seen_lat_lng", "seen_lat", "seen_lng"),
        sa.Index("ix_sightings_pool", "status", "is_hidden", "species"),
        sa.Index("ix_sightings_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    finder_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)

    species: Mapped[str] = mapped_column(enum_type(Species.ALL, "species"), nullable=False)
    breed: Mapped[str | None] = mapped_column(sa.String(80))
    colors: Mapped[str | None] = mapped_column(sa.String(120))
    size: Mapped[str | None] = mapped_column(enum_type(PetSize.ALL, "pet_size"))
    condition: Mapped[str] = mapped_column(
        enum_type(AnimalCondition.ALL, "animal_condition"),
        default=AnimalCondition.UNKNOWN,
        nullable=False,
    )
    is_holding_animal: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(sa.Text)

    seen_lat: Mapped[float] = mapped_column(Coordinate, nullable=False)
    seen_lng: Mapped[float] = mapped_column(Coordinate, nullable=False)
    seen_place: Mapped[str | None] = mapped_column(sa.String(160))
    seen_at: Mapped[datetime] = mapped_column(sa.DateTime, nullable=False)  # naive UTC

    contact_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(sa.String(255))
    contact_phone: Mapped[str | None] = mapped_column(sa.String(16))  # E.164
    contact_pref: Mapped[str] = mapped_column(
        enum_type(ContactPref.ALL, "contact_pref"), default=ContactPref.IN_APP, nullable=False
    )

    status: Mapped[str] = mapped_column(
        enum_type(SightingStatus.ALL, "sighting_status"),
        default=SightingStatus.ACTIVE,
        nullable=False,
    )
    is_hidden: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    hidden_reason: Mapped[str | None] = mapped_column(sa.String(255))
    hidden_by: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    hidden_at: Mapped[datetime | None] = mapped_column(sa.DateTime)
    ai_attributes: Mapped[dict | None] = mapped_column(sa.JSON)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    finder = relationship("User", back_populates="sightings", foreign_keys=[finder_id])
    hidden_by_user = relationship("User", foreign_keys=[hidden_by])

    @property
    def photos(self) -> list[Photo]:
        return Photo.for_owner(PhotoOwner.SIGHTING, self.id)

    @property
    def primary_photo(self) -> Photo | None:
        return Photo.primary_for(PhotoOwner.SIGHTING, self.id)

    @property
    def is_searchable(self) -> bool:
        """In the Lost Pet Scanner pool (SPEC 6.4)."""
        return self.status == SightingStatus.ACTIVE and not self.is_hidden

    @property
    def shares_phone(self) -> bool:
        return self.contact_pref == ContactPref.PHONE and bool(self.contact_phone)

    def set_default_expiry(self, days: int) -> None:
        """expires_at = created (or now) + ``days`` (config SIGHTING_EXPIRY_DAYS)."""
        self.expires_at = (self.created_at or utcnow()) + timedelta(days=days)

    def __repr__(self) -> str:
        return f"<Sighting {self.id} {self.species} {self.status}>"


class SightingAccessLog(db.Model):
    """Every time an owner (or admin) opens a private sighting (SPEC 5.2)."""

    __tablename__ = "sighting_access_log"
    __table_args__ = table_args()

    id: Mapped[int] = mapped_column(primary_key=True)
    sighting_id: Mapped[int] = mapped_column(
        sa.ForeignKey("sightings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    viewer_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)
    scan_id: Mapped[int | None] = mapped_column(sa.ForeignKey("scans.id"), index=True)
    accessed_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow, nullable=False)

    sighting = relationship("Sighting")
    viewer = relationship("User")
