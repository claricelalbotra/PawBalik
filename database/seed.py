"""Sample data: admin account, SAMPLE rescue providers, demo reports and sightings (SPEC 7).

Run with ``python run.py seed`` (or ``python run.py seed --reset-demo``).
Must run inside an app context (run.py provides one).

What it creates - every step is idempotent, so running it twice changes nothing:

* **Administrator** from ``ADMIN_EMAIL`` / ``ADMIN_PASSWORD``. Without
  ``ADMIN_PASSWORD`` a random password is generated and shown ONCE in the summary.
* **SAMPLE providers** (``is_sample=True``) with generic names such as
  "SAMPLE Veterinary Clinic - Downtown". They are not real businesses and use
  placeholder phone numbers (+63 900 000 00xx), so nobody real gets called.
* **Demo accounts** (``@demo.pawbalik.example.com``, one shared generated password
  shown once), **demo lost reports** and **demo sightings** with simple dog/cat
  illustrations drawn with Pillow and saved through ``storage_service.save_image``.

``reset_demo=True`` deletes ONLY the seeded demo data - reports and sightings of the
demo accounts (with their photos, history, conversations, scan results and rescue
requests) and the SAMPLE providers - then recreates it and gives the demo accounts
a new password. Real users' data is never touched.
"""

from __future__ import annotations

import io
import secrets
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from flask import current_app
from PIL import Image, ImageDraw

from app.extensions import db
from app.models import (
    AbuseReport,
    AssistanceProvider,
    CandidateType,
    CaseStatusHistory,
    Conversation,
    ImageComparison,
    LostReport,
    Message,
    Photo,
    PhotoOwner,
    ReportStatus,
    RescueRequest,
    Scan,
    ScanResult,
    Sighting,
    SightingAccessLog,
    User,
    UserRole,
)
from app.utils.case_numbers import next_case_number
from app.utils.time import utcnow

DEMO_EMAIL_DOMAIN = "demo.pawbalik.example.com"

