"""
MCP Server for Garamatic Platform
Exposes tickets, invoices, and emails as AI-callable tools
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastmcp import Context, FastMCP

from .approval import ApprovalRequiredError
from .service import (
    GaramaticService,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
    create_service,
    format_date,
    validate_email,
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
    """Extract service from MCP lifespan context."""
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
# MCP Tools — thin wrappers over GaramaticService
# ------------------------------------------------------------------


@mcp.tool()
async def get_ticket_status(ticket_id: str, *, ctx: Context) -> dict[str, Any]:
    """
    Get the current status of a support ticket.

    Args:
        ticket_id: The ticket ID (e.g., "T-123" or "123")

    Returns:
        Ticket details including status, customer info, and resolution notes
    """
    service = _get_service(ctx)
    try:
        ticket = await service.get_ticket(ticket_id)
    except ValidationError as e:
        return {"ticket_id": ticket_id, "status": "error", "message": str(e)}
    except NotFoundError:
        return {"ticket_id": ticket_id, "status": "not_found", "message": "Ticket not found"}
    except Exception as e:
        return {
            "ticket_id": ticket_id,
            "status": "error",
            "message": f"Failed to retrieve ticket: {e}",
        }

    await ctx.info(f"Found ticket {ticket_id} with status: {ticket.status}")

    return {
        "ticket_id": ticket_id,
        "status": ticket.status,
        "customer_id": ticket.customer_email,
        "title": ticket.title,
        "description": ticket.description,
        "created_at": ticket.created_at,
        "resolved_at": ticket.resolved_at,
        "resolution_notes": ticket.resolution_notes,
        "billable_amount": ticket.billable_amount,
    }


@mcp.tool()
async def list_customer_tickets(
    customer_email: str, status: str | None = None, limit: int = 10, *, ctx: Context
) -> dict[str, Any]:
    """
    List all tickets for a specific customer.

    Args:
        customer_email: Customer's email address
        status: Optional filter by status (open, resolved, closed)
        limit: Maximum number of tickets to return (default 10)

    Returns:
        Dict containing "tickets" list with status and brief details
    """
    service = _get_service(ctx)
    try:
        validate_email(customer_email)
    except ValidationError as e:
        raise ValueError(str(e)) from e

    try:
        tickets = await service.list_tickets(
            customer_email=customer_email,
            status=status,  # type: ignore[arg-type]
            limit=limit,
        )
    except Exception as e:
        return {"error": f"Failed to list tickets: {e}", "tickets": []}

    await ctx.info(f"Found {len(tickets)} tickets for {customer_email}")

    return {
        "tickets": [
            {
                "ticket_id": t.id,
                "title": t.title,
                "status": t.status,
                "created_at": t.created_at,
                "resolved_at": t.resolved_at,
            }
            for t in tickets
        ]
    }


@mcp.tool()
async def create_ticket(
    title: str,
    description: str,
    customer_email: str,
    customer_name: str,
    priority: str = "medium",
    *,
    ctx: Context,
) -> dict[str, Any]:
    """
    Create a new support ticket.

    Args:
        title: Short ticket title/summary
        description: Detailed description of the issue
        customer_email: Customer's email for notifications
        customer_name: Customer's full name
        priority: Ticket priority (low, medium, high, urgent)

    Returns:
        Created ticket details including the new ticket ID
    """
    service = _get_service(ctx)
    try:
        ticket = await service.create_ticket(
            title=title,
            description=description,
            customer_email=customer_email,
            customer_name=customer_name,
            priority=priority,  # type: ignore[arg-type]
        )
    except (ValidationError, ValueError) as e:
        return {"error": str(e), "status": "failed"}
    except Exception as e:
        return {"error": f"Failed to create ticket: {e}", "status": "failed"}

    ticket_id = ticket.id
    if not ticket_id or ticket_id == "N/A":
        return {
            "error": "Ticket created but no ID returned by upstream API",
            "status": "created",
        }

    await ctx.info(f"Created ticket {ticket_id}")

    return {
        "ticket_id": ticket_id,
        "status": "created",
        "customer_email": customer_email,
        "priority": priority,
        "message": (
            f"Ticket {ticket_id} created successfully. Customer will receive confirmation email."
        ),
    }


@mcp.tool()
async def resolve_ticket(
    ticket_id: str, resolution_notes: str, billable_amount: float | None = None, *, ctx: Context
) -> dict[str, Any]:
    """
    Resolve a ticket and optionally create a billable invoice.

    Args:
        ticket_id: Ticket to resolve
        resolution_notes: Summary of what was fixed
        billable_amount: Amount to invoice (optional, omit for non-billable)

    Returns:
        Resolution confirmation and invoice info if billable
    """
    service = _get_service(ctx)
    try:
        result = await service.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=resolution_notes,
            billable_amount=billable_amount,
            approval_store=service.approval_store,
        )
    except (ValidationError, ValueError) as e:
        return {"ticket_id": ticket_id, "status": "error", "message": str(e)}
    except NotFoundError:
        return {"ticket_id": ticket_id, "status": "not_found", "message": "Ticket not found"}
    except ApprovalRequiredError as e:
        return {
            "ticket_id": ticket_id,
            "status": "approval_required",
            "approval_id": e.approval_id,
            "message": str(e),
        }
    except Exception as e:
        return {
            "ticket_id": ticket_id,
            "status": "error",
            "message": f"Failed to resolve ticket: {e}",
        }

    await ctx.info(f"Ticket {ticket_id} resolved")

    response: dict[str, Any] = {
        "ticket_id": ticket_id,
        "status": "resolved",
        "resolution_notes": resolution_notes,
    }

    if result.invoice is not None:
        response["invoice"] = {
            "status": result.invoice.status,
            "estimated_amount": result.invoice.estimated_amount,
            "message": result.invoice.message,
        }

    return response


@mcp.tool()
async def create_invoice(
    ticket_id: str, amount: float, description: str | None = None, *, ctx: Context
) -> dict[str, Any]:
    """
    Create an invoice for a ticket via the Odoo integration.

    Args:
        ticket_id: Associated ticket ID
        amount: Invoice amount in EUR
        description: Line item description (defaults to ticket description)

    Returns:
        Invoice creation confirmation with estimated processing time
    """
    service = _get_service(ctx)
    try:
        result = await service.queue_invoice(
            ticket_id=ticket_id,
            amount=amount,
            description=description,
        )
    except (ValidationError, ValueError) as e:
        return {
            "error": str(e),
            "status": "failed",
            "ticket_id": ticket_id,
        }
    except Exception as e:
        return {
            "error": f"Failed to queue invoice: {e}",
            "status": "failed",
            "ticket_id": ticket_id,
        }

    await ctx.info(f"Invoice creation requested for ticket {ticket_id}")

    return {
        "status": result.status,
        "ticket_id": ticket_id,
        "amount": amount,
        "message": result.message,
    }


@mcp.tool()
async def get_invoice_status(ticket_id: str, *, ctx: Context) -> dict[str, Any]:
    """
    Check invoice status for a ticket.

    Args:
        ticket_id: The ticket to check

    Returns:
        Invoice status: draft, sent, paid, or not_found
    """
    service = _get_service(ctx)
    try:
        invoice = await service.get_invoice_status(ticket_id)
    except (ValidationError, ValueError) as e:
        return {
            "ticket_id": ticket_id,
            "status": "error",
            "message": str(e),
        }
    except Exception as e:
        return {
            "ticket_id": ticket_id,
            "status": "error",
            "message": f"Failed to check invoice status: {e}",
        }

    if invoice.status == "not_found":
        return {
            "ticket_id": ticket_id,
            "status": "not_found",
            "message": "No invoice found for this ticket",
        }

    return {
        "ticket_id": ticket_id,
        "invoice_id": invoice.invoice_id,
        "status": invoice.status,
        "amount": invoice.amount,
        "created_at": invoice.created_at,
        "paid_at": invoice.paid_at,
    }


@mcp.tool()
async def send_email(
    to_email: str, subject: str, body: str, from_name: str = "Garamatic Support", *, ctx: Context
) -> dict[str, Any]:
    """
    Send an email to a customer via the mailing service.

    Args:
        to_email: Recipient email address
        subject: Email subject line
        body: Email body (HTML supported)
        from_name: Sender name

    Returns:
        Send confirmation
    """
    service = _get_service(ctx)
    try:
        result = await service.send_email(
            to_email=to_email,
            subject=subject,
            body=body,
            from_name=from_name,
        )
    except (ValidationError, ValueError) as e:
        return {"error": str(e), "status": "failed", "to_email": to_email}
    except Exception as e:
        return {"error": f"Failed to queue email: {e}", "status": "failed", "to_email": to_email}

    await ctx.info(f"Email queued for {to_email}")

    return {
        "status": result.status,
        "to_email": to_email,
        "subject": subject,
        "message": result.message,
    }


@mcp.tool()
async def get_customer_context(customer_email: str, *, ctx: Context) -> dict[str, Any]:
    """
    Get full customer context across all services.

    Args:
        customer_email: Customer email to look up

    Returns:
        Comprehensive profile: tickets, invoices, and activity
    """
    service = _get_service(ctx)
    try:
        context = await service.get_customer_context(customer_email)
    except ValidationError as e:
        return {"error": f"Invalid email: {e}", "customer_email": customer_email, "status": "error"}
    except Exception as e:
        return {
            "error": f"Failed to retrieve customer context: {e}",
            "customer_email": customer_email,
            "status": "error",
        }

    await ctx.info(f"Retrieved context for {customer_email}")

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

    config = get_settings()
    transport = os.environ.get("MCP_TRANSPORT", config.mcp_transport)
    if transport == "sse":
        mcp.run(transport="sse", host="0.0.0.0", port=3001)
    else:
        mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
