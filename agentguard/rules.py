"""Declarative policy rules.

Each Rule takes a Transaction (and a SpendTracker, for limits that
care about history) and returns a RuleVerdict. The engine combines
these into a final Decision.

Design philosophy: rules are small, composable, easy to read, easy
to test. No magic. If users want complex logic, they write a Python
callable hook (see hooks.py). Most users never need to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .tracking import SpendTracker
from .types import Decision, PolicyConfigError, Transaction


@dataclass
class RuleVerdict:
    """One rule's opinion about one transaction."""

    decision: Decision
    rule_name: str
    reason: str | None = None


class Rule(ABC):
    """Base class for all rules."""

    name: str

    @abstractmethod
    def evaluate(
        self, txn: Transaction, tracker: SpendTracker
    ) -> RuleVerdict | None:
        """Return a verdict, or None if this rule doesn't apply to this txn."""


# ---------------------------------------------------------------------------
# Concrete rules
# ---------------------------------------------------------------------------


class PerTransactionLimit(Rule):
    """Cap on a single transaction. Above hard_max -> deny.
    Above approval_threshold -> require approval."""

    def __init__(
        self,
        agent_id: str | None,
        hard_max_cents: int | None,
        approval_threshold_cents: int | None,
    ) -> None:
        self.name = f"per_transaction_limit({agent_id or '*'})"
        self.agent_id = agent_id  # None = applies to all agents
        self.hard_max = hard_max_cents
        self.approval_threshold = approval_threshold_cents

    def evaluate(
        self, txn: Transaction, tracker: SpendTracker
    ) -> RuleVerdict | None:
        if self.agent_id and txn.agent_id != self.agent_id:
            return None
        if self.hard_max is not None and txn.amount_cents > self.hard_max:
            return RuleVerdict(
                Decision.DENY,
                self.name,
                f"amount {txn.amount_cents}¢ exceeds hard max {self.hard_max}¢",
            )
        if (
            self.approval_threshold is not None
            and txn.amount_cents > self.approval_threshold
        ):
            return RuleVerdict(
                Decision.REQUIRE_APPROVAL,
                self.name,
                f"amount {txn.amount_cents}¢ exceeds approval "
                f"threshold {self.approval_threshold}¢",
            )
        return RuleVerdict(Decision.ALLOW, self.name)


class WindowedSpendLimit(Rule):
    """Limits cumulative spend over a rolling window (e.g. $500/day)."""

    def __init__(
        self,
        agent_id: str | None,
        window: timedelta,
        limit_cents: int,
        currency: str,
    ) -> None:
        self.name = (
            f"windowed_limit({agent_id or '*'},"
            f"{int(window.total_seconds())}s,{currency})"
        )
        self.agent_id = agent_id
        self.window = window
        self.limit_cents = limit_cents
        self.currency = currency

    def evaluate(
        self, txn: Transaction, tracker: SpendTracker
    ) -> RuleVerdict | None:
        if self.agent_id and txn.agent_id != self.agent_id:
            return None
        if txn.currency != self.currency:
            return None  # different currency, this rule doesn't apply
        already_spent = tracker.total(
            txn.agent_id, self.window, self.currency
        )
        # Note: we count the proposed transaction too, since the question
        # is "would this transaction push us over?"
        if already_spent + txn.amount_cents > self.limit_cents:
            return RuleVerdict(
                Decision.DENY,
                self.name,
                f"spend {already_spent + txn.amount_cents}¢ would exceed "
                f"limit {self.limit_cents}¢ over {self.window}",
            )
        return RuleVerdict(Decision.ALLOW, self.name)


class VendorAllowlist(Rule):
    """Only permit transactions to vendors in the allowlist."""

    def __init__(self, agent_id: str | None, vendors: list[str]) -> None:
        self.name = f"vendor_allowlist({agent_id or '*'})"
        self.agent_id = agent_id
        self.vendors = set(vendors)

    def evaluate(
        self, txn: Transaction, tracker: SpendTracker
    ) -> RuleVerdict | None:
        if self.agent_id and txn.agent_id != self.agent_id:
            return None
        if txn.vendor not in self.vendors:
            return RuleVerdict(
                Decision.DENY,
                self.name,
                f"vendor '{txn.vendor}' not in allowlist",
            )
        return RuleVerdict(Decision.ALLOW, self.name)


