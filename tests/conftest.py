"""Shared pytest fixtures for every PawBalik team.

Each test gets a brand-new app with its own in-memory SQLite database (tables
created from the models), local photo storage in a temp dir, jobs running
inline (JOBS_EAGER), CSRF and rate limiting OFF, SMS/push going to in-memory
outboxes and Gemini never called for real.

Fixtures
--------
app, client, db_session           app + test client; ``db_session`` is ``db.session``
csrf_client                       a client of the same app with CSRF ENABLED (fetch the
                                  token with ``get_csrf_token(client)``)
make_user(**fields) / make_admin  create users (password: DEFAULT_PASSWORD)
login(client, user)               log a user in (session-based; works with CSRF on)
make_photo(owner_type, owner_id, ...)          Photo row + real small JPEG on disk
make_lost_report(owner=None, photos=1, ...)     LostReport with case number + history
make_sighting(finder=None, photos=1, ...)       active Sighting inside Tacloban
make_scan_result(user, candidate, ...)          Scan + ScanResult (e.g. to grant sighting access)
jpeg_bytes(color, size) / upload_file(...)       generated images for uploads
fake_gemini                       deterministic GeminiService replacement (configurable)
sms_outbox / push_outbox          lists the memory providers append to
run_jobs()                        run all due jobs (for tests that turn JOBS_EAGER off)

Per-test config: ``@pytest.mark.config(RATELIMIT_ENABLED=True, JOBS_EAGER=False)``.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from PIL import Image

from app import create_app
from app.extensions import db
from app.models import (
    CandidateType,
    LostReport,
    Photo,
    PhotoOwner,
    Scan,
    ScanMode,
    ScanResult,
    ScanStatus,
    ScanTrigger,
    Sighting,
    StorageKind,
    User,
    UserRole,
)
from app.utils.time import utcnow

DEFAULT_PASSWORD = "Tacloban2026"
TACLOBAN = (11.2437, 125.0046)  # city hall area, inside the service area


# --- App / client ---------------------------------------------------------------------------


@pytest.fixture
def app(request: pytest.FixtureRequest, tmp_path) -> Iterator:
    overrides: dict[str, Any] = {"UPLOAD_FOLDER": str(tmp_path / "uploads")}
    for marker in reversed(list(request.node.iter_markers("config"))):
        overrides.update(marker.kwargs)
    app = create_app("testing", **overrides)

    @app.teardown_request
    def _reset_request_globals(exc: BaseException | None = None) -> None:
        # The test keeps one app context open, so requests share `g`. Clear it after each
        # request, otherwise Flask-Login's cached user leaks into the next request.
        from flask import g

        for name in list(g):
            g.pop(name, None)

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    return db.session


@pytest.fixture
def csrf_client(app):
    """A client for the same app but with CSRF protection switched on."""
    app.config["WTF_CSRF_ENABLED"] = True
    yield app.test_client()
    app.config["WTF_CSRF_ENABLED"] = False


_CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def get_csrf_token(client, path: str = "/") -> str:
    """Load a page and read the token from <meta name="csrf-token">."""
    html = client.get(path).get_data(as_text=True)
    match = _CSRF_META.search(html)
    assert match, "csrf meta tag not found"
    return match.group(1)


@pytest.fixture
def csrf_token() -> Callable[..., str]:
    return get_csrf_token


# --- Users ------------------------------------------------------------------------------------


@pytest.fixture
def make_user(app) -> Callable[..., User]:
    counter = {"n": 0}

    def _make(**fields: Any) -> User:
        counter["n"] += 1
        n = counter["n"]
        password = fields.pop("password", DEFAULT_PASSWORD)
        defaults = {
            "full_name": f"Test User {n}",
            "email": f"user{n}@example.com",
            "mobile_e164": f"+63917{n:07d}",
            "role": UserRole.USER,
            "is_active": True,
            "privacy_consented_at": utcnow(),
        }
        defaults.update(fields)
        user = User(**defaults)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    return _make


@pytest.fixture
def make_admin(make_user) -> Callable[..., User]:
    def _make(**fields: Any) -> User:
        fields.setdefault("full_name", "Admin User")
        fields.setdefault("role", UserRole.ADMIN)
        return make_user(**fields)

    return _make


@pytest.fixture
def user(make_user) -> User:
    return make_user()


@pytest.fixture
def admin(make_admin) -> User:
    return make_admin(email="admin@example.com")


def _login(client, user: User) -> None:
    """Log ``user`` in by writing the Flask-Login session (fast; independent of CSRF)."""
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


@pytest.fixture
def login() -> Callable[[Any, User], None]:
    return _login


# --- Images & photos --------------------------------------------------------------------------


def jpeg_bytes(
    color: tuple[int, int, int] = (200, 140, 80), size: tuple[int, int] = (64, 48)
) -> bytes:
    """A small real JPEG (Pillow)."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def upload_file(name: str = "pet.jpg", color: tuple[int, int, int] = (200, 140, 80), size=(64, 48)):
    """A (file, filename) tuple for ``client.post(data={"photos": upload_file()})``."""
    return (io.BytesIO(jpeg_bytes(color, size)), name)


