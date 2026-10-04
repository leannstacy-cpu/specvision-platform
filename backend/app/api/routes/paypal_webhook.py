import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import Session

from app.api.deps import SessionDep
from app.marketplace.paypal import PayPalError, PayPalGateway, get_paypal_gateway
from app.marketplace.webhooks import MalformedEvent, ingest

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def get_webhook_gateway() -> PayPalGateway:
    try:
        return get_paypal_gateway()
    except PayPalError:
        raise HTTPException(status_code=503, detail="Webhooks are not configured")


@router.post("/paypal")
async def paypal_webhook(request: Request, session: SessionDep) -> Any:
    """Receive a PayPal event. Unsigned or unverifiable requests are rejected
    before anything is stored or processed."""
    gateway = request.app.dependency_overrides.get(
        get_webhook_gateway, get_webhook_gateway
    )()
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not gateway.verify_webhook_signature(dict(request.headers), event):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return {"result": _process(session, gateway, event, raw)}


def _process(
    session: Session, gateway: PayPalGateway, event: dict[str, Any], raw: str
) -> str:
    try:
        return ingest(session, gateway, event, raw)
    except MalformedEvent:
        raise HTTPException(status_code=400, detail="Malformed event")
    except Exception:
        # Event is stored as failed; a 500 makes PayPal retry delivery.
        raise HTTPException(status_code=500, detail="Event processing failed")
