"""Production and test adapters for the notification composer.

Adapters bridge the narrow EmailSender / TicketLookup ports to
concrete transport implementations (RabbitMQ, HTTP) and in-memory
fakes for testing.
"""

from __future__ import annotations

from typing import Any

import structlog

from .notifications import (
    EmailMessage,
    EmailSendResult,
    EmailSender,
    SenderIdentity,
    TicketLookup,
    TicketSnapshot,
)
from .ports import MessageQueuePort, TicketClientPort

logger = structlog.get_logger()


# ------------------------------------------------------------------
# Production adapters
# ------------------------------------------------------------------

class MessageQueueEmailSender:
    """Publishes email.send events to RabbitMQ via the MessageQueuePort."""

    def __init__(self, mq: MessageQueuePort | None) -> None:
        self._mq = mq

    async def send_email(self, message: EmailMessage) -> EmailSendResult:
        if self._mq is None:
            return EmailSendResult(
                status="failed",
                to_email=message.to_email,
                subject=message.subject,
                message="Email service unavailable: MQ not initialized",
            )

        payload = {
            "event_type": "email.send",
            "to_email": message.to_email,
            "from_email": message.sender.email,
            "from_name": message.sender.name,
            "subject": message.subject,
            "body_html": message.body_html,
        }

        try:
            await self._mq.publish("email.send", payload)
            return EmailSendResult(
                status="queued",
                to_email=message.to_email,
                subject=message.subject,
                message="Email queued for sending. Will be delivered within 1 minute.",
            )
        except Exception as exc:
            logger.error(
                "mq_email_sender.publish_failed",
                to=message.to_email,
                error=str(exc),
            )
            return EmailSendResult(
                status="failed",
                to_email=message.to_email,
                subject=message.subject,
                message=f"Failed to queue email: {exc}",
            )


class HttpTicketLookup:
    """Wraps the ticket client port to provide TicketSnapshot."""

    def __init__(self, client: TicketClientPort) -> None:
        self._client = client

    async def get_ticket(self, ticket_id: str) -> TicketSnapshot | None:
        try:
            data = await self._client.get_ticket(ticket_id)
            return TicketSnapshot(
                id=data.get("id") or "N/A",
                customer_email=data.get("customerId") or data.get("customer_id"),
                customer_name=data.get("customerName") or data.get("customer_name"),
                title=data.get("title") or "Untitled",
            )
        except Exception as exc:
            logger.warning(
                "http_ticket_lookup.failed",
                ticket_id=ticket_id,
                error=str(exc),
            )
            return None


# ------------------------------------------------------------------
# Compatibility adapters (for incremental migration)
# ------------------------------------------------------------------

class ServiceEmailSender:
    """Temporary bridge from GaramaticService.send_email() to EmailSender port.

    Use this during migration; prefer MessageQueueEmailSender once the
    service layer no longer needs to own email sending.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    async def send_email(self, message: EmailMessage) -> EmailSendResult:
        try:
            result = await self._service.send_email(
                to_email=message.to_email,
                subject=message.subject,
                body=message.body_html,
                from_name=message.sender.name,
                from_email=message.sender.email,
            )
            return EmailSendResult(
                status=result.status,
                to_email=result.to_email,
                subject=result.subject,
                message=result.message,
            )
        except Exception as exc:
            return EmailSendResult(
                status="failed",
                to_email=message.to_email,
                subject=message.subject,
                message=f"Service send_email failed: {exc}",
            )


class ServiceTicketLookup:
    """Temporary bridge from GaramaticService.get_ticket() to TicketLookup port."""

    def __init__(self, service: Any) -> None:
        self._service = service

    async def get_ticket(self, ticket_id: str) -> TicketSnapshot | None:
        try:
            ticket = await self._service.get_ticket(ticket_id)
            return TicketSnapshot(
                id=ticket.id,
                customer_email=ticket.customer_email,
                customer_name=ticket.customer_name,
                title=ticket.title,
            )
        except Exception as exc:
            logger.warning(
                "service_ticket_lookup.failed",
                ticket_id=ticket_id,
                error=str(exc),
            )
            return None


# ------------------------------------------------------------------
# Test adapters
# ------------------------------------------------------------------

class InMemoryEmailSender:
    """Records sent email requests for testing."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[EmailMessage] = []

    async def send_email(self, message: EmailMessage) -> EmailSendResult:
        if self.fail:
            return EmailSendResult(
                status="failed",
                to_email=message.to_email,
                subject=message.subject,
                message="simulated failure",
            )
        self.sent.append(message)
        return EmailSendResult(
            status="queued",
            to_email=message.to_email,
            subject=message.subject,
            message="queued in memory",
        )


class InMemoryTicketLookup:
    """In-memory ticket lookup for testing enrichment paths."""

    def __init__(self, tickets: dict[str, TicketSnapshot] | None = None) -> None:
        self.tickets = tickets or {}
        self.calls: list[str] = []

    async def get_ticket(self, ticket_id: str) -> TicketSnapshot | None:
        self.calls.append(ticket_id)
        return self.tickets.get(ticket_id)
