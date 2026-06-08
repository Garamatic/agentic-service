"""Focused services and domain types for the Garamatic Agentic Service.

Re-exports the public API surface from the services package for backward
compatibility with existing imports.
"""

from .base import (
    BaseService,
    CustomerContext,
    CustomerSummary,
    EmailQueueResult,
    HealthStatus,
    InvoiceQueueResult,
    InvoiceStatus,
    InvoiceSummary,
    NotFoundError,
    ResolutionResult,
    ServiceError,
    ServiceUnavailableError,
    Ticket,
    TicketStatus,
    TicketSummary,
    UpstreamError,
    ValidationError,
    format_date,
    generate_request_id,
    validate_email,
)
from .base import (
    InvoiceStatus as InvoiceStatusEnum,
)
from .base import (
    TicketStatus as TicketStatusEnum,
)
from .customer_context_service import CustomerContextService
from .email_service import EmailService
from .facade import GaramaticService
from .invoice_service import InvoiceService
from .ticket_service import TicketService

__all__ = [
    "BaseService",
    "CustomerContext",
    "CustomerContextService",
    "CustomerSummary",
    "EmailQueueResult",
    "EmailService",
    "GaramaticService",
    "HealthStatus",
    "InvoiceQueueResult",
    "InvoiceService",
    "InvoiceStatus",
    "InvoiceStatusEnum",
    "InvoiceSummary",
    "NotFoundError",
    "ResolutionResult",
    "ServiceError",
    "ServiceUnavailableError",
    "Ticket",
    "TicketService",
    "TicketStatus",
    "TicketStatusEnum",
    "TicketSummary",
    "UpstreamError",
    "ValidationError",
    "create_service",
    "format_date",
    "generate_request_id",
    "validate_email",
]

# Lazy import to avoid circular dependencies
from .facade import create_service as _create_service

create_service = _create_service
