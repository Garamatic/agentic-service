"""Customer tools — registered once, exposed via MCP and LangGraph adapters."""

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


class GetCustomerContextSchema(BaseModel):
    """Schema for get_customer_context tool."""

    customer_email: str = Field(..., description="Customer email to look up")


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------


async def _get_customer_context(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    customer_email = kwargs["customer_email"]
    try:
        context = await service.get_customer_context(customer_email)
    except ValidationError as e:
        return {
            "status": "validation_error",
            "error": str(e),
            "customer_email": customer_email,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to retrieve customer context: {e}",
            "customer_email": customer_email,
        }

    return {
        "status": "success",
        "customer_email": context.customer_email,
        "summary": {
            "total_tickets": context.summary.total_tickets,
            "open_tickets": context.summary.open_tickets,
            "total_invoiced": context.summary.total_invoiced,
            "outstanding_balance": context.summary.outstanding_balance,
            "lifetime_value": context.summary.lifetime_value,
        },
        "recent_tickets": [
            {"id": t.id, "title": t.title, "status": t.status, "created_at": t.created_at}
            for t in context.recent_tickets
        ],
        "recent_invoices": [
            {
                "invoice_id": i.invoice_id,
                "amount": i.amount,
                "status": i.status,
                "created_at": i.created_at,
            }
            for i in context.recent_invoices
        ],
    }


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def register_tools() -> None:
    registry = get_registry()
    registry.register(
        ToolDefinition(
            name="get_customer_context",
            schema=GetCustomerContextSchema,
            handler=_get_customer_context,
            description="Get full customer context across all services.",
            group="customer",
            annotations=ToolAnnotations(
                title="Get customer context",
                readOnlyHint=True,
                idempotentHint=True,
            ),
        )
    )


# Auto-register on import
register_tools()
