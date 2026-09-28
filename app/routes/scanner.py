"""Found/Lost Pet Scanner pages, scanning, results, POST /api/scans (SPEC 5.2, 5.3, 16.4-16.6).

* **Found Pet Scanner** (``/scan/found``): a finder's photo is compared with
  public Lost Pet Reports. Results never include Pet Sightings.
* **Lost Pet Scanner** (``/scan/lost``): an owner picks one of their own
  active reports (its photo is used) or uploads a new photo; results are
  private Pet Sightings, and every sighting shown is written to the access log.

The upload is saved with ``storage_service.save_image`` and matched by
``matching_service.run_scan``. Results are shown by
``matching_service.results_for_display`` and filtered again here with
``policy_service`` (defence in depth). The JSON API and the no-JS form share
:func:`perform_scan`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import config_limit, db, limiter
from app.models import (
    AnimalCondition,
    CandidateType,
    ContactPref,
    LostReport,
    Photo,
    PhotoOwner,
    ReportStatus,
    Scan,
    ScanMode,
    ScanStatus,
    ScanTrigger,
    Sighting,
    SightingStatus,
)
from app.routes import sightings as sighting_routes
from app.services import matching_service, policy_service, storage_service
from app.services.audit_service import log_sighting_access
from app.utils import strings
from app.utils.auth import api_login_required
from app.utils.filters import format_datetime, timeago
from app.utils.geo import in_service_area
from app.utils.http import json_error
from app.utils.maps import directions_url, view_url
from app.utils.validators import ValidationError, parse_int

log = logging.getLogger(__name__)

bp = Blueprint("scanner", __name__)

SCAN_LIMIT = limiter.shared_limit(config_limit("RATE_LIMIT_SCANS"), scope="scans", methods=["POST"])


# --- Scan request parsing ------------------------------------------------------------------


@dataclass
class ScanRequest:
    """A validated scan request (from the JSON API or the no-JS form)."""

    mode: str
    upload: Any = None  # FileStorage or None
    report: LostReport | None = None
    lat: float | None = None
    lng: float | None = None
    warnings: list[str] = field(default_factory=list)


def active_reports_for(user: Any) -> list[LostReport]:
    """The user's own reports that are still ``lost`` (Lost Pet Scanner selector)."""
    return list(
        db.session.scalars(
            sa.select(LostReport)
            .where(LostReport.owner_id == user.id, LostReport.status == ReportStatus.LOST)
            .order_by(LostReport.created_at.desc(), LostReport.id.desc())
        )
    )


def _first_upload() -> Any:
    """First non-empty ``photo`` file (the page has Upload and Capture inputs)."""
    for upload in request.files.getlist("photo"):
        if upload and upload.filename:
            return upload
    return None


def parse_scan_request(user: Any) -> ScanRequest:
    """Validate mode / photo / report / optional location. Raises ValidationError."""
    mode = (request.form.get("mode") or "").strip().lower()
    if mode not in ScanMode.ALL:
        raise ValidationError("Please choose the Found Pet or Lost Pet Scanner.", "mode")
    req = ScanRequest(mode=mode, upload=_first_upload())

    report_id = parse_int(request.form.get("lost_report_id"))
    if report_id is not None and mode == ScanMode.LOST:
        report = db.session.get(LostReport, report_id)
        # Only the owner's own active reports; anything else looks "not found".
        if report is None or report.owner_id != user.id or report.status != ReportStatus.LOST:
            raise ValidationError(
                "Please choose one of your active lost pet reports.", "lost_report_id"
            )
        req.report = report
        if request.form.get("use_report_photo"):
            req.upload = None  # "Scan with the report photo" ignores a chosen file

    if req.upload is None:
        if req.report is None:
            raise ValidationError("Please upload or capture a photo of the pet.", "photo")
        if req.report.primary_photo is None:
            raise ValidationError(
                "This report has no photo yet. Please upload a photo of your pet.", "photo"
            )

    lat_raw, lng_raw = request.form.get("lat"), request.form.get("lng")
    if (lat_raw or "").strip() and (lng_raw or "").strip():
        try:
            lat, lng = float(lat_raw), float(lng_raw)
        except ValueError:
            lat = lng = None
        if lat is not None and in_service_area(lat, lng):
            req.lat, req.lng = lat, lng
        else:
            req.warnings.append(
                "Your location is outside Tacloban City, so results are not ranked by distance."
            )
    if req.lat is None and req.report is not None and req.report.has_location:
        # Rank by distance from where the pet was last seen (SPEC 5.3).
        req.lat, req.lng = req.report.last_seen_lat, req.report.last_seen_lng
    return req


