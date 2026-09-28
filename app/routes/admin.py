"""Admin dashboard, providers CRUD, moderation, users, logs (SPEC 5.9).

Every page uses ``@admin_required`` (guests -> login, users -> 403) and every
admin action is recorded with ``audit_service.log_event("admin.<action>", ...)``.

Moderation never deletes user content: reports and sightings are hidden with a
reason (``is_hidden``, ``hidden_reason``, ``hidden_by``, ``hidden_at``) and can
be unhidden again. Hidden records drop out of public lists and matching pools
because those queries check ``is_hidden``.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import (
    AbuseReport,
    AbuseStatus,
    AssistanceProvider,
    AuditLog,
    CaseStatusHistory,
    LostReport,
    ProviderType,
    PushLog,
    ReportStatus,
    RescueRequest,
    Scan,
    ScanTrigger,
    Sighting,
    SightingStatus,
    SmsLog,
    SmsPurpose,
    Species,
    User,
    UserRole,
)
from app.routes.rescue import parse_hours, safe_http_url
from app.services.audit_service import log_event
from app.utils.auth import admin_required, api_admin_required
from app.utils.filters import format_date
from app.utils.geo import in_service_area
from app.utils.http import safe_next_url
from app.utils.time import start_of_local_day_utc, to_local, utcnow
from app.utils.validators import (
    ValidationError,
    clean_text,
    parse_bool,
    parse_choice,
    parse_coordinates,
    parse_int,
)

bp = Blueprint("admin", __name__)

PER_PAGE = 25
STATS_DAYS = 14
REASON_MAX_LENGTH = 255


# =============================================================================================
# Helpers
# =============================================================================================


def _page() -> int:
    return max(1, parse_int(request.args.get("page"), default=1) or 1)


def _paginate(stmt: sa.Select) -> Any:
    return db.paginate(stmt, page=_page(), per_page=PER_PAGE, error_out=False)


def _like(value: str) -> str:
    """``%value%`` for LIKE, with the wildcard characters escaped."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _back(default_endpoint: str) -> Any:
    return redirect(safe_next_url(url_for(default_endpoint)))


def _count(stmt: sa.Select) -> int:
    return db.session.scalar(stmt) or 0


# =============================================================================================
# Statistics
# =============================================================================================


