"""Tests for REST API endpoints."""

from unittest.mock import MagicMock, patch

import pybreaker
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentic_service.api import app


@pytest_asyncio.fixture
async def api_client(mock_ticket_client, mock_odoo_client, mock_mq_publisher):
    """Create FastAPI test client with mocked dependencies.
    
    Patches both api.* and server.* globals since _get_customer_context_internal
    is shared between modules and uses server-level globals.
    """
    with patch('agentic_service.api.ticket_client', mock_ticket_client), \
         patch('agentic_service.api.odoo_client', mock_odoo_client), \
         patch('agentic_service.api.mq_publisher', mock_mq_publisher), \
         patch('agentic_service.server.ticket_client', mock_ticket_client), \
         patch('agentic_service.server.odoo_client', mock_odoo_client):

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
class TestHealthEndpoint:
    """Test health check endpoint."""

    async def test_returns_healthy_when_all_services_up(self, api_client, mock_ticket_client, mock_odoo_client):
        """Should return healthy status when all services connected."""
        mock_ticket_client.list_tickets.return_value = []
        mock_odoo_client.list_customer_invoices.return_value = []

        response = await api_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["ticket_api"] == "connected"
        assert data["services"]["odoo_bridge"] == "connected"

    async def test_returns_degraded_when_ticket_api_down(self, api_client, mock_ticket_client):
        """Should return degraded status when ticket API is down."""
        from httpx import ConnectError
        mock_ticket_client.list_tickets.side_effect = ConnectError("Connection refused")

        response = await api_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert "error" in data["services"]["ticket_api"]


