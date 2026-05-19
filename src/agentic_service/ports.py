"""Ports (protocols) for dependency inversion.

The service layer depends only on these protocols, not on concrete clients.
Existing clients in clients.py already satisfy these shapes.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TicketClientPort(Protocol):
    """Port for ticket-masala API operations."""

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]: ...
    async def list_tickets(
        self,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    async def create_ticket(
        self, title: str, description: str, customer_id: str, **kwargs: Any
    ) -> dict[str, Any]: ...
    async def resolve_ticket(
        self,
        ticket_id: str,
        resolution_notes: str | None = None,
        billable_amount: float | None = None,
    ) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@runtime_checkable
class OdooClientPort(Protocol):
    """Port for odoo-bridge API operations."""

    async def get_invoice_status(self, ticket_id: str) -> dict[str, Any] | None: ...
    async def list_customer_invoices(self, customer_email: str) -> list[dict[str, Any]]: ...
    async def close(self) -> None: ...


@runtime_checkable
class MessageQueuePort(Protocol):
    """Port for async message queue publishing."""

    async def publish(self, routing_key: str, message: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...
