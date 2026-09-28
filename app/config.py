"""Application configuration.

Every tunable of PawBalik lives here. Values come from environment variables
(loaded from ``.env`` by python-dotenv) with safe defaults for local
development. ``.env.example`` lists every key.

Three config classes exist:

* ``DevelopmentConfig`` - default (MySQL/MariaDB via XAMPP, debug on).
* ``TestingConfig``     - pytest: SQLite in-memory, jobs run inline, no network.
* ``ProductionConfig``  - secure cookies; refuses to start without a real SECRET_KEY.

Pick one with ``create_app("development"|"testing"|"production")`` or the
``PAWBALIK_CONFIG`` environment variable.

Note: ``.env`` may use ``KEY = value`` (spaces around ``=``); python-dotenv
parses that correctly (checked in tests/test_utils.py).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.pool import StaticPool

BASE_DIR = Path(__file__).resolve().parent.parent  # the project root (PawBalik/)
load_dotenv(BASE_DIR / ".env")  # never overrides variables already set in the environment

DEV_SECRET_KEY = "dev-insecure-change-me"


def env_str(name: str, default: str | None = None) -> str | None:
    """Read a string env var; empty strings count as "not set"."""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = env_str(name)
    return int(value) if value is not None else default


def env_float(name: str, default: float) -> float:
    value = env_str(name)
    return float(value) if value is not None else default


class Config:
    """Base configuration shared by every environment."""

    ENV_NAME = "base"

    # --- Core ---------------------------------------------------------------------
    SECRET_KEY = env_str("SECRET_KEY", DEV_SECRET_KEY)
    APP_BASE_URL = env_str("APP_BASE_URL", "http://localhost:5000")  # used in SMS/push links
    APP_NAME = "PawBalik"
    APP_TAGLINE = "Tacloban City's Lost & Found Pet Platform"
    # All datetimes are stored as *naive UTC* (see app/utils/time.py) and shown in this zone.
    APP_TIMEZONE = env_str("APP_TIMEZONE", "Asia/Manila")
    RUN_HOST = env_str("HOST", "127.0.0.1")  # `python run.py` dev server
    RUN_PORT = env_int("PORT", 5000)

    ADMIN_EMAIL = env_str("ADMIN_EMAIL", "admin@pawbalik.local")
    ADMIN_PASSWORD = env_str("ADMIN_PASSWORD")  # used by `python run.py seed`

    # --- Database -------------------------------------------------------------------
    # XAMPP default: user root, empty password. The database must use utf8mb4.
    SQLALCHEMY_DATABASE_URI = env_str(
        "DATABASE_URL", "mysql+pymysql://root:@localhost/pawbalik?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {"pool_pre_ping": True, "pool_recycle": 280}

    # --- Sessions & cookies ------------------------------------------------------------
    SESSION_COOKIE_NAME = "pawbalik_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = False
    REMEMBER_COOKIE_DURATION = 60 * 60 * 24 * 30  # 30 days
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 7

    # --- CSRF (Flask-WTF) ------------------------------------------------------------
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8  # 8 hours: long forms on slow phones
    WTF_CSRF_HEADERS = ["X-CSRFToken", "X-CSRF-Token"]

    # --- Rate limiting (Flask-Limiter; strings use the `limits` syntax) ------------------
    RATELIMIT_ENABLED = env_bool("RATELIMIT_ENABLED", True)
    RATELIMIT_STORAGE_URI = env_str("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATE_LIMIT_LOGIN = env_str("RATE_LIMIT_LOGIN", "10 per minute;50 per hour")
    RATE_LIMIT_REGISTER = env_str("RATE_LIMIT_REGISTER", "10 per hour")
    RATE_LIMIT_SCANS = env_str("RATE_LIMIT_SCANS", "20 per hour")  # SPEC 10: 20/hour/user
    RATE_LIMIT_REPORTS = env_str("RATE_LIMIT_REPORTS", "10 per hour")
    RATE_LIMIT_AVATAR = env_str("RATE_LIMIT_AVATAR", "20 per hour")
    RATE_LIMIT_SIGHTINGS = env_str("RATE_LIMIT_SIGHTINGS", "20 per hour")
    RATE_LIMIT_MESSAGES = env_str("RATE_LIMIT_MESSAGES", "30 per minute;300 per hour")
    RATE_LIMIT_OTP = env_str("RATE_LIMIT_OTP", "5 per hour")
    RATE_LIMIT_RESCUE_REQUESTS = env_str("RATE_LIMIT_RESCUE_REQUESTS", "20 per hour")
    RATE_LIMIT_ABUSE_REPORTS = env_str("RATE_LIMIT_ABUSE_REPORTS", "10 per hour")

    # --- Service area: Tacloban City bounding box (SPEC 3) ------------------------------------
    # A rectangle around all 138 barangays of Tacloban City: from the Palo boundary
    # in the south (~11.17 N) to the northern barangays towards Babatngon (~11.37 N),
    # and from the western uplands (~124.90 E) to San Juanico Strait (~125.05 E),
    # with a small margin. A rectangle cannot follow the real city limits, so it
    # also touches small parts of Palo, Babatngon and Santa Rita (Samar).
    SERVICE_AREA_NAME = "Tacloban City"
    SERVICE_AREA_BOUNDS = {
        "south": env_float("SERVICE_AREA_SOUTH", 11.15),
        "west": env_float("SERVICE_AREA_WEST", 124.88),
        "north": env_float("SERVICE_AREA_NORTH", 11.40),
        "east": env_float("SERVICE_AREA_EAST", 125.08),
    }
    SERVICE_AREA_CENTER = {"lat": 11.2437, "lng": 125.0046}  # Tacloban City Hall area
    MAP_DEFAULT_ZOOM = 13
    # "reject" = validation error; "warn" = accept, the route shows a warning.
    OUT_OF_AREA_POLICY = env_str("OUT_OF_AREA_POLICY", "reject")

    # --- Map picker (SPEC 17.1): Leaflet + OpenStreetMap tiles --------------------------------
    MAP_PICKER = env_str("MAP_PICKER", "leaflet")  # leaflet | none
    LEAFLET_CSS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    LEAFLET_JS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    # Subresource Integrity hashes (verified against unpkg, same as leafletjs.com/download).
    LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
    LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
    OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    OSM_ATTRIBUTION = "&copy; OpenStreetMap contributors"

    # --- Uploads & storage (SPEC 8, 17.3) ---------------------------------------------------
    STORAGE_BACKEND = env_str("STORAGE_BACKEND", "auto")  # auto | cloudinary | local
    CLOUDINARY_URL = env_str("CLOUDINARY_URL")  # cloudinary://KEY:SECRET@CLOUD_NAME
    CLOUDINARY_FOLDER_ROOT = env_str("CLOUDINARY_FOLDER_ROOT", "pawbalik")
    # Local fallback storage: <project root>/uploads/pets/<lost_reports|sightings|scans|messages>/...
    UPLOAD_FOLDER = env_str("UPLOAD_FOLDER", str(BASE_DIR / "uploads" / "pets"))
    MAX_UPLOAD_BYTES = env_int("MAX_UPLOAD_BYTES", 8 * 1024 * 1024)  # per image (SPEC 8)
    MAX_PHOTOS_PER_REPORT = 5
    MAX_PHOTOS_PER_SIGHTING = 5
    ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "HEIF")  # checked with Pillow, not extension
    # Whole request: 5 photos x 8 MB + form fields.
    MAX_CONTENT_LENGTH = env_int("MAX_CONTENT_LENGTH", 48 * 1024 * 1024)
    AI_IMAGE_MAX_EDGE = 1024  # Gemini input (c_limit,w_1024)
    THUMB_SIZE = 400  # c_fill,w_400,h_400
    CLIENT_COMPRESS_MAX_EDGE = 1600  # compressImage() in static/js/main.js
    SCAN_PHOTO_RETENTION_DAYS = env_int("SCAN_PHOTO_RETENTION_DAYS", 30)

    # --- Gemini (SPEC 6) ----------------------------------------------------------------
    GEMINI_API_KEY = env_str("GEMINI_API_KEY")
    # A current Flash-tier multimodal model; the engine team verifies availability.
    GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-2.5-flash")
    GEMINI_TEMPERATURE = env_float("GEMINI_TEMPERATURE", 0.1)  # SPEC 6.5: 0-0.2
    GEMINI_TIMEOUT_SECONDS = env_int("GEMINI_TIMEOUT_SECONDS", 30)
    GEMINI_MAX_ATTEMPTS = env_int("GEMINI_MAX_ATTEMPTS", 3)
    GEMINI_MAX_WORKERS = env_int("GEMINI_MAX_WORKERS", 4)

    # --- Matching (SPEC 5.3, 6.4 - 6.6) -------------------------------------------------------
    MATCH_WEIGHTS = {
        "visual": env_float("MATCH_WEIGHT_VISUAL", 0.70),
        "attribute": env_float("MATCH_WEIGHT_ATTRIBUTE", 0.15),
        "distance": env_float("MATCH_WEIGHT_DISTANCE", 0.10),
        "recency": env_float("MATCH_WEIGHT_RECENCY", 0.05),
    }
    MATCH_RADIUS_KM = env_float("MATCH_RADIUS_KM", 15.0)
    MATCH_DATE_WINDOW_DAYS = env_int("MATCH_DATE_WINDOW_DAYS", 60)
    MATCH_TOP_N = env_int("MATCH_TOP_N", 15)  # candidates sent to visual comparison
    # Display tiers: (tier, minimum final score inclusive, label), highest first.
    MATCH_TIERS = [
        ("high", 80, "High visual similarity"),
        ("moderate", 60, "Moderate visual similarity"),
        ("low", 40, "Low visual similarity"),
    ]
    # Below the lowest tier: tier "hidden", shown only with "Show weaker results".
    MATCH_HIDDEN_LABEL = "Weak visual similarity"
    MATCH_RELEVANCE_THRESHOLD = env_int(
        "MATCH_RELEVANCE_THRESHOLD", 40
    )  # "no relevant match" below
    MATCH_NOTIFY_THRESHOLD = env_int("MATCH_NOTIFY_THRESHOLD", 75)  # SPEC 9

    # --- Sightings (SPEC 5.4) -------------------------------------------------------------
    SIGHTING_EXPIRY_DAYS = env_int("SIGHTING_EXPIRY_DAYS", 30)

    # --- Background jobs (SPEC 6.7) --------------------------------------------------------
    JOBS_EAGER = env_bool("JOBS_EAGER", False)  # True = run jobs inline (no worker needed)
    JOBS_EAGER_RAISE = env_bool("JOBS_EAGER_RAISE", False)  # re-raise handler errors inline
    JOBS_MAX_ATTEMPTS = env_int("JOBS_MAX_ATTEMPTS", 3)
    JOBS_BACKOFF_SECONDS = env_int("JOBS_BACKOFF_SECONDS", 30)  # 30 s, 60 s, 120 s ...
    JOBS_LOCK_TIMEOUT_SECONDS = env_int("JOBS_LOCK_TIMEOUT_SECONDS", 600)  # stuck "running" jobs
    WORKER_POLL_SECONDS = env_float("WORKER_POLL_SECONDS", 3.0)
    # Periodic jobs the worker schedules itself: job type -> interval in seconds.
    PERIODIC_JOBS = {"expire_sightings": 60 * 60, "cleanup_scan_photos": 24 * 60 * 60}

    # --- Notifications (SPEC 9) ---------------------------------------------------------------
    SMS_PROVIDER = env_str("SMS_PROVIDER", "console")  # console | bulksms | memory (tests)
    SMS_DAILY_MATCH_LIMIT = env_int("SMS_DAILY_MATCH_LIMIT", 5)  # match SMS per user per day
    SMS_MAX_CHARS = 160
    BULKSMS_API_URL = env_str("BULKSMS_API_URL")  # TODO(verify) with bulksms.com.ph docs
    BULKSMS_API_KEY = env_str("BULKSMS_API_KEY")
    BULKSMS_SENDER_NAME = env_str("BULKSMS_SENDER_NAME", "PawBalik")
    BULKSMS_TIMEOUT_SECONDS = env_int("BULKSMS_TIMEOUT_SECONDS", 15)

    OTP_LENGTH = 6
    OTP_TTL_MINUTES = env_int("OTP_TTL_MINUTES", 10)
    OTP_MAX_ATTEMPTS = env_int("OTP_MAX_ATTEMPTS", 5)
    OTP_RESEND_COOLDOWN_SECONDS = env_int("OTP_RESEND_COOLDOWN_SECONDS", 60)

    # fcm disables itself (with a log line) when Firebase is not configured.
    PUSH_PROVIDER = env_str("PUSH_PROVIDER", "fcm")  # fcm | memory (tests)
    FIREBASE_CREDENTIALS_PATH = env_str("FIREBASE_CREDENTIALS_PATH")  # service-account JSON
    # Public Firebase web-app config (safe to expose to the browser).
    FIREBASE_WEB_CONFIG = {
        "apiKey": env_str("FIREBASE_WEB_API_KEY"),
        "authDomain": env_str("FIREBASE_AUTH_DOMAIN"),
        "projectId": env_str("FIREBASE_PROJECT_ID"),
        "storageBucket": env_str("FIREBASE_STORAGE_BUCKET"),
        "messagingSenderId": env_str("FIREBASE_MESSAGING_SENDER_ID"),
        "appId": env_str("FIREBASE_APP_ID"),
    }
    FIREBASE_VAPID_KEY = env_str("FIREBASE_VAPID_KEY")
    FIREBASE_JS_VERSION = env_str("FIREBASE_JS_VERSION", "10.14.1")  # gstatic.com/firebasejs/<v>/

    # --- Messaging (SPEC 5.6) ---------------------------------------------------------------
    MESSAGE_POLL_SECONDS = env_int("MESSAGE_POLL_SECONDS", 4)
    MESSAGE_MAX_LENGTH = 2000

    @classmethod
    def init_app(cls, app) -> None:  # noqa: ANN001 - Flask app
        """Hook for environment-specific checks at startup."""


class DevelopmentConfig(Config):
    ENV_NAME = "development"
    DEBUG = True
    TEMPLATES_AUTO_RELOAD = True


class TestingConfig(Config):
    ENV_NAME = "testing"
    TESTING = True
    DEBUG = False
    SECRET_KEY = "testing-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory; one fresh database per app
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "poolclass": StaticPool,  # every session shares the single in-memory connection
        "connect_args": {"check_same_thread": False},
    }
    WTF_CSRF_ENABLED = False  # the `csrf_client` fixture / @pytest.mark.config turn it on
    RATELIMIT_ENABLED = False  # opt in with @pytest.mark.config(RATELIMIT_ENABLED=True)
    JOBS_EAGER = True
    JOBS_EAGER_RAISE = True
    STORAGE_BACKEND = "local"
    UPLOAD_FOLDER = str(Path(tempfile.gettempdir()) / "pawbalik-test-uploads")  # conftest: tmp_path
    CLOUDINARY_URL = None
    GEMINI_API_KEY = None  # tests use the fake_gemini fixture; never the real API
    SMS_PROVIDER = "memory"
    PUSH_PROVIDER = "memory"
    FIREBASE_CREDENTIALS_PATH = None
    APP_BASE_URL = "http://localhost"
    OUT_OF_AREA_POLICY = "reject"


class ProductionConfig(Config):
    ENV_NAME = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"

    @classmethod
    def init_app(cls, app) -> None:  # noqa: ANN001
        secret = app.config.get("SECRET_KEY")
        if not secret or secret == DEV_SECRET_KEY or len(secret) < 32:
            raise RuntimeError(
                "SECRET_KEY must be set to a long random value in production "
                '(python -c "import secrets; print(secrets.token_hex(32))").'
            )


CONFIGS: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    """Return the config class for ``name`` (default: env PAWBALIK_CONFIG or development)."""
    name = name or env_str("PAWBALIK_CONFIG") or "development"
    try:
        return CONFIGS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown config {name!r}; choose one of {sorted(CONFIGS)}") from exc
