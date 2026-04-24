"""
CLI Entry Point — Cashflo AP Policy Rule Engine

Commands:
  extract   Parse policy document and extract rules using LLM
  evaluate  Run extracted rules against an invoice JSON
  detect    Run static conflict detection on a rules JSON file
  demo      Run a built-in demo with sample invoices (no API key needed)

Examples:
  python -m src.main extract --policy policy/cashflo_ap_policy.txt
  python -m src.main evaluate --invoice tests/sample_invoices.json --rules output/extracted_rules.json
  python -m src.main detect  --rules output/extracted_rules.json
  python -m src.main demo
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ap_engine")

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on shell env vars


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_extract(args: argparse.Namespace) -> None:
    """Parse policy document and extract rules via LLM."""
    from src.parser.document_parser import DocumentParser
    from src.extractor.rule_extractor import RuleExtractor

    policy_path = Path(args.policy)
    if not policy_path.exists():
        logger.error("Policy file not found: %s", policy_path)
        sys.exit(1)

    logger.info("Parsing policy document: %s", policy_path)
    parser = DocumentParser()
    doc = parser.parse(policy_path)
    logger.info("%s", parser.summarise(doc))

    logger.info("Starting LLM rule extraction (provider=%s, model=%s) …",
                args.provider, args.model or "default")
    extractor = RuleExtractor(provider=args.provider, model=args.model)
    rule_set = extractor.extract(doc)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        rule_set.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8"
    )

    logger.info("Extracted %d rules → %s", rule_set.total_rules, out_path)
    if rule_set.conflicts:
        logger.warning("Detected %d conflict(s). See output file for details.",
                       len(rule_set.conflicts))


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Evaluate an invoice (or batch of invoices) against extracted rules."""
    from src.engine.rule_engine import RuleEngine
    from src.models import Invoice
    from src.notifier.email_notifier import EmailNotifier

    rules_path = Path(args.rules)
    if not rules_path.exists():
        logger.error("Rules file not found: %s", rules_path)
        sys.exit(1)

    invoice_path = Path(args.invoice)
    if not invoice_path.exists():
        logger.error("Invoice file not found: %s", invoice_path)
        sys.exit(1)

    engine = RuleEngine()
    engine.load_rules(rules_path)

    notifier = EmailNotifier()

    raw = json.loads(invoice_path.read_text(encoding="utf-8"))
    invoices = raw if isinstance(raw, list) else [raw]

    all_reports = []
    for inv_data in invoices:
        invoice = Invoice.model_validate(inv_data)
        report = engine.evaluate(invoice)

        # Send / log notifications
        notifier.send_notifications(report, invoice)

        all_reports.append(report.model_dump(exclude_none=True))

        _print_report(report)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")
        logger.info("Evaluation reports written to %s", out)


def cmd_detect(args: argparse.Namespace) -> None:
    """Run static conflict detection on a rules JSON file."""
    from src.conflict_detector.conflict_detector import ConflictDetector
    from src.models import RuleSet

    rules_path = Path(args.rules)
    if not rules_path.exists():
        logger.error("Rules file not found: %s", rules_path)
        sys.exit(1)

    data = json.loads(rules_path.read_text(encoding="utf-8"))
    rule_set = RuleSet.model_validate(data)

    detector = ConflictDetector()
    conflicts = detector.detect(rule_set.rules)

    print("\n" + detector.report_summary(conflicts))

    if args.output:
        out = Path(args.output)
        out.write_text(
            json.dumps([c.model_dump() for c in conflicts], indent=2),
            encoding="utf-8",
        )
        logger.info("Conflict report written to %s", out)


def cmd_demo(args: argparse.Namespace) -> None:
    """Run a built-in demo without requiring an API key."""
    from src.engine.rule_engine import RuleEngine
    from src.models import Invoice, LineItem
    from src.notifier.email_notifier import EmailNotifier
    from src.conflict_detector.conflict_detector import ConflictDetector
    from src.models import RuleSet
    import json

    rules_path = Path("output/extracted_rules.json")
    if not rules_path.exists():
        logger.error("Pre-extracted rules not found at %s. Run 'extract' first.", rules_path)
        sys.exit(1)

    engine = RuleEngine()
    engine.load_rules(rules_path)
    notifier = EmailNotifier()  # defaults to dry-run

    demo_invoices = _build_demo_invoices()

    print("\n" + "=" * 70)
    print("  Cashflo AP Policy Rule Engine — Demo Run")
    print("=" * 70)

    for label, invoice in demo_invoices:
        print(f"\n{'─' * 70}")
        print(f"  Scenario: {label}")
        print("─" * 70)
        report = engine.evaluate(invoice)
        _print_report(report)
        notifier.send_notifications(report, invoice)

    # Also run static conflict detection
    print(f"\n{'─' * 70}")
    print("  Static Conflict Detection")
    print("─" * 70)
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    rule_set = RuleSet.model_validate(data)
    detector = ConflictDetector()
    conflicts = detector.detect(rule_set.rules)
    print(detector.report_summary(conflicts))
    print()


