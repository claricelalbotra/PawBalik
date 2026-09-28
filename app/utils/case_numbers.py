"""Unique case numbers: ``PB-YYYY-NNNNNN`` (SPEC 5.1), e.g. ``PB-2026-000123``.

The ``case_counters`` table keeps one row per year with the last number
issued. :func:`next_case_number` must be called inside the transaction that
saves the report:

1. make sure the year's counter row exists (an idempotent "insert or do
   nothing", so two first-reports-of-the-year cannot collide),
2. lock it with ``SELECT ... FOR UPDATE`` (MySQL/MariaDB; SQLite serialises
   writers anyway and ignores FOR UPDATE),
3. increment and flush.

The lock is held until the caller commits (or rolls back, which also gives
the number back). Concurrent requests therefore never get the same number.
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from app.extensions import db
from app.models.report import CaseCounter
from app.utils.time import local_now

CASE_NUMBER_RE = re.compile(r"^PB-(\d{4})-(\d{6})$")


def format_case_number(year: int, value: int) -> str:
    """``(2026, 123)`` -> ``"PB-2026-000123"``."""
    return f"PB-{year:04d}-{value:06d}"


def _ensure_counter_row(year: int) -> None:
    """INSERT the counter row for ``year`` unless it already exists (race-safe)."""
    dialect = db.session.get_bind().dialect.name
    table = CaseCounter.__table__
    if dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        stmt = mysql_insert(table).values(year=year, last_value=0)
        stmt = stmt.on_duplicate_key_update(last_value=table.c.last_value)  # no-op update
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(table).values(year=year, last_value=0).on_conflict_do_nothing()
    else:  # pragma: no cover - other databases are not supported targets
        if db.session.get(CaseCounter, year) is None:
            db.session.add(CaseCounter(year=year, last_value=0))
            db.session.flush()
        return
    db.session.execute(stmt)


def next_case_number(year: int | None = None) -> str:
    """Reserve and return the next case number for ``year`` (default: current Manila year).

    Call it inside the transaction that saves the report; the caller commits.
    """
    year = year or local_now().year
    _ensure_counter_row(year)
    counter = db.session.execute(
        sa.select(CaseCounter)
        .where(CaseCounter.year == year)
        .with_for_update()
        .execution_options(populate_existing=True)  # re-read the locked row, not a cached copy
    ).scalar_one()
    counter.last_value += 1
    db.session.flush()
    return format_case_number(year, counter.last_value)


def parse_case_number(value: str | None) -> tuple[int, int] | None:
    """``"PB-2026-000123"`` -> ``(2026, 123)``; None if malformed."""
    match = CASE_NUMBER_RE.match((value or "").strip().upper())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))
