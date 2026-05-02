"""agentguard — policy engine for autonomous AI agent spending.

Quickstart:

    from agentguard import PolicyEngine, Transaction
    from agentguard.rules import PerTransactionLimit, VendorAllowlist

    engine = PolicyEngine(rules=[
        PerTransactionLimit(agent_id=None, hard_max_cents=10_000,
                            approval_threshold_cents=2_000),
        VendorAllowlist(agent_id=None, vendors=["anthropic", "openai"]),
    ])

    txn = Transaction(
        agent_id="ci-bot",
        amount_cents=500,
        currency="USD",
        vendor="anthropic",
    )

    with engine.guard(txn):
        # your real payment call here
        pass

Or load from YAML:

    from agentguard import load_engine_from_yaml
    engine = load_engine_from_yaml("policy.yaml")
"""

from .approvals import (
    ApprovalRequest,
    ApprovalSink,
    InMemoryApprovalSink,
)
from .audit import AuditSink, InMemorySink, JSONLFileSink
from .engine import Hook, PolicyEngine
from .loader import load_engine_from_yaml
from .rules import (
    PerTransactionLimit,
    Rule,
    RuleVerdict,
    VendorAllowlist,
    VendorBlocklist,
    WindowedSpendLimit,
    load_rules_from_dict,
)
from .tracking import InMemorySpendTracker, SpendTracker
from .types import (
    ApprovalRequired,
    Decision,
    PolicyConfigError,
    PolicyDenied,
    PolicyError,
    PolicyResult,
    Transaction,
)

__version__ = "0.1.0"

__all__ = [
    "ApprovalRequest",
    "ApprovalRequired",
    "ApprovalSink",
    "AuditSink",
    "Decision",
    "Hook",
    "InMemoryApprovalSink",
    "InMemorySink",
    "InMemorySpendTracker",
    "JSONLFileSink",
    "PerTransactionLimit",
    "PolicyConfigError",
    "PolicyDenied",
    "PolicyEngine",
    "PolicyError",
    "PolicyResult",
    "Rule",
    "RuleVerdict",
    "SpendTracker",
    "Transaction",
    "VendorAllowlist",
    "VendorBlocklist",
    "WindowedSpendLimit",
    "load_engine_from_yaml",
    "load_rules_from_dict",
]
