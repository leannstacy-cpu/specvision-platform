"""Remote-support integration boundary (placeholder -- NOT operational).

SPECVision will not implement its own remote-control protocol. A legally
compliant third-party provider must be plugged in behind ``RemoteSupportProvider``.
Any implementation must satisfy the rules in ``REQUIREMENTS``.
"""

from dataclasses import dataclass
from typing import Protocol

REQUIREMENTS = (
    "explicit client consent for every session via a visible approval screen",
    "short-lived, single-use session tokens",
    "role-based technician access and multi-factor authentication",
    "client-controlled start, stop and revocation",
    "timestamps and audit logging for every session",
    "automatic expiration; no unattended access without separate explicit authorization",
    "no storage of plain-text credentials",
)


class RemoteSupportUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SessionRequest:
    client_user_id: str
    technician_user_id: str
    project_id: str
    reason: str


class RemoteSupportProvider(Protocol):
    name: str

    def request_session(self, request: SessionRequest) -> str: ...

    def revoke_session(self, session_ref: str) -> None: ...


class PlaceholderRemoteSupportProvider:
    """Safe default: refuses to start anything."""

    name = "placeholder"

    def request_session(self, request: SessionRequest) -> str:
        raise RemoteSupportUnavailable(
            "No remote-support provider is configured; remote sessions are disabled."
        )

    def revoke_session(self, session_ref: str) -> None:
        raise RemoteSupportUnavailable("No remote-support provider is configured.")


def get_remote_support_provider() -> RemoteSupportProvider:
    return PlaceholderRemoteSupportProvider()
