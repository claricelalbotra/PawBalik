"""Authorization rules - the single place that decides who may see what.

Every route that shows a report, sighting, scan, conversation or photo must
ask one of these functions (and have a test proving it). ``user`` may be
``flask_login.current_user`` (possibly anonymous) or a ``User``.

Sighting visibility (SPEC 5.4, 17.6) - a Pet Sighting is visible only to:

(a) the finder who created it,
(b) administrators,
(c) an owner who holds a *scan result* pointing at it - from their Lost Pet
    Scanner run (trigger='manual') or a background match whose Scan.user_id
    is that owner (trigger='background'),
(d) the other participant of a conversation the finder opened and attached
    that sighting to (the finder chose to share it).

Hidden (moderated) sightings are visible only to (a) and (b).
Sightings are never listed publicly and never enter the Found Pet Scanner.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.extensions import db
from app.models import (
    CandidateType,
    Conversation,
    LostReport,
    Photo,
    PhotoOwner,
    ReportStatus,
    Scan,
    ScanResult,
    Sighting,
)


def _uid(user) -> int | None:  # noqa: ANN001
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if not getattr(user, "is_active", False):
        return None
    return getattr(user, "id", None)


def is_admin(user) -> bool:  # noqa: ANN001
    """Active, logged-in administrator."""
    return _uid(user) is not None and bool(getattr(user, "is_admin", False))


# --- Lost reports ---------------------------------------------------------------


def can_manage_report(user, report: LostReport) -> bool:  # noqa: ANN001
    """Edit the report, change its status, see private panels: owner or admin."""
    uid = _uid(user)
    return uid is not None and (report.owner_id == uid or is_admin(user))


def can_view_report(user, report: LostReport) -> bool:  # noqa: ANN001
    """Public unless hidden by a moderator (then owner/admin only)."""
    return (not report.is_hidden) or can_manage_report(user, report)


def can_view_report_phone(user, report: LostReport) -> bool:  # noqa: ANN001
    """Unmasked phone: the owner opted in (show_phone) or viewer is owner/admin."""
    return bool(report.show_phone) or can_manage_report(user, report)


def can_message_owner(user, report: LostReport) -> bool:  # noqa: ANN001
    """Start a conversation about a report: logged-in, not the owner, report visible and open."""
    uid = _uid(user)
    return (
        uid is not None
        and report.owner_id != uid
        and not report.is_hidden
        and report.status != ReportStatus.CLOSED
    )


# --- Sightings ------------------------------------------------------------------


def _has_scan_result_for(uid: int, sighting_id: int) -> bool:
    stmt = (
        sa.select(ScanResult.id)
        .join(Scan, Scan.id == ScanResult.scan_id)
        .where(
            ScanResult.candidate_type == CandidateType.SIGHTING,
            ScanResult.candidate_id == sighting_id,
            Scan.user_id == uid,
        )
        .limit(1)
    )
    return db.session.scalar(stmt) is not None


def _shared_in_conversation(uid: int, sighting: Sighting) -> bool:
    stmt = (
        sa.select(Conversation.id)
        .where(
            Conversation.sighting_id == sighting.id,
            sa.or_(Conversation.owner_id == uid, Conversation.other_user_id == uid),
            sa.or_(
                Conversation.owner_id == sighting.finder_id,
                Conversation.other_user_id == sighting.finder_id,
            ),
        )
        .limit(1)
    )
    return db.session.scalar(stmt) is not None


def can_view_sighting(user, sighting: Sighting) -> bool:  # noqa: ANN001
    """See the module docstring. Log owner access with audit_service.log_sighting_access()."""
    uid = _uid(user)
    if uid is None:
        return False
    if is_admin(user) or sighting.finder_id == uid:
        return True
    if sighting.is_hidden:
        return False
    return _has_scan_result_for(uid, sighting.id) or _shared_in_conversation(uid, sighting)


def can_manage_sighting(user, sighting: Sighting) -> bool:  # noqa: ANN001
    """Edit / mark matched: the finder or an admin."""
    uid = _uid(user)
    return uid is not None and (sighting.finder_id == uid or is_admin(user))


def visible_sighting_ids_via_scans(user) -> sa.Select:  # noqa: ANN001
    """SELECT of sighting ids the (non-admin) user may see through their scan results."""
    uid = _uid(user) or -1
    return (
        sa.select(ScanResult.candidate_id)
        .join(Scan, Scan.id == ScanResult.scan_id)
        .where(ScanResult.candidate_type == CandidateType.SIGHTING, Scan.user_id == uid)
    )


# --- Scans ----------------------------------------------------------------------


def can_view_scan(user, scan: Scan) -> bool:  # noqa: ANN001
    """Scan results belong to the user the scan was run for (or admins)."""
    uid = _uid(user)
    return uid is not None and (scan.user_id == uid or is_admin(user))


# --- Conversations ----------------------------------------------------------------


def can_view_conversation(user, conv: Conversation) -> bool:  # noqa: ANN001
    """Participants only (owner of the case and the other user). Admins do NOT
    read private chats; moderation works from abuse reports."""
    uid = _uid(user)
    return uid is not None and uid in (conv.owner_id, conv.other_user_id)


def can_post_in_conversation(user, conv: Conversation) -> bool:  # noqa: ANN001
    """Participants, while the case is not hidden and both accounts are active."""
    if not can_view_conversation(user, conv):
        return False
    report = conv.lost_report
    if report is None or report.is_hidden:
        return False
    other = conv.other_participant(user)
    return bool(other is not None and other.is_active)


# --- Photos (used by the local /media route) --------------------------------------------


def can_view_photo(user, photo: Photo) -> bool:  # noqa: ANN001
    """Photos inherit the visibility of what they belong to."""
    if is_admin(user):
        return True
    if photo.owner_id is None:
        return False
    if photo.owner_type == PhotoOwner.AVATAR:
        return True  # profile pictures are shown next to names wherever the name is shown
    if photo.owner_type == PhotoOwner.LOST_REPORT:
        report = db.session.get(LostReport, photo.owner_id)
        return report is not None and can_view_report(user, report)
    if photo.owner_type == PhotoOwner.SIGHTING:
        sighting = db.session.get(Sighting, photo.owner_id)
        return sighting is not None and can_view_sighting(user, sighting)
    if photo.owner_type == PhotoOwner.SCAN:
        scan = db.session.get(Scan, photo.owner_id)
        return scan is not None and can_view_scan(user, scan)
    if photo.owner_type == PhotoOwner.MESSAGE:
        conv = db.session.get(Conversation, photo.owner_id)  # owner_id = conversation id
        return conv is not None and can_view_conversation(user, conv)
    return False


# --- Misc ---------------------------------------------------------------------------------


def is_logged_in(user) -> bool:  # noqa: ANN001
    """Logged in with an active account (guests cannot scan, report or message - SPEC 4)."""
    return _uid(user) is not None


def can_request_rescue(
    user, *, report: LostReport | None = None, sighting: Sighting | None = None
) -> bool:  # noqa: ANN001
    """Build a "Request Rescue Help" summary for a case or sighting the user may see."""
    if _uid(user) is None:
        return False
    if report is not None and not can_view_report(user, report):
        return False
    if sighting is not None and not can_view_sighting(user, sighting):
        return False
    return True
