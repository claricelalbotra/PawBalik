"""Scans (manual and background matching runs), their results and the Gemini cache."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import Coordinate, CreatedAtMixin, enum_type, table_args
from app.models.enums import CandidateType, MatchTier, ScanMode, ScanStatus, ScanTrigger


class Scan(CreatedAtMixin, db.Model):
    """One matching run. Background runs have trigger='background' and
    user_id = the owner being notified (SPEC 17.6), so sighting visibility is
    always derived from a scan result that the owner owns.

    ``query_photo_id`` points at the scanned photo (owner_type 'scan') or, for
    background runs, at the report's/sighting's primary photo.
    """

    __tablename__ = "scans"
    __table_args__ = table_args(
        sa.Index("ix_scans_status", "status"),
        sa.Index("ix_scans_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True, nullable=False)
    mode: Mapped[str] = mapped_column(enum_type(ScanMode.ALL, "scan_mode"), nullable=False)
    trigger: Mapped[str] = mapped_column(
        enum_type(ScanTrigger.ALL, "scan_trigger"), default=ScanTrigger.MANUAL, nullable=False
    )
    query_photo_id: Mapped[int | None] = mapped_column(sa.ForeignKey("photos.id"), index=True)
    source_lost_report_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("lost_reports.id"), index=True
    )
    source_sighting_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("sightings.id"), index=True
    )
    scan_lat: Mapped[float | None] = mapped_column(Coordinate)
    scan_lng: Mapped[float | None] = mapped_column(Coordinate)
    precheck: Mapped[dict | None] = mapped_column(sa.JSON)  # SPEC 6.2
    attributes: Mapped[dict | None] = mapped_column(sa.JSON)  # SPEC 6.3
    status: Mapped[str] = mapped_column(
        enum_type(ScanStatus.ALL, "scan_status"), default=ScanStatus.PENDING, nullable=False
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    result_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    gemini_ok: Mapped[bool | None] = mapped_column(sa.Boolean)  # False = coarse-only fallback
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    user = relationship("User")
    query_photo = relationship("Photo", foreign_keys=[query_photo_id])
    source_lost_report = relationship("LostReport", foreign_keys=[source_lost_report_id])
    source_sighting = relationship("Sighting", foreign_keys=[source_sighting_id])
    results = relationship(
        "ScanResult",
        back_populates="scan",
        order_by="ScanResult.rank, ScanResult.id",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Scan {self.id} {self.mode}/{self.trigger} {self.status}>"


class ScanResult(CreatedAtMixin, db.Model):
    __tablename__ = "scan_results"
    __table_args__ = table_args(
        sa.UniqueConstraint("scan_id", "candidate_type", "candidate_id"),  # one row per candidate
        sa.Index("ix_scan_results_candidate", "candidate_type", "candidate_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(
        sa.ForeignKey("scans.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_type: Mapped[str] = mapped_column(
        enum_type(CandidateType.ALL, "candidate_type"), nullable=False
    )
    candidate_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    candidate_photo_id: Mapped[int | None] = mapped_column(sa.ForeignKey("photos.id"), index=True)
    visual_score: Mapped[float | None] = mapped_column(sa.Float)  # 0-100, None if unavailable
    attribute_score: Mapped[float] = mapped_column(sa.Float, default=0, nullable=False)
    distance_km: Mapped[float | None] = mapped_column(sa.Float)
    distance_score: Mapped[float] = mapped_column(sa.Float, default=0, nullable=False)
    recency_score: Mapped[float] = mapped_column(sa.Float, default=0, nullable=False)
    final_score: Mapped[float] = mapped_column(sa.Float, default=0, nullable=False)
    tier: Mapped[str] = mapped_column(
        enum_type(MatchTier.ALL, "match_tier"), default=MatchTier.HIDDEN, nullable=False
    )
    rank: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    visual_check_unavailable: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, nullable=False
    )
    # {"matching_features": [...], "differences": [...], "confidence": "..."}
    explanation: Mapped[dict | None] = mapped_column(sa.JSON)
    notified_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    scan = relationship("Scan", back_populates="results")
    candidate_photo = relationship("Photo", foreign_keys=[candidate_photo_id])

    @property
    def candidate(self):  # noqa: ANN201 - LostReport | Sighting | None
        """Load the LostReport or Sighting this result points at."""
        from app.models.report import LostReport
        from app.models.sighting import Sighting

        model = LostReport if self.candidate_type == CandidateType.LOST_REPORT else Sighting
        return db.session.get(model, self.candidate_id)

    @property
    def similarity_percent(self) -> int:
        return int(round(self.final_score or 0))


class ImageComparison(CreatedAtMixin, db.Model):
    """Cache of Gemini visual comparisons (SPEC 6.5)."""

    __tablename__ = "image_comparisons"
    __table_args__ = table_args(
        sa.UniqueConstraint("query_sha256", "candidate_photo_id", "model_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    query_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    candidate_photo_id: Mapped[int] = mapped_column(
        sa.ForeignKey("photos.id", ondelete="CASCADE"), index=True, nullable=False
    )
    model_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    result: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
