"""Audit trail (SPEC 10): login failures, admin actions, sighting access.

Event names are dotted, lower-case: ``auth.login_failed``, ``auth.login``,
``auth.logout``, ``auth.register``, ``auth.profile_updated``,
``auth.password_changed``, ``sighting.access``, ``admin.report_hidden``,
``admin.report_unhidden``, ``admin.sighting_hidden``, ``admin.user_deactivated``,
``admin.provider_created``, ``admin.abuse_report_updated`` ...

``log_event`` adds the row to the current session; it is saved when the
caller commits (pass ``commit=True`` when nothing else will be committed,
e.g. a failed login).
"""

from __future__ import annotations

from typing import Any

from flask import has_request_context, request

from app.extensions import db
from app.models import AuditLog, SightingAccessLog


def _target_parts(target: Any) -> tuple[str | None, int | None]:
    if target is None:
        return None, None
    if isinstance(target, tuple):
        target_type, target_id = target
        return str(target_type), (int(target_id) if target_id is not None else None)
    table = getattr(target, "__tablename__", None) or type(target).__name__.lower()
    return table, getattr(target, "id", None)


def _client_ip() -> str | None:
    if not has_request_context():
        return None
    return (request.remote_addr or "")[:45] or None


def log_event(
    event: str,
    *,
    actor: Any = None,
    target: Any = None,
    details: dict | None = None,
    commit: bool = False,
) -> AuditLog:
    """Record an audit event.

    ``actor``: a User (anonymous users are stored as NULL).
    ``target``: a model instance (uses its table name + id) or a
    ``(type, id)`` tuple. ``details``: small JSON-serialisable dict - never
    passwords, OTP codes or tokens.
    """
    actor_id = None
    if actor is not None and getattr(actor, "is_authenticated", True):
        actor_id = getattr(actor, "id", None)
    target_type, target_id = _target_parts(target)
    entry = AuditLog(
        actor_id=actor_id,
        event=event[:64],
        target_type=target_type,
        target_id=target_id,
        ip=_client_ip(),
        details=details or None,
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def log_sighting_access(sighting: Any, viewer: Any, scan: Any = None) -> SightingAccessLog:
    """Record that ``viewer`` opened a private sighting (SPEC 5.2). Caller commits."""
    entry = SightingAccessLog(
        sighting_id=sighting.id,
        viewer_id=viewer.id,
        scan_id=getattr(scan, "id", scan),
    )
    db.session.add(entry)
    log_event(
        "sighting.access",
        actor=viewer,
        target=sighting,
        details={"scan_id": getattr(scan, "id", scan)} if scan is not None else None,
    )
    return entry
