"""Thin, replaceable PayPal REST client.

Only the server talks to PayPal. The client secret never leaves the backend.
All amounts and plan identifiers passed in come from database rows, never from
the browser.
"""

import logging
import time
from typing import Any, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class PayPalError(Exception):
    """Raised for failed PayPal calls. Messages never contain credentials."""


class PayPalGateway(Protocol):
    def verify_webhook_signature(
        self, headers: dict[str, str], event: dict[str, Any]
    ) -> bool: ...

    def create_subscription(
        self, *, plan_id: str, return_url: str, cancel_url: str
    ) -> tuple[str, str]: ...

    def cancel_subscription(self, subscription_id: str, reason: str) -> None: ...

    def create_order(
        self,
        *,
        amount_cents: int,
        reference_id: str,
        description: str,
        seller_merchant_id: str | None = None,
        fee_cents: int = 0,
        delayed_disbursement: bool = False,
    ) -> str: ...

    def capture_order(self, order_id: str) -> dict[str, Any]: ...

    def refund_capture(self, capture_id: str) -> dict[str, Any]: ...

    def release_funds(
        self, *, capture_id: str, amount_cents: int, reference_id: str
    ) -> dict[str, Any]: ...


def money(cents: int) -> str:
    if cents < 0:
        raise ValueError("amount must not be negative")
    return f"{cents // 100}.{cents % 100:02d}"


class PayPalClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        webhook_id: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url or settings.paypal_api_base
        self.client_id = client_id or settings.PAYPAL_CLIENT_ID
        self.client_secret = client_secret or settings.PAYPAL_CLIENT_SECRET
        self.webhook_id = webhook_id or settings.PAYPAL_WEBHOOK_ID
        self._http = http or httpx.Client(timeout=15)
        self._token: str | None = None
        self._token_expiry = 0.0

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        if not (self.client_id and self.client_secret):
            raise PayPalError("PayPal credentials are not configured")
        r = self._http.post(
            f"{self.base_url}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        if r.status_code != 200:
            raise PayPalError(f"PayPal authentication failed ({r.status_code})")
        data = r.json()
        self._token = data["access_token"]
        self._token_expiry = time.monotonic() + int(data.get("expires_in", 300)) - 30
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 202, 204),
    ) -> dict[str, Any]:
        h = {"Authorization": "Bearer " + self._access_token()}
        if headers:
            h.update(headers)
        r = self._http.request(method, f"{self.base_url}{path}", json=json, headers=h)
        if r.status_code not in ok:
            logger.warning("PayPal %s %s failed: %s", method, path, r.status_code)
            raise PayPalError(f"PayPal request failed ({r.status_code})")
        return r.json() if r.content else {}

    def verify_webhook_signature(
        self, headers: dict[str, str], event: dict[str, Any]
    ) -> bool:
        if not self.webhook_id:
            logger.error("PAYPAL_WEBHOOK_ID is not configured; rejecting webhook")
            return False
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            payload = {
                "auth_algo": lower["paypal-auth-algo"],
                "cert_url": lower["paypal-cert-url"],
                "transmission_id": lower["paypal-transmission-id"],
                "transmission_sig": lower["paypal-transmission-sig"],
                "transmission_time": lower["paypal-transmission-time"],
                "webhook_id": self.webhook_id,
                "webhook_event": event,
            }
        except KeyError:
            return False
        try:
            result = self._request(
                "POST", "/v1/notifications/verify-webhook-signature", json=payload
            )
        except PayPalError:
            return False
        return result.get("verification_status") == "SUCCESS"

    def create_subscription(
        self, *, plan_id: str, return_url: str, cancel_url: str
    ) -> tuple[str, str]:
        data = self._request(
            "POST",
            "/v1/billing/subscriptions",
            json={
                "plan_id": plan_id,
                "application_context": {
                    "brand_name": "SPECVision Tech Solutions",
                    "user_action": "SUBSCRIBE_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            },
        )
        approve = next(
            (
                link["href"]
                for link in data.get("links", [])
                if link["rel"] == "approve"
            ),
            None,
        )
        if not approve:
            raise PayPalError("PayPal did not return an approval link")
        return data["id"], approve

    def cancel_subscription(self, subscription_id: str, reason: str) -> None:
        self._request(
            "POST",
            f"/v1/billing/subscriptions/{subscription_id}/cancel",
            json={"reason": reason[:128]},
        )

    def create_order(
        self,
        *,
        amount_cents: int,
        reference_id: str,
        description: str,
        seller_merchant_id: str | None = None,
        fee_cents: int = 0,
        delayed_disbursement: bool = False,
    ) -> str:
        unit: dict[str, Any] = {
            "reference_id": reference_id,
            "description": description[:127],
            "amount": {"currency_code": "USD", "value": money(amount_cents)},
        }
        headers: dict[str, str] = {"PayPal-Request-Id": reference_id}
        if seller_merchant_id:
            instruction: dict[str, Any] = {
                "platform_fees": [
                    {
                        "amount": {
                            "currency_code": "USD",
                            "value": money(fee_cents),
                        }
                    }
                ]
                if fee_cents
                else [],
            }
            if delayed_disbursement:
                instruction["disbursement_mode"] = "DELAYED"
            unit["payee"] = {"merchant_id": seller_merchant_id}
            unit["payment_instruction"] = instruction
            if settings.PAYPAL_PARTNER_BN_CODE:
                headers["PayPal-Partner-Attribution-Id"] = (
                    settings.PAYPAL_PARTNER_BN_CODE
                )
        data = self._request(
            "POST",
            "/v2/checkout/orders",
            json={"intent": "CAPTURE", "purchase_units": [unit]},
            headers=headers,
        )
        return str(data["id"])

    def capture_order(self, order_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            headers={"PayPal-Request-Id": f"capture-{order_id}"},
        )

    def refund_capture(self, capture_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json={},
            headers={"PayPal-Request-Id": f"refund-{capture_id}"},
        )

    def release_funds(
        self, *, capture_id: str, amount_cents: int, reference_id: str
    ) -> dict[str, Any]:
        """Release delayed-disbursement funds via PayPal referenced payouts."""
        return self._request(
            "POST",
            "/v1/payments/referenced-payouts-items",
            json={
                "reference_id": capture_id,
                "reference_type": "TRANSACTION_ID",
                "payout_amount": {
                    "currency_code": "USD",
                    "value": money(amount_cents),
                },
            },
            headers={"PayPal-Request-Id": reference_id},
        )


def get_paypal_gateway() -> PayPalGateway:
    """FastAPI dependency; overridden in tests with a recording test double."""
    if not settings.paypal_configured:
        raise PayPalError("PayPal is not configured")
    return PayPalClient()
