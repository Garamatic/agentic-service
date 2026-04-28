"""REST API Gateway for Garamatic Agentic Service."""

from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
import pybreaker

from .clients import TicketClient, OdooBridgeClient, RabbitMQPublisher
from .config import get_settings
from .server import _generate_request_id, _get_customer_context_internal, _validate_email

logger = structlog.get_logger()


# Global clients
ticket_client: TicketClient | None = None
odoo_client: OdooBridgeClient | None = None
mq_publisher: RabbitMQPublisher | None = None


def create_error_response(request_id: str, message: str, internal_error: str | None = None, is_development: bool = False) -> dict:
    """Create sanitized error response.
    
    In production: Only show generic message + request_id for tracing
    In development: Include internal details
    """
    response = {
        "error": message,
        "request_id": request_id
    }
    if is_development and internal_error:
        response["detail"] = internal_error
    return response


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


@app.exception_handler(pybreaker.CircuitBreakerError)
async def circuit_breaker_handler(request: Request, exc: pybreaker.CircuitBreakerError):
    """Handle circuit breaker open state."""
    rid = _generate_request_id()
    logger.warning("api.circuit_breaker_open", request_id=rid, path=request.url.path)
    
    return JSONResponse(
        status_code=503,
        content=create_error_response(rid, "Service temporarily unavailable. Please try again later.")
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle all unhandled exceptions with sanitized responses."""
    rid = _generate_request_id()
    logger.error("api.unhandled_exception", request_id=rid, error=str(exc), path=request.url.path)
    
    # Check for development mode from environment
    config = get_settings()
    is_dev = config.log_level == "DEBUG"
    
    return JSONResponse(
        status_code=500,
        content=create_error_response(rid, "Internal server error", str(exc), is_dev)
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
    rid = _generate_request_id()
    if ticket_client is None:
        raise HTTPException(
            status_code=503, 
            detail=create_error_response(rid, "Ticket service unavailable")
        )
    if odoo_client is None:
        raise HTTPException(
            status_code=503, 
            detail=create_error_response(rid, "Invoice service unavailable")
        )


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
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=create_error_response(rid, "Ticket not found"))
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(status_code=502, detail=create_error_response(rid, "Ticket service error"))
    except pybreaker.CircuitBreakerError:
        # Let global exception handler handle this
        raise
    except Exception as e:
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to retrieve ticket"))


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
        logger.error("api.list_tickets_failed", error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to list tickets"))


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
        logger.error("api.create_ticket_failed", error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to create ticket"))


@app.post("/tickets/{ticket_id}/resolve")
async def api_resolve_ticket(ticket_id: str, request: ResolveTicketRequest):
    """Resolve a ticket."""
    _check_clients()
    rid = _generate_request_id()
    logger.info("api.resolve_ticket", ticket_id=ticket_id, billable=request.billable_amount, request_id=rid)
    try:
        await ticket_client.resolve_ticket(
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
        logger.error("api.resolve_ticket_failed", error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to resolve ticket"))


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
        logger.error("api.get_invoice_failed", error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to retrieve invoice"))


@app.post("/invoices/{ticket_id}")
async def api_create_invoice(ticket_id: str, request: CreateInvoiceRequest):
    """Create an invoice for a ticket."""
    _check_clients()
    if mq_publisher is None:
        rid = _generate_request_id()
        raise HTTPException(status_code=503, detail=create_error_response(rid, "Invoice service unavailable"))
    
    rid = _generate_request_id()
    logger.info("api.create_invoice", ticket_id=ticket_id, amount=request.amount, request_id=rid)
    
    try:
        ticket = await ticket_client.get_ticket(ticket_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=create_error_response(rid, "Ticket not found"))
        logger.error("api.create_invoice.ticket_lookup_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(status_code=502, detail=create_error_response(rid, "Failed to retrieve ticket"))
    except Exception as e:
        logger.error("api.create_invoice.ticket_lookup_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to retrieve ticket"))
    
    # Use envelope format matching server.py and agent.py
    event = {
        "eventType": "invoice.create_requested",
        "timestamp": datetime.now().isoformat(),
        "source": "agentic-service",
        "data": {
            "ticket_id": ticket_id,
            "customer_email": ticket.get("customerEmail"),
            "customer_name": ticket.get("customerName"),
            "amount": request.amount,
            "description": request.description or ticket.get("description", "Service"),
            "requested_at": datetime.now().isoformat()
        }
    }

    try:
        await mq_publisher.publish("invoice.create_requested", event)
    except Exception as e:
        logger.error("api.create_invoice.publish_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(status_code=503, detail=create_error_response(rid, "Failed to queue invoice creation"))

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
        rid = _generate_request_id()
        raise HTTPException(status_code=503, detail=create_error_response(rid, "Email service unavailable"))
    
    rid = _generate_request_id()
    logger.info("api.send_email", to=request.to_email, subject=request.subject, request_id=rid)
    
    # Use envelope format matching server.py
    event = {
        "eventType": "email.send",
        "timestamp": datetime.now().isoformat(),
        "source": "agentic-service",
        "data": {
            "to_email": request.to_email,
            "from_name": request.from_name,
            "subject": request.subject,
            "body_html": request.body,
            "sent_at": datetime.now().isoformat()
        }
    }

    try:
        await mq_publisher.publish("email.send", event)
    except Exception as e:
        logger.error("api.send_email.publish_failed", to=request.to_email, error=str(e), request_id=rid)
        raise HTTPException(status_code=503, detail=create_error_response(rid, "Failed to queue email"))

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
    except ValueError:
        raise HTTPException(status_code=400, detail=create_error_response(rid, "Invalid email format"))
    except Exception as e:
        logger.error("api.get_customer_context_failed", error=str(e), request_id=rid)
        raise HTTPException(status_code=500, detail=create_error_response(rid, "Failed to retrieve customer context"))


def main():
    """Run the REST API server."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)


if __name__ == "__main__":
    main()
