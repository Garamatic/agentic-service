"""Customer intelligence — aggregating tickets and invoices."""

from __future__ import annotations

import contextlib
from typing import Any

import structlog

from ..ports import OdooClientPort, TicketClientPort
from .base import (
    BaseService,
    CustomerContext,
    CustomerSummary,
    _invoice_summary_from_dict,
    _ticket_summary_from_dict,
    validate_email,
)

logger = structlog.get_logger()


class CustomerContextService(BaseService):
    """Aggregates customer data across all services."""

    def __init__(
        self,
        ticket_client: TicketClientPort,
        odoo_client: OdooClientPort | None,
    ) -> None:
        self._ticket = ticket_client
        self._odoo = odoo_client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        for client in (self._ticket, self._odoo):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()

    # ------------------------------------------------------------------
    # Customer intelligence
    # ------------------------------------------------------------------

    async def get_customer_context(self, customer_email: str) -> CustomerContext:
        validate_email(customer_email)

        async def _fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            tickets_data = await self._ticket.list_tickets(customer_id=customer_email, limit=50)
            invoices_data: list[dict[str, Any]] = []
            if self._odoo is not None:
                try:
                    invoices_data = await self._odoo.list_customer_invoices(customer_email)
                except Exception as exc:
                    logger.warning(
                        "service.get_customer_context.odoo_unavailable",
                        error=str(exc),
                        email=customer_email,
                    )
            return tickets_data, invoices_data

        tickets_data, invoices_data = await self._call_port(
            "get_customer_context",
            {"email": customer_email},
            _fetch(),
        )

        def _amount(ticket: dict[str, Any]) -> float:
            val = ticket.get("billableAmount")
            if val is not None:
                return float(val)
            return float(ticket.get("billable_amount") or 0)

        total_ticket_value = sum(
            _amount(t) for t in tickets_data if t.get("status") == "resolved"
        )
        outstanding = sum(
            (inv.get("amount") or 0) for inv in invoices_data if inv.get("status") != "paid"
        )

        return CustomerContext(
            customer_email=customer_email,
            summary=CustomerSummary(
                total_tickets=len(tickets_data),
                open_tickets=sum(1 for t in tickets_data if t.get("status") == "open"),
                total_invoiced=sum((inv.get("amount") or 0) for inv in invoices_data),
                outstanding_balance=outstanding,
                lifetime_value=total_ticket_value,
            ),
            recent_tickets=[_ticket_summary_from_dict(t) for t in tickets_data[:3]],
            recent_invoices=[_invoice_summary_from_dict(inv) for inv in invoices_data[:3]],
        )
