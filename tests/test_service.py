"""Boundary tests for GaramaticService.

Tests the service layer with fake ports — no HTTP mocking, no RabbitMQ.
"""

from unittest.mock import AsyncMock

import pytest

from agentic_service.service import (
    CustomerContext,
    EmailQueueResult,
    GaramaticService,
    HealthStatus,
    InvoiceQueueResult,
    InvoiceStatus,
    NotFoundError,
    ResolutionResult,
    Ticket,
    TicketSummary,
    ValidationError,
    validate_email,
)

# ------------------------------------------------------------------
# Fake ports
# ------------------------------------------------------------------


class FakeTicketClient:
    def __init__(self, tickets: dict | None = None):
        self.tickets = tickets or {}
        self.closed = False

    async def get_ticket(self, ticket_id: str):
        if ticket_id not in self.tickets:
            import httpx
            resp = AsyncMock()
            resp.status_code = 404
            raise httpx.HTTPStatusError("Not found", request=AsyncMock(), response=resp)
        return self.tickets[ticket_id]

    async def list_tickets(self, customer_id=None, status=None, limit=50, offset=0):
        return [
            t for t in self.tickets.values()
            if (customer_id is None or t.get("customerId") == customer_id)
            and (status is None or t.get("status") == status)
        ][:limit]

    async def create_ticket(self, title, description, customer_id, **kwargs):
        new_id = f"T-{len(self.tickets) + 1}"
        ticket = {
            "id": new_id,
            "title": title,
            "description": description,
            "customerId": customer_id,
            **kwargs,
            "status": "open",
        }
        self.tickets[new_id] = ticket
        return ticket

    async def resolve_ticket(self, ticket_id, resolution_notes=None, billable_amount=None):
        ticket = self.tickets.get(ticket_id, {})
        ticket["status"] = "resolved"
        ticket["resolutionNotes"] = resolution_notes
        ticket["billableAmount"] = billable_amount
        return ticket

    async def close(self):
        self.closed = True


class FakeOdooClient:
    def __init__(self, invoices: dict | None = None):
        self.invoices = invoices or {}
        self.closed = False

    async def get_invoice_status(self, ticket_id: str):
        return self.invoices.get(ticket_id)

    async def list_customer_invoices(self, customer_email: str):
        return [
            inv for inv in self.invoices.values()
            if inv.get("customer_email") == customer_email
        ]

    async def close(self):
        self.closed = True


class FakeMQ:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.closed = False

    async def publish(self, routing_key: str, message: dict):
        self.published.append((routing_key, message))

    async def close(self):
        self.closed = True


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def sample_ticket():
    return {
        "id": "T-123",
        "title": "Network Issue",
        "description": "Customer cannot connect to VPN",
        "status": "open",
        "customerId": "john@example.com",
        "customerName": "John Doe",
        "priority": "high",
        "createdAt": "2024-01-15T10:30:00Z",
        "resolvedAt": None,
        "resolutionNotes": None,
        "billableAmount": None,
    }


@pytest.fixture
def sample_invoice():
    return {
        "invoice_id": "INV-789",
        "ticket_id": "T-456",
        "status": "sent",
        "amount": 150.00,
        "created_at": "2024-01-10T14:35:00Z",
        "paid_at": None,
    }


@pytest.fixture
def service(sample_ticket, sample_invoice):
    ticket_client = FakeTicketClient(tickets={"T-123": sample_ticket, "T-456": sample_ticket})
    odoo_client = FakeOdooClient(invoices={"T-456": sample_invoice})
    mq = FakeMQ()
    return GaramaticService(ticket_client, odoo_client, mq)


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------


class TestEmailValidation:
    def test_valid_emails(self):
        for email in [
            "test@example.com",
            "user.name@domain.co.uk",
            "firstname+lastname@example.com",
            "user@subdomain.example.com",
        ]:
            validate_email(email)  # should not raise

    def test_invalid_emails(self):
        for email in [
            "invalid-email",
            "@example.com",
            "test@",
            "test@.com",
            "",
            "test..test@example.com",
            "test@example",
            "test@.example.com",
        ]:
            with pytest.raises(ValidationError):
                validate_email(email)


