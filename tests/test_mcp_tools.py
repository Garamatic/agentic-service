"""Tests for MCP server tools.

These tests verify that MCP tools correctly call the service layer and
format responses. Service-level validation and business logic are tested
in test_service.py.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Context

from agentic_service.server import (
    create_invoice,
    create_ticket,
    get_customer_context,
    get_ticket_status,
    list_customer_tickets,
    resolve_ticket,
    send_email,
)
from agentic_service.service import (
    ValidationError,
    generate_request_id,
    validate_email,
)


class TestEmailValidation:
    """Test email validation helper."""

    @pytest.mark.parametrize("email", [
        "test@example.com",
        "user.name@domain.co.uk",
        "firstname+lastname@example.com",
        "user@subdomain.example.com"
    ])
    def test_valid_emails(self, email):
        """Should not raise for valid emails."""
        validate_email(email)  # Should not raise

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
        """Should raise ValidationError for invalid emails."""
        with pytest.raises(ValidationError, match="Invalid email"):
            validate_email(email)


class TestRequestIdGeneration:
    """Test request ID generation."""

    def test_generates_unique_ids(self):
        """Should generate unique IDs."""
        id1 = generate_request_id()
        id2 = generate_request_id()
        assert id1 != id2
        assert len(id1) == 32  # hex uuid
        assert len(id2) == 32


def _make_mock_ctx(**service_methods):
    """Create a mock MCP Context with a mock service in lifespan_context."""
    ctx = MagicMock(spec=Context)
    ctx.request_context.lifespan_context = {"service": _make_mock_service(**service_methods)}
    ctx.info = AsyncMock()
    return ctx


def _make_mock_service(**methods):
    """Create a mock GaramaticService with specified async methods."""
    service = MagicMock()
    for name, return_value in methods.items():
        setattr(service, name, AsyncMock(return_value=return_value))
    return service


# ------------------------------------------------------------------
# Ticket tools
# ------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetTicketStatus:
    """Test get_ticket_status MCP tool."""

    async def test_returns_ticket_details(self, mock_ticket_data):
        """Should return formatted ticket details."""
        from agentic_service.service import Ticket
        ticket = Ticket(
            id="T-123",
            status="open",
            customer_email="john@example.com",
            customer_name="John Doe",
            title="Network Issue",
            description="Customer cannot connect to VPN",
            created_at="2024-01-15T10:30:00Z",
            resolved_at=None,
            resolution_notes=None,
            billable_amount=None,
        )
        mock_ctx = _make_mock_ctx(get_ticket=ticket)

        result = await get_ticket_status("T-123", ctx=mock_ctx)

        assert result["ticket_id"] == "T-123"
        assert result["status"] == "open"
        assert result["customer_id"] == "john@example.com"
        assert result["title"] == "Network Issue"
        mock_ctx.request_context.lifespan_context["service"].get_ticket.assert_awaited_once_with("T-123")

    async def test_handles_not_found(self):
        """Should return not_found for missing ticket."""
        from agentic_service.service import NotFoundError
        svc = _make_mock_service()
        svc.get_ticket = AsyncMock(side_effect=NotFoundError("Not found"))
        ctx = MagicMock(spec=Context)
        ctx.request_context.lifespan_context = {"service": svc}

        result = await get_ticket_status("NONEXISTENT", ctx=ctx)

        assert result["status"] == "not_found"
        assert result["ticket_id"] == "NONEXISTENT"

    async def test_calls_ctx_info(self, mock_ticket_data):
        """Should log context info."""
        from agentic_service.service import Ticket
        ticket = Ticket(
            id="T-123",
            status="open",
            customer_email="john@example.com",
            customer_name="John Doe",
            title="Network Issue",
            description=None,
            created_at=None,
            resolved_at=None,
            resolution_notes=None,
            billable_amount=None,
        )
        mock_ctx = _make_mock_ctx(get_ticket=ticket)

        await get_ticket_status("T-123", ctx=mock_ctx)

        mock_ctx.info.assert_awaited_once()


@pytest.mark.asyncio
class TestListCustomerTickets:
    """Test list_customer_tickets MCP tool."""

    async def test_returns_tickets_list(self, mock_ticket_data):
        """Should return list of customer tickets."""
        from agentic_service.service import TicketSummary
        summary = TicketSummary(
            id="T-123",
            title="Network Issue",
            status="open",
            created_at="2024-01-15T10:30:00Z",
            resolved_at=None,
        )
        mock_ctx = _make_mock_ctx(list_tickets=[summary])

        result = await list_customer_tickets(
            "john@example.com",
            status="open",
            limit=5,
            ctx=mock_ctx
        )

        assert len(result["tickets"]) == 1
        assert result["tickets"][0]["ticket_id"] == "T-123"


@pytest.mark.asyncio
class TestCreateTicket:
    """Test create_ticket MCP tool."""

    async def test_creates_ticket_successfully(self):
        """Should create ticket and return confirmation."""
        from agentic_service.service import Ticket
        ticket = Ticket(
            id="T-999",
            status="open",
            customer_email="test@example.com",
            customer_name="Test User",
            title="New Issue",
            description="Something broke",
            created_at="2024-01-15T10:30:00Z",
            resolved_at=None,
            resolution_notes=None,
            billable_amount=None,
        )
        mock_ctx = _make_mock_ctx(create_ticket=ticket)

        result = await create_ticket(
            title="New Issue",
            description="Something broke",
            customer_email="test@example.com",
            customer_name="Test User",
            priority="high",
            ctx=mock_ctx
        )

        assert result["ticket_id"] == "T-999"
        assert result["status"] == "created"
        assert "message" in result


@pytest.mark.asyncio
class TestResolveTicket:
    """Test resolve_ticket MCP tool."""

    async def test_resolves_without_invoice(self):
        """Should resolve ticket without invoice."""
        from agentic_service.service import ResolutionResult
        result_obj = ResolutionResult(
            ticket_id="T-123",
            status="resolved",
            resolution_notes="Fixed the issue",
            invoice=None,
        )
        mock_ctx = _make_mock_ctx(resolve_ticket=result_obj)

        result = await resolve_ticket(
            ticket_id="T-123",
            resolution_notes="Fixed the issue",
            billable_amount=None,
            ctx=mock_ctx
        )

        assert result["status"] == "resolved"
        assert result["ticket_id"] == "T-123"
        assert "invoice" not in result

    async def test_resolves_with_invoice(self):
        """Should resolve with invoice info when billable."""
        from agentic_service.service import InvoiceQueueResult, ResolutionResult
        invoice = InvoiceQueueResult(
            status="queued",
            estimated_amount=200.00,
            message="Invoice queued",
        )
        result_obj = ResolutionResult(
            ticket_id="T-123",
            status="resolved",
            resolution_notes="Fixed the issue",
            invoice=invoice,
        )
        mock_ctx = _make_mock_ctx(resolve_ticket=result_obj)

        result = await resolve_ticket(
            ticket_id="T-123",
            resolution_notes="Fixed the issue",
            billable_amount=200.00,
            ctx=mock_ctx
        )

        assert result["status"] == "resolved"
        assert "invoice" in result
        assert result["invoice"]["estimated_amount"] == 200.00


# ------------------------------------------------------------------
# Invoice tools
# ------------------------------------------------------------------

@pytest.mark.asyncio
class TestCreateInvoice:
    """Test create_invoice MCP tool."""

    async def test_queues_invoice_creation(self):
        """Should queue invoice via RabbitMQ."""
        from agentic_service.service import InvoiceQueueResult
        result_obj = InvoiceQueueResult(
            status="queued",
            estimated_amount=250.00,
            message="Invoice queued",
        )
        mock_ctx = _make_mock_ctx(queue_invoice=result_obj)

        result = await create_invoice(
            ticket_id="T-123",
            amount=250.00,
            description="Network troubleshooting",
            ctx=mock_ctx
        )

        assert result["status"] == "queued"
        assert result["amount"] == 250.00
        assert "message" in result
        mock_ctx.request_context.lifespan_context["service"].queue_invoice.assert_awaited_once()

    async def test_handles_unavailable_mq(self):
        """Should handle when MQ is unavailable."""
        from agentic_service.service import InvoiceQueueResult
        result_obj = InvoiceQueueResult(
            status="unavailable",
            estimated_amount=100.00,
            message="Invoice service unavailable",
        )
        mock_ctx = _make_mock_ctx(queue_invoice=result_obj)

        result = await create_invoice(
            ticket_id="T-123",
            amount=100.00,
            ctx=mock_ctx
        )

        assert result["status"] == "unavailable"


# ------------------------------------------------------------------
# Email tools
# ------------------------------------------------------------------

@pytest.mark.asyncio
class TestSendEmail:
    """Test send_email MCP tool."""

    async def test_queues_email(self):
        """Should queue email via RabbitMQ."""
        from agentic_service.service import EmailQueueResult
        result_obj = EmailQueueResult(
            status="queued",
            to_email="customer@example.com",
            subject="Test Subject",
            message="Email queued",
        )
        mock_ctx = _make_mock_ctx(send_email=result_obj)

        result = await send_email(
            to_email="customer@example.com",
            subject="Test Subject",
            body="<p>Hello World</p>",
            from_name="Support Team",
            ctx=mock_ctx
        )

        assert result["status"] == "queued"
        assert result["to_email"] == "customer@example.com"
        mock_ctx.request_context.lifespan_context["service"].send_email.assert_awaited_once()


# ------------------------------------------------------------------
# Customer context
# ------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetCustomerContext:
    """Test get_customer_context MCP tool."""

    async def test_returns_full_context(self, mock_ticket_data, mock_invoice_data):
        """Should return comprehensive customer context."""
        from agentic_service.service import (
            CustomerContext,
            CustomerSummary,
            InvoiceSummary,
            TicketSummary,
        )
        context = CustomerContext(
            customer_email="john@example.com",
            summary=CustomerSummary(
                total_tickets=1,
                open_tickets=1,
                total_invoiced=150.00,
                outstanding_balance=150.00,
                lifetime_value=0.0,
            ),
            recent_tickets=[TicketSummary(
                id="T-123",
                title="Network Issue",
                status="open",
                created_at="2024-01-15T10:30:00Z",
                resolved_at=None,
            )],
            recent_invoices=[InvoiceSummary(
                invoice_id="INV-789",
                amount=150.00,
                status="sent",
                created_at="2024-01-10T14:35:00Z",
            )],
        )
        mock_ctx = _make_mock_ctx(get_customer_context=context)

        result = await get_customer_context("john@example.com", ctx=mock_ctx)

        assert result["customer_email"] == "john@example.com"
        assert "summary" in result
        assert result["summary"]["total_tickets"] == 1
        assert "recent_tickets" in result
        assert "recent_invoices" in result

    async def test_handles_service_error(self):
        """Should return error dict when service fails."""
        from agentic_service.service import ServiceUnavailableError
        svc = _make_mock_service()
        svc.get_customer_context = AsyncMock(
            side_effect=ServiceUnavailableError("Service not initialized")
        )
        ctx = MagicMock(spec=Context)
        ctx.request_context.lifespan_context = {"service": svc}

        result = await get_customer_context("test@example.com", ctx=ctx)

        assert result["status"] == "error"
        assert result["customer_email"] == "test@example.com"