# --- SAMPLE providers ---------------------------------------------------------------------
# Plausible points inside Tacloban City barangays; generic names; placeholder numbers.
SAMPLE_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "SAMPLE Veterinary Clinic - Downtown",
        "type": "vet",
        "address": "Sample address near the city hall area, Tacloban City",
        "barangay": "Downtown",
        "lat": 11.2445,
        "lng": 125.0040,
        "phones": "+63 900 000 0001",
        "hours_text": "Mon-Sat 8:00 AM - 5:00 PM",
        "services": "Consultation, vaccination, deworming, basic wound care",
    },
    {
        "name": "SAMPLE 24/7 Animal Emergency Clinic - Marasbaras",
        "type": "vet",
        "address": "Sample address along the national highway, Tacloban City",
        "barangay": "Marasbaras",
        "lat": 11.2150,
        "lng": 124.9925,
        "phones": "+63 900 000 0002",
        "hours_text": "Open 24 hours",
        "is_24_7": True,
        "services": "Emergency care, surgery, confinement",
    },
    {
        "name": "SAMPLE Animal Rescue Volunteers - San Jose",
        "type": "rescuer",
        "address": "Sample meeting point near the airport road, Tacloban City",
        "barangay": "San Jose",
        "lat": 11.2230,
        "lng": 125.0185,
        "phones": "+63 900 000 0003",
        "hours_text": "Daily 7:00 AM - 7:00 PM",
        "services": "Pick-up of injured strays, temporary foster, transport to vets",
    },
    {
        "name": "SAMPLE City Animal Shelter - Abucay",
        "type": "shelter",
        "address": "Sample shelter compound, Tacloban City",
        "barangay": "Abucay",
        "lat": 11.2560,
        "lng": 124.9950,
        "phones": "+63 900 000 0004",
        "hours_text": "Mon-Fri 8AM-5PM; Sat 8AM-12PM; Sun closed",
        "services": "Impounded animals, adoption, anti-rabies vaccination drives",
    },
    {
        "name": "SAMPLE Pet Clinic - Sagkahan",
        "type": "vet",
        "address": "Sample address, Sagkahan, Tacloban City",
        "barangay": "Sagkahan",
        "lat": 11.2350,
        "lng": 125.0005,
        "phones": "+63 900 000 0005",
        "hours_text": "Mon-Sun 9:00 AM - 6:00 PM",
        "services": "Consultation, grooming, vaccination",
    },
    {
        "name": "SAMPLE Stray Care Group - Caibaan",
        "type": "rescuer",
        "address": "Sample community group, Caibaan, Tacloban City",
        "barangay": "Caibaan",
        "lat": 11.2240,
        "lng": 124.9900,
        "phones": "+63 900 000 0006",
        "hours_text": "Message us anytime, replies within the day",  # unknown format on purpose
        "services": "Feeding program, rescue coordination, rehoming",
    },
    {
        "name": "SAMPLE Veterinary Hospital - V&G Subdivision",
        "type": "vet",
        "address": "Sample address, V&G Subdivision, Tacloban City",
        "barangay": "V&G Subdivision",
        "lat": 11.2330,
        "lng": 124.9955,
        "phones": "+63 900 000 0007",
        "hours_text": "Mon-Sat 8AM-8PM",
        "services": "Laboratory tests, x-ray, surgery, confinement",
    },
    {
        "name": "SAMPLE Cat Rescue Network - Downtown",
        "type": "rescuer",
        "address": "Sample volunteer hotline, Tacloban City",
        "barangay": "Downtown",
        "lat": 11.2425,
        "lng": 125.0060,
        "phones": "+63 900 000 0008",
        "hours_text": "24/7 hotline",
        "is_24_7": True,
        "services": "Cat rescue, trap-neuter-return, foster homes",
    },
    {
        "name": "SAMPLE Temporary Foster Shelter - Utap",
        "type": "shelter",
        "address": "Sample foster home network, Utap, Tacloban City",
        "barangay": "Utap",
        "lat": 11.2080,
        "lng": 124.9780,
        "phones": "+63 900 000 0009",
        "hours_text": "Sat-Sun 10AM-4PM",
        "services": "Short-term foster for found pets while owners are searched",
    },
    {
        "name": "SAMPLE Mobile Vet Outreach - Anibong",
        "type": "vet",
        "address": "Sample outreach point near the barangay hall, Anibong",
        "barangay": "Anibong",
        "lat": 11.2555,
        "lng": 125.0045,
        "phones": "+63 900 000 0010",
        "hours_text": "Tue, Thu 9AM-3PM",
        "services": "Free anti-rabies shots, deworming, basic check-ups",
    },
]

# --- Demo accounts, reports and sightings --------------------------------------------------
DEMO_USERS: list[dict[str, str]] = [
    {"key": "maria", "full_name": "Maria Santos (Demo)", "mobile_e164": "+639000000101"},
    {"key": "ana", "full_name": "Ana Reyes (Demo)", "mobile_e164": "+639000000102"},
    {"key": "jose", "full_name": "Jose Cruz (Demo)", "mobile_e164": "+639000000103"},
]

