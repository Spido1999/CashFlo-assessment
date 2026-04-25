# Cashflo AP Policy Rule Engine

Converts an Accounts Payable policy document into machine-executable JSON rules, then runs those rules against invoices to automatically approve, reject, hold, or escalate them.

Built for the **AI-First Software Engineer hiring challenge â€” Problem A**.

---

## What it does

1. **Reads** your AP policy document (`.md`, `.txt`, or `.pdf`)
2. **Extracts** every business rule using an LLM (Groq â€” free)
3. **Saves** rules as structured JSON (`output/extracted_rules.json`)
4. **Evaluates** any invoice against those rules â†’ gives a decision
5. **Detects** conflicts between rules
6. **Notifies** the right people by email when a deviation is found

---

## Quick Start (5 minutes)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Try the demo â€” no API key needed

Runs 6 built-in invoice scenarios against the pre-extracted rules:

```bash
python -m src.main demo
```

You'll see decisions like:
```
  Scenario: Over-Invoiced â‰¥10%: Escalate to Finance Controller
  Invoice  : INV-2026-002
  Status   : â†’ ESCALATE_TO_FINANCE_CONTROLLER
  Triggered Rules:
  [!] AP-POM-004   Section 2.2(c)  â†’ ESCALATE_TO_FINANCE_CONTROLLER
       Detail: Invoice amount exceeds PO by â‰¥10%
       Notify: finance_controller, internal_audit within 15m
```

### 3. Get a free Groq API key (for re-extracting rules)

1. Go to https://console.groq.com and sign up (free)
2. Create an API key
3. Add it to your `.env` file:

```
GROQ_API_KEY=gsk_your_key_here
```

### 4. Re-extract rules from the policy document

```bash
python -m src.main extract
```

This reads `policy/Sample_AP_Policy_Document (1).md` and writes fresh rules to `output/extracted_rules.json`.

### 5. Evaluate your own invoice

