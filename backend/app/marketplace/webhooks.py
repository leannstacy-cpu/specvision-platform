# ruff: noqa: ARG001  (all handlers share one signature)
"""PayPal webhook ingestion.

PayPal webhooks (verified server-side before this module is called) are the
source of truth for subscription, lead-unlock and project-payment status. A
browser redirect never changes any of these.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.marketplace import audit
from app.marketplace.enums import (
    Interval,
    LeadUnlockStatus,
    MilestoneStatus,
    PaymentStatus,
    SubscriptionStatus,
    WebhookStatus,
)
from app.marketplace.models import (
    LeadUnlock,
    MembershipPlan,
    Milestone,
    Payment,
    SellerAccount,
    Subscription,
    SubscriptionPayment,
    WebhookEvent,
)
from app.marketplace.payments import apply_status
from app.marketplace.paypal import PayPalError, PayPalGateway

logger = logging.getLogger(__name__)


class MalformedEvent(ValueError):
    pass


Handler = Callable[[Session, PayPalGateway | None, dict[str, Any], str], None]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cents(value: Any) -> int:
    from decimal import Decimal

    return int((Decimal(str(value)) * 100).to_integral_value())


# ---------------------------------------------------------------- subscriptions


def _subscription(session: Session, resource_id: str | None) -> Subscription | None:
    if not resource_id:
        return None
    return session.exec(
        select(Subscription).where(Subscription.paypal_subscription_id == resource_id)
    ).first()


def _require_subscription(session: Session, resource_id: str | None) -> Subscription:
    sub = _subscription(session, resource_id)
    if sub is None:
        raise LookupError(f"Unknown PayPal subscription {resource_id}")
    return sub


def _plan_for(session: Session, paypal_plan_id: str | None) -> MembershipPlan | None:
    if not paypal_plan_id:
        return None
    return session.exec(
        select(MembershipPlan).where(MembershipPlan.paypal_plan_id == paypal_plan_id)
    ).first()


def _touch(sub: Subscription) -> None:
    sub.updated_at = _now()


def _next_billing(resource: dict[str, Any]) -> datetime | None:
    return _parse_time((resource.get("billing_info") or {}).get("next_billing_time"))


def on_subscription_created(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    _require_subscription(session, resource.get("id"))  # stays pending approval


def on_subscription_activated(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    sub = _require_subscription(session, resource.get("id"))
    plan = session.get(MembershipPlan, sub.plan_id)
    if plan is None or plan.paypal_plan_id != resource.get("plan_id"):
        raise ValueError("PayPal plan does not match the internal plan mapping")
    start = _parse_time(resource.get("start_time")) or _now()
    if sub.status == SubscriptionStatus.PENDING_APPROVAL or sub.trial_start is None:
        sub.trial_start = start
        sub.trial_end = start + timedelta(days=plan.trial_days)
    sub.status = SubscriptionStatus.ACTIVE
    sub.cancelled_at = None
    sub.current_period_end = _next_billing(resource) or sub.trial_end
    _touch(sub)
    session.add(sub)
    # Upgrade/downgrade: the newly approved plan replaces earlier ones.
    others = session.exec(
        select(Subscription)
        .where(Subscription.user_id == sub.user_id)
        .where(Subscription.id != sub.id)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
    ).all()
    for old in others:
        old.status = SubscriptionStatus.SUPERSEDED
        _touch(old)
        session.add(old)
        cancelled = False
        if gateway is not None:
            try:
                gateway.cancel_subscription(
                    old.paypal_subscription_id, "Replaced by plan change"
                )
                cancelled = True
            except PayPalError:
                logger.warning("Could not cancel superseded subscription at PayPal")
        audit.record(
            session,
            actor_id=None,
            action="subscription.superseded",
            entity_type="subscription",
            entity_id=old.id,
            detail={"paypal_cancelled": cancelled},
        )


def on_subscription_updated(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    sub = _require_subscription(session, resource.get("id"))
    new_plan = _plan_for(session, resource.get("plan_id"))
    if new_plan is not None and new_plan.id != sub.plan_id:
        sub.plan_id = new_plan.id
    nb = _next_billing(resource)
    if nb:
        sub.current_period_end = nb
    _touch(sub)
    session.add(sub)


def _set_sub_status(status: SubscriptionStatus) -> Handler:
    def handler(
        session: Session,
        gateway: PayPalGateway | None,
        resource: dict[str, Any],
        _id: str,
    ) -> None:
        sub = _require_subscription(session, resource.get("id"))
        if sub.status == SubscriptionStatus.SUPERSEDED:
            return
        sub.status = status
        if status == SubscriptionStatus.CANCELLED:
            sub.cancelled_at = _now()
        _touch(sub)
        session.add(sub)

    return handler


def on_subscription_payment_failed(
    session: Session,
    gateway: PayPalGateway | None,
    resource: dict[str, Any],
    event_id: str,
) -> None:
    sub = _require_subscription(session, resource.get("id"))
    if sub.status != SubscriptionStatus.SUPERSEDED:
        sub.status = SubscriptionStatus.PAYMENT_FAILED
        _touch(sub)
        session.add(sub)
    session.add(
        SubscriptionPayment(
            subscription_id=sub.id,
            paypal_transaction_id=f"failed-{event_id}"[:64],
            amount_cents=0,
            status="failed",
        )
    )


def on_sale_completed(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    sub = _require_subscription(session, resource.get("billing_agreement_id"))
    txn = str(resource["id"])
    if session.exec(
        select(SubscriptionPayment).where(
            SubscriptionPayment.paypal_transaction_id == txn
        )
    ).first():
        return
    session.add(
        SubscriptionPayment(
            subscription_id=sub.id,
            paypal_transaction_id=txn,
            amount_cents=_cents((resource.get("amount") or {}).get("total", "0")),
            status="completed",
        )
    )
    if sub.status in (SubscriptionStatus.PAYMENT_FAILED, SubscriptionStatus.SUSPENDED):
        sub.status = SubscriptionStatus.ACTIVE
    plan = session.get(MembershipPlan, sub.plan_id)
    if plan is not None and sub.status == SubscriptionStatus.ACTIVE:
        days = 366 if plan.interval == Interval.YEAR else 31
        base = max(_now(), sub.current_period_end or _now())
        sub.current_period_end = base + timedelta(days=days)
    _touch(sub)
    session.add(sub)


def _sale_adjusted(new_status: str) -> Handler:
    def handler(
        session: Session,
        gateway: PayPalGateway | None,
        resource: dict[str, Any],
        _id: str,
    ) -> None:
        txn = session.exec(
            select(SubscriptionPayment).where(
                SubscriptionPayment.paypal_transaction_id == resource.get("sale_id")
            )
        ).first()
        if txn is None:
            raise LookupError("Unknown subscription payment")
        txn.status = new_status
        session.add(txn)
        audit.record(
            session,
            actor_id=None,
            action=f"subscription.payment_{new_status}",
            entity_type="subscription",
            entity_id=txn.subscription_id,
        )

    return handler


# -------------------------------------------------------------- orders/captures


def _order_id(resource: dict[str, Any]) -> str | None:
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    return related.get("order_id") or resource.get("order_id")


def _capture_id_from_refund(resource: dict[str, Any]) -> str | None:
    for link in resource.get("links", []):
        if link.get("rel") == "up" and "/captures/" in link.get("href", ""):
            return str(link["href"]).rsplit("/", 1)[-1]
    return None


def _unlock_by_order(session: Session, order_id: str | None) -> LeadUnlock | None:
    if not order_id:
        return None
    return session.exec(
        select(LeadUnlock).where(LeadUnlock.paypal_order_id == order_id)
    ).first()


def _payment_by_order(session: Session, order_id: str | None) -> Payment | None:
    if not order_id:
        return None
    return session.exec(
        select(Payment).where(Payment.paypal_order_id == order_id)
    ).first()


def _find_by_capture(
    session: Session, capture_id: str | None
) -> tuple[LeadUnlock | None, Payment | None]:
    if not capture_id:
        return None, None
    unlock = session.exec(
        select(LeadUnlock).where(LeadUnlock.paypal_capture_id == capture_id)
    ).first()
    payment = session.exec(
        select(Payment).where(Payment.paypal_capture_id == capture_id)
    ).first()
    return unlock, payment


def on_order_approved(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    """Buyer approved an order: capture it server-side. Status waits for the capture webhook."""
    order_id = resource.get("id")
    unlock = _unlock_by_order(session, order_id)
    payment = _payment_by_order(session, order_id)
    if unlock is None and payment is None:
        raise LookupError(f"Unknown PayPal order {order_id}")
    if gateway is None:
        raise PayPalError("PayPal gateway unavailable")
    gateway.capture_order(str(order_id))


def on_authorization_created(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    payment = _payment_by_order(session, _order_id(resource))
    if payment is None:
        raise LookupError("Unknown authorization")
    payment.paypal_authorization_id = str(resource.get("id"))
    apply_status(session, payment, PaymentStatus.AUTHORIZED, reason="authorization")


def on_capture_completed(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    capture_id = str(resource.get("id"))
    order_id = _order_id(resource)
    if resource.get("status") not in (None, "COMPLETED"):
        return  # e.g. PENDING: wait for a later event
    unlock = _unlock_by_order(session, order_id)
    if unlock is not None:
        # The paid amount must match the server-computed fee.
        paid = _cents((resource.get("amount") or {}).get("value", "0"))
        if paid != unlock.fee_cents:
            unlock.status = LeadUnlockStatus.FAILED
            session.add(unlock)
            audit.record(
                session,
                actor_id=None,
                action="lead_unlock.amount_mismatch",
                entity_type="lead_unlock",
                entity_id=unlock.id,
                detail={"expected": unlock.fee_cents, "paid": paid},
            )
            return
        unlock.status = LeadUnlockStatus.PAID
        unlock.paypal_capture_id = capture_id
        unlock.paid_at = _now()
        session.add(unlock)
        audit.record(
            session,
            actor_id=unlock.professional_id,
            action="lead_unlock.paid",
            entity_type="lead_unlock",
            entity_id=unlock.id,
        )
        return
    payment = _payment_by_order(session, order_id)
    if payment is None:
        raise LookupError(f"Unknown PayPal order {order_id}")
    payment.paypal_capture_id = capture_id
    payment.captured_at = _now()
    payment.release_deadline = payment.captured_at + timedelta(
        days=settings.PAYPAL_MAX_DISBURSEMENT_DAYS
    )
    apply_status(session, payment, PaymentStatus.CAPTURED, reason="capture")
    confirmed_delayed = resource.get("disbursement_mode") == "DELAYED"
    if payment.disbursement_mode == "delayed":
        if confirmed_delayed:
            apply_status(
                session, payment, PaymentStatus.HELD_PENDING_DISBURSEMENT, reason="held"
            )
        else:
            # Never mark funds secured unless PayPal says so.
            apply_status(
                session,
                payment,
                PaymentStatus.MANUAL_REVIEW_REQUIRED,
                reason="delayed disbursement not confirmed",
            )
    if payment.milestone_id and payment.status in (
        PaymentStatus.CAPTURED,
        PaymentStatus.HELD_PENDING_DISBURSEMENT,
    ):
        milestone = session.get(Milestone, payment.milestone_id)
        if milestone is not None:
            milestone.status = MilestoneStatus.FUNDED
            session.add(milestone)


def on_capture_failed(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    order_id = _order_id(resource)
    unlock = _unlock_by_order(session, order_id)
    if unlock is not None:
        unlock.status = LeadUnlockStatus.FAILED
        session.add(unlock)
        return
    payment = _payment_by_order(session, order_id)
    if payment is None:
        raise LookupError("Unknown PayPal order")
    apply_status(session, payment, PaymentStatus.FAILED, reason="capture denied")


def _reverse_handler(
    lead_status: LeadUnlockStatus, payment_status: PaymentStatus, *, via_refund: bool
) -> Handler:
    def handler(
        session: Session,
        gateway: PayPalGateway | None,
        resource: dict[str, Any],
        _id: str,
    ) -> None:
        capture_id = (
            _capture_id_from_refund(resource) if via_refund else resource.get("id")
        )
        unlock, payment = _find_by_capture(session, capture_id)
        if unlock is None and payment is None:
            raise LookupError("Unknown PayPal capture")
        if unlock is not None:
            unlock.status = lead_status  # revokes access: only PAID unlocks
            session.add(unlock)
            audit.record(
                session,
                actor_id=None,
                action=f"lead_unlock.{lead_status}",
                entity_type="lead_unlock",
                entity_id=unlock.id,
            )
        if payment is not None:
            apply_status(session, payment, payment_status, reason="paypal event")

    return handler


def on_dispute_created(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    for txn in resource.get("disputed_transactions", []):
        _, payment = _find_by_capture(session, txn.get("seller_transaction_id"))
        if payment is not None:
            apply_status(session, payment, PaymentStatus.DISPUTED, reason="dispute")


def on_payout_completed(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    """Disbursement confirmation (event shape must be verified in the sandbox)."""
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    capture_id = related.get("capture_id") or resource.get("reference_id")
    _, payment = _find_by_capture(session, capture_id)
    if payment is None:
        raise LookupError("Unknown disbursement reference")
    amount = _cents((resource.get("payout_amount") or {}).get("value", "0"))
    payment.released_cents += amount
    seller_total = payment.amount_cents - payment.fee_cents
    if payment.released_cents >= seller_total:
        apply_status(session, payment, PaymentStatus.RELEASED, reason="disbursed")
        if payment.milestone_id:
            milestone = session.get(Milestone, payment.milestone_id)
            if milestone is not None:
                milestone.status = MilestoneStatus.RELEASED
                session.add(milestone)
    else:
        apply_status(
            session, payment, PaymentStatus.PARTIALLY_RELEASED, reason="partial"
        )


# ----------------------------------------------------------------------- sellers


def _seller_from(session: Session, resource: dict[str, Any]) -> SellerAccount | None:
    tracking = resource.get("tracking_id")
    merchant = resource.get("merchant_id")
    seller = None
    if tracking:
        try:
            seller = session.exec(
                select(SellerAccount).where(
                    SellerAccount.user_id == uuid.UUID(str(tracking))
                )
            ).first()
        except ValueError:
            seller = None
    if seller is None and merchant:
        seller = session.exec(
            select(SellerAccount).where(SellerAccount.paypal_merchant_id == merchant)
        ).first()
    return seller


def on_seller_onboarded(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    seller = _seller_from(session, resource)
    if seller is None:
        raise LookupError("Unknown seller")
    seller.paypal_merchant_id = resource.get("merchant_id") or seller.paypal_merchant_id
    seller.onboarding_complete = True
    seller.updated_at = _now()
    session.add(seller)


def on_seller_capability(
    session: Session, gateway: PayPalGateway | None, resource: dict[str, Any], _id: str
) -> None:
    """Capability names/statuses must be verified against sandbox payloads."""
    seller = _seller_from(session, resource)
    if seller is None:
        raise LookupError("Unknown seller")
    caps = {
        c.get("name"): c.get("status") for c in resource.get("capabilities", []) or []
    }
    if "payments_receivable" in resource:
        seller.payments_receivable = bool(resource["payments_receivable"])
    seller.delayed_disbursement_enabled = (
        caps.get("DELAY_FUNDS_DISBURSEMENT") == "ACTIVE"
    )
    seller.updated_at = _now()
    session.add(seller)


HANDLERS: dict[str, Handler] = {
    "BILLING.SUBSCRIPTION.CREATED": on_subscription_created,
    "BILLING.SUBSCRIPTION.ACTIVATED": on_subscription_activated,
    "BILLING.SUBSCRIPTION.RE-ACTIVATED": on_subscription_activated,
    "BILLING.SUBSCRIPTION.UPDATED": on_subscription_updated,
    "BILLING.SUBSCRIPTION.SUSPENDED": _set_sub_status(SubscriptionStatus.SUSPENDED),
    "BILLING.SUBSCRIPTION.CANCELLED": _set_sub_status(SubscriptionStatus.CANCELLED),
    "BILLING.SUBSCRIPTION.EXPIRED": _set_sub_status(SubscriptionStatus.EXPIRED),
    "BILLING.SUBSCRIPTION.PAYMENT.FAILED": on_subscription_payment_failed,
    "PAYMENT.SALE.COMPLETED": on_sale_completed,
    "PAYMENT.SALE.REFUNDED": _sale_adjusted("refunded"),
    "PAYMENT.SALE.REVERSED": _sale_adjusted("reversed"),
    "CHECKOUT.ORDER.APPROVED": on_order_approved,
    "PAYMENT.AUTHORIZATION.CREATED": on_authorization_created,
    "PAYMENT.CAPTURE.COMPLETED": on_capture_completed,
    "PAYMENT.CAPTURE.DENIED": on_capture_failed,
    "PAYMENT.CAPTURE.DECLINED": on_capture_failed,
    "PAYMENT.CAPTURE.REFUNDED": _reverse_handler(
        LeadUnlockStatus.REFUNDED, PaymentStatus.REFUNDED, via_refund=True
    ),
    "PAYMENT.CAPTURE.REVERSED": _reverse_handler(
        LeadUnlockStatus.REFUNDED, PaymentStatus.REVERSED, via_refund=False
    ),
    "CUSTOMER.DISPUTE.CREATED": on_dispute_created,
    "PAYMENT.REFERENCED-PAYOUT-ITEM.COMPLETED": on_payout_completed,
    "MERCHANT.ONBOARDING.COMPLETED": on_seller_onboarded,
    "CUSTOMER.MERCHANT-INTEGRATION.CAPABILITY-UPDATED": on_seller_capability,
}


# ---------------------------------------------------------------------- ingestion


def ingest(
    session: Session,
    gateway: PayPalGateway | None,
    event: dict[str, Any],
    raw_body: str,
) -> str:
    """Store and process a verified event exactly once.

    Returns "processed", "ignored" or "duplicate". Raises on handler failure
    after recording it, so PayPal retries delivery.
    """
    event_id = str(event.get("id") or "")
    if not event_id:
        raise MalformedEvent("Webhook event has no id")
    row = WebhookEvent(
        paypal_event_id=event_id,
        event_type=str(event.get("event_type", "")),
        environment=settings.PAYPAL_ENV,
        raw_body=raw_body,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    # Lock the row so concurrent deliveries of the same event serialize.
    stored = session.exec(
        select(WebhookEvent)
        .where(WebhookEvent.paypal_event_id == event_id)
        .with_for_update()
    ).one()
    if stored.status in (WebhookStatus.PROCESSED, WebhookStatus.IGNORED):
        session.rollback()
        return "duplicate"
    stored.attempts += 1
    handler = HANDLERS.get(stored.event_type)
    if handler is None:
        stored.status = WebhookStatus.IGNORED
        stored.processed_at = _now()
        session.add(stored)
        session.commit()
        return "ignored"
    try:
        handler(session, gateway, event.get("resource") or {}, event_id)
        stored.status = WebhookStatus.PROCESSED
        stored.error = None
        stored.processed_at = _now()
        session.add(stored)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.exception("Webhook %s (%s) failed", event_id, stored.event_type)
        failed = session.exec(
            select(WebhookEvent).where(col(WebhookEvent.paypal_event_id) == event_id)
        ).one()
        failed.status = WebhookStatus.FAILED
        failed.attempts += 1
        failed.error = f"{type(exc).__name__}: {exc}"[:2000]
        session.add(failed)
        session.commit()
        raise
    return "processed"
