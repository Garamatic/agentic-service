"""HTTP and RabbitMQ clients for service integration."""

import json
from typing import Any

import aio_pika
import httpx
import pybreaker
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = structlog.get_logger()


def _is_retryable_error(e: Exception) -> bool:
    """Check if an exception should trigger a retry."""
    # Retry on connection/timeout errors
    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    # Retry on 5xx HTTP errors
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500:
        return True
    return False


# Circuit breakers for external services
# Prevents cascading failures when downstream services are down
# "exclude" defines which exceptions should NOT count toward the fail_max
# (i.e., business logic errors like 404s shouldn't open the circuit)
# Note: By default, ALL exceptions count toward the circuit breaker except what's in exclude
# RetryError from tenacity (after retries exhausted) will count toward circuit breaker
ticket_api_breaker = pybreaker.CircuitBreaker(
    fail_max=5,  # Open after 5 failures
    reset_timeout=60,  # Try again after 60 seconds
    exclude=[
        # Don't open circuit for client errors (4xx)
        lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
    ],
    name="ticket_api"
)

odoo_bridge_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=60,
    exclude=[
        lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500
    ],
    name="odoo_bridge"
)


class TicketClient:
    """HTTP client for ticket-masala API."""
    
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            headers={"Accept": "application/json"}
        )

    async def _try_request_json(self, method: str, paths: list[str], **kwargs):
        """Try multiple paths in order and return the first successful JSON response.

        Raises the last HTTP error if none succeed.
        """
        last_exc: Exception | None = None
        for path in paths:
            try:
                resp = await self.client.request(method, path, **kwargs)
            except Exception as e:
                last_exc = e
                continue

            # success
            if 200 <= resp.status_code < 300:
                try:
                    return resp.json()
                except Exception as e:
                    # JSON decode error - treat as failure
                    last_exc = e
                    continue

            # Not found -> try next
            if resp.status_code == 404:
                last_exc = httpx.HTTPStatusError("404 Not Found", request=resp.request, response=resp)
                continue

            # other client/server error -> raise to caller
            resp.raise_for_status()

        # If we reach here, re-raise last exception
        if last_exc:
            raise last_exc
        raise RuntimeError("No paths provided to _try_request_json")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True
    )
    @ticket_api_breaker
    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Get single ticket details."""
        paths = [
            f"/api/tickets/{ticket_id}",
            f"/api/v1/tickets/{ticket_id}",
            f"/api/v1/work-items/{ticket_id}"
        ]
        return await self._try_request_json("GET", paths)
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True
    )
    @ticket_api_breaker
    async def list_tickets(
        self,
        customer_email: str | None = None,
        status: str | None = None,
        limit: int = 10
    ) -> list[dict[str, Any]]:
        """List tickets with optional filters."""
        params = {"limit": limit}
        if customer_email:
            params["customerEmail"] = customer_email
        if status:
            params["status"] = status

        # try legacy unversioned list first, then v1 work-items (which may not support filters)
        paths = ["/api/tickets", "/api/v1/work-items"]
        return await self._try_request_json("GET", paths, params=params)
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True
    )
    @ticket_api_breaker
    async def create_ticket(
        self,
        title: str,
        description: str,
        customer_email: str,
        customer_name: str,
        priority: str = "medium"
    ) -> dict[str, Any]:
        """Create a new ticket."""
        # Candidate payloads for different TicketMasala endpoints
        payloads = [
            # legacy unversioned tickets endpoint (best-effort)
            ("/api/tickets", {
                "title": title,
                "description": description,
                "customerEmail": customer_email,
                "customerName": customer_name,
                "priority": priority
            }),
            # external submission endpoint exposed by TicketMasala for anonymous partners
            ("/api/v1/tickets/external", {
                "Subject": title,
                "Description": description,
                "CustomerEmail": customer_email,
                "CustomerName": customer_name,
                "SourceSite": "agentic-service"
            }),
            # fallback: create work-item (requires CustomerId usually, may fail)
            ("/api/v1/work-items", {
                "Title": title,
                "Description": description,
                "CustomerId": customer_email,  # best-effort (may not be accepted)
                "Priority": priority
            })
        ]

        last_exc: Exception | None = None
        for path, payload in payloads:
            try:
                return await self._try_request_json("POST", [path], json=payload)
            except Exception as e:
                last_exc = e
                continue

        if last_exc:
            raise last_exc
        raise RuntimeError("Failed to create ticket: no payloads attempted")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True
    )
    @ticket_api_breaker
    async def resolve_ticket(
        self,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None
    ) -> dict[str, Any]:
        """Resolve a ticket."""
        payload = {"resolutionNotes": resolution_notes}
        if billable_amount is not None:
            payload["billableAmount"] = billable_amount

        paths = [
            f"/api/tickets/{ticket_id}/resolve",
            f"/api/v1/tickets/{ticket_id}/resolve",
            f"/api/v1/work-items/{ticket_id}/resolve"
        ]

        return await self._try_request_json("POST", paths, json=payload)
    
    async def close(self):
        await self.client.aclose()


class OdooBridgeClient:
    """HTTP client for odoo-bridge API."""
    
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            headers={"Accept": "application/json"}
        )
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True
    )
    @odoo_bridge_breaker
    async def get_invoice_status(self, ticket_id: str) -> dict[str, Any] | None:
        """Get invoice status for a ticket."""
        response = await self.client.get(f"/api/invoices/by-ticket/{ticket_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error),
        reraise=True
    )
    @odoo_bridge_breaker
    async def list_customer_invoices(self, customer_email: str) -> list[dict[str, Any]]:
        """List invoices for a customer."""
        response = await self.client.get(
            "/api/invoices",
            params={"customerEmail": customer_email}
        )
        response.raise_for_status()
        return response.json()
    
    async def close(self):
        await self.client.aclose()


class RabbitMQPublisher:
    """Async RabbitMQ publisher (aio-pika native)."""
    
    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self.connection: aio_pika.RobustConnection | None = None
        self.channel: aio_pika.Channel | None = None
        self.exchange: aio_pika.Exchange | None = None
        self._exchange_name = "event_exchange"
    
    async def connect(self):
        """Establish async connection."""
        self.connection = await aio_pika.connect_robust(self.amqp_url)
        self.channel = await self.connection.channel()
        
        # Declare exchange
        self.exchange = await self.channel.declare_exchange(
            self._exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True
        )
        
        logger.info("rabbitmq.connected", exchange=self._exchange_name)
    
    async def publish(self, routing_key: str, message: dict[str, Any]):
        """Publish message to exchange (async-native)."""
        if not self.exchange:
            raise RuntimeError("Not connected to RabbitMQ")
        
        body = json.dumps(message, default=str).encode()
        
        await self.exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            ),
            routing_key=routing_key
        )
        
        logger.debug("rabbitmq.message_published", routing_key=routing_key)
    
    async def close(self):
        """Close async connection."""
        if self.connection:
            await self.connection.close()
            self.connection = None
            self.channel = None
            self.exchange = None
