"""REST API Gateway for Garamatic Agentic Service."""

import functools
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import pybreaker
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, EmailStr, Field

from .agent import GaramaticAgent
from .approval import ApprovalRequiredError
from .config import get_settings
from .service import (
    GaramaticService,
    NotFoundError,
    ServiceUnavailableError,
    UpstreamError,
    ValidationError,
    create_service,
    generate_request_id,
)
from .thread_store import ThreadStore

# Agent cache bound to prevent unbounded memory growth
MAX_AGENT_CACHE = 100

logger = structlog.get_logger()


def create_error_response(
    request_id: str,
    message: str,
    internal_error: str | None = None,
    is_development: bool = False,
) -> dict[str, Any]:
    """Create sanitized error response.

    In production: Only show generic message + request_id for tracing
    In development: Include internal details
    """
    response = {"error": message, "request_id": request_id}
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
                    status_code=404, detail=create_error_response(rid, not_found_message)
                ) from None
            except ValidationError:
                raise HTTPException(
                    status_code=400, detail=create_error_response(rid, validation_message)
                ) from None
            except ApprovalRequiredError as e:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": str(e),
                        "approval_id": e.approval_id,
                        "request_id": rid,
                    },
                ) from None
            except UpstreamError as e:
                logger.error(f"api.{op_name}_failed", error=str(e), request_id=rid)
                raise HTTPException(
                    status_code=500, detail=create_error_response(rid, "Failed to process request")
                ) from None
            except pybreaker.CircuitBreakerError:
                raise
            except Exception as e:
                logger.error(f"api.{op_name}_failed", error=str(e), request_id=rid)
                raise HTTPException(
                    status_code=500, detail=create_error_response(rid, "Failed to process request")
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
    approval_id: str | None = Field(default=None)


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
async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """Initialize and cleanup service.

    Uses create_service() as the single composition root.
    """
    service = await create_service()
    app.state.service = service
    app.state.agent_cache = {}

    # Thread store for conversation history
    config = get_settings()
    db_path = config.agent_db_path or "/app/data/approvals.db"
    app.state.thread_store = ThreadStore(db_path)
    await app.state.thread_store._ensure_table()

    logger.info("api.started")
    yield {}

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
    lifespan=lifespan,
)

# OpenTelemetry tracing
_otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4318")
if _otel_endpoint:
    _resource = Resource(attributes={SERVICE_NAME: "agentic-service"})
    _provider = TracerProvider(resource=_resource)
    _processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{_otel_endpoint}/v1/traces")
    )
    _provider.add_span_processor(_processor)
    trace.set_tracer_provider(_provider)
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

# Prometheus metrics endpoint
@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Allow cross-origin requests based on configuration
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Ensure every response has an X-Request-ID header."""
    rid = request.headers.get("X-Request-ID", generate_request_id())
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


# ------------------------------------------------------------------
# Auth middleware — protects all routes except /docs, /openapi.json, /health
# ------------------------------------------------------------------


@app.middleware("http")
async def auth_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Check API key on protected endpoints."""
    # Public paths — no auth needed
    public_paths = {"/docs", "/openapi.json", "/redoc", "/health", "/metrics"}
    if request.url.path in public_paths or request.url.path.startswith("/openapi.json") or request.url.path.startswith("/agent/"):
        return await call_next(request)

    settings = get_settings()
    expected = settings.api_key.get_secret_value()
    if not expected:
        # No API key configured — allow all requests
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if token != expected:
        rid = generate_request_id()
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing API key", "request_id": rid},
            headers={"X-Request-ID": rid},
        )

    return await call_next(request)


@app.exception_handler(pybreaker.CircuitBreakerError)
async def circuit_breaker_handler(
    request: Request, exc: pybreaker.CircuitBreakerError
) -> JSONResponse:
    """Handle circuit breaker open state."""
    rid = generate_request_id()
    logger.warning("api.circuit_breaker_open", request_id=rid, path=request.url.path)

    return JSONResponse(
        status_code=503,
        content=create_error_response(
            rid, "Service temporarily unavailable. Please try again later."
        ),
    )


