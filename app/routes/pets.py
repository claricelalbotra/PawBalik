"""Lost Pet Reports: 5-step create wizard, public browse, edit, /api/lost-reports (SPEC 5.1).

Pages
    pets.browse   GET       /lost-pets                  public list (filters, near me, pages)
    pets.new      GET/POST  /lost-pets/new              5-step wizard (one long form without JS)
    pets.edit     GET/POST  /lost-pets/<id>/edit        owner or admin
JSON API
    pets.api_list    GET    /api/lost-reports?near=lat,lng&species=&q=&status=&radius=&page=
    pets.api_create  POST   /api/lost-reports (multipart, same fields as the form) -> 201
    pets.api_detail  GET    /api/lost-reports/<id>

Saving a report (SPEC 5.1): photos through storage_service.save_image (the first
photo is the primary photo), a case number from next_case_number() inside the same
transaction, status ``lost`` plus the first case_status_history row, then the
``extract_attributes`` and ``match_lost_report`` background jobs, an audit log row
and one commit. The helpers here (validation, JSON shapes, visibility lookups) are
also used by app/routes/cases.py.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload
from werkzeug.datastructures import FileStorage, MultiDict

from app.extensions import config_limit, db, limiter
from app.models import (
    LostReport,
    PetSex,
    PetSize,
    Photo,
    PhotoOwner,
    ReportStatus,
    Species,
    User,
)
from app.services import policy_service, storage_service
from app.services.audit_service import log_event
from app.services.job_service import enqueue
from app.utils.auth import api_login_required
from app.utils.cards import lost_report_card
from app.utils.case_numbers import next_case_number
from app.utils.geo import haversine_km, parse_near
from app.utils.http import json_error
from app.utils.phone import format_phone, mask_phone, normalize_ph, tel_href
from app.utils.time import local_now, to_local
from app.utils.validators import (
    ValidationError,
    clean_text,
    parse_bool,
    parse_choice,
    parse_coordinates,
    parse_int,
    parse_local_datetime,
)

bp = Blueprint("pets", __name__)

# --- Wizard layout (SPEC 16.8) -------------------------------------------------------------

WIZARD_STEPS = (
    "Pet Information",
    "Identifying Features",
    "Where & When Lost",
    "Contact & Reward",
    "Review and Submit",
)

# Which wizard step shows each field, so a server-side error re-opens the right step.
FIELD_STEPS = {
    "species": 1,
    "pet_name": 1,
    "breed": 1,
    "colors": 1,
    "size": 1,
    "sex": 1,
    "age_text": 1,
    "photos": 1,
    "identifying_features": 2,
    "location": 3,
    "place": 3,
    "last_seen_date": 3,
    "last_seen_time": 3,
    "contact_name": 4,
    "contact_email": 4,
    "contact_phone": 4,
    "show_phone": 4,
    "reward_amount": 4,
    "reward_text": 4,
}

BREED_SUGGESTIONS = {
    "dog": (
        "Aspin (mixed)",
        "Mixed breed",
        "Shih Tzu",
        "Labrador Retriever",
        "Golden Retriever",
        "Siberian Husky",
        "Pomeranian",
        "Chihuahua",
        "Beagle",
        "Poodle",
        "Pug",
        "Dachshund",
        "German Shepherd",
        "Japanese Spitz",
        "Belgian Malinois",
        "Rottweiler",
    ),
    "cat": (
        "Puspin (mixed)",
        "Mixed breed",
        "Persian",
        "Siamese",
        "British Shorthair",
        "Scottish Fold",
        "Maine Coon",
        "Ragdoll",
        "Bengal",
        "Himalayan",
    ),
}

REWARD_MAX = Decimal("99999999.99")  # lost_reports.reward_amount is NUMERIC(10, 2)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Fields whose change should re-run background matching after an edit.
MATCH_FIELDS = ("species", "breed", "colors", "size", "last_seen_lat", "last_seen_lng")

# --- Browse -----------------------------------------------------------------------------------

PER_PAGE = 12
API_MAX_PER_PAGE = 50
BROWSE_STATUSES = ("lost", "found", "closed", "all")
RADIUS_CHOICES = (1, 3, 5, 10, 15, 25)
DEFAULT_RADIUS_KM = 5
NEAR_SCAN_LIMIT = 2000  # safety cap on rows checked with Haversine for "near me"


# =============================================================================================
# Helpers shared with cases.py
# =============================================================================================


def iso_utc(value: datetime | None) -> str | None:
    """Naive-UTC datetime -> ``"2026-09-16T07:45:00Z"`` for JSON."""
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def get_visible_report_or_404(report_id: int) -> LostReport:
    """The report, or 404 if it does not exist or the viewer may not see it (hidden).

    A hidden report answers 404 (not 403) so its existence is not revealed.
    """
    report = db.session.get(LostReport, report_id)
    if report is None or not policy_service.can_view_report(current_user, report):
        abort(404)
    return report


def contact_info(report: LostReport, viewer: Any) -> dict[str, Any]:
    """Contact details as the viewer may see them (SPEC 10: phones masked publicly).

    The full phone is shown when the owner opted in (``show_phone``) or the viewer is
    the owner/an admin. The contact email is private to the owner and admins; other
    people reach the owner through case messaging.
    """
    phone = report.contact_phone
    full = policy_service.can_view_report_phone(viewer, report)
    manage = policy_service.can_manage_report(viewer, report)
    return {
        "name": report.contact_name,
        "phone": (format_phone(phone) if full else mask_phone(phone)) if phone else None,
        "phone_masked": bool(phone) and not full,
        "tel": tel_href(phone) if (phone and full) else None,
        "email": report.contact_email if manage else None,
    }


def history_list(report: LostReport, viewer: Any) -> list[dict[str, Any]]:
    """Status timeline for JSON/templates. Owner notes are shown to the owner/admins only."""
    manage = policy_service.can_manage_report(viewer, report)
    items = []
    for entry in report.status_history:
        items.append(
            {
                "from_status": entry.from_status,
                "to_status": entry.to_status,
                "changed_at": iso_utc(entry.changed_at),
                "note": entry.note if (manage or entry.from_status is None) else None,
            }
        )
    return items


def report_summary(report: LostReport, *, distance_km: float | None = None) -> dict[str, Any]:
    """Public list item for ``GET /api/lost-reports`` (no private contact data)."""
    primary = report.primary_photo
    return {
        "id": report.id,
        "case_number": report.case_number,
        "pet_name": report.pet_name,
        "species": report.species,
        "breed": report.breed,
        "colors": report.colors,
        "size": report.size,
        "sex": report.sex,
        "age_text": report.age_text,
        "status": report.status,
        "last_seen": {
            "lat": report.last_seen_lat,
            "lng": report.last_seen_lng,
            "place": report.last_seen_place,
            "at": iso_utc(report.last_seen_at),
        },
        "reward": {
            "amount": float(report.reward_amount) if report.reward_amount is not None else None,
            "text": report.reward_text,
        },
        "photo_url": storage_service.photo_url(primary, "full") if primary else None,
        "thumb_url": storage_service.photo_url(primary, "thumb") if primary else None,
        "owner_name": report.owner.full_name if report.owner else None,
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "created_at": iso_utc(report.created_at),
        "url": url_for("cases.detail", report_id=report.id),
    }


def report_detail(report: LostReport, viewer: Any) -> dict[str, Any]:
    """Full JSON for ``GET /api/lost-reports/<id>`` as the viewer may see it."""
    data = report_summary(report)
    manage = policy_service.can_manage_report(viewer, report)
    data.update(
        {
            "identifying_features": report.identifying_features,
            "ai_attributes": report.ai_attributes,
            "photos": [
                {
                    "id": photo.id,
                    "url": storage_service.photo_url(photo, "full"),
                    "thumb_url": storage_service.photo_url(photo, "thumb"),
                    "is_primary": bool(photo.is_primary),
                }
                for photo in report.photos
            ],
            "contact": contact_info(report, viewer),
            "history": history_list(report, viewer),
            "maps": {
                "view_url": url_for_maps("view", report),
                "directions_url": url_for_maps("directions", report),
            },
            "can_manage": manage,
            "can_message_owner": policy_service.can_message_owner(viewer, report),
            "updated_at": iso_utc(report.updated_at),
        }
    )
    if manage:
        data["is_hidden"] = bool(report.is_hidden)
        data["hidden_reason"] = report.hidden_reason
    return data


def url_for_maps(kind: str, report: LostReport) -> str | None:
    from app.utils.maps import directions_url, view_url

    if not report.has_location:
        return None
    if kind == "view":
        return view_url(report.last_seen_lat, report.last_seen_lng)
    return directions_url(report.last_seen_lat, report.last_seen_lng)


# =============================================================================================
# Form values and validation (shared by the wizard, the edit page and the JSON API)
# =============================================================================================

TEXT_FIELDS = (
    "species",
    "pet_name",
    "breed",
    "colors",
    "size",
    "sex",
    "age_text",
    "identifying_features",
    "lat",
    "lng",
    "place",
    "last_seen_date",
    "last_seen_time",
    "contact_name",
    "contact_email",
    "contact_phone",
    "reward_amount",
    "reward_text",
)


def default_values(user: User) -> dict[str, Any]:
    """Blank wizard values; contact details default to the account (SPEC 5.1)."""
    values: dict[str, Any] = {name: "" for name in TEXT_FIELDS}
    values.update(
        {
            "contact_name": user.full_name,
            "contact_email": user.email,
            "contact_phone": format_phone(user.mobile_e164) if user.mobile_e164 else "",
            "show_phone": False,
        }
    )
    return values


def values_from_report(report: LostReport) -> dict[str, Any]:
    """Current report fields as form values (for the edit page)."""
    local = to_local(report.last_seen_at)
    return {
        "species": report.species,
        "pet_name": report.pet_name,
        "breed": report.breed,
        "colors": report.colors,
        "size": report.size,
        "sex": report.sex,
        "age_text": report.age_text or "",
        "identifying_features": report.identifying_features or "",
        "lat": report.last_seen_lat,
        "lng": report.last_seen_lng,
        "place": report.last_seen_place or "",
        "last_seen_date": local.strftime("%Y-%m-%d") if local else "",
        "last_seen_time": local.strftime("%H:%M") if local else "",
        "contact_name": report.contact_name,
        "contact_email": report.contact_email or "",
        "contact_phone": format_phone(report.contact_phone) if report.contact_phone else "",
        "show_phone": bool(report.show_phone),
        "reward_amount": (
            format(report.reward_amount.normalize(), "f")
            if report.reward_amount is not None
            else ""
        ),
        "reward_text": report.reward_text or "",
    }


def values_from_request(form: MultiDict) -> dict[str, Any]:
    """Raw submitted values (kept as typed, to re-fill the form after an error)."""
    values: dict[str, Any] = {name: (form.get(name) or "").strip() for name in TEXT_FIELDS}
    # API clients may send one local "last_seen_at" (YYYY-MM-DDTHH:MM) instead of date + time.
    combined = (form.get("last_seen_at") or "").strip()
    if combined and not values["last_seen_date"]:
        date_part, _, time_part = combined.partition("T")
        values["last_seen_date"] = date_part
        values["last_seen_time"] = time_part[:5]
    values["show_phone"] = parse_bool(form.get("show_phone"))
    # Non-numeric coordinates are dropped so the location picker can re-render safely
    # (validation still reports them as missing).
    for name in ("lat", "lng"):
        try:
            float(values[name])
        except ValueError:
            values[name] = ""
    return values


def _parse_reward(raw: str) -> Decimal | None:
    text = (raw or "").replace(",", "").replace("₱", "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValidationError("Enter the reward in pesos, e.g. 1500.", "reward_amount") from None
    if not amount.is_finite():
        raise ValidationError("Enter the reward in pesos, e.g. 1500.", "reward_amount")
    if amount < 0:
        raise ValidationError("The reward cannot be negative.", "reward_amount")
    if amount > REWARD_MAX:
        raise ValidationError("That reward amount is too large.", "reward_amount")
    return amount.quantize(Decimal("0.01"))


def validate_report_fields(values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Check every non-photo field. Returns ``(clean model fields, errors by field)``.

    Error keys match the form field names (``location`` covers lat/lng).
    """
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def check(name: str, func) -> None:  # noqa: ANN001
        try:
            func()
        except ValidationError as exc:
            errors.setdefault(exc.field or name, exc.message)

    def _species() -> None:
        data["species"] = parse_choice(values["species"], Species.ALL, field="species")

    def _size() -> None:
        data["size"] = parse_choice(values["size"], PetSize.ALL, field="size")

    def _sex() -> None:
        data["sex"] = parse_choice(values["sex"], PetSex.ALL, field="sex")

    def _text(name: str, max_length: int, required: bool = False, column: str | None = None):
        def run() -> None:
            data[column or name] = clean_text(
                values[name], max_length=max_length, required=required, field=name
            )

        return run

    def _location() -> None:
        lat, lng = parse_coordinates(values["lat"], values["lng"], require_service_area=True)
        data["last_seen_lat"], data["last_seen_lng"] = lat, lng

    def _last_seen() -> None:
        if not values["last_seen_date"]:
            raise ValidationError("Enter the date your pet was last seen.", "last_seen_date")
        data["last_seen_at"] = parse_local_datetime(
            values["last_seen_date"], values["last_seen_time"], field="last_seen_date"
        )

    def _email() -> None:
        email = clean_text(values["contact_email"], max_length=255, field="contact_email")
        if email and not EMAIL_RE.match(email):
            raise ValidationError("Enter a valid email address.", "contact_email")
        data["contact_email"] = email.lower() if email else None

    def _phone() -> None:
        raw = values["contact_phone"]
        if not raw:
            data["contact_phone"] = None
            return
        try:
            data["contact_phone"] = normalize_ph(raw)
        except ValueError as exc:
            raise ValidationError(str(exc), "contact_phone") from None

    def _reward() -> None:
        data["reward_amount"] = _parse_reward(values["reward_amount"])

    check("species", _species)
    check("pet_name", _text("pet_name", 80, required=True))
    check("breed", _text("breed", 80, required=True))
    check("colors", _text("colors", 120, required=True))
    check("size", _size)
    check("sex", _sex)
    check("age_text", _text("age_text", 40))
    check("identifying_features", _text("identifying_features", 2000))
    check("location", _location)
    check("place", _text("place", 160, column="last_seen_place"))
    check("last_seen_date", _last_seen)
    check("contact_name", _text("contact_name", 120, required=True))
    check("contact_email", _email)
    check("contact_phone", _phone)
    check("reward_amount", _reward)
    check("reward_text", _text("reward_text", 160))

    # Only a real phone number can be shown publicly.
    data["show_phone"] = bool(values.get("show_phone")) and bool(data.get("contact_phone"))
    if values.get("show_phone") and "contact_phone" not in errors and not data["contact_phone"]:
        errors["contact_phone"] = "Add a phone number, or untick “Show my phone number”."
    return data, errors


