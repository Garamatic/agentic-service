"""
MCP Server for Garamatic Platform
Exposes tickets, invoices, and emails as AI-callable tools
"""

import asyncio
import json
from typing import Any, AsyncIterator

import httpx
import structlog
from fastmcp import FastMCP, Context
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, get_settings
from .clients import TicketClient, OdooBridgeClient, RabbitMQPublisher

logger = structlog.get_logger()

# Create MCP server
mcp = FastMCP("garamatic")

# Global settings (initialized on startup)
settings: Settings | None = None
ticket_client: TicketClient | None = None
odoo_client: OdooBridgeClient | None = None
mq_publisher: RabbitMQPublisher | None = None


@mcp.tool()
async def get_ticket_status(
    ticket_id: str,
    ctx: Context
) -> dict[str, Any]:
    """
    Get the current status of a support ticket.
    
    Args:
        ticket_id: The ticket ID (e.g., "T-123" or "123")
    
    Returns:
        Ticket details including status, customer info, and resolution notes
    """
    logger.info("Getting ticket status", ticket_id=ticket_id)
    
    try:
        result = await ticket_client.get_ticket(ticket_id)
        
        ctx.info(f"Found ticket {ticket_id} with status: {result.get('status')}")
        
        return {
            "ticket_id": ticket_id,
            "status": result.get("status", "unknown"),
            "customer_email": result.get("customerEmail"),
            "customer_name": result.get("customerName"),
            "title": result.get("title"),
            "description": result.get("description"),
            "created_at": result.get("createdAt"),
            "resolved_at": result.get("resolvedAt"),
            "resolution_notes": result.get("resolutionNotes"),
            "billable_amount": result.get("billableAmount")
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            ctx.error(f"Ticket {ticket_id} not found")
            return {"error": f"Ticket {ticket_id} not found"}
        raise


@mcp.tool()
async def list_customer_tickets(
    customer_email: str,
    status: str | None = None,
    limit: int = 10,
    ctx: Context = None
) -> list[dict[str, Any]]:
    """
    List all tickets for a specific customer.
    
    Args:
        customer_email: Customer's email address
        status: Optional filter by status (open, resolved, closed)
        limit: Maximum number of tickets to return (default 10)
    
    Returns:
        List of tickets with status and brief details
    """
    logger.info("Listing customer tickets", email=customer_email, status=status)
    
    result = await ticket_client.list_tickets(
        customer_email=customer_email,
        status=status,
        limit=limit
    )
    
    ctx.info(f"Found {len(result)} tickets for {customer_email}")
    
    return [
        {
            "ticket_id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "created_at": t.get("createdAt"),
            "resolved_at": t.get("resolvedAt")
        }
        for t in result
    ]


@mcp.tool()
async def create_ticket(
    title: str,
    description: str,
    customer_email: str,
    customer_name: str,
    priority: str = "medium",
    ctx: Context = None
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
    logger.info("Creating ticket", title=title, customer=customer_email)
    
    result = await ticket_client.create_ticket(
        title=title,
        description=description,
        customer_email=customer_email,
        customer_name=customer_name,
        priority=priority
    )
    
    ticket_id = result.get("id")
    ctx.info(f"Created ticket {ticket_id}")
    
    return {
        "ticket_id": ticket_id,
        "status": "created",
        "customer_email": customer_email,
        "priority": priority,
        "message": f"Ticket {ticket_id} created successfully. Customer will receive confirmation email."
    }


@mcp.tool()
async def resolve_ticket(
    ticket_id: str,
    resolution_notes: str,
    billable_amount: float | None = None,
    ctx: Context = None
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
    logger.info("Resolving ticket", ticket_id=ticket_id, billable=billable_amount)
    
    # Resolve in ticket-masala
    result = await ticket_client.resolve_ticket(
        ticket_id=ticket_id,
        resolution_notes=resolution_notes,
        billable_amount=billable_amount
    )
    
    ctx.info(f"Ticket {ticket_id} resolved")
    
    response = {
        "ticket_id": ticket_id,
        "status": "resolved",
        "resolution_notes": resolution_notes
    }
    
    if billable_amount:
        response["invoice"] = {
            "status": "queued",
            "estimated_amount": billable_amount,
            "message": "Invoice creation queued. Will be created in Odoo within 1 minute."
        }
    
    return response


@mcp.tool()
async def create_invoice(
    ticket_id: str,
    amount: float,
    description: str | None = None,
    ctx: Context = None
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
    logger.info("Creating invoice", ticket_id=ticket_id, amount=amount)
    
    # Get ticket details first
    ticket = await ticket_client.get_ticket(ticket_id)
    
    # Emit event to RabbitMQ for odoo-bridge to process
    event = {
        "event_type": "invoice.create_requested",
        "ticket_id": ticket_id,
        "customer_email": ticket.get("customerEmail"),
        "customer_name": ticket.get("customerName"),
        "amount": amount,
        "description": description or ticket.get("description", "Service"),
        "requested_at": datetime.now().isoformat()
    }
    
    await mq_publisher.publish("invoice.create_requested", event)
    
    ctx.info(f"Invoice creation requested for ticket {ticket_id}")
    
    return {
        "status": "queued",
        "ticket_id": ticket_id,
        "amount": amount,
        "customer_email": ticket.get("customerEmail"),
        "message": "Invoice creation queued. Odoo integration will process this within 30 seconds."
    }


@mcp.tool()
async def get_invoice_status(
    ticket_id: str,
    ctx: Context = None
) -> dict[str, Any]:
    """
    Check invoice status for a ticket.
    
    Args:
        ticket_id: The ticket to check
    
    Returns:
        Invoice status: draft, sent, paid, or not_found
    """
    logger.info("Checking invoice status", ticket_id=ticket_id)
    
    # Query odoo-bridge for invoice status
    # This is a simplified version - real implementation would query DB
    result = await odoo_client.get_invoice_status(ticket_id)
    
    if not result:
        return {
            "ticket_id": ticket_id,
            "status": "not_found",
            "message": "No invoice found for this ticket"
        }
    
    return {
        "ticket_id": ticket_id,
        "invoice_id": result.get("invoice_id"),
        "status": result.get("status"),  # draft, sent, paid
        "amount": result.get("amount"),
        "created_at": result.get("created_at"),
        "paid_at": result.get("paid_at")
    }


@mcp.tool()
async def send_email(
    to_email: str,
    subject: str,
    body: str,
    from_name: str = "Garamatic Support",
    ctx: Context = None
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
    logger.info("Sending email", to=to_email, subject=subject)
    
    # Emit event for mailing-service
    event = {
        "event_type": "email.send",
        "to_email": to_email,
        "from_name": from_name,
        "subject": subject,
        "body_html": body,
        "sent_at": datetime.now().isoformat()
    }
    
    await mq_publisher.publish("email.send", event)
    
    ctx.info(f"Email queued for {to_email}")
    
    return {
        "status": "queued",
        "to_email": to_email,
        "subject": subject,
        "message": "Email queued for sending. Will be delivered within 1 minute."
    }


@mcp.tool()
async def get_customer_context(
    customer_email: str,
    ctx: Context = None
) -> dict[str, Any]:
    """
    Get full customer context across all services.
    
    Args:
        customer_email: Customer email to look up
    
    Returns:
        Comprehensive profile: tickets, invoices, and activity
    """
    logger.info("Getting customer context", email=customer_email)
    
    # Parallel queries
    tickets_task = ticket_client.list_tickets(customer_email, limit=5)
    invoices_task = odoo_client.list_customer_invoices(customer_email)
    
    tickets, invoices = await asyncio.gather(tickets_task, invoices_task)
    
    total_ticket_value = sum(
        t.get("billableAmount", 0) for t in tickets if t.get("status") == "resolved"
    )
    
    outstanding = sum(
        inv.get("amount", 0) for inv in invoices if inv.get("status") != "paid"
    )
    
    return {
        "customer_email": customer_email,
        "summary": {
            "total_tickets": len(tickets),
            "open_tickets": sum(1 for t in tickets if t.get("status") != "resolved"),
            "total_invoiced": sum(inv.get("amount", 0) for inv in invoices),
            "outstanding_balance": outstanding,
            "lifetime_value": total_ticket_value
        },
        "recent_tickets": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "status": t.get("status"),
                "created_at": t.get("createdAt")
            }
            for t in tickets[:3]
        ],
        "recent_invoices": [
            {
                "id": inv.get("id"),
                "amount": inv.get("amount"),
                "status": inv.get("status"),
                "created_at": inv.get("createdAt")
            }
            for inv in invoices[:3]
        ]
    }


@mcp.resource("customer://{email}")
async def get_customer_resource(email: str) -> str:
    """
    MCP Resource: Full customer profile as formatted text.
    AI can read this for rich context about a customer.
    """
    context = await get_customer_context(email)
    
    return f"""
# Customer Profile: {email}

## Summary
- Total Tickets: {context['summary']['total_tickets']}
- Open Tickets: {context['summary']['open_tickets']}
- Outstanding Balance: €{context['summary']['outstanding_balance']:.2f}
- Lifetime Value: €{context['summary']['lifetime_value']:.2f}

## Recent Tickets
{chr(10).join(f"- [{t['status'].upper()}] {t['title']} ({t['created_at'][:10]})" for t in context['recent_tickets'])}

## Recent Invoices
{chr(10).join(f"- [{inv['status'].upper()}] €{inv['amount']:.2f}" for inv in context['recent_invoices'])}
"""


# Setup and teardown
@mcp.on_startup
async def on_startup():
    """Initialize clients on server startup."""
    global settings, ticket_client, odoo_client, mq_publisher
    
    settings = get_settings()
    
    ticket_client = TicketClient(settings.ticket_api_url)
    odoo_client = OdooBridgeClient(settings.odoo_bridge_url)
    mq_publisher = RabbitMQPublisher(settings.rabbitmq_url)
    
    await mq_publisher.connect()
    
    logger.info("Agentic service started", 
                ticket_api=settings.ticket_api_url,
                odoo_bridge=settings.odoo_bridge_url)


@mcp.on_shutdown
async def on_shutdown():
    """Cleanup on shutdown."""
    if ticket_client:
        await ticket_client.close()
    if odoo_client:
        await odoo_client.close()
    if mq_publisher:
        await mq_publisher.close()
    
    logger.info("Agentic service stopped")


def main():
    """Run the MCP server."""
    # FastMCP handles stdio or sse transport
    mcp.run()


if __name__ == "__main__":
    from datetime import datetime
    main()