# ------------------------------------------------------------------
# Ticket operations
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetTicket:
    async def test_returns_ticket(self, service):
        result = await service.get_ticket("T-123")
        assert isinstance(result, Ticket)
        assert result.id == "T-123"
        assert result.status == "open"
        assert result.customer_email == "john@example.com"

    async def test_raises_not_found(self, service):
        with pytest.raises(NotFoundError):
            await service.get_ticket("NONEXISTENT")

    async def test_raises_validation_for_empty_id(self, service):
        with pytest.raises(ValidationError):
            await service.get_ticket("   ")


@pytest.mark.asyncio
class TestListTickets:
    async def test_returns_tickets(self, service):
        result = await service.list_tickets(customer_email="john@example.com")
        assert len(result) == 2
        assert all(isinstance(t, TicketSummary) for t in result)

    async def test_filters_by_status(self, service):
        result = await service.list_tickets(status="open")
        assert len(result) == 2


@pytest.mark.asyncio
class TestCreateTicket:
    async def test_creates_ticket(self, service):
        result = await service.create_ticket(
            title="New Issue",
            description="Something broke",
            customer_email="new@example.com",
            customer_name="New User",
            priority="high",
        )
        assert isinstance(result, Ticket)
        assert result.title == "New Issue"
        assert result.status == "open"

    async def test_raises_for_empty_title(self, service):
        with pytest.raises(ValidationError):
            await service.create_ticket(
                title="   ",
                description="desc",
                customer_email="test@example.com",
                customer_name="Test",
            )

    async def test_raises_for_invalid_email(self, service):
        with pytest.raises(ValidationError):
            await service.create_ticket(
                title="Issue",
                description="desc",
                customer_email="invalid-email",
                customer_name="Test",
            )


@pytest.mark.asyncio
class TestResolveTicket:
    async def test_resolves_without_invoice(self, service):
        result = await service.resolve_ticket(
            ticket_id="T-123",
            resolution_notes="Fixed the issue",
        )
        assert isinstance(result, ResolutionResult)
        assert result.status == "resolved"
        assert result.invoice is None

    async def test_resolves_with_invoice(self, service):
        result = await service.resolve_ticket(
            ticket_id="T-123",
            resolution_notes="Fixed the issue",
            billable_amount=200.00,
        )
        assert result.invoice is not None
        assert result.invoice.status == "queued"
        assert result.invoice.estimated_amount == 200.00

    async def test_raises_for_negative_amount(self, service):
        with pytest.raises(ValidationError):
            await service.resolve_ticket(
                ticket_id="T-123",
                resolution_notes="Fixed",
                billable_amount=-50.00,
            )

    async def test_raises_for_empty_notes(self, service):
        with pytest.raises(ValidationError):
            await service.resolve_ticket(
                ticket_id="T-123",
                resolution_notes="   ",
            )

    async def test_returns_unavailable_when_mq_down(self, sample_ticket):
        svc = GaramaticService(
            ticket_client=FakeTicketClient(tickets={"T-123": sample_ticket}),
            odoo_client=FakeOdooClient(),
            mq_publisher=None,
        )
        result = await svc.resolve_ticket(
            ticket_id="T-123",
            resolution_notes="Fixed",
            billable_amount=100.00,
        )
        assert result.invoice is not None
        assert result.invoice.status == "unavailable"


# ------------------------------------------------------------------
# Invoice operations
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetInvoiceStatus:
    async def test_returns_invoice(self, service, sample_invoice):
        result = await service.get_invoice_status("T-456")
        assert isinstance(result, InvoiceStatus)
        assert result.invoice_id == "INV-789"
        assert result.status == "sent"
        assert result.amount == 150.00

    async def test_returns_not_found_for_missing(self, service):
        result = await service.get_invoice_status("T-123")
        assert result.status == "not_found"


