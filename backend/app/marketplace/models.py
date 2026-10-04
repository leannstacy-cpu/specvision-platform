"""Marketplace tables.

Enumerated values are stored as plain strings (see ``enums.py``) so that adding a
state never needs a PostgreSQL enum migration. All foreign keys to ``user`` use
ON DELETE CASCADE to support the account-deletion workflow.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.marketplace.enums import (
    BidStatus,
    DisbursementMode,
    Interval,
    LeadUnlockStatus,
    MilestoneStatus,
    PaymentStatus,
    ProfileRole,
    ProjectStatus,
    SubscriptionStatus,
    WebhookStatus,
)


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


def _ts(**kwargs: Any) -> Any:
    return Field(sa_type=DateTime(timezone=True), **kwargs)  # type: ignore


def _now() -> Any:
    return _ts(default_factory=get_datetime_utc)


class Profile(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", unique=True, index=True, ondelete="CASCADE"
    )
    role: str = Field(default=ProfileRole.CLIENT, max_length=32)
    display_name: str = Field(max_length=255)
    is_company: bool = False
    company_name: str | None = Field(default=None, max_length=255)
    # "audiovisual" | "it" for professionals
    category: str | None = Field(default=None, max_length=32)
    created_at: datetime | None = _now()


class MembershipPlan(SQLModel, table=True):
    """Internal plan record. Prices and lead fees are only ever read from here."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(unique=True, index=True, max_length=64)
    name: str = Field(max_length=128)
    tier: str = Field(max_length=32)
    interval: str = Field(default=Interval.MONTH, max_length=16)
    price_cents: int = Field(ge=0)
    trial_days: int = Field(default=7, ge=0)
    # None for client plans. Fee is per qualified project, never waived.
    lead_fee_cents: int | None = Field(default=None, ge=0)
    # Set by an administrator from the PayPal dashboard/API; never hard-coded.
    paypal_plan_id: str | None = Field(default=None, unique=True, index=True)
    paypal_product_id: str | None = Field(default=None, max_length=64)
    is_active: bool = True
    is_demo: bool = False


class Subscription(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True, ondelete="CASCADE")
    plan_id: uuid.UUID = Field(foreign_key="membershipplan.id")
    paypal_subscription_id: str = Field(unique=True, index=True, max_length=64)
    status: str = Field(default=SubscriptionStatus.PENDING_APPROVAL, max_length=32)
    trial_start: datetime | None = _ts(default=None)
    trial_end: datetime | None = _ts(default=None)
    current_period_end: datetime | None = _ts(default=None)
    cancelled_at: datetime | None = _ts(default=None)
    created_at: datetime | None = _now()
    updated_at: datetime | None = _now()


class SubscriptionPayment(SQLModel, table=True):
    """Membership payment history, written from verified webhooks only."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    subscription_id: uuid.UUID = Field(
        foreign_key="subscription.id", index=True, ondelete="CASCADE"
    )
    paypal_transaction_id: str = Field(unique=True, index=True, max_length=64)
    amount_cents: int
    status: str = Field(max_length=32)  # completed | failed | refunded | reversed
    occurred_at: datetime | None = _now()


class Project(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(foreign_key="user.id", index=True, ondelete="CASCADE")
    status: str = Field(default=ProjectStatus.OPEN, max_length=16)
    # --- fields visible to professionals before unlock -------------------
    title: str = Field(max_length=255)
    description: str = Field(sa_column=Column(Text, nullable=False))
    category: str = Field(max_length=32)
    project_type: str = Field(max_length=64)
    service_area: str = Field(max_length=128)  # approximate area, never an address
    budget_min_cents: int | None = Field(default=None, ge=0)
    budget_max_cents: int | None = Field(default=None, ge=0)
    desired_timeline: str | None = Field(default=None, max_length=128)
    specialties: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    is_remote: bool = False
    is_onsite: bool = False
    is_urgent: bool = False
    is_after_hours: bool = False
    modifier_codes: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    privacy_requested: bool = True
    # --- protected fields: only revealed after a verified lead unlock -----
    contact_name: str | None = Field(default=None, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=64)
    street_address: str | None = Field(default=None, max_length=255)
    website: str | None = Field(default=None, max_length=255)
    awarded_bid_id: uuid.UUID | None = Field(default=None)
    created_at: datetime | None = _now()


class Bid(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("project_id", "professional_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        foreign_key="project.id", index=True, ondelete="CASCADE"
    )
    professional_id: uuid.UUID = Field(
        foreign_key="user.id", index=True, ondelete="CASCADE"
    )
    amount_cents: int = Field(ge=0)
    timeline_days: int = Field(ge=1)
    message: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default=BidStatus.SUBMITTED, max_length=16)
    revision: int = 1
    created_at: datetime | None = _now()
    updated_at: datetime | None = _now()


class LeadUnlock(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("project_id", "professional_id"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        foreign_key="project.id", index=True, ondelete="CASCADE"
    )
    professional_id: uuid.UUID = Field(
        foreign_key="user.id", index=True, ondelete="CASCADE"
    )
    fee_cents: int
    status: str = Field(default=LeadUnlockStatus.PENDING, max_length=16)
    paypal_order_id: str | None = Field(default=None, unique=True, max_length=64)
    paypal_capture_id: str | None = Field(default=None, max_length=64)
    created_at: datetime | None = _now()
    paid_at: datetime | None = _ts(default=None)


class SellerAccount(SQLModel, table=True):
    """PayPal marketplace seller onboarding state (updated from webhooks)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", unique=True, index=True, ondelete="CASCADE"
    )
    paypal_merchant_id: str | None = Field(default=None, index=True, max_length=64)
    onboarding_complete: bool = False
    payments_receivable: bool = False
    # Delayed disbursement must be confirmed for the seller before it is used.
    delayed_disbursement_enabled: bool = False
    updated_at: datetime | None = _now()