def photo_files(files: MultiDict) -> list[FileStorage]:
    """Uploaded ``photos`` that actually contain a file (an empty picker sends one blank part)."""
    return [f for f in files.getlist("photos") if f and (f.filename or "").strip()]


def check_photo_count(count: int, errors: dict[str, str]) -> None:
    """SPEC 5.1: 1-5 photos per report."""
    limit = int(current_app.config["MAX_PHOTOS_PER_REPORT"])
    if count < 1:
        errors.setdefault("photos", "Add at least one clear photo of your pet.")
    elif count > limit:
        errors.setdefault("photos", f"You can add up to {limit} photos.")


def _discard_saved(photos: list[Photo]) -> None:
    """Remove files already written for a report that is being rolled back."""
    for photo in photos:
        try:
            storage_service.delete_photo(photo)
        except Exception:  # noqa: BLE001 - best effort clean-up; the rollback follows
            current_app.logger.warning("Could not delete photo %s during rollback", photo.id)


def create_report(owner: User, data: dict[str, Any], files: list[FileStorage]) -> LostReport:
    """Save a validated report and its photos in ONE transaction, queue the jobs, commit.

    Raises ``storage_service.ImageValidationError`` if a photo is not acceptable
    (nothing is saved in that case).
    """
    saved: list[Photo] = []
    try:
        report = LostReport(
            owner_id=owner.id,
            case_number=next_case_number(),  # locks the year's counter until commit
            status=ReportStatus.LOST,
            **data,
        )
        db.session.add(report)
        db.session.flush()
        report.record_created(actor=owner)

        folder = storage_service.default_folder(PhotoOwner.LOST_REPORT, report.id)
        for index, file in enumerate(files):
            saved.append(
                storage_service.save_image(
                    file,
                    owner_type=PhotoOwner.LOST_REPORT,
                    owner_id=report.id,
                    folder=folder,
                    is_primary=index == 0,
                    sort_order=index,
                )
            )
        log_event(
            "report.created",
            actor=owner,
            target=report,
            details={"case_number": report.case_number, "photos": len(saved)},
        )
        enqueue("extract_attributes", {"photo_id": saved[0].id})
        enqueue("match_lost_report", {"lost_report_id": report.id})
        db.session.commit()
    except Exception:
        _discard_saved(saved)
        db.session.rollback()
        raise
    return report


