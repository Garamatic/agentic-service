"""Workflow engine for event-driven email automation.

Maps incoming RabbitMQ events to email send operations via a template registry.
Each workflow defines how to extract fields from an event and render an email.
"""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import structlog

from .service import EmailQueueResult, GaramaticService

logger = structlog.get_logger()


@dataclass(frozen=True)
class EmailTemplate:
    """Template for rendering workflow emails."""
    from_email: str
    from_name: str
    subject: Callable[[dict[str, Any]], str]
    body_html: Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class Workflow:
    """Defines how an event type becomes an email."""
    # Email template to render
    template: EmailTemplate
    # Extract recipient email from event; return None to skip
    get_recipient: Callable[[dict[str, Any]], str | None]
    # Optional enrichment: async fn to add fields to event before rendering
    enrich: (
        Callable[[dict[str, Any], GaramaticService], Coroutine[Any, Any, dict[str, Any]]]
        | None
    ) = None


# ------------------------------------------------------------------
# Field extraction helper
# ------------------------------------------------------------------

def _field(event: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Extract a field from an event with multiple key fallbacks."""
    for key in keys:
        if key in event:
            return event[key]
    return default


# ------------------------------------------------------------------
# Template renderers (regular functions, not lambdas, for readability)
# ------------------------------------------------------------------

def _payment_subject(e: dict[str, Any]) -> str:
    inv = _field(e, "invoice_id", "InvoiceId", "OdooInvoiceId", default="N/A")
    return f"Payment Received - Invoice #{inv}"


def _payment_body(e: dict[str, Any]) -> str:
    inv = _field(e, "invoice_id", "InvoiceId", "OdooInvoiceId", default="N/A")
    amt = _field(e, "amount", "Amount", default=0)
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    return (
        f"<h2>Thank you for your payment!</h2>\n"
        f"<p>We've received payment of €{amt:.2f} for invoice #{inv}.</p>\n"
        f"<p>Your ticket #{tid} has been closed.</p>\n"
        f"<p>Best regards,<br>Garamatic Team</p>"
    )


def _invoice_created_subject(e: dict[str, Any]) -> str:
    inv = _field(e, "invoice_id", "InvoiceId", "OdooInvoiceId", default="N/A")
    amt = _field(e, "amount", "Amount", default=0)
    return f"New Invoice #{inv} - €{amt:.2f}"


def _invoice_created_body(e: dict[str, Any]) -> str:
    inv = _field(e, "invoice_id", "InvoiceId", "OdooInvoiceId", default="N/A")
    amt = _field(e, "amount", "Amount", default=0)
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    return (
        f"<h2>New Invoice Available</h2>\n"
        f"<p>An invoice for €{amt:.2f} has been generated for ticket #{tid}.</p>\n"
        f"<p>Invoice #{inv}</p>\n"
        f"<p>Please review and process payment at your earliest convenience.</p>\n"
        f"<p>Best regards,<br>Garamatic Team</p>"
    )


def _invoice_overdue_subject(e: dict[str, Any]) -> str:
    inv = _field(e, "invoice_id", "InvoiceId", "OdooInvoiceId", default="N/A")
    return f"Overdue Invoice #{inv}"


def _invoice_overdue_body(e: dict[str, Any]) -> str:
    inv = _field(e, "invoice_id", "InvoiceId", "OdooInvoiceId", default="N/A")
    amt = _field(e, "amount", "Amount", default=0)
    days = _field(e, "days_overdue", "DaysOverdue", default=0)
    day_word = "day" if days == 1 else "days"
    return (
        f"<h2>Invoice Overdue</h2>\n"
        f"<p>Invoice #{inv} for €{amt:.2f} is now {days} {day_word} overdue.</p>\n"
        f"<p>Please process payment at your earliest convenience.</p>"
    )


def _ticket_resolved_subject(e: dict[str, Any]) -> str:
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    return f"Ticket #{tid} Resolved"


def _ticket_resolved_body(e: dict[str, Any]) -> str:
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    name = _field(e, "customer_name", "CustomerName", default="there")
    notes = _field(
        e, "resolution_notes", "ResolutionNotes",
        default="Your issue has been resolved.",
    )
    return (
        f"<h2>Your ticket has been resolved</h2>\n"
        f"<p>Hi {name},</p>\n"
        f"<p>Ticket #{tid} has been resolved.</p>\n"
        f"<p><strong>Resolution:</strong> {notes}</p>\n"
        f"<p>If you have any further questions, please reply to this email.</p>"
    )


def _ticket_created_subject(e: dict[str, Any]) -> str:
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    title = _field(e, "title", "Title", default="Untitled")
    return f"Ticket #{tid} Created - {title}"


def _ticket_created_body(e: dict[str, Any]) -> str:
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    name = _field(e, "customer_name", "CustomerName", default="Customer")
    title = _field(e, "title", "Title", default="Untitled")
    return (
        f"<h2>Your Support Ticket</h2>\n"
        f"<p>Hi {name},</p>\n"
        f"<p>We've received your support request and created ticket #{tid}.</p>\n"
        f"<p><strong>Title:</strong> {title}</p>\n"
        f"<p>Our team will review and respond within 24 hours.</p>\n"
        f"<p>Best regards,<br>Garamatic Support Team</p>"
    )


def _ticket_escalated_subject(e: dict[str, Any]) -> str:
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    return f"URGENT: Ticket #{tid} Escalated"


def _ticket_escalated_body(e: dict[str, Any]) -> str:
    tid = _field(e, "ticket_id", "TicketId", default="N/A")
    reason = _field(e, "reason", "Reason", default="Unknown")
    cid = _field(e, "customerId", "customer_id", default="N/A")
    title = _field(e, "title", "Title", default="Untitled")
    return (
        f"<h2>Ticket Escalation Alert</h2>\n"
        f"<p>Ticket #{tid} has been escalated.</p>\n"
        f"<p><strong>Reason:</strong> {reason}</p>\n"
        f"<p><strong>Customer ID:</strong> {cid}</p>\n"
        f"<p><strong>Title:</strong> {title}</p>\n"
        f"<p>Please review immediately.</p>"
    )


# ------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------

_payment_received_template = EmailTemplate(
    from_email="billing@garamatic.com",
    from_name="Garamatic Billing",
    subject=_payment_subject,
    body_html=_payment_body,
)

_invoice_created_template = EmailTemplate(
    from_email="billing@garamatic.com",
    from_name="Garamatic Billing",
    subject=_invoice_created_subject,
    body_html=_invoice_created_body,
)

_invoice_overdue_template = EmailTemplate(
    from_email="billing@garamatic.com",
    from_name="Garamatic Billing",
    subject=_invoice_overdue_subject,
    body_html=_invoice_overdue_body,
)

_ticket_resolved_template = EmailTemplate(
    from_email="support@garamatic.com",
    from_name="Garamatic Support",
    subject=_ticket_resolved_subject,
    body_html=_ticket_resolved_body,
)

_ticket_created_template = EmailTemplate(
    from_email="support@garamatic.com",
    from_name="Garamatic Support",
    subject=_ticket_created_subject,
    body_html=_ticket_created_body,
)

_ticket_escalated_template = EmailTemplate(
    from_email="system@garamatic.com",
    from_name="Garamatic System",
    subject=_ticket_escalated_subject,
    body_html=_ticket_escalated_body,
)


# ------------------------------------------------------------------
# Enrichment
# ------------------------------------------------------------------

async def _enrich_ticket(event: dict[str, Any], service: GaramaticService) -> dict[str, Any]:
    """Enrich escalation event with ticket details from ticket-masala."""
    ticket_id = _field(event, "ticket_id", "TicketId")
    if not ticket_id or ticket_id == "N/A":
        return event
    try:
        ticket = await service.get_ticket(ticket_id)
        enriched = dict(event)
        enriched["customerId"] = ticket.customer_email
        enriched["title"] = ticket.title
        return enriched
    except Exception as e:
        logger.warning("workflow.enrich_failed", ticket_id=ticket_id, error=str(e))
        return event


# ------------------------------------------------------------------
# Default workflow registry
# ------------------------------------------------------------------

DEFAULT_WORKFLOWS: dict[str, Workflow] = {
    "payment.received": Workflow(
        template=_payment_received_template,
        get_recipient=lambda e: _field(e, "customer_email", "CustomerEmail"),
    ),
    "invoice.created": Workflow(
        template=_invoice_created_template,
        get_recipient=lambda e: _field(e, "customer_email", "CustomerEmail"),
    ),
    "invoice.overdue": Workflow(
        template=_invoice_overdue_template,
        get_recipient=lambda e: _field(e, "customer_email", "CustomerEmail"),
    ),
    "ticket.resolved": Workflow(
        template=_ticket_resolved_template,
        get_recipient=lambda e: _field(e, "customer_email", "CustomerEmail"),
    ),
    "ticket.created": Workflow(
        template=_ticket_created_template,
        get_recipient=lambda e: _field(e, "customer_email", "CustomerEmail"),
    ),
    "ticket.escalated": Workflow(
        template=_ticket_escalated_template,
        get_recipient=lambda e: "manager@garamatic.com",  # TODO: configurable
        enrich=_enrich_ticket,
    ),
}


# ------------------------------------------------------------------
# WorkflowEngine
# ------------------------------------------------------------------

class WorkflowEngine:
    """Executes event-to-email workflows via a template registry."""

    def __init__(
        self,
        service: GaramaticService,
        workflows: dict[str, Workflow] | None = None,
    ) -> None:
        self._service = service
        self._workflows = workflows or DEFAULT_WORKFLOWS

    async def handle(self, routing_key: str, event: dict[str, Any]) -> EmailQueueResult | None:
        """Execute the workflow for a given event.

        Returns the email queue result, or None if no workflow matched
        or the event was skipped (e.g. no recipient email).
        """
        workflow = self._workflows.get(routing_key)
        if workflow is None:
            logger.warning("workflow.unhandled_routing_key", key=routing_key)
            return None

        recipient = workflow.get_recipient(event)
        if not recipient:
            logger.warning("workflow.no_recipient", routing_key=routing_key)
            return None

        # Enrich event if workflow requires ticket lookup etc.
        if workflow.enrich is not None:
            event = await workflow.enrich(event, self._service)

        template = workflow.template
        result = await self._service.send_email(
            to_email=recipient,
            subject=template.subject(event),
            body=template.body_html(event),
            from_name=template.from_name,
            from_email=template.from_email,
        )

        logger.info(
            "workflow.email_sent",
            routing_key=routing_key,
            to=recipient,
            status=result.status,
        )
        return result
