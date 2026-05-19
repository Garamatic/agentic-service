"""REST API Gateway for Garamatic Agentic Service."""

import contextlib
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pybreaker
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from .clients import OdooBridgeClient, RabbitMQPublisher, TicketClient
from .config import get_settings
from .server import _generate_request_id, _get_customer_context_internal, _validate_email

logger = structlog.get_logger()


# Global clients
ticket_client: TicketClient | None = None
odoo_client: OdooBridgeClient | None = None
mq_publisher: RabbitMQPublisher | None = None


def create_error_response(
    request_id: str,
    message: str,
    internal_error: str | None = None,
    is_development: bool = False,
) -> dict:
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

    for client in (ticket_client, odoo_client, mq_publisher):
        if client:
            with contextlib.suppress(Exception):
                await client.close()
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
        content=create_error_response(
            rid, "Service temporarily unavailable. Please try again later."
        )
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle all unhandled exceptions with sanitized responses."""
    rid = _generate_request_id()
    logger.error("api.unhandled_exception", request_id=rid, error=str(exc), path=request.url.path)

    # Check for development mode from environment
    config = get_settings()
    is_dev = config.log_level.upper() == "DEBUG"

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


def _check_ticket_client(rid: str) -> None:
    """Validate that ticket client is initialized."""
    if ticket_client is None:
        raise HTTPException(
            status_code=503,
            detail=create_error_response(rid, "Ticket service unavailable")
        )


def _check_odoo_client(rid: str) -> None:
    """Validate that odoo client is initialized."""
    if odoo_client is None:
        raise HTTPException(
            status_code=503,
            detail=create_error_response(rid, "Invoice service unavailable")
        )


@app.get("/tickets/{ticket_id}")
async def api_get_ticket(ticket_id: str):
    """Get ticket details."""
    rid = _generate_request_id()
    _check_ticket_client(rid)
    logger.info("api.get_ticket", ticket_id=ticket_id, request_id=rid)
    try:
        result = await ticket_client.get_ticket(ticket_id)
        return {
            "ticket_id": ticket_id,
            "status": result.get("status") or "unknown",
            "customer_email": result.get("customerId"),
            "customer_name": result.get("customerName"),
            "title": result.get("title") or "Untitled",
            "description": result.get("description"),
            "created_at": result.get("createdAt"),
            "resolved_at": result.get("resolvedAt"),
            "resolution_notes": result.get("resolutionNotes"),
            "billable_amount": result.get("billableAmount")
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=create_error_response(rid, "Ticket not found")
            ) from None
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(
            status_code=502,
            detail=create_error_response(rid, "Ticket service error")
        ) from None
    except pybreaker.CircuitBreakerError:
        # Let global exception handler handle this
        raise
    except Exception as e:
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(rid, "Failed to retrieve ticket")
        ) from None


@app.get("/tickets")
async def api_list_tickets(
    customer_email: EmailStr | None = None,
    status: str | None = Query(None, pattern="^(open|resolved|closed)$"),
    limit: int = Query(10, ge=1, le=100)
):
    """List tickets with optional filters."""
    rid = _generate_request_id()
    _check_ticket_client(rid)
    logger.info("api.list_tickets", email=customer_email, status=status, request_id=rid)
    try:
        result = await ticket_client.list_tickets(
            customer_id=customer_email,
            status=status,
            limit=limit
        )
        return [
            {
                "ticket_id": t.get("id"),
                "title": t.get("title") or "Untitled",
                "status": t.get("status") or "unknown",
                "created_at": t.get("createdAt"),
                "resolved_at": t.get("resolvedAt") or t.get("completedAt")
            }
            for t in result
        ]
    except pybreaker.CircuitBreakerError:
        raise
    except Exception as e:
        logger.error("api.list_tickets_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to list tickets")
        ) from None


@app.post("/tickets")
async def api_create_ticket(request: CreateTicketRequest):
    """Create a new ticket."""
    rid = _generate_request_id()
    _check_ticket_client(rid)
    logger.info(
        "api.create_ticket", title=request.title, customer=request.customer_email, request_id=rid
    )
    try:
        result = await ticket_client.create_ticket(
            title=request.title,
            description=request.description,
            customer_id=request.customer_email,
            customer_name=request.customer_name,
            priority=request.priority
        )
        ticket_id = result.get("id")
        if not ticket_id:
            logger.error("api.create_ticket.no_id", request_id=rid)
            raise HTTPException(
                status_code=500,
                detail=create_error_response(rid, "Ticket created but no ID returned")
            )
        return {
            "ticket_id": ticket_id,
            "status": "created",
            "customer_email": request.customer_email,
            "priority": request.priority,
            "message": f"Ticket {ticket_id} created successfully"
        }
    except pybreaker.CircuitBreakerError:
        raise
    except Exception as e:
        logger.error("api.create_ticket_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to create ticket")
        ) from None


@app.post("/tickets/{ticket_id}/resolve")
async def api_resolve_ticket(ticket_id: str, request: ResolveTicketRequest):
    """Resolve a ticket."""
    rid = _generate_request_id()
    _check_ticket_client(rid)
    logger.info(
        "api.resolve_ticket", ticket_id=ticket_id, billable=request.billable_amount, request_id=rid
    )
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
            if mq_publisher is not None:
                payload = {
                    "event_type": "invoice.create_requested",
                    "ticket_id": ticket_id,
                    "customer_email": result.get("customerId"),
                    "customer_name": result.get("customerName"),
                    "amount": request.billable_amount,
                    "description": request.resolution_notes[:100],
                    "requested_at": datetime.now().isoformat()
                }
                try:
                    await mq_publisher.publish("invoice.create_requested", payload)
                    response["invoice"] = {
                        "status": "queued",
                        "estimated_amount": request.billable_amount
                    }
                except Exception as e:
                    logger.error(
                        "api.resolve_ticket.publish_failed",
                        ticket_id=ticket_id, error=str(e), request_id=rid
                    )
                    response["invoice"] = {
                        "status": "unavailable",
                        "estimated_amount": request.billable_amount
                    }
            else:
                response["invoice"] = {
                    "status": "unavailable",
                    "estimated_amount": request.billable_amount
                }
        return response
    except pybreaker.CircuitBreakerError:
        raise
    except Exception as e:
        logger.error("api.resolve_ticket_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to resolve ticket")
        ) from None


@app.get("/invoices/by-ticket/{ticket_id}")
async def api_get_invoice(ticket_id: str):
    """Get invoice for a ticket."""
    rid = _generate_request_id()
    _check_odoo_client(rid)
    logger.info("api.get_invoice", ticket_id=ticket_id, request_id=rid)
    try:
        result = await odoo_client.get_invoice_status(ticket_id)
        if not result:
            return {"ticket_id": ticket_id, "status": "not_found"}
        return {
            "ticket_id": ticket_id,
            "invoice_id": result.get("invoice_id"),
            "status": result.get("status") or "unknown",
            "amount": result.get("amount"),
            "created_at": result.get("created_at"),
            "paid_at": result.get("paid_at")
        }
    except pybreaker.CircuitBreakerError:
        raise
    except Exception as e:
        logger.error("api.get_invoice_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to retrieve invoice")
        ) from None


@app.post("/invoices/{ticket_id}")
async def api_create_invoice(ticket_id: str, request: CreateInvoiceRequest):
    """Create an invoice for a ticket."""
    rid = _generate_request_id()
    _check_ticket_client(rid)
    if mq_publisher is None:
        raise HTTPException(
            status_code=503, detail=create_error_response(rid, "Invoice service unavailable")
        )

    logger.info(
        "api.create_invoice", ticket_id=ticket_id, amount=request.amount, request_id=rid
    )

    try:
        ticket = await ticket_client.get_ticket(ticket_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(
                status_code=404, detail=create_error_response(rid, "Ticket not found")
            ) from None
        logger.error(
            "api.create_invoice.ticket_lookup_failed",
            ticket_id=ticket_id, error=str(e), request_id=rid
        )
        raise HTTPException(
            status_code=502, detail=create_error_response(rid, "Failed to retrieve ticket")
        ) from None
    except pybreaker.CircuitBreakerError:
        raise
    except Exception as e:
        logger.error(
            "api.create_invoice.ticket_lookup_failed",
            ticket_id=ticket_id, error=str(e), request_id=rid
        )
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to retrieve ticket")
        ) from None

    # Flat format per integration-contracts schema
    event = {
        "event_type": "invoice.create_requested",
        "ticket_id": ticket_id,
        "customer_email": ticket.get("customerId"),
        "customer_name": ticket.get("customerName") or ticket.get("title") or "Customer",
        "amount": request.amount,
        "description": request.description or ticket.get("description") or "Service",
        "requested_at": datetime.now().isoformat()
    }

    try:
        await mq_publisher.publish("invoice.create_requested", event)
    except Exception as e:
        logger.error(
            "api.create_invoice.publish_failed",
            ticket_id=ticket_id, error=str(e), request_id=rid
        )
        raise HTTPException(
            status_code=503, detail=create_error_response(rid, "Failed to queue invoice creation")
        ) from None

    return {
        "status": "queued",
        "ticket_id": ticket_id,
        "amount": request.amount,
        "customer_email": ticket.get("customerId"),
        "message": "Invoice creation queued"
    }


@app.post("/emails")
async def api_send_email(request: SendEmailRequest):
    """Send an email."""
    rid = _generate_request_id()
    if mq_publisher is None:
        raise HTTPException(
            status_code=503, detail=create_error_response(rid, "Email service unavailable")
        )

    logger.info(
        "api.send_email", to=request.to_email, subject=request.subject, request_id=rid
    )

    # Flat format per integration-contracts email.send schema
    event = {
        "event_type": "email.send",
        "to_email": request.to_email,
        "from_email": "system@garamatic.com",
        "from_name": request.from_name,
        "subject": request.subject,
        "body_html": request.body
    }

    try:
        await mq_publisher.publish("email.send", event)
    except Exception as e:
        logger.error(
            "api.send_email.publish_failed", to=request.to_email, error=str(e), request_id=rid
        )
        raise HTTPException(
            status_code=503, detail=create_error_response(rid, "Failed to queue email")
        ) from None

    return {
        "status": "queued",
        "to_email": request.to_email,
        "subject": request.subject,
        "message": "Email queued for sending"
    }


@app.get("/customers/{email}/context")
async def api_get_customer_context(email: str):
    """Get full customer context."""
    rid = _generate_request_id()
    _check_ticket_client(rid)
    _check_odoo_client(rid)
    logger.info("api.get_customer_context", email=email, request_id=rid)
    try:
        _validate_email(email)
        return await _get_customer_context_internal(email)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=create_error_response(rid, "Invalid email format")
        ) from None
    except pybreaker.CircuitBreakerError:
        raise
    except Exception as e:
        logger.error("api.get_customer_context_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(rid, "Failed to retrieve customer context"),
        ) from None


def main():
    """Run the REST API server."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)


if __name__ == "__main__":
    main()
