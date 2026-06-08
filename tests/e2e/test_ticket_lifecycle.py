"""E2E tests for ticket lifecycle workflows."""

import pytest

from agentic_service.tools.invoice_tools import _create_invoice as create_invoice
from agentic_service.tools.ticket_tools import (
    _create_ticket as create_ticket,
)
from agentic_service.tools.ticket_tools import (
    _get_ticket_status as get_ticket_status,
)
from agentic_service.tools.ticket_tools import (
    _list_customer_tickets as list_customer_tickets,
)
from agentic_service.tools.ticket_tools import (
    _resolve_ticket as resolve_ticket,
)


@pytest.mark.asyncio
class TestTicketLifecycleApi:
    """Full ticket lifecycle through REST API."""

    async def test_create_ticket(self, api_client, fake_ticket_client):
        """E2E: Create a ticket via API and verify it exists."""
        payload = {
            "title": "Network Issue",
            "description": "Customer cannot connect to VPN",
            "customer_email": "john@example.com",
            "customer_name": "John Doe",
            "priority": "high",
        }
        response = await api_client.post("/tickets", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "created"
        ticket_id = data["ticket_id"]
        assert ticket_id.startswith("T-")
        assert ticket_id in fake_ticket_client.tickets
        assert fake_ticket_client.tickets[ticket_id]["title"] == "Network Issue"

    async def test_create_and_get_ticket(self, api_client, fake_ticket_client):
        """E2E: Create a ticket then retrieve it."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Printer Issue",
            "description": "Printer not responding",
            "customer_email": "jane@example.com",
            "customer_name": "Jane Doe",
            "priority": "medium",
        })
        assert create_resp.status_code == 200
        ticket_id = create_resp.json()["ticket_id"]
        get_resp = await api_client.get(f"/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["ticket_id"] == ticket_id
        assert data["status"] == "open"
        assert data["title"] == "Printer Issue"

    async def test_create_and_list_tickets(self, api_client, fake_ticket_client):
        """E2E: Create multiple tickets and list them."""
        for i in range(2):
            await api_client.post("/tickets", json={
                "title": f"Issue {i}",
                "description": f"Desc {i}",
                "customer_email": "alice@example.com",
                "customer_name": "Alice",
                "priority": "low",
            })
        list_resp = await api_client.get("/tickets")
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    async def test_full_lifecycle(self, api_client, fake_ticket_client, fake_mq):
        """E2E: Create → Get → Resolve → Invoice."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Full Lifecycle Test",
            "description": "Testing the full workflow",
            "customer_email": "lifecycle@example.com",
            "customer_name": "Lifecycle User",
            "priority": "high",
        })
        ticket_id = create_resp.json()["ticket_id"]
        get_resp = await api_client.get(f"/tickets/{ticket_id}")
        assert get_resp.json()["status"] == "open"
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed everything",
            "billable_amount": 200.00,
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "resolved"
        assert "invoice" in data
        assert fake_ticket_client.tickets[ticket_id]["status"] == "resolved"
        assert len(fake_mq.published) == 1

    async def test_resolve_without_invoice(self, api_client, fake_ticket_client):
        """E2E: Resolve a ticket without billable amount."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Non-billable",
            "description": "No charge",
            "customer_email": "free@example.com",
            "customer_name": "Free User",
            "priority": "low",
        })
        ticket_id = create_resp.json()["ticket_id"]
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Quick fix",
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "resolved"
        assert "invoice" not in data


@pytest.mark.asyncio
class TestTicketLifecycleMcp:
    """Full ticket lifecycle through MCP tools."""

    async def test_create_ticket_mcp(self, mcp_client, fake_ticket_client):
        """E2E: Create ticket via MCP tool."""
        result = await create_ticket(
            title="MCP Ticket", description="Created via MCP",
            customer_email="mcp@example.com", customer_name="MCP User",
            priority="medium", service=mcp_client.request_context.lifespan_context["service"],
        )
        assert result["status"] == "success"
        assert result["ticket_id"].startswith("T-")

    async def test_get_ticket_status_mcp(self, mcp_client, fake_ticket_client):
        """E2E: Create ticket then get status via MCP."""
        create_result = await create_ticket(
            title="MCP Status Test", description="Testing get_ticket_status",
            customer_email="status@example.com", customer_name="Status User",
            priority="high", service=mcp_client.request_context.lifespan_context["service"],
        )
        ticket_id = create_result["ticket_id"]
        service = mcp_client.request_context.lifespan_context["service"]
        status_result = await get_ticket_status(ticket_id=ticket_id, service=service)
        assert status_result["status"] == "success"
        assert status_result["ticket_id"] == ticket_id

    async def test_resolve_ticket_mcp(self, mcp_client, fake_ticket_client, fake_mq):
        """E2E: Create and resolve ticket via MCP."""
        create_result = await create_ticket(
            title="MCP Resolve", description="To be resolved",
            customer_email="resolve@example.com", customer_name="Resolve User",
            priority="high", service=mcp_client.request_context.lifespan_context["service"],
        )
        ticket_id = create_result["ticket_id"]
        resolve_result = await resolve_ticket(
            ticket_id=ticket_id, resolution_notes="Resolved via MCP",
            billable_amount=150.00, service=mcp_client.request_context.lifespan_context["service"],
        )
        assert resolve_result["status"] == "success"
        assert "invoice" in resolve_result
        assert len(fake_mq.published) == 1

    async def test_list_and_create_invoice_mcp(self, mcp_client, fake_ticket_client, fake_mq):
        """E2E: Create ticket, list tickets, create invoice via MCP."""
        service = mcp_client.request_context.lifespan_context["service"]
        await create_ticket(
            title="Invoice Test", description="For invoice",
            customer_email="invoice@example.com", customer_name="Invoice User",
            priority="medium", service=service,
        )
        list_result = await list_customer_tickets(
            customer_email="invoice@example.com",
            service=service,
        )
        assert "tickets" in list_result
        assert len(list_result["tickets"]) == 1
        ticket_id = list_result["tickets"][0]["ticket_id"]
        invoice_result = await create_invoice(
            ticket_id=ticket_id,
            amount=300.00,
            description="MCP invoice",
            service=service,
        )
        assert invoice_result["status"] == "success"
        assert invoice_result["ticket_id"] == ticket_id

    async def test_ticket_not_found_mcp(self, mcp_client):
        """E2E: Get non-existent ticket via MCP."""
        service = mcp_client.request_context.lifespan_context["service"]
        result = await get_ticket_status(ticket_id="T-NONEXISTENT", service=service)
        assert result["status"] == "not_found"
