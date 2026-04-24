"""
Rule Execution Engine for AP Invoice Validation.

Given a RuleSet and an Invoice, evaluates every rule in priority order and
returns a detailed EvaluationReport showing which rules fired and the final
disposition of the invoice.

Features:
  - Handles OperandCondition and CompositeCondition recursively
  - Supports expression strings for computed comparisons (safe eval)
  - Pre-computes derived invoice fields (deviation_pct, line-item aggregates, etc.)
  - Short-circuits on hard REJECTs and HOLDs
  - Collects notifications to be sent

Usage:
    engine = RuleEngine()
    engine.load_rules("output/extracted_rules.json")
    report = engine.evaluate(invoice)
    print(report.final_status)
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.models import (
    CompositeCondition,
    EvaluationReport,
    Invoice,
    Notification,
    OperandCondition,
    Rule,
    RuleResult,
    RuleSet,
)

logger = logging.getLogger(__name__)

# Actions that stop further rule evaluation for the invoice
TERMINAL_ACTIONS = {"REJECT", "COMPLIANCE_HOLD"}


# ---------------------------------------------------------------------------
# Safe expression evaluator
# ---------------------------------------------------------------------------

class _SafeEval:
    """
    Evaluate simple arithmetic expressions against an invoice context.
    Only allows: numbers, +, -, *, /, (, ), field references.
    No builtins, no imports, no arbitrary code execution.
    """

    _ALLOWED = re.compile(r"^[\d\s\.\+\-\*\/\(\)a-z_]+$")

    @classmethod
    def evaluate(cls, expr: str, ctx: Dict[str, Any]) -> Any:
        if not isinstance(expr, str):
            return expr

        # Strip quotes → treat as string literal
        if (expr.startswith('"') and expr.endswith('"')) or \
           (expr.startswith("'") and expr.endswith("'")):
            return expr[1:-1]

        # Pure numeric literal
        try:
            return float(expr)
        except ValueError:
            pass

        # Boolean literals
        if expr.lower() == "true":
            return True
        if expr.lower() == "false":
            return False
        if expr.lower() == "null":
            return None

        # Field reference (simple name) — only if the name exists in ctx;
        # otherwise treat as a string literal (e.g. "goods", "intra_state")
        if re.fullmatch(r"[a-z_][a-z0-9_]*", expr):
            if expr in ctx:
                return ctx[expr]
            return expr  # literal string value

        # Arithmetic expression: replace field names with ctx values
        if not cls._ALLOWED.fullmatch(expr.lower()):
            logger.warning("Unsafe expression blocked: %r", expr)
            return None

        resolved = expr
        # Replace field names longest-first to avoid partial substitutions
        for key in sorted(ctx.keys(), key=len, reverse=True):
            val = ctx.get(key)
            if val is None:
                val = 0
            resolved = re.sub(rf"\b{re.escape(key)}\b", str(val), resolved)

        try:
            return eval(resolved, {"__builtins__": {}}, {})  # noqa: S307
        except Exception as exc:
            logger.warning("Expression eval failed %r → %r: %s", expr, resolved, exc)
            return None


# ---------------------------------------------------------------------------
# Invoice context builder
# ---------------------------------------------------------------------------

def build_context(invoice: Invoice, processing_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Flatten an Invoice object into a dict and add derived/computed fields
    used by rule conditions.
    """
    proc_date = processing_date or date.today().isoformat()
    ctx = invoice.model_dump()

    # --- Derived: deviation percentages ---
    if invoice.invoice_total is not None and invoice.po_amount and invoice.po_amount != 0:
        deviation_pct = (invoice.invoice_total - invoice.po_amount) / invoice.po_amount * 100
        under_invoiced_pct = (invoice.po_amount - invoice.invoice_total) / invoice.po_amount * 100
    else:
        deviation_pct = 0.0
        under_invoiced_pct = 0.0

    ctx["deviation_pct"] = deviation_pct
    ctx["under_invoiced_pct"] = under_invoiced_pct
    ctx["processing_date"] = proc_date

    # --- Derived: line-item aggregates ---
    any_qty_exceeds_po = False
    any_qty_exceeds_grn = False
    any_rate_mismatch = False

    for item in invoice.line_items:
        if item.invoice_qty > item.po_qty:
            any_qty_exceeds_po = True
        if item.grn_qty is not None and item.invoice_qty > item.grn_qty:
            any_qty_exceeds_grn = True
        if item.po_unit_rate != 0:
            rate_diff_pct = abs(item.invoice_unit_rate - item.po_unit_rate) / item.po_unit_rate * 100
            if rate_diff_pct > 2.0:
                any_rate_mismatch = True

    ctx["any_line_qty_exceeds_po"] = any_qty_exceeds_po
    ctx["any_line_qty_exceeds_grn"] = any_qty_exceeds_grn
    ctx["any_line_rate_mismatch"] = any_rate_mismatch

    # --- Derived: tax checks ---
    tax_calc_error = False
    if (invoice.taxable_amount is not None and invoice.tax_amount is not None
            and invoice.invoice_total is not None):
        if abs(invoice.taxable_amount + invoice.tax_amount - invoice.invoice_total) > 1.0:
            tax_calc_error = True

    tax_component_error = False
    if invoice.supply_type == "intra_state":
        if invoice.igst_amount > 0:
            tax_component_error = True
        if abs(invoice.cgst_amount - invoice.sgst_amount) > 0.01:
            tax_component_error = True
        if invoice.cgst_amount == 0 and invoice.sgst_amount == 0:
            tax_component_error = True
    elif invoice.supply_type == "inter_state":
        if invoice.igst_amount == 0:
            tax_component_error = True
        if invoice.cgst_amount > 0 or invoice.sgst_amount > 0:
            tax_component_error = True

    ctx["tax_calc_error"] = tax_calc_error
    ctx["tax_component_error"] = tax_component_error

    # --- Derived: PAN-GSTIN cross-check ---
    pan_gstin_mismatch = False
    if invoice.vendor_gstin and invoice.vendor_pan:
        embedded_pan = invoice.vendor_gstin[2:12]  # chars 3–12 (0-indexed 2–11)
        pan_gstin_mismatch = embedded_pan.upper() != invoice.vendor_pan.upper()
    ctx["pan_gstin_mismatch"] = pan_gstin_mismatch

    # --- Derived: compliance failure flag ---
    compliance_failure = (
        tax_calc_error or tax_component_error or pan_gstin_mismatch
    )
    ctx["compliance_failure"] = compliance_failure

    # --- Derived: deviation detected ---
    deviation_detected = (
        any_qty_exceeds_po
        or any_qty_exceeds_grn
        or any_rate_mismatch
        or abs(deviation_pct) > 1.0
        or (invoice.grn_exists and invoice.grn_date and invoice.invoice_date
            and invoice.grn_date > invoice.invoice_date)
    )
    ctx["deviation_detected"] = deviation_detected

    # --- Derived: all_validations_passed ---
    # True if none of the hard-fail validation flags are set
    ctx["all_validations_passed"] = not any([
        tax_calc_error,
        tax_component_error,
        pan_gstin_mismatch,
        any_qty_exceeds_grn,
    ])

    return ctx


