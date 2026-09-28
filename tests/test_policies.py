"""Authorization rules in app/services/policy_service.py (SPEC 5.4, 10, 17.6)."""

from __future__ import annotations

from flask_login import AnonymousUserMixin

from app.extensions import db
from app.models import Conversation, PhotoOwner, ReportStatus
from app.services import policy_service as policy

GUEST = AnonymousUserMixin()


def test_is_admin(make_user, make_admin):
    assert policy.is_admin(make_admin())
    assert not policy.is_admin(make_user())
    assert not policy.is_admin(GUEST)
    assert not policy.is_admin(make_admin(is_active=False))


def test_report_management(make_user, make_admin, make_lost_report):
    owner, other, admin = make_user(), make_user(), make_admin()
    report = make_lost_report(owner)
    assert policy.can_manage_report(owner, report)
    assert policy.can_manage_report(admin, report)
    assert not policy.can_manage_report(other, report)
    assert not policy.can_manage_report(GUEST, report)


def test_hidden_report_visible_to_owner_and_admin_only(make_user, make_admin, make_lost_report):
    owner, other, admin = make_user(), make_user(), make_admin()
    report = make_lost_report(owner, is_hidden=True, hidden_reason="spam")
    assert policy.can_view_report(owner, report)
    assert policy.can_view_report(admin, report)
    assert not policy.can_view_report(other, report)
    assert not policy.can_view_report(GUEST, report)


def test_report_phone_masking_rules(make_user, make_lost_report):
    owner, other = make_user(), make_user()
    report = make_lost_report(owner, show_phone=False)
    assert not policy.can_view_report_phone(other, report)
    assert not policy.can_view_report_phone(GUEST, report)
    assert policy.can_view_report_phone(owner, report)
    report.show_phone = True
    assert policy.can_view_report_phone(GUEST, report)


def test_can_message_owner(make_user, make_lost_report):
    owner, finder = make_user(), make_user()
    report = make_lost_report(owner)
    assert policy.can_message_owner(finder, report)
    assert not policy.can_message_owner(owner, report)
    assert not policy.can_message_owner(GUEST, report)
    report.status = ReportStatus.CLOSED
    assert not policy.can_message_owner(finder, report)


def test_sighting_is_private_by_default(make_user, make_admin, make_sighting):
    finder, stranger, admin = make_user(), make_user(), make_admin()
    sighting = make_sighting(finder)
    assert policy.can_view_sighting(finder, sighting)
    assert policy.can_view_sighting(admin, sighting)
    assert not policy.can_view_sighting(stranger, sighting)
    assert not policy.can_view_sighting(GUEST, sighting)


def test_sighting_visible_to_owner_whose_scan_returned_it(
    make_user, make_sighting, make_scan_result
):
    owner, other_owner = make_user(), make_user()
    sighting = make_sighting()
    make_scan_result(owner, sighting)
    assert policy.can_view_sighting(owner, sighting)
    assert not policy.can_view_sighting(other_owner, sighting)


def test_background_match_grants_access_to_the_notified_owner(
    make_user, make_sighting, make_scan_result
):
    owner = make_user()
    sighting = make_sighting()
    make_scan_result(owner, sighting, trigger="background")
    assert policy.can_view_sighting(owner, sighting)


def test_hidden_sighting_not_visible_via_scan(make_user, make_sighting, make_scan_result):
    owner = make_user()
    sighting = make_sighting(is_hidden=True, hidden_reason="abuse")
    make_scan_result(owner, sighting)
    assert not policy.can_view_sighting(owner, sighting)
    assert policy.can_view_sighting(sighting.finder, sighting)


def test_sighting_shared_by_finder_in_conversation(make_user, make_lost_report, make_sighting):
    owner, finder, stranger = make_user(), make_user(), make_user()
    report = make_lost_report(owner)
    sighting = make_sighting(finder)
    conv = Conversation(
        lost_report_id=report.id,
        sighting_id=sighting.id,
        owner_id=owner.id,
        other_user_id=finder.id,
    )
    db.session.add(conv)
    db.session.commit()
    assert policy.can_view_sighting(owner, sighting)
    assert not policy.can_view_sighting(stranger, sighting)


def test_conversation_participants_only(make_user, make_admin, make_lost_report):
    owner, finder, stranger, admin = make_user(), make_user(), make_user(), make_admin()
    report = make_lost_report(owner)
    conv = Conversation(lost_report_id=report.id, owner_id=owner.id, other_user_id=finder.id)
    db.session.add(conv)
    db.session.commit()
    assert policy.can_view_conversation(owner, conv)
    assert policy.can_view_conversation(finder, conv)
    assert not policy.can_view_conversation(stranger, conv)
    assert not policy.can_view_conversation(admin, conv)  # admins do not read private chats
    assert policy.can_post_in_conversation(finder, conv)
    finder.is_active = False
    assert not policy.can_post_in_conversation(owner, conv)


def test_scan_visibility(make_user, make_admin, make_sighting, make_scan_result):
    owner, other, admin = make_user(), make_user(), make_admin()
    result = make_scan_result(owner, make_sighting())
    assert policy.can_view_scan(owner, result.scan)
    assert policy.can_view_scan(admin, result.scan)
    assert not policy.can_view_scan(other, result.scan)


def test_photo_visibility_follows_owner(make_user, make_lost_report, make_sighting, make_photo):
    finder, stranger = make_user(), make_user()
    report = make_lost_report()
    sighting = make_sighting(finder)
    assert policy.can_view_photo(GUEST, report.primary_photo)
    assert policy.can_view_photo(finder, sighting.primary_photo)
    assert not policy.can_view_photo(stranger, sighting.primary_photo)
    orphan = make_photo(PhotoOwner.SCAN, None)
    assert not policy.can_view_photo(stranger, orphan)


def test_can_request_rescue(make_user, make_sighting):
    finder, stranger = make_user(), make_user()
    sighting = make_sighting(finder)
    assert policy.can_request_rescue(finder, sighting=sighting)
    assert not policy.can_request_rescue(stranger, sighting=sighting)
    assert not policy.can_request_rescue(GUEST)
