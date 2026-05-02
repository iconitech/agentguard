"""Core types for agentguard.

Keep this module dependency-free so it can be imported anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class Decision(str, Enum):
    """The outcome of evaluating a transaction against policy."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class Transaction:
    """A proposed (not yet executed) payment by an agent.

    This is the unit of authorization. Everything the policy engine
    evaluates is shaped like this.
    """

    agent_id: str
    amount_cents: int  # store as integer cents to avoid float drift
    currency: str  # ISO 4217, e.g. "USD"
    vendor: str  # opaque string the user defines, e.g. "anthropic-api"
    category: str | None = None  # e.g. "compute", "data", "subscription"
    metadata: dict[str, Any] = field(default_factory=dict)
    workflow_id: str | None = None  # which workflow/run this came from
    human_owner: str | None = None  # email or user ID of the responsible human
    transaction_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def amount_dollars(self) -> float:
        """Convenience accessor for human-readable amount. Do not use for math."""
        return self.amount_cents / 100


@dataclass
class PolicyResult:
    """The result of running a transaction through the policy engine."""

    decision: Decision
    transaction: Transaction
    matched_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    approval_request_id: str | None = None  # set if REQUIRE_APPROVAL

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


class PolicyError(Exception):
    """Base exception for all agentguard policy errors."""


class PolicyDenied(PolicyError):
    """Raised when a transaction is denied by policy."""

    def __init__(self, result: PolicyResult):
        self.result = result
        reasons = "; ".join(result.reasons) or "no reason given"
        super().__init__(
            f"Transaction {result.transaction.transaction_id} denied: {reasons}"
        )


class ApprovalRequired(PolicyError):
    """Raised when a transaction needs human approval before proceeding."""

    def __init__(self, result: PolicyResult):
        self.result = result
        super().__init__(
            f"Transaction {result.transaction.transaction_id} requires approval "
            f"(request_id={result.approval_request_id})"
        )


class PolicyConfigError(PolicyError):
    """Raised when a policy file is malformed or invalid."""
