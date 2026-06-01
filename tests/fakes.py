"""Shared stateful fakes for the GaramaticService ports.

These satisfy the same Protocols as the production adapters
(RabbitMQPublisher, TicketClient, OdooBridgeClient) and the new
EmailPublisher port. The MagicMock-based fixtures in conftest.py
serve a different assertion style (call-counts, not state) and
deliberately stay separate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx

from agentic_service.notifications import EmailMessage
from agentic_service.service import EmailQueueResult

__all__ = [
    "FakeEmailPublisher",
    "FakeMQ",
    "FakeOdooClient",
    "FakeTicketClient",
]


class FakeTicketClient:
    def __init__(self, tickets: dict | None = None) -> None:
        self.tickets = tickets or {}
        self.closed = False

    async def get_ticket(self, ticket_id: str) -> dict:
        if ticket_id not in self.tickets:
            resp = AsyncMock()
            resp.status_code = 404
            raise httpx.HTTPStatusError("Not found", request=AsyncMock(), response=resp)
        return self.tickets[ticket_id]

    async def list_tickets(
        self,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        return [
            t
            for t in self.tickets.values()
            if (customer_id is None or t.get("customerId") == customer_id)
            and (status is None or t.get("status") == status)
        ][offset : offset + limit]

    async def create_ticket(
        self,
        title: str,
        description: str,
        customer_id: str,
        **kwargs: object,
    ) -> dict:
        new_id = f"T-{len(self.tickets) + 1}"
        ticket: dict = {
            "id": new_id,
            "title": title,
            "description": description,
            "customerId": customer_id,
            **kwargs,
            "status": "open",
        }
        self.tickets[new_id] = ticket
        return ticket

    async def resolve_ticket(
        self,
        ticket_id: str,
        resolution_notes: str | None = None,
        billable_amount: float | None = None,
    ) -> dict:
        ticket = self.tickets.get(ticket_id, {})
        ticket["status"] = "resolved"
        ticket["resolutionNotes"] = resolution_notes
        ticket["billableAmount"] = billable_amount
        return ticket

    async def close(self) -> None:
        self.closed = True


class FakeOdooClient:
    def __init__(self, invoices: dict | None = None) -> None:
        self.invoices = invoices or {}
        self.closed = False

    async def get_invoice_status(self, ticket_id: str) -> dict | None:
        return self.invoices.get(ticket_id)

    async def list_customer_invoices(self, customer_email: str) -> list[dict]:
        return [
            inv for inv in self.invoices.values() if inv.get("customer_email") == customer_email
        ]

    async def close(self) -> None:
        self.closed = True


class FakeMQ:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.closed = False

    async def publish(self, routing_key: str, message: dict) -> None:
        self.published.append((routing_key, message))

    async def close(self) -> None:
        self.closed = True


class FakeEmailPublisher:
    """Records every EmailMessage handed to send_email_message.

    Satisfies the EmailPublisher port (the seam the NotificationComposer
    depends on). Use `fail=True` to simulate a transport outage.
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[EmailMessage] = []

    async def send_email_message(self, email: EmailMessage) -> EmailQueueResult:
        if self.fail:
            raise RuntimeError("MQ is down")
        self.calls.append(email)
        return EmailQueueResult(
            status="queued",
            to_email=email.to_email,
            subject=email.subject,
            message="queued in memory",
        )
