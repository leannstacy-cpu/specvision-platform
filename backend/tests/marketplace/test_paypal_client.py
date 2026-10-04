import json

import httpx
import pytest

from app.marketplace.paypal import PayPalClient, PayPalError


def _client(handler, **kw) -> PayPalClient:  # type: ignore[no-untyped-def]
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return PayPalClient(
        base_url="https://paypal.test",
        client_id="id",
        client_secret="secret",
        webhook_id=kw.get("webhook_id", "WH-ID"),
        http=http,
    )


def test_requests_use_bearer_token_and_server_side_values() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        if req.url.path == "/v1/billing/subscriptions":
            return httpx.Response(
                201,
                json={
                    "id": "I-1",
                    "links": [{"rel": "approve", "href": "https://p/approve"}],
                },
            )
        if req.url.path.endswith("/cancel"):
            return httpx.Response(204)
        if req.url.path == "/v2/checkout/orders":
            return httpx.Response(201, json={"id": "ORD-1"})
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    assert c.create_subscription(plan_id="P-1", return_url="r", cancel_url="c") == (
        "I-1",
        "https://p/approve",
    )
    c.cancel_subscription("I-1", "bye")
    assert (
        c.create_order(
            amount_cents=5000,
            reference_id="ref",
            description="d",
            seller_merchant_id="M1",
            fee_cents=250,
            delayed_disbursement=True,
        )
        == "ORD-1"
    )
    order_req = next(r for r in seen if r.url.path == "/v2/checkout/orders")
    unit = json.loads(order_req.content)["purchase_units"][0]
    assert unit["amount"]["value"] == "50.00"
    assert unit["payee"]["merchant_id"] == "M1"
    assert unit["payment_instruction"]["disbursement_mode"] == "DELAYED"
    assert unit["payment_instruction"]["platform_fees"][0]["amount"]["value"] == "2.50"
    assert order_req.headers["authorization"] == "Bearer " + "tok"
    assert (
        c.create_order(amount_cents=100, reference_id="r2", description="d") == "ORD-1"
    )
    assert c.capture_order("ORD-1") == {"ok": True}
    assert c.refund_capture("CAP") == {"ok": True}
    assert c.release_funds(capture_id="CAP", amount_cents=100, reference_id="x") == {
        "ok": True
    }
    # token is cached
    assert sum(r.url.path == "/v1/oauth2/token" for r in seen) == 1


def test_missing_approval_link_and_http_errors() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(201, json={"id": "I", "links": []})

    with pytest.raises(PayPalError):
        _client(handler).create_subscription(
            plan_id="P", return_url="r", cancel_url="c"
        )

    def failing(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(500, json={"secret": "do-not-leak"})

    with pytest.raises(PayPalError) as exc:
        _client(failing).capture_order("X")
    assert "do-not-leak" not in str(exc.value)

    def auth_fail(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(PayPalError):
        _client(auth_fail).capture_order("X")
    no_creds = PayPalClient(base_url="https://x", client_id="", client_secret="")
    no_creds.client_id = no_creds.client_secret = None
    with pytest.raises(PayPalError):
        no_creds.capture_order("X")


HEADERS = {
    "PayPal-Auth-Algo": "a",
    "PayPal-Cert-Url": "u",
    "PayPal-Transmission-Id": "i",
    "PayPal-Transmission-Sig": "s",
    "PayPal-Transmission-Time": "t",
}


def test_webhook_signature_verification() -> None:
    def make(status: str):  # type: ignore[no-untyped-def]
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/oauth2/token":
                return httpx.Response(200, json={"access_token": "t"})
            body = json.loads(req.content)
            assert body["webhook_id"] == "WH-ID" and body["webhook_event"] == {
                "id": "E"
            }
            return httpx.Response(200, json={"verification_status": status})

        return handler

    assert _client(make("SUCCESS")).verify_webhook_signature(HEADERS, {"id": "E"})
    assert not _client(make("FAILURE")).verify_webhook_signature(HEADERS, {"id": "E"})
    # missing headers, missing webhook id and PayPal errors all fail closed
    assert not _client(make("SUCCESS")).verify_webhook_signature({}, {"id": "E"})
    no_id = _client(make("SUCCESS"))
    no_id.webhook_id = None
    assert not no_id.verify_webhook_signature(HEADERS, {"id": "E"})
    assert not _client(lambda r: httpx.Response(500)).verify_webhook_signature(
        HEADERS, {"id": "E"}
    )
