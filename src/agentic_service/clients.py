"""HTTP clients and RabbitMQ publisher for external service communication.

These are concrete adapters satisfying the ports defined in ports.py.
"""

import json
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
    exclude=[lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500],
    name="ticket_api",
)


odoo_bridge_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=60,
    exclude=[lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500],
    name="odoo_bridge",
)


class TicketClient:
    """Client for ticket-masala API with versioned paths."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float | None = None,
    ):
        """Initialize the ticket client.

        Args:
            base_url: The base URL of the ticket-masala service
            api_key: Optional API key for authentication
            timeout: Request timeout in seconds (defaults to config.client_timeout)
        """
        if timeout is None:
            from .config import get_settings
            timeout = get_settings().client_timeout
        if not base_url or not isinstance(base_url, str):
            raise ValueError("TicketClient requires a valid base_url string")
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
        return response.json()  # type: ignore[no-any-return]

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
        return response.json()  # type: ignore[no-any-return]

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
        # Use the anonymous /api/tickets endpoint which is [AllowAnonymous]
        # and creates customers on-the-fly with snake_case fields.
        # Build the URL by replacing the path on the base_url, not via string replace.
        from urllib.parse import urljoin

        base = str(self.client.base_url).rsplit("/api/v1", 1)[0]
        tickets_url = urljoin(base + "/", "api/tickets")
        payload = {
            "customer_email": customer_id,
            "customer_name": kwargs.get("customer_name", "External User"),
            "description": description,
            "subject": title,
            "source": "agentic-service",
        }

        response = await self.client.post(
            tickets_url,
            json=payload,
        )
        response.raise_for_status()
        ticket = response.json()
        # .NET Web API returns camelCase by default: ticketId, success, message
        ticket_id = ticket.get("ticketId") or ticket.get("ticket_id")
        return {
            "id": ticket_id,
            "title": title,
            "description": description,
            "status": "open",
            "customer_email": customer_id,
        }

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
        return response.json()  # type: ignore[no-any-return]

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()


class OdooBridgeClient:
    """HTTP client for odoo-bridge API."""

    def __init__(self, base_url: str, timeout: float | None = None):
        if timeout is None:
            from .config import get_settings
            timeout = get_settings().client_timeout
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
        return response.json()  # type: ignore[no-any-return]

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
        return response.json()  # type: ignore[no-any-return]

    async def close(self) -> None:
        await self.client.aclose()


class RabbitMQPublisher:
    """Async RabbitMQ publisher (aio-pika native)."""

    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.exchange: aio_pika.abc.AbstractExchange | None = None
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
        if self.exchange is None:
            raise RuntimeError("Exchange not initialized")
        await self.exchange.publish(
            aio_pika.Message(
                body=json.dumps(message, default=str).encode(),
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
