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


class PostInvoiceSchema(BaseModel):
    """Schema for post_invoice tool."""

    invoice_id: str = Field(..., description="The Odoo invoice ID to post")


class GetInvoicePdfSchema(BaseModel):
    """Schema for get_invoice_pdf tool."""

    invoice_id: str = Field(..., description="The Odoo invoice ID")


class RegisterPaymentSchema(BaseModel):
    """Schema for register_payment tool."""

    invoice_id: str = Field(..., description="The Odoo invoice ID")
    amount: float = Field(..., description="Payment amount in EUR")


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


async def _post_invoice(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    invoice_id = kwargs["invoice_id"]
    try:
        result = await service.post_invoice(invoice_id)
    except (ValidationError, ValueError) as e:
        return {"status": "validation_error", "error": str(e), "invoice_id": invoice_id}
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to post invoice: {e}",
            "invoice_id": invoice_id,
        }

    return {
        "status": "success",
        "invoice_id": invoice_id,
        "message": result.get("status", "posted"),
    }


async def _get_invoice_pdf(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    invoice_id = kwargs["invoice_id"]
    try:
        pdf = await service.get_invoice_pdf(invoice_id)
    except (ValidationError, ValueError) as e:
        return {"status": "validation_error", "error": str(e), "invoice_id": invoice_id}
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to get invoice PDF: {e}",
            "invoice_id": invoice_id,
        }

    if pdf is None:
        return {
            "status": "not_found",
            "error": "PDF not found for this invoice",
            "invoice_id": invoice_id,
        }

    # Return base64-encoded PDF for MCP transport
    import base64

    return {
        "status": "success",
        "invoice_id": invoice_id,
        "pdf_base64": base64.b64encode(pdf).decode("ascii"),
        "mime_type": "application/pdf",
    }


async def _register_payment(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    invoice_id = kwargs["invoice_id"]
    amount = kwargs["amount"]
    try:
        result = await service.register_payment(invoice_id, amount)
    except (ValidationError, ValueError) as e:
        return {"status": "validation_error", "error": str(e), "invoice_id": invoice_id}
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to register payment: {e}",
            "invoice_id": invoice_id,
        }

    return {
        "status": "success",
        "invoice_id": invoice_id,
        "amount": amount,
        "message": result.get("status", "payment_registered"),
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
    registry.register(
        ToolDefinition(
            name="post_invoice",
            schema=PostInvoiceSchema,
            handler=_post_invoice,
            description="Post (confirm) an invoice in Odoo.",
            group="invoice",
            annotations=ToolAnnotations(
                title="Post invoice",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="get_invoice_pdf",
            schema=GetInvoicePdfSchema,
            handler=_get_invoice_pdf,
            description="Get the PDF of an invoice.",
            group="invoice",
            annotations=ToolAnnotations(
                title="Get invoice PDF",
                readOnlyHint=True,
                idempotentHint=True,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="register_payment",
            schema=RegisterPaymentSchema,
            handler=_register_payment,
            description="Register a payment for an invoice in Odoo.",
            group="invoice",
            annotations=ToolAnnotations(
                title="Register payment",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
            ),
        )
    )


# Auto-register on import
register_tools()
