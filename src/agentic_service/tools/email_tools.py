"""Email tools — registered once, exposed via MCP and LangGraph adapters."""

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


class SendEmailSchema(BaseModel):
    """Schema for send_email tool."""

    to_email: str = Field(..., description="Recipient email address")
    subject: str = Field(..., description="Email subject line")
    body: str = Field(..., description="Email body (HTML supported)")
    from_name: str = Field(
        default="Garamatic Support", description="Sender display name"
    )


class DraftEmailSchema(BaseModel):
    """Schema for draft_email tool."""

    to_email: str = Field(..., description="Recipient email address")
    subject: str = Field(..., description="Email subject line")
    key_points: str = Field(..., description="Key points to include in the email")
    tone: str = Field(default="professional", description="Tone of the email (professional, friendly, formal)")


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------


async def _send_email(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    try:
        result = await service.send_email(
            to_email=kwargs["to_email"],
            subject=kwargs["subject"],
            body=kwargs["body"],
            from_name=kwargs.get("from_name", "Garamatic Support"),
        )
    except (ValidationError, ValueError) as e:
        return {
            "status": "validation_error",
            "error": str(e),
            "to_email": kwargs["to_email"],
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to queue email: {e}",
            "to_email": kwargs["to_email"],
        }

    return {
        "status": "success",
        "to_email": kwargs["to_email"],
        "subject": kwargs["subject"],
        "message": result.message,
    }


async def _draft_email(service: GaramaticService, **kwargs: Any) -> dict[str, Any]:
    to_email = kwargs["to_email"]
    subject = kwargs["subject"]
    key_points = kwargs["key_points"]
    tone = kwargs.get("tone", "professional")

    if not to_email or "@" not in to_email:
        return {
            "status": "error",
            "error": "Invalid or missing email address",
        }

    draft = f"""Subject: {subject}

Dear {to_email.split('@')[0].replace('.', ' ').title()},

{key_points}

Best regards,
Garamatic Support"""

    return {
        "status": "success",
        "draft": draft,
        "subject": subject,
        "to_email": to_email,
        "tone": tone,
        "message": "Email drafted. Review the draft before sending with send_email.",
    }


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def register_tools() -> None:
    registry = get_registry()
    registry.register(
        ToolDefinition(
            name="send_email",
            schema=SendEmailSchema,
            handler=_send_email,
            description="Send an email to a customer via the mailing service.",
            group="email",
            annotations=ToolAnnotations(
                title="Send email",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            name="draft_email",
            schema=DraftEmailSchema,
            handler=_draft_email,
            description="Draft an email for review before sending.",
            group="email",
            annotations=ToolAnnotations(
                title="Draft email",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
            ),
        )
    )


# Auto-register on import
register_tools()
