"""notification_service.notify/dispatch rules and the notification pages + JSON API."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from freezegun import freeze_time

from app.extensions import db
from app.models import DeviceToken, Job, NotificationEvent, PushLog, SmsLog
from app.services import notification_service as ns
from app.utils.time import utcnow

MATCH = NotificationEvent.POSSIBLE_MATCH
MESSAGE = NotificationEvent.NEW_MESSAGE
STATUS = NotificationEvent.CASE_STATUS_CHANGED


@pytest.fixture
def owner(make_user):
    """A user who can receive everything: verified mobile, opted in, one device."""
    user = make_user(mobile_verified_at=utcnow(), sms_opt_in=True, push_opt_in=True)
    db.session.add(DeviceToken(user_id=user.id, fcm_token="tok-owner-device-1234567890"))
    db.session.commit()
    return user


def _all(model):
    return list(db.session.scalars(sa.select(model).order_by(model.id)))


# --- notify(): in-app record + job ----------------------------------------------------------------


def test_notify_possible_match_content(app, owner, push_outbox, sms_outbox):
    n = ns.notify(
        owner,
        MATCH,
        {"scan_id": 12, "lost_report_id": 3, "similarity": 84.4, "case_number": "PB-2026-000123"},
    )
    db.session.commit()
    assert n.user_id == owner.id and n.type == MATCH and n.is_read is False
    assert n.title == "New possible match for your lost pet"
    assert "possible match (84% visual similarity)" in n.body
    assert "PB-2026-000123" in n.body
    assert "does not confirm" in n.body
    assert n.link_url == "/scans/12"


def test_possible_match_link_falls_back_to_case(app, owner):
    n = ns.notify(owner, MATCH, {"lost_report_id": 7})
    assert n.link_url == "/cases/7"


def test_notify_new_message_content(app, owner):
    n = ns.notify(
        owner,
        MESSAGE,
        {"conversation_id": 5, "sender_name": "Claricel", "case_number": "PB-2026-000001"},
    )
    assert n.body == "Claricel sent you a message about case PB-2026-000001."
    assert n.link_url == "/messages/5"


def test_notify_status_changed_content(app, owner):
    n = ns.notify(
        owner,
        STATUS,
        {
            "lost_report_id": 9,
            "case_number": "PB-2026-000009",
            "from_status": "lost",
            "to_status": "found",
        },
    )
    assert n.body == "Case PB-2026-000009 is now marked Found."
    assert n.link_url == "/cases/9"


def test_notify_overrides_and_rejects_external_links(app, owner):
    n = ns.notify(
        owner,
        MESSAGE,
        {
            "conversation_id": 1,
            "title": "Custom",
            "body": "Body",
            "link_url": "https://evil.example/phish",
        },
    )
    assert (n.title, n.body, n.link_url) == ("Custom", "Body", None)
    n = ns.notify(owner, MESSAGE, {"conversation_id": 1, "link_url": "/messages/1#m9"})
    assert n.link_url == "/messages/1#m9"


def test_notify_rejects_unknown_event(app, owner):
    with pytest.raises(ValueError):
        ns.notify(owner, "birthday", {})


@pytest.mark.config(JOBS_EAGER=False)
def test_notify_enqueues_dispatch_job(app, owner, push_outbox, run_jobs):
    n = ns.notify(owner, MESSAGE, {"conversation_id": 1})
    db.session.commit()
    (job,) = _all(Job)
    assert job.type == "dispatch_notification"
    assert job.payload == {"notification_id": n.id, "event": MESSAGE}
    assert push_outbox == []  # nothing sent until the worker runs
    assert run_jobs() == 1
    assert len(push_outbox) == 1
    assert _all(Job)[0].status == "done"


# --- dispatch(): channels per event ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "data", "expect_sms"),
    [
        (MATCH, {"scan_id": 1, "lost_report_id": 1, "similarity": 90}, True),
        (MESSAGE, {"conversation_id": 1}, False),
        (STATUS, {"lost_report_id": 1, "to_status": "closed"}, False),
    ],
)
def test_event_channels(app, owner, push_outbox, sms_outbox, event, data, expect_sms):
    ns.notify(owner, event, data)
    db.session.commit()
    assert len(push_outbox) == 1  # push for every event
    assert push_outbox[0]["token"] == "tok-owner-device-1234567890"
    assert len(sms_outbox) == (1 if expect_sms else 0)
    assert len(_all(PushLog)) == 1 and _all(PushLog)[0].status == "sent"
    assert len(_all(SmsLog)) == (1 if expect_sms else 0)


def test_event_table_matches_spec():
    assert ns.EVENT_CHANNELS == {
        MATCH: {"push": True, "sms": True},
        MESSAGE: {"push": True, "sms": False},
        STATUS: {"push": True, "sms": False},
    }


def test_push_payload(app, owner, push_outbox):
    n = ns.notify(owner, MATCH, {"scan_id": 4, "lost_report_id": 2, "similarity": 80})
    (sent,) = push_outbox
    assert sent["title"] == n.title
    assert sent["body"] == n.body
    assert sent["link"] == "http://localhost/scans/4"
    assert sent["data"] == {"notification_id": str(n.id), "event": MATCH}


def test_match_sms_text_is_short_and_links_to_result(app, owner, sms_outbox):
    ns.notify(owner, MATCH, {"scan_id": 4, "lost_report_id": 2, "similarity": 80})
    (sms,) = sms_outbox
    assert sms["to"] == owner.mobile_e164
    assert len(sms["message"]) <= 160
    assert "possible match" in sms["message"]
    assert "http://localhost/scans/4" in sms["message"]


def test_no_push_when_opted_out(app, owner, push_outbox):
    owner.push_opt_in = False
    db.session.commit()
    ns.notify(owner, MESSAGE, {"conversation_id": 1})
    assert push_outbox == []
    (log,) = _all(PushLog)
    assert log.status == "skipped" and "turned off" in log.error


def test_no_push_without_devices(app, make_user, push_outbox):
    user = make_user()
    ns.notify(user, MESSAGE, {"conversation_id": 1})
    assert push_outbox == []
    assert _all(PushLog)[0].error == "no registered devices"


def test_push_to_every_device(app, owner, push_outbox):
    db.session.add(DeviceToken(user_id=owner.id, fcm_token="tok-owner-device-2-abcdefghij"))
    db.session.commit()
    ns.notify(owner, MESSAGE, {"conversation_id": 1})
    assert {p["token"] for p in push_outbox} == {
        "tok-owner-device-1234567890",
        "tok-owner-device-2-abcdefghij",
    }
    assert [log.status for log in _all(PushLog)] == ["sent", "sent"]
    assert all(log.token_id for log in _all(PushLog))


def test_no_sms_when_opted_out(app, owner, sms_outbox):
    owner.sms_opt_in = False
    db.session.commit()
    ns.notify(owner, MATCH, {"scan_id": 1, "lost_report_id": 1})
    assert sms_outbox == []
    (log,) = _all(SmsLog)
    assert log.status == "skipped" and "turned off" in log.error


def test_no_sms_when_mobile_not_verified(app, make_user, sms_outbox):
    user = make_user(mobile_verified_at=None)
    ns.notify(user, MATCH, {"scan_id": 1, "lost_report_id": 1})
    assert sms_outbox == []
    (log,) = _all(SmsLog)
    assert log.status == "skipped" and "not verified" in log.error


def test_sms_daily_cap(app, owner, sms_outbox):
    app.config["SMS_DAILY_MATCH_LIMIT"] = 5
    with freeze_time("2026-09-20 03:00:00"):  # 11:00 Manila
        for scan_id in range(1, 8):
            ns.notify(owner, MATCH, {"scan_id": scan_id, "lost_report_id": 1})
        db.session.commit()
    assert len(sms_outbox) == 5
    statuses = [log.status for log in _all(SmsLog)]
    assert statuses == ["sent"] * 5 + ["skipped"] * 2
    assert "daily limit of 5" in _all(SmsLog)[-1].error
    # The next Manila day (00:30 on the 21st = 16:30 UTC on the 20th) the cap resets.
    with freeze_time("2026-09-20 16:30:00"):
        ns.notify(owner, MATCH, {"scan_id": 99, "lost_report_id": 1})
    assert len(sms_outbox) == 6


def test_sms_failures_do_not_count_towards_cap(app, owner, monkeypatch):
    from app.services import sms_service

    class Failing:
        name = "failing"

        def send(self, to, message):
            return sms_service.SmsResult(ok=False, provider="failing", error="down")

    monkeypatch.setattr(sms_service, "get_provider", lambda: Failing())
    for scan_id in range(1, 4):
        ns.notify(owner, MATCH, {"scan_id": scan_id, "lost_report_id": 1})
    assert sms_service.sent_match_sms_today(owner) == 0
    assert [log.status for log in _all(SmsLog)] == ["failed"] * 3


def test_dispatch_is_idempotent_for_sms(app, owner, sms_outbox, push_outbox):
    n = ns.notify(owner, MATCH, {"scan_id": 3, "lost_report_id": 1})
    db.session.commit()
    summary = ns.dispatch(n.id, MATCH)  # e.g. job re-run after a crash
    assert summary["sms"] == "duplicate"
    assert len(sms_outbox) == 1


def test_dispatch_missing_notification_and_inactive_user(app, owner, push_outbox):
    assert ns.dispatch(99999, MATCH) == {"status": "missing"}
    n = ns.notify(owner, MESSAGE, {"conversation_id": 1})
    push_outbox.clear()
    owner.is_active = False
    db.session.commit()
    assert ns.dispatch(n.id, MESSAGE) == {"status": "inactive_user"}
    assert push_outbox == []


def test_push_crash_is_logged_not_raised(app, owner, monkeypatch):
    class Crashing:
        name = "crashing"

        def is_enabled(self):
            return True

        def send(self, tokens, message):
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(ns, "get_push_provider", lambda: Crashing())
    ns.notify(owner, MESSAGE, {"conversation_id": 1})
    (log,) = _all(PushLog)
    assert log.status == "failed" and "provider exploded" in log.error


def test_unread_count_and_mark_helpers(app, owner, make_user):
    other = make_user()
    a = ns.notify(owner, MESSAGE, {"conversation_id": 1})
    ns.notify(owner, MESSAGE, {"conversation_id": 2})
    ns.notify(other, MESSAGE, {"conversation_id": 3})
    assert ns.unread_count(owner) == 2
    assert ns.mark_read(other, a.id) is None  # not theirs
    assert ns.mark_read(owner, a.id).is_read
    assert ns.unread_count(owner) == 1
    assert ns.mark_all_read(owner) == 1
    assert ns.unread_count(owner) == 0
    assert ns.unread_count(other) == 1


# --- Pages ---------------------------------------------------------------------------------------


def test_list_page_requires_login(client):
    assert client.get("/notifications").status_code == 302


def test_list_page_shows_own_notifications(client, login, owner, make_user):
    other = make_user()
    ns.notify(owner, MESSAGE, {"conversation_id": 1, "title": "Mine unread"})
    read = ns.notify(owner, MESSAGE, {"conversation_id": 2, "title": "Mine read"})
    read.is_read = True
    ns.notify(other, MESSAGE, {"conversation_id": 3, "title": "Not mine"})
    db.session.commit()
    login(client, owner)
    html = client.get("/notifications").get_data(as_text=True)
    assert "Mine unread" in html and "Mine read" in html
    assert "Not mine" not in html
    assert html.count("notif-item is-unread") == 1
    assert "Mark all as read" in html


def test_list_page_empty_state(client, login, make_user):
    login(client, make_user())
    html = client.get("/notifications").get_data(as_text=True)
    assert "No notifications yet" in html


def test_list_page_pagination(client, login, owner):
    for i in range(25):
        ns.notify(owner, MESSAGE, {"conversation_id": i + 1, "title": f"N{i:02d}"})
    db.session.commit()
    login(client, owner)
    first = client.get("/notifications").get_data(as_text=True)
    assert "N24" in first and "N04" not in first and "Page 1 of 2" in first
    second = client.get("/notifications?page=2").get_data(as_text=True)
    assert "N04" in second and "N24" not in second


def test_open_marks_read_and_redirects(client, login, owner):
    n = ns.notify(owner, MESSAGE, {"conversation_id": 5})
    db.session.commit()
    login(client, owner)
    response = client.get(f"/notifications/{n.id}")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/messages/5")
    db.session.refresh(n)
    assert n.is_read


def test_open_without_link_goes_to_list(client, login, owner):
    n = ns.notify(owner, MESSAGE, {"body": "x"})
    n.link_url = "//evil.example/x"  # never followed even if stored
    db.session.commit()
    login(client, owner)
    response = client.get(f"/notifications/{n.id}")
    assert response.headers["Location"].endswith("/notifications")


def test_open_someone_elses_notification_is_404(client, login, owner, make_user):
    n = ns.notify(owner, MESSAGE, {"conversation_id": 5})
    db.session.commit()
    login(client, make_user())
    assert client.get(f"/notifications/{n.id}").status_code == 404
    db.session.refresh(n)
    assert not n.is_read


def test_mark_all_read_page(client, login, owner, make_user):
    other = make_user()
    ns.notify(owner, MESSAGE, {"conversation_id": 1})
    theirs = ns.notify(other, MESSAGE, {"conversation_id": 2})
    db.session.commit()
    login(client, owner)
    response = client.post("/notifications/read-all")
    assert response.status_code == 302
    assert ns.unread_count(owner) == 0
    db.session.refresh(theirs)
    assert not theirs.is_read


def test_mark_all_read_page_needs_csrf(csrf_client, login, owner):
    ns.notify(owner, MESSAGE, {"conversation_id": 1})
    db.session.commit()
    login(csrf_client, owner)
    assert csrf_client.post("/notifications/read-all").status_code == 400
    assert ns.unread_count(owner) == 1


# --- JSON API ------------------------------------------------------------------------------------


def test_api_requires_login(client):
    for method, url in [
        ("get", "/api/notifications"),
        ("post", "/api/notifications/1/read"),
        ("post", "/api/notifications/read-all"),
    ]:
        response = getattr(client, method)(url)
        assert response.status_code == 401
        assert response.get_json()["error"]["code"] == "auth_required"


def test_api_list_only_own(client, login, owner, make_user):
    other = make_user()
    ns.notify(owner, MESSAGE, {"conversation_id": 1, "title": "one"})
    ns.notify(owner, MATCH, {"scan_id": 2, "lost_report_id": 1, "title": "two"})
    ns.notify(other, MESSAGE, {"conversation_id": 3, "title": "theirs"})
    db.session.commit()
    login(client, owner)
    data = client.get("/api/notifications").get_json()
    assert data["unread"] == 2
    assert [n["title"] for n in data["notifications"]] == ["two", "one"]
    first = data["notifications"][0]
    assert set(first) == {
        "id",
        "type",
        "title",
        "body",
        "link_url",
        "open_url",
        "is_read",
        "created_at",
    }
    assert first["link_url"] == "/scans/2"
    older = client.get(f"/api/notifications?before_id={first['id']}").get_json()
    assert [n["title"] for n in older["notifications"]] == ["one"]
    assert client.get("/api/notifications?limit=abc").status_code == 400


def test_api_mark_read_ownership(client, login, owner, make_user):
    n = ns.notify(owner, MESSAGE, {"conversation_id": 1})
    db.session.commit()
    login(client, make_user())
    response = client.post(f"/api/notifications/{n.id}/read")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
    login(client, owner)
    response = client.post(f"/api/notifications/{n.id}/read")
    assert response.get_json() == {"ok": True, "unread": 0}


def test_api_mark_all_read(client, login, owner):
    ns.notify(owner, MESSAGE, {"conversation_id": 1})
    ns.notify(owner, MESSAGE, {"conversation_id": 2})
    db.session.commit()
    login(client, owner)
    assert client.post("/api/notifications/read-all").get_json() == {
        "ok": True,
        "updated": 2,
        "unread": 0,
    }


def test_api_mark_read_needs_csrf(csrf_client, login, owner):
    from conftest import get_csrf_token

    n = ns.notify(owner, MESSAGE, {"conversation_id": 1})
    db.session.commit()
    login(csrf_client, owner)
    refused = csrf_client.post(f"/api/notifications/{n.id}/read")
    assert refused.status_code == 400
    token = get_csrf_token(csrf_client, "/notifications")
    ok = csrf_client.post(f"/api/notifications/{n.id}/read", headers={"X-CSRFToken": token})
    assert ok.status_code == 200