def compute_stats(now: Any = None) -> dict[str, Any]:
    """System statistics for the dashboard and ``/api/admin/stats``.

    * ``reunions`` = reports that were ever marked *found* (a history row to
      ``found``), so a case later closed still counts.
    * ``scans_per_day`` covers the last 14 local (Asia/Manila) days, oldest first.
    """
    now = now or utcnow()

    def by_status(model: Any, extra: Any = None) -> dict[str, int]:
        stmt = sa.select(model.status, sa.func.count(model.id)).group_by(model.status)
        if extra is not None:
            stmt = stmt.where(extra)
        return {status: count for status, count in db.session.execute(stmt)}

    visible_reports = by_status(LostReport, LostReport.is_hidden.is_(False))
    all_sightings = by_status(Sighting)
    reunions = _count(
        sa.select(sa.func.count(sa.distinct(CaseStatusHistory.lost_report_id))).where(
            CaseStatusHistory.to_status == ReportStatus.FOUND
        )
    )

    first_day = start_of_local_day_utc(now) - timedelta(days=STATS_DAYS - 1)
    days: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for offset in range(STATS_DAYS):
        day_start = first_day + timedelta(days=offset)
        local_day = to_local(day_start).date()
        days[local_day.isoformat()] = {
            "date": local_day.isoformat(),
            "label": format_date(local_day),
            "manual": 0,
            "background": 0,
            "total": 0,
        }
    for created_at, trigger in db.session.execute(
        sa.select(Scan.created_at, Scan.trigger).where(Scan.created_at >= first_day)
    ):
        key = to_local(created_at).date().isoformat()
        if key in days:
            bucket = days[key]
            bucket["background" if trigger == ScanTrigger.BACKGROUND else "manual"] += 1
            bucket["total"] += 1

    users_total = _count(sa.select(sa.func.count(User.id)))
    users_active = _count(sa.select(sa.func.count(User.id)).where(User.is_active.is_(True)))

    def log_counts(model: Any) -> dict[str, int]:
        counts = {"sent": 0, "failed": 0, "skipped": 0}
        for status, count in db.session.execute(
            sa.select(model.status, sa.func.count(model.id)).group_by(model.status)
        ):
            counts[status] = count
        return counts

    return {
        "active_lost_cases": visible_reports.get(ReportStatus.LOST, 0),
        "found_cases": visible_reports.get(ReportStatus.FOUND, 0),
        "closed_cases": visible_reports.get(ReportStatus.CLOSED, 0),
        "hidden_reports": _count(
            sa.select(sa.func.count(LostReport.id)).where(LostReport.is_hidden.is_(True))
        ),
        "reunions": reunions,
        "sightings": {
            "active": all_sightings.get(SightingStatus.ACTIVE, 0),
            "matched": all_sightings.get(SightingStatus.MATCHED, 0),
            "expired": all_sightings.get(SightingStatus.EXPIRED, 0),
            "hidden": _count(
                sa.select(sa.func.count(Sighting.id)).where(Sighting.is_hidden.is_(True))
            ),
        },
        "scans_per_day": list(days.values()),
        "scans_last_14_days": sum(d["total"] for d in days.values()),
        "users": {
            "total": users_total,
            "active": users_active,
            "deactivated": users_total - users_active,
            "admins": _count(
                sa.select(sa.func.count(User.id)).where(
                    User.role == UserRole.ADMIN, User.is_active.is_(True)
                )
            ),
        },
        "sms": log_counts(SmsLog),
        "push": log_counts(PushLog),
        "abuse_reports_open": _count(
            sa.select(sa.func.count(AbuseReport.id)).where(AbuseReport.status == AbuseStatus.OPEN)
        ),
        "rescue_requests": _count(sa.select(sa.func.count(RescueRequest.id))),
        "providers": {
            "active": _count(
                sa.select(sa.func.count(AssistanceProvider.id)).where(
                    AssistanceProvider.is_active.is_(True)
                )
            ),
            "sample": _count(
                sa.select(sa.func.count(AssistanceProvider.id)).where(
                    AssistanceProvider.is_sample.is_(True)
                )
            ),
        },
    }


@bp.get("/admin")
@admin_required
def dashboard():
    """Counts: active lost cases, sightings, scans per day, reunions; recent SMS/push logs."""
    stats = compute_stats()
    max_scans = max((d["total"] for d in stats["scans_per_day"]), default=0)
    recent_sms = db.session.scalars(sa.select(SmsLog).order_by(SmsLog.id.desc()).limit(5)).all()
    recent_push = db.session.scalars(sa.select(PushLog).order_by(PushLog.id.desc()).limit(5)).all()
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        max_scans=max_scans,
        recent_sms=recent_sms,
        recent_push=recent_push,
        otp_purpose=SmsPurpose.OTP,
    )


# =============================================================================================
# Providers
# =============================================================================================


def _split_phones(raw: str | None) -> list[str]:
    parts = [p.strip() for p in (raw or "").replace(";", ",").replace("\n", ",").split(",")]
    return [p for p in parts if p]