def _scans_using(photo: Photo) -> list[Scan]:
    return list(db.session.scalars(sa.select(Scan).where(Scan.query_photo_id == photo.id)))


def perform_scan(user: Any, req: ScanRequest) -> Scan:
    """Save the photo, run the matching pipeline and commit. Returns the Scan.

    Raises ``storage_service.ImageValidationError`` or ``matching_service.ScanRejected``.
    A rejected upload (not one dog/cat, maybe not even an animal) is deleted right
    away; the rejected Scan row the engine saved keeps its pre-check answer.
    """
    uploaded = req.upload is not None
    if uploaded:
        photo = storage_service.save_image(
            req.upload,
            owner_type=PhotoOwner.SCAN,
            folder=storage_service.default_folder(PhotoOwner.SCAN),
        )
    else:
        photo = req.report.primary_photo
    try:
        scan = matching_service.run_scan(
            user=user,
            mode=req.mode,
            photo=photo,
            lat=req.lat,
            lng=req.lng,
            source_lost_report=req.report,
            trigger=ScanTrigger.MANUAL,
        )
    except matching_service.ScanRejected:
        if uploaded:
            for kept in _scans_using(photo):
                kept.query_photo_id = None
            db.session.flush()
            storage_service.delete_photo(photo)
        db.session.commit()
        raise
    except Exception:
        db.session.rollback()
        raise
    if uploaded and photo.owner_type == PhotoOwner.SCAN and photo.owner_id is None:
        photo.attach_to(PhotoOwner.SCAN, scan.id)
    db.session.commit()
    return scan


# --- Results -------------------------------------------------------------------------------


def _visible_scan(scan_id: int) -> Scan | None:
    scan = db.session.get(Scan, scan_id)
    if scan is None or not policy_service.can_view_scan(current_user, scan):
        return None
    return scan


def scan_warnings(scan: Scan) -> list[str]:
    """Friendly notes about photo quality and the Gemini fallback."""
    warnings: list[str] = []
    precheck = scan.precheck or {}
    if precheck.get("image_quality") == "poor":
        issues = [str(i) for i in precheck.get("quality_issues") or [] if i]
        detail = f" ({', '.join(issues)})" if issues else ""
        warnings.append(
            f"The photo quality looks poor{detail}. Results may be less accurate; "
            "try a clearer photo in good light if you can."
        )
    if scan.gemini_ok is False:
        warnings.append(
            "Photo comparison was unavailable for this scan, so similarity is based on the "
            "listed details only."
        )
    return warnings


def _sighting_extra(sighting: Sighting, viewer: Any, scan: Scan) -> dict[str, Any]:
    """What an owner needs to act on a sighting result (SPEC 5.2), per contact_pref."""
    report = sighting_routes.owner_report_for(viewer, scan)
    shares_phone = sighting.shares_phone
    return {
        "id": sighting.id,
        "seen_at_label": format_datetime(sighting.seen_at),
        "seen_ago": timeago(sighting.seen_at),
        "seen_at": sighting.seen_at.isoformat() + "Z" if sighting.seen_at else None,
        "seen_place": sighting.seen_place,
        "lat": sighting.seen_lat,
        "lng": sighting.seen_lng,
        "view_url": view_url(sighting.seen_lat, sighting.seen_lng),
        "directions_url": directions_url(sighting.seen_lat, sighting.seen_lng),
        "condition": sighting.condition,
        "condition_label": AnimalCondition.label(sighting.condition),
        "is_holding_animal": sighting.is_holding_animal,
        "notes": sighting.notes,
        "contact_name": sighting.contact_name,
        "contact_pref": sighting.contact_pref,
        "contact_pref_label": ContactPref.label(sighting.contact_pref),
        "contact_phone": sighting.contact_phone if shares_phone else None,
        "message_url": sighting_routes.message_finder_url(sighting, report),
        "detail_url": url_for("sightings.detail", sighting_id=sighting.id),
        "status": sighting.status,
    }


