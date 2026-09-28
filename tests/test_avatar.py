"""Profile pictures: upload, replace, remove, display and visibility."""

from __future__ import annotations

import io

import sqlalchemy as sa

from app.extensions import db
from app.models import Photo
from app.services import storage_service
from tests.conftest import jpeg_bytes


def upload(client, data: bytes | None = None, name: str = "me.jpg"):
    payload = {"avatar": (io.BytesIO(data or jpeg_bytes()), name)}
    return client.post("/profile/avatar", data=payload, content_type="multipart/form-data")


def url_of(app, user) -> str:
    with app.test_request_context():
        return user.avatar_url


def avatar_photos() -> list[Photo]:
    return db.session.scalars(sa.select(Photo).where(Photo.owner_type == "avatar")).all()


def test_initial_shown_without_picture(client, make_user, login):
    user = make_user(full_name="reynald Cabujoc")
    login(client, user)
    html = client.get("/profile").get_data(as_text=True)
    assert user.avatar_url is None and user.initial == "R"
    assert 'class="avatar avatar--md" aria-hidden="true">R</span>' in html
    assert "Upload picture" in html and "Remove picture" not in html


def test_upload_sets_avatar_and_shows_it(app, client, user, login):
    login(client, user)
    response = upload(client)
    assert response.status_code == 302
    db.session.refresh(user)
    assert user.avatar_photo is not None
    assert user.avatar_photo.owner_type == "avatar" and user.avatar_photo.owner_id == user.id
    html = client.get("/profile").get_data(as_text=True)
    assert url_of(app, user) in html
    assert "Change picture" in html and "Remove picture" in html
    assert storage_service.local_path(user.avatar_photo.public_id).is_file()


def test_replacing_deletes_the_old_picture(client, user, login):
    login(client, user)
    upload(client, jpeg_bytes((10, 200, 10)))
    db.session.refresh(user)
    old = user.avatar_photo
    old_path = storage_service.local_path(old.public_id)
    upload(client, jpeg_bytes((200, 10, 10)))
    db.session.expire_all()
    assert len(avatar_photos()) == 1
    assert avatar_photos()[0].id != old.id
    assert not old_path.exists()


def test_remove_picture(client, user, login):
    login(client, user)
    upload(client)
    response = client.post("/profile/avatar/delete")
    assert response.status_code == 302
    db.session.expire_all()
    assert avatar_photos() == []
    db.session.refresh(user)
    assert user.avatar_photo_id is None


def test_rejects_non_images_and_missing_file(client, user, login):
    login(client, user)
    response = upload(client, b"definitely not an image")
    assert response.status_code == 302
    assert avatar_photos() == []
    response = client.post("/profile/avatar", data={}, content_type="multipart/form-data")
    assert response.status_code == 302
    assert avatar_photos() == []


def test_requires_login(client):
    assert upload(client).status_code == 302
    assert avatar_photos() == []


def test_upload_needs_csrf(csrf_client, user, login):
    login(csrf_client, user)
    assert upload(csrf_client).status_code == 400


def test_avatar_is_public(app, client, make_user, login):
    owner = make_user()
    login(client, owner)
    upload(client)
    db.session.refresh(owner)
    url = url_of(app, owner)
    client.post("/logout")
    assert client.get(url).status_code == 200  # guests see profile pictures next to names


def test_avatar_in_header_and_case_page(app, client, make_user, make_lost_report, login):
    owner = make_user(full_name="Jonalie Montera")
    report = make_lost_report(owner=owner)
    login(client, owner)
    upload(client)
    db.session.refresh(owner)
    url = url_of(app, owner)
    home = client.get("/").get_data(as_text=True)
    assert f'<img src="{url}"' in home
    case = client.get(f"/cases/{report.id}").get_data(as_text=True)
    assert url in case
