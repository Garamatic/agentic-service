"""
MCP Server for Garamatic Platform
Exposes tickets, invoices, and emails as AI-callable tools
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

import httpx
import structlog
from fastmcp import FastMCP, Context

from .clients import TicketClient, OdooBridgeClient, RabbitMQPublisher
from .config import get_settings

logger = structlog.get_logger()

# Setup and teardown using lifespan context manager
@asynccontextmanager
async def app_lifespan(server: "FastMCP"):
    """Initialize and cleanup clients on server startup/shutdown."""
    global ticket_client, odoo_client, mq_publisher
    
    # Startup
    config = get_settings()
    
    ticket_client = TicketClient(config.ticket_api_url)
    odoo_client = OdooBridgeClient(config.odoo_bridge_url)
    
    # Initialize RabbitMQ but don't fail if unavailable (graceful degradation)
    try:
        mq_publisher = RabbitMQPublisher(config.rabbitmq_url)
        await mq_publisher.connect()
        logger.info("rabbitmq.connected")
    except Exception as e:
        logger.warning("rabbitmq.unavailable", error=str(e))
        mq_publisher = None  # Flag as unavailable - write operations will fail gracefully
    
    logger.info("service.started",
                ticket_api=config.ticket_api_url,
                odoo_bridge=config.odoo_bridge_url)
    
    try:
        yield
    finally:
        # Shutdown
        if ticket_client:
            await ticket_client.close()
        if odoo_client:
            await odoo_client.close()
        if mq_publisher:
            await mq_publisher.close()
        
        logger.info("service.stopped")


# Create MCP server
mcp = FastMCP("garamatic", lifespan=app_lifespan)


def _format_date(d: str | None) -> str:
    """Format an ISO date string to YYYY-MM-DD or return placeholder."""
    return d[:10] if d and len(d) >= 10 else (d or "N/A")


def _validate_email(email: str) -> None:
    """Validate email format, raise ValueError if invalid."""
    if not email or not isinstance(email, str):
        raise ValueError(f"Invalid email format: {email}")
    if "@" not in email:
        raise ValueError(f"Invalid email format: {email}")
    local, _, domain = email.rpartition("@")
    if not local or not domain:
        raise ValueError(f"Invalid email format: {email}")
    # Domain must contain at least one dot, not start/end with dot
    if "." not in domain:
        raise ValueError(f"Invalid email format: {email}")
    if domain.startswith(".") or domain.endswith("."):
        raise ValueError(f"Invalid email format: {email}")
    # Local part cannot contain consecutive dots
    if ".." in local:
        raise ValueError(f"Invalid email format: {email}")


def _generate_request_id() -> str:
    """Generate unique request ID for tracing."""
    return uuid.uuid4().hex

# Global clients (initialized by lifespan context manager)
ticket_client: TicketClient | None = None
odoo_client: OdooBridgeClient | None = None
mq_publisher: RabbitMQPublisher | None = None


@mcp.tool()
async def get_ticket_status(
    ticket_id: str,
    *,
    ctx: Context
) -> dict[str, Any]:
    """
    Get the current status of a support ticket.
    
    Args:
        ticket_id: The ticket ID (e.g., "T-123" or "123")
    
    Returns:
        Ticket details including status, customer info, and resolution notes
    """
    if ticket_client is None:
        return {"error": "Ticket service unavailable", "ticket_id": ticket_id}
    
    rid = _generate_request_id()
    logger.info("Getting ticket status", ticket_id=ticket_id, request_id=rid)
    
    result = await ticket_client.get_ticket(ticket_id)
    
    ctx.info(f"Found ticket {ticket_id} with status: {result.get('status')}")
    
    return {
        "ticket_id": ticket_id,
        "status": result.get("status", "unknown"),
        "customer_id": result.get("customerId"),  # NOTE: API returns CustomerId, not customerEmail
        "title": result.get("title"),
        "description": result.get("description"),
        "created_at": result.get("createdAt"),
        "resolved_at": result.get("resolvedAt") or result.get("completedAt"),
        "resolution_notes": result.get("resolutionNotes"),
        "billable_amount": result.get("billableAmount")
    }


@mcp.tool()
async def list_customer_tickets(
    customer_email: str,
    status: Literal["open", "resolved", "closed"] | None = None,
    limit: int = 10,
    *,
    ctx: Context
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
    if ticket_client is None:
        return {"error": "Ticket service unavailable", "tickets": []}
    
    _validate_email(customer_email)
    rid = _generate_request_id()
    logger.info("Listing customer tickets", email=customer_email, status=status, request_id=rid)
    
    # NOTE: API expects CustomerId (user ID), not email. 
    # We fetch all and filter by matching tickets since we don't have user lookup by email.
    all_tickets = await ticket_client.list_tickets(
        status=status,
        limit=100
    )
    # Filter to tickets where customerId matches the email (stored as customerId in some cases)
    result = [t for t in all_tickets if t.get("customerId") == customer_email]
    
    ctx.info(f"Found {len(result)} tickets for {customer_email}")
    
    return {
        "tickets": [
            {
                "ticket_id": t.get("id"),
                "title": t.get("title"),
                "status": t.get("status"),
                "created_at": t.get("createdAt"),
                "resolved_at": t.get("resolvedAt") or t.get("completedAt")
            }
            for t in result
        ]
    }


@mcp.tool()
async def create_ticket(
    title: str,
    description: str,
    customer_email: str,
    customer_name: str,
    priority: Literal["low", "medium", "high", "urgent"] = "medium",
    *,
    ctx: Context
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
    if ticket_client is None:
        return {"error": "Ticket service unavailable"}
    
    _validate_email(customer_email)
    if not title or not title.strip():
        raise ValueError("Title is required")
    rid = _generate_request_id()
    logger.info("Creating ticket", title=title, customer=customer_email, request_id=rid)
    
    # NOTE: API expects CustomerId (user ID), not email.
    # Using email as customer_id - this works if the system stores email as the user identifier
    result = await ticket_client.create_ticket(
        title=title,
        description=description,
        customer_id=customer_email,
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
    *,
    ctx: Context
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
    if ticket_client is None:
        return {"error": "Ticket service unavailable", "ticket_id": ticket_id}
    
    if not resolution_notes or not resolution_notes.strip():
        raise ValueError("Resolution notes are required")
    if billable_amount is not None and billable_amount < 0:
        raise ValueError("Billable amount cannot be negative")
    rid = _generate_request_id()
    logger.info("Resolving ticket", ticket_id=ticket_id, billable=billable_amount, request_id=rid)
    
    # Resolve in ticket-masala
    await ticket_client.resolve_ticket(
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
    
    if billable_amount is not None:
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
    *,
    ctx: Context
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
    if ticket_client is None:
        return {"error": "Ticket service unavailable", "status": "failed", "ticket_id": ticket_id}
    if amount <= 0:
        raise ValueError("Invoice amount must be positive")
    if mq_publisher is None:
        return {
            "error": "Invoice service unavailable",
            "status": "failed",
            "ticket_id": ticket_id
        }
    rid = _generate_request_id()
    logger.info("Creating invoice", ticket_id=ticket_id, amount=amount, request_id=rid)
    
    # Get ticket details first
    try:
        ticket = await ticket_client.get_ticket(ticket_id)
    except Exception as e:
        logger.error("create_invoice.ticket_lookup_failed", ticket_id=ticket_id, error=str(e))
        return {
            "error": f"Failed to get ticket: {e}",
            "status": "failed",
            "ticket_id": ticket_id
        }
    
    # Emit event to RabbitMQ for odoo-bridge to process (flat snake_case per schema)
    # NOTE: ticket-masala API returns CustomerId, not customerEmail. Using ticket_id to lookup customer.
    payload = {
        "event_type": "invoice.create_requested",
        "timestamp": datetime.now().isoformat(),
        "source": "agentic-service",
        "ticket_id": ticket_id,
        "customer_email": ticket.get("customerId"),  # customerId IS the email in this system
        "customer_name": ticket.get("title", "Customer"),  # Use title as fallback since API has no name field
        "amount": amount,
        "description": description or ticket.get("description", "Service"),
        "requested_at": datetime.now().isoformat()
    }
    
    await mq_publisher.publish("invoice.create_requested", payload)
    
    ctx.info(f"Invoice creation requested for ticket {ticket_id}")
    
    return {
        "status": "queued",
        "ticket_id": ticket_id,
        "amount": amount,
        "customer_email": ticket.get("customerId"),  # customerId IS the email
        "message": "Invoice creation queued. Odoo integration will process this within 30 seconds."
    }


@mcp.tool()
async def get_invoice_status(
    ticket_id: str,
    *,
    ctx: Context
) -> dict[str, Any]:
    """
    Check invoice status for a ticket.
    
    Args:
        ticket_id: The ticket to check
    
    Returns:
        Invoice status: draft, sent, paid, or not_found
    """
    if odoo_client is None:
        return {
            "ticket_id": ticket_id,
            "status": "error",
            "message": "Invoice service unavailable"
        }
    
    rid = _generate_request_id()
    logger.info("Checking invoice status", ticket_id=ticket_id, request_id=rid)
    
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
    *,
    ctx: Context
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
    _validate_email(to_email)
    if not subject or not subject.strip():
        raise ValueError("Email subject is required")
    if mq_publisher is None:
        return {
            "error": "Email service unavailable",
            "status": "failed",
            "to_email": to_email
        }
    rid = _generate_request_id()
    logger.info("Sending email", to=to_email, subject=subject, request_id=rid)
    
    # Emit event for mailing-service (flat snake_case per email.send schema)
    payload = {
        "event_type": "email.send",
        "timestamp": datetime.now().isoformat(),
        "source": "agentic-service",
        "to_email": to_email,
        "from_email": "support@garamatic.com",
        "from_name": from_name,
        "subject": subject,
        "body_html": body,
    }
    
    await mq_publisher.publish("email.send", payload)
    
    ctx.info(f"Email queued for {to_email}")
    
    return {
        "status": "queued",
        "to_email": to_email,
        "subject": subject,
        "message": "Email queued for sending. Will be delivered within 1 minute."
    }


async def _get_customer_context_internal(customer_email: str) -> dict[str, Any]:
    """Internal helper to fetch customer context without MCP context."""
    _validate_email(customer_email)
    
    if ticket_client is None or odoo_client is None:
        raise RuntimeError("Required services unavailable")
    
    rid = _generate_request_id()
    logger.info("Getting customer context", email=customer_email, request_id=rid)
    
    # Parallel queries
    # NOTE: Ticket API filters by CustomerId (user ID), not email
    # This requires mapping email -> user ID first, or the API needs email filter support
    tickets_task = ticket_client.list_tickets(limit=50)  # Fetch all, filter locally
    invoices_task = odoo_client.list_customer_invoices(customer_email)
    
    all_tickets, invoices = await asyncio.gather(tickets_task, invoices_task)
    
    # Filter tickets by customer email (CustomerId field stores email in this system)
    customer_tickets = [t for t in all_tickets if t.get("customerId") == customer_email]
    
    total_ticket_value = sum(
        t.get("billableAmount", 0) for t in customer_tickets if t.get("status") == "resolved"
    )
    
    outstanding = sum(
        inv.get("amount", 0) for inv in invoices if inv.get("status") != "paid"
    )
    
    return {
        "customer_email": customer_email,
        "summary": {
            "total_tickets": len(customer_tickets),
            "open_tickets": sum(1 for t in customer_tickets if t.get("status") != "resolved"),
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
            for t in customer_tickets[:3]
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


@mcp.tool()
async def get_customer_context(
    customer_email: str,
    *,
    ctx: Context
) -> dict[str, Any]:
    """
    Get full customer context across all services.
    
    Args:
        customer_email: Customer email to look up
    
    Returns:
        Comprehensive profile: tickets, invoices, and activity
    """
    context = await _get_customer_context_internal(customer_email)
    ctx.info(f"Retrieved context for {customer_email}")
    return context


@mcp.resource("ticket://{ticket_id}")
async def get_ticket_resource(ticket_id: str) -> str:
    """
    MCP Resource: Single ticket details as formatted text.
    AI can read this for ticket-specific context.
    """
    if ticket_client is None:
        return "Error: Ticket service unavailable"
    
    ticket = await ticket_client.get_ticket(ticket_id)
    invoice = await odoo_client.get_invoice_status(ticket_id) if odoo_client else None
    
    invoice_info = ""
    if invoice:
        invoice_info = f"""

