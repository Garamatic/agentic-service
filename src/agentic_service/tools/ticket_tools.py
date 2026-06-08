"""Ticket tools — registered once, exposed via MCP and LangGraph adapters."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..approval import ApprovalRequiredError
from ..service import GaramaticService, NotFoundError, ValidationError, validate_email
from .registry import get_registry
from .types import ToolDefinition

# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------


class GetTicketStatusSchema(BaseModel):
    """Schema for get_ticket_status tool."""

    ticket_id: str = Field(..., description="The ticket ID (e.g., 'T-123' or '123')")


class ListCustomerTicketsSchema(BaseModel):
    """Schema for list_customer_tickets tool."""

    customer_email: str = Field(..., description="Customer's email address")
    status: str | None = Field(
        default=None, description="Optional filter by status (open, resolved, closed)"
    )
    limit: int = Field(default=10, description="Maximum number of tickets to return")


class CreateTicketSchema(BaseModel):
    """Schema for create_ticket tool."""

    title: str = Field(..., description="Short ticket title/summary")
    description: str = Field(..., description="Detailed description of the issue")
    customer_email: str = Field(..., description="Customer's email for notifications")
    customer_name: str = Field(..., description="Customer's full name")
    priority: str = Field(
        default="medium",
        description="Ticket priority (low, medium, high, urgent)",
    )


class ResolveTicketSchema(BaseModel):
    """Schema for resolve_ticket tool."""

    ticket_id: str = Field(..., description="Ticket to resolve")
    resolution_notes: str = Field(..., description="Summary of what was fixed")
    billable_amount: float | None = Field(
        default=None, description="Amount to invoice (optional, omit for non-billable)"
    )
    approval_id: str | None = Field(
        default=None, description="Approval ID for high-value invoices"
    )


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------


async def _get_ticket_status(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    ticket_id = kwargs["ticket_id"]
    try:
        ticket = await service.get_ticket(ticket_id)
    except NotFoundError:
        return {"status": "not_found", "error": "Ticket not found", "ticket_id": ticket_id}
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to retrieve ticket: {e}",
            "ticket_id": ticket_id,
        }

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "status_detail": ticket.status,
        "customer_id": ticket.customer_email,
        "title": ticket.title,
        "description": ticket.description,
        "created_at": ticket.created_at,
        "resolved_at": ticket.resolved_at,
        "resolution_notes": ticket.resolution_notes,
        "billable_amount": ticket.billable_amount,
    }


async def _list_customer_tickets(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    customer_email = kwargs["customer_email"]
    status = kwargs.get("status")
    limit = kwargs.get("limit", 10)
    try:
        validate_email(customer_email)
    except ValidationError as e:
        return {"status": "validation_error", "error": str(e)}

    try:
        tickets = await service.list_tickets(
            customer_email=customer_email,
            status=status,
            limit=limit,
        )
    except Exception as e:
        return {"status": "error", "error": f"Failed to list tickets: {e}", "tickets": []}

    return {
        "status": "success",
        "tickets": [
            {
                "ticket_id": t.id,
                "title": t.title,
                "status": t.status,
                "created_at": t.created_at,
                "resolved_at": t.resolved_at,
            }
            for t in tickets
        ],
        "count": len(tickets),
        "customer_email": customer_email,
    }


async def _create_ticket(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    try:
        ticket = await service.create_ticket(
            title=kwargs["title"],
            description=kwargs["description"],
            customer_email=kwargs["customer_email"],
            customer_name=kwargs["customer_name"],
            priority=kwargs["priority"],
        )
    except (ValidationError, ValueError) as e:
        return {"status": "validation_error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"Failed to create ticket: {e}"}

    ticket_id = ticket.id
    if not ticket_id or ticket_id == "N/A":
        return {
            "status": "error",
            "error": "Ticket created but no ID returned by upstream API",
        }

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "customer_email": kwargs["customer_email"],
        "priority": kwargs["priority"],
        "message": (
            f"Ticket {ticket_id} created successfully. "
            "Customer will receive confirmation email."
        ),
    }


async def _resolve_ticket(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    ticket_id = kwargs["ticket_id"]
    resolution_notes = kwargs["resolution_notes"]
    billable_amount = kwargs.get("billable_amount")
    approval_id = kwargs.get("approval_id")

    try:
        result = await service.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=resolution_notes,
            billable_amount=billable_amount,
            approval_id=approval_id,
            approval_store=service.approval_store,
        )
    except ValidationError as e:
        return {"status": "validation_error", "error": str(e), "ticket_id": ticket_id}
    except NotFoundError:
        return {"status": "not_found", "error": "Ticket not found", "ticket_id": ticket_id}
    except ApprovalRequiredError as e:
        return {
            "status": "approval_required",
            "error": str(e),
            "approval_id": e.approval_id,
            "ticket_id": ticket_id,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to resolve ticket: {e}",
            "ticket_id": ticket_id,
        }

    response: dict[str, Any] = {
        "status": "success",
        "ticket_id": ticket_id,
        "resolution": "resolved",
        "resolution_notes": resolution_notes,
    }

    if result.invoice is not None:
        response["invoice"] = {
            "status": result.invoice.status,
            "estimated_amount": result.invoice.estimated_amount,
            "message": result.invoice.message,
        }

    return response


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def register_tools() -> None:
    registry = get_registry()
    registry.register(
        ToolDefinition(
            name="get_ticket_status",
            schema=GetTicketStatusSchema,
            handler=_get_ticket_status,
            description="Get the current status of a support ticket.",
            group="ticket",
        )
    )
    registry.register(
        ToolDefinition(
            name="list_customer_tickets",
            schema=ListCustomerTicketsSchema,
            handler=_list_customer_tickets,
            description="List all tickets for a specific customer.",
            group="ticket",
        )
    )
    registry.register(
        ToolDefinition(
            name="create_ticket",
            schema=CreateTicketSchema,
            handler=_create_ticket,
            description="Create a new support ticket.",
            group="ticket",
        )
    )
    registry.register(
        ToolDefinition(
            name="resolve_ticket",
            schema=ResolveTicketSchema,
            handler=_resolve_ticket,
            description="Resolve a ticket and optionally create a billable invoice.",
            group="ticket",
            approval_threshold=1.0,  # Flag: approval check is enabled.
            # Actual threshold from settings.
        )
    )


# Auto-register on import
register_tools()
