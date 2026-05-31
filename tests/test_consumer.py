"""Tests for the RabbitMQ event consumer.

Tests message handling, retry logic, and graceful degradation with
mocked RabbitMQ messages and in-memory notification adapters.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aio_pika import IncomingMessage

from agentic_service.consumer import EventConsumer
from agentic_service.notification_adapters import InMemoryEmailSender
from agentic_service.notifications import NotificationComposer
from agentic_service.service import GaramaticService

# ------------------------------------------------------------------
# Fake service
# ------------------------------------------------------------------


class FakeTicketClient:
    def __init__(self, tickets=None):
        self.tickets = tickets or {}
        self.closed = False

    async def get_ticket(self, ticket_id: str):
        return self.tickets.get(ticket_id, {})

    async def list_tickets(self, customer_id=None, status=None, limit=50, offset=0):
        return [
            t
            for t in self.tickets.values()
            if (customer_id is None or t.get("customerId") == customer_id)
            and (status is None or t.get("status") == status)
        ][:limit]

    async def create_ticket(self, title, description, customer_id, **kwargs):
        return {}

    async def resolve_ticket(self, ticket_id, resolution_notes=None, billable_amount=None):
        return {}

    async def close(self):
        self.closed = True


class FakeOdooClient:
    def __init__(self):
        self.closed = False

    async def get_invoice_status(self, ticket_id: str):
        return None

    async def list_customer_invoices(self, customer_email: str):
        return []

    async def close(self):
        self.closed = True


class FakeMQ:
    def __init__(self):
        self.published = []
        self.closed = False

    async def publish(self, routing_key: str, message: dict):
        self.published.append((routing_key, message))

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_service():
    return GaramaticService(FakeTicketClient(), FakeOdooClient(), FakeMQ())


# ------------------------------------------------------------------
# Mock message builder
# ------------------------------------------------------------------


def _make_mock_message(
    body: dict,
    routing_key: str = "event.payment.received",
    headers: dict | None = None,
):
    """Create a mock aio_pika IncomingMessage."""
    msg = MagicMock(spec=IncomingMessage)
    msg.body = json.dumps(body).encode()
    msg.routing_key = routing_key
    msg.headers = headers or {}
    msg.content_type = "application/json"
    msg.delivery_mode = MagicMock()
    msg.correlation_id = None
    msg.message_id = None

    # Track ack/reject calls
    msg.ack = AsyncMock()
    msg.reject = AsyncMock()
    return msg


# ------------------------------------------------------------------
# EventConsumer
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestEventConsumer:
    async def test_handles_payment_received(self, fake_service):
        """Should process a payment received event and queue an email."""
        sender = InMemoryEmailSender()
        composer = NotificationComposer(email_sender=sender)
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer._composer = composer
        consumer._exchange = AsyncMock()

        msg = _make_mock_message(
            {
                "ticket_id": "T-1",
                "invoice_id": "INV-1",
                "customer_email": "customer@example.com",
                "amount": 150.0,
            }
        )

        await consumer.handle_message(msg)

        msg.ack.assert_awaited_once()
        assert len(sender.sent) == 1
        assert sender.sent[0].to_email == "customer@example.com"
        assert "Payment Received" in sender.sent[0].subject

    async def test_handles_invalid_json(self, fake_service):
        """Should ack and skip invalid JSON messages."""
        sender = InMemoryEmailSender()
        composer = NotificationComposer(email_sender=sender)
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer._composer = composer
        consumer._exchange = AsyncMock()

        msg = MagicMock(spec=IncomingMessage)
        msg.body = b"not json"
        msg.routing_key = "event.payment.received"
        msg.headers = {}
        msg.content_type = "application/json"
        msg.delivery_mode = MagicMock()
        msg.correlation_id = None
        msg.message_id = None
        msg.ack = AsyncMock()
        msg.reject = AsyncMock()

        await consumer.handle_message(msg)

        msg.ack.assert_awaited_once()
        assert len(sender.sent) == 0

    async def test_retries_on_failure(self, fake_service):
        """Should requeue message on processing failure."""
        sender = InMemoryEmailSender()
        composer = NotificationComposer(email_sender=sender)
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer._composer = composer
        consumer._exchange = AsyncMock()
        consumer._exchange.publish = AsyncMock()

        # Make composer fail
        composer.notify = AsyncMock(side_effect=RuntimeError("boom"))

        msg = _make_mock_message(
            {
                "customer_email": "customer@example.com",
                "amount": 100.0,
            }
        )

        await consumer.handle_message(msg)

        # Should requeue, not ack
        msg.ack.assert_awaited_once()
        consumer._exchange.publish.assert_awaited_once()

    async def test_dead_letters_after_max_retries(self, fake_service):
        """Should dead-letter message after max retries."""
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer._exchange = AsyncMock()
        consumer._exchange.publish = AsyncMock()

        # Make composer fail
        composer = MagicMock()
        composer.notify = AsyncMock(side_effect=RuntimeError("boom"))
        consumer._composer = composer

        msg = _make_mock_message(
            {
                "customer_email": "customer@example.com",
                "amount": 100.0,
            },
            headers={"x-retry-count": 3},
        )

        await consumer.handle_message(msg)

        msg.reject.assert_awaited_once_with(requeue=False)
        consumer._exchange.publish.assert_not_awaited()

    async def test_get_retry_count_from_header(self, fake_service):
        """Should extract retry count from message headers."""
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )

        msg = _make_mock_message({"data": "test"}, headers={"x-retry-count": 2})
        count = consumer._get_retry_count(msg)
        assert count == 2

    async def test_get_retry_count_defaults_to_zero(self, fake_service):
        """Should default retry count to 0 when no header."""
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )

        msg = _make_mock_message({"data": "test"})
        count = consumer._get_retry_count(msg)
        assert count == 0

    async def test_closes_service_when_owns_service(self, fake_service):
        """Should close service when owns_service=True."""
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=True,
        )
        consumer.connection = MagicMock()

        await consumer.close()

        assert fake_service._ticket.closed
        assert fake_service._odoo.closed
        assert fake_service._mq.closed

    async def test_does_not_close_service_when_not_owned(self, fake_service):
        """Should not close service when owns_service=False."""
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer.connection = MagicMock()

        await consumer.close()

        assert not fake_service._ticket.closed


# ------------------------------------------------------------------
# Full workflow tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestConsumerWorkflows:
    async def test_ticket_created_sends_confirmation(self, fake_service):
        """Should send confirmation email on ticket creation."""
        sender = InMemoryEmailSender()
        composer = NotificationComposer(email_sender=sender)
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer._composer = composer
        consumer._exchange = AsyncMock()

        msg = _make_mock_message(
            {
                "ticket_id": "T-5",
                "customer_email": "john@example.com",
                "customer_name": "John",
                "title": "Printer issue",
            },
            routing_key="event.ticket.created",
        )

        await consumer.handle_message(msg)

        msg.ack.assert_awaited_once()
        assert len(sender.sent) == 1
        assert sender.sent[0].to_email == "john@example.com"
        assert "Ticket #T-5 Created" in sender.sent[0].subject

    async def test_ticket_escalated_sends_to_manager(self, fake_service):
        """Should send escalation alert to manager."""
        sender = InMemoryEmailSender()
        composer = NotificationComposer(email_sender=sender)
        consumer = EventConsumer(
            service=fake_service,
            amqp_url="amqp://test",
            owns_service=False,
        )
        consumer._composer = composer
        consumer._exchange = AsyncMock()

        msg = _make_mock_message(
            {
                "ticket_id": "T-5",
                "reason": "VIP complaint",
            },
            routing_key="event.ticket.escalated",
        )

        await consumer.handle_message(msg)

        msg.ack.assert_awaited_once()
        assert len(sender.sent) == 1
        assert sender.sent[0].to_email == "manager@garamatic.com"
        assert "URGENT" in sender.sent[0].subject