# `art` drives the placeholder illustration; `attrs` is a SPEC 6.3 attribute dict so the
# engine's coarse ranking has something to work with without calling Gemini.
DEMO_REPORTS: list[dict[str, Any]] = [
    {
        "owner": "maria",
        "pet_name": "Bantay",
        "species": "dog",
        "breed": "Aspin (mixed)",
        "colors": "Brown and white",
        "size": "medium",
        "sex": "male",
        "age_text": "About 3 years",
        "identifying_features": "White patch on the chest, red collar, left ear folds down.",
        "place": "Brgy. San Jose, near the chapel",
        "lat": 11.2268,
        "lng": 125.0170,
        "days_ago": 3,
        "reward_amount": 1500,
        "art": {"base": (150, 95, 55), "patch": (245, 240, 230), "bg": (222, 236, 212)},
        "attrs": {
            "primary_colors": ["brown", "white"],
            "coat_pattern": "bicolor",
            "coat_length": "short",
        },
    },
    {
        "owner": "maria",
        "pet_name": "Mingming",
        "species": "cat",
        "breed": "Puspin (mixed)",
        "colors": "Orange tabby",
        "size": "small",
        "sex": "female",
        "age_text": "1 year",
        "identifying_features": "Short tail with a kink at the end, green eyes.",
        "place": "Sagkahan, near the elementary school",
        "lat": 11.2352,
        "lng": 125.0012,
        "days_ago": 5,
        "art": {"base": (230, 140, 50), "patch": (190, 100, 30), "bg": (253, 236, 214)},
        "attrs": {"primary_colors": ["orange"], "coat_pattern": "tabby", "coat_length": "short"},
    },
    {
        "owner": "ana",
        "pet_name": "Choco",
        "species": "dog",
        "breed": "Shih Tzu mix",
        "colors": "Chocolate brown",
        "size": "small",
        "sex": "male",
        "age_text": "5 years",
        "identifying_features": "Blue collar with a bell, underbite.",
        "place": "Marasbaras, along the highway",
        "lat": 11.2162,
        "lng": 124.9918,
        "days_ago": 1,
        "art": {"base": (95, 60, 35), "patch": (95, 60, 35), "bg": (220, 232, 247)},
        "attrs": {"primary_colors": ["brown"], "coat_pattern": "solid", "coat_length": "long"},
    },
    {
        "owner": "ana",
        "pet_name": "Snow",
        "species": "cat",
        "breed": "Persian mix",
        "colors": "White",
        "size": "medium",
        "sex": "female",
        "age_text": "4 years",
        "identifying_features": "One blue eye and one yellow eye.",
        "place": "Downtown, near the public market",
        "lat": 11.2440,
        "lng": 125.0055,
        "days_ago": 9,
        "art": {"base": (246, 246, 242), "patch": (225, 225, 220), "bg": (234, 242, 227)},
        "attrs": {"primary_colors": ["white"], "coat_pattern": "solid", "coat_length": "long"},
    },
    {
        "owner": "maria",
        "pet_name": "Tagpi",
        "species": "dog",
        "breed": "Aspin (mixed)",
        "colors": "Black and white spotted",
        "size": "large",
        "sex": "female",
        "age_text": "2 years",
        "identifying_features": "Black spot over the right eye.",
        "place": "Abucay, near the barangay hall",
        "lat": 11.2570,
        "lng": 124.9962,
        "days_ago": 12,
        "status": "found",  # demonstrates a reunion in the admin statistics
        "art": {"base": (245, 245, 240), "patch": (30, 30, 30), "bg": (253, 227, 227)},
        "attrs": {
            "primary_colors": ["white", "black"],
            "coat_pattern": "spotted",
            "coat_length": "short",
        },
    },
    {
        "owner": "ana",
        "pet_name": "Kitkat",
        "species": "cat",
        "breed": "Puspin (mixed)",
        "colors": "Black",
        "size": "small",
        "sex": "male",
        "age_text": "8 months",
        "identifying_features": "Small white spot on the throat, yellow eyes.",
        "place": "V&G Subdivision, Phase 2",
        "lat": 11.2322,
        "lng": 124.9948,
        "days_ago": 2,
        "show_phone": True,
        "art": {"base": (35, 35, 38), "patch": (250, 250, 250), "bg": (240, 238, 230)},
        "attrs": {"primary_colors": ["black"], "coat_pattern": "solid", "coat_length": "short"},
    },
]

