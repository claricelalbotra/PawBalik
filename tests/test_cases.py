"""Case details, owner panel, status changes + history, My Cases dashboard, status API."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import get_csrf_token

from app.extensions import db
from app.models import (
    AuditLog,
    Message,
    Notification,
    ScanTrigger,
    SightingAccessLog,
)
from app.services.message_service import get_or_create_conversation

# Notification dispatch jobs stay queued; these tests check the reports code only.
pytestmark = pytest.mark.config(JOBS_EAGER=False)


def change(client, report, status, note=None):
    data = {"status": status}
    if note is not None:
        data["note"] = note
    return client.post(f"/cases/{report.id}/status", data=data)


def link_scan_to_report(result, report):
    """make_scan_result does not set the source report; background/manual lost scans do."""
    result.scan.source_lost_report_id = report.id
    db.session.commit()
    return result


# --- Case details -------------------------------------------------------------------------------


def test_detail_public_view_masks_phone(client, make_lost_report, make_user):
    owner = make_user(mobile_e164="+639171234567", email="private@example.com")
    report = make_lost_report(
        owner=owner,
        photos=2,
        reward_amount=1500,
        reward_text="No questions asked",
        identifying_features="White patch on left ear",
        ai_attributes={"breed_guess": "Aspin", "primary_colors": ["brown", "white"]},
    )
    response = client.get(f"/cases/{report.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert report.case_number in html and report.pet_name in html
    assert "+63 9** *** 4567" in html
    assert "+63 917 123 4567" not in html and "tel:+639171234567" not in html
    assert "private@example.com" not in html
    assert "₱1,500" in html and "No questions asked" in html
    assert "White patch on left ear" in html
    assert "AI notes" in html and "brown, white" in html
    assert "https://www.google.com/maps/search/?api=1&amp;query=" in html
    assert "https://www.google.com/maps/dir/?api=1&amp;destination=" in html
    assert f"/messages/new?lost_report_id={report.id}" in html
    assert f"/rescue/request?lost_report_id={report.id}" in html
    assert "Report filed" in html
    assert "Owner panel" not in html and "Mark as Found" not in html
    assert html.count("/media/") >= 3  # main photo + 2 thumbnails


def test_detail_show_phone_opt_in(client, make_lost_report, make_user):
    owner = make_user(mobile_e164="+639171234567")
    report = make_lost_report(owner=owner, show_phone=True)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "+63 917 123 4567" in html and "tel:+639171234567" in html


def test_detail_owner_panel(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Owner panel" in html
    assert "Mark as Found" in html and "Close Case" in html
    assert f"/scan/lost?lost_report_id={report.id}" in html
    assert f"/lost-pets/{report.id}/edit" in html
    assert f"/messages/new?lost_report_id={report.id}" not in html  # no messaging yourself
    assert report.contact_email in html  # the owner sees their own email


def test_detail_other_user_sees_message_button(client, login, make_lost_report, make_user):
    report = make_lost_report()
    login(client, make_user())
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert f"/messages/new?lost_report_id={report.id}" in html
    assert "Owner panel" not in html


def test_detail_closed_case_has_no_message_button(client, login, make_lost_report, make_user):
    report = make_lost_report(status="closed")
    login(client, make_user())
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "This case is closed" in html
    assert f"/messages/new?lost_report_id={report.id}" not in html


def test_hidden_case_visibility(client, login, make_lost_report, make_user, admin):
    report = make_lost_report(is_hidden=True, hidden_reason="Duplicate report")
    assert client.get(f"/cases/{report.id}").status_code == 404
    login(client, make_user())
    assert client.get(f"/cases/{report.id}").status_code == 404
    login(client, report.owner)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "hidden by a moderator" in html and "Duplicate report" in html
    login(client, admin)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Moderator view" in html


def test_missing_case_404(client):
    assert client.get("/cases/12345").status_code == 404


def test_by_number_redirects(client, make_lost_report):
    report = make_lost_report()
    response = client.get(f"/cases/{report.case_number}")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/cases/{report.id}")
    assert client.get(f"/cases/{report.case_number.lower()}").status_code == 302


@pytest.mark.parametrize("number", ["PB-2026-999999", "PB-26-1", "hello"])
def test_by_number_unknown_or_malformed(client, number):
    assert client.get(f"/cases/{number}").status_code == 404


def test_by_number_hidden_is_404(client, make_lost_report):
    report = make_lost_report(is_hidden=True)
    assert client.get(f"/cases/{report.case_number}").status_code == 404


# --- Owner panel: possible matches and conversations --------------------------------------------


def test_owner_sees_possible_matches_and_access_is_logged(
    client, login, make_lost_report, make_sighting, make_scan_result, make_user
):
    report = make_lost_report()
    owner = report.owner
    finder = make_user(full_name="Claricel Finder")
    sighting = make_sighting(finder=finder, seen_place="Near the Astrodome")
    result = link_scan_to_report(
        make_scan_result(owner, sighting, final_score=84.0, trigger=ScanTrigger.BACKGROUND),
        report,
    )
    unrelated = make_sighting(seen_place="Unrelated place")  # no scan result -> never shown
    login(client, owner)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Possible matches found so far" in html
    assert "84% match" in html
    assert f"/sightings/{sighting.id}" in html
    assert "Near the Astrodome" in html
    assert "Unrelated place" not in html and f"/sightings/{unrelated.id}" not in html
    assert f"/messages/new?lost_report_id={report.id}&amp;sighting_id={sighting.id}" in html
    assert "visual indicators only" in html  # disclaimer
    log = db.session.scalar(sa.select(SightingAccessLog))
    assert log.sighting_id == sighting.id and log.viewer_id == owner.id
    assert log.scan_id == result.scan_id


def test_matches_skip_weak_hidden_and_other_reports(
    client, login, make_lost_report, make_sighting, make_scan_result
):
    report = make_lost_report()
    other_report = make_lost_report(owner=report.owner, pet_name="Other")
    owner = report.owner
    weak = make_sighting(seen_place="Weak place")
    result = make_scan_result(owner, weak, final_score=20.0)
    result.tier = "hidden"
    link_scan_to_report(result, report)
    hidden = make_sighting(seen_place="Hidden place", is_hidden=True)
    link_scan_to_report(make_scan_result(owner, hidden), report)
    elsewhere = make_sighting(seen_place="Other report place")
    link_scan_to_report(make_scan_result(owner, elsewhere), other_report)
    login(client, owner)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Weak place" not in html
    assert "Hidden place" not in html
    assert "Other report place" not in html
    assert "No possible matches yet" in html


def test_matches_are_not_shown_to_the_public(
    client, make_lost_report, make_sighting, make_scan_result
):
    report = make_lost_report()
    sighting = make_sighting(seen_place="Secret sighting place")
    link_scan_to_report(make_scan_result(report.owner, sighting), report)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Secret sighting place" not in html
    assert f"/sightings/{sighting.id}" not in html


def test_owner_sees_conversations(client, login, make_lost_report, make_user, admin):
    report = make_lost_report()
    other = make_user(full_name="Helpful Finder")
    conv = get_or_create_conversation(report, other)
    db.session.commit()
    login(client, report.owner)
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Helpful Finder" in html and f"/messages/{conv.id}" in html
    login(client, admin)  # admins do not read private chats
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert f"/messages/{conv.id}" not in html


# --- Status changes -------------------------------------------------------------------------------


def test_owner_marks_found_then_closed(client, login, make_lost_report, make_user):
    report = make_lost_report()
    owner = report.owner
    finder = make_user(full_name="Finder One")
    conv = get_or_create_conversation(report, finder)
    db.session.commit()
    login(client, owner)

    response = change(client, report, "found", note="Reunited at the barangay hall")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/cases/{report.id}")
    db.session.refresh(report)
    assert report.status == "found"
    history = report.status_history
    assert [(h.from_status, h.to_status) for h in history] == [(None, "lost"), ("lost", "found")]
    assert history[-1].changed_by == owner.id
    assert history[-1].note == "Reunited at the barangay hall"

    system = db.session.scalars(
        sa.select(Message).where(Message.conversation_id == conv.id, Message.type == "system")
    ).all()
    assert len(system) == 1
    assert "marked as Found" in system[0].body and report.case_number in system[0].body
    assert "Reunited at the barangay hall" in system[0].body

    notes = db.session.scalars(sa.select(Notification)).all()
    assert [(n.user_id, n.type) for n in notes] == [(finder.id, "case_status_changed")]
    assert notes[0].link_url == f"/cases/{report.id}"
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "case.status_changed"))

    assert change(client, report, "closed").status_code == 302
    db.session.refresh(report)
    assert report.status == "closed"
    assert [h.to_status for h in report.status_history] == ["lost", "found", "closed"]


def test_lost_can_close_directly(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    change(client, report, "closed")
    db.session.refresh(report)
    assert report.status == "closed"


@pytest.mark.parametrize(
    ("start", "target"),
    [("closed", "found"), ("closed", "lost"), ("found", "lost"), ("lost", "lost")],
)
def test_invalid_transitions_are_refused(client, login, make_lost_report, start, target):
    report = make_lost_report(status=start)
    login(client, report.owner)
    response = change(client, report, target)
    assert response.status_code == 302
    db.session.refresh(report)
    assert report.status == start
    assert len(report.status_history) == 1
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "cannot be" in html


def test_status_change_bad_input(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    change(client, report, "teleported")
    change(client, report, "found", note="x" * 256)
    db.session.refresh(report)
    assert report.status == "lost"


def test_only_owner_or_admin_can_change_status(client, login, make_lost_report, make_user, admin):
    report = make_lost_report()
    response = change(client, report, "found")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    login(client, make_user())
    assert change(client, report, "found").status_code == 403
    db.session.refresh(report)
    assert report.status == "lost"
    login(client, admin)
    assert change(client, report, "closed").status_code == 302
    db.session.refresh(report)
    assert report.status == "closed"
    assert report.status_history[-1].changed_by == admin.id


def test_admin_change_notifies_owner_too(client, login, make_lost_report, make_user, admin):
    report = make_lost_report()
    finder = make_user()
    get_or_create_conversation(report, finder)
    db.session.commit()
    login(client, admin)
    change(client, report, "closed")
    recipients = set(db.session.scalars(sa.select(Notification.user_id)))
    assert recipients == {finder.id, report.owner_id}


def test_status_form_requires_csrf(csrf_client, login, make_lost_report):
    report = make_lost_report()
    login(csrf_client, report.owner)
    assert change(csrf_client, report, "found").status_code == 400
    token = get_csrf_token(csrf_client, f"/cases/{report.id}")
    response = csrf_client.post(
        f"/cases/{report.id}/status", data={"status": "found", "csrf_token": token}
    )
    assert response.status_code == 302
    db.session.refresh(report)
    assert report.status == "found"


def test_timeline_notes_are_private(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    change(client, report, "found", note="Picked up from Brgy. 88 hall")
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Marked as Found" in html and "Picked up from Brgy. 88 hall" in html
    client.get("/")  # new request cycle
    with client.session_transaction() as session:
        session.clear()
    html = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert "Marked as Found" in html and "Picked up from Brgy. 88 hall" not in html


# --- Status API --------------------------------------------------------------------------------------


def patch(client, report, payload, **kwargs):
    return client.patch(f"/api/lost-reports/{report.id}/status", json=payload, **kwargs)


def test_api_update_status(client, login, make_lost_report, make_user):
    report = make_lost_report()
    finder = make_user()
    get_or_create_conversation(report, finder)
    db.session.commit()
    login(client, report.owner)
    response = patch(client, report, {"status": "found", "note": "Home safe"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["id"] == report.id and body["status"] == "found"
    assert body["case_number"] == report.case_number
    assert [h["to_status"] for h in body["history"]] == ["lost", "found"]
    assert body["history"][-1]["note"] == "Home safe"
    assert body["history"][-1]["changed_at"].endswith("Z")
    assert db.session.scalar(sa.select(sa.func.count(Notification.id))) == 1


def test_api_update_status_errors(client, login, make_lost_report, make_user):
    report = make_lost_report(status="closed")
    response = patch(client, report, {"status": "found"})
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "auth_required"

    login(client, make_user())
    response = patch(client, report, {"status": "found"})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "forbidden"

    login(client, report.owner)
    response = patch(client, report, {"status": "found"})
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "validation_error" and error["details"]["allowed"] == []

    response = patch(client, report, {"status": "gone"})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"

    response = client.patch(
        f"/api/lost-reports/{report.id}/status", data="nope", content_type="text/plain"
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_request"

    response = patch(client, report, {"status": "closed", "note": "x" * 300})
    assert response.status_code == 400

    response = client.patch("/api/lost-reports/9999/status", json={"status": "found"})
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_api_update_status_hidden_report_is_404_for_others(
    client, login, make_lost_report, make_user
):
    report = make_lost_report(is_hidden=True)
    login(client, make_user())
    assert patch(client, report, {"status": "found"}).status_code == 404


def test_api_update_status_requires_csrf(csrf_client, login, make_lost_report):
    report = make_lost_report()
    login(csrf_client, report.owner)
    response = patch(csrf_client, report, {"status": "found"})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "csrf_failed"
    token = get_csrf_token(csrf_client)
    response = patch(csrf_client, report, {"status": "found"}, headers={"X-CSRFToken": token})
    assert response.status_code == 200


def test_api_history(client, login, make_lost_report):
    report = make_lost_report()
    login(client, report.owner)
    patch(client, report, {"status": "found", "note": "Private note"})
    body = client.get(f"/api/lost-reports/{report.id}/history").get_json()
    assert [h["to_status"] for h in body["history"]] == ["lost", "found"]
    assert body["history"][1]["note"] == "Private note"
    with client.session_transaction() as session:
        session.clear()
    body = client.get(f"/api/lost-reports/{report.id}/history").get_json()
    assert body["history"][1]["note"] is None
    assert set(body["history"][0]) == {"from_status", "to_status", "changed_at", "note"}


def test_api_history_hidden(client, make_lost_report):
    report = make_lost_report(is_hidden=True)
    assert client.get(f"/api/lost-reports/{report.id}/history").status_code == 404


# --- My Cases dashboard ----------------------------------------------------------------------------


def test_dashboard_requires_login(client):
    response = client.get("/my-cases")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_dashboard_empty_states(client, login, user):
    login(client, user)
    html = client.get("/my-cases").get_data(as_text=True)
    assert "No lost pet reports yet" in html and "/lost-pets/new" in html
    assert "No sightings yet" in html and "/scan/found" in html


def test_dashboard_lists_only_my_cases(
    client, login, make_user, make_lost_report, make_sighting, make_scan_result
):
    me = make_user()
    mine = make_lost_report(owner=me, pet_name="MyDog")
    make_lost_report(owner=me, pet_name="MyOldCat", species="cat", status="closed")
    make_lost_report(owner=me, pet_name="MyHidden", is_hidden=True)
    make_lost_report(pet_name="SomeoneElse")
    my_sighting = make_sighting(finder=me, seen_place="My sighting spot")
    make_sighting(seen_place="Other finder spot")
    match = make_sighting(seen_place="Match spot")
    link_scan_to_report(make_scan_result(me, match), mine)

    login(client, me)
    html = client.get("/my-cases").get_data(as_text=True)
    assert "MyDog" in html and "MyOldCat" in html and "MyHidden" in html
    assert "SomeoneElse" not in html
    assert "Hidden by moderator" in html
    assert "My sighting spot" in html and f"/sightings/{my_sighting.id}" in html
    assert "Other finder spot" not in html
    assert "1 possible match" in html
    assert f"/cases/{mine.id}" in html
    assert "Closed" in html
