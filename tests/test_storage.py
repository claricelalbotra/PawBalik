"""storage_service: validation, EXIF stripping, hashing, local + Cloudinary backends (SPEC 8)."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pytest
from PIL import Image

from app.extensions import db
from app.models import Photo, PhotoOwner, Scan, ScanMode, ScanStatus, StorageKind
from app.services import storage_service
from app.services.storage_service import ImageValidationError, StorageError

HEX_NAME = re.compile(r"^[0-9a-f]{32}\.jpg$")


def _image_bytes(
    fmt: str = "JPEG", size=(64, 48), color=(10, 120, 200), mode="RGB", **save
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt, **save)
    return buffer.getvalue()


def _jpeg_with_exif(size=(80, 40), orientation: int = 6) -> bytes:
    """A JPEG carrying GPS + camera EXIF and an orientation tag."""
    exif = Image.Exif()
    exif[0x0112] = orientation  # Orientation: 6 = rotate 90 CW when displaying
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {1: "N", 2: (11.0, 14.0, 37.0), 3: "E", 4: (125.0, 0.0, 16.0)}  # GPS IFD
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _save(data: bytes, owner_type=PhotoOwner.LOST_REPORT, owner_id=7, **kw) -> Photo:
    folder = kw.pop("folder", storage_service.default_folder(owner_type, owner_id))
    return storage_service.save_image(
        data, owner_type=owner_type, owner_id=owner_id, folder=folder, **kw
    )


# --- Local backend: validation and cleaning -----------------------------------------------


def test_save_jpeg_locally_with_unguessable_key_and_original_sha256(app):
    data = _image_bytes()
    photo = _save(data, is_primary=True, sort_order=2)

    assert photo.id is not None  # flushed
    assert photo.storage == StorageKind.LOCAL
    assert photo.secure_url is None
    assert photo.public_id.startswith("lost_reports/7/")
    assert HEX_NAME.match(photo.public_id.rsplit("/", 1)[1])
    assert photo.sha256 == hashlib.sha256(data).hexdigest()
    assert (photo.width, photo.height) == (64, 48)
    assert photo.is_primary and photo.sort_order == 2
    stored = Path(app.config["UPLOAD_FOLDER"]) / photo.public_id
    assert stored.is_file()
    with Image.open(stored) as image:
        assert image.format == "JPEG"


def test_exif_and_gps_are_stripped_and_orientation_applied(app):
    data = _jpeg_with_exif(size=(80, 40), orientation=6)
    with Image.open(io.BytesIO(data)) as original:
        assert original.getexif()  # the upload really carries EXIF

    photo = _save(data)
    stored = Path(app.config["UPLOAD_FOLDER"]) / photo.public_id
    raw = stored.read_bytes()
    assert b"SecretCam" not in raw
    assert b"Exif" not in raw
    with Image.open(stored) as image:
        assert len(image.getexif()) == 0
        assert image.size == (40, 80)  # rotated upright
    assert (photo.width, photo.height) == (40, 80)
    assert photo.sha256 == hashlib.sha256(data).hexdigest()  # hash of the ORIGINAL bytes


@pytest.mark.parametrize(
    "data",
    [
        _image_bytes("PNG", mode="RGBA", color=(0, 0, 0, 0)),
        _image_bytes("PNG", mode="P", color=3),
        _image_bytes("WEBP"),
        _image_bytes("JPEG", mode="L", color=90),
    ],
    ids=["png-transparent", "png-palette", "webp", "jpeg-grey"],
)
def test_png_webp_and_grey_images_become_rgb_jpeg(app, data):
    photo = _save(data)
    with Image.open(Path(app.config["UPLOAD_FOLDER"]) / photo.public_id) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"this is definitely not an image", "invalid_image"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 50, "invalid_image"),  # JPEG magic, broken body
        (_image_bytes("GIF", mode="P", color=1), "unsupported_format"),
        (_image_bytes("BMP"), "unsupported_format"),
        (b"", "empty_file"),
    ],
    ids=["text", "truncated-jpeg", "gif", "bmp", "empty"],
)
def test_bad_uploads_are_rejected_by_header_not_extension(app, data, code):
    upload = (io.BytesIO(data), "cute_dog.jpg")[0]  # a .jpg name does not matter
    with pytest.raises(ImageValidationError) as err:
        _save(upload)
    assert err.value.code == code
    assert err.value.message
    assert db.session.query(Photo).count() == 0


@pytest.mark.config(MAX_UPLOAD_BYTES=1024)
def test_too_big_upload_is_rejected(app):
    data = _image_bytes(size=(300, 300), quality=100)
    assert len(data) > 1024
    with pytest.raises(ImageValidationError) as err:
        _save(data)
    assert err.value.code == "file_too_large"
    assert "MB" in err.value.message


def test_decompression_bomb_guard(app, monkeypatch):
    monkeypatch.setattr(storage_service, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(ImageValidationError) as err:
        _save(_image_bytes(size=(64, 48)))
    assert err.value.code == "too_many_pixels"


def test_large_photos_are_downscaled_for_storage(app):
    photo = _save(_image_bytes(size=(3000, 1500)))
    assert max(photo.width, photo.height) == storage_service.STORED_MAX_EDGE


def test_accepts_filestorage_path_and_file_objects(app, tmp_path):
    from werkzeug.datastructures import FileStorage

    data = _image_bytes()
    path = tmp_path / "pet.png"
    path.write_bytes(_image_bytes("PNG"))
    p1 = _save(FileStorage(stream=io.BytesIO(data), filename="x.jpg"))
    p2 = _save(path)
    p3 = _save(io.BytesIO(data))
    assert len({p1.public_id, p2.public_id, p3.public_id}) == 3


def test_folder_traversal_is_neutralised(app):
    photo = _save(_image_bytes(), folder="../../..//etc/C:/passwd")
    root = Path(app.config["UPLOAD_FOLDER"]).resolve()
    stored = (root / photo.public_id).resolve()
    assert root in stored.parents
    assert ".." not in photo.public_id
    assert photo.public_id.startswith("etc/passwd/")


def test_unknown_owner_type_is_a_programming_error(app):
    with pytest.raises(ValueError):
        storage_service.save_image(_image_bytes(), owner_type="banner", folder="x")


def test_default_folders(app):
    assert storage_service.default_folder(PhotoOwner.LOST_REPORT, 12) == "lost_reports/12"
    assert storage_service.default_folder(PhotoOwner.SIGHTING, 7) == "sightings/7"
    assert storage_service.default_folder(PhotoOwner.MESSAGE, 3) == "messages/3"
    assert storage_service.default_folder(PhotoOwner.AVATAR, 5) == "avatars/5"
    assert re.fullmatch(r"scans/\d{4}-\d{2}", storage_service.default_folder(PhotoOwner.SCAN))


# --- Local serving ---------------------------------------------------------------------------


def test_local_path_is_traversal_safe(app, tmp_path):
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(_image_bytes())
    photo = _save(_image_bytes())
    assert storage_service.local_path(photo.public_id) is not None
    assert storage_service.local_path("../secret.jpg") is None
    assert storage_service.local_path(str(outside)) is None
    assert storage_service.local_path("lost_reports/7/../../../secret.jpg") is None
    assert storage_service.local_path("lost_reports/7/missing.jpg") is None
    assert storage_service.local_path("bad\x00key") is None


def test_media_route_serves_public_photo_and_thumbnail(app, client, make_lost_report):
    report = make_lost_report()
    photo = report.primary_photo
    with app.test_request_context():
        full_url = storage_service.photo_url(photo)
        thumb_url = storage_service.photo_url(photo, "thumb")
    assert full_url == f"/media/{photo.public_id}"
    assert thumb_url.endswith("?v=thumb")

    response = client.get(full_url)
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"

    response = client.get(thumb_url)
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.size == (400, 400)
    assert storage_service.local_path(photo.public_id, "thumb").name.endswith(".thumb.jpg")


def test_media_route_hides_private_and_traversal_paths(app, client, make_sighting):
    sighting = make_sighting()
    photo = sighting.primary_photo
    assert client.get(f"/media/{photo.public_id}").status_code == 404  # guest: private sighting
    assert client.get("/media/../../app/config.py").status_code == 404
    assert client.get("/media/lost_reports/1/..%2F..%2F..%2Fapp%2Fconfig.py").status_code == 404


def test_image_bytes_for_ai_resizes_to_1024(app):
    photo = _save(_image_bytes(size=(2000, 1000)))
    data = storage_service.image_bytes_for_ai(photo)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert max(image.size) == 1024


def test_image_bytes_for_ai_missing_file_raises_storage_error(app):
    photo = _save(_image_bytes())
    (Path(app.config["UPLOAD_FOLDER"]) / photo.public_id).unlink()
    with pytest.raises(StorageError):
        storage_service.image_bytes_for_ai(photo)


def test_delete_photo_removes_files_row_and_references(app, user):
    photo = _save(_image_bytes(), owner_type=PhotoOwner.SCAN, owner_id=None)
    scan = Scan(
        user_id=user.id, mode=ScanMode.FOUND, query_photo_id=photo.id, status=ScanStatus.COMPLETE
    )
    db.session.add(scan)
    db.session.commit()
    path = storage_service.local_path(photo.public_id)
    thumb = storage_service.local_path(photo.public_id, "thumb")
    assert path.is_file() and thumb.is_file()

    storage_service.delete_photo(photo)
    db.session.commit()
    assert not path.exists() and not thumb.exists()
    assert db.session.get(Photo, photo.id) is None
    assert db.session.get(Scan, scan.id).query_photo_id is None


# --- Backend selection --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "url", "expected"),
    [
        ("auto", None, "local"),
        ("auto", "cloudinary://key:secret@demo", "cloudinary"),
        ("local", "cloudinary://key:secret@demo", "local"),
        ("cloudinary", "cloudinary://key:secret@demo", "cloudinary"),
        ("cloudinary", None, "local"),  # misconfigured: warn and keep working
        ("auto", "https://not-a-cloudinary-url", "local"),
    ],
)
def test_backend_choice(app, choice, url, expected):
    app.config.update(STORAGE_BACKEND=choice, CLOUDINARY_URL=url)
    assert storage_service.backend() == expected


# --- Cloudinary backend (SDK mocked; no network) ----------------------------------------------


@pytest.fixture
def cloudinary_on(app, monkeypatch):
    """Configure Cloudinary and replace the SDK's network calls with recorders."""
    import cloudinary.uploader

    app.config.update(STORAGE_BACKEND="auto", CLOUDINARY_URL="cloudinary://123key:s3cr%40t@demo")
    calls: dict[str, list] = {"upload": [], "destroy": []}

    def fake_upload(file, **options):
        calls["upload"].append((file.read(), options))
        public_id = f"{options['folder']}/{options['public_id']}"
        return {
            "public_id": public_id,
            "secure_url": f"https://res.cloudinary.com/demo/image/upload/v1/{public_id}.jpg",
            "width": 64,
            "height": 48,
        }

    def fake_destroy(public_id, **options):
        calls["destroy"].append((public_id, options))
        return {"result": "ok"}

    monkeypatch.setattr(cloudinary.uploader, "upload", fake_upload)
    monkeypatch.setattr(cloudinary.uploader, "destroy", fake_destroy)
    return calls


