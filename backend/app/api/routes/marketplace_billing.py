import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, SQLModel, col, select

from app.api.deps import CurrentProfile, CurrentUser, GatewayDep, SessionDep
from app.core.config import settings
from app.marketplace import audit, entitlements
from app.marketplace.contact_scanner import describe_findings
from app.marketplace.enums import (
    Category,
    ProfileRole,
    SubscriptionStatus,
    Tier,
)
from app.marketplace.models import (
    DiscountRule,
    MembershipPlan,
    Profile,
    Project,
    ProjectModifier,
    Subscription,
    SubscriptionPayment,
)
from app.marketplace.paypal import PayPalError

router = APIRouter(tags=["marketplace-billing"])


class PlanPublic(SQLModel):
    code: str
    name: str
    tier: str
    interval: str
    price_cents: int
    trial_days: int
    lead_fee_cents: int | None
    purchasable: bool  # an administrator has mapped the PayPal plan


@router.get("/plans", response_model=list[PlanPublic])
def list_plans(session: SessionDep) -> Any:
    """Public membership comparison data (no PayPal identifiers exposed)."""
    plans = session.exec(
        select(MembershipPlan)
        .where(MembershipPlan.is_active)
        .order_by(col(MembershipPlan.price_cents))
    ).all()
    return [
        PlanPublic(
            **p.model_dump(include=set(PlanPublic.model_fields) - {"purchasable"}),
            purchasable=bool(p.paypal_plan_id),
        )
        for p in plans
    ]


class ModifierPublic(SQLModel):
    code: str
    label: str
    price_cents: int


@router.get("/modifiers", response_model=list[ModifierPublic])
def list_modifiers(session: SessionDep) -> Any:
    return session.exec(select(ProjectModifier).where(ProjectModifier.is_active)).all()


class QuoteRequest(BaseModel):
    modifier_codes: list[str] = []


class Quote(SQLModel):
    lines: list[ModifierPublic]
    discount_cents: int
    total_cents: int


def build_quote(session: Session, user_id: uuid.UUID, codes: list[str]) -> Quote:
    unique = list(dict.fromkeys(codes))
    mods = session.exec(
        select(ProjectModifier)
        .where(col(ProjectModifier.code).in_(unique))
        .where(ProjectModifier.is_active)
    ).all()
    if len(mods) != len(unique):
        raise HTTPException(status_code=422, detail="Unknown or inactive modifier")
    subtotal = sum(m.price_cents for m in mods)
    discount = 0
    has_projects = (
        session.exec(select(Project.id).where(Project.owner_id == user_id)).first()
        is not None
    )
    for rule in session.exec(select(DiscountRule).where(DiscountRule.is_active)):
        if rule.new_customers_only and has_projects:
            continue
        discount += rule.amount_cents  # only active, eligible rules apply
    discount = min(discount, subtotal)
    return Quote(
        lines=[ModifierPublic(**m.model_dump()) for m in mods],
        discount_cents=discount,
        total_cents=subtotal - discount,
    )


@router.post("/modifiers/quote", response_model=Quote)
def quote_modifiers(
    body: QuoteRequest, session: SessionDep, current_user: CurrentUser
) -> Any:
    return build_quote(session, current_user.id, body.modifier_codes)


class ProfileCreate(BaseModel):
    role: ProfileRole = ProfileRole.CLIENT
    display_name: str
    is_company: bool = False
    company_name: str | None = None
    category: Category | None = None


class ProfilePublic(SQLModel):
    id: uuid.UUID
    role: str
    display_name: str
    is_company: bool
    company_name: str | None
    category: str | None


