"""Tests for MCP server tools."""

from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Context

from agentic_service.server import (
    _generate_request_id,
    _validate_email,
    create_invoice,
    create_ticket,
    get_customer_context,
    get_ticket_status,
    list_customer_tickets,
    resolve_ticket,
    send_email,
)


class TestEmailValidation:
    """Test email validation logic."""

    @pytest.mark.parametrize("email", [
        "test@example.com",
        "user.name@domain.co.uk",
        "firstname+lastname@example.com",
        "user@subdomain.example.com"
    ])
    def test_valid_emails(self, email):
        """Should not raise for valid emails."""
        _validate_email(email)  # Should not raise

    @pytest.mark.parametrize("email", [
        "invalid-email",
        "@example.com",
        "test@",
        "test@.com",
        "",
        "test..test@example.com",
        "test@example",
        "test@.example.com"
    ])
    def test_invalid_emails(self, email):
        """Should raise ValueError for invalid emails."""
        with pytest.raises(ValueError, match="Invalid email"):
            _validate_email(email)


class TestRequestIdGeneration:
    """Test request ID generation."""

    def test_generates_unique_ids(self):
        """Should generate unique IDs."""
        id1 = _generate_request_id()
        id2 = _generate_request_id()
        assert id1 != id2
        assert len(id1) == 32  # hex uuid
        assert len(id2) == 32


@pytest.mark.asyncio
class TestGetTicketStatus:
    """Test get_ticket_status MCP tool."""

    async def test_returns_ticket_details(self, mock_ticket_client, mock_ticket_data, mock_mcp_context):
        """Should return formatted ticket details."""
        mock_ticket_client.get_ticket.return_value = mock_ticket_data

        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            result = await get_ticket_status("T-123", ctx=mock_mcp_context)

        assert result["ticket_id"] == "T-123"
        assert result["status"] == "open"
        assert result["customer_id"] == "john@example.com"
        assert result["title"] == "Network Issue"

    async def test_handles_unavailable_service(self):
        """Should handle when ticket service is unavailable."""
        with patch('agentic_service.server.ticket_client', None):
            ctx = MagicMock(spec=Context)
            result = await get_ticket_status("T-123", ctx=ctx)

        assert "error" in result
        assert result["ticket_id"] == "T-123"

    async def test_calls_ctx_info(self, mock_ticket_client, mock_ticket_data, mock_mcp_context):
        """Should log context info."""
        mock_ticket_client.get_ticket.return_value = mock_ticket_data

        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            await get_ticket_status("T-123", ctx=mock_mcp_context)

        mock_mcp_context.info.assert_called_once()


