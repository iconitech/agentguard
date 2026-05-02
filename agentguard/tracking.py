"""Spend tracking for time-windowed limits.

The policy engine needs to know "how much has agent X spent in the
last 24h?" to enforce daily limits. We abstract this behind a Protocol
so users can back it with Redis, Postgres, etc. for production. v0.1
ships an in-memory implementation that works for single-process use.

Important caveat documented in the README: in-memory tracking is NOT
safe across multiple processes or pods. For distributed deployments
you need a shared backend.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .types import Transaction


class SpendTracker(Protocol):
    """Tracks accumulated spend per agent over rolling time windows."""

    def record(self, transaction: Transaction) -> None: ...

    def total(
        self, agent_id: str, window: timedelta, currency: str
    ) -> int:
        """Sum of spend in cents for this agent in the last `window`.

        Currency-scoped because mixing USD and EUR cents is wrong.
        """
        ...


class InMemorySpendTracker:
    """Single-process spend tracker.

    Stores (timestamp, amount, currency) tuples per agent in a deque.
    On query, prunes expired entries lazily. Fine for v0.1 / single
    process. Document loudly that this is not production-safe across
    multiple workers.
    """

    def __init__(self, max_window: timedelta = timedelta(days=30)) -> None:
        # Cap retention to bound memory. Anything older than max_window
        # cannot affect any policy, so we drop it.
        self._max_window = max_window
        self._spend: dict[str, deque[tuple[datetime, int, str]]] = (
            defaultdict(deque)
        )
        self._lock = threading.Lock()

    def record(self, transaction: Transaction) -> None:
        with self._lock:
            self._spend[transaction.agent_id].append(
                (
                    transaction.timestamp,
                    transaction.amount_cents,
                    transaction.currency,
                )
            )
            self._prune(transaction.agent_id)

    def total(
        self, agent_id: str, window: timedelta, currency: str
    ) -> int:
        cutoff = datetime.now(timezone.utc) - window
        with self._lock:
            self._prune(agent_id)
            return sum(
                amount
                for ts, amount, curr in self._spend[agent_id]
                if ts >= cutoff and curr == currency
            )

    def _prune(self, agent_id: str) -> None:
        """Drop entries older than max_window. Caller must hold the lock."""
        cutoff = datetime.now(timezone.utc) - self._max_window
        dq = self._spend[agent_id]
        while dq and dq[0][0] < cutoff:
            dq.popleft()
