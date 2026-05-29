"""REST API Gateway for Garamatic Agentic Service."""

from contextlib import asynccontextmanager

import pybreaker
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from .config import get_settings
from .service import (
    GaramaticService,
    NotFoundError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
    _generate_request_id,
    create_facade_service,
)

logger = structlog.get_logger()


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
        response["debug"] = internal_error
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
    """Initialize and cleanup service.

    Uses create_facade_service() to wire the unified AgenticService facade
    with the GaramaticService business layer.
    """
    facade, service = await create_facade_service()
    app.state.service = service

    logger.info("api.started")
    yield

    await facade.close()
    logger.info("api.stopped")


app = FastAPI(
    title="Garamatic Agentic API",
    description="REST API for ticket, invoice, and email operations",
    version="0.2.0",
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


@app.exception_handler(ServiceUnavailableError)
async def service_unavailable_handler(request: Request, exc: ServiceUnavailableError):
    """Handle service not initialized."""
    rid = exc.request_id
    logger.warning("api.service_unavailable", request_id=rid, path=request.url.path)

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

    config = get_settings()
    is_dev = config.log_level.upper() == "DEBUG"

    return JSONResponse(
        status_code=500,
        content=create_error_response(rid, "Internal server error", str(exc), is_dev)
    )


def _get_service() -> GaramaticService:
    """Get the business-layer service from app state."""
    service = getattr(app.state, "service", None)
    if service is None:
        raise ServiceUnavailableError("Service not initialized")
    return service


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    service = _get_service()
    health = await service.health_check()
    return {
        "status": health.status,
        "services": health.services,
    }


@app.get("/tickets/{ticket_id}")
async def api_get_ticket(ticket_id: str):
    """Get ticket details."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info("api.get_ticket", ticket_id=ticket_id, request_id=rid)
    try:
        ticket = await service.get_ticket(ticket_id)
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=create_error_response(rid, "Ticket not found")
        ) from None
    except UpstreamError as e:
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(rid, "Failed to retrieve ticket")
        ) from None
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.get_ticket_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(rid, "Failed to retrieve ticket")
        ) from None

    return {
        "ticket_id": ticket_id,
        "status": ticket.status,
        "customer_email": ticket.customer_email,
        "customer_name": ticket.customer_name,
        "title": ticket.title,
        "description": ticket.description,
        "created_at": ticket.created_at,
        "resolved_at": ticket.resolved_at,
        "resolution_notes": ticket.resolution_notes,
        "billable_amount": ticket.billable_amount,
    }


@app.get("/tickets")
async def api_list_tickets(
    customer_email: EmailStr | None = None,
    status: str | None = Query(None, pattern="^(open|resolved|closed)$"),
    limit: int = Query(10, ge=1, le=100)
):
    """List tickets with optional filters."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info("api.list_tickets", email=customer_email, status=status, request_id=rid)
    try:
        tickets = await service.list_tickets(
            customer_email=customer_email,
            status=status,  # type: ignore[arg-type]
            limit=limit,
        )
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.list_tickets_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to list tickets")
        ) from None

    return [
        {
            "ticket_id": t.id,
            "title": t.title,
            "status": t.status,
            "created_at": t.created_at,
            "resolved_at": t.resolved_at,
        }
        for t in tickets
    ]


@app.post("/tickets")
async def api_create_ticket(request: CreateTicketRequest):
    """Create a new ticket."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info(
        "api.create_ticket", title=request.title, customer=request.customer_email, request_id=rid
    )
    try:
        ticket = await service.create_ticket(
            title=request.title,
            description=request.description,
            customer_email=request.customer_email,
            customer_name=request.customer_name,
            priority=request.priority,  # type: ignore[arg-type]
        )
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.create_ticket_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to create ticket")
        ) from None

    ticket_id = ticket.id
    if not ticket_id or ticket_id == "N/A":
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


@app.post("/tickets/{ticket_id}/resolve")
async def api_resolve_ticket(ticket_id: str, request: ResolveTicketRequest):
    """Resolve a ticket."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info(
        "api.resolve_ticket", ticket_id=ticket_id, billable=request.billable_amount, request_id=rid
    )
    try:
        result = await service.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=request.resolution_notes,
            billable_amount=request.billable_amount,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=create_error_response(rid, "Ticket not found")
        ) from None
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.resolve_ticket_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to resolve ticket")
        ) from None

    response = {
        "ticket_id": ticket_id,
        "status": "resolved",
        "resolution_notes": request.resolution_notes,
    }
    if result.invoice is not None:
        response["invoice"] = {
            "status": result.invoice.status,
            "estimated_amount": result.invoice.estimated_amount,
        }
    return response