def _photo_error(exc: storage_service.ImageValidationError) -> str:
    return f"{exc.message} Please choose your photos again."


# =============================================================================================
# Browse
# =============================================================================================


@dataclass
class BrowseParams:
    species: str | None = None
    q: str | None = None
    status: str = ReportStatus.LOST
    near: tuple[float, float] | None = None
    radius_km: float = DEFAULT_RADIUS_KM
    page: int = 1
    per_page: int = PER_PAGE
    near_invalid: bool = False


def parse_browse_params(args: MultiDict, *, per_page: int = PER_PAGE) -> BrowseParams:
    """Read the filters from the query string; invalid values fall back to defaults."""
    species = (args.get("species") or "").strip().lower()
    status = (args.get("status") or ReportStatus.LOST).strip().lower()
    q = (args.get("q") or "").strip()[:80] or None
    near_raw = (args.get("near") or "").strip()
    near = parse_near(near_raw)
    radius = parse_int(args.get("radius"), default=DEFAULT_RADIUS_KM) or DEFAULT_RADIUS_KM
    return BrowseParams(
        species=species if species in Species.ALL else None,
        q=q,
        status=status if status in BROWSE_STATUSES else ReportStatus.LOST,
        near=near,
        radius_km=float(min(max(radius, 1), 50)),
        page=max(1, parse_int(args.get("page"), default=1) or 1),
        per_page=per_page,
        near_invalid=bool(near_raw) and near is None,
    )


