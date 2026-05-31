"""Agent toolkit — domain tools exposed to LLM agents.

The service layer owns the agent-facing interface. The agent module
consumes a toolkit from its service, becoming a thin orchestration adapter.
"""

from __future__ import annotations

import contextlib
from typing import Any

from langchain_core.tools import StructuredTool

from .service import GaramaticService
from .service import generate_request_id


class AgentToolkit:
    """Domain tools exposed to LLM agents, backed by a GaramaticService.

    This is the seam between the business-logic service and the agent
    orchestration layer. One adapter = LangGraph tools. Another could be
    MCP tools or OpenAI function definitions.
    """

    def __init__(self, service: GaramaticService) -> None:
        self._service = service

    def get_tools(self) -> list[StructuredTool]:
        """Return all tools as LangGraph StructuredTool instances."""
        return [
            StructuredTool.from_function(
                coroutine=self.search_tickets_by_customer,
                name="search_tickets_by_customer",
                description="Search for tickets by customer email address.",
            ),
            StructuredTool.from_function(
                coroutine=self.get_ticket_details,
                name="get_ticket_details",
                description="Get detailed information about a specific ticket.",
            ),
            StructuredTool.from_function(
                coroutine=self.get_customer_summary,
                name="get_customer_summary",
                description="Get comprehensive customer context including tickets and invoices.",
            ),
            StructuredTool.from_function(
                coroutine=self.resolve_ticket_with_invoice,
                name="resolve_ticket_with_invoice",
                description="Resolve a ticket AND create an invoice in one action.",
            ),
            StructuredTool.from_function(
                coroutine=self.request_human_approval,
                name="request_human_approval",
                description="Request human approval for sensitive actions.",
            ),
        ]

    async def search_tickets_by_customer(self, customer_email: str, limit: int = 10) -> str:
        """Search for tickets by customer email address."""
        try:
            tickets = await self._service.list_tickets(customer_email=customer_email, limit=limit)
            if not tickets:
                return f"No tickets found for {customer_email}"

            ticket_word = "ticket" if len(tickets) == 1 else "tickets"
            result = f"Found {len(tickets)} {ticket_word} for {customer_email}:\n\n"
            for t in tickets:
                result += f"- Ticket {t.id}: {t.title} [{t.status}]\n"
            return result
        except Exception as e:
            return f"Error searching tickets: {str(e)}"

    async def get_ticket_details(self, ticket_id: str) -> str:
        """Get detailed information about a specific ticket."""
        if not ticket_id or not ticket_id.strip():
            return "Error: Ticket ID is required"

        try:
            ticket = await self._service.get_ticket(ticket_id)
        except Exception as e:
            return f"Error getting ticket: {str(e)}"

        # Invoice lookup is best-effort; don't fail the whole tool if Odoo is down
        invoice = None
        with contextlib.suppress(Exception):
            invoice = await self._service.get_invoice_status(ticket_id)

        result = f"""Ticket {ticket_id}:
- Title: {ticket.title}
- Status: {ticket.status}
- Customer: {ticket.customer_name or 'Unknown'} ({ticket.customer_email or 'Unknown'})
- Created: {ticket.created_at or 'N/A'}
- Description: {ticket.description or 'No description'}
- Resolution Notes: {ticket.resolution_notes or 'Not resolved'}
"""
        if invoice and invoice.status != "not_found":
            amount = invoice.amount or 0
            result += (
                f"\nInvoice:\n- Status: {invoice.status}\n"
                f"- Amount: €{amount:.2f}\n"
            )

        return result

    async def get_customer_summary(self, customer_email: str) -> str:
        """Get comprehensive customer context including tickets and invoices."""
        try:
            context = await self._service.get_customer_context(customer_email)

            result = f"""Customer {customer_email} Summary:
- Total Tickets: {context.summary.total_tickets}
- Open Tickets: {context.summary.open_tickets}
- Lifetime Value: €{context.summary.lifetime_value:.2f}
- Outstanding Balance: €{context.summary.outstanding_balance:.2f}
"""
            if context.recent_tickets:
                result += "\nRecent Tickets:\n"
                for t in context.recent_tickets:
                    result += f"- {t.title} [{t.status}]\n"

            return result
        except Exception as e:
            return f"Error getting customer context: {str(e)}"

    async def resolve_ticket_with_invoice(
        self,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None,
    ) -> str:
        """Resolve a ticket AND create an invoice in one action.

        Only use when explicitly requested or when work is clearly billable.
        """
        if not ticket_id or not ticket_id.strip():
            return "Error: Ticket ID is required"
        if not resolution_notes or not resolution_notes.strip():
            return "Error: Resolution notes are required"
        if billable_amount is not None and billable_amount < 0:
            return "Error: Billable amount cannot be negative"

        try:
            result = await self._service.resolve_ticket(
                ticket_id=ticket_id,
                resolution_notes=resolution_notes,
                billable_amount=billable_amount,
            )
        except Exception as e:
            return f"❌ Error resolving ticket: {e}"

        if result.invoice and result.invoice.status == "queued":
            return (
                f"✅ Ticket {ticket_id} resolved and invoice "
                f"(€{result.invoice.estimated_amount:.2f}) queued."
            )
        if result.invoice and result.invoice.status == "unavailable":
            return (
                f"✅ Ticket {ticket_id} resolved. "
                f"Invoice service unavailable — invoice not queued."
            )
        return f"✅ Ticket {ticket_id} resolved."

    async def request_human_approval(
        self,
        action: str,
        details: str,
        estimated_amount: float | None = None,
    ) -> str:
        """Request human approval for sensitive actions like large invoices.

        Use when amount > €500 or when uncertain about action.
        """
        approval_id = generate_request_id()

        if estimated_amount is not None and estimated_amount > 500:
            return f"""⚠️ APPROVAL REQUIRED

Action: {action}
Details: {details}
Amount: €{estimated_amount:.2f}

Approval ID: {approval_id}

This action requires human approval due to high value.
Please confirm: "approve {approval_id}" or "reject {approval_id}"
"""
        else:
            return f"Action '{action}' approved automatically (below threshold)."
