"""Base domain types, exceptions, helpers, and the BaseService class.

Everything in this module is shared across all focused services.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, TypeVar

import httpx
import pybreaker
import structlog

from ..approval import ApprovalRequiredError

_T = TypeVar("_T")

logger = structlog.get_logger()


# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------


class TicketStatus(StrEnum):
    """Canonical ticket statuses."""

    OPEN = "open"
    RESOLVED = "resolved"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class InvoiceStatusCode(StrEnum):
    """Canonical invoice status codes."""

    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"


# ------------------------------------------------------------------
# Exceptions
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
    status: str
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


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def generate_request_id() -> str:
    """Generate unique request ID for tracing."""
    return uuid.uuid4().hex


def _redact_email(email: str) -> str:
    """Redact an email address for safe logging.

    Returns a partially masked string like 'a***@example.com'.
    """
    if "@" not in email:
        return "***"
    local, _, domain = email.rpartition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


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
# Mapping helpers
# ------------------------------------------------------------------


def _ticket_from_dict(data: dict[str, Any]) -> Ticket:
    """Map upstream ticket dict to domain Ticket."""
    return Ticket(
        id=data.get("id") or "N/A",
        status=data.get("status") or TicketStatus.UNKNOWN,
        customer_email=(
            data.get("customerId") or data.get("customer_id") or data.get("customerEmail")
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
        status=data.get("status") or TicketStatus.UNKNOWN,
        created_at=data.get("createdAt"),
        resolved_at=data.get("resolvedAt") or data.get("completedAt"),
    )


def _invoice_status_from_dict(data: dict[str, Any] | None) -> InvoiceStatus:
    if data is None:
        return InvoiceStatus(
            invoice_id=None,
            status=InvoiceStatusCode.NOT_FOUND,
            amount=None,
            created_at=None,
            paid_at=None,
        )
    return InvoiceStatus(
        invoice_id=data.get("invoice_id"),
        status=data.get("status") or InvoiceStatusCode.UNKNOWN,
        amount=data.get("amount"),
        created_at=data.get("created_at"),
        paid_at=data.get("paid_at"),
    )


def _invoice_summary_from_dict(data: dict[str, Any]) -> InvoiceSummary:
    return InvoiceSummary(
        invoice_id=data.get("invoice_id") or "N/A",
        amount=data.get("amount") or 0.0,
        status=data.get("status") or InvoiceStatusCode.UNKNOWN,
        created_at=data.get("created_at"),
    )


# ------------------------------------------------------------------
# Error mapping
# ------------------------------------------------------------------


def _map_client_error(e: Exception, rid: str) -> ServiceError:
    """Map client-level exceptions to service-level exceptions.

    Raises pybreaker and ApprovalRequiredError directly so FastAPI's
    global handler can catch them.
    Returns ServiceError subclasses for everything else.
    """
    if isinstance(e, pybreaker.CircuitBreakerError):
        raise e

    if isinstance(e, ApprovalRequiredError):
        raise e

    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == 404:
            return NotFoundError(str(e), rid)
        return UpstreamError(str(e), rid)
    return UpstreamError(str(e), rid)


# ------------------------------------------------------------------
# BaseService
# ------------------------------------------------------------------


class BaseService:
    """Shared infrastructure for all focused services.

    Provides the _call_port helper, lifecycle management, and
    a common interface for health checks.
    """

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

    async def close(self) -> None:
        """Override in subclasses to close resources."""

    async def health_check(self) -> HealthStatus:
        """Override in subclasses to provide health status."""
        return HealthStatus(status="healthy", services={})
