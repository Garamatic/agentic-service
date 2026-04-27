"""REST API Gateway for Garamatic Agentic Service."""

from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field

from .clients import TicketClient, OdooBridgeClient, RabbitMQPublisher
from .config import get_settings
from .server import _generate_request_id, _get_customer_context_internal, _validate_email

logger = structlog.get_logger()

# Global clients
ticket_client: TicketClient | None = None
odoo_client: OdooBridgeClient | None = None
mq_publisher: RabbitMQPublisher | None = None


class CreateTicketRequest(BaseModel):
    """Request to create a new ticket."""
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    customer_email: EmailStr
    customer_name: str = Field(..., min_length=1)
    priority: str = Field(default="medium", pattern="^(low|medium|high|urgent)$")


class ResolveTicketRequest(BaseModel):
    """Request to resolve a ticket."""
    resolution_notes: str = Field(..., min_length=1)
    billable_amount: float | None = Field(default=None, ge=0)


class CreateInvoiceRequest(BaseModel):
    """Request to create an invoice."""
    amount: float = Field(..., gt=0)
    description: str | None = None


class SendEmailRequest(BaseModel):
    """Request to send an email."""
    to_email: EmailStr
    subject: str = Field(..., min_length=1, max_length=500)
    body: str = Field(..., min_length=1)
    from_name: str = Field(default="Garamatic Support")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup clients."""
    global ticket_client, odoo_client, mq_publisher
    
    config = get_settings()
    
    ticket_client = TicketClient(config.ticket_api_url)
    odoo_client = OdooBridgeClient(config.odoo_bridge_url)
    
    try:
        mq_publisher = RabbitMQPublisher(config.rabbitmq_url)
        await mq_publisher.connect()
        logger.info("api.rabbitmq.connected")
    except Exception as e:
        logger.warning("api.rabbitmq.unavailable", error=str(e))
        mq_publisher = None
    
    logger.info("api.started")
    yield
    
    if ticket_client:
        await ticket_client.close()
    if odoo_client:
        await odoo_client.close()
    if mq_publisher:
        await mq_publisher.close()
    logger.info("api.stopped")


app = FastAPI(
    title="Garamatic Agentic API",
    description="REST API for ticket, invoice, and email operations",
    version="0.1.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    health = {
        "status": "healthy",
        "services": {
            "ticket_api": "unknown",
            "odoo_bridge": "unknown",
            "rabbitmq": mq_publisher is not None
        }
    }
    
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
    
    if odoo_client is None:
        health["services"]["odoo_bridge"] = "not_initialized"
        health["status"] = "degraded"
    else:
        try:
            await odoo_client.list_customer_invoices("test@example.com")
            health["services"]["odoo_bridge"] = "connected"
        except Exception as e:
            # 404 is OK - endpoint works even if no invoices for test email
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                health["services"]["odoo_bridge"] = "connected"
            else:
                health["services"]["odoo_bridge"] = f"error: {str(e)[:50]}"
                health["status"] = "degraded"
    
    return health


def _check_clients():
    """Validate that required clients are initialized."""
    if ticket_client is None:
        raise HTTPException(status_code=503, detail="Ticket service unavailable")
    if odoo_client is None:
        raise HTTPException(status_code=503, detail="Invoice service unavailable")


@app.get("/tickets/{ticket_id}")
async def api_get_ticket(ticket_id: str):
    """Get ticket details."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.get_ticket", ticket_id=ticket_id, request_id=rid)
    try:
        result = await ticket_client.get_ticket(ticket_id)
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
    except Exception as e:
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tickets")
async def api_list_tickets(
    customer_email: EmailStr | None = None,
    status: str | None = Query(None, pattern="^(open|resolved|closed)$"),
    limit: int = Query(10, ge=1, le=100)
):
    """List tickets with optional filters."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.list_tickets", email=customer_email, status=status, request_id=rid)
    try:
        result = await ticket_client.list_tickets(
            customer_email=customer_email,
            status=status,
            limit=limit
        )
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
    except Exception as e:
        logger.error("api.list_tickets_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tickets")
async def api_create_ticket(request: CreateTicketRequest):
    """Create a new ticket."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.create_ticket", title=request.title, customer=request.customer_email, request_id=rid)
    try:
        result = await ticket_client.create_ticket(
            title=request.title,
            description=request.description,
            customer_email=request.customer_email,
            customer_name=request.customer_name,
            priority=request.priority
        )
        return {
            "ticket_id": result.get("id"),
            "status": "created",
            "customer_email": request.customer_email,
            "priority": request.priority,
            "message": f"Ticket {result.get('id')} created successfully"
        }
    except Exception as e:
        logger.error("api.create_ticket_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tickets/{ticket_id}/resolve")
