"""Matching pipeline: candidate pools, scoring, run_scan, background matching (SPEC 6).

OWNER: engine team. Names, signatures and job payloads are FINAL (SPEC 18 +
docs/CONTRACTS.md).

The pipeline (SPEC 6.1)
-----------------------
1. **Pre-check** (Gemini, manual scans only): exactly one real dog/cat? -> else ScanRejected.
2. **Attributes** (Gemini, cached on ``Photo.ai_attributes``): breed, colours, pattern, size.
3. **Candidate pool** (DB): :func:`get_candidate_pool` - the ONLY place the privacy rules live.
4. **Coarse ranking** (Python): attribute overlap + distance + recency -> top ``MATCH_TOP_N``.
5. **Visual comparison** (Gemini, 4 threads, cached in ``image_comparisons``).
6. **Final score** -> tiers -> ``scans`` + ``scan_results`` rows.

Privacy-critical pool rules (SPEC 6.4), enforced ONLY in get_candidate_pool:

* mode "found" (Found Pet Scanner) -> lost_reports WHERE status='lost' AND is_hidden=0
* mode "lost"  (Lost Pet Scanner)  -> sightings    WHERE status='active' AND is_hidden=0
* a user's own records are never returned to that user.

Scores (all 0-100, SPEC 6.6)
----------------------------
::

    final  = 0.70*visual + 0.15*attribute + 0.10*distance + 0.05*recency   (MATCH_WEIGHTS)
    coarse = (0.15*attribute + 0.10*distance + 0.05*recency) / 0.30

* distance: 100 at 0 km -> 0 at ``MATCH_RADIUS_KM``; 50 (neutral) when a location is unknown.
* recency: 100 for an event today -> 0 at ``MATCH_DATE_WINDOW_DAYS`` ago.
* When the visual check is unavailable (no API key, Gemini error, unreadable photo,
  or Gemini says the photos cannot be compared) the result keeps the coarse score
  multiplied by ``COARSE_ONLY_FACTOR`` (0.6) and is flagged
  ``visual_check_unavailable``. So without a real photo comparison a result can
  never reach the "High" tier or the notification threshold (human-verification
  principle: never alarm an owner on listed details alone).

Background matching (SPEC 5.1 step 4, 5.4, 17.6)
------------------------------------------------
Background matches are stored as ``Scan(trigger='background', user_id=<owner being
notified>)`` so sighting visibility always comes from a scan result the owner owns.
Owners are notified (``notify(owner, "possible_match", ...)``) for results
``>= MATCH_NOTIFY_THRESHOLD`` and never twice for the same (report, sighting) pair.
Finders are never shown the owner's report automatically (no scan is saved for them).

Threads: only Gemini calls run in the ThreadPoolExecutor. All database and file
work happens in the calling thread; worker threads get plain bytes.
"""

from __future__ import annotations

import hashlib
import logging
import re
import statistics
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from flask import current_app, has_request_context, url_for
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    CandidateType,
    ImageComparison,
    LostReport,
    MatchTier,
    Photo,
    PhotoOwner,
    ReportStatus,
    Scan,
    ScanMode,
    ScanResult,
    ScanStatus,
    ScanTrigger,
    Sighting,
    SightingStatus,
    Species,
)
from app.services import storage_service
from app.services.gemini_service import GeminiError, GeminiService
from app.services.job_service import register
from app.utils.geo import distance_or_none
from app.utils.time import utcnow

log = logging.getLogger(__name__)

COARSE_ONLY_FACTOR = 0.6  # scale for scores without a visual check (see module docstring)
NEUTRAL_SCORE = 50.0  # used when a component cannot be computed (unknown location ...)
EVENT_TOLERANCE_DAYS = 3  # a sighting may be dated a little before the "last seen" time
POOL_SQL_LIMIT = 500  # safety cap on rows loaded before Python filtering
SIZE_ORDER = {"small": 0, "medium": 1, "large": 2}
COAT_LENGTH_ORDER = {"hairless": -1, "short": 0, "medium": 1, "long": 2}


class ScanRejected(Exception):
    """The photo cannot be scanned (pre-check failed, not a dog/cat, several animals ...).

    ``code`` (e.g. "not_dog_or_cat", "no_animal", "multiple_animals", "not_a_photo",
    "photo_unreadable"), a friendly ``message`` and optional ``details`` (the pre-check
    dict). ``scan_id`` is set when a rejected Scan row was saved. Routes return it as
    ``json_error(code, message, 422, details)``.
    """

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.scan_id: int | None = None


REJECTION_MESSAGES = {
    "no_animal": (
        "We could not find a dog or cat in this photo. Please try a clearer photo "
        "that shows the animal."
    ),
    "not_dog_or_cat": (
        "This photo does not seem to show a dog or a cat. PawBalik works with dogs and "
        "cats only."
    ),
    "multiple_animals": (
        "This photo shows more than one animal. Please use a photo with only one pet in it."
    ),
    "not_a_photo": (
        "This looks like a drawing, screenshot or toy rather than a photo of a real pet. "
        "Please use a real photo."
    ),
    "photo_unreadable": "We could not read that photo. Please try another one.",
}


def _cfg(key: str) -> Any:
    return current_app.config[key]


# =============================================================================================
# 1. Attribute profiles and scoring math (pure functions - unit tested)
# =============================================================================================

# Coat colour words (English + a few Filipino words) -> one canonical colour.
COLOR_SYNONYMS = {
    "black": "black",
    "itim": "black",
    "white": "white",
    "puti": "white",
    "brown": "brown",
    "chocolate": "brown",
    "liver": "brown",
    "brownish": "brown",
    "kayumanggi": "brown",
    "tan": "tan",
    "fawn": "tan",
    "golden": "tan",
    "gold": "tan",
    "yellow": "tan",
    "blonde": "tan",
    "beige": "tan",
    "cream": "tan",
    "buff": "tan",
    "dilaw": "tan",
    "orange": "orange",
    "ginger": "orange",
    "red": "orange",
    "rust": "orange",
    "gray": "gray",
    "grey": "gray",
    "silver": "gray",
    "blue": "gray",
    "abuhin": "gray",
}
PATTERN_WORDS = {
    "tabby": "tabby",
    "calico": "calico",
    "tortoiseshell": "tortoiseshell",
    "tortie": "tortoiseshell",
    "brindle": "brindle",
    "spotted": "spotted",
    "spots": "spotted",
    "merle": "merle",
    "tricolor": "tricolor",
    "tricolour": "tricolor",
    "bicolor": "bicolor",
    "bicolour": "bicolor",
    "tuxedo": "bicolor",
    "solid": "solid",
    "pointed": "pointed",
    "siamese": "pointed",
}


