# ruff: noqa: ARG001  (fixtures such as `gateway` are requested for their side effects)
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.marketplace import entitlements
from app.marketplace.enums import (
    LeadUnlockStatus,
    MilestoneStatus,
    PaymentStatus,
    SubscriptionStatus,
    Tier,
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
from tests.marketplace.conftest import (
    Actor,
    FakeGateway,
    event,
    grant_membership,
    make_actor,
    post_event,
    project_payload,
)

API = settings.API_V1_STR
PROTECTED = [
    "contact_name",
    "company_name",
    "contact_email",
    "contact_phone",
    "street_address",
    "website",
]
SECRETS = ["Jane Secret", "Secret Corp", "secretcorp", "512-555-0142", "Congress"]


def _client_with_project(
    client: TestClient, db: Session, **overrides: Any
) -> tuple[Actor, str]:
    owner = make_actor(client, db, role="client", plan="client_monthly")
    r = client.post(
        f"{API}/projects/", json=project_payload(**overrides), headers=owner.headers
    )
    assert r.status_code == 200, r.text
    return owner, r.json()["id"]


def _pro(client: TestClient, db: Session, plan: str = "pro_plus_monthly") -> Actor:
    return make_actor(
        client, db, role="professional", category="audiovisual", plan=plan
    )


def _bid(client: TestClient, pro: Actor, pid: str) -> dict[str, Any]:
    r = client.post(
        f"{API}/projects/{pid}/bids",
        json={
            "amount_cents": 700000,
            "timeline_days": 30,
            "message": "We can do this in a month.",
        },
        headers=pro.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _paid_unlock(  # noqa: ARG001
    client: TestClient, db: Session, gw: FakeGateway, owner: Actor, pid: str, pro: Actor
) -> dict[str, Any]:
    bid = _bid(client, pro, pid)
    r = client.post(
        f"{API}/projects/{pid}/bids/{bid['id']}/shortlist", headers=owner.headers
    )
    assert r.status_code == 200
    r = client.post(f"{API}/projects/{pid}/unlock", headers=pro.headers)
    assert r.status_code == 200, r.text
    return r.json()


def _capture_event(
    order_id: str, value: str, capture_id: str = "CAP-1", **extra: Any
) -> dict[str, Any]:
    return event(
        "PAYMENT.CAPTURE.COMPLETED",
        {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": value},
            "supplementary_data": {"related_ids": {"order_id": order_id}},
            **extra,
        },
    )


# ------------------------------------------------------------------ catalog/plans


def test_plans_catalog_prices_and_fees(client: TestClient) -> None:
    r = client.get(f"{API}/plans")
    assert r.status_code == 200
    plans = {p["code"]: p for p in r.json()}
    assert plans["client_monthly"]["price_cents"] == 999
    assert plans["client_annual"]["price_cents"] == 9900
    assert plans["professional_monthly"]["lead_fee_cents"] == 5000
    assert plans["pro_plus_annual"]["price_cents"] == 39900
    assert plans["pro_plus_monthly"]["lead_fee_cents"] == 2500
    assert plans["elite_annual"]["price_cents"] == 69900
    assert plans["elite_monthly"]["lead_fee_cents"] == 1500
    assert all(p["trial_days"] == 7 for p in plans.values())
    assert "paypal_plan_id" not in plans["client_monthly"]
    mods = {m["code"]: m["price_cents"] for m in client.get(f"{API}/modifiers").json()}
    assert mods == {
        "urgent_request": 3000,
        "on_site_assessment": 5000,
        "after_hours": 5000,
    }


def test_modifier_quote_and_discount_rules(client: TestClient, db: Session) -> None:
    owner = make_actor(client, db, role="client", plan="client_monthly")
    body = {"modifier_codes": ["urgent_request", "after_hours"]}
    r = client.post(f"{API}/modifiers/quote", json=body, headers=owner.headers)
    assert r.json()["total_cents"] == 8000 and r.json()["discount_cents"] == 0
    bad = client.post(
        f"{API}/modifiers/quote",
        json={"modifier_codes": ["nope"]},
        headers=owner.headers,
    )
    assert bad.status_code == 422
    from app.marketplace.models import DiscountRule

    rule = db.exec(
        select(DiscountRule).where(DiscountRule.code == "new_customer")
    ).one()
    rule.amount_cents, rule.is_active = 1000, True
    db.add(rule)
    db.commit()
    try:
        r = client.post(f"{API}/modifiers/quote", json=body, headers=owner.headers)
        assert r.json()["discount_cents"] == 1000 and r.json()["total_cents"] == 7000
        client.post(f"{API}/projects/", json=project_payload(), headers=owner.headers)
        r = client.post(f"{API}/modifiers/quote", json=body, headers=owner.headers)
        assert r.json()["discount_cents"] == 0  # no longer a new customer
    finally:
        rule.amount_cents, rule.is_active = 0, False
        db.add(rule)
        db.commit()


# ----------------------------------------------------------------- entitlements


def test_entitlement_never_comes_from_browser(client: TestClient, db: Session) -> None:
    actor = make_actor(client, db, role="professional", category="it")
    r = client.get(f"{API}/projects/", headers=actor.headers)
    assert r.status_code == 403
    r = client.get(
        f"{API}/projects/?tier=elite&plan=elite_monthly", headers=actor.headers
    )
    assert r.status_code == 403
    assert entitlements.active_tier(db, actor.id) is None


def test_entitlement_status_matrix(client: TestClient, db: Session) -> None:
    actor = make_actor(client, db, role="professional", category="it")
    sub = grant_membership(db, actor.user, "elite_monthly")
    assert entitlements.active_tier(db, actor.id) == Tier.ELITE
    assert entitlements.has_feature(db, actor.id, "white_label_portal")
    assert entitlements.is_active_professional(db, actor.id)
    assert entitlements.lead_fee_cents(db, actor.id) == 1500  # never waived
    for status in (
        SubscriptionStatus.SUSPENDED,
        SubscriptionStatus.PAYMENT_FAILED,
        SubscriptionStatus.EXPIRED,
        SubscriptionStatus.PENDING_APPROVAL,
        SubscriptionStatus.SUPERSEDED,
    ):
        sub.status = status
        db.add(sub)
        db.commit()
        assert entitlements.active_tier(db, actor.id) is None, status
        assert entitlements.lead_fee_cents(db, actor.id) is None
        assert not entitlements.has_feature(db, actor.id, "browse_projects")
    # cancelled keeps access only until the paid period ends
    sub.status = SubscriptionStatus.CANCELLED
    sub.current_period_end = datetime.now(UTC) + timedelta(days=3)
    db.add(sub)
    db.commit()
    assert entitlements.active_tier(db, actor.id) == Tier.ELITE
    sub.current_period_end = datetime.now(UTC) - timedelta(days=1)
    db.add(sub)
    db.commit()
    assert entitlements.active_tier(db, actor.id) is None


def test_feature_tiers(client: TestClient, db: Session) -> None:
    pro = make_actor(
        client, db, role="professional", category="it", plan="professional_monthly"
    )
    plus = make_actor(
        client, db, role="professional", category="it", plan="pro_plus_annual"
    )
    cl = make_actor(client, db, role="client", plan="client_annual")
    assert entitlements.has_feature(db, pro.id, "submit_bids")
    assert not entitlements.has_feature(db, pro.id, "estimator")
    assert entitlements.has_feature(db, plus.id, "estimator")
    assert not entitlements.has_feature(db, plus.id, "project_management_full")
    assert not entitlements.has_feature(db, cl.id, "browse_projects")
    assert entitlements.is_active_client(db, cl.id)
    assert not entitlements.feature_enabled(db, "estimator")  # unfinished -> off


def test_highest_active_tier_wins(client: TestClient, db: Session) -> None:
    actor = make_actor(
        client, db, role="professional", category="it", plan="professional_monthly"
    )
    grant_membership(db, actor.user, "pro_plus_monthly")
    assert entitlements.active_tier(db, actor.id) == Tier.PRO_PLUS


# ----------------------------------------------------------------------- privacy


def test_project_requires_client_membership_and_clean_text(
    client: TestClient, db: Session
) -> None:
    no_plan = make_actor(client, db, role="client")
    r = client.post(f"{API}/projects/", json=project_payload(), headers=no_plan.headers)
    assert r.status_code == 403
    pro = _pro(client, db)
    r = client.post(f"{API}/projects/", json=project_payload(), headers=pro.headers)
    assert r.status_code == 403
    no_profile = make_actor(client, db)
    r = client.post(
        f"{API}/projects/", json=project_payload(), headers=no_profile.headers
    )
    assert r.status_code == 403
    owner = make_actor(client, db, role="client", plan="client_monthly")
    for bad in (
        {"description": "Please email me at bob@gmail.com about this job"},
        {"title": "Call 512 555 0199 now"},
        {"service_area": "100 Main Street"},
        {"project_type": "bogus"},
        {"modifier_codes": ["unknown"]},
        {"budget_min_cents": 10, "budget_max_cents": 5},
    ):
        r = client.post(
            f"{API}/projects/", json=project_payload(**bad), headers=owner.headers
        )
        assert r.status_code == 422, bad


def test_preview_never_contains_protected_data(client: TestClient, db: Session) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db, "professional_monthly")
    outputs = [client.get(f"{API}/projects/{pid}", headers=pro.headers).text]
    pro_plus = _pro(client, db)
    outputs.append(client.get(f"{API}/projects/", headers=pro_plus.headers).text)
    for text in outputs:
        for key in PROTECTED:
            assert key not in text
        for secret in SECRETS:
            assert secret not in text
    one = client.get(f"{API}/projects/{pid}", headers=pro.headers).json()
    assert one["unlocked"] is False and one["title"]


def test_bid_without_unlock_does_not_leak(client: TestClient, db: Session) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db)
    bid = _bid(client, pro, pid)
    assert bid["status"] == "submitted"
    for secret in SECRETS:
        assert secret not in json.dumps(bid)
    assert secret not in client.get(f"{API}/projects/{pid}", headers=pro.headers).text


