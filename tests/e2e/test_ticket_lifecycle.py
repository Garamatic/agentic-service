"""E2E tests for ticket lifecycle workflows.

Tests full end-to-end workflows through the REST API:
- Create → Get → List → Resolve → Invoice
"""

import pytest

from agentic_service.server import (
    create_invoice,
    create_ticket,
    get_ticket_status,
    list_customer_tickets,
    resolve_ticket,
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
        assert data["status"] == "success"
        ticket_id = data["data"]["ticket_id"]
        assert ticket_id.startswith("T-")

        # Verify the ticket was stored in the fake client
        assert ticket_id in fake_ticket_client.tickets
        ticket = fake_ticket_client.tickets[ticket_id]
        assert ticket["title"] == "Network Issue"
        assert ticket["customerId"] == "john@example.com"

    async def test_create_and_get_ticket(self, api_client, fake_ticket_client):
        """E2E: Create a ticket then retrieve it."""
        # Create
        create_resp = await api_client.post("/tickets", json={
            "title": "Printer Issue",
            "description": "Printer not responding",
            "customer_email": "jane@example.com",
            "customer_name": "Jane Doe",
            "priority": "medium",
        })
        assert create_resp.status_code == 200
        ticket_id = create_resp.json()["data"]["ticket_id"]

        # Get
        get_resp = await api_client.get(f"/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        data = get_resp.json()["data"]
        assert data["ticket_id"] == ticket_id
        assert data["status"] == "open"
        assert data["title"] == "Printer Issue"

    async def test_create_and_list_tickets(self, api_client, fake_ticket_client):
        """E2E: Create multiple tickets and list them with filters."""
        # Create two tickets for the same customer
        for i in range(2):
            await api_client.post("/tickets", json={
                "title": f"Issue {i}",
                "description": f"Description {i}",
                "customer_email": "alice@example.com",
                "customer_name": "Alice",
                "priority": "low",
            })

        # List without filters
        list_resp = await api_client.get("/tickets")
        assert list_resp.status_code == 200
        data = list_resp.json()["data"]
        assert len(data) == 2

        # List with customer filter
        list_resp = await api_client.get("/tickets?customer_email=alice@example.com")
        assert list_resp.status_code == 200
        data = list_resp.json()["data"]
        assert len(data) == 2
        assert all(t["ticket_id"].startswith("T-") for t in data)

    async def test_full_lifecycle(self, api_client, fake_ticket_client, fake_mq):
        """E2E: Create → Get → Resolve → Invoice."""
        # 1. Create ticket
        create_resp = await api_client.post("/tickets", json={
            "title": "Full Lifecycle Test",
            "description": "Testing the full workflow",
            "customer_email": "lifecycle@example.com",
            "customer_name": "Lifecycle User",
            "priority": "high",
        })
        assert create_resp.status_code == 200
        ticket_id = create_resp.json()["data"]["ticket_id"]

        # 2. Get ticket
        get_resp = await api_client.get(f"/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["data"]["status"] == "open"

        # 3. Resolve ticket with billable amount
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed everything",
            "billable_amount": 200.00,
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()["data"]
        assert data["status"] == "resolved"
        assert "invoice" in data

        # 4. Verify ticket updated in fake client
        ticket = fake_ticket_client.tickets[ticket_id]
        assert ticket["status"] == "resolved"
        assert ticket["resolutionNotes"] == "Fixed everything"

        # 5. Verify invoice queued via MQ
        assert len(fake_mq.published) == 1
        routing_key, message = fake_mq.published[0]
        assert routing_key == "invoice.create_requested"
        assert message["ticket_id"] == ticket_id

    async def test_resolve_without_invoice(self, api_client, fake_ticket_client):
        """E2E: Resolve a ticket without billable amount."""
        # Create
        create_resp = await api_client.post("/tickets", json={
            "title": "Non-billable",
            "description": "No charge",
            "customer_email": "free@example.com",
            "customer_name": "Free User",
            "priority": "low",
        })
        ticket_id = create_resp.json()["data"]["ticket_id"]

        # Resolve without billable_amount
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Quick fix",
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()["data"]
        assert data["status"] == "resolved"
        assert "invoice" not in data


@pytest.mark.asyncio
class TestTicketLifecycleMcp:
    """Full ticket lifecycle through MCP tools."""

    async def test_create_ticket_mcp(self, mcp_client, fake_ticket_client):
        """E2E: Create ticket via MCP tool."""
        result = await create_ticket(
            title="MCP Ticket",
            description="Created via MCP",
            customer_email="mcp@example.com",
            customer_name="MCP User",
            priority="medium",
            ctx=mcp_client,
        )
        assert result["status"] == "created"
        assert result["ticket_id"].startswith("T-")
        assert result["customer_email"] == "mcp@example.com"

    async def test_get_ticket_status_mcp(self, mcp_client, fake_ticket_client):
        """E2E: Create ticket then get status via MCP."""
        # Create via MCP
        create_result = await create_ticket(
            title="MCP Status Test",
            description="Testing get_ticket_status",
            customer_email="status@example.com",
            customer_name="Status User",
            priority="high",
            ctx=mcp_client,
        )
        ticket_id = create_result["ticket_id"]

        # Get status via MCP
        status_result = await get_ticket_status(ticket_id, ctx=mcp_client)
        assert status_result["ticket_id"] == ticket_id
        assert status_result["status"] == "open"

    async def test_resolve_ticket_mcp(self, mcp_client, fake_ticket_client, fake_mq):
        """E2E: Create and resolve ticket via MCP."""
        # Create
        create_result = await create_ticket(
            title="MCP Resolve",
            description="To be resolved",
            customer_email="resolve@example.com",
            customer_name="Resolve User",
            priority="high",
            ctx=mcp_client,
        )
        ticket_id = create_result["ticket_id"]

        # Resolve
        resolve_result = await resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes="Resolved via MCP",
            billable_amount=150.00,
            ctx=mcp_client,
        )
        assert resolve_result["status"] == "resolved"
        assert resolve_result["resolution_notes"] == "Resolved via MCP"
        assert "invoice" in resolve_result

        # Verify MQ message
        assert len(fake_mq.published) == 1
        routing_key, message = fake_mq.published[0]
        assert routing_key == "invoice.create_requested"
        assert message["ticket_id"] == ticket_id

    async def test_list_and_create_invoice_mcp(self, mcp_client, fake_ticket_client, fake_mq):
        """E2E: Create ticket, list tickets, create invoice via MCP."""
        # Create ticket
        await create_ticket(
            title="Invoice Test",
            description="For invoice",
            customer_email="invoice@example.com",
            customer_name="Invoice User",
            priority="medium",
            ctx=mcp_client,
        )

        # List tickets
        list_result = await list_customer_tickets(
            customer_email="invoice@example.com",
            ctx=mcp_client,
        )
        assert list_result["status"] == "success"
        assert len(list_result["tickets"]) == 1

        ticket_id = list_result["tickets"][0]["ticket_id"]

        # Create invoice
        invoice_result = await create_invoice(
            ticket_id=ticket_id,
            amount=300.00,
            description="MCP invoice",
            ctx=mcp_client,
        )
        assert invoice_result["status"] == "queued"
        assert invoice_result["ticket_id"] == ticket_id
        assert invoice_result["amount"] == 300.00

        # Verify invoice queued via MQ
        assert len(fake_mq.published) == 1
        routing_key, message = fake_mq.published[0]
        assert routing_key == "invoice.create_requested"
        assert message["ticket_id"] == ticket_id

    async def test_ticket_not_found_mcp(self, mcp_client):
        """E2E: Get non-existent ticket via MCP."""
        result = await get_ticket_status("T-NONEXISTENT", ctx=mcp_client)
        assert result["status"] == "not_found"
        assert result["error"] == "Ticket not found"
