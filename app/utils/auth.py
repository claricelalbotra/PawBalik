"""Authorization decorators.

* Page routes: use Flask-Login's ``@login_required`` (redirects to login;
  for ``/api`` paths it answers JSON 401 automatically, see create_app) and
  ``@admin_required``.
* JSON routes: ``@api_login_required`` / ``@api_admin_required`` always
  answer with the SPEC JSON error shape.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import abort
from flask_login import current_user, login_required

from app.utils.http import json_error, wants_json


def _is_admin() -> bool:
    return bool(current_user.is_authenticated and current_user.is_active and current_user.is_admin)


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Logged-in administrators only. Guests -> login (or JSON 401); users -> 403."""

    @wraps(view)
    @login_required
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not _is_admin():
            if wants_json():
                return json_error("forbidden", "Administrator access is required.", 403)
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def api_login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """JSON endpoints: 401 ``{"error": {"code": "auth_required", ...}}`` for guests."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            return json_error("auth_required", "Please log in to continue.", 401)
        return view(*args, **kwargs)

    return wrapped


def api_admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """JSON endpoints: 401 for guests, 403 for non-admins."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not current_user.is_authenticated:
            return json_error("auth_required", "Please log in to continue.", 401)
        if not _is_admin():
            return json_error("forbidden", "Administrator access is required.", 403)
        return view(*args, **kwargs)

    return wrapped
