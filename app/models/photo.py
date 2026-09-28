"""Photos for reports, sightings, scans and chat messages.

A photo belongs to its owner through ``(owner_type, owner_id)`` (a
polymorphic association, so there is no DB foreign key on ``owner_id``):

* ``lost_report`` -> ``lost_reports.id``
* ``sighting``    -> ``sightings.id``
* ``scan``        -> ``scans.id`` (None until the scan row exists; attach with :meth:`attach_to`)
* ``message``     -> ``conversations.id`` (the chat the image was sent in;
  ``messages.photo_id`` points back at the photo)

Use :meth:`Photo.for_owner` or the ``photos`` / ``primary_photo`` helpers on
LostReport and Sighting. URLs always come from
``app.services.storage_service.photo_url(photo, variant)``.

``public_id`` is the Cloudinary public_id, or for local storage the
unguessable key relative to UPLOAD_FOLDER, e.g.
``lost_reports/12/9f2c...e1.jpg`` (served by ``main.media``).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import CreatedAtMixin, enum_type, table_args
from app.models.enums import PhotoOwner, StorageKind


class Photo(CreatedAtMixin, db.Model):
    __tablename__ = "photos"
    __table_args__ = table_args(
        sa.Index("ix_photos_owner", "owner_type", "owner_id"),
        sa.Index("ix_photos_sha256", "sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str] = mapped_column(
        enum_type(PhotoOwner.ALL, "photo_owner_type"), nullable=False
    )
    owner_id: Mapped[int | None] = mapped_column(sa.Integer)  # None until the owner row exists
    storage: Mapped[str] = mapped_column(
        enum_type(StorageKind.ALL, "photo_storage"), nullable=False
    )
    # Cloudinary public_id, or the unguessable storage key for local files.
    public_id: Mapped[str] = mapped_column(sa.String(255), unique=True, nullable=False)
    secure_url: Mapped[str | None] = mapped_column(sa.String(512))  # None for local storage
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)  # of the original bytes
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    ai_attributes: Mapped[dict | None] = mapped_column(sa.JSON)  # SPEC 6.3, analysed once

    @classmethod
    def for_owner(cls, owner_type: str, owner_id: int | None) -> list[Photo]:
        """All photos of one owner, primary first, then by sort_order."""
        if owner_id is None:
            return []
        stmt = (
            sa.select(cls)
            .where(cls.owner_type == owner_type, cls.owner_id == owner_id)
            .order_by(cls.is_primary.desc(), cls.sort_order, cls.id)
        )
        return list(db.session.scalars(stmt))

    @classmethod
    def primary_for(cls, owner_type: str, owner_id: int | None) -> Photo | None:
        photos = cls.for_owner(owner_type, owner_id)
        return photos[0] if photos else None

    @classmethod
    def by_key(cls, public_id: str) -> Photo | None:
        return db.session.scalar(sa.select(cls).where(cls.public_id == public_id))

    def attach_to(self, owner_type: str, owner_id: int) -> None:
        """Point a photo saved with owner_id=None at its owner (e.g. after a scan row exists)."""
        self.owner_type = owner_type
        self.owner_id = owner_id

    def __repr__(self) -> str:
        return f"<Photo {self.id} {self.owner_type}:{self.owner_id}>"
