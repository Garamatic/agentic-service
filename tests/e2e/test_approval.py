"""E2E tests for approval workflow.

Tests the human-in-the-loop approval for high-value invoices.
"""

import pytest

from agentic_service.server import create_ticket, list_customer_tickets, resolve_ticket


@pytest.mark.asyncio
class TestApprovalWorkflow:
    """End-to-end approval workflow tests."""

    async def test_approval_required_api(self, api_client, fake_ticket_client):
        """E2E: High-value invoice requires approval via API."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Expensive Issue",
            "description": "Critical fix",
            "customer_email": "expensive@example.com",
            "customer_name": "Expensive User",
            "priority": "high",
        })
        ticket_id = create_resp.json()["ticket_id"]

        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed everything",
            "billable_amount": 600.00,
        })
        assert resolve_resp.status_code == 402
        data = resolve_resp.json()
        detail = data.get("detail", data)
        assert "approval_id" in detail
        assert "error" in detail

    async def test_approval_auto_approve_below_threshold_api(self, api_client, fake_ticket_client):
        """E2E: Low-value invoice auto-approved via API."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Cheap Issue",
            "description": "Simple fix",
            "customer_email": "cheap@example.com",
            "customer_name": "Cheap User",
            "priority": "low",
        })
        ticket_id = create_resp.json()["ticket_id"]

        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Quick fix",
            "billable_amount": 100.00,
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "resolved"
        assert "invoice" in data

    async def test_approval_required_mcp(self, mcp_client, fake_ticket_client):
        """E2E: High-value invoice requires approval via MCP."""
        await create_ticket(
            title="MCP Expensive",
            description="Critical",
            customer_email="mcp-expensive@example.com",
            customer_name="MCP Expensive",
            priority="high",
            ctx=mcp_client,
        )

        list_result = await list_customer_tickets(
            customer_email="mcp-expensive@example.com",
            ctx=mcp_client,
        )
        ticket_id = list_result["tickets"][0]["ticket_id"]

        result = await resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes="Fixed",
            billable_amount=600.00,
            ctx=mcp_client,
        )
        assert result["status"] == "approval_required"
        assert "approval_id" in result

    async def test_approval_approved_api(self, api_client, fake_ticket_client, e2e_service):
        """E2E: Approved invoice can be resolved via API."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Approved API",
            "description": "For approval",
            "customer_email": "approved-api@example.com",
            "customer_name": "Approved API",
            "priority": "medium",
        })
        ticket_id = create_resp.json()["ticket_id"]

        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed",
            "billable_amount": 600.00,
        })
        assert resolve_resp.status_code == 402
        data = resolve_resp.json()
        detail = data.get("detail", data)
        approval_id = detail["approval_id"]

        # Approve via the API endpoint
        approve_resp = await api_client.post(f"/approvals/{approval_id}/approve")
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"

        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed",
            "billable_amount": 600.00,
            "approval_id": approval_id,
        })
        assert resolve_resp.status_code == 200
        data = resolve_resp.json()
        assert data["status"] == "resolved"

    async def test_approval_rejected_api(self, api_client, fake_ticket_client):
        """E2E: Rejected approval cannot be used."""
        create_resp = await api_client.post("/tickets", json={
            "title": "Rejected Approval",
            "description": "For rejection",
            "customer_email": "rejected-api@example.com",
            "customer_name": "Rejected API",
            "priority": "medium",
        })
        ticket_id = create_resp.json()["ticket_id"]

        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed",
            "billable_amount": 600.00,
        })
        assert resolve_resp.status_code == 402
        approval_id = resolve_resp.json()["detail"]["approval_id"]

        # Reject via the API endpoint
        reject_resp = await api_client.post(f"/approvals/{approval_id}/reject")
        assert reject_resp.status_code == 200
        assert reject_resp.json()["status"] == "rejected"

        # Rejecting again should 404
        reject_again = await api_client.post(f"/approvals/{approval_id}/reject")
        assert reject_again.status_code == 404

        # Resolving with rejected approval_id should 402 again
        resolve_resp = await api_client.post(f"/tickets/{ticket_id}/resolve", json={
            "resolution_notes": "Fixed",
            "billable_amount": 600.00,
            "approval_id": approval_id,
        })
        assert resolve_resp.status_code == 402

    async def test_approval_store_operations(self, e2e_service):
        """E2E: Approval store lifecycle."""
        store = e2e_service.approval_store
        if not store:
            pytest.skip("No approval store configured")

        await store.initialize()

        approval_id = await store.create_request("create invoice", 1000.0)
        assert approval_id.startswith("ap-")
        assert await store.is_approved(approval_id) is False

        result = await store.approve(approval_id)
        assert result is True
        assert await store.is_approved(approval_id) is True

        result = await store.reject(approval_id)
        assert result is False

    async def test_invalid_approval_id(self, e2e_service):
        """E2E: Invalid approval ID is rejected."""
        store = e2e_service.approval_store
        if not store:
            pytest.skip("No approval store configured")

        await store.initialize()

        result = await store.approve("ap-nonexistent")
        assert result is False
        assert await store.is_approved("ap-nonexistent") is False