def test_cloudinary_signed_upload_with_spec_folder(app, cloudinary_on):
    data = _jpeg_with_exif()
    photo = _save(data, owner_type=PhotoOwner.SIGHTING, owner_id=5)

    assert photo.storage == StorageKind.CLOUDINARY
    assert photo.public_id.startswith("pawbalik/test/sightings/5/")
    assert photo.secure_url.startswith("https://res.cloudinary.com/demo/")
    assert photo.sha256 == hashlib.sha256(data).hexdigest()
    sent, options = cloudinary_on["upload"][0]
    assert options["folder"] == "pawbalik/test/sightings/5"
    assert options["api_key"] == "123key"
    assert options["api_secret"] == "s3cr@t"  # URL-decoded, passed per call (signed upload)
    assert options["cloud_name"] == "demo"
    assert options["overwrite"] is False
    assert b"SecretCam" not in sent  # EXIF stripped BEFORE upload
    # Nothing written locally.
    assert not any(Path(app.config["UPLOAD_FOLDER"]).rglob("*.jpg"))


def test_cloudinary_urls_use_spec_transformations(app, cloudinary_on):
    photo = _save(_image_bytes())
    thumb = storage_service.photo_url(photo, "thumb")
    assert thumb.startswith("https://res.cloudinary.com/demo/image/upload/")
    for part in ("c_fill", "w_400", "h_400"):
        assert part in thumb
    assert photo.public_id in thumb
    assert storage_service.photo_url(photo) == photo.secure_url


