"""Private Pet Sightings: create, detail, edit, status, /api/sightings, expiry job (SPEC 5.4).

Privacy rules (SPEC 5.4, 17.6):

* Sightings are NEVER listed publicly and never enter the Found Pet Scanner.
* Every view goes through ``policy_service.can_view_sighting``. A viewer who
  may not see a sighting gets **404**, never 403, so its existence is not revealed.
* Every view by someone other than the finder (an owner whose scan returned
  it, or an admin) is logged with ``audit_service.log_sighting_access``.
* A finder's contact details are shown to owners only through the channel the
  finder chose (``contact_pref``): in-app messages only, or also phone.

A sighting can be created from a Found Pet Scan (``?scan_id=``): the scanned
photo is *re-owned* by the new sighting (the same file, no copy), and the
scan's Gemini attributes pre-fill species/breed/colours.

This module also registers the ``expire_sightings`` job (job_service imports
it through ``HANDLER_MODULES``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import config_limit, db, limiter
from app.models import (
    AnimalCondition,
    CandidateType,
    ContactPref,
    LostReport,
    PetSize,
    Photo,
    PhotoOwner,
    ReportStatus,
    Scan,
    ScanMode,
    ScanResult,
    ScanStatus,
    Sighting,
    SightingStatus,
    Species,
)
from app.services import policy_service, storage_service
from app.services.audit_service import log_event, log_sighting_access
from app.services.job_service import enqueue, register
from app.utils.auth import api_login_required
from app.utils.http import json_error
from app.utils.maps import directions_url, view_url
from app.utils.phone import normalize_ph
from app.utils.time import local_now, to_local, utcnow
from app.utils.validators import (
    ValidationError,
    clean_text,
    parse_bool,
    parse_choice,
    parse_coordinates,
    parse_int,
    parse_local_datetime,
)

log = logging.getLogger(__name__)

bp = Blueprint("sightings", __name__)

NOTES_MAX = 1000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Finder/admin status changes. "expired" doubles as "withdrawn" (stops matching).
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    SightingStatus.ACTIVE: (SightingStatus.MATCHED, SightingStatus.EXPIRED),
    SightingStatus.MATCHED: (SightingStatus.ACTIVE,),
    SightingStatus.EXPIRED: (SightingStatus.ACTIVE,),
}
STATUS_MESSAGES = {
    SightingStatus.MATCHED: "Marked as matched. Thank you for helping a pet get home!",
    SightingStatus.EXPIRED: "Sighting withdrawn. It will no longer appear in owners' scans.",
    SightingStatus.ACTIVE: "Sighting is active again for another {days} days.",
}


# --- Helpers used by scanner.py too --------------------------------------------------------


def scan_for_prefill(scan_id: Any, user: Any) -> Scan | None:
    """The user's own completed Found Pet Scan with a query photo, or None.

    Only scans the user ran themselves (not admin access to others' scans)
    may become a sighting, so nobody can re-own somebody else's photo.
    """
    sid = parse_int(scan_id)
    if sid is None or not policy_service.is_logged_in(user):
        return None
    scan = db.session.get(Scan, sid)
    if (
        scan is None
        or scan.user_id != user.id
        or scan.mode != ScanMode.FOUND
        or scan.status != ScanStatus.COMPLETE
        or scan.query_photo_id is None
    ):
        return None
    return scan


def scan_photo_state(scan: Scan) -> tuple[Photo | None, Sighting | None]:
    """(photo still available for a new sighting, sighting it already belongs to)."""
    photo = scan.query_photo
    if photo is None:
        return None, None
    if photo.owner_type == PhotoOwner.SCAN and photo.owner_id in (None, scan.id):
        return photo, None
    if photo.owner_type == PhotoOwner.SIGHTING and photo.owner_id is not None:
        return None, db.session.get(Sighting, photo.owner_id)
    return None, None


def _scan_prefill(scan: Scan) -> dict[str, Any]:
    """Species/breed/colours/size/location suggested by the scan (user can edit them)."""
    precheck = scan.precheck or {}
    attrs = scan.attributes or {}
    values: dict[str, Any] = {}
    species = attrs.get("species") or precheck.get("species")
    if species in Species.ALL:
        values["species"] = species
    if isinstance(attrs.get("breed_guess"), str):
        values["breed"] = attrs["breed_guess"][:80]
    colors = attrs.get("primary_colors")
    if isinstance(colors, list) and colors:
        text = ", ".join(str(c) for c in colors if c)
        pattern = attrs.get("coat_pattern")
        if isinstance(pattern, str) and pattern and pattern not in ("solid", "unknown"):
            text = f"{text} ({pattern})"
        values["colors"] = text[:120]
    if attrs.get("size_estimate") in PetSize.ALL:
        values["size"] = attrs["size_estimate"]
    if scan.scan_lat is not None and scan.scan_lng is not None:
        values["lat"], values["lng"] = scan.scan_lat, scan.scan_lng
    return values


def default_form_values(user: Any, scan: Scan | None = None) -> dict[str, Any]:
    """Initial values of the sighting form: contact from the account, now as date/time."""
    now = local_now()
    values: dict[str, Any] = {
        "species": "",
        "breed": "",
        "colors": "",
        "size": "",
        "condition": AnimalCondition.UNKNOWN,
        "is_holding_animal": False,
        "notes": "",
        "lat": None,
        "lng": None,
        "place": "",
        "seen_date": now.strftime("%Y-%m-%d"),
        "seen_time": now.strftime("%H:%M"),
        "contact_name": getattr(user, "full_name", "") or "",
        "contact_email": getattr(user, "email", "") or "",
        "contact_phone": getattr(user, "mobile_e164", "") or "",
        "contact_pref": ContactPref.IN_APP,
        "consent": False,
    }
    if scan is not None:
        values.update(_scan_prefill(scan))
    return values


def values_from_sighting(sighting: Sighting) -> dict[str, Any]:
    """Form values for the edit page."""
    seen = to_local(sighting.seen_at)
    return {
        "species": sighting.species,
        "breed": sighting.breed or "",
        "colors": sighting.colors or "",
        "size": sighting.size or "",
        "condition": sighting.condition,
        "is_holding_animal": sighting.is_holding_animal,
        "notes": sighting.notes or "",
        "lat": sighting.seen_lat,
        "lng": sighting.seen_lng,
        "place": sighting.seen_place or "",
        "seen_date": seen.strftime("%Y-%m-%d") if seen else "",
        "seen_time": seen.strftime("%H:%M") if seen else "",
        "contact_name": sighting.contact_name or "",
        "contact_email": sighting.contact_email or "",
        "contact_phone": sighting.contact_phone or "",
        "contact_pref": sighting.contact_pref,
        "consent": True,
    }


def form_context() -> dict[str, Any]:
    """Choices and limits the sighting form template needs."""
    return {
        "species_options": [(s, Species.label(s), s) for s in Species.ALL],
        "condition_options": AnimalCondition.choices(),
        "size_options": [("", "Not sure")] + PetSize.choices(),
        "contact_pref_options": ContactPref.choices(),
        "max_photos": current_app.config["MAX_PHOTOS_PER_SIGHTING"],
        "today": local_now().strftime("%Y-%m-%d"),
    }


def _text(values: Mapping, key: str) -> str:
    value = values.get(key)
    return "" if value is None else str(value)


def parse_sighting_form(
    values: Mapping, *, require_consent: bool = True
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the sighting fields (form or JSON). Returns (clean data, errors by field)."""
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def run(error_key: str, func, /, *args, **kwargs):  # noqa: ANN001, ANN202
        """Call a validator; record its message under its field (or ``error_key``)."""
        try:
            return func(*args, **kwargs)
        except ValidationError as exc:
            errors.setdefault(exc.field or error_key, exc.message)
            return None

    data["species"] = run(
        "species", parse_choice, _text(values, "species"), Species.ALL, field="species"
    )
    data["breed"] = run("breed", clean_text, _text(values, "breed"), max_length=80, field="breed")
    data["colors"] = run(
        "colors", clean_text, _text(values, "colors"), max_length=120, field="colors"
    )
    data["size"] = run(
        "size", parse_choice, _text(values, "size"), PetSize.ALL, field="size", required=False
    )
    data["condition"] = (
        run(
            "condition",
            parse_choice,
            _text(values, "condition"),
            AnimalCondition.ALL,
            field="condition",
            required=False,
        )
        or AnimalCondition.UNKNOWN
    )
    data["is_holding_animal"] = parse_bool(values.get("is_holding_animal"))
    data["notes"] = run(
        "notes", clean_text, _text(values, "notes"), max_length=NOTES_MAX, field="notes"
    )

    coords = run("location", parse_coordinates, values.get("lat"), values.get("lng"))
    if coords:
        data["seen_lat"], data["seen_lng"] = coords
    data["seen_place"] = run("location", clean_text, _text(values, "place"), max_length=160)

    if not _text(values, "seen_date").strip():
        errors["seen_date"] = "Please enter the date you saw the pet."
    else:
        seen_at = run(
            "seen_date",
            parse_local_datetime,
            _text(values, "seen_date"),
            _text(values, "seen_time"),
            field="seen_date",
        )
        if seen_at is not None:
            if seen_at < utcnow() - timedelta(days=365):
                errors["seen_date"] = "Please enter a date within the last year."
            else:
                data["seen_at"] = seen_at

    data["contact_name"] = run(
        "contact_name",
        clean_text,
        _text(values, "contact_name"),
        max_length=120,
        required=True,
        field="contact_name",
    )
    email = run("contact_email", clean_text, _text(values, "contact_email"), max_length=255)
    if email and not EMAIL_RE.match(email):
        errors["contact_email"] = "Enter a valid email address."
    data["contact_email"] = email.lower() if email else None
    phone_raw = _text(values, "contact_phone").strip()
    data["contact_phone"] = None
    if phone_raw:
        try:
            data["contact_phone"] = normalize_ph(phone_raw)
        except ValueError as exc:
            errors["contact_phone"] = str(exc) or "Enter a valid PH mobile number."
    data["contact_pref"] = (
        run(
            "contact_pref",
            parse_choice,
            _text(values, "contact_pref"),
            ContactPref.ALL,
            field="contact_pref",
            required=False,
        )
        or ContactPref.IN_APP
    )
    if data["contact_pref"] == ContactPref.PHONE and not data["contact_phone"]:
        errors.setdefault("contact_phone", "Add a contact number, or choose in-app messages only.")
    if require_consent and not parse_bool(values.get("consent")):
        errors["consent"] = "Please agree before submitting the sighting."
    return data, errors


def _uploaded_files() -> list:
    return [f for f in request.files.getlist("photos") if f and f.filename]


def create_sighting(
    user: Any, data: dict[str, Any], *, uploads: list, scan: Scan | None = None
) -> Sighting:
    """Create a sighting with its photos, schedule background matching and commit.

    ``scan``: the finder's own Found Pet Scan whose photo becomes the primary
    photo. Raises ``ValidationError(field="photos")`` for photo problems; nothing
    is saved in that case.
    """
    scan_photo = scan_photo_state(scan)[0] if scan is not None else None
    max_photos = current_app.config["MAX_PHOTOS_PER_SIGHTING"]
    total = len(uploads) + (1 if scan_photo else 0)
    if total == 0:
        raise ValidationError("Please add at least one photo of the pet.", "photos")
    if total > max_photos:
        raise ValidationError(f"You can add up to {max_photos} photos.", "photos")

    sighting = Sighting(finder_id=user.id, status=SightingStatus.ACTIVE, **data)
    db.session.add(sighting)
    db.session.flush()
    sighting.set_default_expiry(current_app.config["SIGHTING_EXPIRY_DAYS"])

    saved: list[Photo] = []
    order = 0
    if scan_photo is not None:
        scan_photo.attach_to(PhotoOwner.SIGHTING, sighting.id)
        scan_photo.is_primary = True
        scan_photo.sort_order = 0
        if scan.attributes and not scan_photo.ai_attributes:
            scan_photo.ai_attributes = scan.attributes
        if scan.attributes:
            sighting.ai_attributes = scan.attributes
        order = 1
    try:
        for i, upload in enumerate(uploads):
            saved.append(
                storage_service.save_image(
                    upload,
                    owner_type=PhotoOwner.SIGHTING,
                    owner_id=sighting.id,
                    folder=storage_service.default_folder(PhotoOwner.SIGHTING, sighting.id),
                    is_primary=(order + i == 0),
                    sort_order=order + i,
                )
            )
    except storage_service.ImageValidationError as exc:
        for photo in saved:  # remove files already written for this request
            storage_service.delete_photo(photo)
        db.session.rollback()
        raise ValidationError(exc.message, "photos") from None

    log_event(
        "sighting.created",
        actor=user,
        target=sighting,
        details={"scan_id": scan.id} if scan_photo is not None else None,
    )
    enqueue("match_sighting", {"sighting_id": sighting.id})
    db.session.commit()
    return sighting


def _get_visible_sighting(sighting_id: int) -> Sighting:
    """The sighting if the current user may see it; otherwise 404 (never 403)."""
    sighting = db.session.get(Sighting, sighting_id)
    if sighting is None or not policy_service.can_view_sighting(current_user, sighting):
        abort(404)
    return sighting


def _access_scan_id(user: Any, sighting: Sighting) -> int | None:
    """Most recent scan of ``user`` that returned ``sighting`` (for the access log)."""
    return db.session.scalar(
        sa.select(Scan.id)
        .join(ScanResult, ScanResult.scan_id == Scan.id)
        .where(
            Scan.user_id == user.id,
            ScanResult.candidate_type == CandidateType.SIGHTING,
            ScanResult.candidate_id == sighting.id,
        )
        .order_by(Scan.id.desc())
        .limit(1)
    )


def record_access(sighting: Sighting, viewer: Any, scan: Any = None) -> None:
    """Log a view by anyone but the finder (owners through scans, admins). Caller commits."""
    if sighting.finder_id == viewer.id:
        return
    if scan is None:
        scan = _access_scan_id(viewer, sighting)
    log_sighting_access(sighting, viewer, scan=scan)


def owner_report_for(user: Any, scan: Scan | None = None) -> LostReport | None:
    """The owner's case a "Message finder" conversation should belong to."""
    if scan is not None and scan.source_lost_report_id:
        report = db.session.get(LostReport, scan.source_lost_report_id)
        if report is not None and report.owner_id == user.id:
            return report
    return db.session.scalar(
        sa.select(LostReport)
        .where(LostReport.owner_id == user.id, LostReport.status == ReportStatus.LOST)
        .order_by(LostReport.created_at.desc(), LostReport.id.desc())
        .limit(1)
    )


def message_finder_url(sighting: Sighting, report: LostReport | None) -> str:
    """Link for the owner's "Message finder" button (messages.new)."""
    params: dict[str, Any] = {"sighting_id": sighting.id}
    if report is not None:
        params["lost_report_id"] = report.id
    return url_for("messages.new", **params)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def sighting_json(sighting: Sighting, viewer: Any) -> dict[str, Any]:
    """API representation. Contact details follow the finder's contact preference
    for owners; the finder and admins see everything they entered."""
    manage = policy_service.can_manage_sighting(viewer, sighting)
    data: dict[str, Any] = {
        "id": sighting.id,
        "url": url_for("sightings.detail", sighting_id=sighting.id),
        "species": sighting.species,
        "breed": sighting.breed,
        "colors": sighting.colors,
        "size": sighting.size,
        "condition": sighting.condition,
        "is_holding_animal": sighting.is_holding_animal,
        "notes": sighting.notes,
        "seen_lat": sighting.seen_lat,
        "seen_lng": sighting.seen_lng,
        "seen_place": sighting.seen_place,
        "seen_at": _iso(sighting.seen_at),
        "status": sighting.status,
        "expires_at": _iso(sighting.expires_at),
        "created_at": _iso(sighting.created_at),
        "view_url": view_url(sighting.seen_lat, sighting.seen_lng),
        "directions_url": directions_url(sighting.seen_lat, sighting.seen_lng),
        "photos": [
            {
                "id": p.id,
                "url": storage_service.photo_url(p),
                "thumb_url": storage_service.photo_url(p, "thumb"),
            }
            for p in sighting.photos
        ],
        "contact": {
            "name": sighting.contact_name,
            "pref": sighting.contact_pref,
            "phone": sighting.contact_phone if (manage or sighting.shares_phone) else None,
        },
        "is_mine": sighting.finder_id == getattr(viewer, "id", None),
    }
    if manage:
        data["contact"]["email"] = sighting.contact_email
        data["is_hidden"] = sighting.is_hidden
    return data


def _form_errors_summary(errors: dict[str, str]) -> list[str]:
    return list(dict.fromkeys(errors.values()))


# --- Pages ---------------------------------------------------------------------------------


@bp.route("/sightings/new", methods=["GET", "POST"])
@login_required
@limiter.shared_limit(config_limit("RATE_LIMIT_SIGHTINGS"), scope="sightings", methods=["POST"])
def new():
    """Report a Pet Sighting (standalone page; also a modal on scan results).
    Optional ?scan_id= pre-fills the photo from the user's own Found Pet Scan."""
    user = current_user._get_current_object()
    scan_id = request.values.get("scan_id")
    scan = scan_for_prefill(scan_id, user) if scan_id else None
    if scan_id and scan is None:
        flash("That scan cannot be used for a sighting. Please add a photo instead.", "warning")
    scan_photo = None
    if scan is not None:
        scan_photo, existing = scan_photo_state(scan)
        if existing is not None and existing.finder_id == user.id:
            flash("You already reported this scanned pet as a sighting.", "info")
            return redirect(url_for("sightings.detail", sighting_id=existing.id))
        if scan_photo is None:
            scan = None

    values = default_form_values(user, scan)
    errors: dict[str, str] = {}
    status = 200
    if request.method == "POST":
        values.update({k: request.form.get(k) for k in values if k in request.form})
        values["is_holding_animal"] = parse_bool(request.form.get("is_holding_animal"))
        values["consent"] = parse_bool(request.form.get("consent"))
        data, errors = parse_sighting_form(request.form)
        uploads = _uploaded_files()
        if not errors:
            try:
                sighting = create_sighting(user, data, uploads=uploads, scan=scan)
            except ValidationError as exc:
                errors[exc.field or "photos"] = exc.message
            else:
                flash("Thank you! Your pet sighting was saved.", "success")
                return redirect(url_for("sightings.detail", sighting_id=sighting.id, created=1))
        status = 422
    return (
        render_template(
            "sightings/new.html",
            values=values,
            errors=errors,
            error_list=_form_errors_summary(errors),
            scan=scan,
            scan_photo_url=storage_service.photo_url(scan_photo, "thumb") if scan_photo else None,
            **form_context(),
        ),
        status,
    )


@bp.get("/sightings/<int:sighting_id>")
@login_required
def detail(sighting_id: int):
    """Private sighting details (finder, admins, owners whose scan returned it)."""
    sighting = _get_visible_sighting(sighting_id)
    user = current_user._get_current_object()
    is_finder = sighting.finder_id == user.id
    access_scan = None
    if not is_finder:
        scan_id = _access_scan_id(user, sighting)
        access_scan = db.session.get(Scan, scan_id) if scan_id else None
        log_sighting_access(sighting, user, scan=access_scan)
        db.session.commit()
    can_manage = policy_service.can_manage_sighting(user, sighting)
    # Owners who reached the sighting through a scan (or a shared conversation) may
    # message the finder; an admin moderating it gets no message button.
    message_url = None
    if not is_finder and (access_scan is not None or not policy_service.is_admin(user)):
        message_url = message_finder_url(sighting, owner_report_for(user, access_scan))
    photos = [
        {"url": storage_service.photo_url(p), "thumb": storage_service.photo_url(p, "thumb")}
        for p in sighting.photos
    ]
    return render_template(
        "sightings/detail.html",
        sighting=sighting,
        photos=photos,
        is_finder=is_finder,
        can_manage=can_manage,
        message_url=message_url,
        show_phone=can_manage or sighting.shares_phone,
        show_email=can_manage,
        created=bool(request.args.get("created")) and is_finder,
        next_statuses=STATUS_TRANSITIONS.get(sighting.status, ()) if can_manage else (),
        expiry_days=current_app.config["SIGHTING_EXPIRY_DAYS"],
    )


@bp.route("/sightings/<int:sighting_id>/edit", methods=["GET", "POST"])
@login_required
def edit(sighting_id: int):
    """Finder/admin edits the sighting details (photos stay as they are)."""
    sighting = _get_visible_sighting(sighting_id)
    user = current_user._get_current_object()
    if not policy_service.can_manage_sighting(user, sighting):
        abort(403)
    values = values_from_sighting(sighting)
    errors: dict[str, str] = {}
    status = 200
    if request.method == "POST":
        values.update({k: request.form.get(k) for k in values if k in request.form})
        values["is_holding_animal"] = parse_bool(request.form.get("is_holding_animal"))
        data, errors = parse_sighting_form(request.form, require_consent=False)
        if not errors:
            for key, value in data.items():
                setattr(sighting, key, value)
            log_event("sighting.updated", actor=user, target=sighting)
            db.session.commit()
            flash("Sighting updated.", "success")
            return redirect(url_for("sightings.detail", sighting_id=sighting.id))
        status = 422
    return (
        render_template(
            "sightings/edit.html",
            sighting=sighting,
            values=values,
            errors=errors,
            error_list=_form_errors_summary(errors),
            **form_context(),
        ),
        status,
    )


@bp.post("/sightings/<int:sighting_id>/status")
@login_required
def update_status(sighting_id: int):
    """Finder/admin: mark matched, withdraw (expired) or reactivate (field: status)."""
    sighting = _get_visible_sighting(sighting_id)
    user = current_user._get_current_object()
    if not policy_service.can_manage_sighting(user, sighting):
        abort(403)
    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in STATUS_TRANSITIONS.get(sighting.status, ()):
        flash("That status change is not allowed.", "error")
        return redirect(url_for("sightings.detail", sighting_id=sighting.id))
    old_status = sighting.status
    sighting.status = new_status
    days = current_app.config["SIGHTING_EXPIRY_DAYS"]
    if new_status == SightingStatus.ACTIVE:
        sighting.expires_at = utcnow() + timedelta(days=days)
    event = (
        "sighting.withdrawn" if new_status == SightingStatus.EXPIRED else "sighting.status_changed"
    )
    log_event(event, actor=user, target=sighting, details={"from": old_status, "to": new_status})
    if new_status == SightingStatus.ACTIVE and not sighting.is_hidden:
        enqueue("match_sighting", {"sighting_id": sighting.id})
    db.session.commit()
    flash(STATUS_MESSAGES[new_status].format(days=days), "success")
    return redirect(url_for("sightings.detail", sighting_id=sighting.id))


# --- JSON API ------------------------------------------------------------------------------


@bp.post("/api/sightings")
@api_login_required
@limiter.shared_limit(config_limit("RATE_LIMIT_SIGHTINGS"), scope="sightings", methods=["POST"])
def api_create():
    """POST /api/sightings (multipart: fields + photos, and/or scan_id) -> 201 {"id", "url"}."""
    user = current_user._get_current_object()
    values: Mapping = request.form if request.form else (request.get_json(silent=True) or {})
    scan = None
    if values.get("scan_id"):
        scan = scan_for_prefill(values.get("scan_id"), user)
        if scan is None:
            return json_error("validation_error", "That scan cannot be used for a sighting.", 422)
        photo, existing = scan_photo_state(scan)
        if photo is None:
            details = (
                {"sighting_id": existing.id} if existing and existing.finder_id == user.id else None
            )
            return json_error(
                "already_reported",
                "This scanned pet was already reported as a sighting.",
                409,
                details,
            )
    data, errors = parse_sighting_form(values)
    if errors:
        return json_error(
            "validation_error", _form_errors_summary(errors)[0], 422, {"fields": errors}
        )
    try:
        sighting = create_sighting(user, data, uploads=_uploaded_files(), scan=scan)
    except ValidationError as exc:
        return json_error(
            "validation_error", exc.message, 422, {"fields": {exc.field: exc.message}}
        )
    body = {
        "id": sighting.id,
        "url": url_for("sightings.detail", sighting_id=sighting.id, created=1),
        "status": sighting.status,
        "expires_at": _iso(sighting.expires_at),
    }
    return jsonify(body), 201


@bp.get("/api/sightings/mine")
@api_login_required
def api_mine():
    """GET /api/sightings/mine -> {"sightings": [...]} (the finder's own sightings)."""
    user = current_user._get_current_object()
    rows = db.session.scalars(
        sa.select(Sighting)
        .where(Sighting.finder_id == user.id)
        .order_by(Sighting.created_at.desc(), Sighting.id.desc())
    )
    items = []
    for s in rows:
        photo = s.primary_photo
        items.append(
            {
                "id": s.id,
                "url": url_for("sightings.detail", sighting_id=s.id),
                "species": s.species,
                "breed": s.breed,
                "colors": s.colors,
                "condition": s.condition,
                "seen_place": s.seen_place,
                "seen_at": _iso(s.seen_at),
                "status": s.status,
                "is_hidden": s.is_hidden,
                "expires_at": _iso(s.expires_at),
                "thumb_url": storage_service.photo_url(photo, "thumb") if photo else None,
            }
        )
    return {"sightings": items}


@bp.get("/api/sightings/<int:sighting_id>")
@api_login_required
def api_detail(sighting_id: int):
    """GET /api/sightings/<id> (same visibility rules as the page; logs non-finder access)."""
    sighting = db.session.get(Sighting, sighting_id)
    user = current_user._get_current_object()
    if sighting is None or not policy_service.can_view_sighting(user, sighting):
        return json_error("not_found", "Sighting not found.", 404)
    if sighting.finder_id != user.id:
        record_access(sighting, user)
        db.session.commit()
    return {"sighting": sighting_json(sighting, user)}


# --- Background job ------------------------------------------------------------------------


def expire_sightings(now: datetime | None = None) -> int:
    """Mark active sightings whose ``expires_at`` has passed as "expired". Returns the count."""
    now = now or utcnow()
    result = db.session.execute(
        sa.update(Sighting)
        .where(
            Sighting.status == SightingStatus.ACTIVE,
            Sighting.expires_at.is_not(None),
            Sighting.expires_at <= now,
        )
        .values(status=SightingStatus.EXPIRED, updated_at=now)
        .execution_options(synchronize_session="fetch")
    )
    db.session.commit()
    count = result.rowcount or 0
    if count:
        log.info("expire_sightings: %d sighting(s) expired", count)
    return count


@register("expire_sightings")
def _job_expire_sightings(payload: dict) -> None:
    """payload: {}"""
    expire_sightings()
