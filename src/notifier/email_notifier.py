"""
Email Notifier for AP Policy Deviations.

Sends structured email notifications when AP rules fire deviations.
Supports:
  - SMTP (plain, TLS, SSL) via Python's smtplib
  - Dry-run mode (logs emails without sending — default when SMTP not configured)

Email content follows Section 6.2 of the Cashflo AP Policy:
  Invoice Number, Vendor Name, PO Number, Deviation Type,
  Deviation Details (expected vs actual), and Recommended Action.

Configuration via environment variables:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_USE_TLS,
  EMAIL_FROM, EMAIL_CC (comma-separated), EMAIL_DRY_RUN

Usage:
    notifier = EmailNotifier()
    notifier.send_notifications(report, invoice)
"""

from __future__ import annotations

import logging
import os
import smtplib
import textwrap
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from src.models import EvaluationReport, Invoice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SMTP Configuration
# ---------------------------------------------------------------------------

class SMTPConfig:
    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
        self.from_addr = os.getenv("EMAIL_FROM", "ap-automation@cashflo.io")
        self.cc: List[str] = [
            addr.strip()
            for addr in os.getenv("EMAIL_CC", "").split(",")
            if addr.strip()
        ]
        self.dry_run = os.getenv("EMAIL_DRY_RUN", "true").lower() == "true"

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password)


# Stakeholder → email address mapping
# In production, this should be fetched from a user/role directory.
ROLE_EMAIL_MAP: Dict[str, str] = {
    "ap_clerk":             "ap-clerk@cashflo.io",
    "ap_manager":           "ap-manager@cashflo.io",
    "dept_head":            "dept-head@cashflo.io",
    "procurement":          "procurement@cashflo.io",
    "finance_controller":   "finance-controller@cashflo.io",
    "internal_audit":       "internal-audit@cashflo.io",
    "cfo":                  "cfo@cashflo.io",
    "relevant_stakeholder": "ap-team@cashflo.io",
    "next_level_approver":  "approvals@cashflo.io",
}


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

def _build_email_body(
    notification_payload: Dict[str, Any],
    invoice: Invoice,
) -> str:
    """Build a plain-text email body following Section 6.2 format."""
    body_data = notification_payload.get("body", {})
    subject = notification_payload.get("subject", "AP Alert")

    invoice_number = invoice.invoice_number or body_data.get("invoice_number", "N/A")
    vendor_name = invoice.vendor_name or body_data.get("vendor_name", "N/A")
    po_number = invoice.po_number or body_data.get("po_number", "N/A")
    deviation_type = body_data.get("deviation_type", "Deviation")
    deviation_details = body_data.get("deviation_details", "See attached invoice")
    recommended_action = body_data.get("recommended_action") or _default_action(notification_payload)

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    body = textwrap.dedent(f"""
    AP DEVIATION ALERT — {timestamp}
    {'=' * 60}

    Rule Triggered : {notification_payload.get('rule_id', 'N/A')}
    Subject        : {subject}

    INVOICE DETAILS
    ---------------
    Invoice Number : {invoice_number}
    Vendor Name    : {vendor_name}
    PO Number      : {po_number}
    Invoice Amount : INR {invoice.invoice_total:,.2f}

    DEVIATION SUMMARY
    -----------------
    Deviation Type    : {deviation_type}
    Deviation Details : {deviation_details}

    RECOMMENDED ACTION
    ------------------
    {recommended_action}

    {'=' * 60}
    This is an automated notification from Cashflo AP Automation.
    Please do not reply to this email.
    For queries, contact ap-support@cashflo.io
    """).strip()

    return body


