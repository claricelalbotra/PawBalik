"""Photo storage: Cloudinary, or local files under uploads/pets/ (SPEC 8, 17.3).

OWNER: engine team. Public names and signatures are FINAL (SPEC 18).

Every upload goes through :func:`save_image`, which

1. reads at most ``MAX_UPLOAD_BYTES`` (8 MB) and checks the *real* format with
   Pillow (JPEG, PNG, WebP; HEIC/HEIF when ``pillow_heif`` is installed) - never
   the file name;
2. refuses decompression bombs (more than ``MAX_IMAGE_PIXELS`` pixels);
3. hashes the ORIGINAL bytes (SHA-256, stored on the Photo);
4. applies the EXIF orientation, then re-encodes to a clean JPEG with **no
   EXIF/GPS metadata** (long edge at most ``STORED_MAX_EDGE``);
5. stores it on the configured backend and adds a :class:`Photo` row (flush, no commit).

Backends (``STORAGE_BACKEND``: auto | cloudinary | local; auto = Cloudinary when
``CLOUDINARY_URL`` is set, otherwise local):

* **Cloudinary** - official SDK, signed uploads (credentials from
  ``CLOUDINARY_URL=cloudinary://KEY:SECRET@CLOUD_NAME``, passed per call; the
  secret never reaches the browser). Folders ``pawbalik/{env}/lost_reports/{id}/``,
  ``.../sightings/{id}/``, ``.../scans/{yyyy-mm}/``, ``.../messages/{conversation_id}/``.
  Thumbnails ``c_fill,w_400,h_400``; Gemini input ``c_limit,w_1024,q_auto,f_jpg``.
  If an upload fails, the photo is stored locally instead (logged) so the user's
  report is not lost.
* **Local** - ``<UPLOAD_FOLDER>/<folder>/<32 random hex>.jpg`` (git-ignored),
  served by ``main.media`` after ``policy_service.can_view_photo``.
  ``Photo.public_id`` holds that relative key. Thumbnails are made on first request
  and cached next to the photo as ``<name>.thumb.jpg``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
import sqlalchemy as sa
from flask import current_app, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from app.extensions import db
from app.models import Photo, PhotoOwner, StorageKind
from app.utils.time import utcnow

try:  # optional HEIC/HEIF (iPhone) support
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover
    HEIF_SUPPORTED = False

log = logging.getLogger(__name__)

FOLDER_BY_OWNER = {
    PhotoOwner.LOST_REPORT: "lost_reports",
    PhotoOwner.SIGHTING: "sightings",
    PhotoOwner.SCAN: "scans",
    PhotoOwner.MESSAGE: "messages",
    PhotoOwner.AVATAR: "avatars",
}
JPEG_QUALITY = 88
STORED_MAX_EDGE = 2048  # long edge of the stored copy (phones already send ~1600 px)
MAX_IMAGE_PIXELS = 50_000_000  # decompression-bomb guard (a 48 MP phone photo still fits)
THUMB_SUFFIX = ".thumb.jpg"
CLOUDINARY_TIMEOUT_SECONDS = 30
ENV_FOLDER = {"development": "dev", "production": "prod", "testing": "test"}

# Cloudinary transformations (SPEC 8).
THUMB_TRANSFORM = {"crop": "fill", "width": 400, "height": 400, "quality": "auto"}
AI_TRANSFORM = {"crop": "limit", "width": 1024, "quality": "auto", "fetch_format": "jpg"}


class ImageValidationError(ValueError):
    """The upload is not an acceptable image. ``message`` is safe to show to users."""

    def __init__(self, message: str, code: str = "invalid_image"):
        super().__init__(message)
        self.message = message
        self.code = code


class StorageError(RuntimeError):
    """A stored photo could not be read (missing file, Cloudinary unreachable ...)."""


# --- Configuration helpers -----------------------------------------------------------------


def default_folder(owner_type: str, owner_id: int | None = None) -> str:
    """Folder for an owner (SPEC 8): ``lost_reports/12``, ``sightings/7``,
    ``scans/2026-09``, ``messages/<conversation_id>``."""
    base = FOLDER_BY_OWNER[owner_type]
    if owner_type == PhotoOwner.SCAN or owner_id is None:
        return f"{base}/{utcnow():%Y-%m}"
    return f"{base}/{owner_id}"


def cloudinary_credentials() -> dict[str, str] | None:
    """``{"cloud_name", "api_key", "api_secret"}`` parsed from CLOUDINARY_URL, or None."""
    url = current_app.config.get("CLOUDINARY_URL")
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "cloudinary" or not (
        parsed.hostname and parsed.username and parsed.password
    ):
        log.error("CLOUDINARY_URL must look like cloudinary://API_KEY:API_SECRET@CLOUD_NAME")
        return None
    return {
        "cloud_name": parsed.hostname,
        "api_key": unquote(parsed.username),
        "api_secret": unquote(parsed.password),
    }


def backend() -> str:
    """ "cloudinary" or "local" according to STORAGE_BACKEND (auto = Cloudinary if configured)."""
    choice = (current_app.config.get("STORAGE_BACKEND") or "auto").lower()
    configured = cloudinary_credentials() is not None
    if choice == StorageKind.LOCAL:
        return StorageKind.LOCAL
    if choice == StorageKind.CLOUDINARY and not configured:
        log.warning("STORAGE_BACKEND=cloudinary but CLOUDINARY_URL is not usable; using local")
    return StorageKind.CLOUDINARY if configured else StorageKind.LOCAL


def upload_root() -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"])


def _safe_folder(folder: str) -> str:
    """Relative folder with no empty, ``.`` or ``..`` parts (path-traversal safe)."""
    parts = [p for p in str(folder).replace("\\", "/").split("/") if p not in ("", ".", "..")]
    cleaned = [p for p in parts if ":" not in p]  # no drive letters on Windows
    if not cleaned:
        raise ValueError("folder must not be empty")
    return "/".join(cleaned)


def _cloudinary_folder(folder: str) -> str:
    root = current_app.config.get("CLOUDINARY_FOLDER_ROOT") or "pawbalik"
    env = current_app.config.get("ENV_NAME") or "development"
    return f"{root}/{ENV_FOLDER.get(env, env)}/{folder}"


# --- Validation and cleaning --------------------------------------------------------------


def _read_bytes(file: Any) -> bytes:
    """Accept a Werkzeug FileStorage, a file object, raw bytes or a path (max MAX_UPLOAD_BYTES)."""
    limit = int(current_app.config["MAX_UPLOAD_BYTES"])
    if file is None:
        raise ImageValidationError("Please choose a photo.", "empty_file")
    if isinstance(file, bytes | bytearray):
        data = bytes(file)
    elif isinstance(file, str | Path):
        path = Path(file)
        if not path.is_file():
            raise ImageValidationError("Please choose a photo.", "empty_file")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    else:
        stream = getattr(file, "stream", file)
        data = stream.read(limit + 1)
    if not data:
        raise ImageValidationError("Please choose a photo.", "empty_file")
    if len(data) > limit:
        mb = limit // (1024 * 1024)
        raise ImageValidationError(f"Photos must be {mb} MB or smaller.", "file_too_large")
    return data


def _allowed_formats() -> set[str]:
    allowed = {f.upper() for f in current_app.config["ALLOWED_IMAGE_FORMATS"]}
    if not HEIF_SUPPORTED:  # pragma: no cover - pillow_heif is in requirements.txt
        allowed.discard("HEIF")
    return allowed


def _decode(data: bytes) -> Image.Image:
    """Open and fully decode the image; check the real format (not the file name)."""
    too_big = ImageValidationError(
        "That photo has too many pixels. Please upload a smaller photo.", "too_many_pixels"
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                fmt = (probe.format or "").upper()
                width, height = probe.size
                if fmt in ("JPEG", "PNG", "WEBP"):
                    probe.verify()  # header/structure check (HEIF has no verify step)
            if fmt not in _allowed_formats():
                raise ImageValidationError(
                    "Please upload a JPEG, PNG or WebP photo.", "unsupported_format"
                )
            if width * height > MAX_IMAGE_PIXELS:
                raise too_big
            image = Image.open(io.BytesIO(data))
            if fmt == "JPEG":
                # Decode at reduced scale when possible: faster, less memory.
                image.draft("RGB", (STORED_MAX_EDGE, STORED_MAX_EDGE))
            image.load()
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise too_big from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
        raise ImageValidationError("That file is not a readable photo.", "invalid_image") from None
    return image


def _to_clean_jpeg(image: Image.Image, max_edge: int | None = None) -> tuple[bytes, int, int]:
    """Apply EXIF rotation, drop metadata (EXIF/GPS), convert to an RGB JPEG."""
    image = ImageOps.exif_transpose(image) or image
    if image.mode in ("RGBA", "LA", "P", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")
    if max_edge and max(image.size) > max_edge:
        image = image.copy()
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # No exif= / icc_profile= arguments: the new file carries no metadata at all.
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), image.width, image.height


def validate_image(file: Any) -> tuple[bytes, bytes, int, int]:
    """Validate an upload without storing it: ``(original_bytes, clean_jpeg, width, height)``."""
    data = _read_bytes(file)
    clean, width, height = _to_clean_jpeg(_decode(data), STORED_MAX_EDGE)
    return data, clean, width, height


def jpeg_for_ai(data: bytes) -> bytes:
    """Clean JPEG bytes with the long edge <= AI_IMAGE_MAX_EDGE (for Gemini)."""
    max_edge = int(current_app.config.get("AI_IMAGE_MAX_EDGE", 1024))
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            clean, _, _ = _to_clean_jpeg(image, max_edge)
    except OSError as exc:
        raise StorageError("Not a readable image") from exc
    return clean


# --- Saving -----------------------------------------------------------------------------


def _save_local(clean: bytes, folder: str) -> str:
    key = f"{folder}/{secrets.token_hex(16)}.jpg"
    path = upload_root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(clean)
    return key


def _save_cloudinary(clean: bytes, folder: str) -> dict[str, Any]:
    """Signed upload of the clean JPEG. Returns Cloudinary's response dict."""
    import cloudinary.uploader

    creds = cloudinary_credentials() or {}
    return cloudinary.uploader.upload(
        io.BytesIO(clean),
        folder=_cloudinary_folder(folder),
        public_id=secrets.token_hex(16),
        resource_type="image",
        overwrite=False,
        unique_filename=False,
        use_filename=False,
        timeout=CLOUDINARY_TIMEOUT_SECONDS,
        **creds,
    )


