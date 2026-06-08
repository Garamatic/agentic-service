"""System tools — registered once, exposed via MCP and LangGraph adapters."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ..config import get_settings
from ..service import GaramaticService
from .registry import get_registry
from .types import ToolDefinition

# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------


class RequestHumanApprovalSchema(BaseModel):
    """Schema for request_human_approval tool."""

    action: str = Field(..., description="Action description")
    details: str = Field(..., description="Details of the action")
    estimated_amount: float | None = Field(
        default=None, description="Estimated monetary amount"
    )


class ApproveRequestSchema(BaseModel):
    """Schema for approve_request tool."""

    approval_id: str = Field(..., description="Approval ID to approve")


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------


async def _request_human_approval(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    action = kwargs["action"]
    _ = kwargs["details"]
    estimated_amount = kwargs.get("estimated_amount")
    approval_store = service.approval_store

    if estimated_amount is not None and estimated_amount > 0:
        threshold = get_settings().approval_threshold
        if estimated_amount > threshold:
            if approval_store is not None:
                approval_id = await approval_store.create_request(action, estimated_amount)
            else:
                from ..approval import generate_approval_id
                approval_id = generate_approval_id()

            return {
                "status": "approval_required",
                "approval_id": approval_id,
                "action": action,
                "estimated_amount": estimated_amount,
                "message": (
                    f"Approval required for '{action}' (€{estimated_amount:.2f}).\n"
                    f"Approval ID: {approval_id}\n"
                    f"To approve, run: approve_request(approval_id='{approval_id}')\n"
                    f"Then retry resolve_ticket with approval_id='{approval_id}'."
                ),
            }

    return {
        "status": "success",
        "action": action,
        "estimated_amount": estimated_amount,
        "message": f"Action '{action}' approved automatically (below threshold).",
    }


async def _approve_request(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    approval_id = kwargs["approval_id"]
    approval_store = service.approval_store
    if approval_store is None:
        return {"status": "error", "error": "Approval store is not available"}
    try:
        success = await approval_store.approve(approval_id)
    except Exception as e:
        return {"status": "error", "error": str(e)}
    if not success:
        return {
            "status": "error",
            "error": f"Approval request '{approval_id}' not found or already processed.",
        }
    return {
        "status": "success",
        "approval_id": approval_id,
        "message": f"Approval request '{approval_id}' has been approved.",
    }


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def register_tools() -> None:
    registry = get_registry()
    registry.register(
        ToolDefinition(
            name="request_human_approval",
            schema=RequestHumanApprovalSchema,
            handler=_request_human_approval,
            description="Request human approval for sensitive actions like large invoices.",
            group="system",
            annotations=ToolAnnotations(
                title="Request human approval",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="approve_request",
            schema=ApproveRequestSchema,
            handler=_approve_request,
            description="Approve a pending approval request by ID.",
            group="system",
            annotations=ToolAnnotations(
                title="Approve request",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
            ),
        )
    )


# Auto-register on import
register_tools()