@pytest.mark.asyncio
class TestQueueInvoice:
    async def test_queues_invoice(self, service, sample_ticket):
        result = await service.queue_invoice(
            ticket_id="T-123",
            amount=250.00,
            description="Consulting",
        )
        assert isinstance(result, InvoiceQueueResult)
        assert result.status == "queued"
        assert result.estimated_amount == 250.00

    async def test_raises_for_non_positive_amount(self, service):
        with pytest.raises(ValidationError):
            await service.queue_invoice(ticket_id="T-123", amount=0)


# ------------------------------------------------------------------
# Email operations
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestSendEmail:
    async def test_queues_email(self, service):
        result = await service.send_email(
            to_email="customer@example.com",
            subject="Test",
            body="<p>Hello</p>",
        )
        assert isinstance(result, EmailQueueResult)
        assert result.status == "queued"
        assert result.to_email == "customer@example.com"

    async def test_raises_for_invalid_email(self, service):
        with pytest.raises(ValidationError):
            await service.send_email(
                to_email="invalid-email",
                subject="Subject",
                body="Body",
            )

    async def test_returns_failed_when_mq_down(self):
        svc = GaramaticService(
            ticket_client=FakeTicketClient(),
            odoo_client=FakeOdooClient(),
            mq_publisher=None,
        )
        result = await svc.send_email(
            to_email="test@example.com",
            subject="Subject",
            body="Body",
        )
        assert result.status == "failed"


# ------------------------------------------------------------------
# Customer context
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetCustomerContext:
    async def test_returns_context(self, service):
        result = await service.get_customer_context("john@example.com")
        assert isinstance(result, CustomerContext)
        assert result.customer_email == "john@example.com"
        assert result.summary.total_tickets >= 0
        assert "recent_tickets" in vars(result)
        assert "recent_invoices" in vars(result)

    async def test_raises_for_invalid_email(self, service):
        with pytest.raises(ValidationError):
            await service.get_customer_context("invalid-email")


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy_when_all_up(self, service):
        result = await service.health_check()
        assert isinstance(result, HealthStatus)
        assert result.status == "healthy"
        assert result.services["ticket_api"] == "connected"
        assert result.services["rabbitmq"] == "connected"

    async def test_degraded_when_mq_down(self, sample_ticket):
        svc = GaramaticService(
            ticket_client=FakeTicketClient(tickets={"T-123": sample_ticket}),
            odoo_client=FakeOdooClient(),
            mq_publisher=None,
        )
        result = await svc.health_check()
        assert result.status == "degraded"
        assert result.services["rabbitmq"] == "not_initialized"


# ------------------------------------------------------------------
# CustomerSession
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestCustomerSession:
    async def test_list_tickets(self, service):
        session = service.for_customer("john@example.com")
        result = await session.list_tickets()
        assert len(result) == 2
        assert all(isinstance(t, TicketSummary) for t in result)

    async def test_get_context(self, service):
        session = service.for_customer("john@example.com")
        result = await session.get_context()
        assert isinstance(result, CustomerContext)
        assert result.customer_email == "john@example.com"

    async def test_send_email(self, service):
        session = service.for_customer("john@example.com")
        result = await session.send_email(subject="Hello", body="<p>Hi</p>")
        assert result.status == "queued"
        assert result.to_email == "john@example.com"


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestLifecycle:
    async def test_closes_all_clients(self, service):
        await service.close()
        assert service._ticket.closed
        assert service._odoo.closed
        assert service._mq.closed

    async def test_context_manager(self, sample_ticket, sample_invoice):
        ticket_client = FakeTicketClient(tickets={"T-123": sample_ticket})
        odoo_client = FakeOdooClient(invoices={"T-456": sample_invoice})
        mq = FakeMQ()
        async with GaramaticService(ticket_client, odoo_client, mq) as svc:
            result = await svc.get_ticket("T-123")
            assert result.id == "T-123"
        assert ticket_client.closed
        assert odoo_client.closed
        assert mq.closed