def parse_provider_form(form: Any) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Validate the provider form. Returns (clean data, field errors, warnings)."""
    errors: dict[str, str] = {}
    warnings: list[str] = []
    data: dict[str, Any] = {}

    def text(field: str, max_length: int, *, required: bool = False, source: str | None = None):
        try:
            data[field] = clean_text(
                form.get(source or field), max_length=max_length, required=required, field=field
            )
        except ValidationError as exc:
            errors[field] = exc.message

    text("name", 160, required=True)
    try:
        data["type"] = parse_choice(form.get("type"), ProviderType.ALL, field="type")
    except ValidationError as exc:
        errors["type"] = exc.message
    text("address", 255)
    text("barangay", 120, source="place")  # the location picker's place field
    text("hours_text", 255)
    text("services", 2000)

    raw_lat, raw_lng = (form.get("lat") or "").strip(), (form.get("lng") or "").strip()
    data["lat"] = data["lng"] = None
    if raw_lat or raw_lng:
        try:
            data["lat"], data["lng"] = parse_coordinates(
                raw_lat, raw_lng, require_service_area=False
            )
        except ValidationError as exc:
            errors["location"] = exc.message
        else:
            if not in_service_area(data["lat"], data["lng"]):
                warnings.append(
                    "The pin is outside the Tacloban City service area. Please double-check the location."
                )
    else:
        warnings.append("No map location: this provider cannot be sorted by distance.")

    phones = _split_phones(form.get("phones"))
    bad = [
        p
        for p in phones
        if not (7 <= sum(ch.isdigit() for ch in p) <= 15)
        or any(not (ch.isdigit() or ch in " -+().") for ch in p)
    ]
    joined = ", ".join(phones)
    if bad:
        errors["phones"] = f"Not a valid phone number: {bad[0]}"
    elif len(joined) > 255:
        errors["phones"] = "Too many phone numbers (max 255 characters)."
    data["phones"] = joined or None

    raw_url = (form.get("facebook_url") or "").strip()
    if raw_url:
        url = safe_http_url(raw_url)
        if url is None:
            errors["facebook_url"] = "Enter a full link that starts with https:// (or http://)."
        data["facebook_url"] = url
    else:
        data["facebook_url"] = None

    data["is_24_7"] = parse_bool(form.get("is_24_7"))
    data["is_active"] = parse_bool(form.get("is_active"))
    data["is_sample"] = parse_bool(form.get("is_sample"))
    if data.get("hours_text") and not data["is_24_7"] and parse_hours(data["hours_text"]) is None:
        warnings.append(
            'PawBalik cannot read these hours, so "Open now" will show as unknown. '
            'Use a format like "Mon-Sat 8:00 AM - 5:00 PM".'
        )
    return data, errors, warnings


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if -90 <= number <= 180 else None


def _provider_form_values(provider: AssistanceProvider | None, form: Any = None) -> dict[str, Any]:
    if form is not None:
        return {
            "name": form.get("name", ""),
            # The shared location_picker macro builds a Maps link from these, so only
            # numbers are passed back; an invalid entry is shown as an error instead.
            "lat": _float_or_none(form.get("lat")),
            "lng": _float_or_none(form.get("lng")),
            "type": form.get("type", ""),
            "address": form.get("address", ""),
            "place": form.get("place", ""),
            "phones": form.get("phones", ""),
            "facebook_url": form.get("facebook_url", ""),
            "hours_text": form.get("hours_text", ""),
            "services": form.get("services", ""),
            "is_24_7": parse_bool(form.get("is_24_7")),
            "is_active": parse_bool(form.get("is_active")),
            "is_sample": parse_bool(form.get("is_sample")),
        }
    p = provider
    return {
        "name": p.name if p else "",
        "type": p.type if p else "",
        "address": (p.address if p else "") or "",
        "place": (p.barangay if p else "") or "",
        "lat": p.lat if p else None,
        "lng": p.lng if p else None,
        "phones": (p.phones if p else "") or "",
        "facebook_url": (p.facebook_url if p else "") or "",
        "hours_text": (p.hours_text if p else "") or "",
        "services": (p.services if p else "") or "",
        "is_24_7": bool(p.is_24_7) if p else False,
        "is_active": bool(p.is_active) if p else True,
        "is_sample": bool(p.is_sample) if p else False,
    }


def _render_provider_form(
    provider: AssistanceProvider | None, values: dict, errors: dict, status: int = 200
):
    return (
        render_template(
            "admin/provider_form.html",
            provider=provider,
            values=values,
            errors=errors,
            type_choices=ProviderType.choices(),
        ),
        status,
    )


@bp.get("/admin/providers")
@admin_required
def providers():
    """List of rescue/vet/shelter providers (active and inactive)."""
    q = (request.args.get("q") or "").strip()
    provider_type = request.args.get("type") or ""
    stmt = sa.select(AssistanceProvider).order_by(
        AssistanceProvider.is_active.desc(), AssistanceProvider.name
    )
    if q:
        pattern = _like(q)
        stmt = stmt.where(
            sa.or_(
                AssistanceProvider.name.ilike(pattern, escape="\\"),
                AssistanceProvider.barangay.ilike(pattern, escape="\\"),
            )
        )
    if provider_type in ProviderType.ALL:
        stmt = stmt.where(AssistanceProvider.type == provider_type)
    page = _paginate(stmt)
    return render_template(
        "admin/providers.html",
        page=page,
        q=q,
        provider_type=provider_type,
        type_choices=ProviderType.choices(),
    )


@bp.route("/admin/providers/new", methods=["GET", "POST"])
@admin_required
def provider_new():
    if request.method == "GET":
        return _render_provider_form(None, _provider_form_values(None), {})
    data, errors, warnings = parse_provider_form(request.form)
    if errors:
        return _render_provider_form(None, _provider_form_values(None, request.form), errors, 400)
    item = AssistanceProvider(**data)
    db.session.add(item)
    db.session.flush()
    log_event(
        "admin.provider_created",
        actor=current_user,
        target=item,
        details={"name": item.name, "type": item.type},
    )
    db.session.commit()
    for warning in warnings:
        flash(warning, "warning")
    flash(f'Provider "{item.name}" added.', "success")
    return redirect(url_for("admin.providers"))


@bp.route("/admin/providers/<int:provider_id>/edit", methods=["GET", "POST"])
@admin_required
def provider_edit(provider_id: int):
    item = db.session.get(AssistanceProvider, provider_id) or abort(404)
    if request.method == "GET":
        return _render_provider_form(item, _provider_form_values(item), {})
    data, errors, warnings = parse_provider_form(request.form)
    if errors:
        return _render_provider_form(item, _provider_form_values(None, request.form), errors, 400)
    changed = sorted(k for k, v in data.items() if getattr(item, k) != v)
    for key, value in data.items():
        setattr(item, key, value)
    log_event(
        "admin.provider_updated", actor=current_user, target=item, details={"changed": changed}
    )
    db.session.commit()
    for warning in warnings:
        flash(warning, "warning")
    flash(f'Provider "{item.name}" saved.', "success")
    return redirect(url_for("admin.providers"))


@bp.post("/admin/providers/<int:provider_id>/delete")
@admin_required
def provider_delete(provider_id: int):
    """Delete a provider; if rescue requests reference it, deactivate it instead (keeps history)."""
    item = db.session.get(AssistanceProvider, provider_id) or abort(404)
    in_use = _count(
        sa.select(sa.func.count(RescueRequest.id)).where(RescueRequest.provider_id == item.id)
    )
    name = item.name
    if in_use:
        item.is_active = False
        log_event(
            "admin.provider_deactivated",
            actor=current_user,
            target=item,
            details={"name": name, "rescue_requests": in_use},
        )
        flash(
            f'"{name}" is linked to {in_use} rescue request(s), so it was deactivated '
            "instead of deleted. It no longer appears in the directory.",
            "info",
        )
    else:
        log_event(
            "admin.provider_deleted",
            actor=current_user,
            target=("assistance_providers", item.id),
            details={"name": name},
        )
        db.session.delete(item)
        flash(f'Provider "{name}" deleted.', "success")
    db.session.commit()
    return redirect(url_for("admin.providers"))


# =============================================================================================
# Moderation
# =============================================================================================


def _apply_visibility(item: LostReport | Sighting, kind: str, label: str) -> None:
    """Hide/unhide ``item`` from the POSTed ``action`` and ``reason`` (both required)."""
    action = (request.form.get("action") or "").strip().lower()
    if action not in ("hide", "unhide"):
        abort(400, description="Unknown moderation action.")
    try:
        reason = clean_text(
            request.form.get("reason"), max_length=REASON_MAX_LENGTH, required=True, field="reason"
        )
    except ValidationError:
        flash(
            f"Please give a reason (up to {REASON_MAX_LENGTH} characters) to {action} {label}.",
            "error",
        )
        return
    if action == "hide":
        item.is_hidden = True
        item.hidden_reason = reason
        item.hidden_by = current_user.id
        item.hidden_at = utcnow()
    else:
        item.is_hidden = False
        item.hidden_reason = None
        item.hidden_by = None
        item.hidden_at = None
    details = {"reason": reason}
    if isinstance(item, LostReport):
        details["case_number"] = item.case_number
    log_event(
        f"admin.{kind}_{'hidden' if action == 'hide' else 'unhidden'}",
        actor=current_user,
        target=item,
        details=details,
    )
    db.session.commit()
    flash(
        f"{label[0].upper()}{label[1:]} {'hidden' if action == 'hide' else 'is visible again'}.",
        "success",
    )


def _visibility_filter(stmt: sa.Select, model: Any, value: str) -> sa.Select:
    if value == "hidden":
        return stmt.where(model.is_hidden.is_(True))
    if value == "visible":
        return stmt.where(model.is_hidden.is_(False))
    return stmt


@bp.get("/admin/reports")
@admin_required
def reports():
    """Moderate Lost Pet Reports (search by case number, pet name or owner email)."""
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    visibility = request.args.get("visibility") or ""
    stmt = sa.select(LostReport).join(User, User.id == LostReport.owner_id)
    if q:
        pattern = _like(q)
        stmt = stmt.where(
            sa.or_(
                LostReport.case_number.ilike(pattern, escape="\\"),
                LostReport.pet_name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
                User.full_name.ilike(pattern, escape="\\"),
            )
        )
    if status in ReportStatus.ALL:
        stmt = stmt.where(LostReport.status == status)
    stmt = _visibility_filter(stmt, LostReport, visibility).order_by(LostReport.id.desc())
    return render_template(
        "admin/reports.html",
        page=_paginate(stmt),
        q=q,
        status=status,
        visibility=visibility,
        status_choices=ReportStatus.choices(),
    )


@bp.post("/admin/reports/<int:report_id>/visibility")
@admin_required
def report_visibility(report_id: int):
    """Hide/unhide a report (fields: action=hide|unhide, reason)."""
    report = db.session.get(LostReport, report_id) or abort(404)
    _apply_visibility(report, "report", f"case {report.case_number}")
    return _back("admin.reports")


@bp.get("/admin/sightings")
@admin_required
def sightings():
    """Moderate Pet Sightings (admins may see all sightings)."""
    status = request.args.get("status") or ""
    species = request.args.get("species") or ""
    visibility = request.args.get("visibility") or ""
    stmt = sa.select(Sighting)
    if status in SightingStatus.ALL:
        stmt = stmt.where(Sighting.status == status)
    if species in Species.ALL:
        stmt = stmt.where(Sighting.species == species)
    stmt = _visibility_filter(stmt, Sighting, visibility).order_by(Sighting.id.desc())
    return render_template(
        "admin/sightings.html",
        page=_paginate(stmt),
        status=status,
        species=species,
        visibility=visibility,
        status_choices=SightingStatus.choices(),
        species_choices=Species.choices(),
    )


@bp.post("/admin/sightings/<int:sighting_id>/visibility")
@admin_required
def sighting_visibility(sighting_id: int):
    """Hide/unhide a sighting (fields: action=hide|unhide, reason)."""
    sighting = db.session.get(Sighting, sighting_id) or abort(404)
    _apply_visibility(sighting, "sighting", f"sighting #{sighting.id}")
    return _back("admin.sightings")


# =============================================================================================
# Users
# =============================================================================================


def deactivation_problem(actor: User, target: User) -> str | None:
    """Why ``actor`` may not deactivate ``target`` (None = allowed)."""
    if target.id == actor.id:
        return "You cannot deactivate your own account."
    if target.role == UserRole.ADMIN:
        other_admins = _count(
            sa.select(sa.func.count(User.id)).where(
                User.role == UserRole.ADMIN, User.is_active.is_(True), User.id != target.id
            )
        )
        if other_admins == 0:
            return "You cannot deactivate the last active administrator."
    return None


@bp.get("/admin/users")
@admin_required
def users():
    q = (request.args.get("q") or "").strip()
    role = request.args.get("role") or ""
    state = request.args.get("state") or ""
    stmt = sa.select(User)
    if q:
        pattern = _like(q)
        stmt = stmt.where(
            sa.or_(
                User.full_name.ilike(pattern, escape="\\"),
                User.email.ilike(pattern, escape="\\"),
                User.mobile_e164.ilike(pattern, escape="\\"),
            )
        )
    if role in UserRole.ALL:
        stmt = stmt.where(User.role == role)
    if state == "active":
        stmt = stmt.where(User.is_active.is_(True))
    elif state == "inactive":
        stmt = stmt.where(User.is_active.is_(False))
    stmt = stmt.order_by(User.id.desc())
    return render_template(
        "admin/users.html",
        page=_paginate(stmt),
        q=q,
        role=role,
        state=state,
        role_choices=UserRole.choices(),
    )


@bp.post("/admin/users/<int:user_id>/active")
@admin_required
def user_active(user_id: int):
    """Deactivate/reactivate a user (field: active=0|1). Not yourself, not the last admin."""
    target = db.session.get(User, user_id) or abort(404)
    raw = (request.form.get("active") or "").strip()
    if raw not in ("0", "1"):
        abort(400, description="active must be 0 or 1.")
    make_active = raw == "1"
    reason = (request.form.get("reason") or "").strip()[:REASON_MAX_LENGTH] or None
    if not make_active:
        problem = deactivation_problem(current_user, target)
        if problem:
            flash(problem, "error")
            return _back("admin.users")
    if target.is_active == make_active:
        flash(
            f"{target.full_name} is already {'active' if make_active else 'deactivated'}.", "info"
        )
        return _back("admin.users")
    target.is_active = make_active
    log_event(
        "admin.user_reactivated" if make_active else "admin.user_deactivated",
        actor=current_user,
        target=target,
        details={"email": target.email, "reason": reason},
    )
    db.session.commit()
    flash(
        f"{target.full_name} {'can log in again' if make_active else 'was deactivated and can no longer log in'}.",
        "success",
    )
    return _back("admin.users")


# =============================================================================================
# Abuse reports
# =============================================================================================


@bp.get("/admin/abuse-reports")
@admin_required
def abuse_reports():
    status = request.args.get("status", AbuseStatus.OPEN)
    stmt = sa.select(AbuseReport).order_by(AbuseReport.id.desc())
    if status in AbuseStatus.ALL:
        stmt = stmt.where(AbuseReport.status == status)
    else:
        status = ""
    return render_template(
        "admin/abuse_reports.html",
        page=_paginate(stmt),
        status=status,
        status_choices=AbuseStatus.choices(),
    )


@bp.post("/admin/abuse-reports/<int:abuse_report_id>")
@admin_required
def abuse_report_update(abuse_report_id: int):
    """Set an abuse report's status (field: status=reviewed|dismissed|open)."""
    item = db.session.get(AbuseReport, abuse_report_id) or abort(404)
    try:
        new_status = parse_choice(request.form.get("status"), AbuseStatus.ALL, field="status")
    except ValidationError:
        abort(400, description="Unknown status.")
    old_status = item.status
    item.status = new_status
    log_event(
        "admin.abuse_report_updated",
        actor=current_user,
        target=item,
        details={"from": old_status, "to": new_status, "target_user_id": item.target_user_id},
    )
    db.session.commit()
    flash(f"Abuse report #{item.id} marked {AbuseStatus.label(new_status).lower()}.", "success")
    return _back("admin.abuse_reports")