@pytest.fixture
def make_photo(app) -> Callable[..., Photo]:
    """Create a local Photo row and its JPEG file (same key layout as storage_service)."""
    import hashlib
    import secrets
    from pathlib import Path

    from app.services.storage_service import default_folder

    def _make(
        owner_type: str = PhotoOwner.LOST_REPORT,
        owner_id: int | None = None,
        *,
        color: tuple[int, int, int] = (200, 140, 80),
        is_primary: bool = True,
        sort_order: int = 0,
        ai_attributes: dict | None = None,
        commit: bool = True,
    ) -> Photo:
        data = jpeg_bytes(color)
        key = f"{default_folder(owner_type, owner_id)}/{secrets.token_hex(16)}.jpg"
        path = Path(app.config["UPLOAD_FOLDER"]) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        photo = Photo(
            owner_type=owner_type,
            owner_id=owner_id,
            storage=StorageKind.LOCAL,
            public_id=key,
            width=64,
            height=48,
            sha256=hashlib.sha256(data).hexdigest(),
            is_primary=is_primary,
            sort_order=sort_order,
            ai_attributes=ai_attributes,
        )
        db.session.add(photo)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return photo

    return _make


# --- Reports, sightings, scans ------------------------------------------------------------------


@pytest.fixture
def make_lost_report(app, make_user, make_photo) -> Callable[..., LostReport]:
    from app.utils.case_numbers import next_case_number

    def _make(owner: User | None = None, *, photos: int = 1, **fields: Any) -> LostReport:
        owner = owner or make_user()
        defaults = {
            "pet_name": "Bantay",
            "species": "dog",
            "breed": "Aspin (mixed)",
            "colors": "Brown and white",
            "size": "medium",
            "sex": "male",
            "last_seen_lat": TACLOBAN[0],
            "last_seen_lng": TACLOBAN[1],
            "last_seen_place": "Brgy. San Jose",
            "last_seen_at": utcnow() - timedelta(days=2),
            "contact_name": owner.full_name,
            "contact_email": owner.email,
            "contact_phone": owner.mobile_e164,
            "show_phone": False,
            "status": "lost",
        }
        defaults.update(fields)
        report = LostReport(owner_id=owner.id, case_number=next_case_number(), **defaults)
        db.session.add(report)
        db.session.flush()
        report.record_created(actor=owner)
        for i in range(photos):
            make_photo(
                PhotoOwner.LOST_REPORT, report.id, is_primary=(i == 0), sort_order=i, commit=False
            )
        db.session.commit()
        return report

    return _make


@pytest.fixture
def make_sighting(app, make_user, make_photo) -> Callable[..., Sighting]:
    def _make(finder: User | None = None, *, photos: int = 1, **fields: Any) -> Sighting:
        finder = finder or make_user()
        defaults = {
            "species": "dog",
            "breed": "Aspin",
            "colors": "Brown",
            "size": "medium",
            "condition": "healthy",
            "is_holding_animal": False,
            "seen_lat": TACLOBAN[0] + 0.005,
            "seen_lng": TACLOBAN[1] + 0.004,
            "seen_place": "Near Robinsons Place",
            "seen_at": utcnow() - timedelta(hours=5),
            "contact_name": finder.full_name,
            "contact_email": finder.email,
            "contact_phone": finder.mobile_e164,
            "contact_pref": "in_app",
            "status": "active",
        }
        defaults.update(fields)
        sighting = Sighting(finder_id=finder.id, **defaults)
        db.session.add(sighting)
        db.session.flush()
        sighting.set_default_expiry(app.config["SIGHTING_EXPIRY_DAYS"])
        for i in range(photos):
            make_photo(
                PhotoOwner.SIGHTING, sighting.id, is_primary=(i == 0), sort_order=i, commit=False
            )
        db.session.commit()
        return sighting

    return _make


@pytest.fixture
def make_scan_result(app) -> Callable[..., ScanResult]:
    """A completed Scan owned by ``user`` with one result pointing at ``candidate``."""

    def _make(
        user: User,
        candidate: LostReport | Sighting,
        *,
        final_score: float = 82.0,
        trigger: str = ScanTrigger.MANUAL,
    ) -> ScanResult:
        is_sighting = isinstance(candidate, Sighting)
        scan = Scan(
            user_id=user.id,
            mode=ScanMode.LOST if is_sighting else ScanMode.FOUND,
            trigger=trigger,
            status=ScanStatus.COMPLETE,
            result_count=1,
            gemini_ok=True,
        )
        db.session.add(scan)
        db.session.flush()
        result = ScanResult(
            scan_id=scan.id,
            candidate_type=CandidateType.SIGHTING if is_sighting else CandidateType.LOST_REPORT,
            candidate_id=candidate.id,
            candidate_photo_id=candidate.primary_photo.id if candidate.primary_photo else None,
            visual_score=final_score,
            attribute_score=70,
            distance_km=0.6,
            distance_score=96,
            recency_score=90,
            final_score=final_score,
            tier="high" if final_score >= 80 else "moderate",
            rank=1,
        )
        db.session.add(result)
        db.session.commit()
        return result

    return _make