DEMO_SIGHTINGS: list[dict[str, Any]] = [
    {
        "finder": "jose",
        "species": "dog",
        "breed": "Aspin",
        "colors": "Brown with white chest",
        "size": "medium",
        "condition": "healthy",
        "is_holding_animal": True,
        "notes": "Friendly, wearing a faded red collar. Staying in our yard for now.",
        "place": "San Jose, near the airport road",
        "lat": 11.2250,
        "lng": 125.0200,
        "hours_ago": 20,
        "contact_pref": "phone",
        "art": {"base": (150, 95, 55), "patch": (245, 240, 230), "bg": (236, 230, 214)},
        "attrs": {
            "primary_colors": ["brown", "white"],
            "coat_pattern": "bicolor",
            "coat_length": "short",
        },
    },
    {
        "finder": "jose",
        "species": "cat",
        "breed": None,
        "colors": "Orange tabby",
        "size": "small",
        "condition": "injured",
        "is_holding_animal": False,
        "notes": "Limping on the front left leg, hiding under a parked tricycle.",
        "place": "Sagkahan, beside a sari-sari store",
        "lat": 11.2340,
        "lng": 125.0030,
        "hours_ago": 30,
        "contact_pref": "in_app",
        "art": {"base": (230, 140, 50), "patch": (190, 100, 30), "bg": (230, 236, 247)},
        "attrs": {"primary_colors": ["orange"], "coat_pattern": "tabby", "coat_length": "short"},
    },
    {
        "finder": "jose",
        "species": "dog",
        "breed": None,
        "colors": "Black",
        "size": "large",
        "condition": "unknown",
        "is_holding_animal": False,
        "notes": "Seen walking along the seawall in the early morning.",
        "place": "Anibong, seawall area",
        "lat": 11.2548,
        "lng": 125.0060,
        "hours_ago": 50,
        "contact_pref": "in_app",
        "art": {"base": (40, 38, 36), "patch": (40, 38, 36), "bg": (234, 242, 227)},
        "attrs": {"primary_colors": ["black"], "coat_pattern": "solid", "coat_length": "short"},
    },
    {
        "finder": "ana",
        "species": "cat",
        "breed": "Persian-looking",
        "colors": "White, long fur",
        "size": "medium",
        "condition": "healthy",
        "is_holding_animal": True,
        "notes": "Very tame. I am keeping it indoors until the owner is found.",
        "place": "Downtown, Real Street",
        "lat": 11.2450,
        "lng": 125.0035,
        "hours_ago": 8,
        "contact_pref": "in_app",
        "art": {"base": (246, 246, 242), "patch": (225, 225, 220), "bg": (253, 236, 214)},
        "attrs": {"primary_colors": ["white"], "coat_pattern": "solid", "coat_length": "long"},
    },
]


# =============================================================================================
# Placeholder illustrations
# =============================================================================================


