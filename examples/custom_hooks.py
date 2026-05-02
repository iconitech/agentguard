"""Custom hook examples.

Hooks let you add Python logic that the YAML rules can't express:
time-of-day checks, anomaly detection, lookups in external systems,
ML scoring, etc. A hook is just a callable that takes (txn, tracker)
and returns a RuleVerdict or None.
"""

from datetime import datetime, timedelta, timezone

from agentspend import (
    Decision,
    PolicyEngine,
    Transaction,
    VendorAllowlist,
)
from agentspend.rules import RuleVerdict


def business_hours_only(txn: Transaction, tracker) -> RuleVerdict | None:
    """Require approval for any transaction outside 8am-8pm UTC."""
    hour = txn.timestamp.astimezone(timezone.utc).hour
    if hour < 8 or hour >= 20:
        return RuleVerdict(
            Decision.REQUIRE_APPROVAL,
            "business_hours_only",
            f"transaction at {hour:02d}:00 UTC is outside business hours",
        )
    return None  # within business hours, defer to other rules


def velocity_anomaly(txn: Transaction, tracker) -> RuleVerdict | None:
    """Toy anomaly detector: flag if this txn alone is more than 3x
    the agent's average transaction over the past 7 days.

    A real implementation would use a proper baseline + std dev, but
    this shows the shape: hook reads from tracker, applies its own
    logic, returns a verdict.
    """
    week_total = tracker.total(txn.agent_id, timedelta(days=7), txn.currency)
    # We don't have a count() on the tracker — in a real impl you'd
    # add that. For demo, we'll just check absolute thresholds.
    if week_total == 0:
        return None  # no baseline yet
    # If this single txn is more than half of all past-week spend, flag it
    if txn.amount_cents > week_total / 2:
        return RuleVerdict(
            Decision.REQUIRE_APPROVAL,
            "velocity_anomaly",
            f"txn ${txn.amount_dollars:.2f} unusually large vs "
            f"weekly baseline ${week_total/100:.2f}",
        )
    return None


def main():
    engine = PolicyEngine(
        rules=[VendorAllowlist(None, ["anthropic", "openai"])],
        hooks=[business_hours_only, velocity_anomaly],
    )

    txn = Transaction(
        agent_id="ci-agent",
        amount_cents=500,
        currency="USD",
        vendor="anthropic",
    )
    result = engine.evaluate(txn)
    print(f"Decision: {result.decision.value}")
    print(f"Matched rules + hooks: {result.matched_rules}")
    print(f"Reasons: {result.reasons}")


if __name__ == "__main__":
    main()
