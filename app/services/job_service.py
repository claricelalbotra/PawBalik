"""DB-backed background job queue and worker loop (SPEC 6.7).

How it works
------------
* ``enqueue("match_sighting", {"sighting_id": 7})`` inserts a row into ``jobs``
  (status ``pending``) in the caller's transaction - it becomes visible to the
  worker when the caller commits, together with the data it refers to.
* ``python run.py worker`` calls :func:`run_pending` in a loop. Each pending job
  is *claimed* with one atomic ``UPDATE jobs SET status='running' ... WHERE
  id=:id AND status='pending'``; the database row lock taken by that UPDATE
  means only one worker can claim a job (portable to MariaDB 10.4, which has no
  ``SKIP LOCKED``). Stuck ``running`` jobs are released after
  ``JOBS_LOCK_TIMEOUT_SECONDS``.
* A failing handler is retried with exponential backoff
  (``JOBS_BACKOFF_SECONDS * 2**(attempt-1)``) up to ``JOBS_MAX_ATTEMPTS`` times,
  then marked ``failed`` with ``last_error``.
* ``JOBS_EAGER=True`` (tests, simple dev setups) runs the handler inline inside
  :func:`enqueue`, in the caller's transaction. With ``JOBS_EAGER_RAISE`` the
  handler's exception propagates (tests); otherwise the job is marked failed.

Handlers
--------
Register with the decorator; a handler receives the payload dict::

    from app.services.job_service import register

    @register("match_sighting")
    def handle_match_sighting(payload: dict) -> None:
        match_new_sighting(payload["sighting_id"])

Handlers may commit. They must be safe to run twice (a crash between the
handler finishing and the job being marked done re-runs it).

Only modules listed in :data:`HANDLER_MODULES` are imported by the worker, so
their ``@register`` decorators run. Job types (SPEC 18): ``match_lost_report``,
``match_sighting``, ``extract_attributes``, ``dispatch_notification``,
``expire_sightings``, ``cleanup_scan_photos``.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from flask import current_app

from app.extensions import db
from app.models import Job, JobStatus
from app.utils.time import utcnow

log = logging.getLogger(__name__)

Handler = Callable[[dict], None]

# Modules whose import registers job handlers (fixed list - add new modules here).
HANDLER_MODULES: tuple[str, ...] = (
    "app.services.matching_service",  # match_lost_report, match_sighting, extract_attributes, cleanup_scan_photos
    "app.services.notification_service",  # dispatch_notification
    "app.routes.sightings",  # expire_sightings
)

JOB_TYPES: tuple[str, ...] = (
    "match_lost_report",
    "match_sighting",
    "extract_attributes",
    "dispatch_notification",
    "expire_sightings",
    "cleanup_scan_photos",
)

_HANDLERS: dict[str, Handler] = {}
_handlers_loaded = False


class UnknownJobType(LookupError):
    """No handler is registered for a job's type."""


def register(job_type: str) -> Callable[[Handler], Handler]:
    """Decorator: register ``func(payload: dict) -> None`` as the handler for ``job_type``."""

    def decorator(func: Handler) -> Handler:
        existing = _HANDLERS.get(job_type)
        if existing is not None and existing is not func:
            log.debug("Replacing handler for job type %s", job_type)
        _HANDLERS[job_type] = func
        return func

    return decorator


def load_handlers() -> dict[str, Handler]:
    """Import every module in HANDLER_MODULES (once) and return the registry."""
    global _handlers_loaded
    if not _handlers_loaded:
        for module in HANDLER_MODULES:
            importlib.import_module(module)
        _handlers_loaded = True
    return _HANDLERS


def get_handler(job_type: str) -> Handler:
    handlers = load_handlers()
    try:
        return handlers[job_type]
    except KeyError:
        raise UnknownJobType(f"No handler registered for job type {job_type!r}") from None


def _cfg(key: str) -> Any:
    return current_app.config[key]


def backoff_delay(attempts: int) -> timedelta:
    """Delay before retry number ``attempts`` (1-based): base, 2x base, 4x base, ..."""
    base = int(_cfg("JOBS_BACKOFF_SECONDS"))
    return timedelta(seconds=base * (2 ** max(0, attempts - 1)))


# --- Enqueue ----------------------------------------------------------------------------


def enqueue(job_type: str, payload: dict | None = None, run_after: datetime | None = None) -> Job:
    """Add a job (flushes; the caller commits). With JOBS_EAGER, run it inline right away.

    ``run_after`` is naive UTC; jobs scheduled in the future are never run eagerly.
    """
    job = Job(
        type=job_type,
        payload=dict(payload or {}),
        status=JobStatus.PENDING,
        attempts=0,
        run_after=run_after or utcnow(),
    )
    db.session.add(job)
    db.session.flush()
    if _cfg("JOBS_EAGER") and (run_after is None or run_after <= utcnow()):
        _run_eager(job)
    return job


