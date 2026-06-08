"""Facade: GaramaticService composes focused services.

Existing callers (MCP, REST API, agent) depend on this class. The facade
delegates to focused services while keeping the same public API.
"""

from __future__ import annotations

import contextlib
from typing import Literal

import structlog

from ..approval import ApprovalRequiredError, ApprovalStore
from ..clients import OdooBridgeClient, RabbitMQPublisher, TicketClient
from ..config import get_settings
from ..ports import MessageQueuePort, OdooClientPort, TicketClientPort
from .base import (
    CustomerContext,
    EmailQueueResult,
    HealthStatus,
    InvoiceQueueResult,
    InvoiceStatus,
    ResolutionResult,
    Ticket,
    TicketSummary,
    ValidationError,
    validate_email,
)
from .customer_context_service import CustomerContextService
from .email_service import EmailService
from .invoice_service import InvoiceService
from .ticket_service import TicketService

logger = structlog.get_logger()


class GaramaticService:
    """Single entry point for all business logic.

    Owns client lifecycle, orchestration, validation, logging, and
    graceful degradation. Callers only adapt results to their formats.

    This is a thin facade over focused services (TicketService,
    InvoiceService, EmailService, CustomerContextService).
    """

    def __init__(
        self,
        ticket_client: TicketClientPort,
        odoo_client: OdooClientPort | None,
        mq_publisher: MessageQueuePort | None,
        approval_store: ApprovalStore | None = None,
    ) -> None:
        self._ticket = TicketService(ticket_client)
        self._invoice = InvoiceService(odoo_client, mq_publisher)
        self._email = EmailService(mq_publisher)
        self._context = CustomerContextService(ticket_client, odoo_client)
        self._approval_store = approval_store
        self._mq = mq_publisher

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        for svc in (self._ticket, self._invoice, self._email, self._context):
            if svc is not None:
                with contextlib.suppress(Exception):
                    await svc.close()

    async def __aenter__(self) -> GaramaticService:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Ticket operations
    # ------------------------------------------------------------------

    async def get_ticket(self, ticket_id: str) -> Ticket:
        return await self._ticket.get_ticket(ticket_id)

    async def list_tickets(
        self,
        *,
        customer_email: str | None = None,
        status: Literal["open", "resolved", "closed"] | None = None,
        limit: int = 10,
    ) -> list[TicketSummary]:
        return await self._ticket.list_tickets(
            customer_email=customer_email,
            status=status,
            limit=limit,
        )

    async def create_ticket(
        self,
        *,
        title: str,
        description: str,
        customer_email: str,
        customer_name: str,
        priority: Literal["low", "medium", "high", "urgent"] = "medium",
    ) -> Ticket:
        return await self._ticket.create_ticket(
            title=title,
            description=description,
            customer_email=customer_email,
            customer_name=customer_name,
            priority=priority,
        )

    async def resolve_ticket(
        self,
        *,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None,
        approval_id: str | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> ResolutionResult:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        if not resolution_notes or not resolution_notes.strip():
            raise ValidationError("Resolution notes are required")
        if billable_amount is not None and billable_amount < 0:
            raise ValidationError("Billable amount cannot be negative")

        # Approval check: high-value invoices need explicit approval
        if billable_amount is not None and billable_amount > 0:
            threshold = get_settings().approval_threshold
            if billable_amount > threshold:
                if approval_id is None:
                    from ..approval import generate_approval_id

                    if approval_store is not None:
                        new_approval_id = await approval_store.create_request(
                            f"resolve_ticket:{ticket_id}", billable_amount
                        )
                    else:
                        new_approval_id = generate_approval_id()
                    raise ApprovalRequiredError(
                        f"Amount €{billable_amount:.2f} exceeds approval threshold "
                        f"of €{threshold:.2f}. Create an approval request and retry "
                        f"with approval_id='{new_approval_id}'.",
                        approval_id=new_approval_id,
                        amount=billable_amount,
                    )
                if approval_store is None or not await approval_store.is_approved(approval_id):
                    raise ApprovalRequiredError(
                        f"Approval ID '{approval_id}' is not valid or has expired. "
                        "Request a new approval and try again.",
                        amount=billable_amount,
                    )

        ticket_data = await self._ticket.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=resolution_notes,
            billable_amount=billable_amount,
        )

        invoice_result: InvoiceQueueResult | None = None
        if billable_amount is not None and billable_amount > 0:
            invoice_result = await self._invoice.queue_invoice(
                ticket_id=ticket_id,
                amount=billable_amount,
                description=resolution_notes[:100],
                ticket_data=ticket_data,
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
        return await self._invoice.get_invoice_status(ticket_id)

    async def queue_invoice(
        self,
        *,
        ticket_id: str,
        amount: float,
        description: str | None = None,
    ) -> InvoiceQueueResult:
        # Need ticket data to resolve customer info
        ticket_data = await self._ticket._ticket.get_ticket(ticket_id)
        return await self._invoice.queue_invoice(
            ticket_id=ticket_id,
            amount=amount,
            description=description,
            ticket_data=ticket_data,
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
        return await self._email.send_email(
            to_email=to_email,
            subject=subject,
            body=body,
            from_name=from_name,
            from_email=from_email,
        )

    # ------------------------------------------------------------------
    # Customer intelligence
    # ------------------------------------------------------------------

    async def get_customer_context(self, customer_email: str) -> CustomerContext:
        return await self._context.get_customer_context(customer_email)

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

        if self._invoice._odoo is None:
            services["odoo_bridge"] = "not_configured"
        else:
            try:
                await self._invoice._odoo.list_customer_invoices("test@example.com")
                services["odoo_bridge"] = "connected"
            except Exception as e:
                if hasattr(e, "response") and getattr(e.response, "status_code", None) == 404:
                    services["odoo_bridge"] = "connected"
                else:
                    services["odoo_bridge"] = f"error: {str(e)[:50]}"

        services["rabbitmq"] = "not_initialized"
        if self._mq is not None:
            try:
                conn = getattr(self._mq, "connection", None)
                if conn is not None and not getattr(conn, "is_closed", True):
                    services["rabbitmq"] = "connected"
                else:
                    services["rabbitmq"] = "disconnected"
            except Exception as e:
                services["rabbitmq"] = f"error: {str(e)[:50]}"

        status: Literal["healthy", "degraded"] = (
            "healthy" if all(v == "connected" for v in services.values()) else "degraded"
        )
        return HealthStatus(status=status, services=services)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mq(self) -> MessageQueuePort | None:
        """Expose the message queue port for narrow adapters."""
        return self._mq

    @property
    def approval_store(self) -> ApprovalStore | None:
        """Expose the approval store for agent tools."""
        return self._approval_store

    def for_customer(self, email: str) -> CustomerSession:
        """Return a session scoped to a single customer."""
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
# Factory
# ------------------------------------------------------------------


async def create_service() -> GaramaticService:
    """Factory: create a fully wired GaramaticService from config.

    Creates TicketClient, OdooBridgeClient, and RabbitMQPublisher,
    connects RabbitMQ best-effort, and returns the composed service.
    This is the single composition root for all entry points.
    """
    config = get_settings()

    ticket_client = TicketClient(config.ticket_api_url)
    odoo_client = OdooBridgeClient(config.odoo_bridge_url) if config.odoo_bridge_url else None

    mq_publisher: RabbitMQPublisher | None = None
    try:
        mq_publisher = RabbitMQPublisher(config.rabbitmq_url)
        await mq_publisher.connect()
        logger.info("service_factory.mq.connected")
    except Exception as e:
        logger.warning("service_factory.mq.unavailable", error=str(e))

    approval_store = ApprovalStore(db_path=config.agent_db_path or None)
    await approval_store.initialize()

    return GaramaticService(
        ticket_client=ticket_client,
        odoo_client=odoo_client,
        mq_publisher=mq_publisher,
        approval_store=approval_store,
    )
