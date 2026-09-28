"""DB-backed job queue (app/services/job_service.py, SPEC 6.7)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Job, JobStatus
from app.services import job_service
from app.utils.time import utcnow

CALLS: list[dict] = []


@pytest.fixture(autouse=True)
def _test_handlers():
    """Register throwaway handlers for this module and remove them afterwards."""
    CALLS.clear()
    state = {"fail_times": 0}

    @job_service.register("test_ok")
    def ok(payload: dict) -> None:
        CALLS.append(payload)

    @job_service.register("test_flaky")
    def flaky(payload: dict) -> None:
        if state["fail_times"] > 0:
            state["fail_times"] -= 1
            raise RuntimeError("temporary failure")
        CALLS.append(payload)

    yield state
    job_service._HANDLERS.pop("test_ok", None)
    job_service._HANDLERS.pop("test_flaky", None)


def test_handler_modules_register_every_spec_job_type(app):
    handlers = job_service.load_handlers()
    for job_type in job_service.JOB_TYPES:
        assert job_type in handlers, job_type


def test_eager_mode_runs_inline(app):
    job = job_service.enqueue("test_ok", {"n": 1})
    assert CALLS == [{"n": 1}]
    assert job.status == JobStatus.DONE
    assert job.attempts == 1


def test_eager_mode_raises_in_tests(app, _test_handlers):
    _test_handlers["fail_times"] = 1
    with pytest.raises(RuntimeError):
        job_service.enqueue("test_flaky", {})


@pytest.mark.config(JOBS_EAGER_RAISE=False)
def test_eager_mode_marks_failed_without_raising(app, _test_handlers):
    _test_handlers["fail_times"] = 1
    job = job_service.enqueue("test_flaky", {})
    assert job.status == JobStatus.FAILED
    assert "temporary failure" in job.last_error


@pytest.mark.config(JOBS_EAGER=False)
def test_worker_runs_pending_jobs(app, run_jobs):
    job_service.enqueue("test_ok", {"n": 2})
    db.session.commit()
    assert CALLS == []
    assert run_jobs() == 1
    assert CALLS == [{"n": 2}]
    job = db.session.scalar(db.select(Job).where(Job.type == "test_ok"))
    assert job.status == JobStatus.DONE


@pytest.mark.config(JOBS_EAGER=False)
def test_future_jobs_wait(app):
    job_service.enqueue("test_ok", {}, run_after=utcnow() + timedelta(hours=1))
    db.session.commit()
    assert job_service.run_pending() == 0
    assert CALLS == []


@pytest.mark.config(JOBS_EAGER=False, JOBS_BACKOFF_SECONDS=10, JOBS_MAX_ATTEMPTS=3)
def test_retry_with_backoff_then_success(app, _test_handlers):
    _test_handlers["fail_times"] = 1
    job = job_service.enqueue("test_flaky", {"x": 1})
    db.session.commit()
    job_id = job.id
    assert job_service.run_pending() == 1
    job = db.session.get(Job, job_id)
    assert job.status == JobStatus.PENDING
    assert job.attempts == 1
    assert job.run_after > utcnow() + timedelta(seconds=5)  # backed off
    assert job_service.run_pending() == 0  # not due yet
    job.run_after = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert job_service.run_pending() == 1
    assert db.session.get(Job, job_id).status == JobStatus.DONE
    assert CALLS == [{"x": 1}]


@pytest.mark.config(JOBS_EAGER=False, JOBS_BACKOFF_SECONDS=0, JOBS_MAX_ATTEMPTS=2)
def test_job_fails_after_max_attempts(app, _test_handlers):
    _test_handlers["fail_times"] = 10
    job = job_service.enqueue("test_flaky", {})
    db.session.commit()
    job_service.run_pending()
    job_service.run_pending()
    job = db.session.get(Job, job.id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 2


@pytest.mark.config(JOBS_EAGER=False)
def test_unknown_job_type_fails_permanently(app):
    job = job_service.enqueue("no_such_job", {})
    db.session.commit()
    job_service.run_pending()
    job = db.session.get(Job, job.id)
    assert job.status == JobStatus.FAILED
    assert "No handler" in job.last_error


@pytest.mark.config(JOBS_EAGER=False)
def test_claim_is_exclusive(app):
    job = job_service.enqueue("test_ok", {})
    db.session.commit()
    assert job_service._claim(job.id) is True
    assert job_service._claim(job.id) is False  # a second worker cannot take it
    assert job_service.run_job(job.id) == "skipped"


@pytest.mark.config(JOBS_EAGER=False, JOBS_LOCK_TIMEOUT_SECONDS=60)
def test_stale_running_jobs_are_released(app):
    job = job_service.enqueue("test_ok", {})
    job.status = JobStatus.RUNNING
    job.locked_at = utcnow() - timedelta(minutes=5)
    db.session.commit()
    assert job_service.run_pending() == 1
    assert CALLS == [{}]


@pytest.mark.config(JOBS_EAGER=False)
def test_worker_once_schedules_periodic_jobs(app):
    job_service.run_worker(once=True, poll_seconds=0)
    types = {j.type: j.status for j in db.session.scalars(db.select(Job))}
    assert types.get("expire_sightings") == JobStatus.DONE
    assert types.get("cleanup_scan_photos") == JobStatus.DONE
