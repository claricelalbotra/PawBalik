"""Security headers and the Content-Security-Policy - defined ONLY here.

Allowed third-party origins (and why):

* ``https://fonts.googleapis.com`` (styles) / ``https://fonts.gstatic.com``
  (font files): Inter + Pacifico from Google Fonts.
* ``https://unpkg.com``: Leaflet 1.9.4 JS/CSS (SRI-pinned in config) and
  Leaflet's default marker images.
* ``https://tile.openstreetmap.org``: OSM map tiles for the pin picker.
* ``https://res.cloudinary.com``: photos stored on Cloudinary.
* ``https://www.gstatic.com``: Firebase JS SDK (page + service worker
  ``importScripts``).
* ``https://fcmregistrations.googleapis.com``,
  ``https://firebaseinstallations.googleapis.com``,
  ``https://fcm.googleapis.com``: calls the Firebase Messaging SDK makes to
  obtain/refresh a push token.

Inline ``<script>`` is blocked; a per-request nonce is available as
``{{ csp_nonce() }}`` for the rare case a template truly needs one. Put
data for JS in ``<script type="application/json">`` blocks (not executed,
so no nonce needed) or ``data-`` attributes. Inline *styles* are allowed
(``style=""`` attributes are convenient and low-risk).
"""

from __future__ import annotations

import secrets

from flask import Flask, Response, g

FONT_STYLE_ORIGINS = ["https://fonts.googleapis.com"]
FONT_FILE_ORIGINS = ["https://fonts.gstatic.com"]
LEAFLET_ORIGINS = ["https://unpkg.com"]
TILE_ORIGINS = ["https://tile.openstreetmap.org"]
CLOUDINARY_ORIGINS = ["https://res.cloudinary.com"]
FIREBASE_SCRIPT_ORIGINS = ["https://www.gstatic.com"]
FIREBASE_CONNECT_ORIGINS = [
    "https://fcmregistrations.googleapis.com",
    "https://firebaseinstallations.googleapis.com",
    "https://fcm.googleapis.com",
]


def csp_nonce() -> str:
    """Per-request nonce for ``<script nonce="...">``."""
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


def build_csp(nonce: str) -> str:
    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'", f"'nonce-{nonce}'", *LEAFLET_ORIGINS, *FIREBASE_SCRIPT_ORIGINS],
        "style-src": ["'self'", "'unsafe-inline'", *FONT_STYLE_ORIGINS, *LEAFLET_ORIGINS],
        "font-src": ["'self'", *FONT_FILE_ORIGINS],
        "img-src": [
            "'self'",
            "data:",
            "blob:",
            *TILE_ORIGINS,
            *CLOUDINARY_ORIGINS,
            *LEAFLET_ORIGINS,
        ],
        "media-src": ["'self'", "blob:"],
        "connect-src": ["'self'", *FIREBASE_CONNECT_ORIGINS],
        "worker-src": ["'self'"],
        "manifest-src": ["'self'"],
        "frame-src": ["'none'"],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


def apply_security_headers(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = build_csp(csp_nonce())
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(self), geolocation=(self), microphone=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    return response


def init_security(app: Flask) -> None:
    app.after_request(apply_security_headers)
    app.jinja_env.globals["csp_nonce"] = csp_nonce
    if app.config.get("SESSION_COOKIE_SECURE"):

        @app.after_request
        def _hsts(response: Response) -> Response:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response
