"""Production and test adapters for the notification composer.

Adapters bridge the narrow EmailSender / TicketLookup ports to
concrete transport implementations (RabbitMQ, HTTP) and in-memory
fakes for testing.
"""

from __future__ import annotations

import structlog

from .notifications import (
    EmailMessage,
    EmailSendResult,
    TicketSnapshot,
)
from .ports import MessageQueuePort

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
