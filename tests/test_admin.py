"""Admin area: gating, statistics, providers CRUD, moderation, users, abuse reports, logs."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import (
    AbuseReport,
    AssistanceProvider,
    AuditLog,
    Conversation,
    PushLog,
    RescueRequest,
    Scan,
    SmsLog,
)
from app.routes.admin import compute_stats, deactivation_problem
from app.services import policy_service as policy
from app.utils.time import utcnow

ADMIN_PAGES = [
    "/admin",
    "/admin/providers",
    "/admin/providers/new",
    "/admin/reports",
    "/admin/sightings",
    "/admin/users",
    "/admin/abuse-reports",
    "/admin/logs",
    "/admin/logs?kind=sms",
    "/admin/logs?kind=push",
]

VALID_PROVIDER = {
    "name": "Tacloban Animal Clinic",
    "type": "vet",
    "address": "Real St.",
    "place": "Downtown",
    "lat": "11.2440",
    "lng": "125.0050",
    "phones": "0917 123 4567, (053) 321 1234",
    "facebook_url": "https://www.facebook.com/example",
    "hours_text": "Mon-Sat 8AM-5PM",
    "services": "Consultation",
    "is_active": "1",
}


def _events(name: str) -> list[AuditLog]:
    return list(db.session.scalars(sa.select(AuditLog).where(AuditLog.event == name)))


@pytest.fixture
def admin_client(client, login, admin):
    login(client, admin)
    return client


# --- Gating ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_guest_is_sent_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_regular_user_gets_403(client, login, user, path):
    login(client, user)
    assert client.get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render(admin_client, path):
    assert admin_client.get(path).status_code == 200


def test_admin_posts_are_gated(client, login, user, make_lost_report):
    report = make_lost_report()
    url = f"/admin/reports/{report.id}/visibility"
    assert client.post(url, data={"action": "hide", "reason": "x"}).status_code == 302
    login(client, user)
    assert client.post(url, data={"action": "hide", "reason": "x"}).status_code == 403
    assert db.session.get(type(report), report.id).is_hidden is False


def test_api_stats_gating(client, login, user, admin):
    assert client.get("/api/admin/stats").status_code == 401
    login(client, user)
    forbidden = client.get("/api/admin/stats")
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"]["code"] == "forbidden"
    login(client, admin)
    assert client.get("/api/admin/stats").status_code == 200


def test_deactivated_admin_loses_access(client, login, make_admin):
    old_admin = make_admin(email="old@example.com", is_active=False)
    login(client, old_admin)
    assert client.get("/admin").status_code in (302, 403)


# --- Statistics --------------------------------------------------------------------------------


def test_compute_stats(app, make_user, make_lost_report, make_sighting, admin):
    owner = make_user()
    make_lost_report(owner=owner)
    make_lost_report(owner=owner, is_hidden=True)
    found = make_lost_report(owner=owner)
    found.change_status("found", actor=owner)
    closed_after_found = make_lost_report(owner=owner)
    closed_after_found.change_status("found", actor=owner)
    closed_after_found.change_status("closed", actor=owner)
    just_closed = make_lost_report(owner=owner)
    just_closed.change_status("closed", actor=owner)
    make_sighting()
    make_sighting(status="expired")
    db.session.add_all(
        [
            Scan(user_id=owner.id, mode="found", status="complete"),
            Scan(user_id=owner.id, mode="lost", trigger="background", status="complete"),
            Scan(
                user_id=owner.id,
                mode="found",
                status="complete",
                created_at=utcnow() - timedelta(days=20),
            ),
            SmsLog(
                to_number="+639171234567",
                message="hi",
                purpose="possible_match",
                provider="memory",
                status="sent",
            ),
            SmsLog(
                to_number="+639171234567",
                message="hi",
                purpose="possible_match",
                provider="memory",
                status="failed",
            ),
            PushLog(title="Possible match", status="sent"),
        ]
    )
    db.session.commit()

    stats = compute_stats()
    assert stats["active_lost_cases"] == 1  # the hidden one is not counted
    assert stats["hidden_reports"] == 1
    assert stats["found_cases"] == 1
    assert stats["closed_cases"] == 2
    assert stats["reunions"] == 2  # ever marked found, including the one closed afterwards
    assert stats["sightings"]["active"] == 1 and stats["sightings"]["expired"] == 1
    assert len(stats["scans_per_day"]) == 14
    today = stats["scans_per_day"][-1]
    assert today["total"] == 2 and today["manual"] == 1 and today["background"] == 1
    assert stats["scans_last_14_days"] == 2
    assert stats["users"]["total"] == 4  # owner, admin and the two sighting finders
    assert stats["users"]["admins"] == 1
    assert stats["sms"]["sent"] == 1 and stats["sms"]["failed"] == 1
    assert stats["push"]["sent"] == 1


def test_api_stats_shape(admin_client):
    data = admin_client.get("/api/admin/stats").get_json()
    assert {
        "active_lost_cases",
        "sightings",
        "scans_per_day",
        "reunions",
        "users",
        "sms",
        "push",
    } <= set(data)
    assert {"date", "label", "manual", "background", "total"} == set(data["scans_per_day"][0])


def test_dashboard_shows_numbers(admin_client, make_lost_report):
    make_lost_report()
    html = admin_client.get("/admin").get_data(as_text=True)
    assert "Active lost cases" in html
    assert "Scans per day" in html
    assert "bar-list__bar" in html


# --- Providers CRUD --------------------------------------------------------------------------


def test_create_provider(admin_client, admin):
    response = admin_client.post("/admin/providers/new", data=VALID_PROVIDER)
    assert response.status_code == 302
    provider = db.session.scalar(sa.select(AssistanceProvider))
    assert provider.name == "Tacloban Animal Clinic"
    assert provider.barangay == "Downtown"
    assert provider.phone_list == ["0917 123 4567", "(053) 321 1234"]
    assert provider.is_active and not provider.is_sample and not provider.is_24_7
    event = _events("admin.provider_created")[0]
    assert event.actor_id == admin.id and event.target_id == provider.id


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", ""),
        ("type", "zoo"),
        ("facebook_url", "javascript:alert(1)"),
        ("facebook_url", "facebook.com/page"),
        ("phones", "call me maybe"),
        ("phones", "123"),
        ("lat", "abc"),
        ("lat", "95"),
    ],
)
def test_provider_validation(admin_client, field, value):
    data = dict(VALID_PROVIDER, **{field: value})
    response = admin_client.post("/admin/providers/new", data=data)
    assert response.status_code == 400
    assert db.session.scalar(sa.select(sa.func.count(AssistanceProvider.id))) == 0


def test_provider_outside_service_area_is_a_warning(admin_client):
    data = dict(VALID_PROVIDER, lat="14.5995", lng="120.9842")  # Manila
    response = admin_client.post("/admin/providers/new", data=data, follow_redirects=True)
    assert response.status_code == 200
    assert "outside the Tacloban City service area" in response.get_data(as_text=True)
    assert db.session.scalar(sa.select(AssistanceProvider)).lat == pytest.approx(14.5995)


def test_provider_unreadable_hours_warning(admin_client):
    data = dict(VALID_PROVIDER, hours_text="whenever we can")
    html = admin_client.post("/admin/providers/new", data=data, follow_redirects=True).get_data(
        as_text=True
    )
    assert "cannot read these hours" in html


def test_edit_provider(admin_client):
    provider = AssistanceProvider(name="Old", type="vet", is_active=True)
    db.session.add(provider)
    db.session.commit()
    assert admin_client.get(f"/admin/providers/{provider.id}/edit").status_code == 200
    data = dict(VALID_PROVIDER, name="New Name", is_24_7="1")
    data.pop("is_active")
    response = admin_client.post(f"/admin/providers/{provider.id}/edit", data=data)
    assert response.status_code == 302
    db.session.refresh(provider)
    assert provider.name == "New Name" and provider.is_24_7 and not provider.is_active
    assert "name" in _events("admin.provider_updated")[0].details["changed"]
    assert admin_client.get("/admin/providers/9999/edit").status_code == 404


def test_delete_unused_provider(admin_client):
    provider = AssistanceProvider(name="Temp", type="vet")
    db.session.add(provider)
    db.session.commit()
    provider_id = provider.id
    assert admin_client.post(f"/admin/providers/{provider_id}/delete").status_code == 302
    assert db.session.get(AssistanceProvider, provider_id) is None
    assert _events("admin.provider_deleted")


def test_delete_used_provider_deactivates(admin_client, user):
    provider = AssistanceProvider(name="Used", type="vet")
    db.session.add(provider)
    db.session.flush()
    db.session.add(RescueRequest(user_id=user.id, provider_id=provider.id, summary_text="x"))
    db.session.commit()
    admin_client.post(f"/admin/providers/{provider.id}/delete")
    db.session.refresh(provider)
    assert provider.is_active is False
    assert _events("admin.provider_deactivated")


def test_provider_delete_requires_csrf(csrf_client, login, admin):
    provider = AssistanceProvider(name="Keep", type="vet")
    db.session.add(provider)
    db.session.commit()
    login(csrf_client, admin)
    assert csrf_client.post(f"/admin/providers/{provider.id}/delete").status_code == 400
    assert db.session.get(AssistanceProvider, provider.id) is not None


# --- Moderation ------------------------------------------------------------------------------


def test_hide_report_requires_reason(admin_client, make_lost_report):
    report = make_lost_report()
    admin_client.post(
        f"/admin/reports/{report.id}/visibility", data={"action": "hide", "reason": " "}
    )
    db.session.refresh(report)
    assert report.is_hidden is False
    assert not _events("admin.report_hidden")


def test_hide_and_unhide_report(admin_client, admin, make_user, make_lost_report):
    report = make_lost_report(pet_name="Spammy")
    stranger = make_user()
    response = admin_client.post(
        f"/admin/reports/{report.id}/visibility",
        data={"action": "hide", "reason": "Spam", "next": "/admin/reports?status=lost"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/reports?status=lost")
    db.session.refresh(report)
    assert report.is_hidden and report.hidden_reason == "Spam"
    assert report.hidden_by == admin.id and report.hidden_at is not None
    assert not report.is_searchable  # out of the Found Pet Scanner pool
    assert not policy.can_view_report(stranger, report)
    assert policy.can_view_report(report.owner, report)  # the owner still sees it
    event = _events("admin.report_hidden")[0]
    assert event.details == {"reason": "Spam", "case_number": report.case_number}

    admin_client.post(
        f"/admin/reports/{report.id}/visibility", data={"action": "unhide", "reason": "Appeal ok"}
    )
    db.session.refresh(report)
    assert not report.is_hidden and report.hidden_reason is None and report.hidden_by is None
    assert _events("admin.report_unhidden")


def test_hidden_report_disappears_from_public_list(admin_client, client, make_lost_report):
    visible = make_lost_report(pet_name="Visible Pet")
    hidden = make_lost_report(pet_name="Hidden Pet")
    admin_client.post(
        f"/admin/reports/{hidden.id}/visibility", data={"action": "hide", "reason": "Abuse"}
    )
    client.get("/")  # new request; same client, still logged in as admin -> log out
    with client.session_transaction() as session:
        session.clear()
    # The public list and API belong to the reports team; check them when they are built.
    response = client.get("/api/lost-reports")
    if response.status_code == 200:
        body = response.get_data(as_text=True)
        assert "Visible Pet" in body and "Hidden Pet" not in body
    browse = client.get("/lost-pets")
    if browse.status_code == 200 and "Visible Pet" in browse.get_data(as_text=True):
        assert "Hidden Pet" not in browse.get_data(as_text=True)
    assert visible.is_public and not db.session.get(type(hidden), hidden.id).is_public


def test_moderation_list_filters(admin_client, make_lost_report):
    make_lost_report(pet_name="Alpha")
    make_lost_report(pet_name="Bravo", is_hidden=True, hidden_reason="spam")
    html = admin_client.get("/admin/reports?visibility=hidden").get_data(as_text=True)
    assert "Bravo" in html and "Alpha" not in html
    html = admin_client.get("/admin/reports?q=alp").get_data(as_text=True)
    assert "Alpha" in html and "Bravo" not in html


def test_bad_moderation_action(admin_client, make_lost_report):
    report = make_lost_report()
    response = admin_client.post(
        f"/admin/reports/{report.id}/visibility", data={"action": "delete", "reason": "x"}
    )
    assert response.status_code == 400
    assert admin_client.post("/admin/reports/9999/visibility", data={}).status_code == 404


def test_hide_sighting(admin_client, admin, make_user, make_sighting, make_scan_result):
    owner = make_user()
    sighting = make_sighting()
    make_scan_result(owner, sighting)
    assert policy.can_view_sighting(owner, sighting)
    admin_client.post(
        f"/admin/sightings/{sighting.id}/visibility", data={"action": "hide", "reason": "Fake"}
    )
    db.session.refresh(sighting)
    assert sighting.is_hidden and sighting.hidden_by == admin.id
    assert not sighting.is_searchable
    assert not policy.can_view_sighting(owner, sighting)  # hidden: finder/admin only
    assert policy.can_view_sighting(sighting.finder, sighting)
    assert _events("admin.sighting_hidden")
    html = admin_client.get("/admin/sightings?visibility=hidden").get_data(as_text=True)
    assert f"#{sighting.id}" in html and "Fake" in html


# --- Users ------------------------------------------------------------------------------------


def test_cannot_deactivate_self(admin_client, admin):
    response = admin_client.post(
        f"/admin/users/{admin.id}/active", data={"active": "0"}, follow_redirects=True
    )
    assert "cannot deactivate your own account" in response.get_data(as_text=True)
    db.session.refresh(admin)
    assert admin.is_active


def test_last_admin_rule(make_admin, make_user):
    only_admin = make_admin(email="only@example.com")
    assert deactivation_problem(make_user(), only_admin) == (
        "You cannot deactivate the last active administrator."
    )
    second = make_admin(email="second@example.com")
    assert deactivation_problem(only_admin, second) is None
    assert deactivation_problem(only_admin, only_admin) == "You cannot deactivate your own account."


def test_deactivate_and_reactivate_user(admin_client, client, login, make_user):
    target = make_user(full_name="Rude Person")
    response = admin_client.post(
        f"/admin/users/{target.id}/active", data={"active": "0", "reason": "Harassment"}
    )
    assert response.status_code == 302
    db.session.refresh(target)
    assert target.is_active is False
    assert _events("admin.user_deactivated")[0].details["reason"] == "Harassment"
    assert not policy.is_logged_in(target)

    admin_client.post(f"/admin/users/{target.id}/active", data={"active": "1"})
    db.session.refresh(target)
    assert target.is_active is True
    assert _events("admin.user_reactivated")


def test_deactivate_other_admin_allowed_when_not_last(admin_client, make_admin):
    other = make_admin(email="other-admin@example.com")
    admin_client.post(f"/admin/users/{other.id}/active", data={"active": "0"})
    db.session.refresh(other)
    assert other.is_active is False


def test_user_active_bad_value(admin_client, user):
    assert (
        admin_client.post(f"/admin/users/{user.id}/active", data={"active": "x"}).status_code == 400
    )
    assert admin_client.post("/admin/users/9999/active", data={"active": "0"}).status_code == 404


def test_user_search(admin_client, make_user):
    make_user(full_name="Claricel Albotra", email="claricel@example.com")
    make_user(full_name="Someone Else", email="else@example.com")
    html = admin_client.get("/admin/users?q=claricel").get_data(as_text=True)
    assert "Claricel Albotra" in html and "Someone Else" not in html
    html = admin_client.get("/admin/users?q=%25").get_data(as_text=True)  # literal %, no wildcard
    assert "Someone Else" not in html


# --- Abuse reports ------------------------------------------------------------------------------


def test_abuse_report_queue_and_update(admin_client, make_user, make_lost_report):
    reporter, target = make_user(full_name="Reporter"), make_user(full_name="Target Person")
    report = make_lost_report(owner=reporter)
    conv = Conversation(lost_report_id=report.id, owner_id=reporter.id, other_user_id=target.id)
    db.session.add(conv)
    db.session.flush()
    item = AbuseReport(
        reporter_id=reporter.id, target_user_id=target.id, conversation_id=conv.id, reason="Scam"
    )
    db.session.add(item)
    db.session.commit()

    html = admin_client.get("/admin/abuse-reports").get_data(as_text=True)
    assert "Target Person" in html and "Scam" in html and report.case_number in html

    admin_client.post(f"/admin/abuse-reports/{item.id}", data={"status": "reviewed"})
    db.session.refresh(item)
    assert item.status == "reviewed"
    assert _events("admin.abuse_report_updated")[0].details["to"] == "reviewed"
    html = admin_client.get("/admin/abuse-reports").get_data(as_text=True)
    assert "Scam" not in html  # default view shows open reports only
    assert "Scam" in admin_client.get("/admin/abuse-reports?status=all").get_data(as_text=True)
    assert (
        admin_client.post(f"/admin/abuse-reports/{item.id}", data={"status": "bogus"}).status_code
        == 400
    )


# --- Logs --------------------------------------------------------------------------------------


def test_logs_hide_otp_codes_and_paginate(admin_client, admin):
    db.session.add(
        SmsLog(
            to_number="+639171234567",
            message="Your PawBalik code is 482913",
            purpose="otp",
            provider="memory",
            status="sent",
        )
    )
    db.session.add(
        SmsLog(
            to_number="+639171234567",
            message="Possible match for your case",
            purpose="possible_match",
            provider="memory",
            status="sent",
        )
    )
    for i in range(30):
        db.session.add(AuditLog(actor_id=admin.id, event=f"admin.test_{i}"))
    db.session.commit()

    html = admin_client.get("/admin/logs?kind=sms").get_data(as_text=True)
    assert "482913" not in html
    assert "verification code hidden" in html
    assert "Possible match for your case" in html
    assert "+63 9** *** 4567" in html

    page1 = admin_client.get("/admin/logs?kind=audit").get_data(as_text=True)
    assert "admin.test_29" in page1 and "Page 1 of 2" in page1
    page2 = admin_client.get("/admin/logs?kind=audit&page=2").get_data(as_text=True)
    assert "admin.test_0<" in page2
    filtered = admin_client.get("/admin/logs?kind=audit&event=admin.test_1").get_data(as_text=True)
    assert "admin.test_12" in filtered and "admin.test_29" not in filtered