def _build_html_body(
    notification_payload: Dict[str, Any],
    invoice: Invoice,
) -> str:
    """Build an HTML email body for richer email clients."""
    plain = _build_email_body(notification_payload, invoice)
    # Simple HTML wrapper
    html_lines = ["<html><body><pre style='font-family:monospace;font-size:13px;'>"]
    html_lines.append(plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html_lines.append("</pre></body></html>")
    return "\n".join(html_lines)


def _default_action(payload: Dict[str, Any]) -> str:
    rule_id = payload.get("rule_id", "")
    if "REJECT" in rule_id or "reject" in str(payload):
        return "Reject the invoice and notify the vendor."
    if "HOLD" in rule_id or "hold" in str(payload):
        return "Place invoice on hold pending resolution."
    return "Review and take appropriate action per policy."


def _resolve_recipients(roles: List[str]) -> List[str]:
    """Map role names to email addresses."""
    addrs: List[str] = []
    for role in roles:
        addr = ROLE_EMAIL_MAP.get(role.lower())
        if addr:
            addrs.append(addr)
        elif "@" in role:
            addrs.append(role)  # Already an email address
        else:
            logger.warning("No email mapping found for role: %r", role)
    return addrs


# ---------------------------------------------------------------------------
# EmailNotifier
# ---------------------------------------------------------------------------

class EmailNotifier:
    """
    Sends (or logs in dry-run mode) email notifications for AP deviations.
    """

    def __init__(self, config: Optional[SMTPConfig] = None) -> None:
        self.config = config or SMTPConfig()

    def send_notifications(
        self,
        report: EvaluationReport,
        invoice: Invoice,
    ) -> List[Dict[str, Any]]:
        """
        Process all notifications in an EvaluationReport.

        Returns a list of send-result dicts with status and details.
        """
        results: List[Dict[str, Any]] = []

        if not report.notifications_to_send:
            logger.info("No notifications to send for invoice %s", report.invoice_number)
            return results

        for payload in report.notifications_to_send:
            result = self._dispatch(payload, invoice)
            results.append(result)

        return results

    def send_single(
        self,
        notification_payload: Dict[str, Any],
        invoice: Invoice,
    ) -> Dict[str, Any]:
        """Send (or log) a single notification payload."""
        return self._dispatch(notification_payload, invoice)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        payload: Dict[str, Any],
        invoice: Invoice,
    ) -> Dict[str, Any]:
        to_roles = payload.get("to", [])
        to_addrs = _resolve_recipients(to_roles)
        cc_addrs = self.config.cc

        if not to_addrs:
            logger.warning("No recipients resolved for payload: %s", payload.get("rule_id"))
            return {"rule_id": payload.get("rule_id"), "status": "SKIPPED", "reason": "No recipients"}

        subject = payload.get("subject", "AP Alert")
        plain_body = _build_email_body(payload, invoice)
        html_body = _build_html_body(payload, invoice)

        if self.config.dry_run or not self.config.is_configured:
            self._log_dry_run(subject, to_addrs, plain_body)
            return {
                "rule_id": payload.get("rule_id"),
                "status": "DRY_RUN",
                "to": to_addrs,
                "subject": subject,
                "preview": plain_body[:300],
            }

        try:
            self._send_smtp(subject, to_addrs, cc_addrs, plain_body, html_body)
            logger.info("Email sent for rule %s → %s", payload.get("rule_id"), to_addrs)
            return {
                "rule_id": payload.get("rule_id"),
                "status": "SENT",
                "to": to_addrs,
                "subject": subject,
            }
        except Exception as exc:
            logger.error("Failed to send email for rule %s: %s",
                         payload.get("rule_id"), exc)
            return {
                "rule_id": payload.get("rule_id"),
                "status": "FAILED",
                "error": str(exc),
                "to": to_addrs,
            }

    def _send_smtp(
        self,
        subject: str,
        to: List[str],
        cc: List[str],
        plain: str,
        html: str,
    ) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.config.from_addr
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)

        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        all_recipients = to + cc

        if self.config.use_tls:
            with smtplib.SMTP(self.config.host, self.config.port) as server:
                server.ehlo()
                server.starttls()
                server.login(self.config.user, self.config.password)
                server.sendmail(self.config.from_addr, all_recipients, msg.as_string())
        else:
            with smtplib.SMTP_SSL(self.config.host, self.config.port) as server:
                server.login(self.config.user, self.config.password)
                server.sendmail(self.config.from_addr, all_recipients, msg.as_string())

    def _log_dry_run(self, subject: str, to: List[str], body: str) -> None:
        logger.info(
            "[DRY RUN] Would send email:\n  Subject: %s\n  To: %s\n  Body (preview): %s",
            subject,
            ", ".join(to),
            body[:200].replace("\n", " "),
        )
