"""Case details page, My Cases dashboard, status changes + history (SPEC 5.5, 5.1).

Pages
    cases.dashboard       GET   /my-cases                    the user's reports and sightings
    cases.detail          GET   /cases/<id>                  public unless hidden; owner panel
    cases.by_number       GET   /cases/PB-2026-000123        redirect to cases.detail
    cases.change_status   POST  /cases/<id>/status           "Mark as Found" / "Close Case"
JSON API
    cases.api_update_status  PATCH /api/lost-reports/<id>/status  {"status", "note"}
    cases.api_history        GET   /api/lost-reports/<id>/history

Status changes: ``report.change_status()`` checks the allowed transitions
(lost -> found -> closed, lost -> closed) and adds the history row; then every
conversation about the case gets a system message and each participant other
than the person who made the change gets a ``case_status_changed`` notification.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    CandidateType,
    CaseStatusHistory,
    InvalidStatusTransition,
    LostReport,
    MatchTier,
    ReportStatus,
    Scan,
    ScanMode,
    ScanResult,
    Sighting,
    SightingStatus,
    User,
    allowed_transitions,
)
from app.routes.pets import (
    contact_info,
    get_visible_report_or_404,
    history_list,
    iso_utc,
)
from app.services import policy_service, storage_service
from app.services.audit_service import log_event, log_sighting_access
from app.services.message_service import conversations_for_report, post_system_message
from app.services.notification_service import notify
from app.utils.auth import api_login_required
from app.utils.cards import lost_report_card, sighting_card
from app.utils.case_numbers import parse_case_number
from app.utils.http import json_error
from app.utils.validators import ValidationError, clean_text

bp = Blueprint("cases", __name__)

# Wording for each target status (system messages, notifications, flash messages).
STATUS_PHRASES = {
    ReportStatus.FOUND: "marked as Found",
    ReportStatus.CLOSED: "closed",
}
STATUS_BUTTONS = {
    ReportStatus.FOUND: ("Mark as Found", "btn-primary", "check-circle"),
    ReportStatus.CLOSED: ("Close Case", "btn-outline", "x"),
}
MAX_PANEL_MATCHES = 12


# =============================================================================================
# Status changes
# =============================================================================================


def apply_status_change(
    report: LostReport, to_status: str, *, actor: User, note: str | None = None
) -> CaseStatusHistory:
    """Change the status, tell every conversation and its participants, audit. Caller commits.

    Raises ``InvalidStatusTransition`` when the move is not allowed.
    """
    from_status = report.status
    entry = report.change_status(to_status, actor=actor, note=note)
    phrase = STATUS_PHRASES.get(to_status, f"set to {ReportStatus.label(to_status)}")
    body = f"The case for {report.pet_name} ({report.case_number}) was {phrase}."
    if note:
        body += f" Note from the owner: {note}"
    link = url_for("cases.detail", report_id=report.id)

    notified: set[int] = set()
    for conversation in conversations_for_report(report):
        post_system_message(conversation, body)
        for participant in (conversation.owner, conversation.other_user):
            if (
                participant is None
                or participant.id == actor.id
                or participant.id in notified
                or not participant.is_active
            ):
                continue
            notified.add(participant.id)
            notify(
                participant,
                "case_status_changed",
                {
                    "lost_report_id": report.id,
                    "case_number": report.case_number,
                    "from_status": from_status,
                    "to_status": to_status,
                    "link_url": link,
                    "title": f"Case {report.case_number} was {phrase}",
                    "body": f"{report.pet_name}'s case is now " f"{ReportStatus.label(to_status)}.",
                },
            )
    log_event(
        "case.status_changed",
        actor=actor,
        target=report,
        details={"from": from_status, "to": to_status, "notified": len(notified)},
    )
    return entry


def _parse_status_input(raw_status: Any, raw_note: Any) -> tuple[str, str | None]:
    """Validate the submitted status/note (raises ValidationError)."""
    status = str(raw_status or "").strip().lower()
    if status not in ReportStatus.ALL:
        raise ValidationError("Choose 'found' or 'closed'.", "status")
    note = clean_text(None if raw_note is None else str(raw_note), max_length=255, field="note")
    return status, note


@bp.post("/cases/<int:report_id>/status")
@login_required
def change_status(report_id: int):
    """Form post from the owner panel: "Mark as Found" / "Close Case" (fields: status, note)."""
    report = get_visible_report_or_404(report_id)
    if not policy_service.can_manage_report(current_user, report):
        abort(403)
    detail_url = url_for("cases.detail", report_id=report.id)
    try:
        to_status, note = _parse_status_input(request.form.get("status"), request.form.get("note"))
    except ValidationError as exc:
        flash(exc.message if exc.field == "note" else "Please choose a valid status.", "error")
        return redirect(detail_url)
    try:
        apply_status_change(report, to_status, actor=current_user, note=note)
    except InvalidStatusTransition:
        flash(
            f"A {ReportStatus.label(report.status).lower()} case cannot be "
            f"{STATUS_PHRASES.get(to_status, 'changed that way')}.",
            "error",
        )
        return redirect(detail_url)
    db.session.commit()
    if to_status == ReportStatus.FOUND:
        flash(
            f"Wonderful news! {report.pet_name}'s case was marked as Found. "
            "Close the case when everything is settled.",
            "success",
        )
    else:
        flash(f"Case {report.case_number} was closed.", "success")
    return redirect(detail_url)


# =============================================================================================
# Case details
# =============================================================================================


def possible_matches(report: LostReport, viewer: Any) -> list[dict[str, Any]]:
    """Sighting cards found for this report so far (owner panel).

    Source: results of the owner's Lost Pet Scanner runs and background matches for
    this report (``Scan.user_id`` = owner, ``Scan.source_lost_report_id`` = report).
    Weak results (tier "hidden") are left out; each sighting appears once (best score);
    only sightings the viewer may see are shown, and each shown sighting is logged
    as an access (SPEC 5.2).
    """
    rows = db.session.execute(
        sa.select(ScanResult, Scan)
        .join(Scan, Scan.id == ScanResult.scan_id)
        .where(
            Scan.user_id == report.owner_id,
            Scan.mode == ScanMode.LOST,
            Scan.source_lost_report_id == report.id,
            ScanResult.candidate_type == CandidateType.SIGHTING,
            ScanResult.tier != MatchTier.HIDDEN,
        )
        .order_by(ScanResult.final_score.desc(), ScanResult.id.desc())
    ).all()
    cards: list[dict[str, Any]] = []
    seen: set[int] = set()
    is_owner = getattr(viewer, "id", None) == report.owner_id
    for result, scan in rows:
        if result.candidate_id in seen or len(cards) >= MAX_PANEL_MATCHES:
            continue
        sighting = db.session.get(Sighting, result.candidate_id)
        if sighting is None or not policy_service.can_view_sighting(viewer, sighting):
            continue
        seen.add(sighting.id)
        card = sighting_card(
            sighting,
            distance_km=result.distance_km,
            similarity=result.final_score,
            tier=result.tier,
            explanation=result.explanation,
            visual_check_unavailable=bool(result.visual_check_unavailable),
            message_url=(
                url_for("messages.new", lost_report_id=report.id, sighting_id=sighting.id)
                if is_owner and sighting.finder_id != report.owner_id
                else None
            ),
        )
        cards.append(card)
        if is_owner and sighting.finder_id != viewer.id:
            log_sighting_access(sighting, viewer, scan)
    return cards


def conversation_rows(report: LostReport, viewer: Any) -> list[dict[str, Any]]:
    """Conversations about the case, for the owner panel (participants only, SPEC 5.6)."""
    rows = []
    for conversation in conversations_for_report(report):
        if not policy_service.can_view_conversation(viewer, conversation):
            continue
        other = conversation.other_participant(viewer)
        rows.append(
            {
                "id": conversation.id,
                "other_name": other.full_name if other else "PawBalik user",
                "has_sighting": conversation.sighting_id is not None,
                "last_activity": conversation.last_message_at or conversation.created_at,
                "unread": conversation.unread_count_for(viewer),
                "url": url_for("messages.thread", conversation_id=conversation.id),
            }
        )
    return rows


def ai_notes(report: LostReport) -> dict[str, Any] | None:
    """Gemini attribute extraction for the primary photo (SPEC 6.3), if available."""
    attributes = report.ai_attributes
    if not attributes:
        primary = report.primary_photo
        attributes = primary.ai_attributes if primary is not None else None
    if not isinstance(attributes, dict) or not attributes:
        return None
    notes = {
        "Breed (AI guess)": attributes.get("breed_guess"),
        "Colours": ", ".join(attributes.get("primary_colors") or []) or None,
        "Coat pattern": attributes.get("coat_pattern"),
        "Coat length": attributes.get("coat_length"),
        "Size (estimate)": attributes.get("size_estimate"),
        "Distinctive marks": ", ".join(attributes.get("distinctive_marks") or []) or None,
        "Accessories": ", ".join(attributes.get("accessories") or []) or None,
    }
    notes = {k: str(v) for k, v in notes.items() if v}
    return notes or None


@bp.get("/cases/<int:report_id>")
def detail(report_id: int):
    """Case details (lost_pet/details.html). Public unless hidden; owners get the extra panel."""
    report = get_visible_report_or_404(report_id)
    can_manage = policy_service.can_manage_report(current_user, report)
    is_owner = current_user.is_authenticated and current_user.id == report.owner_id
    photos = [
        {
            "full": storage_service.photo_url(photo, "full"),
            "thumb": storage_service.photo_url(photo, "thumb"),
            "is_primary": bool(photo.is_primary),
        }
        for photo in report.photos
    ]
    context: dict[str, Any] = {
        "report": report,
        "photos": photos,
        "contact": contact_info(report, current_user),
        "history": report.status_history,
        "show_notes": can_manage,
        "ai_notes": ai_notes(report),
        "can_manage": can_manage,
        "is_owner": is_owner,
        # Guests see the button too; messages.new asks them to log in first.
        "show_message_button": (
            policy_service.can_message_owner(current_user, report)
            or (not current_user.is_authenticated and report.status != ReportStatus.CLOSED)
        ),
        "status_actions": [],
        "matches": [],
        "conversations": [],
    }
    if can_manage:
        context["status_actions"] = [
            (status, *STATUS_BUTTONS[status]) for status in allowed_transitions(report.status)
        ]
        context["matches"] = possible_matches(report, current_user)
        context["conversations"] = conversation_rows(report, current_user)
        if context["matches"] and is_owner:
            db.session.commit()  # save the sighting access log rows
    return render_template("lost_pet/details.html", **context)


@bp.get("/cases/<case_number>")
def by_number(case_number: str):
    """Look up a case by its number (PB-YYYY-NNNNNN) and redirect to cases.detail."""
    normalized = case_number.strip().upper()
    if parse_case_number(normalized) is None:
        abort(404)
    report = db.session.scalar(sa.select(LostReport).where(LostReport.case_number == normalized))
    if report is None or not policy_service.can_view_report(current_user, report):
        abort(404)
    return redirect(url_for("cases.detail", report_id=report.id))


# =============================================================================================
# My Cases dashboard
# =============================================================================================


def _match_counts(owner_id: int, report_ids: list[int]) -> dict[int, int]:
    """Possible matches (distinct sightings, not weak) per report of this owner."""
    if not report_ids:
        return {}
    rows = db.session.execute(
        sa.select(Scan.source_lost_report_id, sa.func.count(sa.distinct(ScanResult.candidate_id)))
        .join(ScanResult, ScanResult.scan_id == Scan.id)
        .join(
            Sighting,
            sa.and_(
                ScanResult.candidate_type == CandidateType.SIGHTING,
                Sighting.id == ScanResult.candidate_id,
            ),
        )
        .where(
            Scan.user_id == owner_id,
            Scan.mode == ScanMode.LOST,
            Scan.source_lost_report_id.in_(report_ids),
            ScanResult.tier != MatchTier.HIDDEN,
            Sighting.is_hidden.is_(False),
        )
        .group_by(Scan.source_lost_report_id)
    ).all()
    return {report_id: count for report_id, count in rows}


@bp.get("/my-cases")
@login_required
def dashboard():
    """My Cases dashboard (dashboard.html): the user's reports and sightings with status badges."""
    reports = list(
        db.session.scalars(
            sa.select(LostReport)
            .where(LostReport.owner_id == current_user.id)
            .order_by(LostReport.created_at.desc(), LostReport.id.desc())
        )
    )
    sightings = list(
        db.session.scalars(
            sa.select(Sighting)
            .where(Sighting.finder_id == current_user.id)
            .order_by(Sighting.created_at.desc(), Sighting.id.desc())
        )
    )
    match_counts = _match_counts(current_user.id, [r.id for r in reports])

    report_cards = []
    for report in reports:
        card = lost_report_card(report, show_message=False)
        if report.is_hidden:
            card["chip"], card["chip_kind"] = "Hidden by moderator", "hidden"
        report_cards.append(
            {"card": card, "report": report, "matches": match_counts.get(report.id, 0)}
        )

    sighting_cards = []
    for sighting in sightings:
        card = sighting_card(sighting)
        if sighting.status != SightingStatus.ACTIVE:
            card["chip"] = SightingStatus.label(sighting.status)
            card["chip_kind"] = sighting.status
        if sighting.is_hidden:
            card["chip"], card["chip_kind"] = "Hidden by moderator", "hidden"
        sighting_cards.append({"card": card, "sighting": sighting})

    counts = {
        "lost": sum(1 for r in reports if r.status == ReportStatus.LOST),
        "found": sum(1 for r in reports if r.status == ReportStatus.FOUND),
        "closed": sum(1 for r in reports if r.status == ReportStatus.CLOSED),
        "sightings_active": sum(1 for s in sightings if s.status == SightingStatus.ACTIVE),
        "matches": sum(match_counts.values()),
    }
    return render_template(
        "dashboard.html",
        report_cards=report_cards,
        sighting_cards=sighting_cards,
        counts=counts,
    )


