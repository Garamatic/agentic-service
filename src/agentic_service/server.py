"""
MCP Server for Garamatic Platform
Exposes tickets, invoices, and emails as AI-callable tools
"""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastmcp import Context, FastMCP
from starlette.responses import Response

from .service import (
    GaramaticService,
    ServiceUnavailableError,
    ValidationError,
    create_service,
    format_date,
    validate_email,
)
from .tools import (
    McpAdapter,
    customer_tools,  # noqa: F401
    email_tools,  # noqa: F401
    get_registry,
    invoice_tools,  # noqa: F401
    system_tools,  # noqa: F401
    ticket_tools,  # noqa: F401 — triggers auto-registration
)

logger = structlog.get_logger()


# ------------------------------------------------------------------
# Lifespan: create service, inject into MCP context
# ------------------------------------------------------------------

# Module-level reference for lifespan-managed service (used by resources)
_lifespan_service: GaramaticService | None = None


@asynccontextmanager
async def app_lifespan(server: "FastMCP") -> AsyncIterator[dict[str, Any]]:
    """Initialize and cleanup service on server startup/shutdown.

    Uses create_service() as the single composition root.
    """
    global _lifespan_service
    service = await create_service()
    _lifespan_service = service

    logger.info("service.started")

    try:
        yield {"service": service}
    finally:
        _lifespan_service = None
        await service.close()
        logger.info("service.stopped")


mcp = FastMCP("garamatic", lifespan=app_lifespan)


def _get_service(ctx: Context) -> GaramaticService:
    """Extract service from MCP lifespan context.

    Handles the case where a small model hallucinates a ``ctx`` dict
    instead of letting the framework inject the real Context object.
    """
    if isinstance(ctx, dict):
        # Model passed ctx as a keyword argument — ignore it and use
        # the lifespan-managed service directly.
        if _lifespan_service is not None:
            return _lifespan_service
        raise ServiceUnavailableError("Service not initialized")
    rc = ctx.request_context
    if rc is None:
        raise ServiceUnavailableError("Service not initialized")
    lc = rc.lifespan_context
    if lc is None:
        raise ServiceUnavailableError("Service not initialized")
    service = lc.get("service")
    if service is None:
        raise ServiceUnavailableError("Service not initialized")
    return service  # type: ignore[no-any-return]


# ------------------------------------------------------------------
# Auto-register all tools from the ToolRegistry
# ------------------------------------------------------------------

adapter = McpAdapter(get_registry(), mcp)
adapter.register_all(_get_service)


# ------------------------------------------------------------------
# Favicon
# ------------------------------------------------------------------

@mcp.custom_route("/favicon.ico", methods=["GET"])
async def _favicon(request: Any) -> Response:
    """Serve the Garamatic favicon."""
    favicon_path = os.path.join(os.path.dirname(__file__), "favicon.ico")
    if os.path.exists(favicon_path):
        with open(favicon_path, "rb") as f:
            return Response(content=f.read(), media_type="image/x-icon")
    return Response(status_code=404)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _get_resource_service() -> GaramaticService:
    """Return the lifespan-managed service for resources.

    Raises ServiceUnavailableError if the lifespan service is not available.
    """
    if _lifespan_service is not None:
        return _lifespan_service
    raise ServiceUnavailableError("Lifespan service not available")


# ------------------------------------------------------------------
# MCP Prompts
# ------------------------------------------------------------------


@mcp.prompt()
def create_ticket_prompt(customer_email: str = "", issue_description: str = "") -> str:
    """Prompt template for creating a new support ticket."""
    return f"""Create a support ticket for the customer.

Customer email: {customer_email or "[to be provided]"}
Issue description: {issue_description or "[to be provided]"}

Please use the create_ticket tool to create the ticket. Make sure to gather:
- A clear, concise title
- A detailed description of the issue
- The customer's full name
- Priority level (low, medium, high, urgent)"""


@mcp.prompt()
def get_customer_context_prompt(customer_email: str = "") -> str:
    """Prompt template for retrieving full customer context."""
    return f"""Retrieve the complete customer context for {customer_email or "[email to be provided]"}.

Use the get_customer_context tool to look up:
- Total tickets and open tickets
- Outstanding balance and lifetime value
- Recent ticket history
- Recent invoice history

Summarize the key findings and suggest any follow-up actions."""


@mcp.prompt()
def resolve_ticket_prompt(ticket_id: str = "", resolution_notes: str = "") -> str:
    """Prompt template for resolving a support ticket."""
    return f"""Resolve ticket {ticket_id or "[ID to be provided]"}.

Resolution summary: {resolution_notes or "[to be provided]"}

Please use the resolve_ticket tool. If the resolution involves billable work,
provide the billable amount. The system will handle invoice creation automatically."""


