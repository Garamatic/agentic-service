"""HTTP clients for external service communication."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import aio_pika
import httpx
import pybreaker
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger()


def _is_retryable_error(exception: BaseException) -> bool:
    """Module-level retryable error helper."""
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code >= 500
    return isinstance(exception, (httpx.NetworkError, httpx.TimeoutException))


ticket_api_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=60,
    exclude=[
        lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
    ],
    name="ticket_api",
)


odoo_bridge_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=60,
    exclude=[
        lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
    ],
    name="odoo_bridge",
)


class TicketClient:
    """Client for ticket-masala API with versioned paths."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        """Initialize the ticket client.

        Args:
            base_url: The base URL of the ticket-masala service
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds
        """
        self.client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
        )

    @ticket_api_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Fetch a ticket by ID.

        Args:
            ticket_id: The ticket/work-item ID

        Returns:
            The ticket data as a dictionary
        """
        response = await self.client.get(f"/work-items/{ticket_id}")
        response.raise_for_status()
        return response.json()

    @ticket_api_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def list_tickets(
        self,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List tickets with optional filters.

        Args:
            customer_id: Filter by customer ID (not email)
            status: Filter by ticket status
            limit: Maximum number of results
            offset: Pagination offset

        Returns:
            List of ticket data dictionaries
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }
        if customer_id:
            params["customerId"] = customer_id
        if status:
            params["status"] = status

        response = await self.client.get("/work-items", params=params)
        response.raise_for_status()
        return response.json()

    @ticket_api_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def create_ticket(
        self,
        title: str,
        description: str,
        customer_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a new ticket.

        Args:
            title: Ticket title
            description: Ticket description
            customer_id: Customer ID (not email)
            **kwargs: Additional ticket fields

        Returns:
            The created ticket data
        """
        payload = {
            "title": title,
            "description": description,
            "customerId": customer_id,
            **kwargs,
        }

        response = await self.client.post("/work-items", json=payload)
        response.raise_for_status()
        return response.json()

    @ticket_api_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def resolve_ticket(
        self,
        ticket_id: str,
        resolution_notes: str | None = None,
        billable_amount: float | None = None,
    ) -> dict[str, Any]:
        """Resolve a ticket.

        Args:
            ticket_id: The ticket ID to resolve
            resolution_notes: Notes about the resolution
            billable_amount: Billable amount for the resolution

        Returns:
            The resolved ticket data
        """
        payload: dict[str, Any] = {}
        if resolution_notes:
            payload["resolutionNotes"] = resolution_notes
        if billable_amount is not None:
            payload["billableAmount"] = billable_amount

        response = await self.client.post(
            f"/work-items/{ticket_id}/resolve",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def __aenter__(self) -> "TicketClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()


class OdooBridgeClient:
    """HTTP client for odoo-bridge API."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        if not base_url or not isinstance(base_url, str):
            raise ValueError("OdooBridgeClient requires a valid base_url string")
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    @odoo_bridge_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def get_invoice_status(self, ticket_id: str) -> dict[str, Any] | None:
        response = await self.client.get(f"/api/invoices/by-ticket/{ticket_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    @odoo_bridge_breaker
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True,
    )
    async def list_customer_invoices(self, customer_email: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            "/api/invoices",
            params={"customerEmail": customer_email},
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> "OdooBridgeClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()


class RabbitMQPublisher:
    """Async RabbitMQ publisher (aio-pika native)."""

    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.Channel | None = None
        self.exchange: aio_pika.Exchange | None = None
        self._exchange_name = "event_exchange"

    async def connect(self) -> None:
        if self.connection is not None:
            return
        self.connection = await aio_pika.connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        self.exchange = await self.channel.declare_exchange(
            self._exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        logger.info("rabbitmq.connected", exchange=self._exchange_name)

    async def publish(
        self,
        routing_key: str,
        message: dict[str, Any],
        headers: dict[str, Any] | None = None,
    ) -> None:
        if self.exchange is None:
            await self.connect()
        body = json.dumps(message, default=str).encode()
        await self.exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers=headers,
            ),
            routing_key=routing_key,
        )
        logger.debug("rabbitmq.message_published", routing_key=routing_key)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None
            self.channel = None
            self.exchange = None

    async def __aenter__(self) -> "RabbitMQPublisher":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()


# ═════════════════════════════════════════════════════════════════════════════
# Unified Service Facade — hides all transport detail behind a single entry point
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Immutable configuration for the service facade."""

    ticket_api_url: str
    amqp_url: str
    odoo_base_url: str | None = None
    ticket_api_key: str | None = None
    ticket_timeout: float = 30.0
    odoo_timeout: float = 30.0


class _OdooNamespace:
    """Secondary namespace for Odoo bridge operations."""

    def __init__(self, client: OdooBridgeClient) -> None:
        self._client = client

    async def get_invoice_status(self, ticket_id: str) -> dict[str, Any] | None:
        """Fetch invoice status for a ticket."""
        return await self._client.get_invoice_status(ticket_id)

    async def list_customer_invoices(self, customer_email: str) -> list[dict[str, Any]]:
        """List invoices for a customer."""
        return await self._client.list_customer_invoices(customer_email)


class AgenticService:
    """Unified cross-service facade optimized for the common caller.

    Hides HTTP (httpx), retry (tenacity), circuit-breaker (pybreaker),
    and AMQP (aio-pika) wiring. 80% operations are true one-liners.

    Usage (production):
        async with AgenticService(config) as service:
            ticket = await service.get_ticket("WI-123")
            await service.resolve_ticket("WI-123", resolution_notes="Fixed")
            await service.publish_event("ticket.resolved", {"id": "WI-123"})

    Usage (tests — inject mocks):
        service = AgenticService(
            ticket_client=fake_ticket_client,
            rabbitmq_publisher=fake_mq,
        )
    """

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        ticket_client: TicketClient | None = None,
        odoo_client: OdooBridgeClient | None = None,
        rabbitmq_publisher: RabbitMQPublisher | None = None,
    ) -> None:
        """Initialize from config (production) or pre-built clients (tests)."""
        if config is not None and any((ticket_client, odoo_client, rabbitmq_publisher)):
            raise ValueError(
                "Pass either 'config' OR pre-built clients, not both."
            )

        if config is not None:
            self._ticket = TicketClient(
                base_url=config.ticket_api_url,
                api_key=config.ticket_api_key,
                timeout=config.ticket_timeout,
            )
            self._odoo = (
                OdooBridgeClient(config.odoo_base_url, timeout=config.odoo_timeout)
                if config.odoo_base_url
                else None
            )
            self._mq = RabbitMQPublisher(config.amqp_url)
            self._owns_clients = True
        else:
            if ticket_client is None or rabbitmq_publisher is None:
                raise ValueError(
                    "When 'config' is omitted, both 'ticket_client' and "
                    "'rabbitmq_publisher' are required."
                )
            self._ticket = ticket_client
            self._odoo = odoo_client
            self._mq = rabbitmq_publisher
            self._owns_clients = False

        self._odoo_ns = _OdooNamespace(self._odoo) if self._odoo else None
        self._mq_lock = asyncio.Lock()
        self._mq_connected = False

    # ------------------------------------------------------------------
    # Port accessors — for injecting into GaramaticService
    # ------------------------------------------------------------------

    @property
    def tickets(self) -> TicketClient:
        """Expose the ticket client port."""
        return self._ticket

    @property
    def odoo(self) -> _OdooNamespace:
        """Odoo bridge operations (secondary)."""
        if self._odoo_ns is None:
            raise RuntimeError("Odoo bridge not configured")
        return self._odoo_ns

    @property
    def messages(self) -> RabbitMQPublisher:
        """Expose the message queue publisher port."""
        return self._mq

    # ------------------------------------------------------------------
    # 80% operations — flat, one-liner interface
    # ------------------------------------------------------------------

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Fetch a ticket by ID."""
        return await self._ticket.get_ticket(ticket_id)

    async def list_tickets(
        self,
        customer_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List tickets with optional filters."""
        return await self._ticket.list_tickets(
            customer_id=customer_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def create_ticket(
        self,
        title: str,
        description: str,
        customer_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a new ticket."""
        return await self._ticket.create_ticket(
            title=title,
            description=description,
            customer_id=customer_id,
            **kwargs,
        )

    async def resolve_ticket(
        self,
        ticket_id: str,
        *,
        resolution_notes: str | None = None,
        billable_amount: float | None = None,
    ) -> dict[str, Any]:
        """Resolve a ticket."""
        return await self._ticket.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=resolution_notes,
            billable_amount=billable_amount,
        )

    async def publish_event(
        self,
        routing_key: str,
        message: dict[str, Any],
        *,
        headers: dict[str, Any] | None = None,
    ) -> None:
        """Publish an event to RabbitMQ.

        Auto-connects on first publish if not already connected.
        """
        if not self._mq_connected:
            async with self._mq_lock:
                if not self._mq_connected:
                    try:
                        await self._mq.connect()
                        self._mq_connected = True
                    except Exception as e:
                        logger.warning("agentic_service.mq_connect_failed", error=str(e))
                        raise RuntimeError("RabbitMQ not connected and connection failed") from e

        await self._mq.publish(routing_key=routing_key, message=message, headers=headers)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Idempotent close of all underlying transports."""
        errors: list[Exception] = []

        for client, name in (
            (self._ticket, "ticket"),
            (self._odoo, "odoo"),
            (self._mq, "mq"),
        ):
            if client is None:
                continue
            try:
                await client.close()
            except Exception as e:
                logger.warning("agentic_service.close_failed", client=name, error=str(e))
                errors.append(e)

        self._mq_connected = False

        if errors:
            raise RuntimeError(f"Failed to close {len(errors)} transport(s)")

    async def __aenter__(self) -> "AgenticService":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()
