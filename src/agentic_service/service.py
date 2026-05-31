"""Deepened service layer for Garamatic Agentic Service.

Owns all business logic, orchestration, validation, logging, and graceful
degradation. Callers (MCP, FastAPI, LangGraph) only adapt results to their
native response formats.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Literal, TypeVar

import httpx
import pybreaker
import structlog

from .ports import MessageQueuePort, OdooClientPort, TicketClientPort

_T = TypeVar("_T")

logger = structlog.get_logger()

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def generate_request_id() -> str:
    """Generate unique request ID for tracing."""
    return uuid.uuid4().hex


def validate_email(email: str) -> None:
    """Validate email format, raise ValidationError if invalid."""
    if not email or not isinstance(email, str):
        raise ValidationError(f"Invalid email format: {email}")
    if "@" not in email:
        raise ValidationError(f"Invalid email format: {email}")
    local, _, domain = email.rpartition("@")
    if not local or not domain or "." not in domain:
        raise ValidationError(f"Invalid email format: {email}")
    if domain.startswith(".") or domain.endswith(".") or ".." in local:
        raise ValidationError(f"Invalid email format: {email}")


def format_date(d: str | None) -> str:
    """Format an ISO date string to YYYY-MM-DD or return placeholder."""
    return d[:10] if d and len(d) >= 10 else (d or "N/A")


# ------------------------------------------------------------------
# Exception taxonomy
# ------------------------------------------------------------------


class ServiceError(Exception):
    """Base for all service-layer errors."""

    def __init__(self, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id or generate_request_id()


class NotFoundError(ServiceError):
    """Upstream returned 404."""


class UpstreamError(ServiceError):
    """Upstream 5xx or network failure (already retried by client)."""


class ValidationError(ServiceError):
    """Input failed business validation (e.g. invalid email)."""


class ServiceUnavailableError(ServiceError):
    """Required dependency not initialized (e.g. MQ down)."""


# ------------------------------------------------------------------
# Result dataclasses
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    id: str
    status: str
    customer_email: str | None
    customer_name: str | None
    title: str
    description: str | None
    created_at: str | None
    resolved_at: str | None
    resolution_notes: str | None
    billable_amount: float | None


@dataclass(frozen=True)
class TicketSummary:
    id: str
    title: str
    status: str
    created_at: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class InvoiceStatus:
    invoice_id: str | None
    status: str  # draft | sent | paid | unknown | not_found
    amount: float | None
    created_at: str | None
    paid_at: str | None


@dataclass(frozen=True)
class InvoiceSummary:
    invoice_id: str
    amount: float
    status: str
    created_at: str | None


@dataclass(frozen=True)
class InvoiceQueueResult:
    status: Literal["queued", "unavailable"]
    estimated_amount: float
    message: str


@dataclass(frozen=True)
class ResolutionResult:
    ticket_id: str
    status: Literal["resolved"]
    resolution_notes: str
    invoice: InvoiceQueueResult | None


@dataclass(frozen=True)
class EmailQueueResult:
    status: Literal["queued", "failed"]
    to_email: str
    subject: str
    message: str


@dataclass(frozen=True)
class CustomerSummary:
    total_tickets: int
    open_tickets: int
    total_invoiced: float
    outstanding_balance: float
    lifetime_value: float


@dataclass(frozen=True)
class CustomerContext:
    customer_email: str
    summary: CustomerSummary
    recent_tickets: list[TicketSummary]
    recent_invoices: list[InvoiceSummary]


@dataclass(frozen=True)
class HealthStatus:
    status: Literal["healthy", "degraded"]
    services: dict[str, str]


def _ticket_from_dict(data: dict[str, Any]) -> Ticket:
    """Map upstream ticket dict to domain Ticket."""
    return Ticket(
        id=data.get("id") or "N/A",
        status=data.get("status") or "unknown",
        customer_email=(
            data.get("customerId")
            or data.get("customer_id")
            or data.get("customerEmail")
        ),
        customer_name=data.get("customerName") or data.get("customer_name"),
        title=data.get("title") or "Untitled",
        description=data.get("description"),
        created_at=data.get("createdAt"),
        resolved_at=data.get("resolvedAt") or data.get("completedAt"),
        resolution_notes=data.get("resolutionNotes") or data.get("resolution_notes"),
        billable_amount=(
            data.get("billableAmount")
            if data.get("billableAmount") is not None
            else data.get("billable_amount")
        ),
    )


def _ticket_summary_from_dict(data: dict[str, Any]) -> TicketSummary:
    return TicketSummary(
        id=data.get("id") or "N/A",
        title=data.get("title") or "Untitled",
        status=data.get("status") or "unknown",
        created_at=data.get("createdAt"),
        resolved_at=data.get("resolvedAt") or data.get("completedAt"),
    )


def _invoice_status_from_dict(data: dict[str, Any] | None) -> InvoiceStatus:
    if data is None:
        return InvoiceStatus(
            invoice_id=None,
            status="not_found",
            amount=None,
            created_at=None,
            paid_at=None,
        )
    return InvoiceStatus(
        invoice_id=data.get("invoice_id"),
        status=data.get("status") or "unknown",
        amount=data.get("amount"),
        created_at=data.get("created_at"),
        paid_at=data.get("paid_at"),
    )


def _invoice_summary_from_dict(data: dict[str, Any]) -> InvoiceSummary:
    return InvoiceSummary(
        invoice_id=data.get("invoice_id") or "N/A",
        amount=data.get("amount") or 0.0,
        status=data.get("status") or "unknown",
        created_at=data.get("created_at"),
    )


# ------------------------------------------------------------------
# GaramaticService
# ------------------------------------------------------------------


class GaramaticService:
    """Single entry point for all business logic.

    Owns client lifecycle, orchestration, validation, logging, and
    graceful degradation. Callers only adapt results to their formats.
    """

    def __init__(
        self,
        ticket_client: TicketClientPort,
        odoo_client: OdooClientPort | None,
        mq_publisher: MessageQueuePort | None,
    ) -> None:
        self._ticket = ticket_client
        self._odoo = odoo_client
        self._mq = mq_publisher

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        for client in (self._ticket, self._odoo, self._mq):
            if client:
                with contextlib.suppress(Exception):
                    await client.close()

    async def __aenter__(self) -> GaramaticService:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Port call helper — request ID, logging, error mapping
    # ------------------------------------------------------------------

    async def _call_port(
        self,
        operation: str,
        log_kwargs: dict[str, Any],
        coro: Awaitable[_T],
    ) -> _T:
        """Wrap a port call with tracing and error mapping.

        Generates a request ID, logs the start, awaits the coroutine,
        and maps any exception to the service-level taxonomy.
        """
        rid = generate_request_id()
        logger.info(f"service.{operation}", request_id=rid, **log_kwargs)
        try:
            return await coro
        except Exception as e:
            logger.error(
                f"service.{operation}.failed",
                error=str(e),
                request_id=rid,
                **log_kwargs,
            )
            raise _map_client_error(e, rid) from e

    # ------------------------------------------------------------------
    # Ticket operations
    # ------------------------------------------------------------------

    async def get_ticket(self, ticket_id: str) -> Ticket:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        data = await self._call_port(
            "get_ticket",
            {"ticket_id": ticket_id},
            self._ticket.get_ticket(ticket_id),
        )
        return _ticket_from_dict(data)

    async def list_tickets(
        self,
        *,
        customer_email: str | None = None,
        status: Literal["open", "resolved", "closed"] | None = None,
        limit: int = 10,
    ) -> list[TicketSummary]:
        if status is not None and status not in {"open", "resolved", "closed"}:
            raise ValidationError(
                f"Invalid status filter: {status!r}. "
                "Must be one of: open, resolved, closed."
            )
        data = await self._call_port(
            "list_tickets",
            {"email": customer_email, "status": status, "limit": limit},
            self._ticket.list_tickets(
                customer_id=customer_email,
                status=status,
                limit=limit,
            ),
        )
        return [_ticket_summary_from_dict(d) for d in data]

    async def create_ticket(
        self,
        *,
        title: str,
        description: str,
        customer_email: str,
        customer_name: str,
        priority: Literal["low", "medium", "high", "urgent"] = "medium",
    ) -> Ticket:
        validate_email(customer_email)
        if not title or not title.strip():
            raise ValidationError("Title is required")
        if priority not in {"low", "medium", "high", "urgent"}:
            raise ValidationError(
                f"Invalid priority: {priority!r}. "
                "Must be one of: low, medium, high, urgent."
            )
        data = await self._call_port(
            "create_ticket",
            {"title": title, "customer": customer_email},
            self._ticket.create_ticket(
                title=title,
                description=description,
                customer_id=customer_email,
                customer_name=customer_name,
                priority=priority,
            ),
        )
        return _ticket_from_dict(data)

    async def resolve_ticket(
        self,
        *,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None,
    ) -> ResolutionResult:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        if not resolution_notes or not resolution_notes.strip():
            raise ValidationError("Resolution notes are required")
        if billable_amount is not None and billable_amount < 0:
            raise ValidationError("Billable amount cannot be negative")
        ticket_data = await self._call_port(
            "resolve_ticket",
            {"ticket_id": ticket_id, "billable": billable_amount},
            self._ticket.resolve_ticket(
                ticket_id=ticket_id,
                resolution_notes=resolution_notes,
                billable_amount=billable_amount,
            ),
        )

        invoice_result: InvoiceQueueResult | None = None
        if billable_amount is not None and billable_amount > 0:
            ticket = _ticket_from_dict(ticket_data)
            invoice_result = await self._queue_invoice(
                ticket_id=ticket_id,
                customer_email=ticket.customer_email,
                customer_name=ticket.customer_name,
                amount=billable_amount,
                description=resolution_notes[:100],
                rid=generate_request_id(),
            )

        return ResolutionResult(
            ticket_id=ticket_id,
            status="resolved",
            resolution_notes=resolution_notes,
            invoice=invoice_result,
        )

    # ------------------------------------------------------------------
    # Invoice operations
    # ------------------------------------------------------------------

    async def get_invoice_status(self, ticket_id: str) -> InvoiceStatus:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        if self._odoo is None:
            raise ServiceUnavailableError("Odoo bridge not configured")
        data = await self._call_port(
            "get_invoice_status",
            {"ticket_id": ticket_id},
            self._odoo.get_invoice_status(ticket_id),
        )
        return _invoice_status_from_dict(data)

    async def queue_invoice(
        self,
        *,
        ticket_id: str,
        amount: float,
        description: str | None = None,
    ) -> InvoiceQueueResult:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        if amount <= 0:
            raise ValidationError("Invoice amount must be positive")
        ticket_data = await self._call_port(
            "queue_invoice",
            {"ticket_id": ticket_id, "amount": amount},
            self._ticket.get_ticket(ticket_id),
        )

        ticket = _ticket_from_dict(ticket_data)
        return await self._queue_invoice(
            ticket_id=ticket_id,
            customer_email=ticket.customer_email,
            customer_name=ticket.customer_name or ticket.title or "Customer",
            amount=amount,
            description=description or ticket.description or "Service",
            rid=generate_request_id(),
        )

    async def _queue_invoice(
        self,
        *,
        ticket_id: str,
        customer_email: str | None,
        customer_name: str | None,
        amount: float,
        description: str,
        rid: str,
    ) -> InvoiceQueueResult:
        if self._mq is None:
            return InvoiceQueueResult(
                status="unavailable",
                estimated_amount=amount,
                message=(
                    "Invoice service unavailable. "
                    "Ticket resolved but invoice was not created."
                ),
            )

        payload = {
            "event_type": "invoice.create_requested",
            "ticket_id": ticket_id,
            "customer_email": customer_email,
            "customer_name": customer_name or "Customer",
            "amount": amount,
            "description": description,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await self._mq.publish("invoice.create_requested", payload)
            return InvoiceQueueResult(
                status="queued",
                estimated_amount=amount,
                message="Invoice creation queued. Will be created in Odoo within 1 minute.",
            )
        except Exception as e:
            logger.error(
                "service.queue_invoice.publish_failed",
                ticket_id=ticket_id,
                error=str(e),
                request_id=rid,
            )
            return InvoiceQueueResult(
                status="unavailable",
                estimated_amount=amount,
                message=(
                    "Invoice service unavailable. "
                    "Ticket resolved but invoice was not created."
                ),
            )

    # ------------------------------------------------------------------
    # Messaging operations
    # ------------------------------------------------------------------

    async def send_email(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        from_name: str = "Garamatic Support",
        from_email: str = "support@garamatic.com",
    ) -> EmailQueueResult:
        validate_email(to_email)
        if not subject or not subject.strip():
            raise ValidationError("Email subject is required")
        rid = generate_request_id()
        logger.info("service.send_email", to=to_email, subject=subject, request_id=rid)

        if self._mq is None:
            return EmailQueueResult(
                status="failed",
                to_email=to_email,
                subject=subject,
                message="Email service unavailable",
            )

        payload = {
            "event_type": "email.send",
            "to_email": to_email,
            "from_email": from_email,
            "from_name": from_name,
            "subject": subject,
            "body_html": body,
        }

        try:
            await self._mq.publish("email.send", payload)
            return EmailQueueResult(
                status="queued",
                to_email=to_email,
                subject=subject,
                message="Email queued for sending. Will be delivered within 1 minute.",
            )
        except Exception as e:
            logger.error("service.send_email.failed", to=to_email, error=str(e), request_id=rid)
            return EmailQueueResult(
                status="failed",
                to_email=to_email,
                subject=subject,
                message=f"Failed to queue email: {e}",
            )

    # ------------------------------------------------------------------
    # Customer intelligence
    # ------------------------------------------------------------------

    async def get_customer_context(self, customer_email: str) -> CustomerContext:
        validate_email(customer_email)

        async def _fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            tickets_data = await self._ticket.list_tickets(customer_id=customer_email, limit=50)
            invoices_data = (
                await self._odoo.list_customer_invoices(customer_email)
                if self._odoo is not None
                else []
            )
            return tickets_data, invoices_data

        tickets_data, invoices_data = await self._call_port(
            "get_customer_context",
            {"email": customer_email},
            _fetch(),
        )

        def _amount(ticket: dict) -> float:
            val = ticket.get("billableAmount")
            if val is not None:
                return val
            return ticket.get("billable_amount") or 0

        total_ticket_value = sum(
            _amount(t) for t in tickets_data if t.get("status") == "resolved"
        )
        outstanding = sum(
            (inv.get("amount") or 0)
            for inv in invoices_data
            if inv.get("status") != "paid"
        )

        return CustomerContext(
            customer_email=customer_email,
            summary=CustomerSummary(
                total_tickets=len(tickets_data),
                open_tickets=sum(1 for t in tickets_data if t.get("status") == "open"),
                total_invoiced=sum((inv.get("amount") or 0) for inv in invoices_data),
                outstanding_balance=outstanding,
                lifetime_value=total_ticket_value,
            ),
            recent_tickets=[_ticket_summary_from_dict(t) for t in tickets_data[:3]],
            recent_invoices=[_invoice_summary_from_dict(inv) for inv in invoices_data[:3]],
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def health_check(self) -> HealthStatus:
        services: dict[str, str] = {}

        try:
            await self._ticket.list_tickets(limit=1)
            services["ticket_api"] = "connected"
        except Exception as e:
            services["ticket_api"] = f"error: {str(e)[:50]}"

        if self._odoo is None:
            services["odoo_bridge"] = "not_configured"
        else:
            try:
                await self._odoo.list_customer_invoices("test@example.com")
                services["odoo_bridge"] = "connected"
            except Exception as e:
                if hasattr(e, "response") and getattr(e.response, "status_code", None) == 404:
                    services["odoo_bridge"] = "connected"
                else:
                    services["odoo_bridge"] = f"error: {str(e)[:50]}"

        services["rabbitmq"] = "connected" if self._mq is not None else "not_initialized"

        status: Literal["healthy", "degraded"] = (
            "healthy" if all(v == "connected" for v in services.values()) else "degraded"
        )
        return HealthStatus(status=status, services=services)

    # ------------------------------------------------------------------
    # Optimized default: customer session
    # ------------------------------------------------------------------

    @property
    def mq(self) -> MessageQueuePort | None:
        """Expose the message queue port for narrow adapters."""
        return self._mq

    def for_customer(self, email: str) -> CustomerSession:
        """Return a session scoped to a single customer.

        Usage:
            session = service.for_customer("alice@example.com")
            tickets = await session.list_tickets()
            result = await session.resolve_ticket("T-123", "Fixed", 150.0)
        """
        validate_email(email)
        return CustomerSession(service=self, email=email)


# ------------------------------------------------------------------
# CustomerSession
# ------------------------------------------------------------------


class CustomerSession:
    """Scoped to a single customer — the trivial default case."""

    def __init__(self, service: GaramaticService, email: str) -> None:
        self._svc = service
        self.email = email

    async def list_tickets(
        self,
        status: Literal["open", "resolved", "closed"] | None = None,
        limit: int = 10,
    ) -> list[TicketSummary]:
        return await self._svc.list_tickets(customer_email=self.email, status=status, limit=limit)

    async def get_context(self) -> CustomerContext:
        return await self._svc.get_customer_context(self.email)

    async def resolve_ticket(
        self,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None,
    ) -> ResolutionResult:
        return await self._svc.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=resolution_notes,
            billable_amount=billable_amount,
        )

    async def send_email(
        self,
        subject: str,
        body: str,
        from_name: str = "Garamatic Support",
        from_email: str = "support@garamatic.com",
    ) -> EmailQueueResult:
        return await self._svc.send_email(
            to_email=self.email,
            subject=subject,
            body=body,
            from_name=from_name,
            from_email=from_email,
        )

    async def queue_invoice(
        self,
        ticket_id: str,
        amount: float,
        description: str | None = None,
    ) -> InvoiceQueueResult:
        return await self._svc.queue_invoice(
            ticket_id=ticket_id,
            amount=amount,
            description=description,
        )


# ------------------------------------------------------------------
# Error mapping helper
# ------------------------------------------------------------------


def _map_client_error(e: Exception, rid: str) -> ServiceError:
    """Map client-level exceptions to service-level exceptions.

    Raises pybreaker errors directly so FastAPI's global handler can catch them.
    Returns ServiceError subclasses for everything else.
    """
    # Pass through circuit breaker errors so FastAPI's global handler can catch them
    if isinstance(e, pybreaker.CircuitBreakerError):
        raise e

    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == 404:
            return NotFoundError(str(e), rid)
        return UpstreamError(str(e), rid)
    return UpstreamError(str(e), rid)


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

async def create_service() -> GaramaticService:
    """Factory: create a fully wired GaramaticService from config.

    Creates TicketClient, OdooBridgeClient, and RabbitMQPublisher,
    connects RabbitMQ best-effort, and returns the composed service.
    This is the single composition root for all entry points.
    """
    from .clients import OdooBridgeClient, RabbitMQPublisher, TicketClient
    from .config import get_settings

    config = get_settings()

    ticket_client = TicketClient(config.ticket_api_url)
    odoo_client = (
        OdooBridgeClient(config.odoo_bridge_url)
        if config.odoo_bridge_url
        else None
    )

    mq_publisher: RabbitMQPublisher | None = None
    try:
        mq_publisher = RabbitMQPublisher(config.rabbitmq_url)
        await mq_publisher.connect()
        logger.info("service_factory.mq.connected")
    except Exception as e:
        logger.warning("service_factory.mq.unavailable", error=str(e))

    return GaramaticService(
        ticket_client=ticket_client,
        odoo_client=odoo_client,
        mq_publisher=mq_publisher,
    )