def test_bid_scanning_and_revision(client: TestClient, db: Session) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db)
    r = client.post(
        f"{API}/projects/{pid}/bids",
        json={
            "amount_cents": 1,
            "timeline_days": 2,
            "message": "text me 512-555-0100 directly",
        },
        headers=pro.headers,
    )
    assert r.status_code == 422
    first = _bid(client, pro, pid)
    r = client.post(
        f"{API}/projects/{pid}/bids",
        json={
            "amount_cents": 650000,
            "timeline_days": 20,
            "message": "Revised: lower price.",
        },
        headers=pro.headers,
    )
    assert r.json()["revision"] == first["revision"] + 1
    assert r.json()["id"] == first["id"]
    # owner cannot bid on own project, non-members cannot bid
    r = client.post(
        f"{API}/projects/{pid}/bids",
        json={"amount_cents": 1, "timeline_days": 2, "message": "bid on my own"},
        headers=owner.headers,
    )
    assert r.status_code == 403
    nobody = make_actor(client, db, role="professional", category="it")
    r = client.post(
        f"{API}/projects/{pid}/bids",
        json={"amount_cents": 1, "timeline_days": 2, "message": "no membership here"},
        headers=nobody.headers,
    )
    assert r.status_code == 403


def test_access_control_on_projects_and_bids(client: TestClient, db: Session) -> None:
    owner, pid = _client_with_project(client, db)
    other_client = make_actor(client, db, role="client", plan="client_monthly")
    assert (
        client.get(f"{API}/projects/{pid}", headers=other_client.headers).status_code
        == 404
    )
    assert client.get(f"{API}/projects/{pid}", headers=owner.headers).status_code == 200
    assert client.get(f"{API}/projects/{pid}").status_code == 401
    pro_a, pro_b = _pro(client, db), _pro(client, db)
    bid_a = _bid(client, pro_a, pid)
    _bid(client, pro_b, pid)
    # a professional only sees their own bid
    seen = client.get(f"{API}/projects/{pid}/bids", headers=pro_a.headers).json()
    assert [b["id"] for b in seen] == [bid_a["id"]]
    # owner sees both, with no contact data
    assert (
        len(client.get(f"{API}/projects/{pid}/bids", headers=owner.headers).json()) == 2
    )
    # others cannot shortlist/award
    for path in (f"bids/{bid_a['id']}/shortlist", f"award/{bid_a['id']}"):
        r = client.post(f"{API}/projects/{pid}/{path}", headers=pro_a.headers)
        assert r.status_code == 404
        r = client.post(f"{API}/projects/{pid}/{path}", headers=other_client.headers)
        assert r.status_code == 404
    mine = client.get(f"{API}/projects/mine", headers=owner.headers).json()
    assert len(mine) == 1 and mine[0]["contact_email"]
    assert client.get(f"{API}/projects/mine", headers=other_client.headers).json() == []


# ------------------------------------------------------------------ lead unlocks


def test_unlock_requires_shortlist_and_membership(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db)
    # no bid at all
    assert (
        client.post(f"{API}/projects/{pid}/unlock", headers=pro.headers).status_code
        == 403
    )
    _bid(client, pro, pid)
    # bid not shortlisted
    assert (
        client.post(f"{API}/projects/{pid}/unlock", headers=pro.headers).status_code
        == 403
    )
    nobody = make_actor(client, db, role="professional", category="it")
    assert (
        client.post(f"{API}/projects/{pid}/unlock", headers=nobody.headers).status_code
        == 403
    )
    assert "create_order" not in gateway.names()
    assert (
        client.get(f"{API}/projects/{pid}/unlock", headers=pro.headers).status_code
        == 404
    )


@pytest.mark.parametrize(
    "plan,fee",
    [
        ("professional_monthly", 5000),
        ("professional_annual", 5000),
        ("pro_plus_monthly", 2500),
        ("pro_plus_annual", 2500),
        ("elite_monthly", 1500),
        ("elite_annual", 1500),
    ],
)
def test_lead_fee_by_tier_is_never_waived(
    client: TestClient, db: Session, gateway: FakeGateway, plan: str, fee: int
) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db, plan)
    unlock = _paid_unlock(client, db, gateway, owner, pid, pro)
    assert unlock["fee_cents"] == fee > 0
    order = [c for c in gateway.calls if c[0] == "create_order"][-1][1]
    assert order["amount_cents"] == fee