# --- Fakes for external services ---------------------------------------------------------------


class FakeGemini:
    """Deterministic stand-in for GeminiService. Change the attributes in a test:

    fake_gemini.precheck_result = {...}            # SPEC 6.2 dict
    fake_gemini.attributes_result = {...}          # SPEC 6.3 dict
    fake_gemini.compare_score = 85                 # visual_similarity for every compare()
    fake_gemini.compare_fn = lambda a, b: {...}    # full control (overrides compare_score)
    fake_gemini.fail = {"compare"}                 # these methods raise GeminiError
    fake_gemini.configured = False                 # is_configured() result
    fake_gemini.calls -> [("precheck", nbytes), ("compare", nbytes_a, nbytes_b), ...]
    """

    def __init__(self) -> None:
        self.configured = True
        self.fail: set[str] = set()
        self.calls: list[tuple] = []
        self.compare_score = 85
        self.compare_fn: Callable[[bytes, bytes], dict] | None = None
        self.precheck_result = {
            "contains_animal": True,
            "species": "dog",
            "animal_count": 1,
            "image_quality": "good",
            "quality_issues": [],
            "is_real_photo": True,
        }
        self.attributes_result = {
            "species": "dog",
            "breed_guess": "Aspin (mixed)",
            "primary_colors": ["brown", "white"],
            "coat_pattern": "bicolor",
            "coat_length": "short",
            "size_estimate": "medium",
            "distinctive_marks": ["white blaze on chest"],
            "accessories": ["red collar"],
        }

    def _maybe_fail(self, name: str) -> None:
        from app.services.gemini_service import GeminiError

        if name in self.fail:
            raise GeminiError(f"fake {name} failure")

    def precheck(self, image_bytes: bytes) -> dict:
        self.calls.append(("precheck", len(image_bytes)))
        self._maybe_fail("precheck")
        return dict(self.precheck_result)

    def extract_attributes(self, image_bytes: bytes) -> dict:
        self.calls.append(("extract_attributes", len(image_bytes)))
        self._maybe_fail("extract_attributes")
        return dict(self.attributes_result)

    def compare(self, image_a: bytes, image_b: bytes) -> dict:
        self.calls.append(("compare", len(image_a), len(image_b)))
        self._maybe_fail("compare")
        if self.compare_fn is not None:
            return self.compare_fn(image_a, image_b)
        return {
            "visual_similarity": self.compare_score,
            "matching_features": ["similar coat colour", "same ear shape"],
            "differences": ["different collar"],
            "confidence": "medium",
            "comparison_possible": True,
        }


@pytest.fixture
def fake_gemini(monkeypatch) -> FakeGemini:
    """Replace GeminiService's API methods on the class (every instance uses the fake)."""
    from app.services import gemini_service

    fake = FakeGemini()
    monkeypatch.setattr(gemini_service.GeminiService, "is_configured", lambda self: fake.configured)
    monkeypatch.setattr(gemini_service.GeminiService, "precheck", lambda self, b: fake.precheck(b))
    monkeypatch.setattr(
        gemini_service.GeminiService,
        "extract_attributes",
        lambda self, b: fake.extract_attributes(b),
    )
    monkeypatch.setattr(
        gemini_service.GeminiService, "compare", lambda self, a, b: fake.compare(a, b)
    )
    return fake


@pytest.fixture
def sms_outbox(app) -> list[dict]:
    """Messages sent with SMS_PROVIDER=memory: [{"to": "+639...", "message": "..."}]."""
    outbox = app.extensions.setdefault("sms_outbox", [])
    outbox.clear()
    return outbox


@pytest.fixture
def push_outbox(app) -> list[dict]:
    """Pushes sent with PUSH_PROVIDER=memory (the notifications team appends dicts here)."""
    outbox = app.extensions.setdefault("push_outbox", [])
    outbox.clear()
    return outbox


@pytest.fixture
def run_jobs(app) -> Callable[[], int]:
    """Run every due job now (use with @pytest.mark.config(JOBS_EAGER=False))."""
    from app.services.job_service import run_pending

    def _run() -> int:
        total = 0
        while True:
            ran = run_pending(limit=50)
            total += ran
            if ran == 0:
                return total

    return _run


@pytest.fixture
def jpeg() -> Callable[..., bytes]:
    """Fixture form of jpeg_bytes(color=(r, g, b), size=(w, h))."""
    return jpeg_bytes


@pytest.fixture
def upload() -> Callable[..., tuple]:
    """Fixture form of upload_file(name="pet.jpg", color=..., size=...)."""
    return upload_file