@pytest.mark.asyncio
class TestListCustomerTickets:
    """Test list_customer_tickets MCP tool."""

    async def test_returns_tickets_list(self, mock_ticket_client, mock_ticket_data, mock_mcp_context):
        """Should return list of customer tickets."""
        mock_ticket_client.list_tickets.return_value = [mock_ticket_data]

        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            result = await list_customer_tickets(
                "john@example.com",
                status="open",
                limit=5,
                ctx=mock_mcp_context
            )

        assert len(result["tickets"]) == 1
        assert result["tickets"][0]["ticket_id"] == "T-123"

        # Verify client called with correct params (fetches all, filters locally)
        mock_ticket_client.list_tickets.assert_called_once_with(
            customer_id="john@example.com",
            status="open",
            limit=5
        )

    async def test_validates_email(self, mock_ticket_client, mock_mcp_context):
        """Should validate email format."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            with pytest.raises(ValueError, match="Invalid email"):
                await list_customer_tickets("invalid-email", ctx=mock_mcp_context)


@pytest.mark.asyncio
class TestCreateTicket:
    """Test create_ticket MCP tool."""

    async def test_creates_ticket_successfully(self, mock_ticket_client, mock_mcp_context):
        """Should create ticket and return confirmation."""
        mock_ticket_client.create_ticket.return_value = {"id": "T-999"}

        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            result = await create_ticket(
                title="New Issue",
                description="Something broke",
                customer_email="test@example.com",
                customer_name="Test User",
                priority="high",
                ctx=mock_mcp_context
            )

        assert result["ticket_id"] == "T-999"
        assert result["status"] == "created"
        assert "message" in result

    async def test_validates_empty_title(self, mock_ticket_client, mock_mcp_context):
        """Should reject empty title."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            with pytest.raises(ValueError, match="Title is required"):
                await create_ticket(
                    title="   ",
                    description="Description",
                    customer_email="test@example.com",
                    customer_name="Test",
                    ctx=mock_mcp_context
                )

    async def test_validates_invalid_email(self, mock_ticket_client, mock_mcp_context):
        """Should reject invalid email."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            with pytest.raises(ValueError, match="Invalid email"):
                await create_ticket(
                    title="Issue",
                    description="Description",
                    customer_email="invalid-email",
                    customer_name="Test",
                    ctx=mock_mcp_context
                )


@pytest.mark.asyncio
class TestResolveTicket:
    """Test resolve_ticket MCP tool."""

    async def test_resolves_without_invoice(self, mock_ticket_client, mock_mcp_context):
        """Should resolve ticket without invoice."""
        mock_ticket_client.resolve_ticket.return_value = {
            "id": "T-123",
            "status": "resolved",
            "customerId": "test@example.com",
            "customerName": "Test User"
        }

        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            result = await resolve_ticket(
                ticket_id="T-123",
                resolution_notes="Fixed the issue",
                billable_amount=None,
                ctx=mock_mcp_context
            )

        assert result["status"] == "resolved"
        assert result["ticket_id"] == "T-123"
        assert "invoice" not in result

    async def test_resolves_with_invoice(self, mock_ticket_client, mock_mq_publisher, mock_mcp_context):
        """Should resolve with invoice info when billable and queue invoice."""
        mock_ticket_client.resolve_ticket.return_value = {
            "id": "T-123",
            "status": "resolved",
            "customerId": "test@example.com",
            "customerName": "Test User"
        }

        with patch('agentic_service.server.ticket_client', mock_ticket_client), \
             patch('agentic_service.server.mq_publisher', mock_mq_publisher):
            result = await resolve_ticket(
                ticket_id="T-123",
                resolution_notes="Fixed the issue",
                billable_amount=200.00,
                ctx=mock_mcp_context
            )

        assert result["status"] == "resolved"
        assert "invoice" in result
        assert result["invoice"]["estimated_amount"] == 200.00
        mock_mq_publisher.publish.assert_called_once()

    async def test_validates_negative_amount(self, mock_ticket_client, mock_mcp_context):
        """Should reject negative billable amount."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            with pytest.raises(ValueError, match="cannot be negative"):
                await resolve_ticket(
                    ticket_id="T-123",
                    resolution_notes="Fixed",
                    billable_amount=-50.00,
                    ctx=mock_mcp_context
                )

    async def test_validates_empty_notes(self, mock_ticket_client, mock_mcp_context):
        """Should reject empty resolution notes."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            with pytest.raises(ValueError, match="Resolution notes are required"):
                await resolve_ticket(
                    ticket_id="T-123",
                    resolution_notes="   ",
                    ctx=mock_mcp_context
                )


@pytest.mark.asyncio
class TestCreateInvoice:
    """Test create_invoice MCP tool."""

    async def test_queues_invoice_creation(self, mock_ticket_client, mock_mq_publisher, mock_ticket_data, mock_mcp_context):
        """Should queue invoice via RabbitMQ."""
        mock_ticket_client.get_ticket.return_value = mock_ticket_data

        with patch('agentic_service.server.ticket_client', mock_ticket_client), \
             patch('agentic_service.server.mq_publisher', mock_mq_publisher):
            result = await create_invoice(
                ticket_id="T-123",
                amount=250.00,
                description="Network troubleshooting",
                ctx=mock_mcp_context
            )

        assert result["status"] == "queued"
        assert result["amount"] == 250.00
        assert "message" in result

        # Verify MQ publish was called
        mock_mq_publisher.publish.assert_called_once()
        call_args = mock_mq_publisher.publish.call_args[0]
        assert call_args[0] == "invoice.create_requested"

    async def test_validates_positive_amount(self, mock_ticket_client, mock_mcp_context):
        """Should reject non-positive amounts."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client):
            with pytest.raises(ValueError, match="must be positive"):
                await create_invoice(
                    ticket_id="T-123",
                    amount=0,
                    ctx=mock_mcp_context
                )

    async def test_handles_unavailable_mq(self, mock_ticket_client, mock_mcp_context):
        """Should handle when MQ is unavailable."""
        with patch('agentic_service.server.ticket_client', mock_ticket_client), \
             patch('agentic_service.server.mq_publisher', None):
            result = await create_invoice(
                ticket_id="T-123",
                amount=100.00,
                ctx=mock_mcp_context
            )

        assert "error" in result
        assert result["status"] == "failed"