@app.get("/invoices/by-ticket/{ticket_id}")
async def api_get_invoice(ticket_id: str):
    """Get invoice for a ticket."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info("api.get_invoice", ticket_id=ticket_id, request_id=rid)
    try:
        invoice = await service.get_invoice_status(ticket_id)
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.get_invoice_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500, detail=create_error_response(rid, "Failed to retrieve invoice")
        ) from None

    if invoice.status == "not_found":
        raise HTTPException(
            status_code=404,
            detail=create_error_response(rid, "Invoice not found for this ticket")
        )

    return {
        "ticket_id": ticket_id,
        "invoice_id": invoice.invoice_id,
        "status": invoice.status,
        "amount": invoice.amount,
        "created_at": invoice.created_at,
        "paid_at": invoice.paid_at,
    }


@app.post("/invoices/{ticket_id}")
async def api_create_invoice(ticket_id: str, request: CreateInvoiceRequest):
    """Create an invoice for a ticket."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info(
        "api.create_invoice", ticket_id=ticket_id, amount=request.amount, request_id=rid
    )
    try:
        result = await service.queue_invoice(
            ticket_id=ticket_id,
            amount=request.amount,
            description=request.description,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail=create_error_response(rid, "Ticket not found")
        ) from None
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.create_invoice_failed", ticket_id=ticket_id, error=str(e), request_id=rid)
        raise HTTPException(
            status_code=503, detail=create_error_response(rid, "Failed to queue invoice creation")
        ) from None

    return {
        "status": result.status,
        "ticket_id": ticket_id,
        "amount": request.amount,
        "message": result.message,
    }


@app.post("/emails")
async def api_send_email(request: SendEmailRequest):
    """Send an email."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info(
        "api.send_email", to=request.to_email, subject=request.subject, request_id=rid
    )
    try:
        result = await service.send_email(
            to_email=request.to_email,
            subject=request.subject,
            body=request.body,
            from_name=request.from_name,
        )
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.send_email_failed", to=request.to_email, error=str(e), request_id=rid)
        raise HTTPException(
            status_code=503, detail=create_error_response(rid, "Failed to queue email")
        ) from None

    return {
        "status": result.status,
        "to_email": request.to_email,
        "subject": request.subject,
        "message": result.message,
    }


@app.get("/customers/{email}/context")
async def api_get_customer_context(email: str):
    """Get full customer context."""
    service = _get_service()
    rid = _generate_request_id()
    logger.info("api.get_customer_context", email=email, request_id=rid)
    try:
        context = await service.get_customer_context(email)
    except ValidationError as e:
        raise HTTPException(
            status_code=400, detail=create_error_response(rid, "Invalid email format")
        ) from e
    except Exception as e:
        if isinstance(e, pybreaker.CircuitBreakerError):
            raise
        logger.error("api.get_customer_context_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(rid, "Failed to retrieve customer context"),
        ) from None

    return {
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
# Agent chat endpoint
# ------------------------------------------------------------------

class ChatRequest(BaseModel):
    """Request to chat with the AI agent."""
    message: str = Field(..., min_length=1, max_length=2000)
    thread_id: str = Field(default="web-session")


@app.post("/agent/chat")
async def api_agent_chat(request: ChatRequest):
    """Chat with the Garamatic AI agent.

    The agent can answer questions about tickets, customers, and invoices,
    and can perform actions like resolving tickets or creating invoices.
    """
    import os
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail={"error": "Agent unavailable", "message": "OPENAI_API_KEY not configured"}
        )

    from .agent import GaramaticAgent

    rid = _generate_request_id()
    logger.info("api.agent_chat", thread_id=request.thread_id, request_id=rid)

    agent = None
    try:
        agent = GaramaticAgent(thread_id=request.thread_id)
        response = await agent.process(request.message)
    except Exception as e:
        logger.error("api.agent_chat_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail={"error": "Agent failed", "message": str(e)}
        ) from None
    finally:
        if agent is not None:
            await agent.close()

    return {
        "thread_id": request.thread_id,
        "response": response,
        "request_id": rid,
    }


def main():
    """Run the REST API server."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)


if __name__ == "__main__":
    main()