def save_image(
    file: Any,
    *,
    owner_type: str,
    owner_id: int | None = None,
    folder: str,
    is_primary: bool = False,
    sort_order: int = 0,
) -> Photo:
    """Validate, strip EXIF, hash, store; add a ``Photo`` to the session and flush.

    Does not commit. Raises :class:`ImageValidationError` with a friendly message.
    ``folder`` is relative, e.g. ``default_folder("lost_report", report.id)``.
    """
    if owner_type not in FOLDER_BY_OWNER:
        raise ValueError(f"Unknown owner_type {owner_type!r}")
    original, clean, width, height = validate_image(file)
    sha256 = hashlib.sha256(original).hexdigest()  # of the ORIGINAL bytes (SPEC 8)
    safe_folder = _safe_folder(folder)

    storage, public_id, secure_url = StorageKind.LOCAL, None, None
    if backend() == StorageKind.CLOUDINARY:
        try:
            result = _save_cloudinary(clean, safe_folder)
            storage = StorageKind.CLOUDINARY
            public_id = result["public_id"]
            secure_url = result["secure_url"]
            width = int(result.get("width") or width)
            height = int(result.get("height") or height)
        except Exception:  # noqa: BLE001 - network/API errors: keep the photo locally
            log.exception("Cloudinary upload failed; storing the photo locally instead")
    if storage == StorageKind.LOCAL:
        public_id = _save_local(clean, safe_folder)

    photo = Photo(
        owner_type=owner_type,
        owner_id=owner_id,
        storage=storage,
        public_id=public_id,
        secure_url=secure_url,
        width=width,
        height=height,
        sha256=sha256,
        is_primary=is_primary,
        sort_order=sort_order,
    )
    db.session.add(photo)
    db.session.flush()
    return photo


