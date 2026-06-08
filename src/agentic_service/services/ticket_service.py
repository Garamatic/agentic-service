"""Ticket operations — search, create, resolve."""

from __future__ import annotations

import contextlib
from typing import Any, Literal

import structlog

from ..ports import TicketClientPort
from .base import (
    BaseService,
    Ticket,
    TicketSummary,
    ValidationError,
    _ticket_from_dict,
    _ticket_summary_from_dict,
    validate_email,
)

logger = structlog.get_logger()


class TicketService(BaseService):
    """All ticket-related business logic."""

    def __init__(self, ticket_client: TicketClientPort) -> None:
        self._ticket = ticket_client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._ticket is not None:
            with contextlib.suppress(Exception):
                await self._ticket.close()

    # ------------------------------------------------------------------
    # Ticket operations
    # ------------------------------------------------------------------

    async def get_ticket(self, ticket_id: str) -> Ticket:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        data = await self._call_port(
            "get_ticket",
            {"ticket_id": ticket_id},
            self._ticket.get_ticket(ticket_id),
        )
        return _ticket_from_dict(data)

    async def list_tickets(
        self,
        *,
        customer_email: str | None = None,
        status: Literal["open", "resolved", "closed"] | None = None,
        limit: int = 10,
    ) -> list[TicketSummary]:
        if status is not None and status not in {"open", "resolved", "closed"}:
            raise ValidationError(
                f"Invalid status filter: {status!r}. Must be one of: open, resolved, closed."
            )
        data = await self._call_port(
            "list_tickets",
            {"email": customer_email, "status": status, "limit": limit},
            self._ticket.list_tickets(
                customer_id=customer_email,
                status=status,
                limit=limit,
            ),
        )
        return [_ticket_summary_from_dict(d) for d in data]

    async def create_ticket(
        self,
        *,
        title: str,
        description: str,
        customer_email: str,
        customer_name: str,
        priority: Literal["low", "medium", "high", "urgent"] = "medium",
    ) -> Ticket:
        validate_email(customer_email)
        if not title or not title.strip():
            raise ValidationError("Title is required")
        if priority not in {"low", "medium", "high", "urgent"}:
            raise ValidationError(
                f"Invalid priority: {priority!r}. Must be one of: low, medium, high, urgent."
            )
        data = await self._call_port(
            "create_ticket",
            {"title": title, "customer": customer_email},
            self._ticket.create_ticket(
                title=title,
                description=description,
                customer_id=customer_email,
                customer_name=customer_name,
                priority=priority,
            ),
        )
        return _ticket_from_dict(data)

    async def resolve_ticket(
        self,
        *,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None,
    ) -> dict[str, Any]:
        """Resolve a ticket. Returns raw ticket data (caller maps to domain)."""
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        if not resolution_notes or not resolution_notes.strip():
            raise ValidationError("Resolution notes are required")
        if billable_amount is not None and billable_amount < 0:
            raise ValidationError("Billable amount cannot be negative")

        data = await self._call_port(
            "resolve_ticket",
            {"ticket_id": ticket_id, "billable": billable_amount},
            self._ticket.resolve_ticket(
                ticket_id=ticket_id,
                resolution_notes=resolution_notes,
                billable_amount=billable_amount,
            ),
        )
        return data
