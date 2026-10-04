import pytest

from app.marketplace.contact_scanner import (
    contains_contact_info,
    describe_findings,
    find_contact_info,
)
from app.marketplace.enums import PaymentStatus
from app.marketplace.payments import ALLOWED, can_transition
from app.marketplace.paypal import money
from app.marketplace.remote_support import (
    PlaceholderRemoteSupportProvider,
    RemoteSupportUnavailable,
    SessionRequest,
    get_remote_support_provider,
)


@pytest.mark.parametrize(
    "text,kind",
    [
        ("mail me at bob@example.com", "email"),
        ("bob at example dot com", "email"),
        ("call 512-555-0142", "phone"),
        ("five one two five five five zero one four two", "phone"),
        ("5 1 2 . 5 5 5 . 0 1 4 2", "phone"),
        ("see www.example.com", "website"),
        ("visit acme-av.io/contact", "website"),
        ("find me on whatsapp", "social"),
        ("pay via venmo", "payment_handle"),
        ("send to $cashhandle", "payment_handle"),
        ("I'm at 100 Congress Ave", "street_address"),
    ],
)
def test_scanner_flags_contact_attempts(text: str, kind: str) -> None:
    assert kind in {f.kind for f in find_contact_info(text)}
    assert contains_contact_info(text)
    assert describe_findings(text)


@pytest.mark.parametrize(
    "text",
    [
        "Install 12 displays across 3 rooms with 4K projectors",
        "Budget between 10,000 and 15,000 within 6 weeks",
        "",
        None,
    ],
)
def test_scanner_allows_normal_text(text: str | None) -> None:
    assert find_contact_info(text) == []
    assert describe_findings(text) is None


def test_payment_state_machine() -> None:
    assert set(ALLOWED) == set(PaymentStatus)
    assert can_transition("pending_payment", PaymentStatus.CAPTURED)
    assert not can_transition("refunded", PaymentStatus.RELEASED)
    assert not can_transition("draft", PaymentStatus.RELEASED)
    assert len(PaymentStatus) == 13


def test_money_format() -> None:
    assert money(5000) == "50.00"
    assert money(1999) == "19.99"
    assert money(5) == "0.05"


def test_remote_support_placeholder_refuses() -> None:
    provider = get_remote_support_provider()
    assert isinstance(provider, PlaceholderRemoteSupportProvider)
    with pytest.raises(RemoteSupportUnavailable):
        provider.request_session(SessionRequest("c", "t", "p", "fix"))
    with pytest.raises(RemoteSupportUnavailable):
        provider.revoke_session("x")
