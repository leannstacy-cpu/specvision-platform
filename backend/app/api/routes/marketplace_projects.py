import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import SQLModel, col, select

from app.api.deps import CurrentProfile, CurrentUser, GatewayDep, SessionDep
from app.api.routes.marketplace_billing import build_quote
from app.marketplace import audit, entitlements
from app.marketplace.contact_scanner import describe_findings
from app.marketplace.enums import (
    BidStatus,
    Category,
    LeadUnlockStatus,
    ProfileRole,
    ProjectStatus,
    Tier,
)
from app.marketplace.models import Bid, LeadUnlock, Profile, Project
from app.marketplace.paypal import PayPalError
from app.marketplace.privacy import (
    ProjectOwnerView,
    ProjectPreview,
    preview_for_professional,
    view_for_user,
)

router = APIRouter(prefix="/projects", tags=["marketplace-projects"])

PROJECT_TYPES = {
    "standard", "urgent_request", "on_site_assessment", "after_hours_service",
    "remote_it_support", "consultation", "system_design", "installation",
    "troubleshooting", "maintenance", "upgrade", "managed_service_request",
    "multi_location_business_project", "request_for_proposal",
    "ongoing_b2b_service_relationship",
}  # fmt: skip

# Pro Plus and Elite members see new projects first.
EARLY_ACCESS_WINDOW = timedelta(hours=1)


