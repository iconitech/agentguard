"""Loading policies from YAML files.

Kept in a separate module so the core has no PyYAML dependency for
users who'd rather construct rules in Python directly.
"""

from __future__ import annotations

from pathlib import Path

from .engine import PolicyEngine
from .rules import load_rules_from_dict
from .types import Decision, PolicyConfigError


def load_engine_from_yaml(
    path: str | Path,
    default_decision: Decision = Decision.ALLOW,
) -> PolicyEngine:
    """Build a PolicyEngine from a YAML policy file.

    Hooks cannot be loaded from YAML (they're Python callables) — add
    those after construction with `engine.hooks.append(my_hook)`.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        raise ImportError(
            "PyYAML is required to load policies from YAML. "
            "Install with: pip install agentspend[yaml]"
        ) from e

    text = Path(path).read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    if not isinstance(config, dict):
        raise PolicyConfigError(
            f"policy file {path} must contain a YAML mapping at the top level"
        )
    rules = load_rules_from_dict(config)
    return PolicyEngine(rules=rules, default_decision=default_decision)
