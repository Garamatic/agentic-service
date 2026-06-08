"""Invoice tools — registered once, exposed via MCP and LangGraph adapters."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..service import GaramaticService, ValidationError
from .registry import get_registry
from .types import ToolDefinition

# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------


class CreateInvoiceSchema(BaseModel):
    """Schema for create_invoice tool."""

    ticket_id: str = Field(..., description="Associated ticket ID")
    amount: float = Field(..., description="Invoice amount in EUR")
    description: str | None = Field(
        default=None, description="Line item description (defaults to ticket description)"
    )


class GetInvoiceStatusSchema(BaseModel):
    """Schema for get_invoice_status tool."""

    ticket_id: str = Field(..., description="The ticket to check")


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------


async def _create_invoice(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    try:
        result = await service.queue_invoice(
            ticket_id=kwargs["ticket_id"],
            amount=kwargs["amount"],
            description=kwargs.get("description"),
        )
    except (ValidationError, ValueError) as e:
        return {
            "status": "validation_error",
            "error": str(e),
            "ticket_id": kwargs["ticket_id"],
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to queue invoice: {e}",
            "ticket_id": kwargs["ticket_id"],
        }

    return {
        "status": "success",
        "ticket_id": kwargs["ticket_id"],
        "amount": kwargs["amount"],
        "message": result.message,
    }


async def _get_invoice_status(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    ticket_id = kwargs["ticket_id"]
    try:
        invoice = await service.get_invoice_status(ticket_id)
    except (ValidationError, ValueError) as e:
        return {"status": "validation_error", "error": str(e), "ticket_id": ticket_id}
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to check invoice status: {e}",
            "ticket_id": ticket_id,
        }

    if invoice.status == "not_found":
        return {
            "status": "not_found",
            "error": "No invoice found for this ticket",
            "ticket_id": ticket_id,
        }

    return {
        "status": "success",
        "ticket_id": ticket_id,
        "invoice_id": invoice.invoice_id,
        "status_detail": invoice.status,
        "amount": invoice.amount,
        "created_at": invoice.created_at,
        "paid_at": invoice.paid_at,
    }


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def register_tools() -> None:
    registry = get_registry()
    registry.register(
        ToolDefinition(
            name="create_invoice",
            schema=CreateInvoiceSchema,
            handler=_create_invoice,
            description="Create an invoice for a ticket via the Odoo integration.",
            group="invoice",
            annotations=ToolAnnotations(
                title="Create invoice",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="get_invoice_status",
            schema=GetInvoiceStatusSchema,
            handler=_get_invoice_status,
            description="Check invoice status for a ticket.",
            group="invoice",
            annotations=ToolAnnotations(
                title="Get invoice status",
                readOnlyHint=True,
                idempotentHint=True,
            ),
        )
    )


# Auto-register on import
register_tools()
