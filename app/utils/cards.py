"""Build the dicts rendered by the ``pet_card`` macro (templates/partials/cards.html).

Keeping cards as plain dicts lets reports, scanner results and dashboards
share one card design. Keys (all optional except ``title`` / ``detail_url``):

    photo_url, photo_alt, similarity (int %), tier, tier_label, title, chip,
    chip_kind ("lost"|"found"|"closed"|"sighting"), subtitle, location_prefix,
    distance_km, place, date_label, person_label, detail_url, message_url,
    why ({"matching_features": [...], "differences": [...]}),
    visual_check_unavailable (bool), case_number, status
"""

from __future__ import annotations

from typing import Any

from flask import url_for

from app.models.enums import AnimalCondition, PetSex, ReportStatus, Species
from app.utils.filters import format_date, timeago


def _photo(photo, variant: str = "thumb") -> str | None:  # noqa: ANN001
    if photo is None:
        return None
    from app.services.storage_service import photo_url  # imported lazily to avoid a cycle

    return photo_url(photo, variant)


def _tier_fields(similarity: float | None, tier: str | None) -> dict[str, Any]:
    from app.models.enums import MatchTier

    if similarity is None:
        return {"similarity": None, "tier": None, "tier_label": None}
    return {
        "similarity": int(round(similarity)),
        "tier": tier,
        "tier_label": MatchTier.label(tier) if tier else None,
    }


def lost_report_card(
    report,  # noqa: ANN001 - LostReport
    *,
    distance_km: float | None = None,
    similarity: float | None = None,
    tier: str | None = None,
    explanation: dict | None = None,
    visual_check_unavailable: bool = False,
    show_message: bool = True,
    detail_url: str | None = None,
) -> dict[str, Any]:
    """Card for a Lost Pet Report (public browse, Found Pet Scanner results, My Cases)."""
    if report.status == ReportStatus.LOST:
        chip, kind = f"Lost {timeago(report.last_seen_at)}", "lost"
    else:
        chip, kind = ReportStatus.label(report.status), report.status
    parts = [
        report.breed,
        report.colors,
        PetSex.label(report.sex) if report.sex != PetSex.UNKNOWN else None,
    ]
    card = {
        "photo_url": _photo(report.primary_photo),
        "photo_alt": f"Photo of {report.pet_name}, a lost {Species.label(report.species).lower()}",
        "title": report.pet_name,
        "chip": chip,
        "chip_kind": kind,
        "subtitle": " · ".join(p for p in parts if p),
        "location_prefix": "Last seen",
        "distance_km": distance_km,
        "place": report.last_seen_place,
        "date_label": f"Reported {format_date(report.created_at)}",
        "person_label": f"Owner: {report.owner.full_name}" if report.owner else None,
        "detail_url": detail_url or url_for("cases.detail", report_id=report.id),
        "message_url": url_for("messages.new", lost_report_id=report.id) if show_message else None,
        "why": explanation,
        "visual_check_unavailable": visual_check_unavailable,
        "case_number": report.case_number,
        "status": report.status,
    }
    card.update(_tier_fields(similarity, tier))
    return card


def sighting_card(
    sighting,  # noqa: ANN001 - Sighting
    *,
    distance_km: float | None = None,
    similarity: float | None = None,
    tier: str | None = None,
    explanation: dict | None = None,
    visual_check_unavailable: bool = False,
    detail_url: str | None = None,
    message_url: str | None = None,
) -> dict[str, Any]:
    """Card for a private Pet Sighting - only render for users allowed to see it."""
    species = Species.label(sighting.species)
    parts = [sighting.breed or species, sighting.colors]
    if sighting.condition != AnimalCondition.UNKNOWN:
        parts.append(AnimalCondition.label(sighting.condition))
    card = {
        "photo_url": _photo(sighting.primary_photo),
        "photo_alt": f"Photo of a sighted {species.lower()}",
        "title": f"Sighted {species.lower()}",
        "chip": f"Seen {timeago(sighting.seen_at)}",
        "chip_kind": "sighting",
        "subtitle": " · ".join(p for p in parts if p),
        "location_prefix": "Seen",
        "distance_km": distance_km,
        "place": sighting.seen_place,
        "date_label": f"Seen {format_date(sighting.seen_at)}",
        "person_label": f"Finder: {sighting.contact_name}",
        "detail_url": detail_url or url_for("sightings.detail", sighting_id=sighting.id),
        "message_url": message_url,
        "why": explanation,
        "visual_check_unavailable": visual_check_unavailable,
        "case_number": None,
        "status": sighting.status,
    }
    card.update(_tier_fields(similarity, tier))
    return card