# ---------------------------------------------------------------------------
# Demo invoice scenarios
# ---------------------------------------------------------------------------

def _build_demo_invoices():
    from src.models import Invoice, LineItem

    scenarios = []

    # 1. Happy path — clean invoice within tolerance
    scenarios.append(("Happy Path: Clean Invoice (auto-approve)", Invoice(
        invoice_number="INV-2026-001",
        invoice_date="2026-04-20",
        vendor_gstin="27AABCU9603R1ZX",
        vendor_name="TechSupplies Pvt Ltd",
        vendor_pan="AABCU9603R",
        vendor_master_gstin="27AABCU9603R1ZX",
        po_number="PO-2026-100",
        invoice_total=95000.0,
        taxable_amount=80508.47,
        tax_amount=14491.53,
        po_amount=95500.0,
        po_active=True,
        po_type="goods",
        grn_exists=True,
        grn_date="2026-04-18",
        is_handwritten=False,
        duplicate_exists=False,
        vendor_on_watchlist=False,
        supply_type="intra_state",
        place_of_supply_state="27",
        buyer_gstin_state="27",
        qr_code_present=False,
        cgst_amount=7245.76,
        sgst_amount=7245.77,
        igst_amount=0.0,
        line_items=[
            LineItem(line_id="L1", invoice_qty=10, po_qty=10, grn_qty=10,
                     invoice_unit_rate=9500.0, po_unit_rate=9550.0,
                     taxable_amount=95000.0)
        ]
    )))

    # 2. Over-invoiced by 12% — triggers Finance Controller escalation
    scenarios.append(("Over-Invoiced ≥10%: Escalate to Finance Controller", Invoice(
        invoice_number="INV-2026-002",
        invoice_date="2026-04-21",
        vendor_gstin="27AABCU9603R1ZX",
        vendor_name="TechSupplies Pvt Ltd",
        vendor_pan="AABCU9603R",
        vendor_master_gstin="27AABCU9603R1ZX",
        po_number="PO-2026-101",
        invoice_total=112000.0,
        taxable_amount=94915.25,
        tax_amount=17084.75,
        po_amount=100000.0,
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
        cgst_amount=8542.37,
        sgst_amount=8542.38,
        igst_amount=0.0,
        line_items=[]
    )))

    # 3. GSTIN mismatch — compliance reject
    scenarios.append(("GSTIN Mismatch: Compliance Rejection", Invoice(
        invoice_number="INV-2026-003",
        invoice_date="2026-04-22",
        vendor_gstin="27AABCU9603R1ZX",
        vendor_name="Fake Vendor Ltd",
        vendor_pan="AABCU9603R",
        vendor_master_gstin="27AABCU9999R1ZY",   # ← different GSTIN on file
        po_number="PO-2026-102",
        invoice_total=50000.0,
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
        line_items=[]
    )))

    # 4. Invoice quantity exceeds GRN quantity — reject
    scenarios.append(("Invoice Qty > GRN Qty: Reject", Invoice(
        invoice_number="INV-2026-004",
        invoice_date="2026-04-22",
        vendor_gstin="29AABCE1234F1ZR",
        vendor_name="Office Supplies Co",
        vendor_pan="AABCE1234F",
        vendor_master_gstin="29AABCE1234F1ZR",
        po_number="PO-2026-103",
        invoice_total=30000.0,
        taxable_amount=25423.73,
        tax_amount=4576.27,
        po_amount=30000.0,
        po_active=True,
        po_type="goods",
        grn_exists=True,
        grn_date="2026-04-20",
        is_handwritten=False,
        duplicate_exists=False,
        vendor_on_watchlist=False,
        supply_type="intra_state",
        place_of_supply_state="29",
        buyer_gstin_state="29",
        qr_code_present=False,
        cgst_amount=2288.14,
        sgst_amount=2288.13,
        igst_amount=0.0,
        line_items=[
            LineItem(line_id="L1", invoice_qty=15, po_qty=10, grn_qty=8,  # inv > grn
                     invoice_unit_rate=2000.0, po_unit_rate=2000.0,
                     taxable_amount=30000.0)
        ]
    )))

    # 5. Large invoice >50L — CFO route + QR code required
    scenarios.append(("Large Invoice >50L: CFO Route + QR Check", Invoice(
        invoice_number="INV-2026-005",
        invoice_date="2026-04-23",
        vendor_gstin="07AAACR5055K1ZA",
        vendor_name="Infrastructure Corp",
        vendor_pan="AAACR5055K",
        vendor_master_gstin="07AAACR5055K1ZA",
        po_number="PO-2026-104",
        invoice_total=6500000.0,  # INR 65L
        taxable_amount=5508474.58,
        tax_amount=991525.42,
        po_amount=6500000.0,
        po_active=True,
        po_type="service",
        grn_exists=False,
        is_handwritten=False,
        duplicate_exists=False,
        vendor_on_watchlist=False,
        supply_type="inter_state",
        place_of_supply_state="27",
        buyer_gstin_state="27",
        qr_code_present=False,   # ← Missing QR, should trigger hold
        cgst_amount=0.0,
        sgst_amount=0.0,
        igst_amount=991525.42,
        line_items=[]
    )))

    # 6. Watchlist vendor — always Dept Head regardless of amount
    scenarios.append(("Watchlist Vendor: Force Dept Head Approval", Invoice(
        invoice_number="INV-2026-006",
        invoice_date="2026-04-23",
        vendor_gstin="06AABCW1234K1ZB",
        vendor_name="Watchlisted Vendor",
        vendor_pan="AABCW1234K",
        vendor_master_gstin="06AABCW1234K1ZB",
        po_number="PO-2026-105",
        invoice_total=75000.0,   # ≤1L but on watchlist
        po_amount=75000.0,
        po_active=True,
        po_type="service",
        grn_exists=False,
        is_handwritten=False,
        duplicate_exists=False,
        vendor_on_watchlist=True,  # ← watchlist flag
        supply_type="intra_state",
        place_of_supply_state="06",
        buyer_gstin_state="06",
        qr_code_present=False,
        cgst_amount=0.0,
        sgst_amount=0.0,
        igst_amount=0.0,
        line_items=[]
    )))

    return scenarios


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def _print_report(report) -> None:
    status_symbols = {
        "AUTO_APPROVE": "✓",
        "REJECT": "✗",
        "HOLD": "⏸",
        "FLAG": "⚑",
        "COMPLIANCE_HOLD": "⛔",
    }
    symbol = status_symbols.get(report.final_status, "→")
    print(f"\n  Invoice  : {report.invoice_number}")
    print(f"  Status   : {symbol} {report.final_status}")
    print(f"  Timestamp: {report.evaluation_timestamp}")

    if report.triggered_rules:
        print(f"\n  Triggered Rules ({len(report.triggered_rules)}):")
        for r in report.triggered_rules:
            flag = "  [!]" if r.requires_justification else "  [·]"
            print(f"{flag} {r.rule_id:15s} {r.source_clause:20s} → {r.action}")
            if r.action_detail:
                print(f"       Detail: {r.action_detail}")
            if r.notification:
                print(f"       Notify: {', '.join(r.notification.to)} within {r.notification.within_minutes}m")

    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for e in report.errors:
            print(f"  [ERR] {e}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ap-engine",
        description="Cashflo AP Policy Rule Engine — extract, execute, and validate AP rules.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # extract
    p_extract = sub.add_parser("extract", help="Extract rules from policy doc via LLM")
    p_extract.add_argument("--policy", default="policy/Sample_AP_Policy_Document (1).md",
                           help="Path to policy .md, .txt or .pdf file")
    p_extract.add_argument("--output", default="output/extracted_rules.json",
                           help="Output path for extracted rules JSON")
    p_extract.add_argument("--provider", default="groq",
                           choices=["groq", "openai", "anthropic"],
                           help="LLM provider (default: groq — free, get key at console.groq.com)")
    p_extract.add_argument("--model", default=None,
                           help="Override LLM model name")

    # evaluate
    p_eval = sub.add_parser("evaluate", help="Run rules against an invoice JSON")
    p_eval.add_argument("--invoice", required=True,
                        help="Path to invoice JSON file (single object or array)")
    p_eval.add_argument("--rules", default="output/extracted_rules.json",
                        help="Path to extracted rules JSON")
    p_eval.add_argument("--output", default=None,
                        help="Optional: save evaluation reports to this path")

    # detect
    p_detect = sub.add_parser("detect", help="Run static conflict detection")
    p_detect.add_argument("--rules", default="output/extracted_rules.json",
                          help="Path to extracted rules JSON")
    p_detect.add_argument("--output", default=None,
                          help="Optional: save conflict report to this path")

    # demo
    sub.add_parser("demo", help="Run built-in demo scenarios (no API key needed)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "extract":
        cmd_extract(args)
    elif args.command == "evaluate":
        cmd_evaluate(args)
    elif args.command == "detect":
        cmd_detect(args)
    elif args.command == "demo":
        cmd_demo(args)


if __name__ == "__main__":
    main()
