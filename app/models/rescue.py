"""Rescue directory (rescuers, vets, shelters) and rescue-help requests (SPEC 5.8)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import Coordinate, CreatedAtMixin, TimestampMixin, enum_type, table_args
from app.models.enums import ProviderType


class AssistanceProvider(TimestampMixin, db.Model):
    __tablename__ = "assistance_providers"
    __table_args__ = table_args(
        sa.Index("ix_assistance_providers_type", "type"),
        sa.Index("ix_assistance_providers_lat_lng", "lat", "lng"),
        sa.Index("ix_assistance_providers_is_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    type: Mapped[str] = mapped_column(enum_type(ProviderType.ALL, "provider_type"), nullable=False)
    address: Mapped[str | None] = mapped_column(sa.String(255))
    barangay: Mapped[str | None] = mapped_column(sa.String(120))
    lat: Mapped[float | None] = mapped_column(Coordinate)
    lng: Mapped[float | None] = mapped_column(Coordinate)
    phones: Mapped[str | None] = mapped_column(sa.String(255))  # comma-separated
    facebook_url: Mapped[str | None] = mapped_column(sa.String(255))
    hours_text: Mapped[str | None] = mapped_column(sa.String(255))
    is_24_7: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    services: Mapped[str | None] = mapped_column(sa.Text)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    is_sample: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    @property
    def phone_list(self) -> list[str]:
        return [p.strip() for p in (self.phones or "").split(",") if p.strip()]


class RescueRequest(CreatedAtMixin, db.Model):
    __tablename__ = "rescue_requests"
    __table_args__ = table_args()

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)
    lost_report_id: Mapped[int | None] = mapped_column(sa.ForeignKey("lost_reports.id"), index=True)
    sighting_id: Mapped[int | None] = mapped_column(sa.ForeignKey("sightings.id"), index=True)
    provider_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("assistance_providers.id"), index=True
    )
    summary_text: Mapped[str] = mapped_column(sa.Text, nullable=False)

    user = relationship("User")
    lost_report = relationship("LostReport")
    sighting = relationship("Sighting")
    provider = relationship("AssistanceProvider")
