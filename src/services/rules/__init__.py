"""Deterministic rule engine.

Rules inspect the extracted metadata, visual analysis and datasource comparison
results and emit :class:`~src.domain.models.ValidationFinding` objects. This is
pure Python validation — the LLM later *reasons over* these findings but never
produces them.

The engine (:class:`RuleEngine`) runs a registry of :class:`ValidationRule`
objects; new checks are added by implementing a rule and registering it, with no
changes to callers (Open/Closed).
"""

from src.services.rules.base import RuleInput, ValidationRule
from src.services.rules.engine import RuleEngine, default_rules

__all__ = ["RuleInput", "ValidationRule", "RuleEngine", "default_rules"]