@dataclass
class Profile:
    """Comparable traits of one animal (from the owner's/finder's form and/or Gemini)."""

    species: str | None = None
    colors: set[str] = field(default_factory=set)
    pattern: str | None = None
    size: str | None = None
    coat_length: str | None = None
    breed: str | None = None


def color_set(values: Iterable[str] | str | None) -> set[str]:
    """Canonical colour words found in free text or a list ("Brown and white" -> {brown, white})."""
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    found: set[str] = set()
    for value in values:
        for word in re.findall(r"[a-z]+", str(value).lower()):
            if word in COLOR_SYNONYMS:
                found.add(COLOR_SYNONYMS[word])
    return found


def pattern_of(text: str | None) -> str | None:
    """A known coat pattern word in ``text``, or None."""
    for word in re.findall(r"[a-z]+", str(text or "").lower()):
        if word in PATTERN_WORDS:
            return PATTERN_WORDS[word]
    return None


def _clean_choice(value: Any, allowed: Iterable[str]) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in set(allowed) else None


def build_profile(
    *,
    ai: dict | None = None,
    species: str | None = None,
    colors_text: str | None = None,
    size: str | None = None,
    breed: str | None = None,
) -> Profile:
    """Merge form fields (preferred - a human entered them) with Gemini attributes."""
    ai = ai or {}
    colors = color_set(colors_text) or color_set(ai.get("primary_colors"))
    pattern = pattern_of(colors_text) or pattern_of(ai.get("coat_pattern"))
    if pattern is None and ai.get("coat_pattern"):
        pattern = str(ai["coat_pattern"]).strip().lower() or None
    if pattern is None and colors:  # infer from the number of colours in the form text
        pattern = {1: "solid", 2: "bicolor"}.get(len(colors), "tricolor")
    return Profile(
        species=_clean_choice(species, Species.ALL)
        or _clean_choice(ai.get("species"), Species.ALL),
        colors=colors,
        pattern=pattern,
        size=_clean_choice(size, SIZE_ORDER) or _clean_choice(ai.get("size_estimate"), SIZE_ORDER),
        coat_length=_clean_choice(ai.get("coat_length"), COAT_LENGTH_ORDER),
        breed=(breed or ai.get("breed_guess") or None),
    )


def record_ai_attributes(record: LostReport | Sighting) -> dict | None:
    """Gemini attributes of a report/sighting (its own copy, else its primary photo's)."""
    if record.ai_attributes:
        return record.ai_attributes
    photo = record.primary_photo
    return photo.ai_attributes if photo is not None else None


def profile_for_record(record: LostReport | Sighting) -> Profile:
    return build_profile(
        ai=record_ai_attributes(record),
        species=record.species,
        colors_text=record.colors,
        size=record.size,
        breed=record.breed,
    )


def _ordinal_similarity(a: str, b: str, order: dict[str, int]) -> float:
    gap = abs(order[a] - order[b])
    return 1.0 if gap == 0 else 0.5 if gap == 1 else 0.0


# (weight, name) of each attribute component inside the attribute score.
ATTRIBUTE_WEIGHTS = {"colors": 0.40, "size": 0.25, "pattern": 0.20, "coat_length": 0.15}


def attribute_comparison(query: Profile, cand: Profile) -> tuple[float, list[str], list[str]]:
    """(attribute score 0-100, matching phrases, difference phrases).

    Only traits known on both sides count; with nothing to compare the score is neutral (50).
    """
    parts: list[tuple[float, float]] = []
    same: list[str] = []
    diff: list[str] = []
    if query.colors and cand.colors:
        overlap = query.colors & cand.colors
        jaccard = len(overlap) / len(query.colors | cand.colors)
        parts.append((ATTRIBUTE_WEIGHTS["colors"], jaccard))
        if overlap:
            same.append(f"Similar coat colours ({', '.join(sorted(overlap))})")
        if query.colors != cand.colors:
            diff.append(
                f"Coat colours differ ({', '.join(sorted(query.colors))} vs "
                f"{', '.join(sorted(cand.colors))})"
            )
    if query.size and cand.size:
        s = _ordinal_similarity(query.size, cand.size, SIZE_ORDER)
        parts.append((ATTRIBUTE_WEIGHTS["size"], s))
        if s == 1.0:
            same.append(f"Same size ({query.size})")
        else:
            diff.append(f"Different size ({query.size} vs {cand.size})")
    if query.pattern and cand.pattern:
        s = 1.0 if query.pattern == cand.pattern else 0.0
        parts.append((ATTRIBUTE_WEIGHTS["pattern"], s))
        if s:
            same.append(f"Same coat pattern ({query.pattern})")
        else:
            diff.append(f"Different coat pattern ({query.pattern} vs {cand.pattern})")
    if query.coat_length and cand.coat_length:
        s = _ordinal_similarity(query.coat_length, cand.coat_length, COAT_LENGTH_ORDER)
        parts.append((ATTRIBUTE_WEIGHTS["coat_length"], s))
        if s == 1.0:
            same.append(f"Same coat length ({query.coat_length})")
        elif s == 0.0:
            diff.append(f"Different coat length ({query.coat_length} vs {cand.coat_length})")
    if not parts:
        return NEUTRAL_SCORE, same, diff
    total = sum(w for w, _ in parts)
    return round(100.0 * sum(w * s for w, s in parts) / total, 1), same, diff


def attribute_score(query: Profile, cand: Profile) -> float:
    """Attribute overlap 0-100 (colours, size, pattern, coat length)."""
    return attribute_comparison(query, cand)[0]


def distance_score(distance_km: float | None, radius_km: float) -> float:
    """100 at 0 km -> 0 at the radius (neutral 50 when unknown)."""
    if distance_km is None:
        return NEUTRAL_SCORE
    if radius_km <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (1.0 - distance_km / radius_km))), 1)


def recency_score(
    event_at: datetime | None, window_days: float, now: datetime | None = None
) -> float:
    """100 for an event today -> 0 at the edge of the date window (neutral 50 when unknown)."""
    if event_at is None:
        return NEUTRAL_SCORE
    if window_days <= 0:
        return 0.0
    age_days = max(0.0, ((now or utcnow()) - event_at).total_seconds() / 86400.0)
    return round(max(0.0, min(100.0, 100.0 * (1.0 - age_days / window_days))), 1)


def _weights() -> dict[str, float]:
    weights = dict(_cfg("MATCH_WEIGHTS"))
    return {k: float(weights.get(k, 0.0)) for k in ("visual", "attribute", "distance", "recency")}


def coarse_score(
    attribute: float, distance: float, recency: float, weights: dict | None = None
) -> float:
    """Score without the visual part: the non-visual weights re-scaled to 0-100."""
    w = weights or _weights()
    total = w["attribute"] + w["distance"] + w["recency"]
    if total <= 0:
        return 0.0
    value = (w["attribute"] * attribute + w["distance"] * distance + w["recency"] * recency) / total
    return round(value, 1)