def build_results(
    scan: Scan, viewer: Any, *, include_weak: bool = False, log_access: bool = True
) -> tuple[list[dict], int]:
    """(result dicts to show, number of weaker results hidden or shown).

    Found-mode scans only ever show Lost Pet Reports; lost-mode scans only
    Sightings the viewer may see. Sightings shown are logged (caller commits).
    """
    rows = matching_service.results_for_display(scan, include_weak=True)
    visible_ids = {
        r.get("result_id") for r in matching_service.results_for_display(scan, include_weak=False)
    }
    wanted_type = (
        CandidateType.LOST_REPORT if scan.mode == ScanMode.FOUND else CandidateType.SIGHTING
    )
    shown: list[dict] = []
    weak_count = 0
    for row in rows:
        if row.get("candidate_type") != wanted_type:
            continue  # privacy: a found-mode scan must never reveal a sighting
        is_weak = row.get("result_id") not in visible_ids
        if wanted_type == CandidateType.LOST_REPORT:
            report = db.session.get(LostReport, row.get("candidate_id"))
            if report is None or not policy_service.can_view_report(viewer, report):
                continue
            if report.owner_id == getattr(viewer, "id", None):
                row = {**row, "message_url": None}
        else:
            sighting = db.session.get(Sighting, row.get("candidate_id"))
            if sighting is None or not policy_service.can_view_sighting(viewer, sighting):
                continue
            if sighting.status == SightingStatus.EXPIRED and sighting.finder_id != viewer.id:
                continue
            row = {**row, "sighting": _sighting_extra(sighting, viewer, scan)}
            row["detail_url"] = row["sighting"]["detail_url"]
            row["message_url"] = row["sighting"]["message_url"]
        if is_weak:
            weak_count += 1
            if not include_weak:
                continue
        row["is_weak"] = is_weak
        if log_access and wanted_type == CandidateType.SIGHTING:
            sighting_obj = db.session.get(Sighting, row["candidate_id"])
            if sighting_obj.finder_id != viewer.id:
                log_sighting_access(sighting_obj, viewer, scan=scan)
        shown.append(row)
    return shown, weak_count


