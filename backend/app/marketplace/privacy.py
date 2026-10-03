"""Server-side project privacy.

Protected fields are never placed on the object returned to an unauthorized
caller; they are not merely hidden in the UI.
"""

import uuid
from typing import Any

from sqlmodel import Session, SQLModel, select

from app.marketplace.enums import LeadUnlockStatus
from app.marketplace.models import Bid, LeadUnlock, Project


class ProjectPreview(SQLModel):
    """What a professional sees before a verified lead unlock."""

    id: uuid.UUID
    title: str
    description: str
    category: str
    project_type: str
    service_area: str
    budget_min_cents: int | None
    budget_max_cents: int | None
    desired_timeline: str | None
    specialties: list[str]
    is_remote: bool
    is_onsite: bool
    is_urgent: bool
    is_after_hours: bool
    status: str
    unlocked: bool = False


class ProjectProtected(ProjectPreview):
    contact_name: str | None = None
    company_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    street_address: str | None = None
    website: str | None = None


class ProjectOwnerView(ProjectProtected):
    privacy_requested: bool
    modifier_codes: list[str]
    awarded_bid_id: uuid.UUID | None


def is_unlocked(session: Session, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        session.exec(
            select(LeadUnlock.id)
            .where(LeadUnlock.project_id == project_id)
            .where(LeadUnlock.professional_id == user_id)
            .where(LeadUnlock.status == LeadUnlockStatus.PAID)
        ).first()
        is not None
    )


def _is_award_winner(session: Session, project: Project, user_id: uuid.UUID) -> bool:
    if project.awarded_bid_id is None:
        return False
    bid = session.get(Bid, project.awarded_bid_id)
    return bid is not None and bid.professional_id == user_id


def _preview_data(project: Project) -> dict[str, Any]:
    return {
        f: getattr(project, f) for f in ProjectPreview.model_fields if f != "unlocked"
    }


def preview_for_professional(project: Project) -> ProjectPreview:
    return ProjectPreview(**_preview_data(project))


def view_for_user(
    session: Session, project: Project, *, user_id: uuid.UUID, is_admin: bool
) -> ProjectPreview | ProjectProtected | ProjectOwnerView:
    """Pick the single representation this user is allowed to receive."""
    if project.owner_id == user_id or is_admin:
        return ProjectOwnerView(
            **_preview_data(project),
            unlocked=True,
            contact_name=project.contact_name,
            company_name=project.company_name,
            contact_email=project.contact_email,
            contact_phone=project.contact_phone,
            street_address=project.street_address,
            website=project.website,
            privacy_requested=project.privacy_requested,
            modifier_codes=project.modifier_codes,
            awarded_bid_id=project.awarded_bid_id,
        )
    if is_unlocked(session, project.id, user_id):
        protected = ProjectProtected(**_preview_data(project), unlocked=True)
        protected.contact_email = project.contact_email
        protected.contact_phone = project.contact_phone
        protected.street_address = project.street_address
        protected.website = project.website
        # Identity stays private when requested, until the client awards the
        # project to this professional.
        if not project.privacy_requested or _is_award_winner(session, project, user_id):
            protected.contact_name = project.contact_name
            protected.company_name = project.company_name
        return protected
    return preview_for_professional(project)