class Milestone(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        foreign_key="project.id", index=True, ondelete="CASCADE"
    )
    seller_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    title: str = Field(max_length=255)
    amount_cents: int = Field(gt=0)
    status: str = Field(default=MilestoneStatus.PENDING, max_length=16)
    created_at: datetime | None = _now()


class Payment(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        foreign_key="project.id", index=True, ondelete="CASCADE"
    )
    milestone_id: uuid.UUID | None = Field(
        default=None, foreign_key="milestone.id", index=True, ondelete="CASCADE"
    )
    payer_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    seller_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    amount_cents: int = Field(gt=0)
    fee_cents: int = 0  # marketplace fee, configured by an administrator
    status: str = Field(default=PaymentStatus.DRAFT, max_length=32)
    disbursement_mode: str = Field(default=DisbursementMode.DELAYED, max_length=16)
    paypal_order_id: str | None = Field(default=None, unique=True, max_length=64)
    paypal_authorization_id: str | None = Field(default=None, max_length=64)
    paypal_capture_id: str | None = Field(default=None, index=True, max_length=64)
    paypal_merchant_id: str | None = Field(default=None, max_length=64)
    released_cents: int = 0
    captured_at: datetime | None = _ts(default=None)
    release_deadline: datetime | None = _ts(default=None, index=True)
    created_at: datetime | None = _now()


class WebhookEvent(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    paypal_event_id: str = Field(unique=True, index=True, max_length=128)
    event_type: str = Field(index=True, max_length=128)
    environment: str = Field(max_length=16)
    raw_body: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default=WebhookStatus.RECEIVED, max_length=16)
    attempts: int = 0
    error: str | None = Field(default=None, sa_column=Column(Text))
    received_at: datetime | None = _now()
    processed_at: datetime | None = _ts(default=None)


class ProjectModifier(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(unique=True, index=True, max_length=64)
    label: str = Field(max_length=128)
    price_cents: int = Field(ge=0)
    is_active: bool = True
    is_demo: bool = False


class DiscountRule(SQLModel, table=True):
    """A discount only applies when an active rule exists and its eligibility matches."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(unique=True, index=True, max_length=64)
    label: str = Field(max_length=128)
    amount_cents: int = Field(ge=0)
    new_customers_only: bool = True
    is_active: bool = False


class FeatureFlag(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=64)
    enabled: bool = False
    description: str = Field(default="", max_length=255)


class PlatformSetting(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=64)
    value: str = Field(max_length=255)


class AuditLog(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    actor_id: uuid.UUID | None = Field(default=None, index=True)
    action: str = Field(index=True, max_length=64)
    entity_type: str = Field(max_length=64)
    entity_id: str | None = Field(default=None, max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime | None = _now()


__all__ = [
    "AuditLog",
    "Bid",
    "DiscountRule",
    "FeatureFlag",
    "LeadUnlock",
    "MembershipPlan",
    "Milestone",
    "Payment",
    "PlatformSetting",
    "Profile",
    "Project",
    "ProjectModifier",
    "SellerAccount",
    "Subscription",
    "SubscriptionPayment",
    "WebhookEvent",
]
