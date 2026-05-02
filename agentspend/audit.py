"""Audit logging.

We define a simple Protocol so users can plug in their own sink (Datadog,
Splunk, Postgres, etc.) without us depending on those libraries. Two
reference implementations are included: in-memory (for tests) and JSONL
file (for actual use).

Design note: every policy decision is logged, not just denials. Auditors
will ask "show me everything the agent could have done" — you need allows
in the log too.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .types import PolicyResult


class AuditSink(Protocol):
    """Anything that can persist policy decisions."""

    def write(self, result: PolicyResult) -> None: ...


def _serialize(result: PolicyResult) -> dict:
    """Convert a PolicyResult to a JSON-safe dict.

    We do this manually rather than asdict() on the whole thing because
    Transaction has a datetime and an Enum that don't serialize cleanly.
    """
    txn = asdict(result.transaction)
    txn["timestamp"] = result.transaction.timestamp.isoformat()
    return {
        "decision": result.decision.value,
        "matched_rules": result.matched_rules,
        "reasons": result.reasons,
        "approval_request_id": result.approval_request_id,
        "transaction": txn,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }


class InMemorySink:
    """Stores audit events in a list. For tests and demos only."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def write(self, result: PolicyResult) -> None:
        with self._lock:
            self.events.append(_serialize(result))


class JSONLFileSink:
    """Appends one JSON object per line to a file.

    Append-only and flushed every write so logs survive crashes. Not
    cryptographically signed in v0.1 — that's a v0.2 feature for the
    SOC2 evidence story.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, result: PolicyResult) -> None:
        line = json.dumps(_serialize(result), separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
