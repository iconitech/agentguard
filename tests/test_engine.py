"""Tests for the agentguard policy engine.

These cover the critical paths and the gnarly edge cases (decision
precedence, hooks failing closed, spend not being recorded if the
guarded block raises, etc.).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentguard import (
    ApprovalRequired,
    Decision,
    InMemoryApprovalSink,
    InMemorySink,
    InMemorySpendTracker,
    PerTransactionLimit,
    PolicyDenied,
    PolicyEngine,
    Transaction,
    VendorAllowlist,
    VendorBlocklist,
    WindowedSpendLimit,
)
from agentguard.rules import (
    RuleVerdict,
    load_rules_from_dict,
    parse_amount_cents,
    parse_window,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_txn(**overrides) -> Transaction:
    defaults = dict(
        agent_id="test-agent",
        amount_cents=500,
        currency="USD",
        vendor="anthropic",
    )
    defaults.update(overrides)
    return Transaction(**defaults)


# ---------------------------------------------------------------------------
# Rule tests
# ---------------------------------------------------------------------------

class TestPerTransactionLimit:
    def test_allows_under_threshold(self):
        rule = PerTransactionLimit(None, hard_max_cents=10_000,
                                    approval_threshold_cents=5_000)
        v = rule.evaluate(make_txn(amount_cents=100), InMemorySpendTracker())
        assert v.decision == Decision.ALLOW

    def test_requires_approval_above_threshold(self):
        rule = PerTransactionLimit(None, hard_max_cents=10_000,
                                    approval_threshold_cents=5_000)
        v = rule.evaluate(make_txn(amount_cents=6_000), InMemorySpendTracker())
        assert v.decision == Decision.REQUIRE_APPROVAL

    def test_denies_above_hard_max(self):
        rule = PerTransactionLimit(None, hard_max_cents=10_000,
                                    approval_threshold_cents=5_000)
        v = rule.evaluate(make_txn(amount_cents=20_000), InMemorySpendTracker())
        assert v.decision == Decision.DENY

    def test_agent_scoping_skips_other_agents(self):
        rule = PerTransactionLimit("alice", hard_max_cents=100,
                                    approval_threshold_cents=None)
        v = rule.evaluate(make_txn(agent_id="bob", amount_cents=99_999),
                          InMemorySpendTracker())
        assert v is None  # rule didn't apply, so doesn't veto


class TestWindowedSpendLimit:
    def test_allows_first_transaction(self):
        rule = WindowedSpendLimit(None, timedelta(days=1), 10_000, "USD")
        v = rule.evaluate(make_txn(amount_cents=500), InMemorySpendTracker())
        assert v.decision == Decision.ALLOW

    def test_denies_when_cumulative_exceeds_limit(self):
        tracker = InMemorySpendTracker()
        # Pre-record $90 of spend
        for _ in range(9):
            tracker.record(make_txn(amount_cents=1_000))
        rule = WindowedSpendLimit(None, timedelta(days=1), 10_000, "USD")
        # New $20 txn would push us to $110, over the $100 limit
        v = rule.evaluate(make_txn(amount_cents=2_000), tracker)
        assert v.decision == Decision.DENY

    def test_different_currency_is_ignored(self):
        tracker = InMemorySpendTracker()
        rule = WindowedSpendLimit(None, timedelta(days=1), 10_000, "USD")
        v = rule.evaluate(make_txn(currency="EUR", amount_cents=99_999), tracker)
        assert v is None  # rule doesn't apply to EUR


class TestVendorAllowlist:
    def test_allows_listed_vendor(self):
        rule = VendorAllowlist(None, ["anthropic", "openai"])
        v = rule.evaluate(make_txn(vendor="anthropic"), InMemorySpendTracker())
        assert v.decision == Decision.ALLOW

    def test_denies_unlisted_vendor(self):
        rule = VendorAllowlist(None, ["anthropic"])
        v = rule.evaluate(make_txn(vendor="random-vendor"), InMemorySpendTracker())
        assert v.decision == Decision.DENY


# ---------------------------------------------------------------------------
# Engine tests — decision precedence
# ---------------------------------------------------------------------------

class TestDecisionPrecedence:
    """The most important property: DENY > REQUIRE_APPROVAL > ALLOW."""

    def test_deny_beats_allow(self):
        engine = PolicyEngine(rules=[
            VendorAllowlist(None, ["anthropic"]),  # allows
            VendorBlocklist(None, ["anthropic"]),  # denies
        ])
        result = engine.evaluate(make_txn(vendor="anthropic"))
        assert result.decision == Decision.DENY

    def test_deny_beats_approval(self):
        engine = PolicyEngine(rules=[
            PerTransactionLimit(None, hard_max_cents=None,
                                 approval_threshold_cents=100),  # approval
            VendorBlocklist(None, ["anthropic"]),  # deny
        ])
        result = engine.evaluate(make_txn(amount_cents=500, vendor="anthropic"))
        assert result.decision == Decision.DENY

    def test_approval_beats_allow(self):
        engine = PolicyEngine(rules=[
            VendorAllowlist(None, ["anthropic"]),
            PerTransactionLimit(None, hard_max_cents=None,
                                 approval_threshold_cents=100),
        ])
        result = engine.evaluate(make_txn(amount_cents=500))
        assert result.decision == Decision.REQUIRE_APPROVAL

    def test_no_rules_uses_default(self):
        engine_allow = PolicyEngine(default_decision=Decision.ALLOW)
        assert engine_allow.evaluate(make_txn()).decision == Decision.ALLOW

        engine_deny = PolicyEngine(default_decision=Decision.DENY)
        assert engine_deny.evaluate(make_txn()).decision == Decision.DENY


# ---------------------------------------------------------------------------
# Engine tests — guard, spend tracking, side effects
# ---------------------------------------------------------------------------

class TestGuard:
    def test_guard_yields_when_allowed(self):
        engine = PolicyEngine(rules=[VendorAllowlist(None, ["anthropic"])])
        ran = []
        with engine.guard(make_txn(vendor="anthropic")):
            ran.append(True)
        assert ran == [True]

    def test_guard_raises_on_deny(self):
        engine = PolicyEngine(rules=[VendorBlocklist(None, ["anthropic"])])
        with pytest.raises(PolicyDenied):
            with engine.guard(make_txn(vendor="anthropic")):
                pytest.fail("should not reach here")

    def test_guard_raises_on_approval_required(self):
        engine = PolicyEngine(rules=[
            PerTransactionLimit(None, hard_max_cents=None,
                                 approval_threshold_cents=100)
        ])
        with pytest.raises(ApprovalRequired) as exc_info:
            with engine.guard(make_txn(amount_cents=500)):
                pytest.fail("should not reach here")
        # Approval request should have been created
        assert exc_info.value.result.approval_request_id is not None

    def test_guard_records_spend_on_success(self):
        tracker = InMemorySpendTracker()
        engine = PolicyEngine(tracker=tracker)
        with engine.guard(make_txn(amount_cents=500)):
            pass
        assert tracker.total("test-agent", timedelta(days=1), "USD") == 500

    def test_guard_does_not_record_spend_on_exception(self):
        """Critical: if the user's payment call raises, we must not
        count the money as spent (it wasn't)."""
        tracker = InMemorySpendTracker()
        engine = PolicyEngine(tracker=tracker)
        with pytest.raises(RuntimeError):
            with engine.guard(make_txn(amount_cents=500)):
                raise RuntimeError("stripe failed")
        assert tracker.total("test-agent", timedelta(days=1), "USD") == 0


# ---------------------------------------------------------------------------
# Engine tests — fail-closed on broken rules/hooks
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_broken_rule_denies(self):
        class ExplodingRule:
            name = "exploding"
            def evaluate(self, txn, tracker):
                raise ValueError("boom")
        engine = PolicyEngine(rules=[ExplodingRule()])
        result = engine.evaluate(make_txn())
        assert result.decision == Decision.DENY
        assert any("exploding" in r for r in result.matched_rules)

    def test_broken_hook_denies(self):
        def bad_hook(txn, tracker):
            raise RuntimeError("oops")
        engine = PolicyEngine(hooks=[bad_hook])
        result = engine.evaluate(make_txn())
        assert result.decision == Decision.DENY


# ---------------------------------------------------------------------------
# Hook tests
# ---------------------------------------------------------------------------

class TestHooks:
    def test_hook_can_deny(self):
        def deny_weekends(txn, tracker):
            # Pretend it's always a weekend
            return RuleVerdict(Decision.DENY, "no_weekends", "weekend")
        engine = PolicyEngine(hooks=[deny_weekends])
        assert engine.evaluate(make_txn()).decision == Decision.DENY

    def test_hook_returning_none_is_skipped(self):
        def passthrough(txn, tracker):
            return None
        engine = PolicyEngine(hooks=[passthrough])
        # No rules + None hook → default ALLOW
        assert engine.evaluate(make_txn()).decision == Decision.ALLOW


# ---------------------------------------------------------------------------
# Audit tests
# ---------------------------------------------------------------------------

class TestAudit:
    def test_audit_log_captures_every_decision(self):
        sink = InMemorySink()
        engine = PolicyEngine(audit=sink)
        engine.evaluate(make_txn())
        engine.evaluate(make_txn())
        assert len(sink.events) == 2
        assert sink.events[0]["decision"] == "allow"

    def test_audit_log_includes_reasons(self):
        sink = InMemorySink()
        engine = PolicyEngine(
            audit=sink,
            rules=[VendorBlocklist(None, ["anthropic"])],
        )
        engine.evaluate(make_txn(vendor="anthropic"))
        assert "anthropic" in sink.events[0]["reasons"][0]


# ---------------------------------------------------------------------------
# Approval tests
# ---------------------------------------------------------------------------

class TestApprovals:
    def test_approval_request_created_on_evaluation(self):
        approvals = InMemoryApprovalSink()
        engine = PolicyEngine(
            approvals=approvals,
            rules=[PerTransactionLimit(None, hard_max_cents=None,
                                        approval_threshold_cents=100)],
        )
        # authorize() requests approval; evaluate() doesn't
        result = engine.authorize(make_txn(amount_cents=500))
        assert result.decision == Decision.REQUIRE_APPROVAL
        assert result.approval_request_id is not None
        assert len(approvals.pending()) == 1


# ---------------------------------------------------------------------------
# Parser tests (the kind of bugs that bite at 2am)
# ---------------------------------------------------------------------------

class TestParsers:
    def test_window_parsing(self):
        assert parse_window("1d") == timedelta(days=1)
        assert parse_window("24h") == timedelta(hours=24)
        assert parse_window("30m") == timedelta(minutes=30)
        assert parse_window("60s") == timedelta(seconds=60)

    def test_window_parsing_rejects_garbage(self):
        with pytest.raises(Exception):
            parse_window("forever")
        with pytest.raises(Exception):
            parse_window("0d")

    def test_amount_parsing(self):
        assert parse_amount_cents(500) == 500           # int -> cents
        assert parse_amount_cents(5.0) == 500            # float dollars
        assert parse_amount_cents("$5") == 500           # $ string
        assert parse_amount_cents("$5.99") == 599
        assert parse_amount_cents("500c") == 500         # explicit cents
        assert parse_amount_cents("5") == 500            # bare string = dollars


class TestYAMLLoading:
    def test_loads_simple_policy(self):
        config = {
            "rules": [
                {
                    "type": "per_transaction_limit",
                    "agent_id": "ci",
                    "hard_max": "$100",
                    "approval_threshold": "$20",
                },
                {
                    "type": "vendor_allowlist",
                    "vendors": ["anthropic", "openai"],
                },
            ]
        }
        rules = load_rules_from_dict(config)
        assert len(rules) == 2