def draw_pet(species: str, base: tuple, patch: tuple, bg: tuple, size: tuple = (640, 480)) -> bytes:
    """A simple, recognisable dog or cat illustration (JPEG bytes) with a SAMPLE label."""
    w, h = size
    image = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(image)
    outline = tuple(max(0, c - 70) for c in base)
    dark = (35, 30, 28)

    # ground shadow, tail, body, legs
    d.ellipse((w * 0.22, h * 0.80, w * 0.80, h * 0.90), fill=tuple(max(0, c - 25) for c in bg))
    if species == "cat":
        d.line(
            [(w * 0.72, h * 0.62), (w * 0.84, h * 0.48), (w * 0.86, h * 0.30)],
            fill=base,
            width=int(w * 0.035),
            joint="curve",
        )
    else:
        d.line([(w * 0.72, h * 0.55), (w * 0.84, h * 0.40)], fill=base, width=int(w * 0.04))
    d.ellipse((w * 0.28, h * 0.46, w * 0.78, h * 0.80), fill=base, outline=outline, width=3)
    for x in (0.34, 0.44, 0.60, 0.70):
        d.rounded_rectangle(
            (w * x, h * 0.68, w * (x + 0.06), h * 0.86),
            radius=10,
            fill=base,
            outline=outline,
            width=2,
        )
    if patch != base:  # coat pattern
        if species == "cat":
            for i in range(4):
                x = w * (0.40 + i * 0.08)
                d.line([(x, h * 0.48), (x - w * 0.02, h * 0.62)], fill=patch, width=8)
        else:
            d.ellipse((w * 0.36, h * 0.54, w * 0.52, h * 0.72), fill=patch)
            d.ellipse((w * 0.58, h * 0.50, w * 0.68, h * 0.60), fill=patch)

    # head
    cx, cy, r = w * 0.30, h * 0.40, w * 0.13
    if species == "cat":
        d.polygon(
            [
                (cx - r, cy - r * 0.2),
                (cx - r * 0.75, cy - r * 1.45),
                (cx - r * 0.15, cy - r * 0.85),
            ],
            fill=base,
            outline=outline,
        )
        d.polygon(
            [
                (cx + r, cy - r * 0.2),
                (cx + r * 0.75, cy - r * 1.45),
                (cx + r * 0.15, cy - r * 0.85),
            ],
            fill=base,
            outline=outline,
        )
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=base, outline=outline, width=3)
    if species == "dog":
        d.ellipse((cx - r * 1.25, cy - r * 0.7, cx - r * 0.65, cy + r * 0.6), fill=outline)
        d.ellipse((cx + r * 0.65, cy - r * 0.7, cx + r * 1.25, cy + r * 0.6), fill=outline)
        d.ellipse(
            (cx - r * 0.5, cy + r * 0.05, cx + r * 0.5, cy + r * 0.75),
            fill=patch if patch != base else tuple(min(255, c + 40) for c in base),
        )
    elif patch != base:
        d.line([(cx - r * 0.3, cy - r * 0.9), (cx - r * 0.2, cy - r * 0.5)], fill=patch, width=6)
        d.line([(cx + r * 0.3, cy - r * 0.9), (cx + r * 0.2, cy - r * 0.5)], fill=patch, width=6)
    eye = (90, 170, 80) if species == "cat" else dark
    for ex in (-0.4, 0.4):
        d.ellipse(
            (cx + r * ex - 9, cy - r * 0.25 - 9, cx + r * ex + 9, cy - r * 0.25 + 9), fill=eye
        )
        d.ellipse(
            (cx + r * ex - 3, cy - r * 0.25 - 5, cx + r * ex + 3, cy - r * 0.25 + 5), fill=dark
        )
    d.polygon([(cx - 10, cy + r * 0.2), (cx + 10, cy + r * 0.2), (cx, cy + r * 0.35)], fill=dark)
    if species == "cat":
        for dy in (-6, 6):
            d.line(
                [(cx - r * 0.3, cy + r * 0.3 + dy), (cx - r * 1.2, cy + r * 0.2 + dy * 2)],
                fill=dark,
                width=2,
            )
            d.line(
                [(cx + r * 0.3, cy + r * 0.3 + dy), (cx + r * 1.2, cy + r * 0.2 + dy * 2)],
                fill=dark,
                width=2,
            )

    # label - these are illustrations, not photos of real animals
    d.rounded_rectangle((w - 190, 16, w - 16, 56), radius=18, fill=(255, 244, 194))
    d.text((w - 170, 30), "SAMPLE IMAGE", fill=(107, 82, 0))

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _ai_attributes(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item["attrs"]
    return {
        "species": item["species"],
        "breed_guess": item.get("breed")
        or ("Aspin (mixed)" if item["species"] == "dog" else "Puspin (mixed)"),
        "primary_colors": attrs["primary_colors"],
        "coat_pattern": attrs["coat_pattern"],
        "coat_length": attrs["coat_length"],
        "size_estimate": item.get("size") or "medium",
        "distinctive_marks": [],
        "accessories": [],
    }


def _save_photo(owner_type: str, owner_id: int, item: dict[str, Any]) -> Photo:
    from app.services.storage_service import default_folder, save_image

    art = item["art"]
    photo = save_image(
        draw_pet(item["species"], art["base"], art["patch"], art["bg"]),
        owner_type=owner_type,
        owner_id=owner_id,
        folder=default_folder(owner_type, owner_id),
        is_primary=True,
        sort_order=0,
    )
    photo.ai_attributes = _ai_attributes(item)
    return photo


# =============================================================================================
# Steps
# =============================================================================================


def ensure_admin() -> str:
    """Create the admin from ADMIN_EMAIL/ADMIN_PASSWORD (or promote/update it). Returns a note.

    Without ADMIN_PASSWORD a random password is generated and returned in the note
    (shown once by ``python run.py seed``); an existing admin's password is never changed.
    """
    email = (current_app.config.get("ADMIN_EMAIL") or "").strip().lower()
    if not email:
        return "admin skipped (set ADMIN_EMAIL in .env)"
    password = current_app.config.get("ADMIN_PASSWORD")
    user = db.session.scalar(sa.select(User).where(User.email == email))
    if user is None:
        generated = not password
        password = password or secrets.token_urlsafe(12)
        user = User(
            full_name="PawBalik Administrator",
            email=email,
            role=UserRole.ADMIN,
            privacy_consented_at=utcnow(),
        )
        user.set_password(password)
        db.session.add(user)
        note = f"admin {email} created"
        if generated:
            note += (
                f" with generated password {password} (shown only now - save it, or set "
                "ADMIN_PASSWORD in .env and log in to change it)"
            )
    else:
        user.role = UserRole.ADMIN
        user.is_active = True
        note = f"admin {email} already exists (role ensured)"
    db.session.commit()
    return note


def ensure_sample_providers() -> int:
    """Create missing SAMPLE providers (matched by name; admin edits are kept). Returns count."""
    created = 0
    for item in SAMPLE_PROVIDERS:
        exists = db.session.scalar(
            sa.select(AssistanceProvider.id).where(AssistanceProvider.name == item["name"])
        )
        if exists:
            continue
        db.session.add(
            AssistanceProvider(
                is_24_7=item.get("is_24_7", False),
                is_active=True,
                is_sample=True,
                **{k: v for k, v in item.items() if k != "is_24_7"},
            )
        )
        created += 1
    db.session.commit()
    return created


def demo_email(key: str) -> str:
    return f"{key}@{DEMO_EMAIL_DOMAIN}"


def _demo_users() -> dict[str, User]:
    emails = [demo_email(u["key"]) for u in DEMO_USERS]
    users = db.session.scalars(sa.select(User).where(User.email.in_(emails)))
    return {u.email.split("@")[0]: u for u in users}


def ensure_demo_users(reset_password: bool = False) -> tuple[dict[str, User], str | None]:
    """Create the demo accounts. Returns (users by key, the new password or None)."""
    existing = _demo_users()
    missing = [u for u in DEMO_USERS if u["key"] not in existing]
    password = secrets.token_urlsafe(9) if (missing or reset_password) else None
    for spec in missing:
        user = User(
            full_name=spec["full_name"],
            email=demo_email(spec["key"]),
            role=UserRole.USER,
            privacy_consented_at=utcnow(),
        )
        # Skip the mobile number if a real account already uses it.
        if not db.session.scalar(sa.select(User.id).where(User.mobile_e164 == spec["mobile_e164"])):
            user.mobile_e164 = spec["mobile_e164"]
        user.set_password(password)
        db.session.add(user)
        existing[spec["key"]] = user
    if password:  # one shared password, so the summary is true for every demo account
        for user in existing.values():
            user.set_password(password)
            if reset_password:
                user.is_active = True
    db.session.commit()
    return existing, password


def ensure_demo_reports(users: dict[str, User]) -> int:
    created = 0
    now = utcnow()
    for item in DEMO_REPORTS:
        owner = users[item["owner"]]
        exists = db.session.scalar(
            sa.select(LostReport.id).where(
                LostReport.owner_id == owner.id, LostReport.pet_name == item["pet_name"]
            )
        )
        if exists:
            continue
        report = LostReport(
            case_number=next_case_number(),
            owner_id=owner.id,
            pet_name=item["pet_name"],
            species=item["species"],
            breed=item["breed"],
            colors=item["colors"],
            size=item["size"],
            sex=item["sex"],
            age_text=item.get("age_text"),
            identifying_features=item.get("identifying_features"),
            last_seen_lat=item["lat"],
            last_seen_lng=item["lng"],
            last_seen_place=item["place"],
            last_seen_at=now - timedelta(days=item["days_ago"]),
            contact_name=owner.full_name,
            contact_email=owner.email,
            contact_phone=owner.mobile_e164,
            show_phone=item.get("show_phone", False),
            reward_amount=item.get("reward_amount"),
            status=ReportStatus.LOST,
            ai_attributes=_ai_attributes(item),
        )
        db.session.add(report)
        db.session.flush()
        report.record_created(actor=owner)
        if item.get("status") == ReportStatus.FOUND:
            report.change_status(ReportStatus.FOUND, actor=owner, note="Reunited (demo data)")
        _save_photo(PhotoOwner.LOST_REPORT, report.id, item)
        db.session.commit()
        created += 1
    return created


def ensure_demo_sightings(users: dict[str, User]) -> int:
    created = 0
    now = utcnow()
    for item in DEMO_SIGHTINGS:
        finder = users[item["finder"]]
        exists = db.session.scalar(
            sa.select(Sighting.id).where(
                Sighting.finder_id == finder.id,
                Sighting.seen_place == item["place"],
                Sighting.species == item["species"],
            )
        )
        if exists:
            continue
        sighting = Sighting(
            finder_id=finder.id,
            species=item["species"],
            breed=item["breed"],
            colors=item["colors"],
            size=item["size"],
            condition=item["condition"],
            is_holding_animal=item["is_holding_animal"],
            notes=item["notes"],
            seen_lat=item["lat"],
            seen_lng=item["lng"],
            seen_place=item["place"],
            seen_at=now - timedelta(hours=item["hours_ago"]),
            contact_name=finder.full_name,
            contact_email=finder.email,
            contact_phone=finder.mobile_e164,
            contact_pref=item["contact_pref"],
            ai_attributes=_ai_attributes(item),
        )
        db.session.add(sighting)
        db.session.flush()
        sighting.set_default_expiry(current_app.config.get("SIGHTING_EXPIRY_DAYS", 30))
        _save_photo(PhotoOwner.SIGHTING, sighting.id, item)
        db.session.commit()
        created += 1
    return created


def remove_demo_data() -> dict[str, int]:
    """Delete the demo accounts' reports/sightings (+ dependants) and SAMPLE providers."""
    from app.services.storage_service import delete_photo

    user_ids = [u.id for u in _demo_users().values()]
    report_ids = list(
        db.session.scalars(sa.select(LostReport.id).where(LostReport.owner_id.in_(user_ids)))
    )
    sighting_ids = list(
        db.session.scalars(sa.select(Sighting.id).where(Sighting.finder_id.in_(user_ids)))
    )
    photos = list(
        db.session.scalars(
            sa.select(Photo).where(
                sa.or_(
                    sa.and_(
                        Photo.owner_type == PhotoOwner.LOST_REPORT, Photo.owner_id.in_(report_ids)
                    ),
                    sa.and_(
                        Photo.owner_type == PhotoOwner.SIGHTING, Photo.owner_id.in_(sighting_ids)
                    ),
                )
            )
        )
    )
    photo_ids = [p.id for p in photos]

    # Scans that start from demo records or use their photos, and every result about them.
    scan_ids = list(
        db.session.scalars(
            sa.select(Scan.id).where(
                sa.or_(
                    Scan.source_lost_report_id.in_(report_ids),
                    Scan.source_sighting_id.in_(sighting_ids),
                    Scan.query_photo_id.in_(photo_ids),
                )
            )
        )
    )
    db.session.execute(
        sa.delete(ScanResult).where(
            sa.or_(
                ScanResult.scan_id.in_(scan_ids),
                sa.and_(
                    ScanResult.candidate_type == CandidateType.LOST_REPORT,
                    ScanResult.candidate_id.in_(report_ids),
                ),
                sa.and_(
                    ScanResult.candidate_type == CandidateType.SIGHTING,
                    ScanResult.candidate_id.in_(sighting_ids),
                ),
                ScanResult.candidate_photo_id.in_(photo_ids),
            )
        )
    )
    db.session.execute(
        sa.delete(SightingAccessLog).where(
            sa.or_(
                SightingAccessLog.sighting_id.in_(sighting_ids),
                SightingAccessLog.scan_id.in_(scan_ids),
            )
        )
    )
    scan_photos = list(
        db.session.scalars(
            sa.select(Photo).where(
                Photo.owner_type == PhotoOwner.SCAN, Photo.owner_id.in_(scan_ids)
            )
        )
    )
    db.session.execute(sa.delete(Scan).where(Scan.id.in_(scan_ids)))

    # Conversations about demo reports (with their messages and chat photos).
    conversation_ids = list(
        db.session.scalars(
            sa.select(Conversation.id).where(Conversation.lost_report_id.in_(report_ids))
        )
    )
    db.session.execute(sa.delete(Message).where(Message.conversation_id.in_(conversation_ids)))
    chat_photos = list(
        db.session.scalars(
            sa.select(Photo).where(
                Photo.owner_type == PhotoOwner.MESSAGE, Photo.owner_id.in_(conversation_ids)
            )
        )
    )
    db.session.execute(
        sa.update(AbuseReport)
        .where(AbuseReport.conversation_id.in_(conversation_ids))
        .values(conversation_id=None)
    )
    db.session.execute(sa.delete(Conversation).where(Conversation.id.in_(conversation_ids)))
    db.session.execute(
        sa.update(Conversation)
        .where(Conversation.sighting_id.in_(sighting_ids))
        .values(sighting_id=None)
    )

    db.session.execute(
        sa.delete(RescueRequest).where(
            sa.or_(
                RescueRequest.lost_report_id.in_(report_ids),
                RescueRequest.sighting_id.in_(sighting_ids),
            )
        )
    )
    all_photo_ids = photo_ids + [p.id for p in scan_photos + chat_photos]
    db.session.execute(
        sa.delete(ImageComparison).where(ImageComparison.candidate_photo_id.in_(all_photo_ids))
    )
    db.session.execute(
        sa.delete(CaseStatusHistory).where(CaseStatusHistory.lost_report_id.in_(report_ids))
    )
    db.session.flush()
    db.session.expire_all()
    for photo in photos + scan_photos + chat_photos:
        delete_photo(photo)
    db.session.flush()
    db.session.execute(sa.delete(Sighting).where(Sighting.id.in_(sighting_ids)))
    db.session.execute(sa.delete(LostReport).where(LostReport.id.in_(report_ids)))

    sample_ids = list(
        db.session.scalars(
            sa.select(AssistanceProvider.id).where(AssistanceProvider.is_sample.is_(True))
        )
    )
    db.session.execute(
        sa.update(RescueRequest)
        .where(RescueRequest.provider_id.in_(sample_ids))
        .values(provider_id=None)
    )
    db.session.execute(sa.delete(AssistanceProvider).where(AssistanceProvider.id.in_(sample_ids)))
    db.session.commit()
    return {
        "reports": len(report_ids),
        "sightings": len(sighting_ids),
        "providers": len(sample_ids),
        "photos": len(all_photo_ids),
    }


def run_seed(reset_demo: bool = False) -> dict:
    """Seed the database. Returns a small summary dict for the CLI."""
    summary: dict[str, Any] = {"admin": ensure_admin()}
    if reset_demo:
        summary["removed"] = remove_demo_data()
    summary["sample_providers_created"] = ensure_sample_providers()
    users, password = ensure_demo_users(reset_password=reset_demo)
    if password:
        summary["demo_accounts"] = (
            ", ".join(demo_email(u["key"]) for u in DEMO_USERS)
            + f" - password {password} (shown only now)"
        )
    else:
        summary["demo_accounts"] = "already exist (use --reset-demo for a new password)"
    summary["demo_reports_created"] = ensure_demo_reports(users)
    summary["demo_sightings_created"] = ensure_demo_sightings(users)
    return summary