class ProjectCreate(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    description: str = Field(min_length=10, max_length=10_000)
    category: Category
    project_type: str
    service_area: str = Field(min_length=2, max_length=128)
    budget_min_cents: int | None = Field(default=None, ge=0)
    budget_max_cents: int | None = Field(default=None, ge=0)
    desired_timeline: str | None = Field(default=None, max_length=128)
    specialties: list[str] = Field(default_factory=list, max_length=20)
    is_remote: bool = False
    is_onsite: bool = False
    is_urgent: bool = False
    is_after_hours: bool = False
    modifier_codes: list[str] = Field(default_factory=list, max_length=10)
    privacy_requested: bool = True
    contact_name: str | None = Field(default=None, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=64)
    street_address: str | None = Field(default=None, max_length=255)
    website: str | None = Field(default=None, max_length=255)


def _get_project_or_404(session: SessionDep, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _owner_or_404(
    session: SessionDep, project_id: uuid.UUID, user: CurrentUser
) -> Project:
    project = _get_project_or_404(session, project_id)
    if project.owner_id != user.id and not user.is_superuser:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/", response_model=ProjectOwnerView)
def create_project(
    body: ProjectCreate,
    session: SessionDep,
    current_user: CurrentUser,
    profile: CurrentProfile,
) -> Any:
    if profile.role != ProfileRole.CLIENT or not entitlements.is_active_client(
        session, current_user.id
    ):
        raise HTTPException(
            status_code=403, detail="An active client membership is required"
        )
    if body.project_type not in PROJECT_TYPES:
        raise HTTPException(status_code=422, detail="Unknown project type")
    if (
        body.budget_min_cents is not None
        and body.budget_max_cents is not None
        and body.budget_min_cents > body.budget_max_cents
    ):
        raise HTTPException(status_code=422, detail="Invalid budget range")
    problem = describe_findings(
        body.title,
        body.description,
        body.service_area,
        body.desired_timeline,
        *body.specialties,
    )
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    build_quote(session, current_user.id, body.modifier_codes)  # validates codes
    project = Project(owner_id=current_user.id, **body.model_dump())
    session.add(project)
    audit.record(
        session,
        actor_id=current_user.id,
        action="project.created",
        entity_type="project",
        entity_id=project.id,
    )
    session.commit()
    session.refresh(project)
    return view_for_user(session, project, user_id=current_user.id, is_admin=False)


@router.get("/mine", response_model=list[ProjectOwnerView])
def my_projects(session: SessionDep, current_user: CurrentUser) -> Any:
    projects = session.exec(
        select(Project)
        .where(Project.owner_id == current_user.id)
        .order_by(col(Project.created_at).desc())
    ).all()
    return [
        view_for_user(session, p, user_id=current_user.id, is_admin=False)
        for p in projects
    ]


@router.get("/", response_model=list[ProjectPreview])
def browse_projects(
    session: SessionDep,
    current_user: CurrentUser,
    category: Category | None = None,
    service_area: str | None = None,
    project_type: str | None = None,
    skip: int = 0,
    limit: int = Query(default=50, le=100),
) -> Any:
    """Sanitized listings for professionals. Never includes protected fields."""
    if not entitlements.has_feature(session, current_user.id, "browse_projects"):
        raise HTTPException(
            status_code=403, detail="A professional membership is required"
        )
    tier = entitlements.active_tier(session, current_user.id)
    stmt = select(Project).where(Project.status == ProjectStatus.OPEN)
    if category:
        stmt = stmt.where(Project.category == category)
    if service_area:
        stmt = stmt.where(col(Project.service_area).ilike(f"%{service_area}%"))
    if project_type:
        stmt = stmt.where(Project.project_type == project_type)
    if tier == Tier.PROFESSIONAL:
        stmt = stmt.where(
            col(Project.created_at) <= datetime.now(UTC) - EARLY_ACCESS_WINDOW
        )
    projects = session.exec(
        stmt.order_by(col(Project.created_at).desc()).offset(skip).limit(limit)
    ).all()
    return [preview_for_professional(p) for p in projects]


@router.get("/{project_id}", response_model=None)
def read_project(
    project_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    project = _get_project_or_404(session, project_id)
    is_owner = project.owner_id == current_user.id
    if not (
        is_owner
        or current_user.is_superuser
        or entitlements.has_feature(session, current_user.id, "browse_projects")
    ):
        raise HTTPException(status_code=404, detail="Project not found")
    return view_for_user(
        session, project, user_id=current_user.id, is_admin=current_user.is_superuser
    )


class BidWrite(BaseModel):
    amount_cents: int = Field(ge=0)
    timeline_days: int = Field(ge=1, le=3650)
    message: str = Field(min_length=10, max_length=5000)


class BidPublic(SQLModel):
    id: uuid.UUID
    project_id: uuid.UUID
    professional_id: uuid.UUID
    professional_name: str | None = None
    professional_category: str | None = None
    amount_cents: int
    timeline_days: int
    message: str
    status: str
    revision: int


def _bid_out(session: SessionDep, bid: Bid) -> BidPublic:
    prof = session.exec(
        select(Profile).where(Profile.user_id == bid.professional_id)
    ).first()
    return BidPublic(
        **bid.model_dump(),
        professional_name=(prof.company_name or prof.display_name) if prof else None,
        professional_category=prof.category if prof else None,
    )


@router.post("/{project_id}/bids", response_model=BidPublic)
def submit_bid(
    project_id: uuid.UUID,
    body: BidWrite,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Create or revise a preliminary bid (no protected data is returned)."""
    if not entitlements.has_feature(session, current_user.id, "submit_bids"):
        raise HTTPException(
            status_code=403, detail="A professional membership is required"
        )
    project = _get_project_or_404(session, project_id)
    if project.owner_id == current_user.id or project.status != ProjectStatus.OPEN:
        raise HTTPException(status_code=404, detail="Project not found")
    problem = describe_findings(body.message)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    bid = session.exec(
        select(Bid)
        .where(Bid.project_id == project_id)
        .where(Bid.professional_id == current_user.id)
    ).first()
    if bid is None:
        bid = Bid(
            project_id=project_id, professional_id=current_user.id, **body.model_dump()
        )
    else:
        if bid.status in (BidStatus.WON, BidStatus.DECLINED):
            raise HTTPException(
                status_code=409, detail="This bid can no longer be revised"
            )
        for k, v in body.model_dump().items():
            setattr(bid, k, v)
        bid.revision += 1
        bid.updated_at = datetime.now(UTC)
    session.add(bid)
    session.commit()
    session.refresh(bid)
    return _bid_out(session, bid)


@router.get("/{project_id}/bids", response_model=list[BidPublic])
def list_bids(
    project_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """Owners compare bids; a professional only ever sees their own bid."""
    project = _get_project_or_404(session, project_id)
    stmt = select(Bid).where(Bid.project_id == project_id)
    if project.owner_id != current_user.id and not current_user.is_superuser:
        stmt = stmt.where(Bid.professional_id == current_user.id)
    return [
        _bid_out(session, b) for b in session.exec(stmt.order_by(col(Bid.amount_cents)))
    ]


@router.post("/{project_id}/bids/{bid_id}/shortlist", response_model=BidPublic)
def shortlist_bid(
    project_id: uuid.UUID,
    bid_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    project = _owner_or_404(session, project_id, current_user)
    bid = session.get(Bid, bid_id)
    if bid is None or bid.project_id != project.id:
        raise HTTPException(status_code=404, detail="Bid not found")
    if bid.status != BidStatus.SUBMITTED:
        raise HTTPException(status_code=409, detail="Bid cannot be shortlisted")
    bid.status = BidStatus.SHORTLISTED
    session.add(bid)
    session.commit()
    session.refresh(bid)
    return _bid_out(session, bid)


@router.post("/{project_id}/award/{bid_id}", response_model=ProjectOwnerView)
def award_project(
    project_id: uuid.UUID,
    bid_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    project = _owner_or_404(session, project_id, current_user)
    bid = session.get(Bid, bid_id)
    if bid is None or bid.project_id != project.id:
        raise HTTPException(status_code=404, detail="Bid not found")
    if project.status != ProjectStatus.OPEN:
        raise HTTPException(status_code=409, detail="Project is not open")
    for other in session.exec(select(Bid).where(Bid.project_id == project.id)):
        other.status = BidStatus.WON if other.id == bid.id else BidStatus.DECLINED
        session.add(other)
    project.status = ProjectStatus.AWARDED
    project.awarded_bid_id = bid.id
    session.add(project)
    audit.record(
        session,
        actor_id=current_user.id,
        action="project.awarded",
        entity_type="project",
        entity_id=project.id,
        detail={"bid_id": str(bid.id)},
    )
    session.commit()
    session.refresh(project)
    return view_for_user(session, project, user_id=current_user.id, is_admin=False)


class UnlockResponse(SQLModel):
    unlock_id: uuid.UUID
    fee_cents: int
    paypal_order_id: str | None
    status: str


@router.post("/{project_id}/unlock", response_model=UnlockResponse)
def start_lead_unlock(
    project_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    gateway: GatewayDep,
) -> Any:
    """Create the PayPal order for the lead fee. Access starts only after the
    verified capture webhook; the fee is always taken from the member's plan."""
    if not entitlements.has_feature(session, current_user.id, "submit_bids"):
        raise HTTPException(
            status_code=403, detail="A professional membership is required"
        )
    project = _get_project_or_404(session, project_id)
    if project.owner_id == current_user.id or project.status != ProjectStatus.OPEN:
        raise HTTPException(status_code=404, detail="Project not found")
    bid = session.exec(
        select(Bid)
        .where(Bid.project_id == project_id)
        .where(Bid.professional_id == current_user.id)
    ).first()
    if bid is None or bid.status != BidStatus.SHORTLISTED:
        raise HTTPException(
            status_code=403,
            detail="The client must shortlist your bid before you can unlock this project",
        )
    fee = entitlements.lead_fee_cents(session, current_user.id)
    if not fee:
        raise HTTPException(status_code=409, detail="Lead fee is not configured")
    unlock = session.exec(
        select(LeadUnlock)
        .where(LeadUnlock.project_id == project_id)
        .where(LeadUnlock.professional_id == current_user.id)
    ).first()
    if unlock is not None and unlock.status == LeadUnlockStatus.PAID:
        raise HTTPException(status_code=409, detail="Project already unlocked")
    if unlock is None:
        unlock = LeadUnlock(
            project_id=project_id, professional_id=current_user.id, fee_cents=fee
        )
        session.add(unlock)
        session.flush()
    unlock.fee_cents = fee
    unlock.status = LeadUnlockStatus.PENDING
    try:
        unlock.paypal_order_id = gateway.create_order(
            amount_cents=fee,
            reference_id=f"{unlock.id}-{uuid.uuid4().hex[:8]}"[:127],
            description="SPECVision lead unlock",
        )
    except PayPalError:
        raise HTTPException(status_code=502, detail="PayPal could not create the order")
    session.add(unlock)
    audit.record(
        session,
        actor_id=current_user.id,
        action="lead_unlock.started",
        entity_type="lead_unlock",
        entity_id=unlock.id,
        detail={"fee_cents": fee},
    )
    session.commit()
    return UnlockResponse(
        unlock_id=unlock.id,
        fee_cents=fee,
        paypal_order_id=unlock.paypal_order_id,
        status=unlock.status,
    )


@router.get("/{project_id}/unlock", response_model=UnlockResponse)
def lead_unlock_status(
    project_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    unlock = session.exec(
        select(LeadUnlock)
        .where(LeadUnlock.project_id == project_id)
        .where(LeadUnlock.professional_id == current_user.id)
    ).first()
    if unlock is None:
        raise HTTPException(status_code=404, detail="No unlock found")
    return UnlockResponse(
        unlock_id=unlock.id,
        fee_cents=unlock.fee_cents,
        paypal_order_id=unlock.paypal_order_id,
        status=unlock.status,
    )
