"""Shared model building blocks."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.utils.time import utcnow

# Every table: InnoDB + utf8mb4_unicode_ci on MySQL/MariaDB (ignored by SQLite).
MYSQL_TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def table_args(*items: object) -> tuple:
    """Build ``__table_args__``: indexes/constraints followed by the MySQL options."""
    return (*items, dict(MYSQL_TABLE_OPTIONS))


def enum_type(values: tuple[str, ...], name: str) -> sa.Enum:
    """String enum: native ENUM on MySQL/MariaDB, VARCHAR on SQLite.

    ``validate_strings=True`` makes SQLAlchemy reject unknown values in Python
    (so tests on SQLite catch typos that MySQL would reject).
    """
    return sa.Enum(*values, name=name, native_enum=True, validate_strings=True)


# Latitude/longitude as DOUBLE (float64). MySQL FLOAT would lose precision.
Coordinate = sa.Double


class TimestampMixin:
    """``created_at`` / ``updated_at`` in naive UTC (see app.utils.time)."""

    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(sa.DateTime, default=utcnow, nullable=False)