def final_score(
    visual: float | None,
    attribute: float,
    distance: float,
    recency: float,
    weights: dict | None = None,
) -> float:
    """SPEC 6.6 weighted blend; without a visual score: coarse * COARSE_ONLY_FACTOR."""
    w = weights or _weights()
    if visual is None:
        return round(coarse_score(attribute, distance, recency, w) * COARSE_ONLY_FACTOR, 1)
    total = sum(w.values()) or 1.0
    value = (
        w["visual"] * visual
        + w["attribute"] * attribute
        + w["distance"] * distance
        + w["recency"] * recency
    ) / total
    return round(max(0.0, min(100.0, value)), 1)


def tier_for(score: float, tiers: list | None = None) -> str:
    """ "high" / "moderate" / "low" by MATCH_TIERS, else "hidden"."""
    for tier, minimum, *_ in tiers or _cfg("MATCH_TIERS"):
        if score >= float(minimum):
            return tier
    return MatchTier.HIDDEN


def tier_label(tier: str) -> str:
    for name, _minimum, label in _cfg("MATCH_TIERS"):
        if name == tier:
            return label
    return _cfg("MATCH_HIDDEN_LABEL") if tier == MatchTier.HIDDEN else MatchTier.label(tier)


# =============================================================================================
# 2. Candidate pool (privacy-critical)
# =============================================================================================


def get_candidate_pool(
    mode: str,
    user: Any,
    *,
    species: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    window_days: float | None = None,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
    now: datetime | None = None,
) -> list[LostReport] | list[Sighting]:
    """Candidates for a scan: LostReports (mode "found") or Sightings (mode "lost").

    Privacy rules (never relax these):

    * ``found`` -> Lost Pet Reports with status 'lost' that are not hidden. Never sightings.
    * ``lost``  -> Pet Sightings with status 'active', not hidden and not past ``expires_at``.
    * Records belonging to ``user`` are excluded (``user`` may be None for the CLI).

    Filters: same ``species`` (None = both), event date within ``window_days`` of now
    (default MATCH_DATE_WINDOW_DAYS), optional extra ``not_before``/``not_after`` bounds
    on the event date, and - when ``lat``/``lng`` are given - within ``radius_km``
    (default MATCH_RADIUS_KM, Haversine). Newest events first.
    """
    now = now or utcnow()
    window = float(window_days if window_days is not None else _cfg("MATCH_DATE_WINDOW_DAYS"))
    radius = float(radius_km if radius_km is not None else _cfg("MATCH_RADIUS_KM"))
    uid = getattr(user, "id", None) if getattr(user, "is_authenticated", True) else None

    if mode == ScanMode.FOUND:
        model, event, lat_col, lng_col, owner_col = (
            LostReport,
            LostReport.last_seen_at,
            LostReport.last_seen_lat,
            LostReport.last_seen_lng,
            LostReport.owner_id,
        )
        conditions = [LostReport.status == ReportStatus.LOST, LostReport.is_hidden.is_(False)]
    elif mode == ScanMode.LOST:
        model, event, lat_col, lng_col, owner_col = (
            Sighting,
            Sighting.seen_at,
            Sighting.seen_lat,
            Sighting.seen_lng,
            Sighting.finder_id,
        )
        conditions = [
            Sighting.status == SightingStatus.ACTIVE,
            Sighting.is_hidden.is_(False),
            sa.or_(Sighting.expires_at.is_(None), Sighting.expires_at > now),
        ]
    else:
        raise ValueError(f"Unknown scan mode {mode!r}")

    if uid is not None:
        conditions.append(owner_col != uid)
    if species:
        conditions.append(model.species == species)
    conditions.append(event >= now - timedelta(days=window))
    if not_before is not None:
        conditions.append(event >= not_before)
    if not_after is not None:
        conditions.append(event <= not_after)
    if lat is not None and lng is not None:
        # Cheap bounding box in SQL (uses the lat/lng index); exact Haversine below.
        dlat = radius / 110.574
        dlng = radius / max(1e-6, 111.320 * abs(_cos_deg(lat)))
        conditions += [
            lat_col.between(lat - dlat, lat + dlat),
            lng_col.between(lng - dlng, lng + dlng),
        ]

    stmt = sa.select(model).where(*conditions).order_by(event.desc(), model.id.desc())
    rows = list(db.session.scalars(stmt.limit(POOL_SQL_LIMIT)))
    if lat is None or lng is None:
        return rows
    return [
        r
        for r in rows
        if (d := distance_or_none(lat, lng, *_record_location(r))) is not None and d <= radius
    ]


def _cos_deg(value: float) -> float:
    import math

    return math.cos(math.radians(value))


def _record_location(record: LostReport | Sighting) -> tuple[float | None, float | None]:
    if isinstance(record, LostReport):
        return record.last_seen_lat, record.last_seen_lng
    return record.seen_lat, record.seen_lng


def _record_event_time(record: LostReport | Sighting) -> datetime | None:
    return record.last_seen_at if isinstance(record, LostReport) else record.seen_at


def _candidate_type(record: LostReport | Sighting) -> str:
    return CandidateType.LOST_REPORT if isinstance(record, LostReport) else CandidateType.SIGHTING


# =============================================================================================
# 3. Gemini helpers: attributes (cached on Photo) and visual comparison (cached in DB)
# =============================================================================================


def _gemini() -> GeminiService:
    return GeminiService()


def ensure_photo_attributes(
    photo: Photo, gemini: GeminiService | None = None, image_bytes: bytes | None = None
) -> dict | None:
    """Photo.ai_attributes, extracting them with Gemini once if missing (flushes, no commit).

    Returns None when Gemini is not configured or fails.
    """
    if photo.ai_attributes:
        return photo.ai_attributes
    gemini = gemini or _gemini()
    if not gemini.is_configured():
        return None
    try:
        data = image_bytes if image_bytes is not None else storage_service.image_bytes_for_ai(photo)
        attributes = gemini.extract_attributes(data)
    except (GeminiError, storage_service.StorageError) as exc:
        log.warning("Attribute extraction failed for photo %s: %s", photo.id, exc)
        return None
    photo.ai_attributes = attributes
    db.session.flush()
    return attributes


def _copy_attributes_to_owner(photo: Photo, attributes: dict | None) -> None:
    """A primary photo's attributes are also stored on its report/sighting (SPEC 5.1 step 3)."""
    if not attributes or not photo.is_primary or photo.owner_id is None:
        return
    model = {PhotoOwner.LOST_REPORT: LostReport, PhotoOwner.SIGHTING: Sighting}.get(
        photo.owner_type
    )
    if model is None:
        return
    record = db.session.get(model, photo.owner_id)
    if record is not None and record.ai_attributes != attributes:
        record.ai_attributes = attributes


