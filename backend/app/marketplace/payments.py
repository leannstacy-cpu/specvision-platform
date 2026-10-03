"""Project/milestone payment workflow built on PayPal marketplace payments.

SPECVision never simulates a payment-holding account. Funds are only treated as
held once PayPal (via a verified webhook) confirms a delayed-disbursement
capture. Customer-facing language: "protected project payment" / "payment held
for scheduled release" -- never "escrow".
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from app.core.config import settings
from app.marketplace import audit
from app.marketplace.enums import (
    DisbursementMode,
    MilestoneStatus,
    PaymentStatus,
)
from app.marketplace.models import (
    Milestone,
    Payment,
    PlatformSetting,
    Project,
    SellerAccount,
)
from app.marketplace.paypal import PayPalError, PayPalGateway

S = PaymentStatus

ALLOWED: dict[PaymentStatus, set[PaymentStatus]] = {
    S.DRAFT: {S.PENDING_PAYMENT, S.CANCELED},
    S.PENDING_PAYMENT: {S.AUTHORIZED, S.CAPTURED, S.HELD_PENDING_DISBURSEMENT, S.FAILED, S.CANCELED, S.MANUAL_REVIEW_REQUIRED},
    S.AUTHORIZED: {S.CAPTURED, S.HELD_PENDING_DISBURSEMENT, S.CANCELED, S.FAILED, S.MANUAL_REVIEW_REQUIRED},
    S.CAPTURED: {S.HELD_PENDING_DISBURSEMENT, S.RELEASED, S.REFUNDED, S.DISPUTED, S.REVERSED, S.MANUAL_REVIEW_REQUIRED},
    S.HELD_PENDING_DISBURSEMENT: {S.PARTIALLY_RELEASED, S.RELEASED, S.REFUNDED, S.DISPUTED, S.REVERSED, S.MANUAL_REVIEW_REQUIRED},
    S.PARTIALLY_RELEASED: {S.RELEASED, S.REFUNDED, S.DISPUTED, S.REVERSED, S.MANUAL_REVIEW_REQUIRED},
    S.RELEASED: {S.REFUNDED, S.DISPUTED, S.REVERSED, S.MANUAL_REVIEW_REQUIRED},
    S.DISPUTED: {S.HELD_PENDING_DISBURSEMENT, S.RELEASED, S.REFUNDED, S.REVERSED, S.MANUAL_REVIEW_REQUIRED},
    S.REVERSED: {S.MANUAL_REVIEW_REQUIRED},
    S.REFUNDED: set(),
    S.CANCELED: set(),
    S.FAILED: {S.MANUAL_REVIEW_REQUIRED},
    S.MANUAL_REVIEW_REQUIRED: set(PaymentStatus) - {S.MANUAL_REVIEW_REQUIRED},
}  # fmt: skip

# Statuses in which PayPal is still holding seller funds.
HOLDING = (S.HELD_PENDING_DISBURSEMENT, S.PARTIALLY_RELEASED)


class PaymentError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def can_transition(current: str, new: PaymentStatus) -> bool:
    return new in ALLOWED[PaymentStatus(current)]


def apply_status(
    session: Session,
    payment: Payment,
    new: PaymentStatus,
    *,
    actor_id: uuid.UUID | None = None,
    reason: str = "",
) -> bool:
    """Move a payment to ``new``. Returns False when the move is not allowed.

    An unexpected (e.g. out-of-order) webhook transition flags the payment for
    manual review rather than silently overwriting its state.
    """
    if payment.status == new:
        return True
    if not can_transition(payment.status, new):
        if PaymentStatus(payment.status) not in (
            S.REFUNDED,
            S.CANCELED,
            S.FAILED,
            S.MANUAL_REVIEW_REQUIRED,
        ):
            audit.record(
                session,
                actor_id=actor_id,
                action="payment.invalid_transition",
                entity_type="payment",
                entity_id=payment.id,
                detail={"from": payment.status, "to": new, "reason": reason},
            )
            payment.status = S.MANUAL_REVIEW_REQUIRED
            session.add(payment)
        return False
    audit.record(
        session,
        actor_id=actor_id,
        action="payment.status",
        entity_type="payment",
        entity_id=payment.id,
        detail={"from": payment.status, "to": new, "reason": reason},
    )
    payment.status = new
    session.add(payment)
    return True


def marketplace_fee_cents(session: Session, amount_cents: int) -> int:
    """Fee comes from admin configuration only; defaults to 0 (none invented)."""
    setting = session.get(PlatformSetting, "marketplace_fee_bps")
    if setting is None:
        return 0
    bps = max(0, min(int(setting.value), 10_000))
    return amount_cents * bps // 10_000


def fund_milestone(
    session: Session,
    gateway: PayPalGateway,
    *,
    milestone: Milestone,
    project: Project,
    payer_id: uuid.UUID,
    accept_direct_payment: bool,
) -> tuple[Payment, str]:
    """Create a PayPal order for the milestone. Returns (payment, order id)."""
    if project.owner_id != payer_id:
        raise PaymentError("Only the project owner can fund a milestone", 403)
    if milestone.status != MilestoneStatus.PENDING:
        raise PaymentError("Milestone is not awaiting funding")
    seller = session.exec(
        select(SellerAccount).where(SellerAccount.user_id == milestone.seller_id)
    ).first()
    if not (
        seller
        and seller.paypal_merchant_id
        and seller.onboarding_complete
        and seller.payments_receivable
    ):
        raise PaymentError("The professional has not completed PayPal onboarding")
    mode = (
        DisbursementMode.DELAYED
        if seller.delayed_disbursement_enabled
        else DisbursementMode.DIRECT
    )
    if mode == DisbursementMode.DIRECT and not accept_direct_payment:
        raise PaymentError(
            "Delayed disbursement is unavailable for this professional. Funds "
            "would be paid directly to them and are not held. Re-submit with "
            "accept_direct_payment=true to proceed.",
            422,
        )
    payment = Payment(
        project_id=project.id,
        milestone_id=milestone.id,
        payer_id=payer_id,
        seller_id=milestone.seller_id,
        amount_cents=milestone.amount_cents,
        fee_cents=marketplace_fee_cents(session, milestone.amount_cents),
        disbursement_mode=mode,
        paypal_merchant_id=seller.paypal_merchant_id,
    )
    session.add(payment)
    session.flush()
    try:
        order_id = gateway.create_order(
            amount_cents=payment.amount_cents,
            reference_id=str(payment.id),
            description=f"SPECVision milestone: {milestone.title}",
            seller_merchant_id=seller.paypal_merchant_id,
            fee_cents=payment.fee_cents,
            delayed_disbursement=mode == DisbursementMode.DELAYED,
        )
    except PayPalError as exc:
        raise PaymentError("Could not create the PayPal order", 502) from exc
    payment.paypal_order_id = order_id
    apply_status(session, payment, S.PENDING_PAYMENT, actor_id=payer_id)
    milestone.status = MilestoneStatus.FUNDING
    session.add(milestone)
    audit.record(
        session,
        actor_id=payer_id,
        action="milestone.fund_started",
        entity_type="milestone",
        entity_id=milestone.id,
        detail={"mode": mode, "order_id": order_id},
    )
    session.commit()
    return payment, order_id


def approve_milestone(
    session: Session,
    gateway: PayPalGateway,
    *,
    milestone: Milestone,
    project: Project,
    actor_id: uuid.UUID,
    actor_is_admin: bool,
) -> Payment:
    """Client (or authorized admin) approves; PayPal confirms release by webhook."""
    if project.owner_id != actor_id and not actor_is_admin:
        raise PaymentError("Only the project owner can approve a milestone", 403)
    if milestone.status not in (MilestoneStatus.FUNDED, MilestoneStatus.SUBMITTED):
        raise PaymentError("Milestone is not funded")
    payment = session.exec(
        select(Payment).where(Payment.milestone_id == milestone.id)
    ).first()
    if payment is None or payment.status not in (
        S.HELD_PENDING_DISBURSEMENT,
        S.CAPTURED,
    ):
        raise PaymentError("Payment is not confirmed by PayPal", 409)
    if payment.status == S.HELD_PENDING_DISBURSEMENT:
        if not payment.paypal_capture_id:
            raise PaymentError("Missing PayPal capture reference", 409)
        try:
            gateway.release_funds(
                capture_id=payment.paypal_capture_id,
                amount_cents=payment.amount_cents - payment.fee_cents,
                reference_id=f"release-{payment.id}",
            )
        except PayPalError as exc:
            audit.record(
                session,
                actor_id=actor_id,
                action="payment.release_failed",
                entity_type="payment",
                entity_id=payment.id,
            )
            session.commit()
            raise PaymentError("PayPal could not release the funds", 502) from exc
        milestone.status = MilestoneStatus.APPROVED
    else:
        # Direct-payment fallback: funds already went to the seller at capture.
        milestone.status = MilestoneStatus.RELEASED
        apply_status(session, payment, S.RELEASED, actor_id=actor_id)
    session.add(milestone)
    audit.record(
        session,
        actor_id=actor_id,
        action="milestone.approved",
        entity_type="milestone",
        entity_id=milestone.id,
        detail={"payment_id": str(payment.id)},
    )
    session.commit()
    return payment


def request_refund(
    session: Session,
    gateway: PayPalGateway,
    *,
    payment: Payment,
    actor_id: uuid.UUID,
    actor_is_admin: bool,
) -> Payment:
    """Administrator-initiated refund; status changes only on PayPal's webhook."""
    if not actor_is_admin:
        raise PaymentError("Only an administrator can refund a payment", 403)
    if (
        payment.status
        not in (
            S.CAPTURED,
            S.HELD_PENDING_DISBURSEMENT,
            S.DISPUTED,
            S.MANUAL_REVIEW_REQUIRED,
        )
        or not payment.paypal_capture_id
    ):
        raise PaymentError("Payment cannot be refunded in its current state")
    try:
        gateway.refund_capture(payment.paypal_capture_id)
    except PayPalError as exc:
        raise PaymentError("PayPal could not refund the payment", 502) from exc
    audit.record(
        session,
        actor_id=actor_id,
        action="payment.refund_requested",
        entity_type="payment",
        entity_id=payment.id,
    )
    session.commit()
    return payment


def release_deadline_queue(session: Session) -> list[Payment]:
    """Held payments whose PayPal hold window is close to expiring."""
    cutoff = datetime.now(UTC) + timedelta(days=settings.PAYPAL_RELEASE_WARNING_DAYS)
    return list(
        session.exec(
            select(Payment)
            .where(col(Payment.status).in_([str(s) for s in HOLDING]))
            .where(col(Payment.release_deadline) <= cutoff)
            .order_by(col(Payment.release_deadline))
        ).all()
    )
