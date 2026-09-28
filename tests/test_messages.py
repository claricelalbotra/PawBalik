"""Case messaging and location pins (SPEC 5.6): routes, API and message_service."""

from __future__ import annotations

import io

import pytest
import sqlalchemy as sa
from conftest import TACLOBAN, get_csrf_token, jpeg_bytes

from app.extensions import db
from app.models import (
    AbuseReport,
    AuditLog,
    Conversation,
    Message,
    MessageType,
    Notification,
    Photo,
    PhotoOwner,
    ReportStatus,
)
from app.services import message_service as ms

OUTSIDE_TACLOBAN = (14.5995, 120.9842)  # Manila


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def case(make_user, make_lost_report):
    """An owner, a finder and the owner's lost report."""
    owner = make_user(full_name="Jonalie Montera")
    finder = make_user(full_name="Claricel Albotra")
    report = make_lost_report(owner, pet_name="Bantay")
    return owner, finder, report


@pytest.fixture
def conversation(case):
    owner, finder, report = case
    conv = ms.get_or_create_conversation(report, finder)
    db.session.commit()
    return conv


@pytest.fixture
def notify_calls(monkeypatch):
    """Record notification_service.notify() calls (and still create nothing)."""
    from app.services import notification_service

    calls: list[tuple] = []

    def fake_notify(user, event, data=None):
        calls.append((user, event, dict(data or {})))

    monkeypatch.setattr(notification_service, "notify", fake_notify)
    return calls


def send_json(client, conv_id, **payload):
    return client.post(f"/api/conversations/{conv_id}/messages", json=payload)


def error_code(response):
    return response.get_json()["error"]["code"]


# --- starting conversations ------------------------------------------------------------------


def test_finder_starts_conversation_and_it_is_reused(client, login, case):
    owner, finder, report = case
    login(client, finder)
    first = client.post("/api/conversations", json={"lost_report_id": report.id})
    assert first.status_code == 201
    data = first.get_json()
    assert data["created"] is True
    assert data["url"] == f"/messages/{data['id']}"
    conv = db.session.get(Conversation, data["id"])
    assert (conv.owner_id, conv.other_user_id, conv.sighting_id) == (owner.id, finder.id, None)

    again = client.post("/api/conversations", json={"lost_report_id": report.id})
    assert again.status_code == 200
    assert again.get_json() == {"id": conv.id, "url": data["url"], "created": False}
    assert db.session.scalar(sa.select(sa.func.count(Conversation.id))) == 1


def test_create_with_first_message(client, login, case):
    owner, finder, report = case
    login(client, finder)
    response = client.post(
        "/api/conversations", json={"lost_report_id": report.id, "body": "  I saw him  "}
    )
    assert response.status_code == 201
    msgs = db.session.scalars(sa.select(Message)).all()
    assert [(m.type, m.body, m.sender_id) for m in msgs] == [("text", "I saw him", finder.id)]


def test_owner_cannot_message_themselves(client, login, case):
    owner, _, report = case
    login(client, owner)
    response = client.post("/api/conversations", json={"lost_report_id": report.id})
    assert response.status_code == 400
    assert error_code(response) == "self_conversation"
    with pytest.raises(ms.ConversationError):
        ms.get_or_create_conversation(report, owner)


def test_create_requires_login_and_report_id(client, login, user, case):
    _, _, report = case
    response = client.post("/api/conversations", json={"lost_report_id": report.id})
    assert response.status_code == 401
    login(client, user)
    response = client.post("/api/conversations", json={})
    assert response.status_code == 400
    assert error_code(response) == "validation_error"
    response = client.post("/api/conversations", json={"lost_report_id": 99999})
    assert response.status_code == 404