def _jsonable(value: Any) -> Any:
    """Make result dicts JSON-safe (datetimes -> ISO, Decimals -> float)."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def scan_json(scan: Scan, results: list[dict], weak_count: int, warnings: list[str]) -> dict:
    photo = scan.query_photo
    return {
        "scan_id": scan.id,
        "url": url_for("scanner.results", scan_id=scan.id),
        "scan": {
            "id": scan.id,
            "mode": scan.mode,
            "trigger": scan.trigger,
            "status": scan.status,
            "result_count": scan.result_count,
            "gemini_ok": scan.gemini_ok,
            "created_at": scan.created_at.isoformat() + "Z" if scan.created_at else None,
            "photo_url": storage_service.photo_url(photo, "thumb") if photo else None,
            "source_lost_report_id": scan.source_lost_report_id,
        },
        "results": _jsonable(results),
        "weak_count": weak_count,
        "warnings": warnings,
    }


def _pool_size(mode: str) -> int:
    """How many candidates a scan in this mode compares against (progress text only)."""
    if mode == ScanMode.FOUND:
        stmt = sa.select(sa.func.count(LostReport.id)).where(
            LostReport.status == ReportStatus.LOST, LostReport.is_hidden.is_(False)
        )
    else:
        stmt = sa.select(sa.func.count(Sighting.id)).where(
            Sighting.status == SightingStatus.ACTIVE, Sighting.is_hidden.is_(False)
        )
    return db.session.scalar(stmt) or 0


def _stages(mode: str) -> list[str]:
    n = _pool_size(mode)
    noun = "reports" if mode == ScanMode.FOUND else "sightings"
    return [s.replace("{n} reports", f"{n} {noun}") for s in strings.SCAN_STAGES]


def _choose_page(
    mode: str | None,
    *,
    error: str | None = None,
    status: int = 200,
    selected_report_id: int | None = None,
):
    user = current_user._get_current_object()
    reports = active_reports_for(user) if mode == ScanMode.LOST else []
    if mode == ScanMode.LOST and selected_report_id is None and reports:
        selected_report_id = reports[0].id
    report_items = [
        {
            "id": r.id,
            "name": r.pet_name,
            "case_number": r.case_number,
            "species": r.species,
            "thumb": (
                storage_service.photo_url(r.primary_photo, "thumb") if r.primary_photo else None
            ),
            "has_photo": r.primary_photo is not None,
        }
        for r in reports
    ]
    return (
        render_template(
            "scanner/choose.html",
            mode=mode,
            reports=report_items,
            selected_report_id=selected_report_id,
            stages=_stages(mode) if mode else [],
            error=error,
        ),
        status,
    )


# --- Pages ---------------------------------------------------------------------------------


@bp.get("/scan")
@login_required
def index():
    """Entry point from the mobile bottom nav "Scan": choose Found or Lost Pet Scanner."""
    return _choose_page(None)


@bp.get("/scan/found")
@login_required
def found():
    """Match a Found Pet (scanner/choose.html, mode "found"): Upload Photo / Capture Photo."""
    return _choose_page(ScanMode.FOUND)


@bp.get("/scan/lost")
@login_required
def lost():
    """Scan for My Lost Pet (scanner/choose.html, mode "lost"); optional ?lost_report_id=."""
    return _choose_page(
        ScanMode.LOST, selected_report_id=parse_int(request.args.get("lost_report_id"))
    )


@bp.post("/scan")
@login_required
@SCAN_LIMIT
def submit():
    """No-JS fallback: multipart form (mode, photo, lat, lng, lost_report_id) -> results page."""
    user = current_user._get_current_object()
    mode = request.form.get("mode") if request.form.get("mode") in ScanMode.ALL else None
    selected = parse_int(request.form.get("lost_report_id"))
    try:
        req = parse_scan_request(user)
        scan = perform_scan(user, req)
    except ValidationError as exc:
        return _choose_page(mode, error=exc.message, status=422, selected_report_id=selected)
    except storage_service.ImageValidationError as exc:
        return _choose_page(mode, error=exc.message, status=422, selected_report_id=selected)
    except matching_service.ScanRejected as exc:
        return _choose_page(mode, error=exc.message, status=422, selected_report_id=selected)
    for warning in req.warnings:
        flash(warning, "warning")
    return redirect(url_for("scanner.results", scan_id=scan.id))


def render_results(scan: Scan, *, include_weak: bool, extra_warnings: list[str] | None = None):
    """Render scanner/results.html for a scan the current user may see."""
    user = current_user._get_current_object()
    warnings = list(extra_warnings or []) + scan_warnings(scan)
    results: list[dict] = []
    weak_count = 0
    if scan.status == ScanStatus.COMPLETE:
        results, weak_count = build_results(scan, user, include_weak=include_weak)
        db.session.commit()  # access log rows
    relevant = [r for r in results if not r.get("is_weak")]
    weak = [r for r in results if r.get("is_weak")]

    photo = scan.query_photo
    can_report_sighting = False
    existing_sighting = None
    if scan.mode == ScanMode.FOUND and scan.user_id == user.id:
        prefill_scan = sighting_routes.scan_for_prefill(scan.id, user)
        if prefill_scan is not None:
            available, existing = sighting_routes.scan_photo_state(prefill_scan)
            can_report_sighting = available is not None
            if existing is not None and existing.finder_id == user.id:
                existing_sighting = existing
    sighting_values = (
        sighting_routes.default_form_values(user, scan) if can_report_sighting else None
    )
    return render_template(
        "scanner/results.html",
        scan=scan,
        relevant=relevant,
        weak=weak,
        weak_count=weak_count,
        include_weak=include_weak,
        warnings=warnings,
        scanned_photo_url=storage_service.photo_url(photo, "thumb") if photo else None,
        source_report=scan.source_lost_report,
        can_report_sighting=can_report_sighting,
        existing_sighting=existing_sighting,
        values=sighting_values,
        errors={},
        **(sighting_routes.form_context() if can_report_sighting else {}),
    )


@bp.get("/scans/<int:scan_id>")
@login_required
def results(scan_id: int):
    """Ranked results (scanner/results.html); ?weak=1 also shows results below 40 %."""
    scan = _visible_scan(scan_id)
    if scan is None:
        abort(404)
    return render_results(scan, include_weak=request.args.get("weak") == "1")


# --- JSON API ------------------------------------------------------------------------------


@bp.post("/api/scans")
@api_login_required
@SCAN_LIMIT
def api_create():
    """POST /api/scans (multipart: mode, photo, lat, lng, lost_report_id) -> 201 scan JSON."""
    user = current_user._get_current_object()
    try:
        req = parse_scan_request(user)
    except ValidationError as exc:
        return json_error("validation_error", exc.message, 422, {"field": exc.field})
    try:
        scan = perform_scan(user, req)
    except storage_service.ImageValidationError as exc:
        return json_error(exc.code, exc.message, 422)
    except matching_service.ScanRejected as exc:
        return json_error(exc.code, exc.message, 422, exc.details)
    results_, weak_count = build_results(scan, user)
    db.session.commit()
    body = scan_json(scan, results_, weak_count, req.warnings + scan_warnings(scan))
    return jsonify(body), 201


@bp.get("/api/scans/<int:scan_id>")
@api_login_required
def api_detail(scan_id: int):
    """GET /api/scans/<id> -> {"scan": {...}, "results": [...]} (scan owner or admin)."""
    scan = _visible_scan(scan_id)
    if scan is None:
        return json_error("not_found", "Scan not found.", 404)
    results_: list[dict] = []
    weak_count = 0
    if scan.status == ScanStatus.COMPLETE:
        results_, weak_count = build_results(
            scan, current_user._get_current_object(), include_weak=request.args.get("weak") == "1"
        )
        db.session.commit()
    return jsonify(scan_json(scan, results_, weak_count, scan_warnings(scan)))