def _cached_comparisons(
    query_sha256: str, photo_ids: list[int], model_name: str
) -> dict[int, dict]:
    if not photo_ids:
        return {}
    rows = db.session.scalars(
        sa.select(ImageComparison).where(
            ImageComparison.query_sha256 == query_sha256,
            ImageComparison.candidate_photo_id.in_(photo_ids),
            ImageComparison.model_name == model_name,
        )
    )
    return {row.candidate_photo_id: row.result for row in rows}


def _store_comparison(query_sha256: str, photo_id: int, model_name: str, result: dict) -> None:
    """Insert into the cache; a concurrent duplicate is silently ignored (no rollback needed)."""
    stmt = (
        sa.insert(ImageComparison)
        .values(
            query_sha256=query_sha256,
            candidate_photo_id=photo_id,
            model_name=model_name,
            result=result,
            created_at=utcnow(),
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
        .prefix_with("IGNORE", dialect="mysql")
        .prefix_with("IGNORE", dialect="mariadb")
    )
    db.session.execute(stmt)


def compare_many(
    gemini: GeminiService,
    query_bytes: bytes,
    candidates: dict[int, bytes],
) -> dict[int, dict | None]:
    """Run ``gemini.compare`` for several candidate photos with bounded concurrency.

    ``candidates`` maps a key (photo id) to image bytes. Returns key -> comparison
    dict, or None for a comparison that failed. Only Gemini calls run in the threads.
    """
    if not candidates:
        return {}
    workers = max(1, int(current_app.config.get("GEMINI_MAX_WORKERS", 4)))
    attempts = max(1, int(current_app.config.get("GEMINI_MAX_ATTEMPTS", 3)))
    per_call = float(current_app.config.get("GEMINI_TIMEOUT_SECONDS", 30))
    # Generous overall guard: every attempt timing out plus backoff pauses.
    deadline = per_call * attempts + 2**attempts + 5

    results: dict[int, dict | None] = {}
    executor = ThreadPoolExecutor(max_workers=min(workers, len(candidates)))
    try:
        futures = {
            key: executor.submit(gemini.compare, query_bytes, data)
            for key, data in candidates.items()
        }
        for key, future in futures.items():
            try:
                results[key] = future.result(timeout=deadline)
            except FutureTimeout:
                log.warning("Gemini comparison for photo %s timed out", key)
                results[key] = None
            except GeminiError as exc:
                log.warning("Gemini comparison for photo %s failed: %s", key, exc)
                results[key] = None
            except Exception:  # noqa: BLE001 - one bad candidate must not break the scan
                log.exception("Unexpected error comparing photo %s", key)
                results[key] = None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


# =============================================================================================
# 4. Scoring candidates (shared by run_scan, background matching and the CLI)
# =============================================================================================


@dataclass
class Scored:
    """One candidate with every component score (not yet persisted)."""

    candidate: LostReport | Sighting
    photo: Photo | None
    attribute: float
    distance_km: float | None
    distance: float
    recency: float
    coarse: float
    visual: float | None = None
    final: float = 0.0
    tier: str = MatchTier.HIDDEN
    visual_check_unavailable: bool = True
    explanation: dict = field(default_factory=dict)

    @property
    def candidate_type(self) -> str:
        return _candidate_type(self.candidate)


@dataclass
class ScoringOutcome:
    results: list[Scored]
    gemini_ok: bool


def score_candidates(
    *,
    query_bytes: bytes | None,
    query_sha256: str,
    query_profile: Profile,
    candidates: list,
    lat: float | None,
    lng: float | None,
    gemini: GeminiService | None = None,
    now: datetime | None = None,
    top_n: int | None = None,
) -> ScoringOutcome:
    """Coarse-rank ``candidates``, visually compare the top N, return them sorted by final score."""
    gemini = gemini or _gemini()
    now = now or utcnow()
    weights = _weights()
    radius = float(_cfg("MATCH_RADIUS_KM"))
    window = float(_cfg("MATCH_DATE_WINDOW_DAYS"))
    top_n = int(top_n if top_n is not None else _cfg("MATCH_TOP_N"))

    # Coarse ranking (SPEC 6.4).
    scored: list[Scored] = []
    for cand in candidates:
        attr, same, diff = attribute_comparison(query_profile, profile_for_record(cand))
        km = distance_or_none(lat, lng, *_record_location(cand))
        dist = distance_score(km, radius)
        rec = recency_score(_record_event_time(cand), window, now)
        item = Scored(
            candidate=cand,
            photo=cand.primary_photo,
            attribute=attr,
            distance_km=round(km, 2) if km is not None else None,
            distance=dist,
            recency=rec,
            coarse=coarse_score(attr, dist, rec, weights),
        )
        item.explanation = {"matching_features": same, "differences": diff, "confidence": "low"}
        scored.append(item)
    scored.sort(key=lambda s: (s.coarse, s.recency), reverse=True)
    scored = scored[:top_n]

    # Visual comparison (SPEC 6.5): cache first, then Gemini for the rest.
    gemini_ok = gemini.is_configured() and query_bytes is not None
    comparisons: dict[int, dict | None] = {}
    if gemini_ok:
        photo_ids = [s.photo.id for s in scored if s.photo is not None]
        comparisons.update(_cached_comparisons(query_sha256, photo_ids, gemini.model))
        to_fetch: dict[int, bytes] = {}
        for s in scored:
            if s.photo is None or s.photo.id in comparisons or s.photo.id in to_fetch:
                continue
            try:
                to_fetch[s.photo.id] = storage_service.image_bytes_for_ai(s.photo)
            except storage_service.StorageError as exc:
                log.warning("Candidate photo %s unreadable: %s", s.photo.id, exc)
                comparisons[s.photo.id] = None
                gemini_ok = False
        fresh = compare_many(gemini, query_bytes, to_fetch)
        for photo_id, result in fresh.items():
            comparisons[photo_id] = result
            if result is None:
                gemini_ok = False
            else:
                _store_comparison(query_sha256, photo_id, gemini.model, result)

    for s in scored:
        result = comparisons.get(s.photo.id) if s.photo is not None else None
        if result is not None and result.get("comparison_possible", True):
            s.visual = float(result.get("visual_similarity", 0))
            s.visual_check_unavailable = False
            s.explanation = {
                "matching_features": list(result.get("matching_features") or []),
                "differences": list(result.get("differences") or []),
                "confidence": result.get("confidence") or "low",
            }
        else:
            s.visual = None
            s.visual_check_unavailable = True
            if result is not None:
                s.explanation["note"] = "The photos could not be compared reliably."
        s.final = final_score(s.visual, s.attribute, s.distance, s.recency, weights)
        s.tier = tier_for(s.final)

    scored.sort(key=lambda s: (s.final, s.coarse), reverse=True)
    return ScoringOutcome(results=scored, gemini_ok=bool(gemini_ok))


def _new_result(scan: Scan, s: Scored, rank: int) -> ScanResult:
    return ScanResult(
        scan_id=scan.id,
        candidate_type=s.candidate_type,
        candidate_id=s.candidate.id,
        candidate_photo_id=s.photo.id if s.photo is not None else None,
        visual_score=s.visual,
        attribute_score=s.attribute,
        distance_km=s.distance_km,
        distance_score=s.distance,
        recency_score=s.recency,
        final_score=s.final,
        tier=s.tier,
        rank=rank,
        visual_check_unavailable=s.visual_check_unavailable,
        explanation=s.explanation,
    )


# =============================================================================================
# 5. run_scan and results_for_display
# =============================================================================================


def _reject(scan: Scan, code: str, details: Any = None) -> ScanRejected:
    """Mark the scan rejected, commit it (every scan is persisted) and build the exception."""
    message = REJECTION_MESSAGES.get(code, "This photo cannot be scanned.")
    scan.status = ScanStatus.REJECTED
    scan.error = message
    scan.completed_at = utcnow()
    db.session.commit()
    exc = ScanRejected(code, message, details)
    exc.scan_id = scan.id
    return exc


def check_precheck(precheck: dict) -> str | None:
    """The rejection code for a pre-check answer (SPEC 6.2), or None if the photo is OK."""
    species = precheck.get("species")
    if not precheck.get("contains_animal") or species == "none":
        return "no_animal"
    if species not in Species.ALL:
        return "not_dog_or_cat"
    if int(precheck.get("animal_count") or 0) != 1:
        return "multiple_animals"
    if not precheck.get("is_real_photo", True):
        return "not_a_photo"
    return None


def quality_warning(scan: Scan) -> str | None:
    """Friendly note when the pre-check rated the photo 'poor' (the scan still ran)."""
    precheck = scan.precheck or {}
    if precheck.get("image_quality") != "poor":
        return None
    issues = ", ".join(precheck.get("quality_issues") or [])
    detail = f" ({issues})" if issues else ""
    return (
        f"The photo quality looks poor{detail}. Results may be less accurate - try a clearer "
        "photo in good light."
    )


def _precheck_and_attributes(
    gemini: GeminiService, photo: Photo, image_bytes: bytes, *, want_precheck: bool
) -> tuple[dict | None, dict | None, bool]:
    """(precheck, attributes, ok). Runs both Gemini calls at the same time when both are needed."""
    need_attributes = not photo.ai_attributes
    jobs: dict[str, Any] = {}
    if want_precheck:
        jobs["precheck"] = gemini.precheck
    if need_attributes:
        jobs["attributes"] = gemini.extract_attributes
    answers: dict[str, dict | None] = {}
    ok = True
    if jobs:
        executor = ThreadPoolExecutor(max_workers=len(jobs))
        try:
            futures = {name: executor.submit(fn, image_bytes) for name, fn in jobs.items()}
            for name, future in futures.items():
                try:
                    answers[name] = future.result()
                except GeminiError as exc:
                    log.warning("Gemini %s failed: %s", name, exc)
                    answers[name] = None
                    ok = False
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    attributes = photo.ai_attributes
    if need_attributes and answers.get("attributes"):
        attributes = answers["attributes"]
        photo.ai_attributes = attributes
    return answers.get("precheck"), attributes, ok


def run_scan(
    *,
    user: Any,
    mode: str,
    photo: Photo,
    lat: float | None = None,
    lng: float | None = None,
    source_lost_report: LostReport | None = None,
    source_sighting: Sighting | None = None,
    trigger: str = "manual",
    species: str | None = None,
) -> Scan:
    """Run the full pipeline (SPEC 6.1) and persist ``scans`` + ``scan_results`` (commits).

    * ``mode`` "found" searches public lost reports, "lost" searches private sightings.
    * ``photo`` is the query photo (an uploaded scan photo is attached to the new Scan).
    * ``lat``/``lng`` default to the source record's location.
    * ``species`` is an optional hint used only when neither the source record nor
      Gemini can tell (e.g. no API key).
    * Manual scans run the Gemini pre-check and raise :class:`ScanRejected` (after saving
      a rejected Scan row) for photos that are not one real dog/cat. Background scans
      (``trigger="background"``, ``user`` = the owner being notified) skip the pre-check
      and keep only results at or above MATCH_RELEVANCE_THRESHOLD.

    Falls back to coarse scores with ``visual_check_unavailable=True`` when Gemini is
    unavailable (``Scan.gemini_ok`` is then False).
    """
    if mode not in ScanMode.ALL:
        raise ValueError(f"Unknown scan mode {mode!r}")
    source = source_lost_report or source_sighting
    if lat is None or lng is None:
        lat, lng = _record_location(source) if source is not None else (None, None)

    scan = Scan(
        user_id=user.id,
        mode=mode,
        trigger=trigger,
        query_photo_id=photo.id,
        source_lost_report_id=source_lost_report.id if source_lost_report else None,
        source_sighting_id=source_sighting.id if source_sighting else None,
        scan_lat=lat,
        scan_lng=lng,
        status=ScanStatus.RUNNING,
    )
    db.session.add(scan)
    db.session.flush()
    if photo.owner_type == PhotoOwner.SCAN and photo.owner_id is None:
        photo.attach_to(PhotoOwner.SCAN, scan.id)

    try:
        _run_pipeline(scan, user=user, photo=photo, source=source, species_hint=species)
    except ScanRejected:
        raise
    except Exception as exc:
        log.exception("Scan %s failed", scan.id)
        try:
            scan.status = ScanStatus.FAILED
            scan.error = f"{type(exc).__name__}: {exc}"[:1000]
            scan.completed_at = utcnow()
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
        raise
    return scan


def _run_pipeline(
    scan: Scan, *, user: Any, photo: Photo, source: Any, species_hint: str | None
) -> None:
    gemini = _gemini()
    manual = scan.trigger == ScanTrigger.MANUAL
    try:
        query_bytes = storage_service.image_bytes_for_ai(photo)
    except storage_service.StorageError:
        if manual:
            raise _reject(scan, "photo_unreadable") from None
        query_bytes = None

    precheck, attributes, gemini_ok = None, photo.ai_attributes, True
    if gemini.is_configured() and query_bytes is not None:
        precheck, attributes, gemini_ok = _precheck_and_attributes(
            gemini, photo, query_bytes, want_precheck=manual
        )
    else:
        gemini_ok = False
    scan.precheck = precheck
    scan.attributes = attributes
    _copy_attributes_to_owner(photo, attributes)
    if precheck is not None:
        code = check_precheck(precheck)
        if code is not None:
            raise _reject(scan, code, precheck)

    # Species: the owner's/finder's record, else what Gemini saw, else the caller's hint.
    species = (
        getattr(source, "species", None)
        or _clean_choice((precheck or {}).get("species"), Species.ALL)
        or _clean_choice((attributes or {}).get("species"), Species.ALL)
        or _clean_choice(species_hint, Species.ALL)
    )
    if source is not None:  # form fields first, Gemini attributes fill the gaps
        query_profile = build_profile(
            ai=record_ai_attributes(source) or attributes,
            species=source.species,
            colors_text=source.colors,
            size=source.size,
            breed=source.breed,
        )
    else:
        query_profile = build_profile(ai=attributes, species=species)

    not_before = not_after = None
    tolerance = timedelta(days=EVENT_TOLERANCE_DAYS)
    if isinstance(source, LostReport) and scan.mode == ScanMode.LOST:
        not_before = source.last_seen_at - tolerance  # a sighting cannot predate the loss
    if isinstance(source, Sighting) and scan.mode == ScanMode.FOUND:
        not_after = source.seen_at + tolerance  # the pet was lost before it was seen

    pool = get_candidate_pool(
        scan.mode,
        user,
        species=species,
        lat=scan.scan_lat,
        lng=scan.scan_lng,
        not_before=not_before,
        not_after=not_after,
    )
    outcome = score_candidates(
        query_bytes=query_bytes,
        query_sha256=photo.sha256,
        query_profile=query_profile,
        candidates=pool,
        lat=scan.scan_lat,
        lng=scan.scan_lng,
        gemini=gemini,
    )
    relevance = float(_cfg("MATCH_RELEVANCE_THRESHOLD"))
    results = outcome.results
    if not manual:
        results = [s for s in results if s.final >= relevance]
    for rank, s in enumerate(results, start=1):
        db.session.add(_new_result(scan, s, rank))

    scan.gemini_ok = bool(gemini_ok and outcome.gemini_ok)
    scan.result_count = sum(1 for s in results if s.final >= relevance)
    scan.status = ScanStatus.COMPLETE
    scan.completed_at = utcnow()
    db.session.commit()


def results_for_display(scan: Scan, include_weak: bool = False) -> list[dict]:
    """Ranked result dicts for templates/JSON.

    Keys: every ``pet_card`` key (app/utils/cards.py) plus ``result_id``,
    ``candidate_type``, ``candidate_id``, ``final_score``, ``visual_score``,
    ``attribute_score``, ``distance_score``, ``recency_score``, ``rank``,
    ``visual_check_unavailable``. Results in the hidden tier (< 40 %) are omitted
    unless ``include_weak``. Candidates hidden by a moderator are never returned.
    Callers still apply ``policy_service`` for the viewer (scanner routes do).
    """
    from app.utils.cards import lost_report_card, sighting_card

    rows: list[dict] = []
    for result in scan.results:
        if result.tier == MatchTier.HIDDEN and not include_weak:
            continue
        candidate = result.candidate
        if candidate is None or candidate.is_hidden:
            continue
        why = dict(result.explanation or {})
        common = {
            "distance_km": result.distance_km,
            "similarity": result.final_score,
            "tier": result.tier,
            "explanation": why,
            "visual_check_unavailable": result.visual_check_unavailable,
        }
        if result.candidate_type == CandidateType.LOST_REPORT:
            card = lost_report_card(candidate, **common)
        else:
            message_url = None
            if scan.source_lost_report_id:
                message_url = url_for(
                    "messages.new",
                    lost_report_id=scan.source_lost_report_id,
                    sighting_id=candidate.id,
                )
            card = sighting_card(
                candidate,
                detail_url=url_for("sightings.detail", sighting_id=candidate.id),
                message_url=message_url,
                **common,
            )
        card["tier_label"] = tier_label(result.tier)
        card.update(
            {
                "result_id": result.id,
                "candidate_type": result.candidate_type,
                "candidate_id": result.candidate_id,
                "final_score": result.final_score,
                "visual_score": result.visual_score,
                "attribute_score": result.attribute_score,
                "distance_score": result.distance_score,
                "recency_score": result.recency_score,
                "rank": result.rank,
                "visual_check_unavailable": result.visual_check_unavailable,
            }
        )
        rows.append(card)
    return rows


# =============================================================================================
# 6. Background matching (jobs) and notifications
# =============================================================================================


def _relative_url(endpoint: str, **values: Any) -> str:
    """url_for that also works in the worker (no request context)."""
    if has_request_context():
        return url_for(endpoint, **values)
    with current_app.test_request_context():
        return url_for(endpoint, **values)


def _already_notified(report_id: int, sighting_id: int) -> bool:
    stmt = (
        sa.select(ScanResult.id)
        .join(Scan, Scan.id == ScanResult.scan_id)
        .where(
            Scan.source_lost_report_id == report_id,
            ScanResult.candidate_type == CandidateType.SIGHTING,
            ScanResult.candidate_id == sighting_id,
            ScanResult.notified_at.is_not(None),
        )
        .limit(1)
    )
    return db.session.scalar(stmt) is not None


def _notify_owner(scan: Scan, result: ScanResult, report: LostReport) -> None:
    from app.services.notification_service import notify

    percent = int(round(result.final_score))
    notify(
        report.owner,
        "possible_match",
        {
            "scan_id": scan.id,
            "lost_report_id": report.id,
            "sighting_id": result.candidate_id,
            "similarity": percent,
            "case_number": report.case_number,
            "link_url": _relative_url("scanner.results", scan_id=scan.id),
            "title": f"Possible match for {report.pet_name} ({percent}% visual similarity)",
            "body": (
                f"A new pet sighting looks similar to {report.pet_name} ({report.case_number}). "
                "Similarity is a visual indicator only - please check the details."
            ),
        },
    )
    result.notified_at = utcnow()


def _should_notify(result: ScanResult | Scored) -> bool:
    final = result.final_score if isinstance(result, ScanResult) else result.final
    unavailable = result.visual_check_unavailable
    return not unavailable and final >= float(_cfg("MATCH_NOTIFY_THRESHOLD"))


def match_new_lost_report(report_id: int) -> Scan | None:
    """Background: extract attributes if missing, then match the report against active
    sightings as a background Scan owned by the report's owner; notify the owner
    for results >= MATCH_NOTIFY_THRESHOLD (once per report/sighting pair). Commits."""
    report = db.session.get(LostReport, report_id)
    if report is None or not report.is_searchable:
        return None
    photo = report.primary_photo
    if photo is None:
        log.info("Lost report %s has no photo; nothing to match", report_id)
        return None
    attributes = ensure_photo_attributes(photo)
    _copy_attributes_to_owner(photo, attributes)
    scan = run_scan(
        user=report.owner,
        mode=ScanMode.LOST,
        photo=photo,
        source_lost_report=report,
        trigger=ScanTrigger.BACKGROUND,
    )
    for result in scan.results:
        if (
            result.candidate_type == CandidateType.SIGHTING
            and _should_notify(result)
            and not _already_notified(report.id, result.candidate_id)
        ):
            _notify_owner(scan, result, report)
    db.session.commit()
    return scan


def match_new_sighting(sighting_id: int) -> list[Scan]:
    """Background: match a new sighting against active lost reports. For each report whose
    result is >= MATCH_NOTIFY_THRESHOLD, save a background Scan for that report's OWNER
    (query = the report's photo, one result = this sighting) and notify them.

    The finder is never shown the reports (no scan is saved for the finder). Commits.
    """
    sighting = db.session.get(Sighting, sighting_id)
    if sighting is None or not sighting.is_searchable:
        return []
    photo = sighting.primary_photo
    if photo is None:
        return []
    gemini = _gemini()
    try:
        query_bytes = storage_service.image_bytes_for_ai(photo)
    except storage_service.StorageError:
        log.warning("Sighting %s photo unreadable; skipping background match", sighting_id)
        return []
    attributes = ensure_photo_attributes(photo, gemini, query_bytes)
    _copy_attributes_to_owner(photo, attributes)

    pool = get_candidate_pool(
        ScanMode.FOUND,
        sighting.finder,
        species=sighting.species,
        lat=sighting.seen_lat,
        lng=sighting.seen_lng,
        not_after=sighting.seen_at + timedelta(days=EVENT_TOLERANCE_DAYS),
    )
    outcome = score_candidates(
        query_bytes=query_bytes,
        query_sha256=photo.sha256,
        query_profile=profile_for_record(sighting),
        candidates=pool,
        lat=sighting.seen_lat,
        lng=sighting.seen_lng,
        gemini=gemini,
    )

    window = float(_cfg("MATCH_DATE_WINDOW_DAYS"))
    scans: list[Scan] = []
    for s in outcome.results:
        report: LostReport = s.candidate  # type: ignore[assignment]
        # Same comparison seen from the owner's side: the candidate is the sighting,
        # so recency is about when the sighting happened.
        mirrored = Scored(
            candidate=sighting,
            photo=photo,
            attribute=s.attribute,
            distance_km=s.distance_km,
            distance=s.distance,
            recency=recency_score(sighting.seen_at, window),
            coarse=s.coarse,
            visual=s.visual,
            visual_check_unavailable=s.visual_check_unavailable,
            explanation=s.explanation,
        )
        mirrored.final = final_score(s.visual, s.attribute, s.distance, mirrored.recency)
        mirrored.tier = tier_for(mirrored.final)
        if not _should_notify(mirrored) or _already_notified(report.id, sighting.id):
            continue
        owner_scan = Scan(
            user_id=report.owner_id,  # SPEC 17.6: the owner being notified
            mode=ScanMode.LOST,
            trigger=ScanTrigger.BACKGROUND,
            query_photo_id=report.primary_photo.id if report.primary_photo else None,
            source_lost_report_id=report.id,
            scan_lat=report.last_seen_lat,
            scan_lng=report.last_seen_lng,
            precheck=None,
            attributes=record_ai_attributes(report),
            status=ScanStatus.COMPLETE,
            result_count=1,
            gemini_ok=True,
            completed_at=utcnow(),
        )
        db.session.add(owner_scan)
        db.session.flush()
        result = _new_result(owner_scan, mirrored, 1)
        db.session.add(result)
        db.session.flush()
        _notify_owner(owner_scan, result, report)
        scans.append(owner_scan)
    db.session.commit()
    return scans


def extract_attributes_for_photo(photo_id: int) -> dict | None:
    """Store Gemini attributes on Photo.ai_attributes (and on the owning report/
    sighting when it is the primary photo). Flushes; the job handler commits."""
    photo = db.session.get(Photo, photo_id)
    if photo is None:
        return None
    attributes = ensure_photo_attributes(photo)
    _copy_attributes_to_owner(photo, attributes)
    db.session.flush()
    return attributes


def cleanup_scan_photos() -> int:
    """Delete scan photos older than SCAN_PHOTO_RETENTION_DAYS (SPEC 8). Scans keep their
    scores; ``Scan.query_photo_id`` is cleared. Returns the number deleted. Commits."""
    cutoff = utcnow() - timedelta(days=int(_cfg("SCAN_PHOTO_RETENTION_DAYS")))
    photos = list(
        db.session.scalars(
            sa.select(Photo).where(Photo.owner_type == PhotoOwner.SCAN, Photo.created_at < cutoff)
        )
    )
    for photo in photos:
        storage_service.delete_photo(photo)
    db.session.commit()
    if photos:
        log.info("Deleted %s old scan photos", len(photos))
    return len(photos)


# =============================================================================================
# 7. Command-line tools: python run.py match-test / eval-matching
# =============================================================================================


def _load_cli_image(path: Path) -> tuple[bytes, str]:
    """(JPEG bytes <= AI_IMAGE_MAX_EDGE, sha256 of the original file)."""
    original, clean, _w, _h = storage_service.validate_image(path)
    return storage_service.jpeg_for_ai(clean), hashlib.sha256(original).hexdigest()


def cli_match_test(
    photo_path: str | Path, mode: str = "found", lat: float | None = None, lng: float | None = None
) -> int:
    """``python run.py match-test <photo>``: print pre-check, attributes and ranked
    results for a photo WITHOUT saving a scan. Works without a Gemini key (coarse only).
    Returns a process exit code."""
    path = Path(photo_path)
    try:
        query_bytes, sha256 = _load_cli_image(path)
    except storage_service.ImageValidationError as exc:
        print(f"Cannot use {path}: {exc.message}")
        return 1
    if mode not in ScanMode.ALL:
        print("--mode must be 'found' or 'lost'")
        return 1

    gemini = _gemini()
    print(f"Photo: {path.name}  mode: {mode}  model: {gemini.model}")
    precheck = attributes = None
    if gemini.is_configured():
        try:
            precheck = gemini.precheck(query_bytes)
            print(f"Pre-check: {precheck}")
            code = check_precheck(precheck)
            if code:
                print(f"REJECTED ({code}): {REJECTION_MESSAGES[code]}")
                return 3
            attributes = gemini.extract_attributes(query_bytes)
            print(f"Attributes: {attributes}")
        except GeminiError as exc:
            print(f"Gemini unavailable ({exc}); continuing with coarse ranking only.")
    else:
        print("GEMINI_API_KEY is not set: no pre-check, attributes or visual comparison.")

    species = _clean_choice((precheck or {}).get("species"), Species.ALL) or _clean_choice(
        (attributes or {}).get("species"), Species.ALL
    )
    try:
        pool = get_candidate_pool(mode, None, species=species, lat=lat, lng=lng)
        outcome = score_candidates(
            query_bytes=query_bytes,
            query_sha256=sha256,
            query_profile=build_profile(ai=attributes, species=species),
            candidates=pool,
            lat=lat,
            lng=lng,
            gemini=gemini,
        )
        db.session.commit()  # keep cached comparisons
    except SQLAlchemyError as exc:
        db.session.rollback()
        print(f"Cannot read candidates from the database: {type(exc).__name__}")
        print("Is the database running (XAMPP MySQL) and initialised (python run.py init-db)?")
        return 2

    kind = "lost reports" if mode == ScanMode.FOUND else "sightings"
    print(f"\nCandidates in pool: {len(pool)} {kind}; compared: {len(outcome.results)}")
    if not outcome.results:
        print("No candidates.")
        return 0
    header = f"{'#':>2} {'id':>5} {'final':>6} {'visual':>6} {'attr':>5} {'dist':>5} {'rec':>5} {'km':>6}  tier"
    print(header)
    print("-" * len(header))
    for rank, s in enumerate(outcome.results, start=1):
        visual = f"{s.visual:.0f}" if s.visual is not None else "n/a"
        km = f"{s.distance_km:.1f}" if s.distance_km is not None else "?"
        label = getattr(s.candidate, "case_number", None) or f"sighting {s.candidate.id}"
        print(
            f"{rank:>2} {s.candidate.id:>5} {s.final:>6.1f} {visual:>6} {s.attribute:>5.0f} "
            f"{s.distance:>5.0f} {s.recency:>5.0f} {km:>6}  {tier_label(s.tier)} ({label})"
        )
    print("\nSimilarity percentages are visual indicators only, not proof of identity.")
    return 0


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
PAIR_SUFFIX = re.compile(r"[_\-. ](a|b|1|2)$", re.IGNORECASE)


def find_pairs(folder: Path) -> list[tuple[Path, Path]]:
    """Image pairs in ``folder``: every sub-folder with exactly two images, plus files
    named ``<name>_a.jpg`` / ``<name>_b.jpg`` (also ``-a``/``-b``, ``_1``/``_2``)."""
    if not folder.is_dir():
        return []
    pairs: list[tuple[Path, Path]] = []
    groups: dict[str, list[Path]] = {}
    for entry in sorted(folder.iterdir()):
        if entry.is_dir():
            images = sorted(p for p in entry.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
            if len(images) == 2:
                pairs.append((images[0], images[1]))
        elif entry.suffix.lower() in IMAGE_SUFFIXES:
            stem = PAIR_SUFFIX.sub("", entry.stem)
            groups.setdefault(stem.lower(), []).append(entry)
    for files in groups.values():
        if len(files) == 2:
            pairs.append((files[0], files[1]))
    return pairs


def _summary(scores: list[float]) -> dict[str, float | int | None]:
    if not scores:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(scores),
        "mean": round(statistics.mean(scores), 1),
        "median": round(statistics.median(scores), 1),
        "min": min(scores),
        "max": max(scores),
    }


def suggest_threshold(same: list[float], different: list[float]) -> float | None:
    """The cut-off that best separates the two groups (max true-positive minus
    false-positive rate; ties -> the higher, more cautious value)."""
    if not same or not different:
        return None
    best, best_gain = None, -2.0
    for t in sorted(set(same) | set(different)):
        tpr = sum(s >= t for s in same) / len(same)
        fpr = sum(d >= t for d in different) / len(different)
        gain = tpr - fpr
        if gain > best_gain or (gain == best_gain and best is not None and t > best):
            best, best_gain = t, gain
    return best


def evaluate_folder(folder: str | Path) -> dict:
    """``python run.py eval-matching <folder>``: Gemini visual similarity for labelled
    photo pairs in ``<folder>/same/`` (same pet) and ``<folder>/different/``.
    Prints mean/median per group and a suggested threshold; returns the numbers."""
    root = Path(folder)
    gemini = _gemini()
    if not gemini.is_configured():
        print("GEMINI_API_KEY is not set; the evaluation needs Gemini.")
        return {"error": "gemini_not_configured"}
    groups = {"same": find_pairs(root / "same"), "different": find_pairs(root / "different")}
    if not any(groups.values()):
        print(f"No photo pairs found under {root}/same and {root}/different.")
        return {"error": "no_pairs"}

    scores: dict[str, list[float]] = {"same": [], "different": []}
    rows: list[dict] = []
    for label, pairs in groups.items():
        for a, b in pairs:
            try:
                bytes_a, _ = _load_cli_image(a)
                bytes_b, _ = _load_cli_image(b)
                result = gemini.compare(bytes_a, bytes_b)
            except (storage_service.ImageValidationError, GeminiError) as exc:
                print(f"  skipped {a.name} / {b.name}: {getattr(exc, 'message', exc)}")
                continue
            score = float(result["visual_similarity"])
            scores[label].append(score)
            rows.append({"group": label, "a": str(a), "b": str(b), "visual_similarity": score})
            print(f"  [{label:9}] {a.name} vs {b.name}: {score:.0f}")

    report = {
        "same": _summary(scores["same"]),
        "different": _summary(scores["different"]),
        "suggested_threshold": suggest_threshold(scores["same"], scores["different"]),
        "model": gemini.model,
        "pairs": rows,
    }
    print("\nGroup       n   mean  median   min   max")
    for label in ("same", "different"):
        s = report[label]
        if s["n"]:
            print(
                f"{label:9} {s['n']:>3} {s['mean']:>6} {s['median']:>7} {s['min']:>5.0f} "
                f"{s['max']:>5.0f}"
            )
    if report["suggested_threshold"] is not None:
        print(
            f"\nSuggested visual-similarity threshold: {report['suggested_threshold']:.0f} "
            "(compare with MATCH_NOTIFY_THRESHOLD; the final score also includes attributes, "
            "distance and recency)."
        )
    return {k: v for k, v in report.items() if k != "pairs"} | {"pair_count": len(rows)}


# =============================================================================================
# 8. Job handlers (payload contracts are FINAL)
# =============================================================================================


@register("match_lost_report")
def _job_match_lost_report(payload: dict) -> None:
    """payload: {"lost_report_id": int}"""
    match_new_lost_report(int(payload["lost_report_id"]))


@register("match_sighting")
def _job_match_sighting(payload: dict) -> None:
    """payload: {"sighting_id": int}"""
    match_new_sighting(int(payload["sighting_id"]))


@register("extract_attributes")
def _job_extract_attributes(payload: dict) -> None:
    """payload: {"photo_id": int}"""
    extract_attributes_for_photo(int(payload["photo_id"]))
    db.session.commit()


@register("cleanup_scan_photos")
def _job_cleanup_scan_photos(payload: dict) -> None:
    """payload: {}"""
    cleanup_scan_photos()
