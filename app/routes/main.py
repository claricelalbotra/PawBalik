"""Landing, about, privacy, error pages and local media serving.

Error handling (for the whole app) lives here too:

* ``/api/...`` requests (and clients that prefer JSON) get the SPEC shape
  ``{"error": {"code": "...", "message": "..."}}`` with the HTTP status.
* Everything else gets a friendly HTML page (templates/errors/).
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException, InternalServerError

from app.extensions import db
from app.models import Photo, StorageKind
from app.services import policy_service, storage_service
from app.utils.http import json_error, wants_json

log = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# HTTP status -> (JSON error code, friendly message)
ERROR_TEXT: dict[int, tuple[str, str]] = {
    400: (
        "bad_request",
        "The request could not be understood. Please check the form and try again.",
    ),
    401: ("auth_required", "Please log in to continue."),
    403: ("forbidden", "You do not have permission to do that."),
    404: ("not_found", "We could not find that page."),
    405: ("method_not_allowed", "That action is not allowed here."),
    413: ("file_too_large", "That upload is too large. Photos must be 8 MB or smaller."),
    429: ("rate_limited", "Too many requests. Please wait a moment and try again."),
    500: ("server_error", "Something went wrong on our side. Please try again later."),
    501: ("not_implemented", "This feature is not available yet."),
}
HTML_ERROR_TEMPLATES = {403, 404, 429, 500}


@bp.get("/")
def index():
    """Landing page (SPEC 16.3). Uses static/images/hero.png when present, else the bundled SVG."""
    hero_png = Path(current_app.static_folder or "") / "images" / "hero.png"
    return render_template("index.html", hero_image_exists=hero_png.is_file())


@bp.get("/about")
def about():
    return render_template("about.html")


@bp.get("/privacy")
def privacy():
    return render_template("privacy.html")


@bp.get("/healthz")
def healthz():
    """Tiny health check for monitoring (no database access)."""
    return jsonify({"status": "ok"})


@bp.get("/media/<path:key>")
def media(key: str):
    """Serve a locally stored photo (storage fallback, SPEC 8) after a visibility check.

    Keys are unguessable, and a photo the viewer may not see answers 404 (not
    403) so its existence is not revealed. ``?v=thumb`` asks for the thumbnail.
    """
    photo = Photo.by_key(key)
    if photo is None or photo.storage != StorageKind.LOCAL:
        abort(404)
    if not policy_service.can_view_photo(current_user, photo):
        abort(404)
    variant = request.args.get("v", "full")
    path = storage_service.local_path(photo.public_id, variant)
    if path is None:
        abort(404)
    response = send_file(path, mimetype="image/jpeg", max_age=3600, conditional=True)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


# --- Error handlers (registered app-wide) ------------------------------------------------------


def _error_response(status: int, code: str | None = None, message: str | None = None):
    default_code, default_message = ERROR_TEXT.get(status, ("error", "Something went wrong."))
    code = code or default_code
    message = message or default_message
    if wants_json():
        return json_error(code, message, status)
    template = f"errors/{status}.html" if status in HTML_ERROR_TEMPLATES else "errors/error.html"
    return render_template(template, status=status, code=code, message=message), status


@bp.app_errorhandler(CSRFError)
def handle_csrf_error(error: CSRFError):
    return _error_response(
        400,
        "csrf_failed",
        "Your session expired or the form is out of date. Please reload the page and try again.",
    )


@bp.app_errorhandler(HTTPException)
def handle_http_exception(error: HTTPException):
    status = error.code or 500
    if status == 500:
        return handle_server_error(error)
    message = None
    if status in (400, 422) and error.description and error.description != type(error).description:
        message = error.description  # a specific message given with abort(400, "...")
    return _error_response(status, message=message)


@bp.app_errorhandler(InternalServerError)
def handle_server_error(error: Exception):
    try:
        db.session.rollback()
    except Exception:  # noqa: BLE001 - never fail while reporting a failure
        log.exception("Rollback failed while handling a server error")
    return _error_response(500)
