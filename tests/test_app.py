"""App-wide checks: blueprints, every GET page renders, context, models, stubs, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from app import ROUTE_MODULES
from app.extensions import db
from app.models import (
    CaseStatusHistory,
    InvalidStatusTransition,
    LostReport,
    Message,
    Notification,
    allowed_transitions,
    can_transition,
)

ROUTES_DIR = Path(__file__).resolve().parent.parent / "app" / "routes"


def _parameterless_get_rules(app):
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or rule.endpoint == "static":
            continue
        yield rule


def test_every_route_file_is_registered(app):
    files = {p.stem for p in ROUTES_DIR.glob("*.py") if p.stem != "__init__"}
    assert files == set(ROUTE_MODULES)
    assert set(ROUTE_MODULES) <= set(app.blueprints)


@pytest.mark.parametrize("role", ["guest", "user", "admin"])
def test_every_get_page_renders(app, client, make_user, make_admin, login, role):
    if role == "user":
        login(client, make_user())
    elif role == "admin":
        login(client, make_admin())
    checked = 0
    for rule in _parameterless_get_rules(app):
        path = rule.rule
        response = client.get(path)
        status = response.status_code
        if path.startswith("/api/"):
            assert response.is_json, path
            assert status in (200, 401, 403, 501), (role, path, status)
            continue
        if path == "/firebase-messaging-sw.js":
            assert status in (200, 404, 501), path
            continue
        allowed = {200, 302}
        if role == "user" and path.startswith("/admin"):
            allowed = {403}
        assert status in allowed, (role, path, status)
        checked += 1
    assert checked >= 15


def test_landing_page_content(client):
    html = client.get("/").get_data(as_text=True)
    assert "Bringing Lost Paws Back" in html
    assert 'class="hero__accent">Home.</span>' in html
    for text in (
        "Report Lost Pet",
        "Match a Found Pet",
        "Find Rescue Help",
        "For dogs and cats only",
        "Scan Matching",
        "Nearby Help",
        "Updates",
        "Community Driven",
        "Tacloban City&#39;s Lost &amp; Found Pet Platform",
    ):
        assert text in html, text
    assert 'href="/rescue"' in html  # third card links to rescue (SPEC 16.3)


def test_header_for_guest_and_user(client, make_user, login):
    html = client.get("/").get_data(as_text=True)
    assert "Log In" in html and "Sign Up" in html
    user = make_user(full_name="Claricel Albotra")
    login(client, user)
    html = client.get("/").get_data(as_text=True)
    assert "My Cases" in html and "Log out" in html and "Claricel" in html
    assert "Admin</a>" not in html


def test_admin_link_for_admin(client, make_admin, login):
    login(client, make_admin())
    assert "Admin</a>" in client.get("/").get_data(as_text=True)


def test_active_nav_item(client):
    html = client.get("/about").get_data(as_text=True)
    assert 'class="site-nav__link is-active" href="/about"' in html


def test_unread_counts_badges(client, make_user, make_lost_report, login):
    owner, finder = make_user(), make_user()
    report = make_lost_report(owner)
    from app.services.message_service import get_or_create_conversation
    from app.services.notification_service import notify

    conv = get_or_create_conversation(report, finder)
    db.session.add(Message(conversation_id=conv.id, sender_id=finder.id, type="text", body="Hi"))
    notify(owner, "new_message", {"conversation_id": conv.id})
    db.session.commit()
    login(client, owner)
    html = client.get("/").get_data(as_text=True)
    assert "Messages (1 unread)" in html
    assert "Notifications (1 unread)" in html


def test_public_config_is_rendered_as_json(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="pawbalik-config"' in html
    assert '"bounds"' in html and '"mapPicker"' in html
    assert "GEMINI" not in html and "SECRET" not in html


def test_about_and_privacy(client):
    about = client.get("/about").get_data(as_text=True)
    assert "Reynald Jr. A. Cabujoc" in about
    assert "Similarity percentages are visual indicators only." in about
    assert "RA 10173" in client.get("/privacy").get_data(as_text=True)


def test_case_status_transitions_are_pure():
    assert can_transition("lost", "found")
    assert can_transition("lost", "closed")
    assert can_transition("found", "closed")
    assert not can_transition("found", "lost")
    assert not can_transition("closed", "lost")
    assert not can_transition("lost", "lost")
    assert allowed_transitions("closed") == ()


def test_report_helpers(app, make_lost_report, make_user):
    owner = make_user()
    report = make_lost_report(owner, photos=3)
    assert report.case_number.startswith("PB-")
    assert len(report.photos) == 3
    assert report.primary_photo.is_primary
    assert report.is_searchable
    report.change_status("found", actor=owner, note="Reunited!")
    db.session.commit()
    history = db.session.scalars(
        sa.select(CaseStatusHistory).where(CaseStatusHistory.lost_report_id == report.id)
    ).all()
    assert [(h.from_status, h.to_status) for h in history] == [(None, "lost"), ("lost", "found")]
    with pytest.raises(InvalidStatusTransition):
        report.change_status("lost", actor=owner)
    assert not report.is_searchable


def test_sighting_helpers(app, make_sighting):
    sighting = make_sighting(photos=2)
    assert len(sighting.photos) == 2
    assert sighting.primary_photo is not None
    assert sighting.expires_at > sighting.created_at
    assert sighting.is_searchable


def test_message_service_minimal_contract(app, make_user, make_lost_report):
    from app.services.message_service import (
        ConversationError,
        conversations_for_report,
        get_or_create_conversation,
        post_system_message,
    )

    owner, finder = make_user(), make_user()
    report = make_lost_report(owner)
    conv = get_or_create_conversation(report, finder)
    assert get_or_create_conversation(report, finder).id == conv.id
    with pytest.raises(ConversationError):
        get_or_create_conversation(report, owner)
    msg = post_system_message(conv, "Case marked as Found")
    assert msg.type == "system" and msg.sender_id is None
    assert conversations_for_report(report) == [conv]


def test_notify_creates_in_app_record(app, user):
    from app.services.notification_service import notify, unread_count

    n = notify(user, "possible_match", {"scan_id": 1, "lost_report_id": 1, "link_url": "/scans/1"})
    db.session.commit()
    assert n.title and n.link_url == "/scans/1"
    assert unread_count(user) == 1
    assert db.session.scalar(sa.select(sa.func.count(Notification.id))) == 1
    with pytest.raises(ValueError):
        notify(user, "unknown_event", {})


def test_storage_service_local_roundtrip(app, jpeg):
    from app.services import storage_service
    from app.services.storage_service import ImageValidationError, default_folder

    with app.test_request_context():
        photo = storage_service.save_image(
            jpeg(size=(2000, 1500)), owner_type="scan", folder=default_folder("scan")
        )
        assert photo.id and photo.storage == "local" and len(photo.sha256) == 64
        assert storage_service.local_path(photo.public_id).is_file()
        assert storage_service.photo_url(photo).startswith("/media/scans/")
        ai = storage_service.image_bytes_for_ai(photo)
        from io import BytesIO

        from PIL import Image

        assert max(Image.open(BytesIO(ai)).size) <= 1024
        with pytest.raises(ImageValidationError):
            storage_service.save_image(b"not an image", owner_type="scan", folder="scans/x")
        storage_service.delete_photo(photo)


def test_gemini_stub_and_fake(app, fake_gemini):
    from app.services.gemini_service import GeminiService

    service = GeminiService()
    assert service.is_configured()
    assert service.precheck(b"x")["species"] == "dog"
    fake_gemini.compare_score = 42
    assert service.compare(b"a", b"b")["visual_similarity"] == 42
    assert fake_gemini.calls[0][0] == "precheck"


def test_sms_memory_provider(app, sms_outbox):
    from app.services.sms_service import get_provider

    result = get_provider().send("+639171234567", "Hello")
    assert result.ok
    assert sms_outbox == [{"to": "+639171234567", "message": "Hello"}]


def test_seed_creates_admin(app):
    from database.seed import run_seed

    app.config.update(ADMIN_EMAIL="boss@example.com", ADMIN_PASSWORD="Admin2026pass")
    summary = run_seed()
    assert "created" in summary["admin"]
    from app.models import User

    admin = db.session.scalar(sa.select(User).where(User.email == "boss@example.com"))
    assert admin.is_admin and admin.check_password("Admin2026pass")
    assert "already exists" in run_seed()["admin"]


def test_cli_commands_registered(app):
    commands = set(app.cli.list_commands(None))
    assert {"init-db", "export-schema", "seed", "worker", "match-test", "eval-matching"} <= commands


def test_run_py_parser():
    import run

    parser = run.build_parser()
    assert parser.parse_args(["seed", "--reset-demo"]).reset_demo is True
    assert parser.parse_args(["worker", "--once"]).once is True
    args = parser.parse_args(["match-test", "dog.jpg", "--mode", "lost"])
    assert (args.photo, args.mode) == ("dog.jpg", "lost")
    assert parser.parse_args(["--port", "5101"]).port == 5101


def test_lost_report_model_roundtrip(app, make_lost_report):
    report = make_lost_report(reward_amount=1500, reward_text="Salamat!")
    loaded = db.session.get(LostReport, report.id)
    assert float(loaded.reward_amount) == 1500.0
    assert loaded.owner.lost_reports == [loaded]
