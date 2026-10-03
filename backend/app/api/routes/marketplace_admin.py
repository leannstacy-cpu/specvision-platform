import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import SQLModel, col, select

from app.api.deps import (
    CurrentUser,
    GatewayDep,
    SessionDep,
    get_current_active_superuser,
)
from app.marketplace import audit
from app.marketplace.enums import Interval, Tier
from app.marketplace.models import (
    AuditLog,
    FeatureFlag,
    MembershipPlan,
    Payment,
    PlatformSetting,
    ProjectModifier,
    WebhookEvent,
)
from app.marketplace.payments import (
    PaymentError,
    release_deadline_queue,
    request_refund,
)
from app.marketplace.remote_support import REQUIREMENTS, get_remote_support_provider
from app.marketplace.webhooks import ingest

router = APIRouter(
    prefix="/admin",
    tags=["marketplace-admin"],
    dependencies=[Depends(get_current_active_superuser)],
)


class WebhookEventPublic(SQLModel):
    id: uuid.UUID
    paypal_event_id: str
    event_type: str
    environment: str
    status: str
    attempts: int
    error: str | None
    received_at: Any
    processed_at: Any


@router.get("/webhook-events", response_model=list[WebhookEventPublic])
def webhook_events(
    session: SessionDep,
    status: str | None = None,
    skip: int = 0,
    limit: int = Query(default=50, le=200),
) -> Any:
    stmt = select(WebhookEvent)
    if status:
        stmt = stmt.where(WebhookEvent.status == status)
    return session.exec(
        stmt.order_by(col(WebhookEvent.received_at).desc()).offset(skip).limit(limit)
    ).all()


@router.post("/webhook-events/{event_id}/reprocess")
def reprocess_event(
    event_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    gateway: GatewayDep,
) -> Any:
    row = session.get(WebhookEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    import json

    audit.record(
        session,
        actor_id=current_user.id,
        action="webhook.reprocess",
        entity_type="webhook_event",
        entity_id=row.id,
    )
    session.commit()
    try:
        result = ingest(session, gateway, json.loads(row.raw_body), row.raw_body)
    except Exception:
        raise HTTPException(
            status_code=500, detail="Reprocessing failed; see event error"
        )
    return {"result": result}


class PaymentAdmin(SQLModel):
    id: uuid.UUID
    project_id: uuid.UUID
    amount_cents: int
    status: str
    disbursement_mode: str
    release_deadline: Any


@router.get("/payments/release-queue", response_model=list[PaymentAdmin])
def release_queue(session: SessionDep) -> Any:
    """Held payments nearing their PayPal release deadline."""
    return release_deadline_queue(session)


@router.get("/payments", response_model=list[PaymentAdmin])
def all_payments(
    session: SessionDep, status: str | None = None, limit: int = 100
) -> Any:
    stmt = select(Payment)
    if status:
        stmt = stmt.where(Payment.status == status)
    return session.exec(stmt.limit(min(limit, 500))).all()


@router.post("/payments/{payment_id}/refund")
def refund_payment(
    payment_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    gateway: GatewayDep,
) -> Any:
    payment = session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    try:
        request_refund(
            session,
            gateway,
            payment=payment,
            actor_id=current_user.id,
            actor_is_admin=True,
        )
    except PaymentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"message": "Refund requested; status updates when PayPal confirms."}


class FlagPublic(SQLModel):
    key: str
    enabled: bool
    description: str


class ModifierAdmin(SQLModel):
    code: str
    label: str
    price_cents: int
    is_active: bool