def _browse_query(params: BrowseParams) -> sa.Select:
    """Public, non-hidden reports matching the filters (never includes hidden ones)."""
    stmt = sa.select(LostReport).where(LostReport.is_hidden.is_(False))
    if params.status != "all":
        stmt = stmt.where(LostReport.status == params.status)
    if params.species:
        stmt = stmt.where(LostReport.species == params.species)
    if params.q:
        term = params.q
        stmt = stmt.where(
            sa.or_(
                LostReport.pet_name.icontains(term, autoescape=True),
                LostReport.breed.icontains(term, autoescape=True),
                LostReport.colors.icontains(term, autoescape=True),
                LostReport.last_seen_place.icontains(term, autoescape=True),
                LostReport.case_number.icontains(term, autoescape=True),
            )
        )
    return stmt


def search_reports(params: BrowseParams) -> dict[str, Any]:
    """Run a browse search. Returns ``{"items": [(report, distance_km)], "total", "pages", "page"}``.

    With ``near`` the rows inside a bounding box are loaded, measured with Haversine,
    filtered to the radius and sorted nearest first; otherwise newest first.
    """
    stmt = _browse_query(params).options(selectinload(LostReport.owner))
    if params.near:
        lat, lng = params.near
        dlat = params.radius_km / 111.32
        dlng = params.radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
        stmt = stmt.where(
            LostReport.last_seen_lat.between(lat - dlat, lat + dlat),
            LostReport.last_seen_lng.between(lng - dlng, lng + dlng),
        ).limit(NEAR_SCAN_LIMIT)
        rows = []
        for report in db.session.scalars(stmt):
            distance = haversine_km(lat, lng, report.last_seen_lat, report.last_seen_lng)
            if distance <= params.radius_km:
                rows.append((report, distance))
        rows.sort(key=lambda row: (row[1], -row[0].id))
        total = len(rows)
        start = (params.page - 1) * params.per_page
        items = rows[start : start + params.per_page]
    else:
        total = db.session.scalar(sa.select(sa.func.count()).select_from(stmt.subquery())) or 0
        page_stmt = (
            stmt.order_by(LostReport.created_at.desc(), LostReport.id.desc())
            .offset((params.page - 1) * params.per_page)
            .limit(params.per_page)
        )
        items = [(report, None) for report in db.session.scalars(page_stmt)]
    pages = max(1, math.ceil(total / params.per_page)) if total else 1
    return {"items": items, "total": total, "pages": pages, "page": params.page}


