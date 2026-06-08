"""E2E tests for error handling across all entry points."""

from unittest.mock import AsyncMock

import pytest

from agentic_service.tools.customer_tools import _get_customer_context as get_customer_context
from agentic_service.tools.email_tools import _send_email as send_email
from agentic_service.tools.invoice_tools import _get_invoice_status as get_invoice_status
from agentic_service.tools.ticket_tools import (
    _create_ticket as create_ticket,
)
from agentic_service.tools.ticket_tools import (
    _get_ticket_status as get_ticket_status,
)
from agentic_service.tools.ticket_tools import (
    _resolve_ticket as resolve_ticket,
)


@pytest.mark.asyncio
class TestApiErrors:
    """Error handling through REST API."""

    async def test_get_ticket_not_found(self, api_client):
        """E2E: Return 404 for non-existent ticket."""
        response = await api_client.get("/tickets/T-NONEXISTENT")
        assert response.status_code == 404
        data = response.json()
        detail = data.get("detail", data)
        assert "error" in detail
        assert "request_id" in detail

    async def test_get_invoice_not_found(self, api_client):
        """E2E: Return 404 for non-existent invoice."""
        response = await api_client.get("/invoices/by-ticket/T-NOINVOICE")
        assert response.status_code == 404
        data = response.json()
        detail = data.get("detail", data)
        assert "error" in detail

    async def test_create_ticket_invalid_email(self, api_client):
        """E2E: Reject invalid email in ticket creation."""
        response = await api_client.post("/tickets", json={
            "title": "Test",
            "description": "Desc",
            "customer_email": "not-an-email",
            "customer_name": "Test",
            "priority": "medium",
        })
        assert response.status_code == 422

    async def test_create_ticket_missing_fields(self, api_client):
        """E2E: Reject missing required fields."""
        response = await api_client.post("/tickets", json={
            "title": "",
            "description": "Desc",
            "customer_email": "test@example.com",
            "customer_name": "Test",
        })
        assert response.status_code == 422

    async def test_invalid_email_format(self, api_client):
        """E2E: Return 400 for invalid email format."""
        response = await api_client.get("/customers/invalid-email/context")
        assert response.status_code == 400
        data = response.json()
        detail = data.get("detail", data)
        assert "error" in detail

    async def test_service_uninitialized(self, api_client):
        """E2E: Return 503 when service not initialized."""
        from agentic_service.api import app
        original = app.state.service
        app.state.service = None
        try:
            response = await api_client.get("/tickets/T-123")
            assert response.status_code == 503
            data = response.json()
            detail = data.get("detail", data)
            assert "unavailable" in detail["error"].lower()
        finally:
            app.state.service = original

    async def test_circuit_breaker_error(self, api_client):
        """E2E: Return 503 when circuit breaker is open."""
        import pybreaker

        from agentic_service.api import app
        from agentic_service.service import GaramaticService
        from tests.fakes import FakeMQ, FakeOdooClient, FakeTicketClient

        fake_ticket = FakeTicketClient()
        fake_ticket.tickets["T-123"] = {
            "id": "T-123",
            "title": "Test",
            "description": "Test",
            "customerId": "test@example.com",
            "customerName": "Test",
            "status": "open",
            "createdAt": "2024-01-01T00:00:00Z",
        }
        fake_ticket.get_ticket = AsyncMock(
            side_effect=pybreaker.CircuitBreakerError("Circuit open")
        )
        fake_odoo = FakeOdooClient()
        fake_mq = FakeMQ()
        service = GaramaticService(fake_ticket, fake_odoo, fake_mq)

        original = app.state.service
        app.state.service = service
        try:
            response = await api_client.get("/tickets/T-123")
            assert response.status_code == 503
            data = response.json()
            detail = data.get("detail", data)
            assert "unavailable" in detail["error"].lower()
        finally:
            app.state.service = original


@pytest.mark.asyncio
class TestMcpErrors:
    """Error handling through MCP tools."""

    async def test_ticket_not_found_mcp(self, mcp_client):
        """E2E: Handle ticket not found via MCP."""
        service = mcp_client.request_context.lifespan_context["service"]
        result = await get_ticket_status(ticket_id="T-NONEXISTENT", service=service)
        assert result["status"] == "not_found"
        assert "error" in result

    async def test_invoice_not_found_mcp(self, mcp_client):
        """E2E: Handle invoice not found via MCP."""
        service = mcp_client.request_context.lifespan_context["service"]
        result = await get_invoice_status(ticket_id="T-NOINVOICE", service=service)
        assert result["status"] == "not_found"
        assert "error" in result

    async def test_invalid_email_mcp(self, mcp_client):
        """E2E: Handle invalid email via MCP."""
        service = mcp_client.request_context.lifespan_context["service"]
        result = await get_customer_context(customer_email="invalid-email", service=service)
        assert "error" in result

    async def test_create_ticket_validation_error(self, mcp_client):
        """E2E: Handle validation error via MCP."""
        result = await create_ticket(
            title="", description="Desc",
            customer_email="test@example.com", customer_name="Test",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        assert "error" in result

    async def test_resolve_invalid_amount(self, mcp_client):
        """E2E: Handle negative billable amount via MCP."""
        result = await resolve_ticket(
            ticket_id="T-123", resolution_notes="Fixed",
            billable_amount=-100.0, service=mcp_client.request_context.lifespan_context["service"],
        )
        assert "error" in result

    async def test_send_email_invalid_email(self, mcp_client):
        """E2E: Handle invalid email in send_email via MCP."""
        result = await send_email(
            to_email="invalid-email", subject="Test", body="Body",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        assert "error" in result


@pytest.mark.asyncio
class TestSanitization:
    """Test error sanitization in responses."""

    async def test_error_contains_request_id(self, api_client):
        """E2E: Error responses should contain request_id for tracing."""
        response = await api_client.get("/tickets/NONEXISTENT")
        data = response.json()
        detail = data.get("detail", data)
        assert "request_id" in detail
        assert len(detail["request_id"]) > 0