def test_cloudinary_ai_bytes_downloads_transformed_url(app, cloudinary_on, monkeypatch):
    photo = _save(_image_bytes())
    seen: dict = {}

    class FakeResponse:
        content = _image_bytes(size=(500, 300))

        def raise_for_status(self):
            return None

    def fake_get(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return FakeResponse()

    monkeypatch.setattr(storage_service.requests, "get", fake_get)
    data = storage_service.image_bytes_for_ai(photo)
    assert data[:3] == b"\xff\xd8\xff"
    for part in ("c_limit", "w_1024", "q_auto", "f_jpg"):
        assert part in seen["url"]
    assert seen["timeout"]


def test_cloudinary_download_error_becomes_storage_error(app, cloudinary_on, monkeypatch):
    import requests

    photo = _save(_image_bytes())

    def boom(url, timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(storage_service.requests, "get", boom)
    with pytest.raises(StorageError):
        storage_service.image_bytes_for_ai(photo)


def test_cloudinary_delete_calls_destroy(app, cloudinary_on):
    photo = _save(_image_bytes())
    public_id = photo.public_id
    storage_service.delete_photo(photo)
    db.session.commit()
    assert cloudinary_on["destroy"][0][0] == public_id
    assert cloudinary_on["destroy"][0][1]["api_key"] == "123key"
    assert db.session.query(Photo).count() == 0


def test_cloudinary_failure_falls_back_to_local(app, cloudinary_on, monkeypatch):
    import cloudinary.uploader

    def broken(file, **options):
        raise cloudinary.exceptions.Error("Cloudinary is down")

    monkeypatch.setattr(cloudinary.uploader, "upload", broken)
    photo = _save(_image_bytes())
    assert photo.storage == StorageKind.LOCAL
    assert storage_service.local_path(photo.public_id) is not None
