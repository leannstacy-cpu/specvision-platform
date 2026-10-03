"""Server-side membership entitlements.

Everything here is derived from rows written by verified PayPal webhooks. No
value (plan, price, tier, status) is ever read from a browser request.
"""

import uuid
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.marketplace.enums import SubscriptionStatus, Tier
from app.marketplace.models import FeatureFlag, MembershipPlan, Subscription

TIER_RANK = {
    Tier.CLIENT: 0,
    Tier.PROFESSIONAL: 1,
    Tier.PRO_PLUS: 2,
    Tier.ELITE: 3,
}

# Feature -> minimum professional tier. Fees are NOT a feature: no tier waives them.
FEATURE_MIN_TIER = {
    "browse_projects": Tier.PROFESSIONAL,
    "submit_bids": Tier.PROFESSIONAL,
    "estimator": Tier.PRO_PLUS,
    "project_management_limited": Tier.PRO_PLUS,
    "early_access": Tier.PRO_PLUS,
    "project_management_full": Tier.ELITE,
    "white_label_portal": Tier.ELITE,
    "advanced_analytics": Tier.ELITE,
}


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def grants_benefits(sub: Subscription, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if sub.status == SubscriptionStatus.ACTIVE:
        return True
    if sub.status == SubscriptionStatus.CANCELLED:
        # PayPal keeps paid-for access until the end of the billing period.
        end = _aware(sub.current_period_end) or _aware(sub.trial_end)
        return end is not None and end > now
    return False


def get_active_membership(
    session: Session, user_id: uuid.UUID
) -> tuple[Subscription, MembershipPlan] | None:
    rows = session.exec(
        select(Subscription, MembershipPlan)
        .where(Subscription.user_id == user_id)
        .where(Subscription.plan_id == MembershipPlan.id)
    ).all()
    best: tuple[Subscription, MembershipPlan] | None = None
    for sub, plan in rows:
        if not grants_benefits(sub):
            continue
        if best is None or TIER_RANK[Tier(plan.tier)] > TIER_RANK[Tier(best[1].tier)]:
            best = (sub, plan)
    return best


def active_tier(session: Session, user_id: uuid.UUID) -> Tier | None:
    membership = get_active_membership(session, user_id)
    return Tier(membership[1].tier) if membership else None


def has_feature(session: Session, user_id: uuid.UUID, feature: str) -> bool:
    tier = active_tier(session, user_id)
    if tier is None or tier == Tier.CLIENT:
        return False
    return TIER_RANK[tier] >= TIER_RANK[FEATURE_MIN_TIER[feature]]


def is_active_client(session: Session, user_id: uuid.UUID) -> bool:
    return active_tier(session, user_id) == Tier.CLIENT


def is_active_professional(session: Session, user_id: uuid.UUID) -> bool:
    tier = active_tier(session, user_id)
    return tier is not None and tier != Tier.CLIENT


def lead_fee_cents(session: Session, user_id: uuid.UUID) -> int | None:
    """Per-project lead fee for the user's current tier (never zero by design)."""
    membership = get_active_membership(session, user_id)
    if membership is None:
        return None
    return membership[1].lead_fee_cents


def feature_enabled(session: Session, key: str) -> bool:
    flag = session.get(FeatureFlag, key)
    return bool(flag and flag.enabled)
