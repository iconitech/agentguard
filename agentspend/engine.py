"""The PolicyEngine.

This is the public API users interact with. They construct an engine
with their rules, optional hooks, and audit/approval sinks, then call
either:

  - engine.evaluate(txn) -> PolicyResult  (just decides, doesn't track)
  - engine.authorize(txn) -> PolicyResult (decides AND records spend on allow)
  - engine.guard(txn) -> contextmanager   (raises if denied; records on success)

Why the distinction? `evaluate` is for dry-runs and tests. `authorize`
is for "I am about to call Stripe, please tell me if I can". `guard`
is the cleanest integration point — wrap your payment call in `with
engine.guard(txn):` and policy enforcement is automatic.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator

from .approvals import ApprovalSink, InMemoryApprovalSink
from .audit import AuditSink, InMemorySink
from .rules import Rule, RuleVerdict
from .tracking import InMemorySpendTracker, SpendTracker
from .types import (
    ApprovalRequired,
    Decision,
    PolicyDenied,
    PolicyResult,
    Transaction,
)

logger = logging.getLogger("agentspend")


# Hooks let users add arbitrary Python logic that the YAML rules can't
# express (anomaly detection, ML scoring, time-of-day checks, lookups in
# external systems, etc.). A hook returns a RuleVerdict or None.
Hook = Callable[[Transaction, SpendTracker], RuleVerdict | None]


class PolicyEngine:
    """Evaluates transactions against a list of rules and hooks.

    Decision precedence:
      1. Any DENY wins (deny is final)
      2. Otherwise any REQUIRE_APPROVAL wins
      3. Otherwise ALLOW

    This is intentionally strict. A single rule can veto a transaction
    even if 99 others allow it. That's what users expect from policy
    engines (cf. how OPA/Gatekeeper work, also AWS IAM's explicit deny).
    """

    def __init__(
        self,
        rules: list[Rule] | None = None,
        hooks: list[Hook] | None = None,
        tracker: SpendTracker | None = None,
        audit: AuditSink | None = None,
        approvals: ApprovalSink | None = None,
        default_decision: Decision = Decision.ALLOW,
    ) -> None:
        self.rules: list[Rule] = list(rules or [])
        self.hooks: list[Hook] = list(hooks or [])
        self.tracker: SpendTracker = tracker or InMemorySpendTracker()
        self.audit: AuditSink = audit or InMemorySink()
        self.approvals: ApprovalSink = approvals or InMemoryApprovalSink()
        # If you have NO rules and NO hooks, what happens? Configurable.
        # We default to ALLOW so an empty engine is a no-op (useful for
        # gradual rollout). Production users should set DENY.
        self.default_decision = default_decision

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, txn: Transaction) -> PolicyResult:
        """Evaluate without side effects (no spend tracking, no approval req)."""
        return self._evaluate(txn, request_approval=False, log=True)

    def authorize(self, txn: Transaction) -> PolicyResult:
        """Evaluate, record spend if allowed, request approval if needed."""
        result = self._evaluate(txn, request_approval=True, log=True)
        if result.decision == Decision.ALLOW:
            self.tracker.record(txn)
        return result

    @contextmanager
    def guard(self, txn: Transaction) -> Iterator[PolicyResult]:
        """Context manager that authorizes, then yields, then commits.

        Usage:
            with engine.guard(txn) as result:
                stripe_client.charge(...)   # only runs if allowed
            # spend recorded only if the `with` block completed without exception
        """
        result = self._evaluate(txn, request_approval=True, log=True)
        if result.decision == Decision.DENY:
            raise PolicyDenied(result)
        if result.decision == Decision.REQUIRE_APPROVAL:
            raise ApprovalRequired(result)
        # Allowed. Yield to the caller. Only record spend if their
        # block succeeds — if Stripe raises, we shouldn't count it.
        try:
            yield result
        except Exception:
            logger.warning(
                "guarded transaction %s raised; not recording spend",
                txn.transaction_id,
            )
            raise
        else:
            self.tracker.record(txn)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate(
        self, txn: Transaction, request_approval: bool, log: bool
    ) -> PolicyResult:
        verdicts: list[RuleVerdict] = []

        for rule in self.rules:
            try:
                v = rule.evaluate(txn, self.tracker)
            except Exception as e:
                # A broken rule should not silently allow things through.
                # Convert to a DENY with a clear reason. This is a
                # deliberate fail-closed posture.
                logger.exception(
                    "rule %s raised; failing closed", rule.name
                )
                v = RuleVerdict(
                    Decision.DENY,
                    rule.name,
                    f"rule raised exception: {e!r}",
                )
            if v is not None:
                verdicts.append(v)

        for i, hook in enumerate(self.hooks):
            hook_name = getattr(hook, "__name__", f"hook_{i}")
            try:
                v = hook(txn, self.tracker)
            except Exception as e:
                logger.exception(
                    "hook %s raised; failing closed", hook_name
                )
                v = RuleVerdict(
                    Decision.DENY,
                    f"hook:{hook_name}",
                    f"hook raised exception: {e!r}",
                )
            if v is not None:
                verdicts.append(v)

        decision = self._combine(verdicts)
        result = PolicyResult(
            decision=decision,
            transaction=txn,
            matched_rules=[v.rule_name for v in verdicts],
            reasons=[v.reason for v in verdicts if v.reason],
        )

        if (
            decision == Decision.REQUIRE_APPROVAL
            and request_approval
        ):
            reason = "; ".join(result.reasons) or "policy required approval"
            result.approval_request_id = self.approvals.request(txn, reason)

        if log:
            try:
                self.audit.write(result)
            except Exception:
                # Audit failures should not block decisions, but we
                # absolutely want to know about them.
                logger.exception("audit sink failed for txn %s", txn.transaction_id)

        return result

    def _combine(self, verdicts: list[RuleVerdict]) -> Decision:
        if not verdicts:
            return self.default_decision
        decisions = {v.decision for v in verdicts}
        if Decision.DENY in decisions:
            return Decision.DENY
        if Decision.REQUIRE_APPROVAL in decisions:
            return Decision.REQUIRE_APPROVAL
        return Decision.ALLOW
