"""Shared test fixtures for agentic-service tests."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient as HTTPXAsyncClient
import respx


@pytest.fixture
def mock_ticket_data():
    """Sample ticket data for tests."""
    return {
        "id": "T-123",
        "title": "Network Issue",
        "description": "Customer cannot connect to VPN",
        "status": "open",
        "customerEmail": "john@example.com",
        "customerName": "John Doe",
        "priority": "high",
        "createdAt": "2024-01-15T10:30:00Z",
        "resolvedAt": None,
        "resolutionNotes": None,
        "billableAmount": None
    }


@pytest.fixture
def mock_resolved_ticket():
    """Sample resolved ticket data."""
    return {
        "id": "T-456",
        "title": "Email Setup",
        "description": "Configure email client",
        "status": "resolved",
        "customerEmail": "jane@example.com",
        "customerName": "Jane Smith",
        "priority": "medium",
        "createdAt": "2024-01-10T09:00:00Z",
        "resolvedAt": "2024-01-10T14:30:00Z",
        "resolutionNotes": "Configured Outlook with IMAP settings",
        "billableAmount": 150.00
    }


@pytest.fixture
def mock_invoice_data():
    """Sample invoice data."""
    return {
        "invoice_id": "INV-789",
        "ticket_id": "T-456",
        "status": "sent",
        "amount": 150.00,
        "created_at": "2024-01-10T14:35:00Z",
        "paid_at": None
    }


@pytest.fixture
def mock_ticket_client():
    """Mock TicketClient for unit tests."""
    client = MagicMock()
    client.get_ticket = AsyncMock()
    client.list_tickets = AsyncMock(return_value=[])
    client.create_ticket = AsyncMock()
    client.resolve_ticket = AsyncMock()
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_odoo_client():
    """Mock OdooBridgeClient for unit tests."""
    client = MagicMock()
    client.get_invoice_status = AsyncMock(return_value=None)
    client.list_customer_invoices = AsyncMock(return_value=[])
    client.close = AsyncMock()
    return client


@pytest.fixture
def mock_mq_publisher():
    """Mock RabbitMQPublisher for unit tests."""
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    publisher.close = AsyncMock()
    publisher.channel = MagicMock()  # Non-None to pass channel checks
    return publisher


@pytest_asyncio.fixture
async def mock_mcp_context():
    """Mock MCP Context with properly configured async info method."""
    ctx = MagicMock()
    ctx.info = AsyncMock()
    return ctx


@pytest.fixture
def valid_email():
    return "test@example.com"


@pytest.fixture
def invalid_emails():
    return [
        "invalid-email",
        "@example.com",
        "test@",
        "test@.com",
        "",
        None
    ]


@pytest.fixture
def respx_mock():
    """Provide respx mock router for HTTP mocking."""
    with respx.mock:
        yield respx


@pytest.fixture(autouse=True)
def reset_breakers():
    """Reset circuit breakers before each test."""
    from agentic_service.clients import ticket_api_breaker, odoo_bridge_breaker
    ticket_api_breaker.close()
    odoo_bridge_breaker.close()
    yield
    ticket_api_breaker.close()
    odoo_bridge_breaker.close()
