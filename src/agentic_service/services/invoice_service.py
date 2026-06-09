"""Invoice operations — status lookup and queueing."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

import structlog

from ..idempotency import idempotency_key_for_invoice
from ..ports import MessageQueuePort, OdooClientPort
from .base import (
    BaseService,
    InvoiceQueueResult,
    InvoiceStatus,
    ServiceUnavailableError,
    ValidationError,
    _invoice_status_from_dict,
    _ticket_from_dict,
    generate_request_id,
)

logger = structlog.get_logger()


class InvoiceService(BaseService):
    """All invoice-related business logic."""

    def __init__(
        self,
        odoo_client: OdooClientPort | None,
        mq_publisher: MessageQueuePort | None,
    ) -> None:
        self._odoo = odoo_client
        self._mq = mq_publisher

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        for client in (self._odoo, self._mq):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()

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

    async def post_invoice(self, invoice_id: str) -> dict[str, Any]:
        if not invoice_id or not invoice_id.strip():
            raise ValidationError("Invoice ID is required")
        if self._odoo is None:
            raise ServiceUnavailableError("Odoo bridge not configured")
        return await self._call_port(
            "post_invoice",
            {"invoice_id": invoice_id},
            self._odoo.post_invoice(invoice_id),
        )

    async def get_invoice_pdf(self, invoice_id: str) -> bytes | None:
        if not invoice_id or not invoice_id.strip():
            raise ValidationError("Invoice ID is required")
        if self._odoo is None:
            raise ServiceUnavailableError("Odoo bridge not configured")
        return await self._call_port(
            "get_invoice_pdf",
            {"invoice_id": invoice_id},
            self._odoo.get_invoice_pdf(invoice_id),
        )

    async def register_payment(self, invoice_id: str, amount: float) -> dict[str, Any]:
        if not invoice_id or not invoice_id.strip():
            raise ValidationError("Invoice ID is required")
        if amount <= 0:
            raise ValidationError("Payment amount must be positive")
        if self._odoo is None:
            raise ServiceUnavailableError("Odoo bridge not configured")
        return await self._call_port(
            "register_payment",
            {"invoice_id": invoice_id, "amount": amount},
            self._odoo.register_payment(invoice_id, amount),
        )

    async def queue_invoice(
        self,
        *,
        ticket_id: str,
        amount: float,
        description: str | None = None,
        ticket_data: dict[str, Any] | None = None,
    ) -> InvoiceQueueResult:
        if not ticket_id or not ticket_id.strip():
            raise ValidationError("Ticket ID is required")
        if amount <= 0:
            raise ValidationError("Invoice amount must be positive")

        if self._mq is None:
            return InvoiceQueueResult(
                status="unavailable",
                estimated_amount=amount,
                message="Invoice service unavailable.",
            )

        # If ticket_data is provided, extract customer info from it
        if ticket_data is not None:
            ticket = _ticket_from_dict(ticket_data)
            customer_email = ticket.customer_email
            customer_name = ticket.customer_name or ticket.title or "Customer"
        else:
            customer_email = None
            customer_name = None

        payload = {
            "event_type": "invoice.create_requested",
            "ticket_id": ticket_id,
            "customer_email": customer_email,
            "customer_name": customer_name or "Customer",
            "amount": amount,
            "description": description or "Service",
            "requested_at": datetime.now(UTC).isoformat(),
            "idempotency_key": idempotency_key_for_invoice(ticket_id, amount),
        }

        rid = generate_request_id()
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
                message="Invoice service unavailable. Ticket resolved but invoice was not created.",
            )