def _browse_url(params: BrowseParams, **changes: Any) -> str:
    """Browse URL keeping the current filters (used for pagination links)."""
    args: dict[str, Any] = {
        "species": params.species,
        "q": params.q,
        "status": params.status if params.status != ReportStatus.LOST else None,
        "near": f"{params.near[0]:.5f},{params.near[1]:.5f}" if params.near else None,
        "radius": int(params.radius_km) if params.near else None,
        "page": params.page if params.page > 1 else None,
    }
    args.update(changes)
    return url_for("pets.browse", **{k: v for k, v in args.items() if v not in (None, "")})


@bp.get("/lost-pets")
def browse():
    """Public list of Lost Pet Reports (status, species, text search, near me; pages)."""
    params = parse_browse_params(request.args)
    result = search_reports(params)
    cards = [
        lost_report_card(
            report,
            distance_km=distance,
            show_message=policy_service.can_message_owner(current_user, report)
            or not current_user.is_authenticated,
        )
        for report, distance in result["items"]
    ]
    return render_template(
        "lost_pet/browse.html",
        params=params,
        cards=cards,
        result=result,
        species_choices=Species.choices(),
        radius_choices=RADIUS_CHOICES,
        status_choices=[
            ("lost", "Still lost"),
            ("found", "Found / reunited"),
            ("closed", "Closed"),
            ("all", "All cases"),
        ],
        prev_url=(
            _browse_url(params, page=params.page - 1 if params.page > 2 else None)
            if params.page > 1
            else None
        ),
        next_url=(
            _browse_url(params, page=params.page + 1) if params.page < result["pages"] else None
        ),
        clear_near_url=_browse_url(params, near=None, radius=None, page=None),
    )