# =============================================================================================
# Logs
# =============================================================================================

LOG_KINDS = OrderedDict(
    [
        ("audit", ("Audit log", AuditLog)),
        ("sms", ("SMS log", SmsLog)),
        ("push", ("Push log", PushLog)),
    ]
)


@bp.get("/admin/logs")
@admin_required
def logs():
    """Recent audit, SMS and push logs with pagination (``?kind=audit|sms|push&page=``)."""
    kind = request.args.get("kind", "audit")
    if kind not in LOG_KINDS:
        kind = "audit"
    model = LOG_KINDS[kind][1]
    stmt = sa.select(model).order_by(model.id.desc())
    event = (request.args.get("event") or "").strip()
    if kind == "audit" and event:
        stmt = stmt.where(
            AuditLog.event.ilike(event.replace("%", "").replace("_", "\\_") + "%", escape="\\")
        )
    return render_template(
        "admin/logs.html",
        kind=kind,
        kinds=LOG_KINDS,
        page=_paginate(stmt),
        event=event,
        otp_purpose=SmsPurpose.OTP,
    )


# =============================================================================================
# JSON API
# =============================================================================================


@bp.get("/api/admin/stats")
@api_admin_required
def api_stats():
    """GET /api/admin/stats -> {"active_lost_cases", "sightings", "scans_per_day": [...], "reunions", ...}."""
    return compute_stats()