# --- Reading ------------------------------------------------------------------------------


def _resolve_local(key: str) -> Path | None:
    """Absolute path for a key if it stays inside UPLOAD_FOLDER (no traversal), else None."""
    if not key or "\x00" in key:
        return None
    root = upload_root().resolve()
    try:
        path = (root / key).resolve()
    except (OSError, ValueError):
        return None
    if root not in path.parents:
        return None
    return path


def _thumb_path(path: Path) -> Path:
    return path.with_name(path.stem + THUMB_SUFFIX)


def _make_thumbnail(source: Path, target: Path) -> bool:
    """Write a THUMB_SIZE square crop (like c_fill,w_400,h_400). True on success."""
    size = int(current_app.config.get("THUMB_SIZE", 400))
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image) or image
            thumb = ImageOps.fit(image.convert("RGB"), (size, size), Image.Resampling.LANCZOS)
            tmp = target.with_name(f"{target.name}.{secrets.token_hex(4)}.tmp")
            thumb.save(tmp, format="JPEG", quality=82, optimize=True)
        os.replace(tmp, target)  # atomic: two requests may race to create it
        return True
    except (OSError, ValueError):
        log.exception("Could not create thumbnail for %s", source.name)
        return False


def local_path(key: str, variant: str = "full") -> Path | None:
    """Absolute path of a local photo key, or None if missing / outside UPLOAD_FOLDER.

    ``variant="thumb"`` returns the cached 400x400 thumbnail (created on first use,
    falling back to the full photo if that fails).
    """
    path = _resolve_local(key)
    if path is None or not path.is_file():
        return None
    if variant == "thumb":
        thumb = _thumb_path(path)
        if thumb.is_file() or _make_thumbnail(path, thumb):
            return thumb
    return path