async def api_resolve_ticket(ticket_id: str, request: ResolveTicketRequest):
    """Resolve a ticket."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.resolve_ticket", ticket_id=ticket_id, billable=request.billable_amount, request_id=rid)
    try:
        result = await ticket_client.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=request.resolution_notes,
            billable_amount=request.billable_amount
        )
        response = {
            "ticket_id": ticket_id,
            "status": "resolved",
            "resolution_notes": request.resolution_notes
        }
        if request.billable_amount is not None:
            response["invoice"] = {
                "status": "queued",
                "estimated_amount": request.billable_amount
            }
        return response
    except Exception as e:
        logger.error("api.resolve_ticket_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/invoices/by-ticket/{ticket_id}")
async def api_get_invoice(ticket_id: str):
    """Get invoice for a ticket."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.get_invoice", ticket_id=ticket_id, request_id=rid)
    try:
        result = await odoo_client.get_invoice_status(ticket_id)
        if not result:
            return {"ticket_id": ticket_id, "status": "not_found"}
        return {
            "ticket_id": ticket_id,
            "invoice_id": result.get("invoice_id"),
            "status": result.get("status"),
            "amount": result.get("amount"),
            "created_at": result.get("created_at"),
            "paid_at": result.get("paid_at")
        }
    except Exception as e:
        logger.error("api.get_invoice_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/invoices/{ticket_id}")
async def api_create_invoice(ticket_id: str, request: CreateInvoiceRequest):
    """Create an invoice for a ticket."""
    _check_clients()
    if mq_publisher is None:
        raise HTTPException(status_code=503, detail="Invoice service unavailable")
    
    rid = _generate_request_id()
    logger.info("api.create_invoice", ticket_id=ticket_id, amount=request.amount, request_id=rid)
    
    try:
        ticket = await ticket_client.get_ticket(ticket_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Ticket not found: {e}")
    
    event = {
        "event_type": "invoice.create_requested",
        "ticket_id": ticket_id,
        "customer_email": ticket.get("customerEmail"),
        "customer_name": ticket.get("customerName"),
        "amount": request.amount,
        "description": request.description or ticket.get("description", "Service"),
        "requested_at": datetime.now().isoformat()
    }
    
    await mq_publisher.publish("invoice.create_requested", event)
    
    return {
        "status": "queued",
        "ticket_id": ticket_id,
        "amount": request.amount,
        "customer_email": ticket.get("customerEmail"),
        "message": "Invoice creation queued"
    }


@app.post("/emails")
async def api_send_email(request: SendEmailRequest):
    """Send an email."""
    if mq_publisher is None:
        raise HTTPException(status_code=503, detail="Email service unavailable")
    
    rid = _generate_request_id()
    logger.info("api.send_email", to=request.to_email, subject=request.subject, request_id=rid)
    
    event = {
        "event_type": "email.send",
        "to_email": request.to_email,
        "from_name": request.from_name,
        "subject": request.subject,
        "body_html": request.body,
        "sent_at": datetime.now().isoformat()
    }
    
    await mq_publisher.publish("email.send", event)
    
    return {
        "status": "queued",
        "to_email": request.to_email,
        "subject": request.subject,
        "message": "Email queued for sending"
    }


@app.get("/customers/{email}/context")
async def api_get_customer_context(email: str):
    """Get full customer context."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.get_customer_context", email=email, request_id=rid)
    try:
        _validate_email(email)
        return await _get_customer_context_internal(email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("api.get_customer_context_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


def main():
    """Run the REST API server."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)


if __name__ == "__main__":
    main()