@app.exception_handler(ServiceUnavailableError)
async def service_unavailable_handler(
    request: Request, exc: ServiceUnavailableError
) -> JSONResponse:
    """Handle service not initialized."""
    rid = exc.request_id
    logger.warning("api.service_unavailable", request_id=rid, path=request.url.path)

    return JSONResponse(
        status_code=503,
        content=create_error_response(
            rid, "Service temporarily unavailable. Please try again later."
        ),
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle all unhandled exceptions with sanitized responses."""
    rid = generate_request_id()
    logger.error("api.unhandled_exception", request_id=rid, error=str(exc), path=request.url.path)

    config = get_settings()
    is_dev = config.log_level.upper() == "DEBUG"

    return JSONResponse(
        status_code=500,
        content=create_error_response(rid, "Internal server error", str(exc), is_dev),
    )


def _get_service() -> GaramaticService:
    """Get the business-layer service from app state."""
    service: Any = getattr(app.state, "service", None)
    if service is None or not isinstance(service, GaramaticService):
        raise ServiceUnavailableError("Service not initialized")
    return cast(GaramaticService, service)


def _get_thread_store() -> ThreadStore:
    """Get the thread store from app state."""
    store: Any = getattr(app.state, "thread_store", None)
    if store is None:
        raise ServiceUnavailableError("Thread store not initialized")
    return cast(ThreadStore, store)


@app.get("/health")
@api_error_handler("health_check")
async def health_check() -> dict[str, Any]:
    """Health check endpoint."""
    service = _get_service()
    health = await service.health_check()
    return {
        "status": health.status,
        "services": health.services,
    }


@app.get("/tickets/{ticket_id}")
@api_error_handler("get_ticket", not_found_message="Ticket not found")
async def api_get_ticket(ticket_id: str) -> dict[str, Any]:
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
    limit: int = Query(10, ge=1, le=100),
) -> list[dict[str, Any]]:
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
async def api_create_ticket(request: CreateTicketRequest) -> dict[str, Any]:
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
            detail=create_error_response(
                generate_request_id(), "Ticket created but no ID returned"
            ),
        )

    return {
        "ticket_id": ticket_id,
        "status": "created",
        "customer_email": request.customer_email,
        "priority": request.priority,
        "message": f"Ticket {ticket_id} created successfully",
    }


@app.post("/tickets/{ticket_id}/resolve")
@api_error_handler("resolve_ticket", not_found_message="Ticket not found")
async def api_resolve_ticket(ticket_id: str, request: ResolveTicketRequest) -> dict[str, Any]:
    """Resolve a ticket."""
    service = _get_service()
    result = await service.resolve_ticket(
        ticket_id=ticket_id,
        resolution_notes=request.resolution_notes,
        billable_amount=request.billable_amount,
        approval_id=request.approval_id,
        approval_store=service.approval_store,
    )

    response: dict[str, Any] = {
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
async def api_get_invoice(ticket_id: str) -> dict[str, Any]:
    """Get invoice for a ticket."""
    service = _get_service()
    invoice = await service.get_invoice_status(ticket_id)

    if invoice.status == "not_found":
        raise HTTPException(
            status_code=404,
            detail=create_error_response(
                generate_request_id(), "Invoice not found for this ticket"
            ),
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
async def api_create_invoice(ticket_id: str, request: CreateInvoiceRequest) -> dict[str, Any]:
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
async def api_send_email(request: SendEmailRequest) -> dict[str, Any]:
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
async def api_get_customer_context(email: str) -> dict[str, Any]:
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
    thread_id: str = Field(default="web-session", max_length=128)
    model: str = Field(default="", max_length=128, description="Optional model override. Uses default if empty.")


class ApprovalRequest(BaseModel):
    """Request to approve or reject an approval ID."""

    approval_id: str = Field(..., min_length=1, max_length=64)


@app.post("/agent/chat")
async def api_agent_chat(request: ChatRequest) -> dict[str, Any]:
    """Chat with the Garamatic AI agent."""
    rid = generate_request_id()
    config = get_settings()

    from .llm_adapter import validate_llm_config

    try:
        validate_llm_config(config)
    except ValueError as e:
        raise HTTPException(
            status_code=503,
            detail=create_error_response(rid, "Agent unavailable", str(e)),
        ) from e

    service = _get_service()
    logger.info("api.agent_chat", thread_id=request.thread_id, request_id=rid)

    agent_cache = getattr(app.state, "agent_cache", {})
    agent = agent_cache.get(request.thread_id)
    if agent is None or (request.model and agent._model != request.model):
        # Evict old agent if model changed
        if request.thread_id in agent_cache:
            old_agent = agent_cache.pop(request.thread_id)
            try:
                await old_agent.close()
            except Exception:
                pass
        agent = GaramaticAgent(service=service, thread_id=request.thread_id, model=request.model)
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
            status_code=503, detail=create_error_response(rid, "Service temporarily unavailable")
        ) from None
    except Exception as e:
        logger.error("api.agent_chat_failed", error=str(e), request_id=rid)
        raise HTTPException(
            status_code=500,
            detail=create_error_response(
                rid,
                "Agent failed",
                str(e),
                get_settings().log_level.upper() == "DEBUG",
            ),
        ) from None

    # Track thread metadata
    try:
        thread_store = _get_thread_store()
        title = request.message[:60] if len(request.message) <= 60 else request.message[:57] + "..."
        await thread_store.upsert(request.thread_id, title=title)
    except Exception:
        logger.warning("thread_store.upsert_failed", thread_id=request.thread_id)

    return {
        "thread_id": request.thread_id,
        "response": response,
        "request_id": rid,
    }


@app.post("/agent/chat/stream")
async def api_agent_chat_stream(request: ChatRequest) -> StreamingResponse:
    """Chat with the Garamatic AI agent — streaming with tool visibility."""
    rid = generate_request_id()
    config = get_settings()

    from .llm_adapter import validate_llm_config

    try:
        validate_llm_config(config)
    except ValueError as exc:
        error_msg = str(exc)
        async def _error_stream() -> AsyncIterator[str]:
            yield f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n"
        return StreamingResponse(_error_stream(), media_type="text/event-stream")

    service = _get_service()
    logger.info("api.agent_chat_stream", thread_id=request.thread_id, request_id=rid)

    agent_cache = getattr(app.state, "agent_cache", {})
    agent = agent_cache.get(request.thread_id)
    if agent is None or (request.model and agent._model != request.model):
        # Evict old agent if model changed
        if request.thread_id in agent_cache:
            old_agent = agent_cache.pop(request.thread_id)
            try:
                await old_agent.close()
            except Exception:
                pass
        agent = GaramaticAgent(service=service, thread_id=request.thread_id, model=request.model)
        agent_cache[request.thread_id] = agent
        while len(agent_cache) > MAX_AGENT_CACHE:
            oldest_thread_id = next(iter(agent_cache))
            oldest_agent = agent_cache.pop(oldest_thread_id)
            try:
                await oldest_agent.close()
            except Exception:
                logger.warning("api.agent_cache_evict_failed", thread_id=oldest_thread_id)
        app.state.agent_cache = agent_cache

    async def _event_generator() -> AsyncIterator[str]:
        try:
            async for event in agent.process_stream(request.message):
                event["request_id"] = rid
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.error("api.agent_chat_stream_failed", error=str(e), request_id=rid)
            yield f"event: error\ndata: {json.dumps({'error': str(e), 'request_id': rid})}\n\n"
            return

        # Track thread metadata
        try:
            thread_store = _get_thread_store()
            title = (
                request.message[:60]
                if len(request.message) <= 60
                else request.message[:57] + "..."
            )
            await thread_store.upsert(request.thread_id, title=title)
        except Exception:
            logger.warning("thread_store.upsert_failed", thread_id=request.thread_id)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Request-ID": rid},
    )


@app.get("/agent/threads")
@api_error_handler("list_threads")
async def api_list_threads(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List conversation threads with metadata."""
    store = _get_thread_store()
    threads = await store.list_threads(limit=limit, offset=offset)
    return {
        "threads": threads,
        "count": len(threads),
    }


@app.get("/agent/threads/{thread_id}")
@api_error_handler("get_thread", not_found_message="Thread not found")
async def api_get_thread(thread_id: str) -> dict[str, Any]:
    """Get a single thread's metadata and messages."""
    store = _get_thread_store()
    meta = await store.get_thread(thread_id)
    if meta is None:
        raise NotFoundError("Thread not found")
    messages = await store.get_messages(thread_id)
    return {
        "thread_id": thread_id,
        "metadata": meta,
        "messages": messages,
    }


@app.delete("/agent/threads/{thread_id}")
@api_error_handler("delete_thread", not_found_message="Thread not found")
async def api_delete_thread(thread_id: str) -> dict[str, Any]:
    """Delete a thread and its metadata."""
    store = _get_thread_store()
    deleted = await store.delete_thread(thread_id)
    if not deleted:
        raise NotFoundError("Thread not found")

    # Also evict from agent cache
    agent_cache = getattr(app.state, "agent_cache", {})
    if thread_id in agent_cache:
        agent = agent_cache.pop(thread_id)
        with suppress(Exception):
            await agent.close()
        app.state.agent_cache = agent_cache

    return {"thread_id": thread_id, "status": "deleted"}


@app.post("/approvals/{approval_id}/approve")
@api_error_handler("approve")
async def api_approve(approval_id: str) -> dict[str, Any]:
    """Approve a pending approval request."""
    service = _get_service()
    store = service.approval_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=create_error_response(generate_request_id(), "Approval store not available"),
        )
    success = await store.approve(approval_id)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=create_error_response(
                generate_request_id(), "Approval request not found or already processed"
            ),
        )
    return {"approval_id": approval_id, "status": "approved"}


@app.post("/approvals/{approval_id}/reject")
@api_error_handler("reject")
async def api_reject(approval_id: str) -> dict[str, Any]:
    """Reject a pending approval request."""
    service = _get_service()
    store = service.approval_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=create_error_response(generate_request_id(), "Approval store not available"),
        )
    success = await store.reject(approval_id)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=create_error_response(
                generate_request_id(), "Approval request not found or already processed"
            ),
        )
    return {"approval_id": approval_id, "status": "rejected"}


def main() -> None:
    """Run the REST API server."""
    import uvicorn

    config = get_settings()
    uvicorn.run(app, host=config.api_host, port=config.api_port)


if __name__ == "__main__":
    main()