# ---------------------------------------------------------------------------
# Condition Evaluator
# ---------------------------------------------------------------------------

class ConditionEvaluator:
    """Recursively evaluates a Condition tree against an invoice context."""

    def evaluate(
        self,
        condition: Union[CompositeCondition, OperandCondition],
        ctx: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """
        Returns (triggered: bool, error_message: str | None).
        """
        if isinstance(condition, CompositeCondition):
            return self._eval_composite(condition, ctx)
        return self._eval_operand(condition, ctx)

    def _eval_composite(
        self,
        cond: CompositeCondition,
        ctx: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        results = []
        for operand in cond.operands:
            result, err = self.evaluate(operand, ctx)
            if err:
                return False, err
            results.append(result)

        if cond.operator == "AND":
            return all(results), None
        if cond.operator == "OR":
            return any(results), None
        if cond.operator == "NOT":
            return not results[0], None

        return False, f"Unknown operator: {cond.operator}"

    def _eval_operand(
        self,
        cond: OperandCondition,
        ctx: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        field_val = ctx.get(cond.field)
        op = cond.op

        # --- Null checks ---
        if op == "is_null":
            return field_val is None, None
        if op == "is_not_null":
            return field_val is not None, None
        if op == "exists":
            return cond.field in ctx, None

        # Resolve right-hand side
        rhs = _SafeEval.evaluate(cond.value, ctx) if isinstance(cond.value, str) else cond.value

        # Handle None left-hand side gracefully
        if field_val is None:
            return False, None

        # --- Date comparisons (ISO string comparison is lexicographically correct) ---
        # Both sides as strings → natural ISO ordering works

        try:
            if op == "==":
                return field_val == rhs, None
            if op == "!=":
                return field_val != rhs, None
            if op == ">":
                return _compare(field_val, rhs) > 0, None
            if op == "<":
                return _compare(field_val, rhs) < 0, None
            if op == ">=":
                return _compare(field_val, rhs) >= 0, None
            if op == "<=":
                return _compare(field_val, rhs) <= 0, None
            if op == "in":
                return field_val in rhs, None
            if op == "not_in":
                return field_val not in rhs, None
        except (TypeError, ValueError) as exc:
            return False, f"Comparison error on field '{cond.field}': {exc}"

        return False, f"Unknown operator: {op}"


def _compare(a: Any, b: Any) -> int:
    """Return negative, zero, or positive like cmp(a, b)."""
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Rule Engine
# ---------------------------------------------------------------------------

class RuleEngine:
    """
    Evaluates a set of AP policy rules against an invoice.

    Usage:
        engine = RuleEngine()
        engine.load_rules("output/extracted_rules.json")
        report = engine.evaluate(invoice)
    """

    def __init__(self) -> None:
        self.rules: List[Rule] = []
        self._evaluator = ConditionEvaluator()

    def load_rules(self, path: str | Path) -> None:
        """Load rules from a JSON file (extracted_rules.json format)."""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        rule_set = RuleSet.model_validate(data)
        self.rules = sorted(rule_set.rules, key=lambda r: r.priority)
        logger.info("Loaded %d rules from %s", len(self.rules), path)

    def load_rule_set(self, rule_set: RuleSet) -> None:
        self.rules = sorted(rule_set.rules, key=lambda r: r.priority)

    def evaluate(
        self,
        invoice: Invoice,
        processing_date: Optional[str] = None,
        stop_on_terminal: bool = True,
    ) -> EvaluationReport:
        """
        Evaluate all rules against an invoice.

        Args:
            invoice:           The invoice to validate.
            processing_date:   Override today's date (ISO YYYY-MM-DD).
            stop_on_terminal:  If True, stop evaluating after the first REJECT
                               or COMPLIANCE_HOLD action fires.

        Returns:
            EvaluationReport with triggered rules, final status, and
            notifications to send.
        """
        ctx = build_context(invoice, processing_date)
        triggered: List[RuleResult] = []
        non_triggered: List[str] = []
        errors: List[str] = []
        notifications: List[Dict[str, Any]] = []

        final_status = "PENDING"

        for rule in self.rules:
            try:
                fired, err = self._evaluator.evaluate(rule.condition, ctx)
            except Exception as exc:
                err_msg = f"Rule {rule.rule_id} evaluation crashed: {exc}"
                logger.error(err_msg)
                errors.append(err_msg)
                non_triggered.append(rule.rule_id)
                continue

            if err:
                errors.append(f"Rule {rule.rule_id}: {err}")

            result = RuleResult(
                rule_id=rule.rule_id,
                source_clause=rule.source_clause,
                description=rule.description,
                triggered=fired,
                action=rule.action.value if fired else None,
                action_detail=rule.action_detail if fired else None,
                requires_justification=rule.requires_justification if fired else False,
                notification=rule.notification if fired else None,
                error=err,
            )

            if fired:
                triggered.append(result)
                final_status = self._update_status(final_status, rule.action.value)

                # Collect notification payload
                if rule.notification:
                    notifications.append(
                        self._build_notification_payload(rule, invoice, ctx)
                    )

                if stop_on_terminal and rule.action.value in TERMINAL_ACTIONS:
                    logger.info(
                        "Invoice %s: terminal action %s fired by rule %s — stopping evaluation.",
                        invoice.invoice_number, rule.action.value, rule.rule_id,
                    )
                    break
            else:
                non_triggered.append(rule.rule_id)

        return EvaluationReport(
            invoice_number=invoice.invoice_number,
            evaluation_timestamp=datetime.utcnow().isoformat() + "Z",
            final_status=final_status,
            triggered_rules=triggered,
            non_triggered_rules=non_triggered,
            errors=errors,
            notifications_to_send=notifications,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    _STATUS_PRIORITY = {
        "PENDING": 0,
        "AUTO_APPROVE": 1,
        "FLAG": 2,
        "HOLD": 3,
        "ROUTE_TO_AP_CLERK": 4,
        "ROUTE_TO_AP_MANAGER": 4,
        "ROUTE_TO_DEPT_HEAD": 5,
        "ROUTE_TO_PROCUREMENT": 5,
        "ROUTE_TO_FINANCE_CONTROLLER": 6,
        "ESCALATE_TO_FINANCE_CONTROLLER": 6,
        "ROUTE_TO_CFO": 7,
        "COMPLIANCE_HOLD": 8,
        "REJECT": 9,
        "SEND_EMAIL": 0,
        "SEND_IMMEDIATE_EMAIL": 0,
        "ESCALATE_TO_NEXT_LEVEL": 0,
    }

    def _update_status(self, current: str, new_action: str) -> str:
        """Keep the 'most severe' status encountered so far."""
        current_prio = self._STATUS_PRIORITY.get(current, 0)
        new_prio = self._STATUS_PRIORITY.get(new_action, 0)
        if new_prio > current_prio:
            return new_action
        return current

    def _build_notification_payload(
        self,
        rule: Rule,
        invoice: Invoice,
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        notif: Notification = rule.notification  # type: ignore[assignment]
        include = notif.include_fields or [
            "invoice_number", "vendor_name", "po_number",
            "deviation_type", "deviation_details", "recommended_action",
        ]
        body = {field: ctx.get(field) for field in include}
        body["deviation_type"] = invoice.deviation_type or _derive_deviation_type(rule)
        body["deviation_details"] = invoice.deviation_details or rule.action_detail

        return {
            "rule_id": rule.rule_id,
            "to": notif.to,
            "within_minutes": notif.within_minutes,
            "subject": notif.subject_template or f"AP Alert: {rule.description[:60]}",
            "body": body,
        }


def _derive_deviation_type(rule: Rule) -> str:
    mapping = {
        "PO_MATCH": "Amount Mismatch",
        "LINE_ITEM": "Quantity Mismatch",
        "GRN_MATCH": "Quantity Mismatch",
        "TAX": "Compliance Failure",
    }
    return mapping.get(rule.category.value, "Deviation")
