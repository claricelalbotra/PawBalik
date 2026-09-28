"""Rescue/vet/shelter directory, Request Rescue Help, /api/providers (SPEC 5.8).

* ``/rescue`` is public (guests too). Providers are sorted by distance from the
  user's location (``?near=lat,lng``, filled by "Use my current location") or
  from a case / sighting location (``?lost_report_id=`` / ``?sighting_id=``;
  a sighting only for users allowed by ``policy_service.can_view_sighting``).
  Filters: ``?type=rescuer|vet|shelter`` and ``?open=now|24_7``.
* "Request Rescue Help" (``/rescue/request``) builds a copyable plain-text
  summary for the report owner (or a sighting viewer) and logs it in
  ``rescue_requests``.

The page always shows ``strings.EMERGENCY_NOTE``: PawBalik does not replace
emergency services. Sample providers are labelled SAMPLE.

Opening hours are free text written by admins. :func:`parse_hours` understands
simple formats ("8:00 AM - 5:00 PM", "Mon-Sat 8AM-5PM",
"Mon-Fri 8AM-5PM; Sat 8AM-12PM; Sun closed", "Daily 7AM-7PM", "24/7");
anything else counts as *unknown* (never as open).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

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

from app.extensions import config_limit, db, limiter
from app.models import (
    AnimalCondition,
    AssistanceProvider,
    LostReport,
    PetSex,
    PetSize,
    ProviderType,
    RescueRequest,
    Sighting,
    Species,
)
from app.services import audit_service
from app.services import policy_service as policy
from app.utils.auth import api_login_required
from app.utils.filters import format_datetime
from app.utils.geo import distance_or_none, in_service_area, parse_near
from app.utils.http import json_error
from app.utils.maps import directions_url, view_url
from app.utils.phone import format_phone, normalize_ph
from app.utils.strings import EMERGENCY_NOTE
from app.utils.time import local_now, to_local
from app.utils.validators import ValidationError, clean_text, parse_bool, parse_int

bp = Blueprint("rescue", __name__)

NOTE_MAX_LENGTH = 500
PICKER_LIMIT = 8  # providers offered in the Request Rescue Help picker
OPEN_FILTERS = ("now", "24_7")


# =============================================================================================
# Opening hours
# =============================================================================================

ALWAYS_OPEN = "always"

_DAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}  # fmt: skip
_EVERY_DAY = frozenset(range(7))
_DAY_WORDS = {
    "": _EVERY_DAY,
    "daily": _EVERY_DAY,
    "everyday": _EVERY_DAY,
    "every day": _EVERY_DAY,
    "open daily": _EVERY_DAY,
    "open": _EVERY_DAY,
    "weekdays": frozenset(range(5)),
    "weekends": frozenset({5, 6}),
}
_ALWAYS_RE = re.compile(r"24\s*/\s*7|24\s*hours|24\s*hrs|open\s+24\b|24-hour", re.IGNORECASE)
_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*(?:([ap])\.?\s*m\b\.?)?"
_RANGE_RE = re.compile(rf"{_TIME}\s*(?:-|–|—|to|until)\s*{_TIME}", re.IGNORECASE)

# One opening interval: (days, start minute, end minute). end <= start means past midnight.
Interval = tuple[frozenset[int], int, int]


def _parse_days(text: str) -> frozenset[int] | None:
    """ "Mon-Sat", "Mon, Wed, Fri", "Weekdays", "Daily", "" -> weekday numbers (Mon=0)."""
    text = text.strip().lower().rstrip(":").strip()
    text = re.sub(r"\s*(–|—|\bto\b)\s*", "-", text)
    text = re.sub(r"\s*(&|\band\b|/)\s*", ",", text)
    if text in _DAY_WORDS:
        return _DAY_WORDS[text]
    days: set[int] = set()
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        if "-" in token:
            first, _, last = (p.strip() for p in token.partition("-"))
            a, b = _day_number(first), _day_number(last)
            if a is None or b is None:
                return None
            day = a
            days.add(day)
            while day != b:
                day = (day + 1) % 7
                days.add(day)
        else:
            number = _day_number(token)
            if number is None:
                return None
            days.add(number)
    return frozenset(days) or None


def _day_number(token: str) -> int | None:
    token = token.strip(". ")
    if token in _DAYS:
        return _DAYS[token]
    if token.endswith("s") and token[:-1] in _DAYS:  # "Sundays"
        return _DAYS[token[:-1]]
    return None


def _to_minutes(hour: int, minute: int, meridiem: str | None) -> int | None:
    if not 0 <= minute <= 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        return (hour % 12 + (12 if meridiem == "p" else 0)) * 60 + minute
    if not 0 <= hour <= 24 or (hour == 24 and minute):
        return None
    return hour * 60 + minute


def _parse_range(match: re.Match[str]) -> tuple[int, int] | None:
    h1, m1, ap1, h2, m2, ap2 = match.groups()
    h1, h2 = int(h1), int(h2)
    m1, m2 = int(m1 or 0), int(m2 or 0)
    ap1 = ap1.lower() if ap1 else None
    ap2 = ap2.lower() if ap2 else None
    if ap1 is None and ap2 is not None:  # "8-5PM": 8 PM would be after 5 PM, so 8 AM
        ap1 = ap2
        if _to_minutes(h1, m1, ap1) is not None and _to_minutes(h1, m1, ap1) >= (
            _to_minutes(h2, m2, ap2) or 0
        ):
            ap1 = "a" if ap2 == "p" else "p"
    elif ap2 is None and ap1 is not None:  # "8AM-5": 5 AM would be before 8 AM, so 5 PM
        ap2 = ap1
        if (_to_minutes(h2, m2, ap2) or 0) <= (_to_minutes(h1, m1, ap1) or 0):
            ap2 = "p" if ap1 == "a" else "a"
    elif ap1 is None and ap2 is None and h2 <= h1 <= 12 and h2 < 12:  # "8-5" means 8:00-17:00
        h2 += 12
    start, end = _to_minutes(h1, m1, ap1), _to_minutes(h2, m2, ap2)
    if start is None or end is None or start == end:
        return None
    return start, end


def _segments(text: str) -> Iterator[str]:
    """Split hours text into "<days> <time range>" parts.

    ";", "|" and new lines always separate parts. A comma separates parts only
    when the text before it already holds a time range (or "closed"), so
    "Tue, Thu 9AM-3PM" stays one part and "Mon-Sat 8AM-5PM, Sun 9AM-12PM" becomes two.
    """
    for part in re.split(r"[;\n|]+", text):
        buffer = ""
        for piece in part.split(","):
            buffer = f"{buffer},{piece}" if buffer else piece
            if _RANGE_RE.search(buffer) or re.search(r"\bclosed\b", buffer, re.IGNORECASE):
                yield buffer
                buffer = ""
        if buffer.strip():
            yield buffer


def parse_hours(text: str | None) -> str | list[Interval] | None:
    """Parse a provider's ``hours_text``.

    Returns ``ALWAYS_OPEN`` for "24/7"-style text, a list of intervals for the
    simple formats in the module docstring, or ``None`` when the text is empty
    or not understood (the UI then says "Hours not verified").
    """
    if not text or not text.strip():
        return None
    if _ALWAYS_RE.search(text):
        return ALWAYS_OPEN
    intervals: list[Interval] = []
    parsed_any = False
    for segment in _segments(text):
        segment = segment.strip().strip(",.").strip()
        if not segment:
            continue
        match = _RANGE_RE.search(segment)
        if match is None:
            if re.search(r"\bclosed\b", segment, re.IGNORECASE):
                days = _parse_days(re.sub(r"\bclosed\b", "", segment, flags=re.IGNORECASE))
                if days is None:
                    return None
                parsed_any = True  # a known closed day: no interval
                continue
            return None
        before = segment[: match.start()].strip().strip(",")
        after = segment[match.end() :].strip().strip(",.")
        if before and after:
            return None
        days = _parse_days(before or after)
        times = _parse_range(match)
        if days is None or times is None:
            return None
        intervals.append((days, times[0], times[1]))
        parsed_any = True
    return intervals if parsed_any else None


def open_state(provider: AssistanceProvider, now: datetime | None = None) -> bool | None:
    """True = open now, False = closed now, None = unknown hours.

    ``now`` is a local (Asia/Manila) datetime; defaults to the current local time.
    """
    if provider.is_24_7:
        return True
    spec = parse_hours(provider.hours_text)
    if spec is None:
        return None
    if spec == ALWAYS_OPEN:
        return True
    now = now or local_now()
    today, minute = now.weekday(), now.hour * 60 + now.minute
    yesterday = (today - 1) % 7
    for days, start, end in spec:
        if end > start:
            if today in days and start <= minute < end:
                return True
        elif (today in days and minute >= start) or (yesterday in days and minute < end):
            return True
    return False


def is_always_open(provider: AssistanceProvider) -> bool:
    return bool(provider.is_24_7) or parse_hours(provider.hours_text) == ALWAYS_OPEN


# =============================================================================================
# Safe links
# =============================================================================================


def tel_link(phone: str | None) -> str | None:
    """A sanitized ``tel:`` URL (digits and a leading + only), or None if not a phone number."""
    if not phone:
        return None
    try:
        return "tel:" + normalize_ph(phone)
    except ValueError:
        pass
    if re.search(r"[^\d\s\-\+\(\)\.]", phone):
        return None
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15:
        return None
    return "tel:" + ("+" if phone.strip().startswith("+") else "") + digits


def safe_http_url(url: str | None) -> str | None:
    """Return ``url`` only if it is an absolute http(s) URL with a host; otherwise None."""
    if not url:
        return None
    url = url.strip()
    if any(ch.isspace() or ord(ch) < 32 for ch in url) or len(url) > 255:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return url


def phone_entries(provider: AssistanceProvider) -> list[dict[str, str | None]]:
    return [{"display": p, "tel": tel_link(p)} for p in provider.phone_list]


# =============================================================================================
# Directory
# =============================================================================================


@dataclass
class Origin:
    """The point providers are sorted from."""

    lat: float
    lng: float
    label: str
    kind: str  # "near" | "lost_report" | "sighting"
    ref_id: int | None = None


def resolve_origin(args: Any, user: Any) -> tuple[Origin | None, str | None]:
    """Origin from ``near`` / ``lost_report_id`` / ``sighting_id``. Returns (origin, problem)."""
    report_id = parse_int(args.get("lost_report_id"))
    sighting_id = parse_int(args.get("sighting_id"))
    if report_id is not None:
        report = db.session.get(LostReport, report_id)
        if report is None or not policy.can_view_report(user, report):
            return None, "That case is not available."
        return (
            Origin(
                report.last_seen_lat,
                report.last_seen_lng,
                f"case {report.case_number} (last seen {report.last_seen_place or 'location'})",
                "lost_report",
                report.id,
            ),
            None,
        )
    if sighting_id is not None:
        sighting = db.session.get(Sighting, sighting_id)
        if sighting is None or not policy.can_view_sighting(user, sighting):
            return None, "That sighting is not available."
        _log_owner_access(sighting, user)
        return (
            Origin(
                sighting.seen_lat,
                sighting.seen_lng,
                f"pet sighting #{sighting.id}"
                + (f" ({sighting.seen_place})" if sighting.seen_place else ""),
                "sighting",
                sighting.id,
            ),
            None,
        )
    raw_near = (args.get("near") or "").strip()
    if raw_near:
        point = parse_near(raw_near)
        if point is None:
            return None, "We could not read that location."
        return Origin(point[0], point[1], "your location", "near"), None
    return None, None


def _log_owner_access(sighting: Sighting, user: Any) -> None:
    """Private sighting opened by someone other than its finder (SPEC 5.2 access log)."""
    if sighting.finder_id == getattr(user, "id", None) or policy.is_admin(user):
        return
    audit_service.log_sighting_access(sighting, user)
    db.session.commit()


def list_providers(
    *,
    origin: Origin | None = None,
    provider_type: str | None = None,
    open_filter: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Active providers as view dicts, filtered and sorted (distance when there is an origin)."""
    stmt = sa.select(AssistanceProvider).where(AssistanceProvider.is_active.is_(True))
    if provider_type:
        stmt = stmt.where(AssistanceProvider.type == provider_type)
    rows = []
    for provider in db.session.scalars(stmt):
        state = open_state(provider, now)
        if open_filter == "now" and state is not True:
            continue
        if open_filter == "24_7" and not is_always_open(provider):
            continue
        distance = (
            distance_or_none(origin.lat, origin.lng, provider.lat, provider.lng) if origin else None
        )
        rows.append(provider_view(provider, distance, state))
    if origin:
        rows.sort(
            key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0, r["name"].lower())
        )
    else:
        rows.sort(key=lambda r: r["name"].lower())
    return rows


