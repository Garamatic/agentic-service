"""Agent toolkit — domain tools exposed to LLM agents.

The service layer owns the agent-facing interface. The agent module
consumes a toolkit from its service, becoming a thin orchestration adapter.

All tool methods return structured dictionaries with explicit status codes:
  - "success"             — operation completed
  - "error"               — unexpected failure (caller should retry or escalate)
  - "validation_error"    — bad input (caller should fix the request)
  - "not_found"           — resource does not exist
  - "approved"            — auto-approved (below threshold)
  - "approval_required"   — needs human sign-off
"""

from __future__ import annotations

import contextlib
from typing import Any

from langchain_core.tools import StructuredTool

from .approval import ApprovalRequiredError
from .config import get_settings
from .service import GaramaticService


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
            StructuredTool.from_function(
                coroutine=self.approve_request,
                name="approve_request",
                description="Approve a pending approval request by ID.",
            ),
        ]

    @staticmethod
    def _ok(**fields: Any) -> dict[str, Any]:
        """Build a success result."""
        return {"status": "success", **fields}

    @staticmethod
    def _error(message: str, **fields: Any) -> dict[str, Any]:
        """Build an error result."""
        return {"status": "error", "error": message, **fields}

    @staticmethod
    def _validation_error(message: str) -> dict[str, Any]:
        """Build a validation error result."""
        return {"status": "validation_error", "error": message}

    @staticmethod
    def _not_found(message: str) -> dict[str, Any]:
        """Build a not-found result."""
        return {"status": "not_found", "error": message}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    async def search_tickets_by_customer(
        self, customer_email: str, limit: int = 10
    ) -> dict[str, Any]:
        """Search for tickets by customer email address.

        Returns structured ticket list or error.
        """
        try:
            tickets = await self._service.list_tickets(customer_email=customer_email, limit=limit)
            return self._ok(
                tickets=[{"id": t.id, "title": t.title, "status": t.status} for t in tickets],
                count=len(tickets),
                customer_email=customer_email,
            )
        except Exception as e:
            return self._error(str(e))

    async def get_ticket_details(self, ticket_id: str) -> dict[str, Any]:
        """Get detailed information about a specific ticket.

        Returns ticket fields + optional invoice info.
        """
        if not ticket_id or not ticket_id.strip():
            return self._validation_error("Ticket ID is required")

        try:
            ticket = await self._service.get_ticket(ticket_id)
        except Exception as e:
            return self._error(str(e))

        # Invoice lookup is best-effort; don't fail the whole tool if Odoo is down
        invoice = None
        with contextlib.suppress(Exception):
            invoice = await self._service.get_invoice_status(ticket_id)

        invoice_data = None
        if invoice and invoice.status != "not_found":
            invoice_data = {
                "status": invoice.status,
                "amount": invoice.amount,
                "invoice_id": invoice.invoice_id,
            }

        return self._ok(
            ticket={
                "id": ticket.id,
                "title": ticket.title,
                "status": ticket.status,
                "customer_email": ticket.customer_email,
                "customer_name": ticket.customer_name,
                "description": ticket.description,
                "created_at": ticket.created_at,
                "resolved_at": ticket.resolved_at,
                "resolution_notes": ticket.resolution_notes,
                "billable_amount": ticket.billable_amount,
            },
            invoice=invoice_data,
        )

    async def get_customer_summary(self, customer_email: str) -> dict[str, Any]:
        """Get comprehensive customer context including tickets and invoices.

        Returns aggregated summary + recent activity.
        """
        try:
            context = await self._service.get_customer_context(customer_email)

            return self._ok(
                customer_email=customer_email,
                summary={
                    "total_tickets": context.summary.total_tickets,
                    "open_tickets": context.summary.open_tickets,
                    "lifetime_value": context.summary.lifetime_value,
                    "outstanding_balance": context.summary.outstanding_balance,
                },
                recent_tickets=[
                    {"id": t.id, "title": t.title, "status": t.status, "created_at": t.created_at}
                    for t in context.recent_tickets
                ],
            )
        except Exception as e:
            return self._error(str(e))

    async def resolve_ticket_with_invoice(
        self,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a ticket AND create an invoice in one action.

        Only use when explicitly requested or when work is clearly billable.
        Returns structured resolution result with invoice status.
        For amounts over the approval threshold, pass an approval_id
        obtained from request_human_approval.
        """
        if not ticket_id or not ticket_id.strip():
            return self._validation_error("Ticket ID is required")
        if not resolution_notes or not resolution_notes.strip():
            return self._validation_error("Resolution notes are required")
        if billable_amount is not None and billable_amount < 0:
            return self._validation_error("Billable amount cannot be negative")

        try:
            result = await self._service.resolve_ticket(
                ticket_id=ticket_id,
                resolution_notes=resolution_notes,
                billable_amount=billable_amount,
                approval_id=approval_id,
                approval_store=self._service.approval_store,
            )
        except ApprovalRequiredError as e:
            return {
                "status": "approval_required",
                "ticket_id": ticket_id,
                "estimated_amount": e.amount,
                "error": str(e),
            }
        except Exception as e:
            return self._error(str(e))

        invoice_status = None
        estimated_amount = None
        if result.invoice:
            invoice_status = result.invoice.status
            estimated_amount = result.invoice.estimated_amount

        return self._ok(
            ticket_id=ticket_id,
            resolution="resolved",
            invoice_status=invoice_status,
            estimated_amount=estimated_amount,
            resolution_notes=resolution_notes,
        )

    async def request_human_approval(
        self,
        action: str,
        details: str,
        estimated_amount: float | None = None,
    ) -> dict[str, Any]:
        """Request human approval for sensitive actions like large invoices.

        Creates an approval request in the store and returns an approval ID.
        The ID must be passed to resolve_ticket_with_invoice to proceed.
        """
        approval_store = self._service.approval_store

        if estimated_amount is not None and estimated_amount > 0:
            threshold = get_settings().approval_threshold
            if estimated_amount > threshold:
                if approval_store is not None:
                    approval_id = await approval_store.create_request(action, estimated_amount)
                else:
                    from .approval import generate_approval_id

                    approval_id = generate_approval_id()

                return {
                    "status": "approval_required",
                    "approval_id": approval_id,
                    "action": action,
                    "estimated_amount": estimated_amount,
                    "message": (
                        f"Approval required for '{action}' (€{estimated_amount:.2f}).\n"
                        f"Approval ID: {approval_id}\n"
                        f"To approve, run: approve({approval_id})\n"
                        f"Then retry resolve_ticket_with_invoice with approval_id='{approval_id}'."
                    ),
                }

        return {
            "status": "approved",
            "action": action,
            "estimated_amount": estimated_amount,
            "message": f"Action '{action}' approved automatically (below threshold).",
        }

    async def approve_request(self, approval_id: str) -> dict[str, Any]:
        """Approve a pending approval request by ID.

        Returns success or error if the request does not exist or was already processed.
        """
        approval_store = self._service.approval_store
        if approval_store is None:
            return self._error("Approval store is not available")
        try:
            success = await approval_store.approve(approval_id)
        except Exception as e:
            return self._error(str(e))
        if not success:
            return self._error(
                f"Approval request '{approval_id}' not found or already processed."
            )
        return self._ok(
            approval_id=approval_id,
            status="approved",
            message=f"Approval request '{approval_id}' has been approved.",
        )
