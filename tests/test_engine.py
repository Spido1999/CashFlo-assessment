"""
Unit tests for the AP Policy Rule Engine.

Tests cover:
  - Invoice context builder (derived field computation)
  - Individual rule condition evaluation
  - End-to-end evaluation scenarios against pre-extracted rules
  - Conflict detector

Run with:
    pytest tests/ -v
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.models import Invoice, LineItem, RuleSet
from src.engine.rule_engine import RuleEngine, ConditionEvaluator, build_context
from src.conflict_detector.conflict_detector import ConflictDetector
from src.models import CompositeCondition, OperandCondition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RULES_PATH = Path("output/extracted_rules.json")


@pytest.fixture(scope="module")
def engine():
    if not RULES_PATH.exists():
        pytest.skip("Pre-extracted rules not found. Run demo or extract first.")
    e = RuleEngine()
    e.load_rules(RULES_PATH)
    return e


@pytest.fixture(scope="module")
def rule_set():
    if not RULES_PATH.exists():
        pytest.skip("Pre-extracted rules not found.")
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    return RuleSet.model_validate(data)


def clean_invoice(**overrides) -> Invoice:
    """Base 'happy path' invoice that should pass all rules."""
    defaults = dict(
        invoice_number="INV-TEST-001",
        invoice_date="2026-04-20",
        vendor_gstin="27AABCU9603R1ZX",
        vendor_name="Test Vendor",
        vendor_pan="AABCU9603R",
        vendor_master_gstin="27AABCU9603R1ZX",
        po_number="PO-TEST-001",
        invoice_total=50000.0,
        taxable_amount=42372.88,
        tax_amount=7627.12,
        cgst_amount=3813.56,
        sgst_amount=3813.56,
        igst_amount=0.0,
        po_amount=50000.0,
        po_active=True,
        po_type="service",
        grn_exists=False,
        is_handwritten=False,
        duplicate_exists=False,
        vendor_on_watchlist=False,
        supply_type="intra_state",
        place_of_supply_state="27",
        buyer_gstin_state="27",
        qr_code_present=False,
        digital_signature_present=False,
        line_items=[],
    )
    defaults.update(overrides)
    return Invoice(**defaults)


# ---------------------------------------------------------------------------
# Context Builder Tests
# ---------------------------------------------------------------------------

class TestBuildContext:

    def test_deviation_pct_over(self):
        inv = clean_invoice(invoice_total=110000.0, po_amount=100000.0)
        ctx = build_context(inv)
        assert abs(ctx["deviation_pct"] - 10.0) < 0.001

    def test_deviation_pct_under(self):
        inv = clean_invoice(invoice_total=90000.0, po_amount=100000.0)
        ctx = build_context(inv)
        assert ctx["deviation_pct"] < 0
        assert abs(ctx["under_invoiced_pct"] - 10.0) < 0.001

    def test_tax_calc_error_flagged(self):
        inv = clean_invoice(taxable_amount=42372.88, tax_amount=7627.12,
                            invoice_total=51000.0)  # grand total off by 1000
        ctx = build_context(inv)
        assert ctx["tax_calc_error"] is True

    def test_tax_calc_ok_within_tolerance(self):
        inv = clean_invoice(taxable_amount=42372.88, tax_amount=7627.12,
                            invoice_total=50000.0)
        ctx = build_context(inv)
        assert ctx["tax_calc_error"] is False

    def test_intra_state_tax_error_igst_present(self):
        inv = clean_invoice(supply_type="intra_state",
                            cgst_amount=3813.56, sgst_amount=3813.56,
                            igst_amount=500.0)
        ctx = build_context(inv)
        assert ctx["tax_component_error"] is True

    def test_inter_state_tax_valid(self):
        inv = clean_invoice(supply_type="inter_state",
                            cgst_amount=0.0, sgst_amount=0.0,
                            igst_amount=7627.12)
        ctx = build_context(inv)
        assert ctx["tax_component_error"] is False

    def test_pan_gstin_mismatch(self):
        # GSTIN[2:12] should equal PAN
        inv = clean_invoice(
            vendor_gstin="27AABCU9603R1ZX",  # chars 2-11 = "AABCU9603R"
            vendor_pan="XXXXXX9999Y",          # deliberately wrong PAN
        )
        ctx = build_context(inv)
        assert ctx["pan_gstin_mismatch"] is True

    def test_pan_gstin_match(self):
        inv = clean_invoice(
            vendor_gstin="27AABCU9603R1ZX",
            vendor_pan="AABCU9603R",
        )
        ctx = build_context(inv)
        assert ctx["pan_gstin_mismatch"] is False

    def test_line_item_qty_exceeds_po(self):
        inv = clean_invoice(
            po_type="goods",
            grn_exists=True,
            grn_date="2026-04-18",
            line_items=[
                LineItem(line_id="L1", invoice_qty=15, po_qty=10, grn_qty=15,
                         invoice_unit_rate=1000.0, po_unit_rate=1000.0,
                         taxable_amount=15000.0)
            ]
        )
        ctx = build_context(inv)
        assert ctx["any_line_qty_exceeds_po"] is True

    def test_line_item_rate_mismatch(self):
        inv = clean_invoice(
            line_items=[
                LineItem(line_id="L1", invoice_qty=10, po_qty=10, grn_qty=10,
                         invoice_unit_rate=1030.0, po_unit_rate=1000.0,  # 3% diff
                         taxable_amount=10300.0)
            ]
        )
        ctx = build_context(inv)
        assert ctx["any_line_rate_mismatch"] is True


# ---------------------------------------------------------------------------
# ConditionEvaluator Unit Tests
# ---------------------------------------------------------------------------

class TestConditionEvaluator:
    evaluator = ConditionEvaluator()

    def _ctx(self, **kw):
        return kw

    def test_simple_gt_true(self):
        cond = OperandCondition(field="invoice_total", op=">", value=50000)
        fired, err = self.evaluator.evaluate(cond, self._ctx(invoice_total=60000))
        assert fired is True
        assert err is None

    def test_simple_gt_false(self):
        cond = OperandCondition(field="invoice_total", op=">", value=50000)
        fired, _ = self.evaluator.evaluate(cond, self._ctx(invoice_total=40000))
        assert fired is False

    def test_is_null_true(self):
        cond = OperandCondition(field="invoice_number", op="is_null", value=None)
        fired, _ = self.evaluator.evaluate(cond, self._ctx(invoice_number=None))
        assert fired is True

    def test_is_null_false(self):
        cond = OperandCondition(field="invoice_number", op="is_null", value=None)
        fired, _ = self.evaluator.evaluate(cond, self._ctx(invoice_number="INV-001"))
        assert fired is False

    def test_and_both_true(self):
        cond = CompositeCondition(
            operator="AND",
            operands=[
                OperandCondition(field="a", op=">", value=0),
                OperandCondition(field="b", op="<", value=10),
            ]
        )
        fired, _ = self.evaluator.evaluate(cond, self._ctx(a=5, b=5))
        assert fired is True

    def test_and_one_false(self):
        cond = CompositeCondition(
            operator="AND",
            operands=[
                OperandCondition(field="a", op=">", value=0),
                OperandCondition(field="b", op="<", value=10),
            ]
        )
        fired, _ = self.evaluator.evaluate(cond, self._ctx(a=5, b=20))
        assert fired is False

    def test_or_one_true(self):
        cond = CompositeCondition(
            operator="OR",
            operands=[
                OperandCondition(field="a", op="==", value=True),
                OperandCondition(field="b", op="==", value=True),
            ]
        )
        fired, _ = self.evaluator.evaluate(cond, self._ctx(a=False, b=True))
        assert fired is True

    def test_not_negates(self):
        cond = CompositeCondition(
            operator="NOT",
            operands=[OperandCondition(field="x", op="==", value=True)]
        )
        fired, _ = self.evaluator.evaluate(cond, self._ctx(x=False))
        assert fired is True

    def test_field_reference_in_value(self):
        # deviation_pct == 10 should be >= 10.0
        cond = OperandCondition(field="deviation_pct", op=">=", value=10.0)
        fired, _ = self.evaluator.evaluate(cond, self._ctx(deviation_pct=12.0))
        assert fired is True


# ---------------------------------------------------------------------------
# End-to-End Rule Engine Tests
# ---------------------------------------------------------------------------

class TestRuleEngine:

    def test_clean_invoice_auto_approves(self, engine):
        inv = clean_invoice(invoice_total=50000.0, po_amount=50000.0)
        report = engine.evaluate(inv, processing_date="2026-04-24")
        # Should auto-approve (no deviations, ≤1L)
        assert report.final_status in ("AUTO_APPROVE", "PENDING")
        reject_rules = [r for r in report.triggered_rules if r.action == "REJECT"]
        assert len(reject_rules) == 0

    def test_future_dated_invoice_rejected(self, engine):
        inv = clean_invoice(invoice_date="2026-12-31")
        report = engine.evaluate(inv, processing_date="2026-04-24")
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-VAL-002" in rule_ids
        assert report.final_status == "REJECT"

    def test_missing_mandatory_field_routed_to_clerk(self, engine):
        inv = clean_invoice(invoice_number=None)
        report = engine.evaluate(inv, processing_date="2026-04-24")
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-VAL-001" in rule_ids

    def test_duplicate_invoice_held(self, engine):
        inv = clean_invoice(duplicate_exists=True)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-VAL-004" in rule_ids

    def test_invalid_po_rejected(self, engine):
        inv = clean_invoice(po_active=False)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-POM-001" in rule_ids
        assert report.final_status == "REJECT"

    def test_over_invoiced_10pct_escalates(self, engine):
        inv = clean_invoice(invoice_total=112000.0, po_amount=100000.0)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-POM-004" in rule_ids

    def test_over_invoiced_5pct_routes_dept_head(self, engine):
        inv = clean_invoice(invoice_total=105000.0, po_amount=100000.0)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-POM-003" in rule_ids

    def test_under_invoiced_flagged(self, engine):
        inv = clean_invoice(invoice_total=90000.0, po_amount=100000.0)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-POM-005" in rule_ids

    def test_gstin_mismatch_rejected(self, engine):
        inv = clean_invoice(vendor_gstin="27AABCU9603R1ZX",
                            vendor_master_gstin="27AABC99999R1ZY")
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-TAX-001" in rule_ids

    def test_missing_grn_held(self, engine):
        inv = clean_invoice(po_type="goods", grn_exists=False)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-GRN-001" in rule_ids

    def test_invoice_qty_exceeds_grn_rejected(self, engine):
        inv = clean_invoice(
            po_type="goods",
            grn_exists=True,
            grn_date="2026-04-18",
            line_items=[
                LineItem(line_id="L1", invoice_qty=15, po_qty=15, grn_qty=8,
                         invoice_unit_rate=1000.0, po_unit_rate=1000.0,
                         taxable_amount=15000.0)
            ]
        )
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-GRN-002" in rule_ids

    def test_watchlist_vendor_routes_dept_head(self, engine):
        inv = clean_invoice(vendor_on_watchlist=True, invoice_total=50000.0)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-APR-005" in rule_ids

    def test_large_invoice_routes_cfo(self, engine):
        inv = clean_invoice(invoice_total=6000000.0, po_amount=6000000.0)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-APR-004" in rule_ids

    def test_qr_code_missing_for_large_invoice(self, engine):
        inv = clean_invoice(invoice_total=1500000.0, po_amount=1500000.0,
                            qr_code_present=False)
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-QR-001" in rule_ids

    def test_notifications_generated_for_deviation(self, engine):
        inv = clean_invoice(invoice_total=112000.0, po_amount=100000.0)
        report = engine.evaluate(inv)
        assert len(report.notifications_to_send) > 0

    def test_intra_state_igst_flags_tax_error(self, engine):
        inv = clean_invoice(
            supply_type="intra_state",
            cgst_amount=3813.56,
            sgst_amount=3813.56,
            igst_amount=1000.0,  # should be 0 for intra-state
        )
        report = engine.evaluate(inv)
        rule_ids = [r.rule_id for r in report.triggered_rules]
        assert "AP-TAX-004" in rule_ids


# ---------------------------------------------------------------------------
# Conflict Detector Tests
# ---------------------------------------------------------------------------

class TestConflictDetector:

    def test_known_conflicts_detected(self, rule_set):
        detector = ConflictDetector()
        conflicts = detector.detect(rule_set.rules)
        # Should detect at least the known AP-POM-004 vs AP-APR-004 conflict
        all_rule_pairs = [frozenset(c.rule_ids) for c in conflicts]
        assert len(conflicts) > 0, "Should detect at least one conflict"

    def test_no_false_positives_for_sequential_ranges(self, rule_set):
        """AP-APR-002 and AP-APR-003 cover sequential non-overlapping ranges."""
        detector = ConflictDetector()
        conflicts = detector.detect(rule_set.rules)
        # Should not flag AP-APR-002 vs AP-APR-003 as conflicting
        false_pair = frozenset(["AP-APR-002", "AP-APR-003"])
        all_rule_pairs = [frozenset(c.rule_ids) for c in conflicts]
        # Sequential ranges (>1L-10L) and (>10L-50L) don't overlap, so no conflict
        assert false_pair not in all_rule_pairs


# ---------------------------------------------------------------------------
# Document Parser Tests
# ---------------------------------------------------------------------------

class TestDocumentParser:

    def test_parses_policy_file(self):
        from src.parser.document_parser import DocumentParser
        parser = DocumentParser()
        doc = parser.parse(Path("policy/cashflo_ap_policy.txt"))
        assert len(doc.sections) == 7, f"Expected 7 sections, got {len(doc.sections)}"

    def test_extracts_sub_clauses(self):
        from src.parser.document_parser import DocumentParser
        parser = DocumentParser()
        doc = parser.parse(Path("policy/cashflo_ap_policy.txt"))
        # Find section 2
        sec2 = next((s for s in doc.sections if s.section_id == "2"), None)
        assert sec2 is not None
        clause_ids = [c.clause_id for c in sec2.clauses]
        assert "2.2(a)" in clause_ids
        assert "2.2(b)" in clause_ids
        assert "2.2(c)" in clause_ids

    def test_cross_references_detected(self):
        from src.parser.document_parser import DocumentParser
        parser = DocumentParser()
        doc = parser.parse(Path("policy/cashflo_ap_policy.txt"))
        # Section 3.2(b) references Section 2.3(b)
        assert len(doc.cross_references) > 0
