import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import SQLModel, select

from app.api.deps import CurrentUser, GatewayDep, SessionDep
from app.marketplace import audit
from app.marketplace.enums import ProjectStatus
from app.marketplace.models import Bid, Milestone, Payment, Project, SellerAccount
from app.marketplace.payments import PaymentError, approve_milestone, fund_milestone

router = APIRouter(tags=["marketplace-payments"])


class MilestoneCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    amount_cents: int = Field(gt=0, le=100_000_000)


class MilestonePublic(SQLModel):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    amount_cents: int
    status: str


def _http(exc: PaymentError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _owned_project(
    session: SessionDep, project_id: uuid.UUID, user: CurrentUser
) -> Project:
    project = session.get(Project, project_id)
    if project is None or (project.owner_id != user.id and not user.is_superuser):
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/projects/{project_id}/milestones", response_model=MilestonePublic)
def create_milestone(
    project_id: uuid.UUID,
    body: MilestoneCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    project = _owned_project(session, project_id, current_user)
    bid = session.get(Bid, project.awarded_bid_id) if project.awarded_bid_id else None
    if project.status != ProjectStatus.AWARDED or bid is None:
        raise HTTPException(
            status_code=409, detail="Award the project before adding milestones"
        )
    milestone = Milestone(
        project_id=project.id,
        seller_id=bid.professional_id,
        title=body.title,
        amount_cents=body.amount_cents,
    )
    session.add(milestone)
    session.commit()
    session.refresh(milestone)
    return milestone


class FundRequest(BaseModel):
    accept_direct_payment: bool = False


class FundResponse(SQLModel):
    payment_id: uuid.UUID
    paypal_order_id: str
    disbursement_mode: str
    notice: str


@router.post("/milestones/{milestone_id}/fund", response_model=FundResponse)
def fund(
    milestone_id: uuid.UUID,
    body: FundRequest,
    session: SessionDep,
    current_user: CurrentUser,
    gateway: GatewayDep,
) -> Any:
    milestone = session.get(Milestone, milestone_id)
    if milestone is None:
        raise HTTPException(status_code=404, detail="Milestone not found")
    project = _owned_project(session, milestone.project_id, current_user)
    try:
        payment, order_id = fund_milestone(
            session,
            gateway,
            milestone=milestone,
            project=project,
            payer_id=current_user.id,
            accept_direct_payment=body.accept_direct_payment,
        )
    except PaymentError as exc:
        raise _http(exc)
    notice = (
        "Protected project payment: the payment is held for scheduled release "
        "once PayPal confirms it."
        if payment.disbursement_mode == "delayed"
        else "Direct payment: funds go straight to the professional and are not held."
    )
    return FundResponse(
        payment_id=payment.id,
        paypal_order_id=order_id,
        disbursement_mode=payment.disbursement_mode,
        notice=notice,
    )


@router.post("/milestones/{milestone_id}/approve", response_model=MilestonePublic)
def approve(
    milestone_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    gateway: GatewayDep,
) -> Any:
    milestone = session.get(Milestone, milestone_id)
    if milestone is None:
        raise HTTPException(status_code=404, detail="Milestone not found")
    project = _owned_project(session, milestone.project_id, current_user)
    try:
        approve_milestone(
            session,
            gateway,
            milestone=milestone,
            project=project,
            actor_id=current_user.id,
            actor_is_admin=current_user.is_superuser,
        )
    except PaymentError as exc:
        raise _http(exc)
    session.refresh(milestone)
    return milestone


class SellerStatus(SQLModel):
    onboarding_complete: bool
    payments_receivable: bool
    delayed_disbursement_enabled: bool


@router.get("/seller/status", response_model=SellerStatus)
def seller_status(session: SessionDep, current_user: CurrentUser) -> Any:
    seller = session.exec(
        select(SellerAccount).where(SellerAccount.user_id == current_user.id)
    ).first()
    return seller or SellerStatus(
        onboarding_complete=False,
        payments_receivable=False,
        delayed_disbursement_enabled=False,
    )


@router.post("/seller/onboard", status_code=501)
def seller_onboard(session: SessionDep, current_user: CurrentUser) -> Any:
    """PayPal partner referral link creation is not implemented yet."""
    audit.record(
        session,
        actor_id=current_user.id,
        action="seller.onboard_attempt",
        entity_type="seller",
    )
    session.commit()
    raise HTTPException(
        status_code=501,
        detail="Seller onboarding through PayPal partner referrals is not available yet",
    )


class PaymentPublic(SQLModel):
    id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: uuid.UUID | None
    amount_cents: int
    status: str
    disbursement_mode: str


@router.get("/projects/{project_id}/payments", response_model=list[PaymentPublic])
def project_payments(
    project_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    stmt = select(Payment).where(Payment.project_id == project_id)
    if project.owner_id != current_user.id and not current_user.is_superuser:
        stmt = stmt.where(Payment.seller_id == current_user.id)
    return session.exec(stmt).all()
