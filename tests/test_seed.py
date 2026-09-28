"""database/seed.py: admin, SAMPLE providers, demo data, idempotency and --reset-demo."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from PIL import Image

from app.extensions import db
from app.models import (
    AssistanceProvider,
    Conversation,
    LostReport,
    Message,
    Photo,
    RescueRequest,
    Sighting,
    User,
)
from app.utils.geo import in_service_area
from database.seed import (
    DEMO_EMAIL_DOMAIN,
    DEMO_REPORTS,
    DEMO_SIGHTINGS,
    SAMPLE_PROVIDERS,
    draw_pet,
    run_seed,
)


def _count(model) -> int:
    return db.session.scalar(sa.select(sa.func.count(model.id))) or 0


def _demo_users() -> list[User]:
    return list(
        db.session.scalars(sa.select(User).where(User.email.like(f"%@{DEMO_EMAIL_DOMAIN}")))
    )


@pytest.fixture
def seeded(app):
    app.config.update(ADMIN_EMAIL="boss@example.com", ADMIN_PASSWORD="Admin2026pass")
    return run_seed()


def test_seed_creates_everything(app, seeded):
    assert "created" in seeded["admin"]
    assert seeded["sample_providers_created"] == len(SAMPLE_PROVIDERS)
    assert seeded["demo_reports_created"] == len(DEMO_REPORTS)
    assert seeded["demo_sightings_created"] == len(DEMO_SIGHTINGS)
    assert "password" in seeded["demo_accounts"]

    providers = list(db.session.scalars(sa.select(AssistanceProvider)))
    assert 8 <= len(providers) <= 10
    for p in providers:
        assert p.is_sample and p.is_active
        assert p.name.startswith("SAMPLE ")
        assert in_service_area(p.lat, p.lng), p.name
        assert p.facebook_url is None  # never link to real pages
        assert all(phone.startswith("+63 900 000 00") for phone in p.phone_list)
    assert {p.type for p in providers} == {"rescuer", "vet", "shelter"}

    assert len(_demo_users()) == 3
    reports = list(db.session.scalars(sa.select(LostReport)))
    assert {r.status for r in reports} == {"lost", "found"}
    for report in reports:
        assert report.case_number.startswith("PB-")
        assert in_service_area(report.last_seen_lat, report.last_seen_lng)
        assert report.primary_photo is not None
        assert report.status_history  # "Report filed" row
        assert report.ai_attributes["species"] == report.species
    for sighting in db.session.scalars(sa.select(Sighting)):
        assert sighting.status == "active" and sighting.expires_at is not None
        assert sighting.primary_photo is not None


def test_seed_photos_are_real_images_on_disk(app, seeded):
    photo = db.session.scalar(sa.select(Photo))
    path = Path(app.config["UPLOAD_FOLDER"]) / photo.public_id
    assert path.is_file()
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.width >= 400


def test_draw_pet_makes_distinct_images():
    dog = draw_pet("dog", (150, 95, 55), (245, 240, 230), (222, 236, 212))
    cat = draw_pet("cat", (230, 140, 50), (190, 100, 30), (253, 236, 214))
    assert dog != cat
    assert Image.open(io.BytesIO(dog)).size == (640, 480)


def test_seed_is_idempotent(app, seeded):
    counts = (
        _count(AssistanceProvider),
        _count(User),
        _count(LostReport),
        _count(Sighting),
        _count(Photo),
    )
    again = run_seed()
    assert "already exists" in again["admin"]
    assert again["sample_providers_created"] == 0
    assert again["demo_reports_created"] == 0
    assert again["demo_sightings_created"] == 0
    assert "already exist" in again["demo_accounts"]
    assert counts == (
        _count(AssistanceProvider),
        _count(User),
        _count(LostReport),
        _count(Sighting),
        _count(Photo),
    )


def test_seed_keeps_admin_edits_to_sample_providers(app, seeded):
    provider = db.session.scalar(sa.select(AssistanceProvider))
    provider.services = "Edited by admin"
    db.session.commit()
    run_seed()
    db.session.refresh(provider)
    assert provider.services == "Edited by admin"


def test_seed_generates_admin_password_when_missing(app):
    app.config.update(ADMIN_EMAIL="gen@example.com", ADMIN_PASSWORD=None)
    note = run_seed()["admin"]
    assert "created with generated password" in note
    password = note.split("generated password ")[1].split(" ")[0]
    admin = db.session.scalar(sa.select(User).where(User.email == "gen@example.com"))
    assert admin.is_admin and admin.check_password(password)
    assert "already exists" in run_seed()["admin"]  # never shown or changed again


def test_demo_accounts_can_log_in(app, client, seeded):
    password = seeded["demo_accounts"].split("password ")[1].split(" ")[0]
    response = client.post(
        "/login", data={"email": f"maria@{DEMO_EMAIL_DOMAIN}", "password": password}
    )
    assert response.status_code == 302


def test_reset_demo_only_touches_demo_data(app, seeded, make_user, make_lost_report):
    # Real data that must survive.
    real_user = make_user(full_name="Real Person")
    real_report = make_lost_report(owner=real_user, pet_name="RealDog")
    real_provider = AssistanceProvider(name="Real Clinic", type="vet", is_sample=False)
    db.session.add(real_provider)
    sample = db.session.scalar(
        sa.select(AssistanceProvider).where(AssistanceProvider.is_sample.is_(True))
    )
    kept_request = RescueRequest(
        user_id=real_user.id, lost_report_id=real_report.id, provider_id=sample.id, summary_text="x"
    )
    db.session.add(kept_request)
    # A real user's conversation about a demo report (removed with the demo report).
    demo_report = db.session.scalar(
        sa.select(LostReport).where(LostReport.pet_name == DEMO_REPORTS[0]["pet_name"])
    )
    conv = Conversation(
        lost_report_id=demo_report.id, owner_id=demo_report.owner_id, other_user_id=real_user.id
    )
    db.session.add(conv)
    db.session.flush()
    db.session.add(
        Message(conversation_id=conv.id, sender_id=real_user.id, body="Is this your dog?")
    )
    db.session.commit()
    old_photo_paths = [
        Path(app.config["UPLOAD_FOLDER"]) / p.public_id
        for p in db.session.scalars(
            sa.select(Photo).where(
                Photo.owner_id == demo_report.id, Photo.owner_type == "lost_report"
            )
        )
    ]
    old_ids = set(
        db.session.scalars(sa.select(LostReport.id).where(LostReport.id != real_report.id))
    )

    summary = run_seed(reset_demo=True)

    assert summary["removed"]["reports"] == len(DEMO_REPORTS)
    assert summary["removed"]["sightings"] == len(DEMO_SIGHTINGS)
    assert summary["removed"]["providers"] == len(SAMPLE_PROVIDERS)
    assert summary["demo_reports_created"] == len(DEMO_REPORTS)
    assert "password" in summary["demo_accounts"]
    # demo data recreated with new ids, real data untouched
    new_ids = set(
        db.session.scalars(sa.select(LostReport.id).where(LostReport.id != real_report.id))
    )
    assert new_ids and not (new_ids & old_ids)
    assert db.session.get(LostReport, real_report.id).pet_name == "RealDog"
    assert db.session.scalar(
        sa.select(AssistanceProvider).where(AssistanceProvider.name == "Real Clinic")
    )
    kept = db.session.get(RescueRequest, kept_request.id)
    assert kept is not None and kept.provider_id is None
    assert _count(Conversation) == 0 and _count(Message) == 0
    assert all(not path.exists() for path in old_photo_paths)
    assert _count(AssistanceProvider) == len(SAMPLE_PROVIDERS) + 1
    assert len(_demo_users()) == 3
