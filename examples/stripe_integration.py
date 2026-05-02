"""Stripe integration example.

Shows the recommended `engine.guard(txn)` pattern wrapping a real
Stripe payment call. This is the integration pattern users should
copy-paste.

Note: this example uses Stripe in test mode and assumes you have
STRIPE_API_KEY set. The agentspend library does NOT depend on the
stripe package — it's purely a policy engine. You bring your own
Stripe (or your own anything).
"""

from __future__ import annotations

import os

from agentspend import (
    ApprovalRequired,
    JSONLFileSink,
    PerTransactionLimit,
    PolicyDenied,
    PolicyEngine,
    Transaction,
    VendorAllowlist,
)


def make_engine() -> PolicyEngine:
    return PolicyEngine(
        rules=[
            PerTransactionLimit(
                agent_id=None,
                hard_max_cents=10_000,           # $100 absolute max
                approval_threshold_cents=2_000,  # $20 needs approval
            ),
            VendorAllowlist(
                agent_id=None,
                vendors=["anthropic", "openai", "aws", "github"],
            ),
        ],
        # Audit trail to disk — auditors will love you for this.
        audit=JSONLFileSink("./agent-spend-audit.jsonl"),
    )


def buy_with_stripe(engine: PolicyEngine, txn: Transaction) -> str | None:
    """Attempt a Stripe charge through the policy engine.

    Returns the Stripe payment intent ID on success, or None if the
    transaction was blocked / pending approval.
    """
    try:
        with engine.guard(txn):
            # In a real integration you'd call something like:
            #
            #   import stripe
            #   stripe.api_key = os.environ["STRIPE_API_KEY"]
            #   intent = stripe.PaymentIntent.create(
            #       amount=txn.amount_cents,
            #       currency=txn.currency.lower(),
            #       payment_method_types=["card"],
            #       # ... use Shared Payment Token here for agent flows
            #   )
            #   return intent.id
            #
            # We mock it here so the example runs without Stripe creds.
            print(f"  [stripe] charging ${txn.amount_dollars:.2f} to {txn.vendor}")
            return f"pi_mock_{txn.transaction_id[:8]}"

    except PolicyDenied as e:
        print(f"  [denied] {e}")
        return None
    except ApprovalRequired as e:
        print(f"  [pending approval] request_id={e.result.approval_request_id}")
        # In production: notify a human via Slack/email/PagerDuty here.
        # When they approve, you'd retry the transaction (or queue and
        # resume from the approval webhook).
        return None


def main():
    engine = make_engine()

    transactions = [
        # Normal small spend — should succeed
        Transaction(
            agent_id="ci-agent",
            amount_cents=500,
            currency="USD",
            vendor="anthropic",
            workflow_id="ci-build-1",
            human_owner="max@example.com",
        ),
        # Larger spend — will need approval
        Transaction(
            agent_id="ci-agent",
            amount_cents=3_000,
            currency="USD",
            vendor="aws",
            workflow_id="ci-build-1",
            human_owner="max@example.com",
        ),
        # Disallowed vendor — will be denied
        Transaction(
            agent_id="ci-agent",
            amount_cents=100,
            currency="USD",
            vendor="random-bitcoin-place",
        ),
    ]

    for txn in transactions:
        print(f"\nAttempting: {txn.agent_id} -> ${txn.amount_dollars:.2f} -> {txn.vendor}")
        intent_id = buy_with_stripe(engine, txn)
        if intent_id:
            print(f"  [ok] payment_intent={intent_id}")


if __name__ == "__main__":
    main()