@pytest.mark.asyncio
class TestTicketsEndpoints:
    """Test ticket-related API endpoints."""

    async def test_get_ticket_success(self, api_client, mock_ticket_client, mock_ticket_data):
        """Should return ticket details."""
        mock_ticket_client.get_ticket.return_value = mock_ticket_data

        response = await api_client.get("/tickets/T-123")

        assert response.status_code == 200
        data = response.json()
        assert data["ticket_id"] == "T-123"
        assert data["status"] == "open"
        assert data["customer_email"] == "john@example.com"

    async def test_get_ticket_not_found(self, api_client, mock_ticket_client):
        """Should return 404 for non-existent ticket."""
        from httpx import HTTPStatusError
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_ticket_client.get_ticket.side_effect = HTTPStatusError(
            "Not found",
            request=MagicMock(),
            response=mock_response
        )

        response = await api_client.get("/tickets/NONEXISTENT")

        assert response.status_code == 404
        data = response.json()
        assert "error" in data["detail"]
        assert "request_id" in data["detail"]

    async def test_list_tickets_with_filters(self, api_client, mock_ticket_client, mock_ticket_data):
        """Should list tickets with filters applied."""
        mock_ticket_client.list_tickets.return_value = [mock_ticket_data]

        response = await api_client.get("/tickets?customer_email=john@example.com&status=open&limit=5")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["ticket_id"] == "T-123"

        # Verify client called with filters (API maps customer_email -> customer_id)
        mock_ticket_client.list_tickets.assert_called_once_with(
            customer_id="john@example.com",
            status="open",
            limit=5
        )

    async def test_create_ticket_success(self, api_client, mock_ticket_client):
        """Should create new ticket."""
        mock_ticket_client.create_ticket.return_value = {"id": "T-999"}

        payload = {
            "title": "New Issue",
            "description": "Something is broken",
            "customer_email": "test@example.com",
            "customer_name": "Test User",
            "priority": "high"
        }

        response = await api_client.post("/tickets", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["ticket_id"] == "T-999"
        assert data["status"] == "created"

    async def test_create_ticket_validation_error(self, api_client):
        """Should reject invalid ticket data."""
        payload = {
            "title": "",  # Empty title
            "description": "Description",
            "customer_email": "invalid-email",
            "customer_name": "Test"
        }

        response = await api_client.post("/tickets", json=payload)

        assert response.status_code == 422  # Validation error

    async def test_resolve_ticket_success(self, api_client, mock_ticket_client, mock_mq_publisher):
        """Should resolve ticket and queue invoice when billable."""
        mock_ticket_client.resolve_ticket.return_value = {
            "id": "T-123",
            "status": "resolved",
            "customerId": "test@example.com",
            "customerName": "Test User"
        }

        payload = {
            "resolution_notes": "Fixed the network issue",
            "billable_amount": 200.00
        }

        response = await api_client.post("/tickets/T-123/resolve", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "resolved"
        assert "invoice" in data
        mock_mq_publisher.publish.assert_called_once()


@pytest.mark.asyncio
class TestInvoicesEndpoints:
    """Test invoice-related API endpoints."""

    async def test_get_invoice_success(self, api_client, mock_odoo_client, mock_invoice_data):
        """Should return invoice details."""
        mock_odoo_client.get_invoice_status.return_value = mock_invoice_data

        response = await api_client.get("/invoices/by-ticket/T-456")

        assert response.status_code == 200
        data = response.json()
        assert data["invoice_id"] == "INV-789"
        assert data["status"] == "sent"
        assert data["amount"] == 150.00

    async def test_get_invoice_not_found(self, api_client, mock_odoo_client):
        """Should return not_found when no invoice exists."""
        mock_odoo_client.get_invoice_status.return_value = None

        response = await api_client.get("/invoices/by-ticket/T-NOINVOICE")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "not_found"

    async def test_create_invoice_success(self, api_client, mock_ticket_client, mock_mq_publisher, mock_ticket_data):
        """Should queue invoice creation."""
        mock_ticket_client.get_ticket.return_value = mock_ticket_data

        payload = {
            "amount": 300.00,
            "description": "Consulting services"
        }

        response = await api_client.post("/invoices/T-123", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["amount"] == 300.00

        # Verify MQ was called
        mock_mq_publisher.publish.assert_called_once()

    async def test_create_invoice_validation_error(self, api_client):
        """Should reject invalid invoice data."""
        payload = {
            "amount": -100  # Negative amount
        }

        response = await api_client.post("/invoices/T-123", json=payload)

        assert response.status_code == 422


@pytest.mark.asyncio
class TestEmailsEndpoints:
    """Test email-related API endpoints."""

    async def test_send_email_success(self, api_client, mock_mq_publisher):
        """Should queue email sending."""
        payload = {
            "to_email": "customer@example.com",
            "subject": "Test Email",
            "body": "<p>Hello World</p>",
            "from_name": "Support Team"
        }

        response = await api_client.post("/emails", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["to_email"] == "customer@example.com"

        # Verify MQ publish
        mock_mq_publisher.publish.assert_called_once()

    async def test_send_email_validation_error(self, api_client):
        """Should reject invalid email data."""
        payload = {
            "to_email": "invalid-email",
            "subject": "",
            "body": "Body"
        }

        response = await api_client.post("/emails", json=payload)

        assert response.status_code == 422


@pytest.mark.asyncio
class TestCustomersEndpoints:
    """Test customer context endpoint."""

    async def test_get_customer_context_success(
        self, api_client, mock_ticket_client, mock_odoo_client, mock_ticket_data, mock_invoice_data
    ):
        """Should return full customer context."""
        mock_ticket_client.list_tickets.return_value = [mock_ticket_data]
        mock_odoo_client.list_customer_invoices.return_value = [mock_invoice_data]

        response = await api_client.get("/customers/john@example.com/context")

        assert response.status_code == 200
        data = response.json()
        assert data["customer_email"] == "john@example.com"
        assert "summary" in data
        assert data["summary"]["total_tickets"] == 1

    async def test_get_customer_context_invalid_email(self, api_client):
        """Should reject invalid email."""
        response = await api_client.get("/customers/invalid-email/context")

        assert response.status_code == 400
        data = response.json()
        assert "Invalid email" in data["detail"]["error"]


@pytest.mark.asyncio
class TestErrorHandling:
    """Test error handling and sanitization."""

    async def test_sanitizes_internal_errors(self, api_client, mock_ticket_client):
        """Should not expose internal error details to the client."""
        mock_ticket_client.get_ticket.side_effect = Exception("Database connection failed: secret_password_here")

        response = await api_client.get("/tickets/T-123")

        assert response.status_code == 500
        data = response.json()
        # Endpoint-specific handler returns a sanitized detail dict
        assert "detail" in data
        assert "request_id" in data["detail"]
        # The raw exception (with secret) must not leak to the response
        assert "secret_password" not in str(data)

    async def test_circuit_breaker_returns_503(self, api_client, mock_ticket_client):
        """Should return 503 when circuit breaker is open."""
        # CircuitBreakerError is raised by the decorator when circuit is open
        # We need to simulate the circuit being open by raising it manually
        mock_ticket_client.get_ticket.side_effect = pybreaker.CircuitBreakerError("Circuit open")

        response = await api_client.get("/tickets/T-123")

        assert response.status_code == 503
        data = response.json()
        # Error response structure is {"error": "message", "request_id": "..."}
        assert "temporarily unavailable" in data["error"].lower()


@pytest.mark.asyncio
class TestClientInitialization:
    """Test client availability checks."""

    async def test_returns_503_when_clients_not_initialized(self, api_client):
        """Should return 503 when required clients unavailable."""
        with patch('agentic_service.api.ticket_client', None), \
             patch('agentic_service.server.ticket_client', None):
            response = await api_client.get("/tickets/T-123")

        assert response.status_code == 503
        data = response.json()
        # detail is now a dict with error response structure
        assert "unavailable" in data["detail"]["error"].lower()