def test_hidden_and_closed_cases_block_new_conversations(
    client, login, make_user, make_lost_report
):
    finder = make_user()
    hidden = make_lost_report(is_hidden=True, hidden_reason="spam")
    closed = make_lost_report(status=ReportStatus.CLOSED)
    login(client, finder)
    response = client.post("/api/conversations", json={"lost_report_id": hidden.id})
    assert response.status_code == 404  # a hidden case is invisible to non-owners
    response = client.post("/api/conversations", json={"lost_report_id": closed.id})
    assert response.status_code == 403
    assert error_code(response) == "case_closed"
    assert db.session.scalar(sa.select(sa.func.count(Conversation.id))) == 0


def test_inactive_owner_cannot_be_messaged(client, login, make_user, make_lost_report):
    owner = make_user(is_active=False)
    report = make_lost_report(owner)
    login(client, make_user())
    response = client.post("/api/conversations", json={"lost_report_id": report.id})
    assert response.status_code == 403
    assert error_code(response) == "recipient_inactive"


def test_owner_messages_finder_only_with_sighting_access(
    client, login, case, make_sighting, make_scan_result
):
    owner, finder, report = case
    sighting = make_sighting(finder)
    login(client, owner)
    payload = {"lost_report_id": report.id, "sighting_id": sighting.id}

    # No scan result for this sighting yet: the owner may not know it exists.
    response = client.post("/api/conversations", json=payload)
    assert response.status_code == 404

    make_scan_result(owner, sighting)  # the owner's Lost Pet Scanner returned it
    response = client.post("/api/conversations", json=payload)
    assert response.status_code == 201
    conv = db.session.get(Conversation, response.get_json()["id"])
    assert conv.owner_id == owner.id
    assert conv.other_user_id == finder.id
    assert conv.sighting_id == sighting.id


def test_owner_cannot_reach_hidden_sighting(client, login, case, make_sighting, make_scan_result):
    owner, finder, report = case
    sighting = make_sighting(finder, is_hidden=True, hidden_reason="abuse")
    make_scan_result(owner, sighting)
    login(client, owner)
    response = client.post(
        "/api/conversations", json={"lost_report_id": report.id, "sighting_id": sighting.id}
    )
    assert response.status_code == 404


def test_owner_own_sighting_is_self_conversation(
    client, login, case, make_sighting, make_scan_result
):
    owner, _, report = case
    sighting = make_sighting(owner)
    login(client, owner)
    response = client.post(
        "/api/conversations", json={"lost_report_id": report.id, "sighting_id": sighting.id}
    )
    assert response.status_code == 400
    assert error_code(response) == "self_conversation"


def test_finder_attaches_only_own_sighting(client, login, case, make_user, make_sighting):
    owner, finder, report = case
    own = make_sighting(finder)
    someone_elses = make_sighting(make_user())
    login(client, finder)
    response = client.post(
        "/api/conversations", json={"lost_report_id": report.id, "sighting_id": someone_elses.id}
    )
    assert response.status_code == 404
    response = client.post(
        "/api/conversations", json={"lost_report_id": report.id, "sighting_id": own.id}
    )
    assert response.status_code == 201
    conv = db.session.get(Conversation, response.get_json()["id"])
    assert conv.sighting_id == own.id
    # Sharing it lets the owner see the sighting (policy rule d).
    from app.services import policy_service

    assert policy_service.can_view_sighting(owner, own)


def test_existing_conversation_gets_sighting_attached(conversation, case, make_sighting):
    _, finder, report = case
    sighting = make_sighting(finder)
    conv = ms.get_or_create_conversation(report, finder, sighting)
    assert conv.id == conversation.id
    assert conv.sighting_id == sighting.id


# --- the /messages/new page -------------------------------------------------------------------


