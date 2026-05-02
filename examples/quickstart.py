"""Quickstart example: load a policy, evaluate transactions, see results.

Run with:
    pip install agentspend
    python examples/quickstart.py
"""

from agentspend import (
    Decision,
    PerTransactionLimit,
    PolicyEngine,
    Transaction,
    VendorAllowlist,
    WindowedSpendLimit,
)
from datetime import timedelta


def main():
    engine = PolicyEngine(
        rules=[
            PerTransactionLimit(
                agent_id=None,  # all agents
                hard_max_cents=50_000,           # $500 hard cap
                approval_threshold_cents=5_000,  # $50 needs approval
            ),
            WindowedSpendLimit(
                agent_id="ci-agent",
                window=timedelta(days=1),
                limit_cents=20_000,  # $200/day
                currency="USD",
            ),
            VendorAllowlist(
                agent_id="ci-agent",
                vendors=["anthropic", "openai", "aws"],
            ),
        ],
        default_decision=Decision.DENY,  # deny anything not explicitly allowed
    )

    # Case 1: small approved purchase — allowed
    txn1 = Transaction(
        agent_id="ci-agent",
        amount_cents=300,
        currency="USD",
        vendor="anthropic",
        workflow_id="build-#1234",
        human_owner="max@example.com",
    )
    result = engine.authorize(txn1)
    print(f"Case 1 ($3 to anthropic): {result.decision.value}")

    # Case 2: too much for one transaction — needs approval
    txn2 = Transaction(
        agent_id="ci-agent",
        amount_cents=10_000,
        currency="USD",
        vendor="anthropic",
    )
    result = engine.authorize(txn2)
    print(f"Case 2 ($100 to anthropic): {result.decision.value}")
    if result.approval_request_id:
        print(f"  -> approval request {result.approval_request_id}")

    # Case 3: vendor not on allowlist — denied
    txn3 = Transaction(
        agent_id="ci-agent",
        amount_cents=100,
        currency="USD",
        vendor="random-vendor",
    )
    result = engine.authorize(txn3)
    print(f"Case 3 ($1 to random-vendor): {result.decision.value}")
    print(f"  -> reasons: {result.reasons}")


if __name__ == "__main__":
    main()
