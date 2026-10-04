"""Idempotent catalog seed: membership plans, project modifiers, feature flags.

Prices come from the product specification. ``paypal_plan_id`` is intentionally
left empty: an administrator maps each internal plan to its PayPal plan (created
in the sandbox or live PayPal account) after deployment. Plan IDs are never
hard-coded.
"""

from sqlmodel import Session, select

from app.marketplace.enums import Interval, Tier
from app.marketplace.models import (
    DiscountRule,
    FeatureFlag,
    MembershipPlan,
    ProjectModifier,
)

# (code, name, tier, interval, price_cents, lead_fee_cents)
PLANS: list[tuple[str, str, Tier, Interval, int, int | None]] = [
    ("client_monthly", "Client Monthly", Tier.CLIENT, Interval.MONTH, 999, None),
    ("client_annual", "Client Annual", Tier.CLIENT, Interval.YEAR, 9900, None),
    ("professional_monthly", "Professional Monthly", Tier.PROFESSIONAL, Interval.MONTH, 1999, 5000),
    ("professional_annual", "Professional Annual", Tier.PROFESSIONAL, Interval.YEAR, 19900, 5000),
    ("pro_plus_monthly", "Pro Plus Monthly", Tier.PRO_PLUS, Interval.MONTH, 3999, 2500),
    ("pro_plus_annual", "Pro Plus Annual", Tier.PRO_PLUS, Interval.YEAR, 39900, 2500),
    ("elite_monthly", "Elite Monthly", Tier.ELITE, Interval.MONTH, 6999, 1500),
    ("elite_annual", "Elite Annual", Tier.ELITE, Interval.YEAR, 69900, 1500),
]  # fmt: skip

MODIFIERS = [
    ("urgent_request", "Urgent Request", 3000),
    ("on_site_assessment", "On-Site Assessment", 5000),
    ("after_hours", "After-Hours", 5000),
]

# Features that are NOT operational yet stay off until implemented.
FEATURE_FLAGS = [
    ("seller_onboarding", "PayPal partner seller onboarding (not implemented)"),
    ("remote_support", "Remote-support provider integration (placeholder only)"),
    ("estimator", "Project estimator for Pro Plus/Elite (not implemented)"),
    ("project_management", "Project-management suites (not implemented)"),
    ("white_label_portal", "Elite white-label client portal (not implemented)"),
    ("messaging", "In-platform messaging (not implemented)"),
]


def seed_catalog(session: Session) -> None:
    existing_plans = {p.code for p in session.exec(select(MembershipPlan)).all()}
    for code, name, tier, interval, price, fee in PLANS:
        if code not in existing_plans:
            session.add(
                MembershipPlan(
                    code=code,
                    name=name,
                    tier=tier,
                    interval=interval,
                    price_cents=price,
                    lead_fee_cents=fee,
                    trial_days=7,
                )
            )
    existing_mods = {m.code for m in session.exec(select(ProjectModifier)).all()}
    for code, label, price in MODIFIERS:
        if code not in existing_mods:
            session.add(ProjectModifier(code=code, label=label, price_cents=price))
    for key, description in FEATURE_FLAGS:
        if session.get(FeatureFlag, key) is None:
            session.add(FeatureFlag(key=key, enabled=False, description=description))
    # Inactive until an administrator defines the amount and enables it.
    if not session.exec(
        select(DiscountRule).where(DiscountRule.code == "new_customer")
    ).first():
        session.add(
            DiscountRule(
                code="new_customer",
                label="New-customer discount",
                amount_cents=0,
                is_active=False,
            )
        )
    session.commit()
