import uuid
from typing import Any

from sqlmodel import Session

from app.marketplace.models import AuditLog


def record(
    session: Session,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: object | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an audit log row to the session (committed with the caller's work)."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail or {},
    )
    session.add(entry)
    return entry