def test_new_page_form_and_post(client, login, case):
    owner, finder, report = case
    login(client, finder)
    page = client.get(f"/messages/new?lost_report_id={report.id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Message the" in html and "Jonalie Montera" in html and report.case_number in html

    empty = client.post("/messages/new", data={"lost_report_id": report.id, "body": "  "})
    assert empty.status_code == 200
    assert "This field is required." in empty.get_data(as_text=True)
    assert db.session.scalar(sa.select(sa.func.count(Conversation.id))) == 0

    response = client.post("/messages/new", data={"lost_report_id": report.id, "body": "Hello po!"})
    assert response.status_code == 302
    conv = db.session.scalar(sa.select(Conversation))
    assert response.headers["Location"].endswith(f"/messages/{conv.id}")
    assert conv.messages[0].body == "Hello po!"

    # Opening "Message owner" again goes straight to the existing thread.
    again = client.get(f"/messages/new?lost_report_id={report.id}")
    assert again.status_code == 302
    assert again.headers["Location"].endswith(f"/messages/{conv.id}")


def test_new_page_guards(client, login, case, make_lost_report):
    owner, finder, report = case
    assert client.get(f"/messages/new?lost_report_id={report.id}").status_code == 302  # login
    login(client, owner)
    own = client.get(f"/messages/new?lost_report_id={report.id}")
    assert own.status_code == 302  # back to the case with a flash message
    assert f"/cases/{report.id}" in own.headers["Location"]
    hidden = make_lost_report(is_hidden=True)
    assert client.get(f"/messages/new?lost_report_id={hidden.id}").status_code == 404


def test_new_page_infers_owner_case_from_scan(
    client, login, case, make_sighting, make_scan_result, make_lost_report
):
    owner, finder, report = case
    make_lost_report(owner, pet_name="Mingming", species="cat")  # a second open case
    sighting = make_sighting(finder)
    result = make_scan_result(owner, sighting)
    login(client, owner)

    # Two open cases and no scan source: ask which case.
    page = client.get(f"/messages/new?sighting_id={sighting.id}")
    assert page.status_code == 200
    assert "Which case is this" in page.get_data(as_text=True)

    result.scan.source_lost_report_id = report.id
    db.session.commit()
    page = client.get(f"/messages/new?sighting_id={sighting.id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Message the" in html and "finder" in html
    assert f'name="lost_report_id" value="{report.id}"' in html


# --- participation ---------------------------------------------------------------------------


def test_outsiders_get_404_everywhere(client, login, conversation, make_user, make_admin):
    for outsider in (make_user(), make_admin()):
        login(client, outsider)
        cid = conversation.id
        assert client.get(f"/messages/{cid}").status_code == 404
        assert client.get(f"/api/conversations/{cid}/messages").status_code == 404
        assert send_json(client, cid, type="text", body="hi").status_code == 404
        assert client.post(f"/api/conversations/{cid}/read").status_code == 404
        assert client.post(f"/messages/{cid}/report", data={"reason": "spam"}).status_code == 404
        listed = client.get("/api/conversations").get_json()["conversations"]
        assert listed == []
    assert db.session.scalar(sa.select(sa.func.count(Message.id))) == 0
    assert db.session.scalar(sa.select(sa.func.count(AbuseReport.id))) == 0


def test_guests_are_sent_to_login(client, conversation):
    assert client.get("/messages").status_code == 302
    assert client.get(f"/messages/{conversation.id}").status_code == 302
    response = client.get(f"/api/conversations/{conversation.id}/messages")
    assert response.status_code == 401
    assert error_code(response) == "auth_required"


def test_both_participants_can_open_thread(client, login, case, conversation):
    owner, finder, _ = case
    for person, other in ((owner, finder), (finder, owner)):
        login(client, person)
        page = client.get(f"/messages/{conversation.id}")
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert other.full_name in html
        assert "Verification checklist" in html
        assert "Report this user" in html
        assert "never shown in chat automatically" in html


# --- sending ------------------------------------------------------------------------------------


def test_send_text_message_and_notify(client, login, case, conversation, notify_calls):
    owner, finder, _ = case
    login(client, finder)
    response = send_json(client, conversation.id, type="text", body="Nakita ko siya sa plaza")
    assert response.status_code == 201
    message = response.get_json()["message"]
    assert message["type"] == "text"
    assert message["mine"] is True
    assert message["body"] == "Nakita ko siya sa plaza"
    assert conversation.last_message_at is not None

    assert len(notify_calls) == 1
    recipient, event, data = notify_calls[0]
    assert recipient.id == owner.id and event == "new_message"
    assert data["conversation_id"] == conversation.id
    assert data["message_id"] == message["id"]
    assert data["link_url"] == f"/messages/{conversation.id}"
    assert "plaza" not in data["body"]  # the notification never quotes the message


def test_new_message_creates_in_app_notification(client, login, case, conversation):
    owner, finder, _ = case
    login(client, owner)
    assert send_json(client, conversation.id, body="Salamat!").status_code == 201
    notification = db.session.scalar(sa.select(Notification))
    assert notification.user_id == finder.id
    assert notification.type == "new_message"
    assert notification.link_url == f"/messages/{conversation.id}"


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"type": "text", "body": "   "}, "body"),
        ({"type": "text", "body": "x" * 2001}, "body"),
        ({"type": "video", "body": "hi"}, "type"),
        ({"type": "system", "body": "Case closed"}, "type"),
        ({"type": "location_pin"}, "location"),
        ({"type": "location_pin", "lat": "abc", "lng": "1"}, "location"),
        (
            {"type": "location_pin", "lat": OUTSIDE_TACLOBAN[0], "lng": OUTSIDE_TACLOBAN[1]},
            "location",
        ),
        (
            {
                "type": "location_pin",
                "lat": TACLOBAN[0],
                "lng": TACLOBAN[1],
                "location_label": "x" * 161,
            },
            "location_label",
        ),
        ({"type": "image"}, "photo"),
    ],
)
def test_message_validation(client, login, case, conversation, payload, field):
    _, finder, _ = case
    login(client, finder)
    response = send_json(client, conversation.id, **payload)
    assert response.status_code == 400
    body = response.get_json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"]["field"] == field
    assert db.session.scalar(sa.select(sa.func.count(Message.id))) == 0