def provider_view(
    provider: AssistanceProvider, distance_km: float | None = None, state: bool | None = None
) -> dict[str, Any]:
    """Everything a card or the API needs, with links already sanitized."""
    has_coords = provider.lat is not None and provider.lng is not None
    always = is_always_open(provider)
    if always:
        hours_label = "Open 24/7"
    else:
        hours_label = provider.hours_text or "Hours not listed"
    return {
        "id": provider.id,
        "name": provider.name,
        "type": provider.type,
        "type_label": ProviderType.label(provider.type),
        "address": provider.address,
        "barangay": provider.barangay,
        "lat": provider.lat,
        "lng": provider.lng,
        "phones": phone_entries(provider),
        "facebook_url": safe_http_url(provider.facebook_url),
        "hours_text": provider.hours_text,
        "hours_label": hours_label,
        "is_24_7": always,
        "open_now": state,
        "services": provider.services,
        "is_sample": bool(provider.is_sample),
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "maps_url": view_url(provider.lat, provider.lng) if has_coords else None,
        "directions_url": directions_url(provider.lat, provider.lng) if has_coords else None,
        "detail_url": url_for("rescue.provider", provider_id=provider.id),
        "in_service_area": in_service_area(provider.lat, provider.lng),
    }


def _type_filter(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value if value in ProviderType.ALL else None


def _open_filter(args: Any) -> str | None:
    value = (args.get("open") or "").strip().lower()
    if value in OPEN_FILTERS:
        return value
    if parse_bool(args.get("open_now")):
        return "now"
    return None


@bp.get("/rescue")
def directory():
    """Directory, sorted by distance from ?near=lat,lng (or a case), filters ?type= & ?open=."""
    origin, problem = resolve_origin(request.args, current_user)
    if problem:
        flash(problem + " Showing all providers instead.", "warning")
    provider_type = _type_filter(request.args.get("type"))
    open_filter = _open_filter(request.args)
    providers = list_providers(origin=origin, provider_type=provider_type, open_filter=open_filter)
    return render_template(
        "rescue/index.html",
        providers=providers,
        origin=origin,
        origin_outside=origin is not None and not in_service_area(origin.lat, origin.lng),
        provider_type=provider_type,
        open_filter=open_filter,
        type_choices=ProviderType.choices(),
        near=request.args.get("near", "") if origin and origin.kind == "near" else "",
        keep_args={
            k: request.args[k] for k in ("lost_report_id", "sighting_id") if request.args.get(k)
        },
    )


@bp.get("/rescue/providers/<int:provider_id>")
def provider(provider_id: int):
    """One provider's details (inactive providers: admins only)."""
    item = db.session.get(AssistanceProvider, provider_id)
    if item is None or (not item.is_active and not policy.is_admin(current_user)):
        abort(404)
    return render_template(
        "rescue/provider.html", provider=item, view=provider_view(item, None, open_state(item))
    )


# =============================================================================================
# Request Rescue Help
# =============================================================================================


class RescueRequestError(Exception):
    """Bad request for a rescue summary. ``status`` is the HTTP status to answer with."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def load_subject(values: Any, user: Any) -> tuple[LostReport | None, Sighting | None]:
    """The case or sighting named by ``lost_report_id`` / ``sighting_id`` that ``user`` may use.

    Reports: only the owner (or an admin). Sightings: anyone ``can_view_sighting``
    allows; others get 404 so a sighting's existence is not revealed.
    """
    report_id = parse_int(values.get("lost_report_id"))
    sighting_id = parse_int(values.get("sighting_id"))
    if (report_id is None) == (sighting_id is None):
        raise RescueRequestError(
            "validation_error", "Choose one lost pet report or one pet sighting.", 400
        )
    if report_id is not None:
        report = db.session.get(LostReport, report_id)
        if report is None or not policy.can_view_report(user, report):
            raise RescueRequestError("not_found", "That case was not found.", 404)
        if not (
            policy.can_manage_report(user, report)
            and policy.can_request_rescue(user, report=report)
        ):
            raise RescueRequestError(
                "forbidden", "Only the owner of this case can request rescue help for it.", 403
            )
        return report, None
    sighting = db.session.get(Sighting, sighting_id)
    if sighting is None or not policy.can_request_rescue(user, sighting=sighting):
        raise RescueRequestError("not_found", "That sighting was not found.", 404)
    return None, sighting


def default_include_phone(user: Any, report: LostReport | None, sighting: Sighting | None) -> bool:
    """Follow the reporter's privacy choice (show_phone / contact_pref) by default."""
    if report is not None:
        return bool(report.show_phone)
    if sighting is not None and sighting.finder_id == getattr(user, "id", None):
        return sighting.shares_phone
    return False


def _animal_line(
    species: str, breed: str | None, colors: str | None, size: str | None, sex: str | None = None
) -> str:
    parts = [Species.label(species), breed, colors]
    if size:
        parts.append(PetSize.label(size))
    if sex and sex != PetSex.UNKNOWN:
        parts.append(PetSex.label(sex))
    return " · ".join(p for p in parts if p)


def _shorten(text: str | None, limit: int = 200) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _phone_text(e164: str | None) -> str:
    return format_phone(e164) if e164 else ""


def build_summary(
    user: Any,
    *,
    report: LostReport | None = None,
    sighting: Sighting | None = None,
    provider: AssistanceProvider | None = None,
    note: str | None = None,
    include_phone: bool = False,
) -> str:
    """Plain-text rescue summary to paste into SMS, Messenger or a call script.

    Phone numbers appear only when the reporter's privacy settings allow it or the
    requester chose to include their own number (``include_phone``).
    """
    lines = ["PawBalik - Rescue help request"]
    if provider is not None:
        lines.append(f"To: {provider.name}")
    if report is not None:
        lines += [
            f"Case: {report.case_number} (lost {Species.label(report.species).lower()} "
            f'"{report.pet_name}", status: {report.status})',
            "Animal: "
            + _animal_line(report.species, report.breed, report.colors, report.size, report.sex),
            "Condition: unknown (lost pet, not yet found)",
            f"Last seen: {format_datetime(report.last_seen_at)} (Philippine time)",
        ]
        lat, lng, place = report.last_seen_lat, report.last_seen_lng, report.last_seen_place
        details = _shorten(report.identifying_features)
        if details:
            lines.append(f"Identifying features: {details}")
    else:
        assert sighting is not None
        condition = AnimalCondition.label(sighting.condition)
        holding = (
            "the finder is holding the animal"
            if sighting.is_holding_animal
            else "the finder is not holding the animal"
        )
        lines += [
            f"Reference: Pet Sighting #{sighting.id}",
            "Animal: "
            + _animal_line(sighting.species, sighting.breed, sighting.colors, sighting.size),
            f"Condition: {condition} ({holding})",
            f"Seen: {format_datetime(sighting.seen_at)} (Philippine time)",
        ]
        lat, lng, place = sighting.seen_lat, sighting.seen_lng, sighting.seen_place
        details = _shorten(sighting.notes)
        if details:
            lines.append(f"Finder's notes: {details}")

    lines.append(f"Location: {place or 'see map pin'} ({lat:.6f}, {lng:.6f})")
    lines.append(f"Google Maps: {view_url(lat, lng)}")

    # Contact lines - privacy first.
    uid = getattr(user, "id", None)
    if report is not None:
        phone = _phone_text(report.contact_phone) if include_phone else ""
        lines.append(
            f"Contact: {report.contact_name}"
            + (f", {phone}" if phone else " (phone not shared - reply through PawBalik)")
        )
        if report.owner_id != uid:  # an admin preparing it on the owner's behalf
            lines.append(f"Prepared by: {user.full_name} (PawBalik administrator)")
    else:
        is_finder = sighting.finder_id == uid
        if is_finder:
            phone = _phone_text(sighting.contact_phone) if include_phone else ""
        else:
            phone = _phone_text(sighting.contact_phone) if sighting.shares_phone else ""
        lines.append(
            f"Finder: {sighting.contact_name}"
            + (f", {phone}" if phone else " (phone not shared - reply through PawBalik)")
        )
        if not is_finder:
            own = _phone_text(user.mobile_e164) if include_phone else ""
            lines.append(f"Requested by: {user.full_name}" + (f", {own}" if own else ""))

    if note:
        lines.append(f"Note: {note}")
    if report is not None and report.is_public:
        base = (current_app.config.get("APP_BASE_URL") or request.host_url).rstrip("/")
        lines.append(f"Case page: {base}{url_for('cases.detail', report_id=report.id)}")
    lines.append("PawBalik shows possible matches only; please verify details before any handover.")
    return "\n".join(lines)


def subject_location(report: LostReport | None, sighting: Sighting | None) -> tuple[float, float]:
    if report is not None:
        return report.last_seen_lat, report.last_seen_lng
    return sighting.seen_lat, sighting.seen_lng  # type: ignore[union-attr]


def _parse_request_fields(values: Any) -> tuple[AssistanceProvider | None, str | None, bool]:
    """provider_id (optional, active), note (<= 500 chars), include_phone."""
    provider = None
    raw_provider = values.get("provider_id")
    if raw_provider not in (None, ""):
        provider_id = parse_int(raw_provider)
        provider = db.session.get(AssistanceProvider, provider_id) if provider_id else None
        if provider is None or not provider.is_active:
            raise ValidationError("Choose a provider from the list.", "provider_id")
    raw_note = values.get("note")
    note = clean_text(
        raw_note if isinstance(raw_note, str) else None, max_length=NOTE_MAX_LENGTH, field="note"
    )
    return provider, note, parse_bool(values.get("include_phone"))


def create_rescue_request(
    user: Any,
    *,
    report: LostReport | None,
    sighting: Sighting | None,
    provider: AssistanceProvider | None,
    note: str | None,
    include_phone: bool,
) -> RescueRequest:
    """Build the summary and log a ``rescue_requests`` row (commits)."""
    summary = build_summary(
        user,
        report=report,
        sighting=sighting,
        provider=provider,
        note=note,
        include_phone=include_phone,
    )
    item = RescueRequest(
        user_id=user.id,
        lost_report_id=report.id if report else None,
        sighting_id=sighting.id if sighting else None,
        provider_id=provider.id if provider else None,
        summary_text=summary,
    )
    db.session.add(item)
    db.session.flush()
    if sighting is not None:
        _log_owner_access(sighting, user)
    audit_service.log_event(
        "rescue.request_created",
        actor=user,
        target=item,
        details={
            "lost_report_id": item.lost_report_id,
            "sighting_id": item.sighting_id,
            "provider_id": item.provider_id,
        },
    )
    db.session.commit()
    return item


def _subject_args(report: LostReport | None, sighting: Sighting | None) -> dict[str, int]:
    return {"lost_report_id": report.id} if report else {"sighting_id": sighting.id}  # type: ignore[union-attr]


@bp.route("/rescue/request", methods=["GET", "POST"])
@login_required
@limiter.limit(config_limit("RATE_LIMIT_RESCUE_REQUESTS"), methods=["POST"])
def request_help():
    """Request Rescue Help for ?lost_report_id= or ?sighting_id=: copyable summary + log."""
    if not request.values.get("lost_report_id") and not request.values.get("sighting_id"):
        flash("Open “Request Rescue Help” from a lost pet case or a sighting.", "info")
        return redirect(url_for("rescue.directory"))
    try:
        report, sighting = load_subject(request.values, current_user)
    except RescueRequestError as exc:
        abort(exc.status, description=exc.message)

    subject_args = _subject_args(report, sighting)
    errors: dict[str, str] = {}
    form = {
        "note": "",
        "provider_id": parse_int(request.args.get("provider_id")),
        "include_phone": default_include_phone(current_user, report, sighting),
    }
    if request.method == "POST":
        form = {
            "note": request.form.get("note", ""),
            "provider_id": parse_int(request.form.get("provider_id")),
            "include_phone": parse_bool(request.form.get("include_phone")),
        }
        try:
            provider_obj, note, include_phone = _parse_request_fields(request.form)
        except ValidationError as exc:
            errors[exc.field or "form"] = exc.message
        else:
            item = create_rescue_request(
                current_user,
                report=report,
                sighting=sighting,
                provider=provider_obj,
                note=note,
                include_phone=include_phone,
            )
            flash("Rescue summary ready. Copy it and send it to the provider.", "success")
            return redirect(url_for("rescue.request_help", request_id=item.id, **subject_args))

    saved = None
    request_id = parse_int(request.args.get("request_id"))
    if request_id is not None and request.method == "GET":
        saved = db.session.get(RescueRequest, request_id)
        if (
            saved is None
            or saved.user_id != current_user.id
            or saved.lost_report_id != (report.id if report else None)
            or saved.sighting_id != (sighting.id if sighting else None)
        ):
            saved = None

    selected_provider = (
        db.session.get(AssistanceProvider, form["provider_id"]) if form["provider_id"] else None
    )
    if selected_provider is not None and not selected_provider.is_active:
        selected_provider = None
    preview = (
        saved.summary_text
        if saved
        else build_summary(
            current_user,
            report=report,
            sighting=sighting,
            provider=selected_provider,
            note=(form["note"] or "").strip()[:NOTE_MAX_LENGTH] or None,
            include_phone=bool(form["include_phone"]),
        )
    )
    lat, lng = subject_location(report, sighting)
    origin = Origin(lat, lng, "this location", "lost_report" if report else "sighting")
    providers = list_providers(origin=origin)[:PICKER_LIMIT]
    status = 400 if errors else 200
    return (
        render_template(
            "rescue/request_help.html",
            report=report,
            sighting=sighting,
            subject_args=subject_args,
            providers=providers,
            form=form,
            errors=errors,
            summary=preview,
            saved=saved,
            note_max=NOTE_MAX_LENGTH,
            is_reporter=(report is not None and report.owner_id == current_user.id)
            or (sighting is not None and sighting.finder_id == current_user.id),
        ),
        status,
    )


# =============================================================================================
# JSON API
# =============================================================================================


@bp.get("/api/providers")
def api_providers():
    """GET /api/providers?near=lat,lng&type=rescuer|vet|shelter&open_now=1 (or open=now|24_7)."""
    raw_type = (request.args.get("type") or "").strip().lower()
    if raw_type and raw_type not in ProviderType.ALL:
        return json_error(
            "validation_error", "type must be rescuer, vet or shelter.", 400, {"field": "type"}
        )
    origin, problem = resolve_origin(request.args, current_user)
    if problem:
        return json_error("validation_error", problem, 400, {"field": "near"})
    providers = list_providers(
        origin=origin, provider_type=raw_type or None, open_filter=_open_filter(request.args)
    )
    return {
        "providers": providers,
        "count": len(providers),
        "origin": (
            {"lat": origin.lat, "lng": origin.lng, "kind": origin.kind, "label": origin.label}
            if origin
            else None
        ),
        "notice": EMERGENCY_NOTE,
    }


@bp.post("/api/rescue-requests")
@api_login_required
@limiter.limit(config_limit("RATE_LIMIT_RESCUE_REQUESTS"))
def api_create_request():
    """POST /api/rescue-requests {"lost_report_id"|"sighting_id", "provider_id"?, "note"?,
    "include_phone"?} -> 201 {"id", "summary", "provider"}."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("bad_request", "Send a JSON object.", 400)
    try:
        report, sighting = load_subject(payload, current_user)
        provider_obj, note, include_phone = _parse_request_fields(payload)
    except RescueRequestError as exc:
        return json_error(exc.code, exc.message, exc.status)
    except ValidationError as exc:
        return json_error("validation_error", exc.message, 400, {"field": exc.field})
    item = create_rescue_request(
        current_user,
        report=report,
        sighting=sighting,
        provider=provider_obj,
        note=note,
        include_phone=include_phone,
    )
    return (
        {
            "id": item.id,
            "summary": item.summary_text,
            "provider": provider_view(provider_obj) if provider_obj else None,
            "created_at": to_local(item.created_at).isoformat() if item.created_at else None,
        },
        201,
    )
