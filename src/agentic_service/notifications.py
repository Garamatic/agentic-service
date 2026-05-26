"""Deepened notification composer module.

Hides event field normalization, template registry, enrichment orchestration,
sender identity selection, and graceful degradation behind a single boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger()

# ------------------------------------------------------------------
# Result types
# ------------------------------------------------------------------

NotificationStatus = Literal["queued", "skipped", "failed", "unhandled"]
SkipReason = Literal["unknown_event", "missing_recipient"]
FailureReason = Literal["delivery_unavailable", "render_failed", "enrich_failed", "invalid_message"]


@dataclass(frozen=True)
class EmailMessage:
    to_email: str
    subject: str
    body_html: str
    sender: SenderIdentity
    routing_key: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailSendResult:
    status: Literal["queued", "failed"]
    to_email: str
    subject: str
    message: str


@dataclass(frozen=True)
class NotificationResult:
    status: NotificationStatus
    routing_key: str
    reason: SkipReason | FailureReason | None = None
    email: EmailMessage | None = None
    send_result: EmailSendResult | None = None
    message: str = ""

    @property
    def sent(self) -> bool:
        return self.status == "queued"


@dataclass(frozen=True)
class SenderIdentity:
    email: str
    name: str


@dataclass(frozen=True)
class TicketSnapshot:
    id: str
    customer_email: str | None
    customer_name: str | None
    title: str


# ------------------------------------------------------------------
# Ports
# ------------------------------------------------------------------

@runtime_checkable
class EmailSender(Protocol):
    async def send_email(self, message: EmailMessage) -> EmailSendResult: ...


@runtime_checkable
class TicketLookup(Protocol):
    async def get_ticket(self, ticket_id: str) -> TicketSnapshot | None: ...


@runtime_checkable
class SenderDirectory(Protocol):
    def for_routing_key(self, routing_key: str) -> SenderIdentity: ...


# ------------------------------------------------------------------
# EventView — normalized read facade over raw events
# ------------------------------------------------------------------

class EventView:
    """Read-normalized view over raw event payloads.

    Resolves snake_case, PascalCase, camelCase, and registered aliases
    automatically. Replaces the scattered `_field()` helper.
    """

    _DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
        "ticket_id": ("ticket_id", "TicketId", "ticketId"),
        "invoice_id": ("invoice_id", "InvoiceId", "invoiceId", "OdooInvoiceId"),
        "customer_email": ("customer_email", "CustomerEmail", "customerId", "customer_id"),
        "customer_name": ("customer_name", "CustomerName", "customerName"),
        "amount": ("amount", "Amount"),
        "days_overdue": ("days_overdue", "DaysOverdue", "daysOverdue"),
        "resolution_notes": ("resolution_notes", "ResolutionNotes", "resolutionNotes"),
        "title": ("title", "Title"),
        "description": ("description", "Description"),
        "reason": ("reason", "Reason"),
        "priority": ("priority", "Priority"),
        "paid_at": ("paid_at", "PaidAt", "paidAt"),
        "created_at": ("created_at", "CreatedAt", "createdAt"),
    }

    def __init__(
        self,
        event: Mapping[str, Any],
        aliases: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._event = dict(event)
        self._aliases = {**(aliases or {}), **self._DEFAULT_ALIASES}

    def get(self, name: str, default: Any = None) -> Any:
        """Resolve a canonical field name via registered aliases."""
        for key in self._aliases.get(name, (name,)):
            if key in self._event:
                return self._event[key]
        return default

    def str(self, name: str, default: str | None = None) -> str | None:
        val = self.get(name, default)
        return str(val) if val is not None else default

    def money(self, name: str, default: float = 0.0) -> float:
        val = self.get(name, default)
        try:
            return float(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    def merged(self, extra: Mapping[str, Any]) -> EventView:
        """Return a new EventView with extra fields merged in."""
        combined = {**self._event, **extra}
        return EventView(combined, self._aliases)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._event)


# ------------------------------------------------------------------
# Template types
# ------------------------------------------------------------------

RecipientSelector = Callable[[EventView], str | None]
TemplateRenderer = Callable[[EventView], tuple[str, str]]  # subject, body_html
Enricher = Callable[[EventView, TicketLookup], Awaitable[EventView]]


@dataclass(frozen=True)
class NotificationTemplate:
    routing_key: str
    recipient: RecipientSelector
    render: TemplateRenderer
    sender_key: str = "default"
    enrich: Enricher | None = None


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

class NotificationRegistry:
    """Template registry and lookup boundary."""

    def __init__(self, templates: list[NotificationTemplate]) -> None:
        self._templates: dict[str, NotificationTemplate] = {}
        for t in templates:
            self._templates[t.routing_key] = t

    def get(self, routing_key: str) -> NotificationTemplate | None:
        return self._templates.get(routing_key)

    @classmethod
    def defaults(cls) -> NotificationRegistry:
        return cls(_default_templates())


# ------------------------------------------------------------------
# Default templates (converted from old workflow renderers)
# ------------------------------------------------------------------

def _payment_received_template(e: EventView) -> tuple[str, str]:
    inv = e.str("invoice_id", "N/A")
    amt = e.money("amount")
    tid = e.str("ticket_id", "N/A")
    subject = f"Payment Received - Invoice #{inv}"
    body = (
        f"<h2>Thank you for your payment!</h2>\n"
        f"<p>We've received payment of €{amt:.2f} for invoice #{inv}.</p>\n"
        f"<p>Your ticket #{tid} has been closed.</p>\n"
        f"<p>Best regards,<br>Garamatic Team</p>"
    )
    return subject, body


def _invoice_created_template(e: EventView) -> tuple[str, str]:
    inv = e.str("invoice_id", "N/A")
    amt = e.money("amount")
    tid = e.str("ticket_id", "N/A")
    subject = f"New Invoice #{inv} - €{amt:.2f}"
    body = (
        f"<h2>New Invoice Available</h2>\n"
        f"<p>An invoice for €{amt:.2f} has been generated for ticket #{tid}.</p>\n"
        f"<p>Invoice #{inv}</p>\n"
        f"<p>Please review and process payment at your earliest convenience.</p>\n"
        f"<p>Best regards,<br>Garamatic Team</p>"
    )
    return subject, body


def _invoice_overdue_template(e: EventView) -> tuple[str, str]:
    inv = e.str("invoice_id", "N/A")
    amt = e.money("amount")
    days = int(e.get("days_overdue", 0))
    day_word = "day" if days == 1 else "days"
    subject = f"Overdue Invoice #{inv}"
    body = (
        f"<h2>Invoice Overdue</h2>\n"
        f"<p>Invoice #{inv} for €{amt:.2f} is now {days} {day_word} overdue.</p>\n"
        f"<p>Please process payment at your earliest convenience.</p>"
    )
    return subject, body


def _ticket_resolved_template(e: EventView) -> tuple[str, str]:
    tid = e.str("ticket_id", "N/A")
    name = e.str("customer_name", "there")
    notes = e.str("resolution_notes", "Your issue has been resolved.")
    subject = f"Ticket #{tid} Resolved"
    body = (
        f"<h2>Your ticket has been resolved</h2>\n"
        f"<p>Hi {name},</p>\n"
        f"<p>Ticket #{tid} has been resolved.</p>\n"
        f"<p><strong>Resolution:</strong> {notes}</p>\n"
        f"<p>If you have any further questions, please reply to this email.</p>"
    )
    return subject, body


def _ticket_created_template(e: EventView) -> tuple[str, str]:
    tid = e.str("ticket_id", "N/A")
    title = e.str("title", "Untitled")
    name = e.str("customer_name", "Customer")
    subject = f"Ticket #{tid} Created - {title}"
    body = (
        f"<h2>Your Support Ticket</h2>\n"
        f"<p>Hi {name},</p>\n"
        f"<p>We've received your support request and created ticket #{tid}.</p>\n"
        f"<p><strong>Title:</strong> {title}</p>\n"
        f"<p>Our team will review and respond within 24 hours.</p>\n"
        f"<p>Best regards,<br>Garamatic Support Team</p>"
    )
    return subject, body


def _ticket_escalated_template(e: EventView) -> tuple[str, str]:
    tid = e.str("ticket_id", "N/A")
    reason = e.str("reason", "Unknown")
    cid = e.str("customer_id") or e.str("customerId", "N/A")
    title = e.str("title", "Untitled")
    subject = f"URGENT: Ticket #{tid} Escalated"
    body = (
        f"<h2>Ticket Escalation Alert</h2>\n"
        f"<p>Ticket #{tid} has been escalated.</p>\n"
        f"<p><strong>Reason:</strong> {reason}</p>\n"
        f"<p><strong>Customer ID:</strong> {cid}</p>\n"
        f"<p><strong>Title:</strong> {title}</p>\n"
        f"<p>Please review immediately.</p>"
    )
    return subject, body


async def _enrich_ticket_escalation(
    event: EventView,
    lookup: TicketLookup,
) -> EventView:
    """Enrich escalation event with ticket details from ticket-masala."""
    ticket_id = event.str("ticket_id")
    if not ticket_id or ticket_id == "N/A":
        return event
    try:
        ticket = await lookup.get_ticket(ticket_id)
        if ticket is not None:
            return event.merged({
                "customerId": ticket.customer_email,
                "customer_name": ticket.customer_name,
                "title": ticket.title,
            })
    except Exception as exc:
        logger.warning("notification.enrich_failed", ticket_id=ticket_id, error=str(exc))
    return event


def _default_templates() -> list[NotificationTemplate]:
    return [
        NotificationTemplate(
            routing_key="payment.received",
            recipient=lambda e: e.str("customer_email"),
            sender_key="billing",
            render=_payment_received_template,
        ),
        NotificationTemplate(
            routing_key="invoice.created",
            recipient=lambda e: e.str("customer_email"),
            sender_key="billing",
            render=_invoice_created_template,
        ),
        NotificationTemplate(
            routing_key="invoice.overdue",
            recipient=lambda e: e.str("customer_email"),
            sender_key="billing",
            render=_invoice_overdue_template,
        ),
        NotificationTemplate(
            routing_key="ticket.resolved",
            recipient=lambda e: e.str("customer_email"),
            sender_key="support",
            render=_ticket_resolved_template,
        ),
        NotificationTemplate(
            routing_key="ticket.created",
            recipient=lambda e: e.str("customer_email"),
            sender_key="support",
            render=_ticket_created_template,
        ),
        NotificationTemplate(
            routing_key="ticket.escalated",
            recipient=lambda _e: "manager@garamatic.com",
            sender_key="system",
            enrich=_enrich_ticket_escalation,
            render=_ticket_escalated_template,
        ),
    ]


# ------------------------------------------------------------------
# Static sender directory
# ------------------------------------------------------------------

class StaticSenderDirectory:
    """Maps sender keys to configured identities."""

    _KEY_MAP: dict[str, str] = {
        "payment.received": "billing",
        "invoice.created": "billing",
        "invoice.overdue": "billing",
        "ticket.resolved": "support",
        "ticket.created": "support",
        "ticket.escalated": "system",
    }

    def __init__(self, identities: dict[str, SenderIdentity] | None = None) -> None:
        self._identities = identities or {
            "billing": SenderIdentity("billing@garamatic.com", "Garamatic Billing"),
            "support": SenderIdentity("support@garamatic.com", "Garamatic Support"),
            "system": SenderIdentity("system@garamatic.com", "Garamatic System"),
            "default": SenderIdentity("support@garamatic.com", "Garamatic Support"),
        }

    def for_routing_key(self, routing_key: str) -> SenderIdentity:
        sender_key = self._KEY_MAP.get(routing_key, "default")
        return (
            self._identities.get(sender_key)
            or self._identities.get("default")
            or next(iter(self._identities.values()))
        )


# ------------------------------------------------------------------
# Composer
# ------------------------------------------------------------------

class NotificationComposer:
    """Single entry point for event-to-email notifications.

    Hides normalization, template lookup, rendering, enrichment,
    sender selection, and graceful degradation.
    """

    def __init__(
        self,
        *,
        email_sender: EmailSender,
        registry: NotificationRegistry | None = None,
        sender_directory: SenderDirectory | None = None,
        ticket_lookup: TicketLookup | None = None,
    ) -> None:
        self._email_sender = email_sender
        self._registry = registry or NotificationRegistry.defaults()
        self._sender_directory = sender_directory or StaticSenderDirectory()
        self._ticket_lookup = ticket_lookup

    async def notify(
        self,
        routing_key: str,
        event: Mapping[str, Any],
    ) -> NotificationResult:
        """Process an event into a notification.

        Returns a structured result describing what happened.
        """
        template = self._registry.get(routing_key)
        if template is None:
            logger.warning("notification.unhandled", routing_key=routing_key)
            return NotificationResult(
                status="unhandled",
                routing_key=routing_key,
                reason="unknown_event",
                message=f"No template registered for routing key: {routing_key}",
            )

        view = EventView(event)

        # Enrichment
        if template.enrich is not None and self._ticket_lookup is not None:
            try:
                view = await template.enrich(view, self._ticket_lookup)
            except Exception as exc:
                logger.warning(
                    "notification.enrich_failed",
                    routing_key=routing_key,
                    error=str(exc),
                )
                # Degrade gracefully: continue with original event

        # Recipient
        recipient = template.recipient(view)
        if not recipient:
            logger.warning("notification.no_recipient", routing_key=routing_key)
            return NotificationResult(
                status="skipped",
                routing_key=routing_key,
                reason="missing_recipient",
                message="No recipient resolved from event",
            )

        # Render
        try:
            subject, body_html = template.render(view)
        except Exception as exc:
            logger.error("notification.render_failed", routing_key=routing_key, error=str(exc))
            return NotificationResult(
                status="failed",
                routing_key=routing_key,
                reason="render_failed",
                message=f"Template rendering failed: {exc}",
            )

        # Sender
        sender = self._sender_directory.for_routing_key(routing_key)

        email = EmailMessage(
            to_email=recipient,
            subject=subject,
            body_html=body_html,
            sender=sender,
            routing_key=routing_key,
        )

        # Delivery
        send_result: EmailSendResult | None = None
        failed_message = ""
        try:
            send_result = await self._email_sender.send_email(email)
        except Exception as exc:
            logger.error(
                "notification.delivery_failed",
                routing_key=routing_key,
                recipient=recipient,
                error=str(exc),
            )
            failed_message = f"Email delivery failed: {exc}"

        if send_result is None or send_result.status == "failed":
            return NotificationResult(
                status="failed",
                routing_key=routing_key,
                reason="delivery_unavailable",
                email=email,
                send_result=send_result,
                message=failed_message or (send_result.message if send_result else "Email delivery failed"),
            )

        logger.info(
            "notification.queued",
            routing_key=routing_key,
            recipient=recipient,
            subject=subject,
        )
        return NotificationResult(
            status="queued",
            routing_key=routing_key,
            email=email,
            send_result=send_result,
            message=send_result.message,
        )
