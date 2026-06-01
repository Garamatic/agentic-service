"""E2E tests for email, customer context, and cross-cutting workflows."""

import pytest

from agentic_service.server import (
    get_customer_context,
    send_email,
)


@pytest.mark.asyncio
class TestEmailWorkflow:
    """End-to-end email workflow tests."""

    async def test_send_email_api(self, api_client, fake_mq):
        """E2E: Send an email via API and verify it was queued."""
        payload = {
            "to_email": "customer@example.com",
            "subject": "Test Email",
            "body": "<p>Hello from e2e test</p>",
            "from_name": "Garamatic Support",
        }

        response = await api_client.post("/emails", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["status"] == "queued"
        assert data["data"]["to_email"] == "customer@example.com"

        # Verify MQ message was published
        assert len(fake_mq.published) == 1
        routing_key, message = fake_mq.published[0]
        assert routing_key == "email.send"
        assert message["to_email"] == "customer@example.com"

    async def test_send_email_mcp(self, mcp_client, fake_mq):
        """E2E: Send an email via MCP tool."""
        result = await send_email(
            to_email="mcp-customer@example.com",
            subject="MCP Email",
            body="<p>Sent via MCP</p>",
            from_name="MCP Support",
            ctx=mcp_client,
        )
        assert result["status"] == "queued"
        assert result["to_email"] == "mcp-customer@example.com"
        assert result["subject"] == "MCP Email"

        # Verify MQ
        assert len(fake_mq.published) == 1
        routing_key, message = fake_mq.published[0]
        assert routing_key == "email.send"

    async def test_send_email_validation_error(self, api_client):
        """E2E: Reject invalid email via API."""
        payload = {
            "to_email": "invalid-email",
            "subject": "Bad Email",
            "body": "test",
            "from_name": "Support",
        }

        response = await api_client.post("/emails", json=payload)
        # FastAPI EmailStr validation returns 422
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data


@pytest.mark.asyncio
class TestCustomerContext:
    """End-to-end customer context workflow tests."""

    async def test_customer_context_api(self, api_client, fake_ticket_client, fake_odoo_client, fake_mq):
        """E2E: Build customer context with tickets and invoices."""
        customer_email = "context@example.com"

        # Create two tickets
        for i in range(2):
            await api_client.post("/tickets", json={
                "title": f"Context Ticket {i}",
                "description": f"Desc {i}",
                "customer_email": customer_email,
                "customer_name": "Context User",
                "priority": "medium",
            })

        # Create an invoice for one of the tickets
        ticket_id = list(fake_ticket_client.tickets.keys())[0]
        await api_client.post(f"/invoices/{ticket_id}", json={
            "amount": 100.00,
            "description": "Context invoice",
        })

        # Simulate Odoo receiving the invoice from MQ
        fake_odoo_client.invoices[ticket_id] = {
            "invoice_id": "INV-001",
            "ticket_id": ticket_id,
            "customer_email": customer_email,
            "status": "sent",
            "amount": 100.00,
            "created_at": "2024-01-15T10:30:00Z",
            "paid_at": None,
        }

        # Get customer context
        response = await api_client.get(f"/customers/{customer_email}/context")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["customer_email"] == customer_email
        assert data["summary"]["total_tickets"] == 2
        assert data["summary"]["total_invoiced"] == 100.00
        assert len(data["recent_tickets"]) == 2
        assert len(data["recent_invoices"]) == 1

    async def test_customer_context_mcp(self, mcp_client, fake_ticket_client, fake_odoo_client):
        """E2E: Get customer context via MCP tool."""
        from agentic_service.server import create_ticket

        customer_email = "mcp-context@example.com"

        # Create ticket via MCP
        await create_ticket(
            title="MCP Context Ticket",
            description="For context test",
            customer_email=customer_email,
            customer_name="MCP Context User",
            priority="low",
            ctx=mcp_client,
        )

        # Get context via MCP
        result = await get_customer_context(customer_email, ctx=mcp_client)
        assert result["status"] == "success"
        assert result["customer_email"] == customer_email
        assert result["summary"]["total_tickets"] == 1
        assert len(result["recent_tickets"]) == 1

    async def test_customer_context_invalid_email_api(self, api_client):
        """E2E: Reject invalid email for customer context."""
        response = await api_client.get("/customers/invalid-email/context")
        assert response.status_code == 400
        data = response.json()
        assert "error" in data


@pytest.mark.asyncio
class TestHealthCheck:
    """End-to-end health check tests."""

    async def test_health_check_api(self, api_client):
        """E2E: Verify health endpoint returns healthy."""
        response = await api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["status"] == "healthy"
