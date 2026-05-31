"""REST API Gateway for Garamatic Agentic Service."""

from contextlib import asynccontextmanager

import functools
from typing import Any, Callable

import pybreaker
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

import os

from .agent import GaramaticAgent
from .config import get_settings
from .service import (
    GaramaticService,
    NotFoundError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
    generate_request_id,
    create_service,
)

# Agent cache bound to prevent unbounded memory growth
MAX_AGENT_CACHE = 100

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


def api_error_handler(
    operation: str | None = None,
    not_found_message: str = "Not found",
    validation_message: str = "Invalid input",
) -> Callable[..., Callable[..., Any]]:
    """Decorator that wraps API endpoints with error handling.

    Maps service-level exceptions to HTTPExceptions:
    NotFoundError → 404, ValidationError → 400, UpstreamError → 500.
    Passes through HTTPException, ServiceUnavailableError, CircuitBreakerError.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        op_name = operation or func.__name__.removeprefix("api_")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            rid = generate_request_id()
            logger.info(f"api.{op_name}", request_id=rid, **kwargs)
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                raise
            except ServiceUnavailableError:
                raise
            except NotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=create_error_response(rid, not_found_message)
                ) from None
            except ValidationError:
                raise HTTPException(
                    status_code=400,
                    detail=create_error_response(rid, validation_message)
                ) from None
            except UpstreamError as e:
                logger.error(f"api.{op_name}_failed", error=str(e), request_id=rid)
                raise HTTPException(
                    status_code=500,
                    detail=create_error_response(rid, "Failed to process request")
                ) from None
            except pybreaker.CircuitBreakerError:
                raise
            except Exception as e:
                logger.error(f"api.{op_name}_failed", error=str(e), request_id=rid)
                raise HTTPException(
                    status_code=500,
                    detail=create_error_response(rid, "Failed to process request")
                ) from None
        return wrapper
    return decorator


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

    Uses create_service() as the single composition root.
    """
    service = await create_service()
    app.state.service = service
    app.state.agent_cache = {}

    logger.info("api.started")
    yield

    # Close all cached agent instances
    agent_cache = getattr(app.state, "agent_cache", {})
    for agent in agent_cache.values():
        await agent.close()
    app.state.agent_cache = {}

    await service.close()
    logger.info("api.stopped")


app = FastAPI(
    title="Garamatic Agentic API",
    description="REST API for ticket, invoice, and email operations",
    version="0.2.0",
    lifespan=lifespan
)

# Allow cross-origin requests from the Ticket Masala web UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(pybreaker.CircuitBreakerError)
async def circuit_breaker_handler(request: Request, exc: pybreaker.CircuitBreakerError):
    """Handle circuit breaker open state."""
    rid = generate_request_id()
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
    rid = generate_request_id()
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
@api_error_handler("health_check")
async def health_check():
    """Health check endpoint."""
    service = _get_service()
    health = await service.health_check()
    return {
        "status": health.status,
        "services": health.services,
    }


@app.get("/tickets/{ticket_id}")
@api_error_handler("get_ticket", not_found_message="Ticket not found")
async def api_get_ticket(ticket_id: str):
    """Get ticket details."""
    service = _get_service()
    ticket = await service.get_ticket(ticket_id)
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
@api_error_handler("list_tickets")
async def api_list_tickets(
    customer_email: EmailStr | None = None,
    status: str | None = Query(None, pattern="^(open|resolved|closed)$"),
    limit: int = Query(10, ge=1, le=100)
):
    """List tickets with optional filters."""
    service = _get_service()
    tickets = await service.list_tickets(
        customer_email=customer_email,
        status=status,  # type: ignore[arg-type]
        limit=limit,
    )
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
@api_error_handler("create_ticket")
async def api_create_ticket(request: CreateTicketRequest):
    """Create a new ticket."""
    service = _get_service()
    ticket = await service.create_ticket(
        title=request.title,
        description=request.description,
        customer_email=request.customer_email,
        customer_name=request.customer_name,
        priority=request.priority,  # type: ignore[arg-type]
    )

    ticket_id = ticket.id
    if not ticket_id or ticket_id == "N/A":
        raise HTTPException(
            status_code=500,
            detail=create_error_response(generate_request_id(), "Ticket created but no ID returned")
        )

    return {
        "ticket_id": ticket_id,
        "status": "created",
        "customer_email": request.customer_email,
        "priority": request.priority,
        "message": f"Ticket {ticket_id} created successfully"
    }