## Invoice
- ID: {invoice.get('invoice_id', 'N/A')}
- Status: {invoice.get('status', 'unknown').upper()}
- Amount: €{invoice.get('amount', 0):.2f}
- Paid At: {_format_date(invoice.get('paid_at'))}
"""
    
    return f"""
# Ticket {ticket_id}

## Details
- Status: {ticket.get('status', 'unknown').upper()}
- Title: {ticket.get('title', 'Untitled')}
- Customer ID: {ticket.get('customerId', 'Unknown')}
- Priority Score: {ticket.get('priorityScore', 'N/A')}
- Created: {_format_date(ticket.get('createdAt'))}
- Resolved: {_format_date(ticket.get('resolvedAt'))}

## Description
{ticket.get('description', 'No description')}

## Resolution Notes
{ticket.get('resolutionNotes', 'Not resolved')}{invoice_info}
"""


@mcp.resource("system://health")
async def get_health() -> str:
    """
    MCP Resource: System health status.
    Check connectivity to all backend services.
    """
    health = {
        "status": "healthy",
        "services": {
            "ticket_api": "unknown",
            "odoo_bridge": "unknown",
            "rabbitmq": mq_publisher is not None and getattr(mq_publisher, 'connection', None) is not None
        }
    }
    
    # Check ticket API
    if ticket_client is None:
        health["services"]["ticket_api"] = "not_initialized"
        health["status"] = "degraded"
    else:
        try:
            await ticket_client.list_tickets(limit=1)
            health["services"]["ticket_api"] = "connected"
        except Exception as e:
            health["services"]["ticket_api"] = f"error: {str(e)[:50]}"
            health["status"] = "degraded"
    
    # Check odoo bridge
    if odoo_client is None:
        health["services"]["odoo_bridge"] = "not_initialized"
        health["status"] = "degraded"
    else:
        try:
            await odoo_client.list_customer_invoices("test@example.com")
            health["services"]["odoo_bridge"] = "connected"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                health["services"]["odoo_bridge"] = "connected"
            else:
                health["services"]["odoo_bridge"] = f"error: {str(e)[:50]}"
                health["status"] = "degraded"
        except Exception as e:
            health["services"]["odoo_bridge"] = f"error: {str(e)[:50]}"
            health["status"] = "degraded"
    
    return json.dumps(health, indent=2)


@mcp.resource("customer://{email}")
async def get_customer_resource(email: str) -> str:
    """
    MCP Resource: Full customer profile as formatted text.
    AI can read this for rich context about a customer.
    """
    _validate_email(email)
    context = await _get_customer_context_internal(email)
    
    tickets_section = "\n".join(
        f"- [{t['status'].upper() if t.get('status') else 'UNKNOWN'}] "
        f"{t.get('title', 'Untitled')} ({_format_date(t.get('created_at'))})"
        for t in context['recent_tickets']
    ) if context['recent_tickets'] else "No recent tickets"
    
    invoices_section = "\n".join(
        f"- [{inv['status'].upper() if inv.get('status') else 'UNKNOWN'}] "
        f"€{inv.get('amount', 0):.2f}"
        for inv in context['recent_invoices']
    ) if context['recent_invoices'] else "No recent invoices"
    
    return f"""
# Customer Profile: {email}

## Summary
- Total Tickets: {context['summary']['total_tickets']}
- Open Tickets: {context['summary']['open_tickets']}
- Outstanding Balance: €{context['summary']['outstanding_balance']:.2f}
- Lifetime Value: €{context['summary']['lifetime_value']:.2f}

## Recent Tickets
{tickets_section}

## Recent Invoices
{invoices_section}
"""




def main():
    """Run the MCP server."""
    # FastMCP handles stdio or sse transport
    mcp.run()


if __name__ == "__main__":
    main()
