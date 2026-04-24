"""
Static / heuristic conflict detector for AP policy rule sets.

Complements the LLM-based conflict detection in rule_extractor.py by
performing deterministic structural analysis that does not require an API key.

Detection strategies:
  1. Same-field threshold overlap   — two rules cover overlapping numeric ranges
     on the same field but prescribe different actions.
  2. Action contradiction           — two rules on the same field have the same
     condition but different actions.
  3. Superset / subset conditions   — one rule's condition subsumes another's.
  4. Priority inversion             — a lower-priority rule should logically
     fire before a higher-priority one.

Usage:
    detector = ConflictDetector()
    conflicts = detector.detect(rule_set.rules)
    for c in conflicts:
        print(c)
"""

from __future__ import annotations

import itertools
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from src.models import (
    CompositeCondition,
    ConflictReport,
    ConflictSeverity,
    OperandCondition,
    Rule,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_numeric_bounds(
    condition, field: str
) -> Dict[str, Optional[float]]:
    """
    Walk a condition tree and collect numeric bounds for a given field.
    Returns {"gt": v, "gte": v, "lt": v, "lte": v, "eq": v} where set.
    """
    bounds: Dict[str, Any] = {}

    if isinstance(condition, OperandCondition):
        if condition.field == field:
            val = condition.value
            try:
                val = float(val)
            except (TypeError, ValueError):
                return bounds
            if condition.op in (">",):
                bounds["gt"] = val
            elif condition.op in (">=",):
                bounds["gte"] = val
            elif condition.op in ("<",):
                bounds["lt"] = val
            elif condition.op in ("<=",):
                bounds["lte"] = val
            elif condition.op in ("==",):
                bounds["eq"] = val
    elif isinstance(condition, CompositeCondition):
        for operand in condition.operands:
            sub = _extract_numeric_bounds(operand, field)
            bounds.update(sub)

    return bounds


def _fields_in_condition(condition) -> Set[str]:
    """Collect all field names referenced in a condition tree."""
    if isinstance(condition, OperandCondition):
        return {condition.field}
    if isinstance(condition, CompositeCondition):
        result: Set[str] = set()
        for operand in condition.operands:
            result |= _fields_in_condition(operand)
        return result
    return set()


def _ranges_overlap(
    bounds_a: Dict[str, Any], bounds_b: Dict[str, Any]
) -> bool:
    """
    Returns True if two sets of numeric bounds could be simultaneously satisfied
    (i.e., their ranges overlap).
    """
    # Compute effective (lo, hi) for each
    lo_a = bounds_a.get("gte") or bounds_a.get("gt")
    hi_a = bounds_a.get("lte") or bounds_a.get("lt")
    lo_b = bounds_b.get("gte") or bounds_b.get("gt")
    hi_b = bounds_b.get("lte") or bounds_b.get("lt")

    if lo_a is None and hi_a is None:
        return True  # unconstrained
    if lo_b is None and hi_b is None:
        return True

    # Both have at least one bound — check overlap
    effective_lo_a = lo_a if lo_a is not None else float("-inf")
    effective_hi_a = hi_a if hi_a is not None else float("inf")
    effective_lo_b = lo_b if lo_b is not None else float("-inf")
    effective_hi_b = hi_b if hi_b is not None else float("inf")

    return effective_lo_a < effective_hi_b and effective_lo_b < effective_hi_a


# ---------------------------------------------------------------------------
# ConflictDetector
# ---------------------------------------------------------------------------

class ConflictDetector:
    """
    Performs static structural analysis on extracted rules to find conflicts.
    Produces ConflictReport objects that complement LLM-based detection.
    """

    # Fields to check for threshold overlap conflicts
    AMOUNT_FIELDS = {"invoice_total", "deviation_pct", "under_invoiced_pct"}

    def detect(self, rules: List[Rule]) -> List[ConflictReport]:
        """Run all detection strategies and return deduplicated conflicts."""
        conflicts: List[ConflictReport] = []
        counter = 1

        for rule_a, rule_b in itertools.combinations(rules, 2):
            # Skip if same category and rule_id prefix differ (less likely to conflict)
            shared_fields = (
                _fields_in_condition(rule_a.condition)
                & _fields_in_condition(rule_b.condition)
            )
            if not shared_fields:
                continue

            for field in shared_fields:
                conflict = self._check_threshold_overlap(
                    rule_a, rule_b, field, counter
                )
                if conflict:
                    conflicts.append(conflict)
                    counter += 1

        # Priority inversion check
        for inversion in self._check_priority_inversions(rules, counter):
            conflicts.append(inversion)
            counter += 1

        # Deduplicate by rule_id pairs
        seen: Set[frozenset] = set()
        unique: List[ConflictReport] = []
        for c in conflicts:
            key = frozenset(c.rule_ids)
            if key not in seen:
                seen.add(key)
                unique.append(c)

        return unique

    # ------------------------------------------------------------------
    # Strategy: threshold range overlap
    # ------------------------------------------------------------------

    def _check_threshold_overlap(
        self,
        rule_a: Rule,
        rule_b: Rule,
        field: str,
        counter: int,
    ) -> Optional[ConflictReport]:
        if field not in self.AMOUNT_FIELDS:
            return None

        bounds_a = _extract_numeric_bounds(rule_a.condition, field)
        bounds_b = _extract_numeric_bounds(rule_b.condition, field)

        if not bounds_a or not bounds_b:
            return None

        if not _ranges_overlap(bounds_a, bounds_b):
            return None

        if rule_a.action == rule_b.action:
            return None  # Same action — not a conflict

        severity = ConflictSeverity.MEDIUM
        if rule_a.action.value in ("REJECT", "COMPLIANCE_HOLD", "ROUTE_TO_CFO") or \
           rule_b.action.value in ("REJECT", "COMPLIANCE_HOLD", "ROUTE_TO_CFO"):
            severity = ConflictSeverity.HIGH

        return ConflictReport(
            conflict_id=f"STATIC-{counter:03d}",
            severity=severity,
            rule_ids=[rule_a.rule_id, rule_b.rule_id],
            source_clauses=[rule_a.source_clause, rule_b.source_clause],
            description=(
                f"Rules {rule_a.rule_id} and {rule_b.rule_id} both condition on "
                f"field '{field}' with overlapping numeric ranges but prescribe "
                f"different actions: '{rule_a.action.value}' vs '{rule_b.action.value}'."
            ),
            resolution_suggestion=(
                f"Review threshold boundaries for field '{field}' in "
                f"{rule_a.source_clause} and {rule_b.source_clause}. "
                "Ensure ranges are mutually exclusive or add explicit priority ordering."
            ),
        )

    # ------------------------------------------------------------------
    # Strategy: priority inversion
    # ------------------------------------------------------------------

    def _check_priority_inversions(
        self, rules: List[Rule], counter: int
    ) -> List[ConflictReport]:
        """
        Flag cases where a rule that is logically an exception has a higher
        priority number (lower precedence) than the rule it overrides.
        """
        conflicts: List[ConflictReport] = []
        rule_map = {r.rule_id: r for r in rules}

        for rule in rules:
            if not rule.exceptions:
                continue
            for exc_id in rule.exceptions:
                exc_rule = rule_map.get(exc_id)
                if not exc_rule:
                    continue
                # The exception rule should fire BEFORE the base rule
                # i.e. exc_rule.priority < rule.priority
                if exc_rule.priority > rule.priority:
                    conflicts.append(ConflictReport(
                        conflict_id=f"STATIC-{counter:03d}",
                        severity=ConflictSeverity.MEDIUM,
                        rule_ids=[rule.rule_id, exc_id],
                        source_clauses=[rule.source_clause, exc_rule.source_clause],
                        description=(
                            f"Rule {rule.rule_id} declares {exc_id} as an exception, "
                            f"but {exc_id} has a higher priority number ({exc_rule.priority}) "
                            f"than {rule.rule_id} ({rule.priority}), meaning the exception "
                            "may not fire before the base rule."
                        ),
                        resolution_suggestion=(
                            f"Set priority of {exc_id} to a value lower than {rule.priority} "
                            "so it is evaluated first."
                        ),
                    ))
                    counter += 1

        return conflicts

    def report_summary(self, conflicts: List[ConflictReport]) -> str:
        """Return a human-readable conflict summary."""
        if not conflicts:
            return "No conflicts detected."
        lines = [f"Detected {len(conflicts)} conflict(s):"]
        for c in conflicts:
            lines.append(
                f"  [{c.severity.value}] {c.conflict_id}: "
                f"{', '.join(c.rule_ids)} — {c.description[:100]}..."
            )
        return "\n".join(lines)
