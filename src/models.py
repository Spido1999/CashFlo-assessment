"""
Pydantic data models for AP Policy Rule Engine.
All rule structures, invoice models, and evaluation results are defined here.
"""

from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, model_validator
from enum import Enum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class RuleCategory(str, Enum):
    VALIDATION = "VALIDATION"       # Section 1: Basic invoice validation
    PO_MATCH = "PO_MATCH"           # Section 2: PO matching
    LINE_ITEM = "LINE_ITEM"         # Section 2.3: Line-item matching
    GRN_MATCH = "GRN_MATCH"         # Section 3: GRN matching
    TAX = "TAX"                     # Section 4: Tax & compliance
    APPROVAL = "APPROVAL"           # Section 5: Approval matrix
    NOTIFICATION = "NOTIFICATION"   # Section 6: Deviation notifications
    QR_DIGITAL = "QR_DIGITAL"       # Section 7: QR / digital signature


class RuleAction(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    REJECT = "REJECT"
    HOLD = "HOLD"
    FLAG = "FLAG"
    ROUTE_TO_AP_CLERK = "ROUTE_TO_AP_CLERK"
    ROUTE_TO_AP_MANAGER = "ROUTE_TO_AP_MANAGER"
    ROUTE_TO_DEPT_HEAD = "ROUTE_TO_DEPT_HEAD"
    ROUTE_TO_PROCUREMENT = "ROUTE_TO_PROCUREMENT"
    ESCALATE_TO_FINANCE_CONTROLLER = "ESCALATE_TO_FINANCE_CONTROLLER"
    ROUTE_TO_FINANCE_CONTROLLER = "ROUTE_TO_FINANCE_CONTROLLER"
    ROUTE_TO_CFO = "ROUTE_TO_CFO"
    COMPLIANCE_HOLD = "COMPLIANCE_HOLD"
    SEND_EMAIL = "SEND_EMAIL"
    SEND_IMMEDIATE_EMAIL = "SEND_IMMEDIATE_EMAIL"
    ESCALATE_TO_NEXT_LEVEL = "ESCALATE_TO_NEXT_LEVEL"


class ConflictSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------------------
# Condition Models
# ---------------------------------------------------------------------------

class OperandCondition(BaseModel):
    """Leaf-level condition comparing a field to a value."""
    field: str = Field(..., description="Invoice field name (e.g. 'invoice_total')")
    op: str = Field(..., description="Operator: >, <, >=, <=, ==, !=, in, not_in, is_null, is_not_null, exists")
    value: Any = Field(default=None, description="Comparison value; can be a literal, field ref, or expression string. Omit for exists/is_null/is_not_null operators.")
    description: Optional[str] = None


class CompositeCondition(BaseModel):
    """Compound condition combining child conditions with AND / OR / NOT."""
    operator: Literal["AND", "OR", "NOT"] = Field(..., description="Logical operator")
    operands: List[Union[CompositeCondition, OperandCondition]] = Field(
        ..., description="Child conditions (min 2 for AND/OR, exactly 1 for NOT)"
    )
    description: Optional[str] = None

    @model_validator(mode="after")
    def check_operand_count(self) -> "CompositeCondition":
        if self.operator == "NOT" and len(self.operands) != 1:
            raise ValueError("NOT operator requires exactly 1 operand")
        if self.operator in ("AND", "OR") and len(self.operands) < 2:
            raise ValueError(f"{self.operator} operator requires at least 2 operands")
        return self


Condition = Union[CompositeCondition, OperandCondition]


# ---------------------------------------------------------------------------
# Notification Model
# ---------------------------------------------------------------------------

class Notification(BaseModel):
    type: str = Field(default="email", description="Notification channel (email, SMS, etc.)")
    to: List[str] = Field(..., description="Recipient roles/addresses")
    within_minutes: int = Field(default=15, description="SLA for delivery in minutes")
    subject_template: Optional[str] = None
    include_fields: Optional[List[str]] = Field(
        default=["invoice_number", "vendor_name", "po_number",
                 "deviation_type", "deviation_details", "recommended_action"],
        description="Fields to include in notification body"
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_within_minutes(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("within_minutes") is None:
            data = {**data, "within_minutes": 15}
        return data


# ---------------------------------------------------------------------------
# Core Rule Model
# ---------------------------------------------------------------------------

class Rule(BaseModel):
    rule_id: str = Field(..., description="Unique rule identifier, e.g. AP-VAL-001")
    category: RuleCategory
    source_clause: str = Field(..., description="Source section/clause in the policy doc")
    description: str = Field(..., description="Human-readable description of the rule")
    condition: Condition = Field(..., description="Structured condition tree")
    action: RuleAction = Field(..., description="Primary action when condition is True")
    action_detail: Optional[str] = Field(None, description="Reason/status text to attach to the action")
    requires_justification: bool = Field(default=False)
    notification: Optional[Notification] = None
    exceptions: Optional[List[str]] = Field(None, description="Rule IDs or notes describing exceptions")
    cross_references: Optional[List[str]] = Field(None, description="Referenced clauses (e.g. 'Section 2.3(b)')")
    priority: int = Field(default=100, description="Lower number = higher priority (evaluated first)")
    confidence_score: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="LLM confidence in this extraction (1.0 = certain)"
    )
    conflict_flags: Optional[List[str]] = Field(None, description="Conflicting rule IDs detected")


# ---------------------------------------------------------------------------
# Rule Set (top-level container)
# ---------------------------------------------------------------------------

class RuleSet(BaseModel):
    version: str = "1.0"
    source_document: str
    extraction_date: str
    total_rules: int = 0
    rules: List[Rule]
    conflicts: Optional[List["ConflictReport"]] = None

    @model_validator(mode="after")
    def set_total(self) -> "RuleSet":
        self.total_rules = len(self.rules)
        return self


# ---------------------------------------------------------------------------
# Conflict Detection Models
# ---------------------------------------------------------------------------

class ConflictReport(BaseModel):
    conflict_id: str
    severity: ConflictSeverity
    rule_ids: List[str] = Field(..., description="IDs of rules involved in the conflict")
    source_clauses: List[str]
    description: str
    resolution_suggestion: Optional[str] = None


# ---------------------------------------------------------------------------
# Invoice Models (for rule execution)
# ---------------------------------------------------------------------------

class LineItem(BaseModel):
    line_id: str
    description: Optional[str] = None
    invoice_qty: float
    po_qty: float
    grn_qty: Optional[float] = None
    invoice_unit_rate: float
    po_unit_rate: float
    taxable_amount: float


class Invoice(BaseModel):
    # Identifiers
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None       # ISO date string YYYY-MM-DD
    vendor_gstin: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_pan: Optional[str] = None
    po_number: Optional[str] = None

    # Amounts
    invoice_total: Optional[float] = None
    taxable_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    cgst_amount: float = 0.0
    sgst_amount: float = 0.0
    igst_amount: float = 0.0

    # PO / GRN fields (pre-fetched from system)
    po_amount: Optional[float] = None
    po_active: bool = True
    po_type: str = "goods"              # "goods" or "service"
    grn_exists: bool = False
    grn_date: Optional[str] = None      # ISO date string

    # Flags
    is_handwritten: bool = False
    duplicate_exists: bool = False
    vendor_on_watchlist: bool = False
    vendor_master_gstin: Optional[str] = None

    # Tax / compliance
    supply_type: Optional[str] = None   # "intra_state" or "inter_state"
    place_of_supply_state: Optional[str] = None
    buyer_gstin_state: Optional[str] = None

    # QR / digital signature
    qr_code_present: bool = False
    qr_invoice_number: Optional[str] = None
    qr_vendor_gstin: Optional[str] = None
    digital_signature_present: bool = False
    digital_signature_valid: bool = True

    # Line items
    line_items: List[LineItem] = []

    # Deviation tracking (set post-evaluation)
    deviation_type: Optional[str] = None
    deviation_details: Optional[str] = None
    hours_since_detection: float = 0.0


# ---------------------------------------------------------------------------
# Evaluation Result Models
# ---------------------------------------------------------------------------

class RuleResult(BaseModel):
    rule_id: str
    source_clause: str
    description: str
    triggered: bool = Field(..., description="True if condition evaluated to True")
    action: Optional[str] = None
    action_detail: Optional[str] = None
    requires_justification: bool = False
    notification: Optional[Notification] = None
    error: Optional[str] = Field(None, description="Set if evaluation itself raised an error")


class EvaluationReport(BaseModel):
    invoice_number: Optional[str]
    evaluation_timestamp: str
    final_status: str = Field(..., description="Overall invoice status after all rules")
    triggered_rules: List[RuleResult]
    non_triggered_rules: List[str] = Field(default_factory=list, description="Rule IDs that did not fire")
    errors: List[str] = Field(default_factory=list)
    notifications_to_send: List[Dict[str, Any]] = Field(default_factory=list)