# =============================================================================================
# Create (wizard)
# =============================================================================================


def _first_error_step(errors: dict[str, str]) -> int:
    steps = [FIELD_STEPS.get(name, 5) for name in errors]
    return min(steps) if steps else 1


def _render_wizard(values: dict[str, Any], errors: dict[str, str], status: int = 200):
    return (
        render_template(
            "lost_pet/create_report.html",
            values=values,
            errors=errors,
            steps=WIZARD_STEPS,
            start_step=_first_error_step(errors) if errors else 1,
            breed_suggestions=BREED_SUGGESTIONS,
            max_photos=current_app.config["MAX_PHOTOS_PER_REPORT"],
            today=local_now().strftime("%Y-%m-%d"),
        ),
        status,
    )


@bp.route("/lost-pets/new", methods=["GET", "POST"])
@login_required
@limiter.limit(config_limit("RATE_LIMIT_REPORTS"), methods=["POST"])
def new():
    """5-step wizard (lost_pet/create_report.html); works without JS as one long form."""
    if request.method == "GET":
        return _render_wizard(default_values(current_user), {})

    values = values_from_request(request.form)
    data, errors = validate_report_fields(values)
    files = photo_files(request.files)
    check_photo_count(len(files), errors)
    if errors:
        if "photos" not in errors and files:
            errors["photos_reselect"] = "For your safety, please choose your photos again."
        return _render_wizard(values, errors, 400)
    try:
        report = create_report(current_user, data, files)
    except storage_service.ImageValidationError as exc:
        return _render_wizard(values, {"photos": _photo_error(exc)}, 400)

    flash(
        f"Your report was filed. Your case number is {report.case_number}. "
        "We will alert you when a possible match appears.",
        "success",
    )
    return redirect(url_for("cases.detail", report_id=report.id))


