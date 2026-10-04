import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import crud
from app.api.deps import get_gateway
from app.api.routes.paypal_webhook import get_webhook_gateway
from app.main import app
from app.marketplace.enums import SubscriptionStatus
from app.marketplace.models import MembershipPlan, Profile, Subscription
from app.marketplace.paypal import PayPalError
from app.models import User, UserCreate
from tests.utils.user import user_authentication_headers
from tests.utils.utils import random_email, random_lower_string


class FakeGateway:
    """Recording test double for PayPal (tests only; never used in app code)."""

    def __init__(self) -> None:
        self.signature_valid = True
        self.fail = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    def _maybe_fail(self) -> None:
        if self.fail:
            raise PayPalError("boom")

    def verify_webhook_signature(
        self, headers: dict[str, str], event: dict[str, Any]
    ) -> bool:
        return self.signature_valid

    def create_subscription(self, **kw: Any) -> tuple[str, str]:
        self._maybe_fail()
        self.calls.append(("create_subscription", kw))
        sid = self._next("I-SUB")
        return sid, f"https://paypal.test/approve/{sid}"

    def cancel_subscription(self, subscription_id: str, reason: str) -> None:
        self._maybe_fail()
        self.calls.append(("cancel_subscription", {"id": subscription_id}))

    def create_order(self, **kw: Any) -> str:
        self._maybe_fail()
        self.calls.append(("create_order", kw))
        return self._next("ORDER")

    def capture_order(self, order_id: str) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("capture_order", {"order_id": order_id}))
        return {}

    def refund_capture(self, capture_id: str) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("refund_capture", {"capture_id": capture_id}))
        return {}

    def release_funds(self, **kw: Any) -> dict[str, Any]:
        self._maybe_fail()
        self.calls.append(("release_funds", kw))
        return {}

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def gateway() -> Generator[FakeGateway]:
    gw = FakeGateway()
    app.dependency_overrides[get_gateway] = lambda: gw
    app.dependency_overrides[get_webhook_gateway] = lambda: gw
    yield gw
    app.dependency_overrides.pop(get_gateway, None)
    app.dependency_overrides.pop(get_webhook_gateway, None)


class Actor:
    def __init__(self, user: User, headers: dict[str, str]) -> None:
        self.user = user
        self.headers = headers
        self.id = user.id


def make_actor(
    client: TestClient,
    db: Session,
    *,
    role: str | None = None,
    category: str | None = None,
    plan: str | None = None,
    display_name: str | None = None,
) -> Actor:
    password = random_lower_string()
    email = random_email()
    user_in = UserCreate(email=email, password=password)
    user = crud.create_user(session=db, user_create=user_in)
    headers = user_authentication_headers(client=client, email=email, password=password)
    if role:
        db.add(
            Profile(
                user_id=user.id,
                role=role,
                display_name=display_name or "Demo " + random_lower_string()[:6],
                category=category,
            )
        )
        db.commit()
    if plan:
        grant_membership(db, user, plan)
    return Actor(user, headers)


def grant_membership(
    db: Session,
    user: User,
    plan_code: str,
    status: str = SubscriptionStatus.ACTIVE,
    period_days: int = 30,
) -> Subscription:
    plan = db.exec(select(MembershipPlan).where(MembershipPlan.code == plan_code)).one()
    now = datetime.now(UTC)
    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        paypal_subscription_id="I-" + uuid.uuid4().hex[:12],
        status=status,
        trial_start=now,
        trial_end=now + timedelta(days=7),
        current_period_end=now + timedelta(days=period_days),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def project_payload(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Conference room AV upgrade",
        "description": "Replace displays and install a ceiling microphone array.",
        "category": "audiovisual",
        "project_type": "upgrade",
        "service_area": "Austin, TX metro",
        "budget_min_cents": 500000,
        "budget_max_cents": 900000,
        "desired_timeline": "Within 6 weeks",
        "specialties": ["conferencing"],
        "is_onsite": True,
        "privacy_requested": True,
        "contact_name": "Jane Secret",
        "company_name": "Secret Corp",
        "contact_email": "jane@secretcorp.example",
        "contact_phone": "512-555-0142",
        "street_address": "100 Congress Ave",
        "website": "https://secretcorp.example",
    }
    data.update(overrides)
    return data


def event(
    event_type: str, resource: dict[str, Any], event_id: str | None = None
) -> dict[str, Any]:
    return {
        "id": event_id or "WH-" + uuid.uuid4().hex[:12],
        "event_type": event_type,
        "resource": resource,
    }


def post_event(client: TestClient, ev: dict[str, Any]) -> Any:
    return client.post("/api/v1/webhooks/paypal", content=json.dumps(ev))
