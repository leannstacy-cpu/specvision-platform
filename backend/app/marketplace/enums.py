from enum import StrEnum


class ProfileRole(StrEnum):
    CLIENT = "client"
    PROFESSIONAL = "professional"
    SUPPORT = "support"


class Category(StrEnum):
    AUDIOVISUAL = "audiovisual"
    IT = "it"


class Tier(StrEnum):
    CLIENT = "client"
    PROFESSIONAL = "professional"
    PRO_PLUS = "pro_plus"
    ELITE = "elite"


class Interval(StrEnum):
    MONTH = "month"
    YEAR = "year"


class SubscriptionStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"  # includes the free-trial period, see trial_end
    SUSPENDED = "suspended"
    PAYMENT_FAILED = "payment_failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"  # replaced by an upgrade/downgrade


class ProjectStatus(StrEnum):
    OPEN = "open"
    AWARDED = "awarded"
    CLOSED = "closed"


class BidStatus(StrEnum):
    SUBMITTED = "submitted"
    SHORTLISTED = "shortlisted"
    WON = "won"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class LeadUnlockStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"
    FAILED = "failed"


class PaymentStatus(StrEnum):
    DRAFT = "draft"
    PENDING_PAYMENT = "pending_payment"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    HELD_PENDING_DISBURSEMENT = "held_pending_disbursement"
    PARTIALLY_RELEASED = "partially_released"
    RELEASED = "released"
    REFUNDED = "refunded"
    CANCELED = "canceled"
    DISPUTED = "disputed"
    REVERSED = "reversed"
    FAILED = "failed"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class DisbursementMode(StrEnum):
    DELAYED = "delayed"
    # Clearly disclosed fallback when delayed disbursement is unavailable.
    DIRECT = "direct"


class MilestoneStatus(StrEnum):
    PENDING = "pending"
    FUNDING = "funding"
    FUNDED = "funded"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    RELEASED = "released"


class WebhookStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"
