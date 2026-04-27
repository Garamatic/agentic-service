"""HTTP and RabbitMQ clients for service integration."""

import asyncio
import json
from typing import Any

import httpx
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


class TicketClient:
    """HTTP client for ticket-masala API."""
    
    def __init__(self, base_url: str):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=30.0,
            headers={"Accept": "application/json"}
        )
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error)
    )
    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Get single ticket details."""
        response = await self.client.get(f"/api/tickets/{ticket_id}")
        response.raise_for_status()
        return response.json()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error)
    )
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
            
        response = await self.client.get("/api/tickets", params=params)
        response.raise_for_status()
        return response.json()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error)
    )
    async def create_ticket(
        self,
        title: str,
        description: str,
        customer_email: str,
        customer_name: str,
        priority: str = "medium"
    ) -> dict[str, Any]:
        """Create a new ticket."""
        payload = {
            "title": title,
            "description": description,
            "customerEmail": customer_email,
            "customerName": customer_name,
            "priority": priority
        }
        response = await self.client.post("/api/tickets", json=payload)
        response.raise_for_status()
        return response.json()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_error)
    )
    async def resolve_ticket(
        self,
        ticket_id: str,
        resolution_notes: str,
        billable_amount: float | None = None
    ) -> dict[str, Any]:
        """Resolve a ticket."""
        payload = {
            "resolutionNotes": resolution_notes
        }
        if billable_amount is not None:
            payload["billableAmount"] = billable_amount
            
        response = await self.client.post(
            f"/api/tickets/{ticket_id}/resolve",
            json=payload
        )
        response.raise_for_status()
        return response.json()
    
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
        retry=retry_if_exception(_is_retryable_error)
    )
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
        retry=retry_if_exception(_is_retryable_error)
    )
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
    """Async RabbitMQ publisher."""
    
    def __init__(self, amqp_url: str):
        self.amqp_url = amqp_url
        self.connection = None
        self.channel = None
        self.exchange = "event_exchange"
    
    async def connect(self):
        """Establish connection."""
        import pika
        
        loop = asyncio.get_running_loop()
        
        def _connect():
            params = pika.URLParameters(self.amqp_url)
            return pika.BlockingConnection(params)
        
        self.connection = await loop.run_in_executor(None, _connect)
        self.channel = self.connection.channel()
        
        # Declare exchange
        self.channel.exchange_declare(
            exchange=self.exchange,
            exchange_type="topic",
            durable=True
        )
        
        logger.info("rabbitmq.connected", exchange=self.exchange)
    
    async def publish(self, routing_key: str, message: dict[str, Any]):
        """Publish message to exchange."""
        if not self.channel:
            raise RuntimeError("Not connected to RabbitMQ")
        
        body = json.dumps(message, default=str).encode()
        
        import pika
        
        def _publish():
            self.channel.basic_publish(
                exchange=self.exchange,
                routing_key=routing_key,
                body=body,
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2  # Persistent
                )
            )
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _publish)
        
        logger.debug("rabbitmq.message_published", routing_key=routing_key)
    
    async def close(self):
        """Close connection."""
        if self.connection and not self.connection.is_closed:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.connection.close)