class AuditLogPublic(SQLModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    detail: dict[str, Any]
    created_at: Any


class FlagUpdate(BaseModel):
    enabled: bool


@router.get("/feature-flags", response_model=list[FlagPublic])
def feature_flags(session: SessionDep) -> Any:
    return session.exec(select(FeatureFlag)).all()


@router.put("/feature-flags/{key}", response_model=FlagPublic)
def set_feature_flag(
    key: str, body: FlagUpdate, session: SessionDep, current_user: CurrentUser
) -> Any:
    flag = session.get(FeatureFlag, key)
    if flag is None:
        raise HTTPException(status_code=404, detail="Unknown feature flag")
    flag.enabled = body.enabled
    session.add(flag)
    audit.record(
        session,
        actor_id=current_user.id,
        action="feature_flag.set",
        entity_type="feature_flag",
        entity_id=key,
        detail={"enabled": body.enabled},
    )
    session.commit()
    return flag


class PlanUpdate(BaseModel):
    paypal_plan_id: str | None = Field(default=None, max_length=64)
    paypal_product_id: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    lead_fee_cents: int | None = Field(default=None, ge=0)


@router.put("/plans/{code}")
def update_plan(
    code: str, body: PlanUpdate, session: SessionDep, current_user: CurrentUser
) -> Any:
    plan = session.exec(
        select(MembershipPlan).where(MembershipPlan.code == code)
    ).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    changes = body.model_dump(exclude_unset=True)
    if plan.tier == Tier.CLIENT and changes.get("lead_fee_cents") is not None:
        raise HTTPException(status_code=422, detail="Client plans have no lead fee")
    if (
        "paypal_plan_id" in changes
        and changes["paypal_plan_id"]
        and session.exec(
            select(MembershipPlan)
            .where(MembershipPlan.paypal_plan_id == changes["paypal_plan_id"])
            .where(MembershipPlan.id != plan.id)
        ).first()
    ):
        raise HTTPException(status_code=409, detail="PayPal plan already mapped")
    for k, v in changes.items():
        setattr(plan, k, v)
    session.add(plan)
    audit.record(
        session,
        actor_id=current_user.id,
        action="plan.updated",
        entity_type="plan",
        entity_id=plan.code,
        detail=changes,
    )
    session.commit()
    session.refresh(plan)
    return plan


class PlanCreate(BaseModel):
    code: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = Field(max_length=128)
    tier: Tier
    interval: Interval
    price_cents: int = Field(ge=0)
    lead_fee_cents: int | None = Field(default=None, ge=0)
    trial_days: int = Field(default=7, ge=0, le=365)


@router.post("/plans")
def create_plan(
    body: PlanCreate, session: SessionDep, current_user: CurrentUser
) -> Any:
    """Additional plans exist only when an administrator explicitly adds them."""
    if session.exec(
        select(MembershipPlan).where(MembershipPlan.code == body.code)
    ).first():
        raise HTTPException(status_code=409, detail="Plan code already exists")
    plan = MembershipPlan(**body.model_dump())
    session.add(plan)
    audit.record(
        session,
        actor_id=current_user.id,
        action="plan.created",
        entity_type="plan",
        entity_id=body.code,
    )
    session.commit()
    session.refresh(plan)
    return plan


class ModifierUpdate(BaseModel):
    price_cents: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


@router.put("/modifiers/{code}", response_model=ModifierAdmin)
def update_modifier(
    code: str, body: ModifierUpdate, session: SessionDep, current_user: CurrentUser
) -> Any:
    mod = session.exec(
        select(ProjectModifier).where(ProjectModifier.code == code)
    ).first()
    if mod is None:
        raise HTTPException(status_code=404, detail="Modifier not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(mod, k, v)
    session.add(mod)
    audit.record(
        session,
        actor_id=current_user.id,
        action="modifier.updated",
        entity_type="modifier",
        entity_id=code,
        detail=changes,
    )
    session.commit()
    session.refresh(mod)
    return mod


class FeeUpdate(BaseModel):
    basis_points: int = Field(ge=0, le=10_000)


@router.put("/settings/marketplace-fee")
def set_marketplace_fee(
    body: FeeUpdate, session: SessionDep, current_user: CurrentUser
) -> Any:
    setting = session.get(PlatformSetting, "marketplace_fee_bps")
    if setting is None:
        setting = PlatformSetting(key="marketplace_fee_bps", value="0")
    setting.value = str(body.basis_points)
    session.add(setting)
    audit.record(
        session,
        actor_id=current_user.id,
        action="setting.marketplace_fee",
        entity_type="setting",
        entity_id="marketplace_fee_bps",
        detail={"basis_points": body.basis_points},
    )
    session.commit()
    return {"basis_points": body.basis_points}


@router.get("/audit-logs", response_model=list[AuditLogPublic])
def audit_logs(
    session: SessionDep,
    action: str | None = None,
    limit: int = Query(default=100, le=500),
) -> Any:
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return session.exec(
        stmt.order_by(col(AuditLog.created_at).desc()).limit(limit)
    ).all()


@router.get("/remote-support")
def remote_support_config(session: SessionDep) -> Any:
    flag = session.get(FeatureFlag, "remote_support")
    provider = get_remote_support_provider()
    return {
        "enabled": bool(flag and flag.enabled),
        "provider": provider.name,
        "operational": provider.name != "placeholder",
        "requirements": REQUIREMENTS,
    }
