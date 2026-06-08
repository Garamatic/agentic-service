"""Backward-compatible re-export of the services package.

All domain types, exceptions, helpers, and the GaramaticService facade
have moved to the `services` subpackage. This module re-exports the public
API surface so existing imports continue to work.
"""

from .services.base import (
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
    TicketSummary,
    UpstreamError,
    ValidationError,
    format_date,
    generate_request_id,
    validate_email,
)

# Re-export focused services for direct access
from .services.customer_context_service import CustomerContextService  # noqa: F401
from .services.email_service import EmailService  # noqa: F401
from .services.facade import (
    CustomerSession,
    GaramaticService,
    create_service,
)
from .services.invoice_service import InvoiceService  # noqa: F401
from .services.ticket_service import TicketService  # noqa: F401

__all__ = [
    "CustomerContext",
    "CustomerContextService",
    "CustomerSession",
    "CustomerSummary",
    "EmailQueueResult",
    "EmailService",
    "GaramaticService",
    "HealthStatus",
    "InvoiceQueueResult",
    "InvoiceService",
    "InvoiceStatus",
    "InvoiceSummary",
    "NotFoundError",
    "ResolutionResult",
    "ServiceError",
    "ServiceUnavailableError",
    "Ticket",
    "TicketService",
    "TicketSummary",
    "UpstreamError",
    "ValidationError",
    "create_service",
    "format_date",
    "generate_request_id",
    "validate_email",
]
