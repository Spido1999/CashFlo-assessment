"""
LLM prompt templates for AP Policy rule extraction.

All prompts are designed for OpenAI-compatible chat APIs.
They follow a structured Chain-of-Thought approach:
  1. Identify rule type & source clause
  2. Decompose into IF / THEN / ELSE
  3. Map to structured JSON schema
  4. Self-evaluate confidence
"""

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are an expert Accounts Payable (AP) automation engineer specialising in
converting policy documents into machine-executable rule sets.

Your job is to read a section of an AP policy document and extract every
business rule it contains as a structured JSON object.

## Output Schema (strict)
Each rule must follow this exact schema:

{
  "rule_id": "<string>  e.g. AP-VAL-001",
  "category": "<one of: VALIDATION | PO_MATCH | LINE_ITEM | GRN_MATCH | TAX | APPROVAL | NOTIFICATION | QR_DIGITAL>",
  "source_clause": "<string>  e.g. Section 2.2(c)",
  "description": "<concise human-readable description>",
  "condition": <Condition>,
  "action": "<RuleAction>",
  "action_detail": "<optional string for flag/reject reason text>",
  "requires_justification": <boolean>,
  "notification": <Notification | null>,
  "exceptions": [<rule_id or note>, ...] | null,
  "cross_references": ["Section X.Y(z)", ...] | null,
  "priority": <integer  lower = higher priority>,
  "confidence_score": <float 0.0-1.0>
}

## Condition Schema
A Condition is either:

OperandCondition:
{
  "field": "<invoice field name>",
  "op": "<one of: >, <, >=, <=, ==, !=, in, not_in, is_null, is_not_null, exists>",
  "value": <literal | field_name | expression_string>
}

CompositeCondition:
{
  "operator": "<AND | OR | NOT>",
  "operands": [<Condition>, ...]
}

## RuleAction Enum
AUTO_APPROVE | REJECT | HOLD | FLAG |
ROUTE_TO_AP_CLERK | ROUTE_TO_AP_MANAGER | ROUTE_TO_DEPT_HEAD |
ROUTE_TO_PROCUREMENT | ESCALATE_TO_FINANCE_CONTROLLER |
ROUTE_TO_FINANCE_CONTROLLER | ROUTE_TO_CFO | COMPLIANCE_HOLD |
SEND_EMAIL | SEND_IMMEDIATE_EMAIL | ESCALATE_TO_NEXT_LEVEL

## Notification Schema
{
  "type": "email",
  "to": ["<role>", ...],
  "within_minutes": <integer>,
  "subject_template": "<optional>",
  "include_fields": ["invoice_number", "vendor_name", "po_number", ...]
}

## Field Name Conventions (invoice context)
invoice_number, invoice_date, vendor_gstin, vendor_name, vendor_pan,
po_number, invoice_total, taxable_amount, tax_amount,
cgst_amount, sgst_amount, igst_amount,
po_amount, po_active, po_type, grn_exists, grn_date,
is_handwritten, duplicate_exists, vendor_on_watchlist, vendor_master_gstin,
supply_type, place_of_supply_state, buyer_gstin_state,
qr_code_present, qr_invoice_number, qr_vendor_gstin,
digital_signature_present, digital_signature_valid,
line_items[*].invoice_qty, line_items[*].po_qty, line_items[*].grn_qty,
line_items[*].invoice_unit_rate, line_items[*].po_unit_rate,
deviation_pct, under_invoiced_pct, all_validations_passed,
hours_since_detection, deviation_detected

## Rules
1. Extract EVERY rule from the given text, including edge cases and exceptions.
2. Every condition must use the field names above.
3. Use expression strings for arithmetic comparisons:
   e.g. value: "po_amount * 1.10"  or  "po_amount * 0.95"
4. Percentage deviations: compute deviation_pct = (invoice_total - po_amount) / po_amount * 100
5. Assign priority: validations (10-19), PO match (20-29), GRN (30-39), tax (40-49),
   approval (50-59), notification (60-69), QR (70-79).
6. Return a JSON array of rule objects — no extra prose, no markdown fences.
""".strip()


# ---------------------------------------------------------------------------
# User Prompt Template
# ---------------------------------------------------------------------------

USER_PROMPT_TEMPLATE = """
Extract all business rules from the following AP policy section.
Return a valid JSON array of rule objects. No markdown, no explanations — only JSON.

### Policy Section Text ###
{section_text}

### Context ###
Document: {doc_name}
Section: {section_id} — {section_title}
Previous rule IDs already assigned (do not reuse): {existing_ids}

Think step by step:
1. Identify each distinct rule (IF/THEN logic) in the text.
2. Map each condition to the schema using the field conventions.
3. Assign the correct action enum value.
4. Set confidence_score based on how clearly the rule is stated (0.9+ = explicit threshold, 0.6-0.89 = implicit, <0.6 = ambiguous).

Output (JSON array only):
""".strip()


# ---------------------------------------------------------------------------
# Conflict Detection Prompt
# ---------------------------------------------------------------------------

CONFLICT_PROMPT = """
You are an AP policy analyst. Below is a list of extracted business rules in JSON.
Identify any conflicts, ambiguities, or overlapping conditions.

A conflict occurs when:
- Two rules apply to the same condition but prescribe different actions.
- One rule's threshold range overlaps with another's.
- An exception in one rule contradicts another rule.

For each conflict found, output a JSON object:
{{
  "conflict_id": "CONFLICT-001",
  "severity": "<LOW | MEDIUM | HIGH>",
  "rule_ids": ["AP-XXX-001", "AP-YYY-002"],
  "source_clauses": ["Section X.Y", "Section A.B"],
  "description": "<what the conflict is>",
  "resolution_suggestion": "<how it could be resolved>"
}}

Return a JSON array. If no conflicts, return an empty array [].

### Rules ###
{rules_json}
""".strip()
