"""E2E tests for edge cases and boundary conditions."""

import pytest

from agentic_service.tools.customer_tools import _get_customer_context as get_customer_context
from agentic_service.tools.ticket_tools import _create_ticket as create_ticket


@pytest.mark.asyncio
class TestApiEdgeCases:
    """Edge cases through REST API."""

    async def test_empty_ticket_list(self, api_client):
        """E2E: Handle empty ticket list gracefully."""
        response = await api_client.get("/tickets")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert data == []

    async def test_large_limit(self, api_client, fake_ticket_client):
        """E2E: Handle limit at max boundary (100)."""
        for i in range(5):
            await api_client.post("/tickets", json={
                "title": f"Ticket {i}",
                "description": "Desc",
                "customer_email": "bulk@example.com",
                "customer_name": "Bulk",
                "priority": "medium",
            })
        response = await api_client.get("/tickets?limit=100")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 5

    async def test_limit_too_high(self, api_client):
        """E2E: Limit above max should fail validation."""
        response = await api_client.get("/tickets?limit=200")
        assert response.status_code == 422

    async def test_zero_limit(self, api_client):
        """E2E: Zero limit should fail validation."""
        response = await api_client.get("/tickets?limit=0")
        assert response.status_code == 422

    async def test_very_long_title(self, api_client):
        """E2E: Handle long title (200 chars)."""
        long_title = "A" * 200
        response = await api_client.post("/tickets", json={
            "title": long_title,
            "description": "Desc",
            "customer_email": "long@example.com",
            "customer_name": "Long",
            "priority": "medium",
        })
        assert response.status_code == 200

    async def test_title_too_long(self, api_client):
        """E2E: Title over 200 chars should fail."""
        too_long = "A" * 201
        response = await api_client.post("/tickets", json={
            "title": too_long,
            "description": "Desc",
            "customer_email": "test@example.com",
            "customer_name": "Test",
            "priority": "medium",
        })
        assert response.status_code == 422

    async def test_unicode_characters(self, api_client):
        """E2E: Handle unicode characters in all fields."""
        response = await api_client.post("/tickets", json={
            "title": "日本語タイトル",
            "description": "Description with émojis 🎉 and ñoño",
            "customer_email": "unicode@example.com",
            "customer_name": "José García",
            "priority": "medium",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "created"

    async def test_billable_amount_zero(self, api_client, fake_ticket_client):
        """E2E: Zero billable amount should not create invoice."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Zero Bill",
            "description": "No charge",
            "customer_email": "zero@example.com",
            "customer_name": "Zero",
            "priority": "low",
        })
        ticket_id = create_resp.json()["ticket_id"]
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed",
            "billable_amount": 0.00,
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "resolved"
        assert "invoice" not in data

    async def test_billable_amount_none(self, api_client, fake_ticket_client):
        """E2E: None billable amount should not create invoice."""
        create_resp = await api_client.post("/tickets", json={
            "title": "None Bill",
            "description": "No charge",
            "customer_email": "none@example.com",
            "customer_name": "None",
            "priority": "low",
        })
        ticket_id = create_resp.json()["ticket_id"]
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed",
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "resolved"
        assert "invoice" not in data

    async def test_negative_amount_invoice(self, api_client):
        """E2E: Negative amount should fail Pydantic validation."""
        response = await api_client.post("/invoices/T-123", json={
            "amount": -100.00,
            "description": "Negative",
        })
        assert response.status_code == 422

    async def test_health_endpoint(self, api_client):
        """E2E: Health endpoint always returns 200."""
        response = await api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ["healthy", "degraded"]
        assert "services" in data


@pytest.mark.asyncio
class TestMcpEdgeCases:
    """Edge cases through MCP tools."""

    async def test_empty_customer_list(self, mcp_client):
        """E2E: List tickets for customer with no tickets."""
        from agentic_service.tools.ticket_tools import (
            _list_customer_tickets as list_customer_tickets,
        )
        result = await list_customer_tickets(
            customer_email="nobody@example.com",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        assert "tickets" in result
        assert result["tickets"] == []

    async def test_unicode_in_mcp(self, mcp_client):
        """E2E: Handle unicode in MCP tools."""
        result = await create_ticket(
            title="🎉 Unicode Party",
            description="日本語の説明",
            customer_email="unicode-mcp@example.com",
            customer_name="Ünicode Müller",
            priority="medium",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        assert result["status"] == "success"
        assert result["customer_email"] == "unicode-mcp@example.com"

    async def test_zero_amount_invoice_mcp(self, mcp_client, fake_ticket_client):
        """E2E: Zero amount invoice should fail validation."""
        from agentic_service.tools.invoice_tools import _create_invoice as create_invoice
        from agentic_service.tools.ticket_tools import _create_ticket as create_ticket
        from agentic_service.tools.ticket_tools import (
            _list_customer_tickets as list_customer_tickets,
        )
        # Create ticket via MCP first so fake_ticket_client is set up correctly
        await create_ticket(
            service=mcp_client.request_context.lifespan_context["service"],
            title="Zero Invoice Test",
            description="For zero amount invoice",
            customer_email="zero-invoice@example.com",
            customer_name="Zero Invoice",
            priority="medium",
        )
        # Get the ticket ID
        list_result = await list_customer_tickets(
            service=mcp_client.request_context.lifespan_context["service"],
            customer_email="zero-invoice@example.com",
        )
        ticket_id = list_result["tickets"][0]["ticket_id"]
        result = await create_invoice(
            service=mcp_client.request_context.lifespan_context["service"],
            ticket_id=ticket_id,
            amount=0.00,
            description="Zero amount",
        )
        assert result["status"] == "validation_error"
        assert "error" in result


@pytest.mark.asyncio
class TestStateConsistency:
    """Tests verifying state consistency across entry points."""

    async def test_api_and_mcp_see_same_tickets(self, api_client, mcp_client):
        """E2E: Tickets created via API are visible via MCP."""
        from agentic_service.tools.ticket_tools import (
            _list_customer_tickets as list_customer_tickets,
        )
        await api_client.post("/tickets", json={
            "title": "Cross-entry",
            "description": "Visible everywhere",
            "customer_email": "cross@example.com",
            "customer_name": "Cross",
            "priority": "medium",
        })
        result = await list_customer_tickets(
            customer_email="cross@example.com",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        assert "tickets" in result
        assert len(result["tickets"]) == 1
        assert result["tickets"][0]["title"] == "Cross-entry"

    async def test_mcp_and_api_see_same_tickets(self, api_client, mcp_client):
        """E2E: Tickets created via MCP are visible via API."""
        await create_ticket(
            title="MCP Cross-entry",
            description="Also visible via API",
            customer_email="mcp-cross@example.com",
            customer_name="MCP Cross",
            priority="medium",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        response = await api_client.get("/tickets?customer_email=mcp-cross@example.com")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["title"] == "MCP Cross-entry"

    async def test_invoice_reflected_in_context(self, api_client, mcp_client, fake_odoo_client):
        """E2E: Invoice created via API appears in customer context via MCP."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Invoice Context",
            "description": "Test",
            "customer_email": "invoice-context@example.com",
            "customer_name": "Invoice Context",
            "priority": "medium",
        })
        ticket_id = create_resp.json()["ticket_id"]
        await api_client.post(f"/invoices/{ticket_id}", json={
            "amount": 250.00,
            "description": "Context test",
        })
        fake_odoo_client.invoices[ticket_id] = {
            "invoice_id": "INV-001",
            "ticket_id": ticket_id,
            "customer_email": "invoice-context@example.com",
            "status": "sent",
            "amount": 250.00,
            "created_at": "2024-01-15T10:30:00Z",
            "paid_at": None,
        }
        result = await get_customer_context(
            customer_email="invoice-context@example.com",
            service=mcp_client.request_context.lifespan_context["service"],
        )
        assert result["status"] == "success"
        assert result["summary"]["total_tickets"] == 1
        assert result["summary"]["total_invoiced"] == 250.00