@pytest.mark.asyncio
class TestSendEmail:
    """Test send_email MCP tool."""

    async def test_queues_email(self, mock_mq_publisher, mock_mcp_context):
        """Should queue email via RabbitMQ."""
        with patch('agentic_service.server.mq_publisher', mock_mq_publisher):
            result = await send_email(
                to_email="customer@example.com",
                subject="Test Subject",
                body="<p>Test body</p>",
                from_name="Garamatic Support",
                ctx=mock_mcp_context
            )

        assert result["status"] == "queued"
        assert result["to_email"] == "customer@example.com"

        # Verify MQ publish
        mock_mq_publisher.publish.assert_called_once()
        call_args = mock_mq_publisher.publish.call_args[0]
        assert call_args[0] == "email.send"

    async def test_validates_email(self, mock_mq_publisher, mock_mcp_context):
        """Should validate recipient email."""
        with patch('agentic_service.server.mq_publisher', mock_mq_publisher):
            with pytest.raises(ValueError, match="Invalid email"):
                await send_email(
                    to_email="invalid-email",
                    subject="Subject",
                    body="Body",
                    ctx=mock_mcp_context
                )

    async def test_validates_empty_subject(self, mock_mq_publisher, mock_mcp_context):
        """Should reject empty subject."""
        with patch('agentic_service.server.mq_publisher', mock_mq_publisher):
            with pytest.raises(ValueError, match="subject is required"):
                await send_email(
                    to_email="test@example.com",
                    subject="   ",
                    body="Body",
                    ctx=mock_mcp_context
                )


@pytest.mark.asyncio
class TestGetCustomerContext:
    """Test get_customer_context MCP tool."""

    async def test_returns_full_context(
        self, mock_ticket_client, mock_odoo_client, mock_ticket_data, mock_invoice_data, mock_mcp_context
    ):
        """Should return comprehensive customer context."""
        mock_ticket_client.list_tickets.return_value = [mock_ticket_data]
        mock_odoo_client.list_customer_invoices.return_value = [mock_invoice_data]

        with patch('agentic_service.server.ticket_client', mock_ticket_client), \
             patch('agentic_service.server.odoo_client', mock_odoo_client):
            result = await get_customer_context("john@example.com", ctx=mock_mcp_context)

        assert result["customer_email"] == "john@example.com"
        assert "summary" in result
        assert result["summary"]["total_tickets"] == 1
        assert "recent_tickets" in result
        assert "recent_invoices" in result

    async def test_handles_service_unavailable(self, mock_mcp_context):
        """Should return error dict when services unavailable."""
        with patch('agentic_service.server.ticket_client', None), \
             patch('agentic_service.server.odoo_client', None):
            result = await get_customer_context("test@example.com", ctx=mock_mcp_context)

        assert result["status"] == "error"
        assert "unavailable" in result["error"].lower()
        assert result["customer_email"] == "test@example.com"
