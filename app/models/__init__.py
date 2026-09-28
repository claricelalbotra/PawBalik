"""All database models. Import from here: ``from app.models import LostReport``.

Tables follow SPEC section 7. Datetimes are naive UTC (see app.utils.time).
"""

from app.models.enums import (  # noqa: F401
    AbuseStatus,
    AnimalCondition,
    CandidateType,
    ContactPref,
    JobStatus,
    MatchTier,
    MessageType,
    NotificationEvent,
    PetSex,
    PetSize,
    PhotoOwner,
    ProviderType,
    ReportStatus,
    ScanMode,
    ScanStatus,
    ScanTrigger,
    SightingStatus,
    SmsPurpose,
    Species,
    StorageKind,
    UserRole,
)
from app.models.messaging import AbuseReport, Conversation, Message
from app.models.notification import Notification, PushLog, SmsLog
from app.models.photo import Photo
from app.models.report import (
    CaseCounter,
    CaseStatusHistory,
    InvalidStatusTransition,
    LostReport,
    allowed_transitions,
    can_transition,
)
from app.models.rescue import AssistanceProvider, RescueRequest
from app.models.scan import ImageComparison, Scan, ScanResult
from app.models.sighting import Sighting, SightingAccessLog
from app.models.system import AuditLog, Job
from app.models.user import DeviceToken, PhoneVerification, User

__all__ = [
    "AbuseReport",
    "AssistanceProvider",
    "AuditLog",
    "CaseCounter",
    "CaseStatusHistory",
    "Conversation",
    "DeviceToken",
    "ImageComparison",
    "InvalidStatusTransition",
    "Job",
    "LostReport",
    "Message",
    "Notification",
    "PhoneVerification",
    "Photo",
    "PushLog",
    "RescueRequest",
    "Scan",
    "ScanResult",
    "Sighting",
    "SightingAccessLog",
    "SmsLog",
    "User",
    "allowed_transitions",
    "can_transition",
]