# =============================================================================================
# Edit
# =============================================================================================


def _render_edit(report: LostReport, values: dict[str, Any], errors: dict[str, str], status=200):
    photos = [
        {
            "id": photo.id,
            "url": storage_service.photo_url(photo, "thumb"),
            "is_primary": bool(photo.is_primary),
        }
        for photo in report.photos
    ]
    return (
        render_template(
            "lost_pet/edit_report.html",
            report=report,
            values=values,
            errors=errors,
            photos=photos,
            breed_suggestions=BREED_SUGGESTIONS,
            max_photos=current_app.config["MAX_PHOTOS_PER_REPORT"],
            free_slots=max(0, current_app.config["MAX_PHOTOS_PER_REPORT"] - len(photos)),
            today=local_now().strftime("%Y-%m-%d"),
        ),
        status,
    )


@bp.route("/lost-pets/<int:report_id>/edit", methods=["GET", "POST"])
@login_required
@limiter.limit(config_limit("RATE_LIMIT_REPORTS"), methods=["POST"])
def edit(report_id: int):
    """Edit a report (owner or admin - policy_service.can_manage_report)."""
    report = get_visible_report_or_404(report_id)
    if not policy_service.can_manage_report(current_user, report):
        abort(403)
    if report.status == ReportStatus.CLOSED:
        flash("This case is closed, so it can no longer be edited.", "info")
        return redirect(url_for("cases.detail", report_id=report.id))

    if request.method == "GET":
        return _render_edit(report, values_from_report(report), {})

    values = values_from_request(request.form)
    data, errors = validate_report_fields(values)

    existing = report.photos
    by_id = {photo.id: photo for photo in existing}
    remove_ids = {
        pid for pid in (parse_int(v) for v in request.form.getlist("remove_photo")) if pid
    }
    removed = [by_id[pid] for pid in remove_ids if pid in by_id]
    kept = [photo for photo in existing if photo.id not in remove_ids]
    new_files = photo_files(request.files)
    check_photo_count(len(kept) + len(new_files), errors)
    if errors:
        return _render_edit(report, values, errors, 400)

    old_primary_id = existing[0].id if existing else None
    before = {name: getattr(report, name) for name in MATCH_FIELDS}
    added: list[Photo] = []
    try:
        folder = storage_service.default_folder(PhotoOwner.LOST_REPORT, report.id)
        for file in new_files:
            added.append(
                storage_service.save_image(
                    file, owner_type=PhotoOwner.LOST_REPORT, owner_id=report.id, folder=folder
                )
            )
    except storage_service.ImageValidationError as exc:
        _discard_saved(added)
        db.session.rollback()
        return _render_edit(report, values, {"photos": _photo_error(exc)}, 400)

    for name, value in data.items():
        setattr(report, name, value)

    # Photo order: the chosen main photo first, then the kept photos, then new uploads.
    ordered = kept + added
    wanted = parse_int(request.form.get("primary_photo_id"))
    ordered.sort(key=lambda photo: 0 if photo.id == wanted else 1)
    for index, photo in enumerate(ordered):
        photo.is_primary = index == 0
        photo.sort_order = index
    for photo in removed:
        storage_service.delete_photo(photo)

    primary_changed = ordered[0].id != old_primary_id
    if primary_changed:
        report.ai_attributes = None  # re-extracted from the new main photo
        enqueue("extract_attributes", {"photo_id": ordered[0].id})
    match_changed = primary_changed or any(
        getattr(report, name) != before[name] for name in MATCH_FIELDS
    )
    if match_changed and report.status == ReportStatus.LOST:
        enqueue("match_lost_report", {"lost_report_id": report.id})
    log_event(
        "report.updated",
        actor=current_user,
        target=report,
        details={
            "photos_added": len(added),
            "photos_removed": len(removed),
            "rematch": bool(match_changed and report.status == ReportStatus.LOST),
        },
    )
    db.session.commit()
    flash("Your report was updated.", "success")
    return redirect(url_for("cases.detail", report_id=report.id))