def test_send_location_pin(client, login, case, conversation):
    _, finder, _ = case
    login(client, finder)
    response = send_json(
        client,
        conversation.id,
        type="location_pin",
        lat=TACLOBAN[0],
        lng=TACLOBAN[1],
        location_label="Brgy. San Jose chapel",
        body="Dito ko siya nakita",
    )
    assert response.status_code == 201
    message = response.get_json()["message"]
    loc = message["location"]
    assert loc["label"] == "Brgy. San Jose chapel"
    assert loc["view_url"] == (
        "https://www.google.com/maps/search/?api=1&query=11.243700,125.004600"
    )
    assert loc["directions_url"].startswith("https://www.google.com/maps/dir/?api=1&destination=")
    assert message["body"] == "Dito ko siya nakita"

    page = client.get(f"/messages/{conversation.id}").get_data(as_text=True)
    assert "Open in Google Maps" in page and "Get Directions" in page
    assert "11.243700, 125.004600" in page


def test_send_image_message(client, login, case, conversation, make_user):
    owner, finder, _ = case
    login(client, finder)
    response = client.post(
        f"/api/conversations/{conversation.id}/messages",
        data={
            "type": "image",
            "body": "Siya ba ito?",
            "photo": (io.BytesIO(jpeg_bytes()), "p.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    message = response.get_json()["message"]
    assert message["type"] == "image" and message["body"] == "Siya ba ito?"
    photo = db.session.scalar(sa.select(Photo).where(Photo.owner_type == PhotoOwner.MESSAGE))
    assert photo.owner_id == conversation.id
    assert message["photo"]["full_url"].startswith("/media/messages/")
    assert message["photo"]["thumb_url"].endswith("v=thumb")

    # Participants can load the photo; nobody else can.
    assert client.get(message["photo"]["full_url"]).status_code == 200
    login(client, owner)
    assert client.get(message["photo"]["full_url"]).status_code == 200
    login(client, make_user())
    assert client.get(message["photo"]["full_url"]).status_code == 404


def test_image_type_is_inferred_and_bad_files_rejected(client, login, case, conversation):
    _, finder, _ = case
    login(client, finder)
    response = client.post(
        f"/api/conversations/{conversation.id}/messages",
        data={"photo": (io.BytesIO(b"not an image at all"), "evil.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["field"] == "photo"
    ok = client.post(
        f"/api/conversations/{conversation.id}/messages",
        data={"photo": (io.BytesIO(jpeg_bytes()), "p.jpg")},
        content_type="multipart/form-data",
    )
    assert ok.status_code == 201
    assert ok.get_json()["message"]["type"] == "image"


def test_closed_case_is_read_only(client, login, case, conversation):
    owner, finder, report = case
    ms.send_message(conversation, finder, body="Hello")
    report.change_status(ReportStatus.CLOSED, actor=owner)
    db.session.commit()

    login(client, finder)
    response = send_json(client, conversation.id, body="Still there?")
    assert response.status_code == 403
    assert error_code(response) == "read_only"
    data = client.get(f"/api/conversations/{conversation.id}/messages").get_json()
    assert data["can_send"] is False
    assert "closed" in data["read_only_reason"]
    page = client.get(f"/messages/{conversation.id}").get_data(as_text=True)
    assert "data-composer" not in page
    assert "This case is closed" in page
    assert "Hello" in page  # history stays readable


def test_hidden_case_and_inactive_partner_block_sending(client, login, case, conversation):
    owner, finder, report = case
    login(client, finder)
    report.is_hidden = True
    db.session.commit()
    assert send_json(client, conversation.id, body="hi").status_code == 403
    report.is_hidden = False
    owner.is_active = False
    db.session.commit()
    response = send_json(client, conversation.id, body="hi")
    assert response.status_code == 403
    assert "no longer active" in response.get_json()["error"]["message"]


def test_plain_form_post_redirects_back_to_thread(client, login, case, conversation):
    _, finder, _ = case
    login(client, finder)
    response = client.post(
        f"/api/conversations/{conversation.id}/messages",
        data={"type": "text", "body": "Sent without JavaScript"},
        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
    )
    assert response.status_code == 302
    assert f"/messages/{conversation.id}" in response.headers["Location"]
    assert conversation.messages[-1].body == "Sent without JavaScript"

    bad = client.post(
        f"/api/conversations/{conversation.id}/messages",
        data={"type": "location_pin", "lat": OUTSIDE_TACLOBAN[0], "lng": OUTSIDE_TACLOBAN[1]},
        headers={"Accept": "text/html"},
    )
    assert bad.status_code == 302
    page = client.get(bad.headers["Location"]).get_data(as_text=True)
    assert "outside Tacloban City" in page


def test_user_text_is_escaped_in_pages(client, login, case, conversation):
    owner, finder, _ = case
    ms.send_message(conversation, finder, body='<script>alert("x")</script>')
    db.session.commit()
    login(client, owner)
    html = client.get(f"/messages/{conversation.id}").get_data(as_text=True)
    assert '<script>alert("x")</script>' not in html
    assert "&lt;script&gt;" in html
    inbox = client.get("/messages").get_data(as_text=True)
    assert "<script>alert" not in inbox


# --- polling, read receipts, system messages ----------------------------------------------------


def test_after_parameter_semantics(client, login, case, conversation):
    owner, finder, _ = case
    ids = [ms.send_message(conversation, finder, body=f"m{i}").id for i in range(3)]
    db.session.commit()
    login(client, owner)
    url = f"/api/conversations/{conversation.id}/messages"

    everything = client.get(url).get_json()["messages"]
    assert [m["id"] for m in everything] == ids  # oldest first

    newer = client.get(f"{url}?after={ids[0]}").get_json()["messages"]
    assert [m["body"] for m in newer] == ["m1", "m2"]
    assert client.get(f"{url}?after={ids[-1]}").get_json()["messages"] == []
    for bad in ("abc", "-1"):
        response = client.get(f"{url}?after={bad}")
        assert response.status_code == 400
        assert error_code(response) == "validation_error"
    # Polling does not mark anything as read.
    assert conversation.unread_count_for(owner) == 3


def test_read_receipts(client, login, case, conversation):
    owner, finder, _ = case
    login(client, finder)
    sent = send_json(client, conversation.id, body="Hello").get_json()["message"]
    data = client.get(f"/api/conversations/{conversation.id}/messages").get_json()
    assert data["last_read_mine_id"] == 0

    login(client, owner)
    listed = client.get("/api/conversations").get_json()
    assert listed["unread_total"] == 1
    assert listed["conversations"][0]["unread"] == 1
    response = client.post(f"/api/conversations/{conversation.id}/read")
    assert response.get_json() == {"marked": 1, "unread_total": 0}
    assert client.post(f"/api/conversations/{conversation.id}/read").get_json()["marked"] == 0

    login(client, finder)
    data = client.get(f"/api/conversations/{conversation.id}/messages").get_json()
    assert data["last_read_mine_id"] == sent["id"]
    assert data["messages"][0]["read"] is True


def test_opening_thread_marks_messages_read(client, login, case, conversation):
    owner, finder, _ = case
    ms.send_message(conversation, finder, body="Hi")
    db.session.commit()
    assert ms.unread_count(owner) == 1
    login(client, owner)
    assert client.get(f"/messages/{conversation.id}").status_code == 200
    assert ms.unread_count(owner) == 0
    assert ms.unread_count(finder) == 0


def test_system_messages(client, login, case, conversation):
    owner, finder, _ = case
    ms.post_system_message(conversation, "Case marked as Found")
    db.session.commit()
    login(client, finder)
    data = client.get(f"/api/conversations/{conversation.id}/messages").get_json()
    [message] = data["messages"]
    assert message["type"] == "system"
    assert message["mine"] is False and message["sender_id"] is None
    assert message["body"] == "Case marked as Found"
    assert conversation.unread_count_for(finder) == 1
    assert conversation.unread_count_for(owner) == 1
    page = client.get(f"/messages/{conversation.id}").get_data(as_text=True)
    assert "bubble--system" in page and "Case marked as Found" in page


def test_conversations_for_report_orders_by_activity(case, make_user):
    owner, finder, report = case
    other = make_user()
    first = ms.get_or_create_conversation(report, finder)
    second = ms.get_or_create_conversation(report, other)
    ms.send_message(first, finder, body="newest")
    db.session.commit()
    assert [c.id for c in ms.conversations_for_report(report)] == [first.id, second.id]


# --- inbox ----------------------------------------------------------------------------------------


def test_inbox_lists_conversations(client, login, case, make_user, make_lost_report):
    owner, finder, report = case
    other_report = make_lost_report(make_user(full_name="Reynald Cabujoc"), pet_name="Muning")
    quiet = ms.get_or_create_conversation(other_report, finder)
    busy = ms.get_or_create_conversation(report, finder)
    ms.send_message(busy, owner, type=MessageType.LOCATION_PIN, lat=TACLOBAN[0], lng=TACLOBAN[1])
    db.session.commit()

    login(client, finder)
    items = client.get("/api/conversations").get_json()["conversations"]
    assert [i["id"] for i in items] == [busy.id, quiet.id]  # latest activity first
    top = items[0]
    assert top["other_name"] == "Jonalie Montera"
    assert top["case_number"] == report.case_number
    assert top["last_message_preview"] == "Shared a location pin"
    assert top["unread"] == 1
    assert items[1]["last_message_preview"] == "No messages yet"

    html = client.get("/messages").get_data(as_text=True)
    assert "Jonalie Montera" in html and "Reynald Cabujoc" in html
    assert report.case_number in html
    assert "1 unread message" in html


def test_inbox_empty_state(client, login, user):
    login(client, user)
    html = client.get("/messages").get_data(as_text=True)
    assert "No conversations yet" in html


def test_preview_helper(case, conversation):
    owner, finder, _ = case
    assert ms.preview(None) == "No messages yet"
    long = ms.send_message(conversation, finder, body="word " * 40)
    text = ms.preview(long, finder)
    assert text.startswith("You: word") and text.endswith("…")
    assert ms.preview(long, owner).startswith("word")


# --- abuse reports ------------------------------------------------------------------------------


def test_report_this_user(client, login, case, conversation):
    owner, finder, _ = case
    login(client, owner)
    response = client.post(
        f"/messages/{conversation.id}/report", data={"reason": "Asking for money first"}
    )
    assert response.status_code == 302
    report = db.session.scalar(sa.select(AbuseReport))
    assert (report.reporter_id, report.target_user_id, report.conversation_id) == (
        owner.id,
        finder.id,
        conversation.id,
    )
    assert report.status == "open"
    assert report.reason == "Asking for money first"
    log = db.session.scalar(sa.select(AuditLog).where(AuditLog.event == "abuse.reported"))
    assert log.actor_id == owner.id and log.details["target_user_id"] == finder.id

    # A second report while the first is open is merged, not duplicated.
    client.post(f"/messages/{conversation.id}/report", data={"reason": "Also rude"})
    assert db.session.scalar(sa.select(sa.func.count(AbuseReport.id))) == 1
    assert "Also rude" in db.session.scalar(sa.select(AbuseReport)).reason


def test_report_requires_reason(client, login, case, conversation):
    _, finder, _ = case
    login(client, finder)
    response = client.post(f"/messages/{conversation.id}/report", data={"reason": " "})
    assert response.status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(AbuseReport.id))) == 0


# --- security: CSRF and rate limits ---------------------------------------------------------------


def test_sending_requires_csrf_token(csrf_client, login, case, conversation):
    _, finder, _ = case
    login(csrf_client, finder)
    url = f"/api/conversations/{conversation.id}/messages"
    response = csrf_client.post(url, json={"type": "text", "body": "hi"})
    assert response.status_code == 400
    assert error_code(response) == "csrf_failed"
    token = get_csrf_token(csrf_client, f"/messages/{conversation.id}")
    response = csrf_client.post(
        url, json={"type": "text", "body": "hi"}, headers={"X-CSRFToken": token}
    )
    assert response.status_code == 201


@pytest.mark.config(RATELIMIT_ENABLED=True, RATE_LIMIT_MESSAGES="2 per minute")
def test_sending_is_rate_limited(client, login, case, conversation):
    _, finder, _ = case
    login(client, finder)
    for i in range(2):
        assert send_json(client, conversation.id, body=f"m{i}").status_code == 201
    response = send_json(client, conversation.id, body="too many")
    assert response.status_code == 429
    assert error_code(response) == "rate_limited"
    # Reading is not limited.
    assert client.get(f"/api/conversations/{conversation.id}/messages").status_code == 200


# --- service guards -------------------------------------------------------------------------------


def test_service_rejects_non_participants(case, conversation, make_user):
    with pytest.raises(ms.ConversationError) as info:
        ms.send_message(conversation, make_user(), body="hi")
    assert info.value.status == 404
    with pytest.raises(ms.ConversationError):
        ms.report_user(conversation, make_user(), "spam")


def test_foreign_photo_cannot_be_reused(case, conversation, make_photo, make_user):
    _, finder, report = case
    other_conv = ms.get_or_create_conversation(report, make_user())
    foreign = make_photo(PhotoOwner.MESSAGE, other_conv.id)
    with pytest.raises(ms.ValidationError):
        ms.send_message(conversation, finder, type=MessageType.IMAGE, photo=foreign)
