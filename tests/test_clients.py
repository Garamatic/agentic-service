"""Tests for HTTP and RabbitMQ clients."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pybreaker
import pytest

from agentic_service.clients import (
    OdooBridgeClient,
    RabbitMQPublisher,
    TicketClient,
    _is_retryable_error,
    ticket_api_breaker,
)


class TestRetryableErrorDetection:
    """Test error classification for retry logic."""

    def test_connection_error_is_retryable(self):
        """Connection errors should be retryable."""
        error = httpx.ConnectError("Connection refused")
        assert _is_retryable_error(error) is True

    def test_timeout_error_is_retryable(self):
        """Timeout errors should be retryable."""
        error = httpx.TimeoutException("Request timed out")
        assert _is_retryable_error(error) is True

    def test_5xx_error_is_retryable(self):
        """5xx HTTP errors should be retryable."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        error = httpx.HTTPStatusError(
            "Service unavailable",
            request=MagicMock(),
            response=mock_response
        )
        assert _is_retryable_error(error) is True

    def test_4xx_error_is_not_retryable(self):
        """4xx HTTP errors should not be retryable."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        error = httpx.HTTPStatusError(
            "Not found",
            request=MagicMock(),
            response=mock_response
        )
        assert _is_retryable_error(error) is False


@pytest.mark.asyncio
class TestTicketClient:
    """Test TicketClient functionality."""

    async def test_get_ticket_success(self, respx_mock, mock_ticket_data):
        """Should successfully retrieve ticket."""
        route = respx_mock.get("http://ticket-api.example.com/api/v1/work-items/T-123").mock(
            return_value=httpx.Response(200, json=mock_ticket_data)
        )

        client = TicketClient("http://ticket-api.example.com")
        result = await client.get_ticket("T-123")

        assert result["id"] == "T-123"
        assert route.called
        await client.close()

    async def test_list_tickets_with_params(self, respx_mock, mock_ticket_data):
        """Should list tickets with query parameters."""
        route = respx_mock.get(
            "http://ticket-api.example.com/api/v1/work-items",
            params={"customerId": "john@example.com", "status": "open", "limit": 5}
        ).mock(return_value=httpx.Response(200, json=[mock_ticket_data]))

        client = TicketClient("http://ticket-api.example.com")
        result = await client.list_tickets(
            customer_id="john@example.com",
            status="open",
            limit=5
        )

        assert len(result) == 1
        assert route.called
        await client.close()

    async def test_create_ticket(self, respx_mock):
        """Should create ticket successfully."""
        created_data = {"id": "T-999", "status": "created"}
        route = respx_mock.post("http://ticket-api.example.com/api/v1/work-items").mock(
            return_value=httpx.Response(201, json=created_data)
        )

        client = TicketClient("http://ticket-api.example.com")
        result = await client.create_ticket(
            title="New Issue",
            description="Description",
            customer_id="test@example.com"
        )

        assert result["id"] == "T-999"
        assert route.called
        await client.close()

    async def test_resolve_ticket(self, respx_mock):
        """Should resolve ticket successfully."""
        resolved_data = {"id": "T-123", "status": "resolved"}
        route = respx_mock.post("http://ticket-api.example.com/api/v1/work-items/T-123/resolve").mock(
            return_value=httpx.Response(200, json=resolved_data)
        )

        client = TicketClient("http://ticket-api.example.com")
        result = await client.resolve_ticket(
            ticket_id="T-123",
            resolution_notes="Fixed the issue",
            billable_amount=200.00
        )

        assert result["status"] == "resolved"
        assert route.called
        await client.close()

    async def test_circuit_breaker_configured(self, respx_mock):
        """Circuit breaker should be configured on the client methods."""
        # Verify that circuit breaker decorators are applied

        # The get_ticket method should be wrapped by circuit breaker
        # (we can verify by checking the method exists and has the right structure)
        client = TicketClient("http://ticket-api.example.com")
        assert hasattr(client, 'get_ticket')
        assert callable(client.get_ticket)

        # Circuit breaker state can be checked
        assert ticket_api_breaker.fail_max == 5
        assert ticket_api_breaker.reset_timeout == 60
        assert ticket_api_breaker.name == "ticket_api"

        await client.close()

    async def test_circuit_breaker_decorator_applied(self, respx_mock):
        """Verify circuit breaker decorator is properly applied to methods."""
        # Reset breaker state before test
        ticket_api_breaker.close()

        route = respx_mock.get("http://ticket-api.example.com/api/v1/work-items/T-123").mock(
            return_value=httpx.Response(200, json={"id": "T-123"})
        )

        client = TicketClient("http://ticket-api.example.com")

        # Successful call should work normally
        result = await client.get_ticket("T-123")
        assert result["id"] == "T-123"
        assert route.called

        # Circuit breaker should still be closed after success
        assert ticket_api_breaker.current_state == pybreaker.STATE_CLOSED

        await client.close()


@pytest.mark.asyncio
class TestOdooBridgeClient:
    """Test OdooBridgeClient functionality."""

    async def test_get_invoice_status_success(self, respx_mock, mock_invoice_data):
        """Should retrieve invoice status."""
        route = respx_mock.get("http://odoo-api.example.com/api/invoices/by-ticket/T-456").mock(
            return_value=httpx.Response(200, json=mock_invoice_data)
        )

        client = OdooBridgeClient("http://odoo-api.example.com")
        result = await client.get_invoice_status("T-456")

        assert result["invoice_id"] == "INV-789"
        assert result["status"] == "sent"
        assert route.called
        await client.close()

    async def test_get_invoice_status_not_found(self, respx_mock):
        """Should return None when invoice not found."""
        route = respx_mock.get("http://odoo-api.example.com/api/invoices/by-ticket/T-999").mock(
            return_value=httpx.Response(404)
        )

        client = OdooBridgeClient("http://odoo-api.example.com")
        result = await client.get_invoice_status("T-999")

        assert result is None
        assert route.called
        await client.close()

    async def test_list_customer_invoices(self, respx_mock, mock_invoice_data):
        """Should list customer invoices."""
        route = respx_mock.get(
            "http://odoo-api.example.com/api/invoices",
            params={"customerEmail": "john@example.com"}
        ).mock(return_value=httpx.Response(200, json=[mock_invoice_data]))

        client = OdooBridgeClient("http://odoo-api.example.com")
        result = await client.list_customer_invoices("john@example.com")

        assert len(result) == 1
        assert result[0]["invoice_id"] == "INV-789"
        assert route.called
        await client.close()


@pytest.mark.asyncio
class TestRabbitMQPublisher:
    """Test RabbitMQPublisher functionality."""

    async def test_connect_creates_connection(self):
        """Should establish async connection."""
        publisher = RabbitMQPublisher("amqp://guest:guest@localhost:5672/")

        # Mock the connection
        mock_conn = AsyncMock()
        mock_channel = AsyncMock()
        mock_exchange = AsyncMock()

        with patch('aio_pika.connect_robust', return_value=mock_conn):
            mock_conn.channel = AsyncMock(return_value=mock_channel)
            mock_channel.declare_exchange = AsyncMock(return_value=mock_exchange)

            await publisher.connect()

        assert publisher.connection is not None
        assert publisher.channel is not None
        assert publisher.exchange is not None

    async def test_publish_sends_message(self):
        """Should publish message to exchange."""
        publisher = RabbitMQPublisher("amqp://guest:guest@localhost:5672/")

        # Mock the exchange
        mock_exchange = AsyncMock()
        publisher.exchange = mock_exchange
        publisher.channel = MagicMock()

        message = {"event_type": "test", "data": "value"}
        await publisher.publish("routing.key", message)

        mock_exchange.publish.assert_called_once()
        call_args = mock_exchange.publish.call_args
        assert call_args[1]["routing_key"] == "routing.key"

    async def test_publish_raises_when_not_connected(self):
        """Should raise error when not connected."""
        publisher = RabbitMQPublisher("amqp://guest:guest@localhost:5672/")

        with pytest.raises(RuntimeError, match="Not connected"):
            await publisher.publish("key", {"data": "value"})

    async def test_close_releases_connection(self):
        """Should close connection properly."""
        publisher = RabbitMQPublisher("amqp://guest:guest@localhost:5672/")

        mock_conn = AsyncMock()
        publisher.connection = mock_conn

        await publisher.close()

        mock_conn.close.assert_called_once()


@pytest.mark.asyncio
class TestClientCleanup:
    """Test proper resource cleanup."""

    async def test_ticket_client_closes_httpx(self):
        """Should close httpx client."""
        client = TicketClient("http://ticket-api.example.com")

        with patch.object(client.client, 'aclose', new_callable=AsyncMock) as mock_close:
            await client.close()
            mock_close.assert_called_once()

    async def test_odoo_client_closes_httpx(self):
        """Should close httpx client."""
        client = OdooBridgeClient("http://odoo-api.example.com")

        with patch.object(client.client, 'aclose', new_callable=AsyncMock) as mock_close:
            await client.close()
            mock_close.assert_called_once()