def cloudinary_url(public_id: str, **transformation: Any) -> str:
    """HTTPS delivery URL for a Cloudinary public_id with a transformation."""
    import cloudinary.utils

    creds = cloudinary_credentials() or {}
    url, _ = cloudinary.utils.cloudinary_url(
        public_id, secure=True, cloud_name=creds.get("cloud_name"), **transformation
    )
    return url


def photo_url(photo: Photo | None, variant: str = "full") -> str | None:
    """URL for ``<img src>``. ``variant``: "full" or "thumb" (400x400 crop)."""
    if photo is None:
        return None
    if photo.storage == StorageKind.LOCAL:
        params = {"key": photo.public_id}
        if variant != "full":
            params["v"] = variant
        return url_for("main.media", **params)
    if variant == "thumb" and cloudinary_credentials() is not None:
        return cloudinary_url(photo.public_id, **THUMB_TRANSFORM)
    return photo.secure_url


def image_bytes_for_ai(photo: Photo) -> bytes:
    """JPEG bytes, long edge <= AI_IMAGE_MAX_EDGE (1024 px), for Gemini.

    Raises :class:`StorageError` if the photo cannot be read.
    """
    max_edge = int(current_app.config.get("AI_IMAGE_MAX_EDGE", 1024))
    if photo.storage == StorageKind.LOCAL:
        path = local_path(photo.public_id)
        if path is None:
            raise StorageError(f"Local photo {photo.id} is missing")
        return jpeg_for_ai(path.read_bytes())

    transform = dict(AI_TRANSFORM, width=max_edge)
    if cloudinary_credentials() is not None:
        url = cloudinary_url(photo.public_id, **transform)
    else:  # credentials removed later: the stored URL still works, just not resized
        url = photo.secure_url
    if not url:
        raise StorageError(f"Photo {photo.id} has no URL")
    try:
        response = requests.get(url, timeout=CLOUDINARY_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise StorageError(f"Could not download photo {photo.id} from Cloudinary") from exc
    data = response.content
    if not data:
        raise StorageError(f"Photo {photo.id} download was empty")
    if data[:3] != b"\xff\xd8\xff" or url == photo.secure_url:
        # Not already a resized JPEG (e.g. the plain secure_url): convert it here.
        data = jpeg_for_ai(data)
    return data


# --- Deleting -----------------------------------------------------------------------------


def _clear_references(photo: Photo) -> None:
    """Null out rows that point at the photo so the delete never breaks a foreign key."""
    from app.models import ImageComparison, Message, Scan, ScanResult

    db.session.execute(
        sa.update(Scan).where(Scan.query_photo_id == photo.id).values(query_photo_id=None)
    )
    db.session.execute(
        sa.update(ScanResult)
        .where(ScanResult.candidate_photo_id == photo.id)
        .values(candidate_photo_id=None)
    )
    db.session.execute(sa.update(Message).where(Message.photo_id == photo.id).values(photo_id=None))
    db.session.execute(
        sa.delete(ImageComparison).where(ImageComparison.candidate_photo_id == photo.id)
    )


def delete_photo(photo: Photo) -> None:
    """Delete the stored file (and thumbnail) and the Photo row (caller commits)."""
    if photo.storage == StorageKind.LOCAL:
        path = _resolve_local(photo.public_id)
        if path is not None:
            path.unlink(missing_ok=True)
            _thumb_path(path).unlink(missing_ok=True)
    elif photo.storage == StorageKind.CLOUDINARY:
        creds = cloudinary_credentials()
        if creds is None:
            log.warning("Cannot delete Cloudinary photo %s: CLOUDINARY_URL not set", photo.id)
        else:
            import cloudinary.uploader

            try:
                cloudinary.uploader.destroy(
                    photo.public_id,
                    resource_type="image",
                    invalidate=True,
                    timeout=CLOUDINARY_TIMEOUT_SECONDS,
                    **creds,
                )
            except Exception:  # noqa: BLE001 - the DB row still goes; log for the admin
                log.exception("Cloudinary destroy failed for photo %s", photo.id)
    _clear_references(photo)
    db.session.delete(photo)