Create an invoice JSON file (see [Invoice Format](#invoice-format) below), then:

```bash
python -m src.main evaluate --invoice your_invoice.json
```

### 6. Detect rule conflicts

```bash
python -m src.main detect
```

### 7. Run tests

```bash
python -m pytest tests/ -v
```

---

## All CLI Commands

| Command | What it does | Needs API key? |
|---------|-------------|----------------|
| `python -m src.main demo` | Run 6 sample invoice scenarios | No |
| `python -m src.main extract` | Re-extract rules from policy doc | Yes (Groq) |
| `python -m src.main evaluate --invoice <file>` | Evaluate an invoice against rules | No |
| `python -m src.main detect` | Find conflicts in extracted rules | No |

**Useful options:**

```bash
# Use a different policy file
python -m src.main extract --policy policy/my_policy.md

# Save evaluation results to a file
python -m src.main evaluate --invoice tests/sample_invoices.json --output output/report.json

# Use a different LLM provider
python -m src.main extract --provider openai   # needs OPENAI_API_KEY
python -m src.main extract --provider anthropic # needs ANTHROPIC_API_KEY
```

---

## Project Structure

```
policy/
  Sample_AP_Policy_Document (1).md  â† AP policy source (7 sections)

src/
  models.py                    â† Data models (Rule, Invoice, Condition, â€¦)
  parser/
    document_parser.py         â† Splits policy into sections & clauses
  extractor/
    rule_extractor.py          â† Sends sections to LLM, parses JSON rules
    prompts.py                 â† LLM prompt templates
  engine/
    rule_engine.py             â† Evaluates rules against an invoice
  conflict_detector/
    conflict_detector.py       â† Finds overlapping / contradicting rules
  notifier/
    email_notifier.py          â† Sends email alerts (dry-run by default)
  main.py                      â† CLI entry point

output/
  extracted_rules.json         â† 32 pre-extracted rules (ready to use)

tests/
  test_engine.py               â† 40 pytest tests (all passing)
  sample_invoices.json         â† Sample invoice data
```

---

## How it works

```
Policy .md file
      â”‚
      â–¼ (regex splits into sections)
ParsedDocument  â†’  7 sections, 39 clauses
      â”‚
      â–¼ (Groq LLM, one section at a time)
extracted_rules.json  â†’  32 JSON rules
      â”‚
      â”œâ”€â”€â–¶ ConflictDetector  â†’  finds rule overlaps
      â”‚
      â””â”€â”€â–¶ RuleEngine  +  invoice.json  â†’  APPROVE / REJECT / HOLD / ESCALATE
                  â”‚
                  â””â”€â”€â–¶ EmailNotifier  â†’  alerts sent to right roles
```

---

## Invoice Format

Pass a JSON file with these fields:

```json
{
  "invoice_number": "INV-2026-001",
  "invoice_date": "2026-04-20",
  "vendor_name": "ACME Supplies",
  "vendor_gstin": "27AABCU9603R1ZX",
  "vendor_pan": "AABCU9603R",
  "vendor_master_gstin": "27AABCU9603R1ZX",
  "po_number": "PO-2026-100",
  "invoice_total": 95000.0,
  "po_amount": 95500.0,
  "po_active": true,
  "po_type": "goods",
  "grn_exists": true,
  "grn_date": "2026-04-18",
  "taxable_amount": 80508.47,
  "tax_amount": 14491.53,
  "cgst_amount": 7245.76,
  "sgst_amount": 7245.77,
  "igst_amount": 0.0,
  "supply_type": "intra_state",
  "place_of_supply_state": "27",
  "buyer_gstin_state": "27",
  "is_handwritten": false,
  "duplicate_exists": false,
  "vendor_on_watchlist": false,
  "qr_code_present": false,
  "line_items": [
    {
      "line_id": "L1",
      "invoice_qty": 10,
      "po_qty": 10,
      "grn_qty": 10,
      "invoice_unit_rate": 9500.0,
      "po_unit_rate": 9550.0,
      "taxable_amount": 95000.0
    }
  ]
}
```

See `tests/sample_invoices.json` for more examples.

---

## Extracted Rules

32 rules are extracted from 7 policy sections:

| Rule ID | Policy Section | Decision |
|---------|---------------|----------|
| AP-VAL-001 | Â§1.1 | Missing mandatory fields â†’ Flag to AP Clerk |
| AP-VAL-002 | Â§1.2 | Future-dated invoice â†’ Reject |
| AP-VAL-003 | Â§1.3 | Handwritten invoice >â‚¹50K â†’ Route to AP Manager |
| AP-VAL-004 | Â§1.4 | Duplicate invoice â†’ Hold |
| AP-POM-001 | Â§2.1 | Invalid PO reference â†’ Reject |
| AP-POM-002 | Â§2.2(a) | Within Â±1% of PO amount â†’ Auto-approve |
| AP-POM-003 | Â§2.2(b) | 1â€“10% over PO â†’ Route to Dept Head |
| AP-POM-004 | Â§2.2(c) | â‰¥10% over PO â†’ Escalate to Finance Controller |
| AP-POM-005 | Â§2.2(d) | >5% under PO â†’ Flag Under-Invoiced |
| AP-LIM-001 | Â§2.3(b) | Invoice qty > PO qty â†’ Hold |
| AP-LIM-002 | Â§2.3(c) | Unit rate diff >2% â†’ Route to Procurement |
| AP-GRN-001 | Â§3.1 | No GRN for goods PO â†’ Hold (Awaiting GRN) |
| AP-GRN-002 | Â§3.2(b) | Invoice qty > GRN qty â†’ Reject |
| AP-GRN-003 | Â§3.3 | GRN post-dated â†’ Flag |
| AP-TAX-001 | Â§4.1 | GSTIN mismatch â†’ Reject |
| AP-TAX-002 | Â§4.2 | PAN-GSTIN mismatch â†’ Compliance Hold |
| AP-TAX-003 | Â§4.3(a) | Tax calculation error â†’ Flag |
| AP-TAX-004 | Â§4.3(b-d) | Wrong tax components for supply type â†’ Flag |
| AP-TAX-005 | Â§4.4 | Place of supply mismatch â†’ Flag |
| AP-APR-001 | Â§5.1 | â‰¤â‚¹1L â†’ Auto-approve |
| AP-APR-002 | Â§5.2 | â‚¹1Lâ€“â‚¹10L â†’ Route to Dept Head |
| AP-APR-003 | Â§5.3 | â‚¹10Lâ€“â‚¹50L â†’ Route to Finance Controller |
| AP-APR-004 | Â§5.4 | >â‚¹50L â†’ Route to CFO |
| AP-APR-005 | Â§5.5 | Watchlist vendor â†’ Dept Head (overrides Â§5.1) |
| AP-NOT-001 | Â§6.1 | Any deviation â†’ email within 15 min |
| AP-NOT-002 | Â§6.3 | Unresolved deviation 48h â†’ escalate |
| AP-NOT-003 | Â§6.4 | Critical deviation â†’ immediate email to Finance + Audit |
| AP-QR-001 | Â§7.1 | Invoice >â‚¹10L, no QR code â†’ Hold |
| AP-QR-002 | Â§7.2 | QR data mismatch â†’ Flag |
| AP-QR-003 | Â§7.3 | Invalid digital signature â†’ Flag |

### Known Conflicts

| Conflict | Severity | Description |
|----------|----------|-------------|
| CONFLICT-001 | HIGH | Invoice >â‚¹50L AND >10% deviation: Finance Controller (Â§2.2c) vs CFO (Â§5.4) â€” ambiguous approver |
| CONFLICT-002 | MEDIUM | Invoice â‰¤â‚¹1L with 1â€“10% deviation: Auto-approve (Â§5.1) vs Dept Head routing (Â§2.2b) |
| CONFLICT-003 | LOW | Watchlist vendor â‰¤â‚¹1L â€” resolved by priority order (Â§5.5 overrides Â§5.1) |

---

## Rule JSON Structure

Each rule in `extracted_rules.json` looks like this:

```json
{
  "rule_id": "AP-POM-004",
  "category": "PO_MATCH",
  "source_clause": "Section 2.2(c)",
  "description": "Escalate to Finance Controller if Invoice Total exceeds PO Amount by 10% or more",
  "condition": {
    "field": "deviation_pct",
    "op": ">=",
    "value": 10.0
  },
  "action": "ESCALATE_TO_FINANCE_CONTROLLER",
  "action_detail": "Invoice amount exceeds PO by â‰¥10%",
  "requires_justification": true,
  "notification": {
    "type": "email",
    "to": ["finance_controller", "internal_audit"],
    "within_minutes": 15
  },
  "priority": 23,
  "confidence_score": 1.0
}
```

Conditions can be simple (`field op value`) or nested logic trees using `AND` / `OR` / `NOT`.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in what you need:

```
# FREE â€” recommended. Get key at https://console.groq.com
GROQ_API_KEY=gsk_...

# Only needed if using --provider openai
OPENAI_API_KEY=sk-...

# Only needed if using --provider anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Email notifications (optional â€” dry-run by default)
EMAIL_DRY_RUN=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@email.com
SMTP_PASS=your_app_password
```

---

## My Approach & AI Tools Used

### My Contribution (~80%)

I designed and built this solution end-to-end. Here's what I owned entirely:

- **Problem analysis** — Read the AP policy document thoroughly, identified all 7 rule categories, and mapped each clause to a structured IF/THEN logic before writing any code
- **Architecture decisions** — Chose a modular pipeline (parser → extractor → engine → notifier) so each component is independently testable and replaceable
- **Data model design** — Designed the `OperandCondition` / `CompositeCondition` recursive condition tree schema that can represent any boolean logic from the policy
- **Rule engine** — Built the `RuleEngine` from scratch: context flattening, priority-ordered evaluation, terminal action stopping (REJECT/HOLD halts further evaluation), and safe arithmetic expression evaluator (`_SafeEval`) using regex whitelisting to prevent injection
- **Prompt engineering** — Wrote the LLM extraction prompt with strict JSON schema enforcement, field name conventions, and Chain-of-Thought reasoning. Iterated on it to fix edge cases (e.g. `exists` operator with no value, `null` within_minutes)
- **Bug identification and fixing** — Diagnosed and fixed 3 non-obvious bugs: `OperandCondition.value` required for `exists` operator, `CONFLICT_PROMPT` curly brace escaping breaking Python's `.format()`, and `Notification.within_minutes` rejecting `null` from LLM output
- **Test design** — Designed 40 test scenarios covering happy path, GSTIN mismatch, GRN holds, rate mismatches, watchlist vendor overrides, and tax compliance edge cases
- **Integration** — Wired all components together into a 4-command CLI that works offline (demo) or with a live LLM (extract)

### AI Assistance (~20%)

I used **GitHub Copilot (Claude Sonnet 4.6)** as a coding assistant — similar to how a developer uses Stack Overflow or documentation, but faster.

| What I asked AI for | How I used the output |
|--------------------|-----------------------|
| Boilerplate code for Pydantic models | Reviewed, adjusted field types and validators myself |
| Regex patterns for section/clause parsing | Tested against actual policy doc, fixed edge cases for markdown headers |
| SMTP email template HTML | Customised role mappings and trigger conditions |
| pytest test stubs | Rewrote most assertions to match actual engine behaviour |
| Git push troubleshooting | Used guidance to resolve PAT authentication issue |

**LLM for rule extraction (runtime):** The solution uses **Groq** (free — `llama-3.3-70b-versatile`) at runtime to extract rules from any policy document. This is a core feature of the product, not a development tool.

- `temperature=0.1` — for deterministic, reproducible extraction
- Section-by-section prompting — avoids context window limits
- Pydantic validation on every extracted rule — rejects malformed LLM output gracefully

## Sample Output

Running `python -m src.main demo` produces decisions like:

```
Scenario: Over-Invoiced ≥10%: Escalate to Finance Controller
  Invoice  : INV-2026-002
  Status   : → ESCALATE_TO_FINANCE_CONTROLLER
  Triggered Rules (2):
  [!] AP-POM-004   Section 2.2(c)  → ESCALATE_TO_FINANCE_CONTROLLER
       Detail: Invoice amount exceeds PO by ≥10%
       Notify: finance_controller, internal_audit within 15m

Scenario: GSTIN Mismatch: Compliance Rejection
  Invoice  : INV-2026-003
  Status   : ⛔ COMPLIANCE_HOLD
  Triggered Rules (1):
  [·] AP-TAX-001   Section 4.1     → REJECT
       Detail: GSTIN Mismatch — Update Vendor Master or Verify Invoice

Scenario: Happy Path: Clean Invoice (auto-approve)
  Invoice  : INV-2026-001
  Status   : ✓ AUTO_APPROVE
```