@router.post("/profile", response_model=ProfilePublic)
def create_profile(
    body: ProfileCreate, session: SessionDep, current_user: CurrentUser
) -> Any:
    if body.role == ProfileRole.SUPPORT:
        raise HTTPException(
            status_code=403, detail="Support roles are assigned by staff"
        )
    if session.exec(select(Profile).where(Profile.user_id == current_user.id)).first():
        raise HTTPException(status_code=409, detail="Profile already exists")
    if not body.display_name.strip() or len(body.display_name) > 255:
        raise HTTPException(status_code=422, detail="Invalid display name")
    if body.role == ProfileRole.PROFESSIONAL and body.category is None:
        raise HTTPException(
            status_code=422, detail="Professionals must choose a category"
        )
    problem = describe_findings(body.display_name, body.company_name)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    profile = Profile(
        user_id=current_user.id,
        role=body.role,
        display_name=body.display_name.strip(),
        is_company=body.is_company,
        company_name=body.company_name,
        category=body.category,
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile


@router.get("/profile/me", response_model=ProfilePublic)
def read_my_profile(profile: CurrentProfile) -> Any:
    return profile


class CheckoutRequest(BaseModel):
    plan_code: str


class CheckoutResponse(SQLModel):
    subscription_id: uuid.UUID
    approval_url: str


@router.post("/subscriptions/checkout", response_model=CheckoutResponse)
def start_subscription(
    body: CheckoutRequest,
    session: SessionDep,
    current_user: CurrentUser,
    profile: CurrentProfile,
    gateway: GatewayDep,
) -> Any:
    """Create a PayPal subscription. Benefits only start after the webhook."""
    plan = session.exec(
        select(MembershipPlan).where(MembershipPlan.code == body.plan_code)
    ).first()
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="Plan not found")
    is_client_plan = plan.tier == Tier.CLIENT
    if is_client_plan != (profile.role == ProfileRole.CLIENT):
        raise HTTPException(
            status_code=403, detail="Plan is not available for your profile"
        )
    if not plan.paypal_plan_id:
        raise HTTPException(status_code=409, detail="This plan is not yet available")
    try:
        paypal_id, approve_url = gateway.create_subscription(
            plan_id=plan.paypal_plan_id,
            return_url=f"{settings.FRONTEND_HOST}/billing?status=approved",
            cancel_url=f"{settings.FRONTEND_HOST}/billing?status=cancelled",
        )
    except PayPalError:
        raise HTTPException(
            status_code=502, detail="PayPal could not start the subscription"
        )
    sub = Subscription(
        user_id=current_user.id, plan_id=plan.id, paypal_subscription_id=paypal_id
    )
    session.add(sub)
    audit.record(
        session,
        actor_id=current_user.id,
        action="subscription.checkout_started",
        entity_type="subscription",
        entity_id=sub.id,
        detail={"plan": plan.code},
    )
    session.commit()
    return CheckoutResponse(subscription_id=sub.id, approval_url=approve_url)


class SubscriptionPublic(SQLModel):
    id: uuid.UUID
    plan_code: str
    plan_name: str
    status: str
    is_trial: bool
    trial_start: Any = None
    trial_end: Any = None
    renewal_date: Any = None
    grants_benefits: bool


class PaymentHistoryItem(SQLModel):
    amount_cents: int
    status: str
    occurred_at: Any


class BillingSummary(SQLModel):
    current: SubscriptionPublic | None
    subscriptions: list[SubscriptionPublic]
    payments: list[PaymentHistoryItem]


def _public(sub: Subscription, plan: MembershipPlan) -> SubscriptionPublic:
    from datetime import UTC, datetime

    trial_end = sub.trial_end
    if trial_end is not None and trial_end.tzinfo is None:
        trial_end = trial_end.replace(tzinfo=UTC)
    ok = entitlements.grants_benefits(sub)
    return SubscriptionPublic(
        id=sub.id,
        plan_code=plan.code,
        plan_name=plan.name,
        status=sub.status,
        is_trial=bool(
            sub.status == SubscriptionStatus.ACTIVE
            and trial_end
            and trial_end > datetime.now(UTC)
        ),
        trial_start=sub.trial_start,
        trial_end=sub.trial_end,
        renewal_date=sub.current_period_end,
        grants_benefits=ok,
    )


@router.get("/subscriptions/me", response_model=BillingSummary)
def billing_summary(session: SessionDep, current_user: CurrentUser) -> Any:
    rows = session.exec(
        select(Subscription, MembershipPlan)
        .where(Subscription.user_id == current_user.id)
        .where(Subscription.plan_id == MembershipPlan.id)
        .order_by(col(Subscription.created_at).desc())
    ).all()
    subs = [_public(s, p) for s, p in rows]
    payments = session.exec(
        select(SubscriptionPayment)
        .where(col(SubscriptionPayment.subscription_id).in_([s.id for s, _ in rows]))
        .order_by(col(SubscriptionPayment.occurred_at).desc())
    ).all()
    return BillingSummary(
        current=next((s for s in subs if s.grants_benefits), None),
        subscriptions=subs,
        payments=[PaymentHistoryItem(**p.model_dump()) for p in payments],
    )


@router.post("/subscriptions/{subscription_id}/cancel")
def cancel_subscription(
    subscription_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    gateway: GatewayDep,
) -> Any:
    """Ask PayPal to cancel; local status changes on PayPal's webhook."""
    sub = session.get(Subscription, subscription_id)
    if sub is None or sub.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if sub.status != SubscriptionStatus.ACTIVE:
        raise HTTPException(status_code=409, detail="Subscription is not active")
    try:
        gateway.cancel_subscription(sub.paypal_subscription_id, "Cancelled by member")
    except PayPalError:
        raise HTTPException(
            status_code=502, detail="PayPal could not cancel the subscription"
        )
    audit.record(
        session,
        actor_id=current_user.id,
        action="subscription.cancel_requested",
        entity_type="subscription",
        entity_id=sub.id,
    )
    session.commit()
    return {
        "message": "Cancellation requested. Access continues until the period ends."
    }
