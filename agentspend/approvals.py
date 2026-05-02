"""Approval workflows.

When a transaction is REQUIRE_APPROVAL, we need to (a) record the
request, (b) notify a human somehow, (c) wait for an answer.

In v0.1 we ship a Protocol for the approver and an in-memory reference
implementation. Real users will write their own that posts to Slack,
sends an email, or fires a webhook. The point is: don't make us a hard
dependency on Slack SDK / Twilio / etc.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from .types import Transaction


@dataclass
class ApprovalRequest:
    """An outstanding approval request."""

    request_id: str
    transaction: Transaction
    reason: str  # why approval was needed
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    resolved: bool = False
    approved: bool = False
    resolver: str | None = None  # who approved/denied
    resolved_at: datetime | None = None


class ApprovalSink(Protocol):
    """Anything that can request and resolve approvals."""

    def request(self, txn: Transaction, reason: str) -> str:
        """Create an approval request. Returns request_id."""
        ...

    def is_approved(self, request_id: str) -> bool | None:
        """Return True if approved, False if denied, None if pending."""
        ...


class InMemoryApprovalSink:
    """Reference implementation: stores requests in memory, lets you
    approve/deny them programmatically. Good for tests, demos, and as
    a base class for real Slack/email-backed implementations.
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def request(self, txn: Transaction, reason: str) -> str:
        request_id = str(uuid4())
        with self._lock:
            self._requests[request_id] = ApprovalRequest(
                request_id=request_id, transaction=txn, reason=reason
            )
        return request_id

    def is_approved(self, request_id: str) -> bool | None:
        with self._lock:
            req = self._requests.get(request_id)
            if not req or not req.resolved:
                return None
            return req.approved

    def approve(self, request_id: str, resolver: str = "system") -> None:
        self._resolve(request_id, True, resolver)

    def deny(self, request_id: str, resolver: str = "system") -> None:
        self._resolve(request_id, False, resolver)

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            return [r for r in self._requests.values() if not r.resolved]

    def _resolve(self, request_id: str, approved: bool, resolver: str) -> None:
        with self._lock:
            req = self._requests.get(request_id)
            if not req:
                raise KeyError(f"no such approval request: {request_id}")
            if req.resolved:
                raise ValueError(
                    f"approval {request_id} already resolved"
                )
            req.resolved = True
            req.approved = approved
            req.resolver = resolver
            req.resolved_at = datetime.now(timezone.utc)