@mcp.prompt()
def send_email_prompt(to_email: str = "", subject: str = "", body: str = "") -> str:
    """Prompt template for sending an email to a customer."""
    return f"""Send an email to the customer.

To: {to_email or "[email to be provided]"}
Subject: {subject or "[subject to be provided]"}

Body:
{body or "[body to be provided]"}

Use the send_email tool to queue the email. The email will be sent via the mailing service."""


# MCP Resources
# ------------------------------------------------------------------


@mcp.resource("ticket://{ticket_id}")
async def get_ticket_resource(ticket_id: str) -> str:
    """MCP Resource: Single ticket details as formatted text."""
    if not ticket_id or not ticket_id.strip():
        return "Error: Ticket ID is required"

    try:
        service = await _get_resource_service()
    except ServiceUnavailableError as e:
        return f"Error: Service unavailable: {e}"
    try:
        ticket = await service.get_ticket(ticket_id)
    except Exception as e:
        return f"Error: Could not retrieve ticket {ticket_id}: {e}"

    try:
        invoice = await service.get_invoice_status(ticket_id)
    except Exception:
        invoice = None

    invoice_info = ""
    if invoice and invoice.status != "not_found":
        amount = invoice.amount or 0
        invoice_info = (
            f"\n\n## Invoice\n"
            f"- ID: {invoice.invoice_id or 'N/A'}\n"
            f"- Status: {invoice.status.upper()}\n"
            f"- Amount: €{amount:.2f}\n"
            f"- Paid At: {format_date(invoice.paid_at)}"
        )

    return f"""
# Ticket {ticket_id}

## Details
- Status: {ticket.status.upper()}
- Title: {ticket.title}
- Customer: {ticket.customer_name or "Unknown"} ({ticket.customer_email or "Unknown"})
- Created: {format_date(ticket.created_at)}
- Resolved: {format_date(ticket.resolved_at)}

## Description
{ticket.description or "No description"}

## Resolution Notes
{ticket.resolution_notes or "Not resolved"}{invoice_info}
"""


@mcp.resource("system://health")
async def get_health() -> str:
    """MCP Resource: System health status."""
    try:
        service = await _get_resource_service()
    except ServiceUnavailableError as e:
        return json.dumps({"status": "degraded", "services": {"error": str(e)}}, indent=2)
    try:
        health = await service.health_check()
        health_json = {"status": health.status, "services": health.services}
    except Exception as e:
        health_json = {"status": "degraded", "services": {"error": str(e)}}

    return json.dumps(health_json, indent=2)


@mcp.resource("customer://{email}")
async def get_customer_resource(email: str) -> str:
    """MCP Resource: Full customer profile as formatted text."""
    try:
        validate_email(email)
    except ValidationError as e:
        return f"Error: {e}"

    try:
        service = await _get_resource_service()
    except ServiceUnavailableError as e:
        return f"Error: Service unavailable: {e}"
    try:
        context = await service.get_customer_context(email)
    except Exception as e:
        return f"Error: Could not retrieve customer context: {e}"

    tickets_section = (
        "\n".join(
            f"- [{t.status.upper()}] {t.title} ({format_date(t.created_at)})"
            for t in context.recent_tickets
        )
        if context.recent_tickets
        else "No recent tickets"
    )

    invoices_section = (
        "\n".join(f"- [{i.status.upper()}] €{i.amount:.2f}" for i in context.recent_invoices)
        if context.recent_invoices
        else "No recent invoices"
    )

    return f"""
# Customer Profile: {email}

## Summary
- Total Tickets: {context.summary.total_tickets}
- Open Tickets: {context.summary.open_tickets}
- Outstanding Balance: €{context.summary.outstanding_balance:.2f}
- Lifetime Value: €{context.summary.lifetime_value:.2f}

## Recent Tickets
{tickets_section}

## Recent Invoices
{invoices_section}
"""


def main() -> None:
    """Run the MCP server."""
    # Use SSE transport for Docker; stdio for local CLI usage
    import os

    from agentic_service.config import get_settings
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    config = get_settings()
    transport = os.environ.get("MCP_TRANSPORT", config.mcp_transport)
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        ),
    ]
    if transport == "sse":
        mcp.run(transport="sse", host="0.0.0.0", port=3001, middleware=middleware)
    else:
        mcp.run(
            transport=transport,
            host="0.0.0.0",
            port=3001,
            middleware=middleware,
            stateless_http=True,
        )  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
