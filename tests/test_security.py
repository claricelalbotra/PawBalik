"""Security headers, CSRF enforcement, JSON error shape, media access (SPEC 10)."""

from __future__ import annotations

from conftest import DEFAULT_PASSWORD, get_csrf_token


def test_security_headers(client):
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "https://tile.openstreetmap.org" in csp
    assert "https://res.cloudinary.com" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_security_headers_on_api_and_errors(client):
    for path in ("/api/nope", "/does-not-exist", "/healthz"):
        assert "Content-Security-Policy" in client.get(path).headers


def test_session_cookie_flags(client, make_user):
    make_user(email="a@example.com")
    response = client.post("/login", data={"email": "a@example.com", "password": DEFAULT_PASSWORD})
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_csrf_meta_tag_present(csrf_client):
    assert get_csrf_token(csrf_client)


def test_csrf_blocks_form_post_without_token(csrf_client, make_user):
    make_user(email="a@example.com")
    response = csrf_client.post(
        "/login", data={"email": "a@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 400
    assert b"reload the page" in response.data


def test_csrf_allows_form_post_with_token(csrf_client, make_user):
    make_user(email="a@example.com")
    token = get_csrf_token(csrf_client, "/login")
    response = csrf_client.post(
        "/login",
        data={"email": "a@example.com", "password": DEFAULT_PASSWORD, "csrf_token": token},
    )
    assert response.status_code == 302


def test_csrf_json_api_uses_header(csrf_client, user, login):
    login(csrf_client, user)
    response = csrf_client.post("/api/devices", json={"token": "abc"})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "csrf_failed"
    token = get_csrf_token(csrf_client)
    response = csrf_client.post(
        "/api/devices", json={"token": "abc"}, headers={"X-CSRFToken": token}
    )
    body = response.get_json(silent=True) or {}
    assert (
        body.get("error", {}).get("code") != "csrf_failed"
    )  # CSRF passed; validation may still reject the fake token


def test_logout_needs_csrf(csrf_client, user, login):
    login(csrf_client, user)
    assert csrf_client.post("/logout").status_code == 400


def test_api_json_errors(client, user, login):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"

    response = client.get("/api/conversations")
    assert response.status_code == 401
    assert response.get_json() == {
        "error": {"code": "auth_required", "message": "Please log in to continue."}
    }

    response = client.delete("/api/notifications/1/read")
    assert response.status_code == 405
    assert response.get_json()["error"]["code"] == "method_not_allowed"

    login(client, user)
    response = client.get("/api/admin/stats")
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "forbidden"


def test_html_error_pages(client, user, login):
    response = client.get("/nope")
    assert response.status_code == 404
    assert b"Page not found" in response.data
    login(client, user)
    response = client.get("/admin")
    assert response.status_code == 403
    assert b"Access denied" in response.data


def test_server_error_page(app, client):
    @app.get("/boom-test")
    def boom():  # pragma: no cover - raises
        raise RuntimeError("boom")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get("/boom-test")
    assert response.status_code == 500
    assert b"Something went wrong" in response.data


def test_media_serves_public_photo_and_hides_private(
    client, make_lost_report, make_sighting, make_user, login
):
    report = make_lost_report()
    key = report.primary_photo.public_id
    response = client.get(f"/media/{key}")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"

    sighting = make_sighting()
    private_key = sighting.primary_photo.public_id
    assert client.get(f"/media/{private_key}").status_code == 404
    login(client, make_user())
    assert client.get(f"/media/{private_key}").status_code == 404
    login(client, sighting.finder)
    assert client.get(f"/media/{private_key}").status_code == 200


def test_media_rejects_unknown_and_traversal(client):
    assert client.get("/media/lost_reports/1/nothing.jpg").status_code == 404
    assert client.get("/media/../../.env").status_code == 404
    assert client.get("/media/..%2F..%2F.env").status_code == 404
