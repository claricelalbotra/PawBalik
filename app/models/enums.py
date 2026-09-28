"""Enumerated values used in the database.

Each class lists the allowed string values (``ALL``) and human labels
(``LABELS``). Columns store the plain string (e.g. ``"dog"``), so compare
with the constants: ``report.status == ReportStatus.LOST``.
"""

from __future__ import annotations


class _Choices:
    ALL: tuple[str, ...] = ()
    LABELS: dict[str, str] = {}

    @classmethod
    def label(cls, value: str | None) -> str:
        return cls.LABELS.get(value or "", value or "")

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        """(value, label) pairs for WTForms SelectField / RadioField."""
        return [(v, cls.LABELS.get(v, v)) for v in cls.ALL]


class UserRole(_Choices):
    USER = "user"
    ADMIN = "admin"
    ALL = (USER, ADMIN)
    LABELS = {USER: "User", ADMIN: "Administrator"}


class Species(_Choices):
    DOG = "dog"
    CAT = "cat"
    ALL = (DOG, CAT)
    LABELS = {DOG: "Dog", CAT: "Cat"}


class PetSize(_Choices):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    ALL = (SMALL, MEDIUM, LARGE)
    LABELS = {SMALL: "Small", MEDIUM: "Medium", LARGE: "Large"}


class PetSex(_Choices):
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"
    ALL = (MALE, FEMALE, UNKNOWN)
    LABELS = {MALE: "Male", FEMALE: "Female", UNKNOWN: "Unknown"}


class ReportStatus(_Choices):
    LOST = "lost"
    FOUND = "found"
    CLOSED = "closed"
    ALL = (LOST, FOUND, CLOSED)
    LABELS = {LOST: "Lost", FOUND: "Found", CLOSED: "Closed"}
    # Allowed transitions (SPEC 5.1): lost -> found -> closed, lost -> closed.
    TRANSITIONS = {LOST: (FOUND, CLOSED), FOUND: (CLOSED,), CLOSED: ()}


class SightingStatus(_Choices):
    ACTIVE = "active"
    MATCHED = "matched"
    EXPIRED = "expired"
    ALL = (ACTIVE, MATCHED, EXPIRED)
    LABELS = {ACTIVE: "Active", MATCHED: "Matched", EXPIRED: "Expired"}


class AnimalCondition(_Choices):
    HEALTHY = "healthy"
    INJURED = "injured"
    UNKNOWN = "unknown"
    ALL = (HEALTHY, INJURED, UNKNOWN)
    LABELS = {HEALTHY: "Healthy", INJURED: "Injured", UNKNOWN: "Unknown"}


class ContactPref(_Choices):
    """How a finder wants to be contacted about a sighting."""

    IN_APP = "in_app"  # in-app messages only
    PHONE = "phone"  # in-app messages AND phone/SMS
    ALL = (IN_APP, PHONE)
    LABELS = {IN_APP: "In-app messages only", PHONE: "In-app messages and phone"}


class PhotoOwner(_Choices):
    LOST_REPORT = "lost_report"
    SIGHTING = "sighting"
    SCAN = "scan"
    MESSAGE = "message"
    AVATAR = "avatar"  # owner_id = user id
    ALL = (LOST_REPORT, SIGHTING, SCAN, MESSAGE, AVATAR)
    LABELS = {
        LOST_REPORT: "Lost report",
        SIGHTING: "Sighting",
        SCAN: "Scan",
        MESSAGE: "Message",
        AVATAR: "Profile picture",
    }


class StorageKind(_Choices):
    CLOUDINARY = "cloudinary"
    LOCAL = "local"
    ALL = (CLOUDINARY, LOCAL)
    LABELS = {CLOUDINARY: "Cloudinary", LOCAL: "Local"}


class ScanMode(_Choices):
    FOUND = "found"  # finder searches public lost reports
    LOST = "lost"  # owner searches private sightings
    ALL = (FOUND, LOST)
    LABELS = {FOUND: "Found Pet Scanner", LOST: "Lost Pet Scanner"}


class ScanTrigger(_Choices):
    MANUAL = "manual"
    BACKGROUND = "background"
    ALL = (MANUAL, BACKGROUND)
    LABELS = {MANUAL: "Manual", BACKGROUND: "Background"}


class ScanStatus(_Choices):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    REJECTED = "rejected"  # pre-check failed (not a single dog/cat ...)
    FAILED = "failed"
    ALL = (PENDING, RUNNING, COMPLETE, REJECTED, FAILED)
    LABELS = {
        PENDING: "Pending",
        RUNNING: "Scanning",
        COMPLETE: "Complete",
        REJECTED: "Rejected",
        FAILED: "Failed",
    }


class CandidateType(_Choices):
    LOST_REPORT = "lost_report"
    SIGHTING = "sighting"
    ALL = (LOST_REPORT, SIGHTING)
    LABELS = {LOST_REPORT: "Lost pet report", SIGHTING: "Pet sighting"}


class MatchTier(_Choices):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    HIDDEN = "hidden"
    ALL = (HIGH, MODERATE, LOW, HIDDEN)
    LABELS = {
        HIGH: "High visual similarity",
        MODERATE: "Moderate visual similarity",
        LOW: "Low visual similarity",
        HIDDEN: "Weak visual similarity",
    }


class MessageType(_Choices):
    TEXT = "text"
    LOCATION_PIN = "location_pin"
    IMAGE = "image"
    SYSTEM = "system"
    ALL = (TEXT, LOCATION_PIN, IMAGE, SYSTEM)
    LABELS = {TEXT: "Text", LOCATION_PIN: "Location pin", IMAGE: "Image", SYSTEM: "System"}


class ProviderType(_Choices):
    RESCUER = "rescuer"
    VET = "vet"
    SHELTER = "shelter"
    ALL = (RESCUER, VET, SHELTER)
    LABELS = {RESCUER: "Rescuer", VET: "Veterinary clinic", SHELTER: "Shelter"}


class AbuseStatus(_Choices):
    OPEN = "open"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"
    ALL = (OPEN, REVIEWED, DISMISSED)
    LABELS = {OPEN: "Open", REVIEWED: "Reviewed", DISMISSED: "Dismissed"}


class JobStatus(_Choices):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ALL = (PENDING, RUNNING, DONE, FAILED)
    LABELS = {PENDING: "Pending", RUNNING: "Running", DONE: "Done", FAILED: "Failed"}


class NotificationEvent(_Choices):
    POSSIBLE_MATCH = "possible_match"
    NEW_MESSAGE = "new_message"
    CASE_STATUS_CHANGED = "case_status_changed"
    ALL = (POSSIBLE_MATCH, NEW_MESSAGE, CASE_STATUS_CHANGED)
    LABELS = {
        POSSIBLE_MATCH: "New possible match",
        NEW_MESSAGE: "New message",
        CASE_STATUS_CHANGED: "Case status changed",
    }


class SmsPurpose(_Choices):
    OTP = "otp"
    POSSIBLE_MATCH = "possible_match"
    ALL = (OTP, POSSIBLE_MATCH)
    LABELS = {OTP: "Verification code", POSSIBLE_MATCH: "Possible match alert"}