def test_unlock_only_after_verified_payment_webhook(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db, "pro_plus_monthly")
    unlock = _paid_unlock(client, db, gateway, owner, pid, pro)
    order_id = unlock["paypal_order_id"]
    assert unlock["status"] == "pending"

    # still hidden after "approval" without a verified capture
    assert (
        "secretcorp"
        not in client.get(f"{API}/projects/{pid}", headers=pro.headers).text
    )

    # unsigned webhook is rejected and has no effect
    gateway.signature_valid = False
    r = post_event(client, _capture_event(order_id, "25.00"))
    assert r.status_code == 401
    gateway.signature_valid = True
    assert (
        "secretcorp"
        not in client.get(f"{API}/projects/{pid}", headers=pro.headers).text
    )

    # a capture for the wrong amount is not accepted
    r = post_event(client, _capture_event(order_id, "1.00"))
    assert r.status_code == 200
    assert (
        "secretcorp"
        not in client.get(f"{API}/projects/{pid}", headers=pro.headers).text
    )
    db.expire_all()
    row = db.exec(
        select(LeadUnlock).where(LeadUnlock.paypal_order_id == order_id)
    ).one()
    assert row.status == LeadUnlockStatus.FAILED

    # retry creates a fresh order for the same fee; verified capture unlocks
    again = client.post(f"{API}/projects/{pid}/unlock", headers=pro.headers).json()
    assert again["status"] == "pending"
    r = post_event(client, _capture_event(again["paypal_order_id"], "25.00", "CAP-OK"))
    assert r.json()["result"] == "processed"
    full = client.get(f"{API}/projects/{pid}", headers=pro.headers).json()
    assert full["unlocked"] is True
    assert full["contact_email"] == "jane@secretcorp.example"
    assert full["street_address"] == "100 Congress Ave"
    # identity stays private until the client awards this professional
    assert full["contact_name"] is None and full["company_name"] is None
    assert (
        client.post(f"{API}/projects/{pid}/unlock", headers=pro.headers).status_code
        == 409
    )
    status = client.get(f"{API}/projects/{pid}/unlock", headers=pro.headers).json()
    assert status["status"] == "paid"

    # another professional is still blocked
    other = _pro(client, db)
    assert (
        "secretcorp"
        not in client.get(f"{API}/projects/{pid}", headers=other.headers).text
    )

    # award reveals identity to the winner
    bid_id = client.get(f"{API}/projects/{pid}/bids", headers=pro.headers).json()[0][
        "id"
    ]
    assert (
        client.post(
            f"{API}/projects/{pid}/award/{bid_id}", headers=owner.headers
        ).status_code
        == 200
    )
    full = client.get(f"{API}/projects/{pid}", headers=pro.headers).json()
    assert full["contact_name"] == "Jane Secret"

    # a refund of the lead fee revokes access
    refund = event(
        "PAYMENT.CAPTURE.REFUNDED",
        {
            "id": "REF-1",
            "links": [{"rel": "up", "href": "https://api/v2/payments/captures/CAP-OK"}],
        },
    )
    assert post_event(client, refund).status_code == 200
    assert (
        "secretcorp"
        not in client.get(f"{API}/projects/{pid}", headers=pro.headers).text
    )


def test_non_private_project_reveals_name_after_unlock(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pid = _client_with_project(client, db, privacy_requested=False)
    pro = _pro(client, db)
    unlock = _paid_unlock(client, db, gateway, owner, pid, pro)
    assert (
        post_event(
            client, _capture_event(unlock["paypal_order_id"], "25.00")
        ).status_code
        == 200
    )
    assert (
        client.get(f"{API}/projects/{pid}", headers=pro.headers).json()["contact_name"]
        == "Jane Secret"
    )


def test_award_rules(client: TestClient, db: Session) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db)
    bid = _bid(client, pro, pid)
    assert (
        client.post(
            f"{API}/projects/{pid}/award/{bid['id']}", headers=owner.headers
        ).status_code
        == 200
    )
    # closed to further bidding and award
    assert (
        client.post(
            f"{API}/projects/{pid}/award/{bid['id']}", headers=owner.headers
        ).status_code
        == 409
    )
    other = _pro(client, db)
    r = client.post(
        f"{API}/projects/{pid}/bids",
        json={"amount_cents": 1, "timeline_days": 2, "message": "late bid here"},
        headers=other.headers,
    )
    assert r.status_code == 404
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.post(
            f"{API}/projects/{pid}/award/{missing}", headers=owner.headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{API}/projects/{pid}/bids/{missing}/shortlist", headers=owner.headers
        ).status_code
        == 404
    )


def test_browse_filters_and_early_access(client: TestClient, db: Session) -> None:
    owner, pid = _client_with_project(client, db, service_area="Zzyzx County")
    plus = _pro(client, db, "elite_monthly")
    basic = _pro(client, db, "professional_monthly")
    url = (
        f"{API}/projects/?service_area=zzyzx&category=audiovisual&project_type=upgrade"
    )
    assert [p["id"] for p in client.get(url, headers=plus.headers).json()] == [pid]
    assert client.get(url, headers=basic.headers).json() == []  # early access window
    assert (
        client.get(f"{API}/projects/?service_area=nowhere", headers=plus.headers).json()
        == []
    )


# --------------------------------------------------------------- subscriptions