class VendorBlocklist(Rule):
    """Deny transactions to specific vendors."""

    def __init__(self, agent_id: str | None, vendors: list[str]) -> None:
        self.name = f"vendor_blocklist({agent_id or '*'})"
        self.agent_id = agent_id
        self.vendors = set(vendors)

    def evaluate(
        self, txn: Transaction, tracker: SpendTracker
    ) -> RuleVerdict | None:
        if self.agent_id and txn.agent_id != self.agent_id:
            return None
        if txn.vendor in self.vendors:
            return RuleVerdict(
                Decision.DENY,
                self.name,
                f"vendor '{txn.vendor}' is blocked",
            )
        return RuleVerdict(Decision.ALLOW, self.name)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

# A note on parsing windows: YAML users will want to write "1d", "24h",
# "30m" etc. We support a small set of suffixes.
_WINDOW_SUFFIXES = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(value: str) -> timedelta:
    if not value or len(value) < 2:
        raise PolicyConfigError(f"invalid window: {value!r}")
    suffix = value[-1]
    if suffix not in _WINDOW_SUFFIXES:
        raise PolicyConfigError(
            f"unknown window suffix {suffix!r} in {value!r}; "
            f"use one of {sorted(_WINDOW_SUFFIXES)}"
        )
    try:
        n = int(value[:-1])
    except ValueError as e:
        raise PolicyConfigError(f"invalid window number in {value!r}") from e
    if n <= 0:
        raise PolicyConfigError(f"window must be positive: {value!r}")
    return timedelta(seconds=n * _WINDOW_SUFFIXES[suffix])


def parse_amount_cents(value: Any) -> int:
    """Accept int (cents), float (dollars), or string like '$5.00' or '500c'.

    We default to cents for ints to avoid the classic float-money bug.
    Strings ending in 'c' are cents; strings starting with $ are dollars.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value * 100)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("c"):
            return int(s[:-1])
        if s.startswith("$"):
            return round(float(s[1:]) * 100)
        # bare string number - treat as dollars to be safe
        return round(float(s) * 100)
    raise PolicyConfigError(f"cannot parse amount: {value!r}")


def load_rules_from_dict(config: dict) -> list[Rule]:
    """Build Rule objects from a parsed YAML dict.

    Schema (see examples/policy.yaml for a full example):

        rules:
          - type: per_transaction_limit
            agent_id: ci-agent       # optional, omit for "all agents"
            hard_max: $100           # deny above this
            approval_threshold: $20  # require human approval above this
          - type: windowed_limit
            agent_id: ci-agent
            window: 1d
            limit: $500
            currency: USD
          - type: vendor_allowlist
            agent_id: ci-agent
            vendors: [anthropic, openai, sendgrid]
          - type: vendor_blocklist
            vendors: [shady-vendor]
    """
    if "rules" not in config:
        raise PolicyConfigError("policy config must have a 'rules' key")
    rules: list[Rule] = []
    for i, raw in enumerate(config["rules"]):
        if not isinstance(raw, dict) or "type" not in raw:
            raise PolicyConfigError(f"rule #{i}: must be a dict with 'type'")
        rtype = raw["type"]
        agent = raw.get("agent_id")  # None = all agents
        try:
            if rtype == "per_transaction_limit":
                rules.append(
                    PerTransactionLimit(
                        agent_id=agent,
                        hard_max_cents=(
                            parse_amount_cents(raw["hard_max"])
                            if "hard_max" in raw
                            else None
                        ),
                        approval_threshold_cents=(
                            parse_amount_cents(raw["approval_threshold"])
                            if "approval_threshold" in raw
                            else None
                        ),
                    )
                )
            elif rtype == "windowed_limit":
                rules.append(
                    WindowedSpendLimit(
                        agent_id=agent,
                        window=parse_window(raw["window"]),
                        limit_cents=parse_amount_cents(raw["limit"]),
                        currency=raw.get("currency", "USD"),
                    )
                )
            elif rtype == "vendor_allowlist":
                rules.append(VendorAllowlist(agent, list(raw["vendors"])))
            elif rtype == "vendor_blocklist":
                rules.append(VendorBlocklist(agent, list(raw["vendors"])))
            else:
                raise PolicyConfigError(f"unknown rule type: {rtype!r}")
        except KeyError as e:
            raise PolicyConfigError(
                f"rule #{i} ({rtype}): missing required field {e}"
            ) from e
    return rules