# =============================================================================================
# JSON API
# =============================================================================================


@bp.patch("/api/lost-reports/<int:report_id>/status")
@api_login_required
def api_update_status(report_id: int):
    """PATCH /api/lost-reports/<id>/status {"status": "found"|"closed", "note": str}."""
    report = get_visible_report_or_404(report_id)
    if not policy_service.can_manage_report(current_user, report):
        return json_error("forbidden", "Only the owner can change this case's status.", 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("bad_request", 'Send a JSON object: {"status": ..., "note": ...}.')
    try:
        to_status, note = _parse_status_input(payload.get("status"), payload.get("note"))
    except ValidationError as exc:
        return json_error("validation_error", exc.message, 400, details={exc.field: exc.message})
    try:
        apply_status_change(report, to_status, actor=current_user, note=note)
    except InvalidStatusTransition:
        return json_error(
            "validation_error",
            f"A {report.status} case cannot be changed to {to_status}.",
            422,
            details={"status": report.status, "allowed": list(allowed_transitions(report.status))},
        )
    db.session.commit()
    return {
        "id": report.id,
        "case_number": report.case_number,
        "status": report.status,
        "updated_at": iso_utc(report.updated_at),
        "history": history_list(report, current_user),
    }


@bp.get("/api/lost-reports/<int:report_id>/history")
def api_history(report_id: int):
    """GET /api/lost-reports/<id>/history -> {"history": [{from_status, to_status, changed_at, note}]}."""
    report = get_visible_report_or_404(report_id)
    return {"history": history_list(report, current_user)}