# =============================================================================================
# JSON API
# =============================================================================================


@bp.get("/api/lost-reports")
def api_list():
    """GET /api/lost-reports?near=lat,lng&species=dog|cat&q=&status=lost&radius=&page=&per_page=

    Public; hidden reports are never listed; no private contact data.
    """
    args = request.args
    species = (args.get("species") or "").strip().lower()
    if species and species not in Species.ALL:
        return json_error("validation_error", "species must be 'dog' or 'cat'.", 400)
    status = (args.get("status") or "").strip().lower()
    if status and status not in BROWSE_STATUSES:
        return json_error("validation_error", "status must be lost, found, closed or all.", 400)
    per_page = min(API_MAX_PER_PAGE, max(1, parse_int(args.get("per_page"), default=20) or 20))
    params = parse_browse_params(args, per_page=per_page)
    if params.near_invalid:
        return json_error("validation_error", "near must look like 'lat,lng'.", 400)
    result = search_reports(params)
    return {
        "items": [report_summary(r, distance_km=d) for r, d in result["items"]],
        "page": result["page"],
        "per_page": per_page,
        "total": result["total"],
        "pages": result["pages"],
    }


@bp.post("/api/lost-reports")
@api_login_required
@limiter.limit(config_limit("RATE_LIMIT_REPORTS"))
def api_create():
    """POST /api/lost-reports (multipart: fields + photos) -> 201 {"id", "case_number", "url"}."""
    values = values_from_request(request.form)
    data, errors = validate_report_fields(values)
    files = photo_files(request.files)
    check_photo_count(len(files), errors)
    if errors:
        return json_error(
            "validation_error", "Please check the highlighted fields.", 400, details=errors
        )
    try:
        report = create_report(current_user, data, files)
    except storage_service.ImageValidationError as exc:
        return json_error("validation_error", exc.message, 400, details={"photos": exc.message})
    url = url_for("cases.detail", report_id=report.id)
    return {"id": report.id, "case_number": report.case_number, "url": url}, 201, {"Location": url}


@bp.get("/api/lost-reports/<int:report_id>")
def api_detail(report_id: int):
    """GET /api/lost-reports/<id> - public fields (404 if hidden, unless owner/admin)."""
    report = get_visible_report_or_404(report_id)
    return report_detail(report, current_user)