@app.post("/tickets/{ticket_id}/resolve")
@api_error_handler("resolve_ticket", not_found_message="Ticket not found")
async def api_resolve_ticket(ticket_id: str, request: ResolveTicketRequest):
    """Resolve a ticket."""
    service = _get_service()
    result = await service.resolve_ticket(
        ticket_id=ticket_id,
        resolution_notes=request.resolution_notes,
        billable_amount=request.billable_amount,
    )

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
@api_error_handler("get_invoice", not_found_message="Invoice not found for this ticket")
async def api_get_invoice(ticket_id: str):
    """Get invoice for a ticket."""
    service = _get_service()
    invoice = await service.get_invoice_status(ticket_id)

    if invoice.status == "not_found":
        raise HTTPException(
            status_code=404,
            detail=create_error_response(generate_request_id(), "Invoice not found for this ticket")
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
@api_error_handler("create_invoice", not_found_message="Ticket not found")
async def api_create_invoice(ticket_id: str, request: CreateInvoiceRequest):
    """Create an invoice for a ticket."""
    service = _get_service()
    result = await service.queue_invoice(
        ticket_id=ticket_id,
        amount=request.amount,
        description=request.description,
    )

    return {
        "status": result.status,
        "ticket_id": ticket_id,
        "amount": request.amount,
        "message": result.message,
    }


@app.post("/emails")
@api_error_handler("send_email")
async def api_send_email(request: SendEmailRequest):
    """Send an email."""
    service = _get_service()
    result = await service.send_email(
        to_email=request.to_email,
        subject=request.subject,
        body=request.body,
        from_name=request.from_name,
    )

    return {
        "status": result.status,
        "to_email": request.to_email,
        "subject": request.subject,
        "message": result.message,
    }


@app.get("/customers/{email}/context")
@api_error_handler("get_customer_context", validation_message="Invalid email format")
async def api_get_customer_context(email: str):
    """Get full customer context."""
    service = _get_service()
    context = await service.get_customer_context(email)

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
    """Chat with the Garamatic AI agent."""
    rid = generate_request_id()
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail=create_error_response(rid, "Agent unavailable", "OPENAI_API_KEY not configured")
        )

    service = _get_service()
    logger.info("api.agent_chat", thread_id=request.thread_id, request_id=rid)

    agent_cache = getattr(app.state, "agent_cache", {})
    agent = agent_cache.get(request.thread_id)
    if agent is None:
        agent = GaramaticAgent(service=service, thread_id=request.thread_id)
        agent_cache[request.thread_id] = agent
        while len(agent_cache) > MAX_AGENT_CACHE:
            oldest_thread_id = next(iter(agent_cache))
            oldest_agent = agent_cache.pop(oldest_thread_id)
            try:
                await oldest_agent.close()
            except Exception:
                logger.warning("api.agent_cache_evict_failed", thread_id=oldest_thread_id)
        app.state.agent_cache = agent_cache

    try:
        response = await agent.process(request.message)
    except ServiceUnavailableError:
        raise HTTPException(
            status_code=503,
            detail=create_error_response(rid, "Service temporarily unavailable")
        ) from None
    except Exception as e:
        logger.error("api.agent_chat_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(rid, "Agent failed", str(e), get_settings().log_level.upper() == "DEBUG")
        ) from None

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