def test_checkout_flow_and_activation_by_webhook(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    user = make_actor(client, db, role="professional", category="it")
    plan = db.exec(
        select(MembershipPlan).where(MembershipPlan.code == "elite_annual")
    ).one()
    # not purchasable until an admin maps the PayPal plan
    r = client.post(
        f"{API}/subscriptions/checkout",
        json={"plan_code": "elite_annual"},
        headers=user.headers,
    )
    assert r.status_code == 409
    plan.paypal_plan_id = "P-TEST-ELITE-ANNUAL"
    db.add(plan)
    db.commit()
    try:
        assert (
            client.post(
                f"{API}/subscriptions/checkout",
                json={"plan_code": "client_annual"},
                headers=user.headers,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"{API}/subscriptions/checkout",
                json={"plan_code": "zzz"},
                headers=user.headers,
            ).status_code
            == 404
        )
        r = client.post(
            f"{API}/subscriptions/checkout",
            json={"plan_code": "elite_annual"},
            headers=user.headers,
        )
        assert r.status_code == 200 and r.json()["approval_url"].startswith("https://")
        sub = db.exec(select(Subscription).where(Subscription.user_id == user.id)).one()
        # redirect alone grants nothing
        assert entitlements.active_tier(db, user.id) is None
        pp_id = sub.paypal_subscription_id
        start = datetime.now(UTC).isoformat()
        nb = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        # wrong plan id in webhook -> rejected (mapping enforced)
        bad = event(
            "BILLING.SUBSCRIPTION.ACTIVATED",
            {"id": pp_id, "plan_id": "P-OTHER", "start_time": start},
        )
        assert post_event(client, bad).status_code == 500
        ev = event(
            "BILLING.SUBSCRIPTION.ACTIVATED",
            {
                "id": pp_id,
                "plan_id": "P-TEST-ELITE-ANNUAL",
                "start_time": start,
                "billing_info": {"next_billing_time": nb},
            },
        )
        assert post_event(client, ev).json()["result"] == "processed"
        db.expire_all()
        assert entitlements.active_tier(db, user.id) == Tier.ELITE
        summary = client.get(f"{API}/subscriptions/me", headers=user.headers).json()
        assert summary["current"]["is_trial"] is True
        assert summary["current"]["plan_code"] == "elite_annual"
        assert summary["current"]["trial_end"] and summary["current"]["renewal_date"]

        # first paid cycle after the trial
        sale = event(
            "PAYMENT.SALE.COMPLETED",
            {
                "id": "SALE-1",
                "billing_agreement_id": pp_id,
                "amount": {"total": "699.00"},
            },
        )
        assert post_event(client, sale).status_code == 200
        assert (
            post_event(
                client,
                event(
                    "PAYMENT.SALE.COMPLETED",
                    {
                        "id": "SALE-1",
                        "billing_agreement_id": pp_id,
                        "amount": {"total": "699.00"},
                    },
                ),
            ).status_code
            == 200
        )
        pays = client.get(f"{API}/subscriptions/me", headers=user.headers).json()[
            "payments"
        ]
        assert [p["amount_cents"] for p in pays] == [69900]

        for et, expect in (
            ("BILLING.SUBSCRIPTION.PAYMENT.FAILED", None),
            ("BILLING.SUBSCRIPTION.SUSPENDED", None),
        ):
            assert post_event(client, event(et, {"id": pp_id})).status_code == 200
            db.expire_all()
            assert entitlements.active_tier(db, user.id) is expect
        assert (
            post_event(
                client,
                event(
                    "PAYMENT.SALE.COMPLETED",
                    {
                        "id": "SALE-2",
                        "billing_agreement_id": pp_id,
                        "amount": {"total": "699.00"},
                    },
                ),
            ).status_code
            == 200
        )
        db.expire_all()
        assert entitlements.active_tier(db, user.id) == Tier.ELITE  # recovered

        # refunded / reversed history
        assert (
            post_event(
                client, event("PAYMENT.SALE.REFUNDED", {"sale_id": "SALE-1"})
            ).status_code
            == 200
        )
        assert (
            post_event(
                client, event("PAYMENT.SALE.REVERSED", {"sale_id": "SALE-2"})
            ).status_code
            == 200
        )
        assert (
            post_event(
                client, event("PAYMENT.SALE.REFUNDED", {"sale_id": "NOPE"})
            ).status_code
            == 500
        )
        statuses = {p.status for p in db.exec(select(SubscriptionPayment)).all()}
        assert {"refunded", "reversed"} <= statuses

        # cancel through PayPal, then webhook
        cancel = client.post(
            f"{API}/subscriptions/{sub.id}/cancel", headers=user.headers
        )
        assert cancel.status_code == 200 and "cancel_subscription" in gateway.names()
        assert (
            post_event(
                client, event("BILLING.SUBSCRIPTION.CANCELLED", {"id": pp_id})
            ).status_code
            == 200
        )
        db.expire_all()
        assert (
            entitlements.active_tier(db, user.id) == Tier.ELITE
        )  # paid period remains
        assert (
            post_event(
                client, event("BILLING.SUBSCRIPTION.EXPIRED", {"id": pp_id})
            ).status_code
            == 200
        )
        db.expire_all()
        assert entitlements.active_tier(db, user.id) is None
        # others cannot cancel it
        intruder = make_actor(client, db, role="client")
        assert (
            client.post(
                f"{API}/subscriptions/{sub.id}/cancel", headers=intruder.headers
            ).status_code
            == 404
        )
    finally:
        plan.paypal_plan_id = None
        db.add(plan)
        db.commit()


def test_upgrade_supersedes_old_plan_and_update_event(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    user = make_actor(client, db, role="professional", category="it")
    plans = {p.code: p for p in db.exec(select(MembershipPlan)).all()}
    a, b = plans["professional_monthly"], plans["pro_plus_monthly"]
    a.paypal_plan_id, b.paypal_plan_id = "P-A-TEST", "P-B-TEST"
    db.add(a)
    db.add(b)
    db.commit()
    try:
        old = grant_membership(db, user.user, "professional_monthly")
        old.paypal_subscription_id = "I-OLD-1"
        db.add(old)
        new = Subscription(
            user_id=user.id, plan_id=b.id, paypal_subscription_id="I-NEW-1"
        )
        db.add(new)
        db.commit()
        assert (
            post_event(
                client, event("BILLING.SUBSCRIPTION.CREATED", {"id": "I-NEW-1"})
            ).status_code
            == 200
        )
        assert (
            post_event(
                client,
                event(
                    "BILLING.SUBSCRIPTION.ACTIVATED",
                    {"id": "I-NEW-1", "plan_id": "P-B-TEST"},
                ),
            ).status_code
            == 200
        )
        db.expire_all()
        assert db.get(Subscription, old.id).status == SubscriptionStatus.SUPERSEDED  # type: ignore[union-attr]
        assert ("cancel_subscription", {"id": "I-OLD-1"}) in gateway.calls
        assert entitlements.active_tier(db, user.id) == Tier.PRO_PLUS
        # a status event for a superseded sub does not resurrect or kill access
        assert (
            post_event(
                client, event("BILLING.SUBSCRIPTION.CANCELLED", {"id": "I-OLD-1"})
            ).status_code
            == 200
        )
        db.expire_all()
        assert db.get(Subscription, old.id).status == SubscriptionStatus.SUPERSEDED  # type: ignore[union-attr]
        # downgrade through a PayPal revise (UPDATED with another plan id)
        up = event(
            "BILLING.SUBSCRIPTION.UPDATED",
            {
                "id": "I-NEW-1",
                "plan_id": "P-A-TEST",
                "billing_info": {"next_billing_time": datetime.now(UTC).isoformat()},
            },
        )
        assert post_event(client, up).status_code == 200
        db.expire_all()
        assert entitlements.active_tier(db, user.id) == Tier.PROFESSIONAL
        assert (
            post_event(
                client,
                event(
                    "BILLING.SUBSCRIPTION.ACTIVATED", {"id": "UNKNOWN", "plan_id": "x"}
                ),
            ).status_code
            == 500
        )
    finally:
        a.paypal_plan_id = b.paypal_plan_id = None
        db.add(a)
        db.add(b)
        db.commit()


def test_checkout_paypal_failure(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    user = make_actor(client, db, role="client")
    plan = db.exec(
        select(MembershipPlan).where(MembershipPlan.code == "client_monthly")
    ).one()
    plan.paypal_plan_id = "P-CLIENT-M-TEST"
    db.add(plan)
    db.commit()
    try:
        gateway.fail = True
        r = client.post(
            f"{API}/subscriptions/checkout",
            json={"plan_code": "client_monthly"},
            headers=user.headers,
        )
        assert r.status_code == 502
        sub = grant_membership(db, user.user, "client_monthly")
        r = client.post(f"{API}/subscriptions/{sub.id}/cancel", headers=user.headers)
        assert r.status_code == 502
        gateway.fail = False
        sub.status = SubscriptionStatus.SUSPENDED
        db.add(sub)
        db.commit()
        assert (
            client.post(
                f"{API}/subscriptions/{sub.id}/cancel", headers=user.headers
            ).status_code
            == 409
        )
    finally:
        plan.paypal_plan_id = None
        db.add(plan)
        db.commit()


def test_payment_not_configured_returns_503(client: TestClient, db: Session) -> None:
    user = make_actor(client, db, role="client")
    r = client.post(
        f"{API}/subscriptions/checkout",
        json={"plan_code": "client_monthly"},
        headers=user.headers,
    )
    assert r.status_code == 503
    r = client.post(f"{API}/webhooks/paypal", content="{}")
    assert r.status_code == 503


# ---------------------------------------------------------------------- webhooks


def test_webhook_input_validation_and_duplicates(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    assert client.post(f"{API}/webhooks/paypal", content="not json").status_code == 400
    assert client.post(f"{API}/webhooks/paypal", content="[1]").status_code == 400
    assert post_event(client, {"event_type": "X", "resource": {}}).status_code == 400
    ev = event("SOME.UNKNOWN.EVENT", {})
    assert post_event(client, ev).json()["result"] == "ignored"
    assert post_event(client, ev).json()["result"] == "duplicate"
    rows = db.exec(
        select(WebhookEvent).where(WebhookEvent.paypal_event_id == ev["id"])
    ).all()
    assert len(rows) == 1 and rows[0].raw_body and rows[0].status == "ignored"


def test_duplicate_webhook_processed_once(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db)
    unlock = _paid_unlock(client, db, gateway, owner, pid, pro)
    ev = _capture_event(unlock["paypal_order_id"], "25.00")
    assert post_event(client, ev).json()["result"] == "processed"
    assert post_event(client, ev).json()["result"] == "duplicate"
    from app.marketplace.models import AuditLog

    paid = db.exec(
        select(AuditLog)
        .where(AuditLog.action == "lead_unlock.paid")
        .where(AuditLog.entity_id == unlock["unlock_id"])
    ).all()
    assert len(paid) == 1


def test_failed_event_is_retryable_and_visible_to_admin(
    client: TestClient,
    db: Session,
    gateway: FakeGateway,
    superuser_token_headers: dict[str, str],
) -> None:
    ev = event(
        "PAYMENT.CAPTURE.COMPLETED",
        {
            "id": "CAP-X",
            "status": "COMPLETED",
            "amount": {"value": "1.00"},
            "supplementary_data": {"related_ids": {"order_id": "LATER-ORDER"}},
        },
    )
    assert post_event(client, ev).status_code == 500
    row = db.exec(
        select(WebhookEvent).where(WebhookEvent.paypal_event_id == ev["id"])
    ).one()
    assert row.status == "failed" and row.error and row.attempts == 1
    # admin viewer
    r = client.get(
        f"{API}/admin/webhook-events?status=failed", headers=superuser_token_headers
    )
    assert r.status_code == 200 and any(
        e["paypal_event_id"] == ev["id"] for e in r.json()
    )
    assert "raw_body" not in r.json()[0]
    # once the order exists, the same event id is retried and succeeds
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db)
    unlock = _paid_unlock(client, db, gateway, owner, pid, pro)
    row_unlock = db.exec(
        select(LeadUnlock).where(LeadUnlock.id == unlock["unlock_id"])
    ).one()
    row_unlock.paypal_order_id = "LATER-ORDER"
    db.add(row_unlock)
    db.commit()
    r = client.post(
        f"{API}/admin/webhook-events/{row.id}/reprocess",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200 and r.json()["result"] == "processed"
    r = client.post(
        f"{API}/admin/webhook-events/{row.id}/reprocess",
        headers=superuser_token_headers,
    )
    assert r.json()["result"] == "duplicate"
    assert (
        client.post(
            f"{API}/admin/webhook-events/00000000-0000-0000-0000-000000000000/reprocess",
            headers=superuser_token_headers,
        ).status_code
        == 404
    )


def test_reprocess_failure_returns_500(
    client: TestClient,
    db: Session,
    gateway: FakeGateway,
    superuser_token_headers: dict[str, str],
) -> None:
    ev = event(
        "PAYMENT.CAPTURE.DENIED",
        {"supplementary_data": {"related_ids": {"order_id": "NOPE"}}},
    )
    assert post_event(client, ev).status_code == 500
    row = db.exec(
        select(WebhookEvent).where(WebhookEvent.paypal_event_id == ev["id"])
    ).one()
    r = client.post(
        f"{API}/admin/webhook-events/{row.id}/reprocess",
        headers=superuser_token_headers,
    )
    assert r.status_code == 500
    db.expire_all()
    assert db.get(WebhookEvent, row.id).attempts == 2  # type: ignore[union-attr]


# ----------------------------------------------------------- payments / milestones


def _awarded(
    client: TestClient, db: Session, delayed: bool = True, onboarded: bool = True
):
    owner, pid = _client_with_project(client, db)
    pro = _pro(client, db, "elite_monthly")
    bid = _bid(client, pro, pid)
    assert (
        client.post(
            f"{API}/projects/{pid}/award/{bid['id']}", headers=owner.headers
        ).status_code
        == 200
    )
    if onboarded:
        db.add(
            SellerAccount(
                user_id=pro.id,
                paypal_merchant_id="MERCH123",
                onboarding_complete=True,
                payments_receivable=True,
                delayed_disbursement_enabled=delayed,
            )
        )
        db.commit()
    r = client.post(
        f"{API}/projects/{pid}/milestones",
        json={"title": "Phase 1", "amount_cents": 100000},
        headers=owner.headers,
    )
    assert r.status_code == 200
    return owner, pro, pid, r.json()["id"]


def test_milestone_hold_release_flow(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pro, pid, mid = _awarded(client, db)
    # marketplace fee comes from admin configuration only
    r = client.post(f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers)
    assert r.status_code == 200, r.text
    assert (
        r.json()["disbursement_mode"] == "delayed"
        and "held for scheduled release" in r.json()["notice"]
    )
    assert "escrow" not in r.text.lower()
    order_id = r.json()["paypal_order_id"]
    order_call = [c for c in gateway.calls if c[0] == "create_order"][-1][1]
    assert (
        order_call["seller_merchant_id"] == "MERCH123"
        and order_call["delayed_disbursement"] is True
    )
    assert order_call["fee_cents"] == 0

    # cannot fund twice / approve before PayPal confirms
    assert (
        client.post(
            f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{API}/milestones/{mid}/approve", headers=owner.headers
        ).status_code
        == 409
    )

    # redirect/approval alone: server captures but nothing is marked held
    approved = event("CHECKOUT.ORDER.APPROVED", {"id": order_id})
    assert post_event(client, approved).status_code == 200
    assert ("capture_order", {"order_id": order_id}) in gateway.calls
    payment = db.exec(select(Payment).where(Payment.paypal_order_id == order_id)).one()
    assert payment.status == PaymentStatus.PENDING_PAYMENT

    # capture without confirmed delayed disbursement -> manual review, not "secured"
    cap = _capture_event(order_id, "1000.00", "CAP-A")
    assert post_event(client, cap).status_code == 200
    db.expire_all()
    p = db.get(Payment, payment.id)
    assert p.status == PaymentStatus.MANUAL_REVIEW_REQUIRED  # type: ignore[union-attr]

    # fresh payment with PayPal-confirmed delayed disbursement
    owner2, pro2, pid2, mid2 = _awarded(client, db)
    r = client.post(f"{API}/milestones/{mid2}/fund", json={}, headers=owner2.headers)
    order2 = r.json()["paypal_order_id"]
    assert (
        post_event(
            client,
            _capture_event(order2, "1000.00", "CAP-B", disbursement_mode="DELAYED"),
        ).status_code
        == 200
    )
    db.expire_all()
    pay2 = db.exec(select(Payment).where(Payment.paypal_order_id == order2)).one()
    assert pay2.status == PaymentStatus.HELD_PENDING_DISBURSEMENT
    assert pay2.paypal_capture_id == "CAP-B" and pay2.release_deadline is not None
    assert db.get(Milestone, pay2.milestone_id).status == MilestoneStatus.FUNDED  # type: ignore[union-attr, arg-type]

    # non-owner cannot approve; owner can; release is confirmed only by webhook
    assert (
        client.post(
            f"{API}/milestones/{mid2}/approve", headers=pro2.headers
        ).status_code
        == 404
    )
    r = client.post(f"{API}/milestones/{mid2}/approve", headers=owner2.headers)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    rel = [c for c in gateway.calls if c[0] == "release_funds"][-1][1]
    assert rel["capture_id"] == "CAP-B" and rel["amount_cents"] == 100000
    db.expire_all()
    assert db.get(Payment, pay2.id).status == PaymentStatus.HELD_PENDING_DISBURSEMENT  # type: ignore[union-attr]

    # partial then full release via verified payout webhooks
    part = event(
        "PAYMENT.REFERENCED-PAYOUT-ITEM.COMPLETED",
        {"reference_id": "CAP-B", "payout_amount": {"value": "400.00"}},
    )
    assert post_event(client, part).status_code == 200
    db.expire_all()
    assert db.get(Payment, pay2.id).status == PaymentStatus.PARTIALLY_RELEASED  # type: ignore[union-attr]
    rest = event(
        "PAYMENT.REFERENCED-PAYOUT-ITEM.COMPLETED",
        {
            "supplementary_data": {"related_ids": {"capture_id": "CAP-B"}},
            "payout_amount": {"value": "600.00"},
        },
    )
    assert post_event(client, rest).status_code == 200
    db.expire_all()
    assert db.get(Payment, pay2.id).status == PaymentStatus.RELEASED  # type: ignore[union-attr]
    assert db.get(Milestone, pay2.milestone_id).status == MilestoneStatus.RELEASED  # type: ignore[union-attr, arg-type]
    unknown = event(
        "PAYMENT.REFERENCED-PAYOUT-ITEM.COMPLETED",
        {"reference_id": "NOPE", "payout_amount": {"value": "1"}},
    )
    assert post_event(client, unknown).status_code == 500

    visible = client.get(f"{API}/projects/{pid2}/payments", headers=pro2.headers).json()
    assert len(visible) == 1
    # another client sees none of this project's payments
    assert (
        client.get(f"{API}/projects/{pid2}/payments", headers=owner.headers).json()
        == []
    )
    assert (
        client.get(
            f"{API}/projects/{pid2}/payments", headers=owner2.headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{API}/projects/00000000-0000-0000-0000-000000000000/payments",
            headers=owner2.headers,
        ).status_code
        == 404
    )


def test_fee_is_admin_configured(
    client: TestClient,
    db: Session,
    gateway: FakeGateway,
    superuser_token_headers: dict[str, str],
) -> None:
    owner, pro, pid, mid = _awarded(client, db)
    assert (
        client.put(
            f"{API}/admin/settings/marketplace-fee",
            json={"basis_points": 500},
            headers=owner.headers,
        ).status_code
        == 403
    )
    r = client.put(
        f"{API}/admin/settings/marketplace-fee",
        json={"basis_points": 500},
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    try:
        r = client.post(f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers)
        order_call = [c for c in gateway.calls if c[0] == "create_order"][-1][1]
        assert order_call["fee_cents"] == 5000
        order = r.json()["paypal_order_id"]
        assert (
            post_event(
                client,
                _capture_event(order, "1000.00", "CAP-F", disbursement_mode="DELAYED"),
            ).status_code
            == 200
        )
        pay = db.exec(select(Payment).where(Payment.paypal_order_id == order)).one()
        client.post(f"{API}/milestones/{mid}/approve", headers=owner.headers)
        rel = [c for c in gateway.calls if c[0] == "release_funds"][-1][1]
        assert rel["amount_cents"] == 95000
        assert pay.fee_cents == 5000
    finally:
        client.put(
            f"{API}/admin/settings/marketplace-fee",
            json={"basis_points": 0},
            headers=superuser_token_headers,
        )


def test_direct_payment_fallback_is_disclosed(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pro, pid, mid = _awarded(client, db, delayed=False)
    r = client.post(f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers)
    assert r.status_code == 422 and "not held" in r.json()["detail"]
    r = client.post(
        f"{API}/milestones/{mid}/fund",
        json={"accept_direct_payment": True},
        headers=owner.headers,
    )
    assert r.status_code == 200 and r.json()["disbursement_mode"] == "direct"
    assert "not held" in r.json()["notice"]
    order = r.json()["paypal_order_id"]
    assert (
        post_event(client, _capture_event(order, "1000.00", "CAP-D")).status_code == 200
    )
    pay = db.exec(select(Payment).where(Payment.paypal_order_id == order)).one()
    assert pay.status == PaymentStatus.CAPTURED  # never "held"
    assert (
        client.post(
            f"{API}/milestones/{mid}/approve", headers=owner.headers
        ).status_code
        == 200
    )
    db.expire_all()
    assert db.get(Payment, pay.id).status == PaymentStatus.RELEASED  # type: ignore[union-attr]
    assert gateway.names().count("release_funds") == 0


def test_fund_requirements(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pro, pid, mid = _awarded(client, db, onboarded=False)
    assert (
        client.post(
            f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{API}/milestones/{mid}/fund", json={}, headers=pro.headers
        ).status_code
        == 404
    )
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.post(
            f"{API}/milestones/{missing}/fund", json={}, headers=owner.headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{API}/milestones/{missing}/approve", headers=owner.headers
        ).status_code
        == 404
    )
    # milestone needs an awarded project, and only the owner can add them
    o2, pid2 = _client_with_project(client, db)
    body = {"title": "x", "amount_cents": 100}
    assert (
        client.post(
            f"{API}/projects/{pid2}/milestones", json=body, headers=o2.headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{API}/projects/{pid2}/milestones", json=body, headers=pro.headers
        ).status_code
        == 404
    )
    # PayPal failure creating order
    db.add(
        SellerAccount(
            user_id=pro.id,
            paypal_merchant_id="M1",
            onboarding_complete=True,
            payments_receivable=True,
            delayed_disbursement_enabled=True,
        )
    )
    db.commit()
    gateway.fail = True
    assert (
        client.post(
            f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers
        ).status_code
        == 502
    )


def test_release_failure_keeps_funds_held(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    owner, pro, pid, mid = _awarded(client, db)
    order = client.post(
        f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers
    ).json()["paypal_order_id"]
    post_event(
        client, _capture_event(order, "1000.00", "CAP-R", disbursement_mode="DELAYED")
    )
    gateway.fail = True
    assert (
        client.post(
            f"{API}/milestones/{mid}/approve", headers=owner.headers
        ).status_code
        == 502
    )
    db.expire_all()
    assert db.get(Milestone, mid).status == MilestoneStatus.FUNDED  # type: ignore[union-attr, arg-type]


def test_refund_dispute_reverse_and_failed(
    client: TestClient,
    db: Session,
    gateway: FakeGateway,
    superuser_token_headers: dict[str, str],
) -> None:
    def held(cap: str) -> tuple[Actor, str, Payment]:
        owner, pro, pid, mid = _awarded(client, db)
        order = client.post(
            f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers
        ).json()["paypal_order_id"]
        post_event(
            client, _capture_event(order, "1000.00", cap, disbursement_mode="DELAYED")
        )
        return (
            owner,
            mid,
            db.exec(select(Payment).where(Payment.paypal_order_id == order)).one(),
        )

    owner, mid, pay = held("CAP-REFUND")
    # only admins can refund
    assert (
        client.post(
            f"{API}/admin/payments/{pay.id}/refund", headers=owner.headers
        ).status_code
        == 403
    )
    r = client.post(
        f"{API}/admin/payments/{pay.id}/refund", headers=superuser_token_headers
    )
    assert r.status_code == 200
    assert ("refund_capture", {"capture_id": "CAP-REFUND"}) in gateway.calls
    db.expire_all()
    assert db.get(Payment, pay.id).status == PaymentStatus.HELD_PENDING_DISBURSEMENT  # type: ignore[union-attr]
    refund = event(
        "PAYMENT.CAPTURE.REFUNDED",
        {
            "id": "R1",
            "links": [
                {"rel": "up", "href": "https://x/v2/payments/captures/CAP-REFUND"}
            ],
        },
    )
    assert post_event(client, refund).status_code == 200
    db.expire_all()
    assert db.get(Payment, pay.id).status == PaymentStatus.REFUNDED  # type: ignore[union-attr]
    # refunded payments cannot be released or refunded again
    assert (
        client.post(
            f"{API}/admin/payments/{pay.id}/refund", headers=superuser_token_headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{API}/milestones/{mid}/approve", headers=owner.headers
        ).status_code
        == 409
    )
    # out-of-order event does not resurrect it
    late = _capture_event(
        pay.paypal_order_id or "", "1000.00", "CAP-REFUND", disbursement_mode="DELAYED"
    )
    assert post_event(client, late).status_code == 200
    db.expire_all()
    assert db.get(Payment, pay.id).status == PaymentStatus.REFUNDED  # type: ignore[union-attr]

    _, _, disputed = held("CAP-DISPUTE")
    dispute = event(
        "CUSTOMER.DISPUTE.CREATED",
        {"disputed_transactions": [{"seller_transaction_id": "CAP-DISPUTE"}]},
    )
    assert post_event(client, dispute).status_code == 200
    db.expire_all()
    assert db.get(Payment, disputed.id).status == PaymentStatus.DISPUTED  # type: ignore[union-attr]

    _, _, reversed_ = held("CAP-REV")
    assert (
        post_event(
            client, event("PAYMENT.CAPTURE.REVERSED", {"id": "CAP-REV"})
        ).status_code
        == 200
    )
    db.expire_all()
    assert db.get(Payment, reversed_.id).status == PaymentStatus.REVERSED  # type: ignore[union-attr]
    assert (
        post_event(
            client, event("PAYMENT.CAPTURE.REVERSED", {"id": "NOPE"})
        ).status_code
        == 500
    )

    # invalid transition -> manual review (reversed funds then "released")
    owner3, mid3, p3 = held("CAP-ODD")
    p3.status = PaymentStatus.RELEASED
    db.add(p3)
    db.commit()
    assert (
        post_event(
            client,
            event(
                "PAYMENT.CAPTURE.DENIED",
                {
                    "supplementary_data": {
                        "related_ids": {"order_id": p3.paypal_order_id}
                    }
                },
            ),
        ).status_code
        == 200
    )
    db.expire_all()
    assert db.get(Payment, p3.id).status == PaymentStatus.MANUAL_REVIEW_REQUIRED  # type: ignore[union-attr]

    # authorization event
    _, _, pending = (lambda t: (t[0], t[1], t[2]))(_pending(client, db))
    auth = event(
        "PAYMENT.AUTHORIZATION.CREATED",
        {
            "id": "AUTH-1",
            "supplementary_data": {
                "related_ids": {"order_id": pending.paypal_order_id}
            },
        },
    )
    assert post_event(client, auth).status_code == 200
    db.expire_all()
    p = db.get(Payment, pending.id)
    assert (
        p.status == PaymentStatus.AUTHORIZED and p.paypal_authorization_id == "AUTH-1"
    )  # type: ignore[union-attr]
    assert (
        post_event(
            client,
            event(
                "PAYMENT.AUTHORIZATION.CREATED",
                {
                    "id": "A",
                    "supplementary_data": {"related_ids": {"order_id": "NOPE"}},
                },
            ),
        ).status_code
        == 500
    )

    # denied capture on a pending payment fails it
    _, _, pend2 = _pending(client, db)
    assert (
        post_event(
            client,
            event(
                "PAYMENT.CAPTURE.DECLINED",
                {
                    "supplementary_data": {
                        "related_ids": {"order_id": pend2.paypal_order_id}
                    }
                },
            ),
        ).status_code
        == 200
    )
    db.expire_all()
    assert db.get(Payment, pend2.id).status == PaymentStatus.FAILED  # type: ignore[union-attr]
    # pending capture status is ignored until completed
    assert (
        post_event(
            client, _capture_event("whatever", "1.00", status="PENDING")
        ).status_code
        == 200
    )


def _pending(client: TestClient, db: Session) -> tuple[Actor, str, Payment]:
    owner, pro, pid, mid = _awarded(client, db)
    order = client.post(
        f"{API}/milestones/{mid}/fund", json={}, headers=owner.headers
    ).json()["paypal_order_id"]
    return (
        owner,
        mid,
        db.exec(select(Payment).where(Payment.paypal_order_id == order)).one(),
    )


def test_release_deadline_queue(
    client: TestClient,
    db: Session,
    gateway: FakeGateway,
    superuser_token_headers: dict[str, str],
) -> None:
    owner, mid, pay = _pending(client, db)
    assert (
        client.get(
            f"{API}/admin/payments/release-queue", headers=owner.headers
        ).status_code
        == 403
    )
    pay.status = PaymentStatus.HELD_PENDING_DISBURSEMENT
    pay.release_deadline = datetime.now(UTC) + timedelta(days=2)
    db.add(pay)
    db.commit()
    ids = [
        p["id"]
        for p in client.get(
            f"{API}/admin/payments/release-queue", headers=superuser_token_headers
        ).json()
    ]
    assert str(pay.id) in ids
    pay.release_deadline = datetime.now(UTC) + timedelta(
        days=settings.PAYPAL_MAX_DISBURSEMENT_DAYS
    )
    db.add(pay)
    db.commit()
    ids = [
        p["id"]
        for p in client.get(
            f"{API}/admin/payments/release-queue", headers=superuser_token_headers
        ).json()
    ]
    assert str(pay.id) not in ids
    allp = client.get(
        f"{API}/admin/payments?status=held_pending_disbursement",
        headers=superuser_token_headers,
    ).json()
    assert str(pay.id) in [p["id"] for p in allp]


# ---------------------------------------------------------------------- sellers


def test_seller_onboarding_events_and_not_available_endpoint(
    client: TestClient, db: Session, gateway: FakeGateway
) -> None:
    pro = _pro(client, db)
    assert (
        client.get(f"{API}/seller/status", headers=pro.headers).json()[
            "onboarding_complete"
        ]
        is False
    )
    assert client.post(f"{API}/seller/onboard", headers=pro.headers).status_code == 501
    db.add(SellerAccount(user_id=pro.id))
    db.commit()
    done = event(
        "MERCHANT.ONBOARDING.COMPLETED",
        {"tracking_id": str(pro.id), "merchant_id": "MERCH-9"},
    )
    assert post_event(client, done).status_code == 200
    cap = event(
        "CUSTOMER.MERCHANT-INTEGRATION.CAPABILITY-UPDATED",
        {
            "merchant_id": "MERCH-9",
            "payments_receivable": True,
            "capabilities": [{"name": "DELAY_FUNDS_DISBURSEMENT", "status": "ACTIVE"}],
        },
    )
    assert post_event(client, cap).status_code == 200
    s = client.get(f"{API}/seller/status", headers=pro.headers).json()
    assert s == {
        "onboarding_complete": True,
        "payments_receivable": True,
        "delayed_disbursement_enabled": True,
    }
    revoke = event(
        "CUSTOMER.MERCHANT-INTEGRATION.CAPABILITY-UPDATED",
        {"merchant_id": "MERCH-9", "capabilities": []},
    )
    assert post_event(client, revoke).status_code == 200
    assert (
        client.get(f"{API}/seller/status", headers=pro.headers).json()[
            "delayed_disbursement_enabled"
        ]
        is False
    )
    assert (
        post_event(
            client,
            event("MERCHANT.ONBOARDING.COMPLETED", {"tracking_id": "not-a-uuid"}),
        ).status_code
        == 500
    )
    assert (
        post_event(
            client,
            event(
                "CUSTOMER.MERCHANT-INTEGRATION.CAPABILITY-UPDATED", {"merchant_id": "?"}
            ),
        ).status_code
        == 500
    )


# ---------------------------------------------------------------- profiles/admin


def test_profiles(client: TestClient, db: Session) -> None:
    u = make_actor(client, db)
    p = f"{API}/profile"
    assert client.get(f"{p}/me", headers=u.headers).status_code == 403
    assert (
        client.post(
            p, json={"role": "support", "display_name": "x"}, headers=u.headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            p, json={"role": "professional", "display_name": "Pro"}, headers=u.headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            p,
            json={"role": "client", "display_name": "call 512-555-0199"},
            headers=u.headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            p, json={"role": "client", "display_name": "  "}, headers=u.headers
        ).status_code
        == 422
    )
    r = client.post(
        p,
        json={"role": "client", "display_name": "Acme Corp", "is_company": True},
        headers=u.headers,
    )
    assert r.status_code == 200
    assert (
        client.post(
            p, json={"role": "client", "display_name": "Again"}, headers=u.headers
        ).status_code
        == 409
    )
    assert (
        client.get(f"{p}/me", headers=u.headers).json()["display_name"] == "Acme Corp"
    )


def test_admin_controls(
    client: TestClient, db: Session, superuser_token_headers: dict[str, str]
) -> None:
    user = make_actor(client, db, role="client")
    base = f"{API}/admin"
    for path in (
        "feature-flags",
        "audit-logs",
        "webhook-events",
        "remote-support",
        "payments",
    ):
        assert client.get(f"{base}/{path}", headers=user.headers).status_code == 403
        assert client.get(f"{base}/{path}").status_code == 401
        assert (
            client.get(f"{base}/{path}", headers=superuser_token_headers).status_code
            == 200
        )
    flags = {
        f["key"]: f["enabled"]
        for f in client.get(
            f"{base}/feature-flags", headers=superuser_token_headers
        ).json()
    }
    assert flags["estimator"] is False and flags["remote_support"] is False
    assert (
        client.put(
            f"{base}/feature-flags/nope",
            json={"enabled": True},
            headers=superuser_token_headers,
        ).status_code
        == 404
    )
    r = client.put(
        f"{base}/feature-flags/messaging",
        json={"enabled": True},
        headers=superuser_token_headers,
    )
    assert r.json()["enabled"] is True
    client.put(
        f"{base}/feature-flags/messaging",
        json={"enabled": False},
        headers=superuser_token_headers,
    )
    rs = client.get(f"{base}/remote-support", headers=superuser_token_headers).json()
    assert (
        rs["operational"] is False
        and rs["provider"] == "placeholder"
        and rs["requirements"]
    )

    # plan mapping
    r = client.put(
        f"{base}/plans/client_monthly",
        json={"paypal_plan_id": "P-MAP-1", "lead_fee_cents": 1},
        headers=superuser_token_headers,
    )
    assert r.status_code == 422
    r = client.put(
        f"{base}/plans/client_monthly",
        json={"paypal_plan_id": "P-MAP-1"},
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    r = client.put(
        f"{base}/plans/client_annual",
        json={"paypal_plan_id": "P-MAP-1"},
        headers=superuser_token_headers,
    )
    assert r.status_code == 409
    assert (
        client.put(
            f"{base}/plans/nope", json={}, headers=superuser_token_headers
        ).status_code
        == 404
    )
    r = client.put(
        f"{base}/plans/client_monthly",
        json={"paypal_plan_id": None},
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    r = client.put(
        f"{base}/plans/elite_monthly",
        json={"lead_fee_cents": 1500, "is_active": True},
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    # additional plans only by admin
    new = {
        "code": "custom_enterprise",
        "name": "Custom",
        "tier": "elite",
        "interval": "year",
        "price_cents": 100000,
        "lead_fee_cents": 1000,
    }
    assert (
        client.post(f"{base}/plans", json=new, headers=user.headers).status_code == 403
    )
    assert (
        client.post(
            f"{base}/plans", json=new, headers=superuser_token_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{base}/plans", json=new, headers=superuser_token_headers
        ).status_code
        == 409
    )
    db.exec(select(MembershipPlan).where(MembershipPlan.code == "custom_enterprise"))
    from sqlmodel import delete

    db.exec(delete(MembershipPlan).where(MembershipPlan.code == "custom_enterprise"))  # type: ignore[arg-type]
    db.commit()

    # modifiers
    assert (
        client.put(
            f"{base}/modifiers/nope", json={}, headers=superuser_token_headers
        ).status_code
        == 404
    )
    r = client.put(
        f"{base}/modifiers/urgent_request",
        json={"price_cents": 3500},
        headers=superuser_token_headers,
    )
    assert r.json()["price_cents"] == 3500
    client.put(
        f"{base}/modifiers/urgent_request",
        json={"price_cents": 3000},
        headers=superuser_token_headers,
    )
    logs = client.get(
        f"{base}/audit-logs?action=modifier.updated", headers=superuser_token_headers
    ).json()
    assert logs and all(entry["action"] == "modifier.updated" for entry in logs)
    assert (
        client.put(
            f"{base}/settings/marketplace-fee",
            json={"basis_points": 20000},
            headers=superuser_token_headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"{base}/payments?status=failed&limit=5", headers=superuser_token_headers
        ).status_code
        == 200
    )


def test_refund_requires_valid_state_and_paypal(
    client: TestClient,
    db: Session,
    gateway: FakeGateway,
    superuser_token_headers: dict[str, str],
) -> None:
    owner, mid, pay = _pending(client, db)
    base = f"{API}/admin/payments"
    assert (
        client.post(
            f"{base}/{pay.id}/refund", headers=superuser_token_headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{base}/00000000-0000-0000-0000-000000000000/refund",
            headers=superuser_token_headers,
        ).status_code
        == 404
    )
    pay.status, pay.paypal_capture_id = PaymentStatus.CAPTURED, "CAP-Z"
    db.add(pay)
    db.commit()
    gateway.fail = True
    assert (
        client.post(
            f"{base}/{pay.id}/refund", headers=superuser_token_headers
        ).status_code
        == 502
    )
