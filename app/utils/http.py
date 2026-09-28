"""HTTP helpers: the SPEC JSON error shape, content negotiation, safe redirects."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

from flask import Response, jsonify, request


def json_error(
    code: str, message: str, status: int = 400, details: Any = None
) -> tuple[Response, int]:
    """``{"error": {"code": ..., "message": ...}}`` (plus optional ``details``), SPEC 11.

    Return it straight from a view: ``return json_error("not_found", "No such case.", 404)``.
    ``code`` is a short snake_case string clients can switch on; ``message`` is
    human readable and safe to show in a toast.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return jsonify({"error": error}), status


def json_ok(data: Any = None, status: int = 200) -> tuple[Response, int]:
    """Plain JSON success response (``{}`` if data is None)."""
    return jsonify(data if data is not None else {}), status


def wants_json() -> bool:
    """True for ``/api/...`` requests or clients that prefer JSON over HTML."""
    if request.path.startswith("/api/"):
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json"  # ties (e.g. */*) resolve to HTML


def is_safe_redirect_url(target: str | None) -> bool:
    """Only allow redirects to this host (prevents open redirects via ``?next=``)."""
    if not target:
        return False
    target = target.strip()
    if target.startswith("//") or "\\" in target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ("http", "https") and redirect_url.netloc == host_url.netloc


def safe_next_url(default: str) -> str:
    """The ``next`` query/form value if it is a safe local URL, else ``default``."""
    target = request.values.get("next")
    return target if is_safe_redirect_url(target) else default
