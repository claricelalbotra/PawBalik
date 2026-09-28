"""Audit log and the background job queue."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import CreatedAtMixin, TimestampMixin, enum_type, table_args
from app.models.enums import JobStatus
from app.utils.time import utcnow


class AuditLog(CreatedAtMixin, db.Model):
    """Written only through app.services.audit_service.log_event()."""

    __tablename__ = "audit_logs"
    __table_args__ = table_args(
        sa.Index("ix_audit_logs_event", "event"),
        sa.Index("ix_audit_logs_created_at", "created_at"),
        sa.Index("ix_audit_logs_target", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    event: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(sa.String(40))
    target_id: Mapped[int | None] = mapped_column(sa.Integer)
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    details: Mapped[dict | None] = mapped_column(sa.JSON)

    actor = relationship("User")


class Job(TimestampMixin, db.Model):
    """Background job (SPEC 6.7). Use app.services.job_service.enqueue(), never insert directly."""

    __tablename__ = "jobs"
    __table_args__ = table_args(
        sa.Index("ix_jobs_status_run_after", "status", "run_after"),
        sa.Index("ix_jobs_type", "type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        enum_type(JobStatus.ALL, "job_status"), default=JobStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    run_after: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime)

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.type} {self.status} attempts={self.attempts}>"