def _run_eager(job: Job) -> None:
    """Run a job inline in the caller's transaction (no retries)."""
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_at = utcnow()
    try:
        get_handler(job.type)(dict(job.payload or {}))
    except Exception as exc:
        log.exception("Eager job %s (%s) failed", job.id, job.type)
        job.status = JobStatus.FAILED
        job.last_error = _error_text(exc)
        job.locked_at = None
        if _cfg("JOBS_EAGER_RAISE"):
            raise
        return
    job.status = JobStatus.DONE
    job.locked_at = None
    job.last_error = None


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


# --- Worker side -----------------------------------------------------------------------------


def release_stale_jobs() -> int:
    """Put jobs stuck in ``running`` (worker crashed) back to ``pending``. Returns the count."""
    cutoff = utcnow() - timedelta(seconds=int(_cfg("JOBS_LOCK_TIMEOUT_SECONDS")))
    result = db.session.execute(
        sa.update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
        .values(status=JobStatus.PENDING, locked_at=None, updated_at=utcnow())
    )
    db.session.commit()
    return result.rowcount or 0


def _claim(job_id: int) -> bool:
    """Atomically move one job from pending to running. True if this worker got it."""
    now = utcnow()
    result = db.session.execute(
        sa.update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.PENDING)
        .values(
            status=JobStatus.RUNNING,
            locked_at=now,
            attempts=Job.attempts + 1,
            updated_at=now,
        )
    )
    db.session.commit()
    return result.rowcount == 1


def run_job(job_id: int) -> str:
    """Claim and run one job by id. Returns the resulting status ("done", "pending", "failed")
    or "skipped" if another worker claimed it first."""
    if not _claim(job_id):
        return "skipped"
    job = db.session.get(Job, job_id, populate_existing=True)
    job_type, payload = job.type, dict(job.payload or {})
    try:
        handler = get_handler(job_type)
        handler(payload)
    except Exception as exc:
        db.session.rollback()  # discard the handler's partial work
        job = db.session.get(Job, job_id, populate_existing=True)
        job.last_error = _error_text(exc)
        job.locked_at = None
        permanent = isinstance(exc, UnknownJobType)
        if permanent or job.attempts >= int(_cfg("JOBS_MAX_ATTEMPTS")):
            job.status = JobStatus.FAILED
            log.error("Job %s (%s) failed permanently: %s", job_id, job_type, job.last_error)
        else:
            job.status = JobStatus.PENDING
            job.run_after = utcnow() + backoff_delay(job.attempts)
            log.warning(
                "Job %s (%s) failed (attempt %s), retrying at %s: %s",
                job_id,
                job_type,
                job.attempts,
                job.run_after,
                job.last_error,
            )
        db.session.commit()
        return job.status
    job = db.session.get(Job, job_id, populate_existing=True)
    job.status = JobStatus.DONE
    job.locked_at = None
    job.last_error = None
    db.session.commit()
    return JobStatus.DONE


def run_pending(limit: int = 10) -> int:
    """Run up to ``limit`` due jobs (oldest first). Returns how many jobs this call ran."""
    load_handlers()
    release_stale_jobs()
    due_ids = list(
        db.session.scalars(
            sa.select(Job.id)
            .where(Job.status == JobStatus.PENDING, Job.run_after <= utcnow())
            .order_by(Job.run_after, Job.id)
            .limit(limit)
        )
    )
    db.session.commit()  # end the read transaction before claiming
    ran = 0
    for job_id in due_ids:
        if run_job(job_id) != "skipped":
            ran += 1
    return ran


def schedule_periodic_jobs(last_run: dict[str, datetime]) -> None:
    """Enqueue PERIODIC_JOBS whose interval has passed (skips types already pending)."""
    now = utcnow()
    for job_type, interval in _cfg("PERIODIC_JOBS").items():
        previous = last_run.get(job_type)
        if previous is not None and now - previous < timedelta(seconds=interval):
            continue
        already = db.session.scalar(
            sa.select(Job.id)
            .where(Job.type == job_type, Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
            .limit(1)
        )
        if already is None:
            job = Job(
                type=job_type, payload={}, status=JobStatus.PENDING, attempts=0, run_after=now
            )
            db.session.add(job)
        last_run[job_type] = now
    db.session.commit()


def run_worker(once: bool = False, *, poll_seconds: float | None = None) -> None:
    """The ``python run.py worker`` loop. Needs an app context. Ctrl+C stops it.

    ``once=True`` processes everything currently due and returns (handy for cron/tests).
    """
    handlers = load_handlers()
    poll = poll_seconds if poll_seconds is not None else float(_cfg("WORKER_POLL_SECONDS"))
    log.info("Worker started; job types: %s", ", ".join(sorted(handlers)))
    last_run: dict[str, datetime] = {}
    try:
        while True:
            schedule_periodic_jobs(last_run)
            ran = run_pending()
            if once:
                while ran:  # drain everything that is due
                    ran = run_pending()
                return
            if ran == 0:
                time.sleep(poll)
    except KeyboardInterrupt:
        log.info("Worker stopped.")
    finally:
        db.session.remove()
