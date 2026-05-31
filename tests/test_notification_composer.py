"""Boundary tests for NotificationComposer.

Tests the notification boundary with in-memory adapters.
No HTTP mocking, no RabbitMQ, no GaramaticService construction.
"""

from __future__ import annotations

import pytest

from agentic_service.notification_adapters import (
    InMemoryEmailSender,
    InMemoryTicketLookup,
)
from agentic_service.notifications import (
    EmailMessage,
    EmailSendResult,
    EventView,
    NotificationComposer,
    NotificationRegistry,
    NotificationTemplate,
    SenderIdentity,
    StaticSenderDirectory,
    TicketSnapshot,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


class BrokenEmailSender:
    """Always fails for testing degradation paths."""

    async def send_email(self, message: EmailMessage) -> EmailSendResult:
        raise RuntimeError("MQ is down")


class FailingEmailSender:
    """Returns failed status without raising."""

    async def send_email(self, message: EmailMessage) -> EmailSendResult:
        return EmailSendResult(
            status="failed",
            to_email=message.to_email,
            subject=message.subject,
            message="forced failure",
        )


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def email_sender():
    return InMemoryEmailSender()


@pytest.fixture
def ticket_lookup():
    return InMemoryTicketLookup(
        {
            "T-5": TicketSnapshot(
                id="T-5",
                customer_email="john@example.com",
                customer_name="John",
                title="Network Issue",
            ),
        }
    )


@pytest.fixture
def composer(email_sender):
    return NotificationComposer(email_sender=email_sender)


@pytest.fixture
def composer_with_lookup(email_sender, ticket_lookup):
    return NotificationComposer(
        email_sender=email_sender,
        ticket_lookup=ticket_lookup,
    )


# ------------------------------------------------------------------
# EventView
# ------------------------------------------------------------------


class TestEventView:
    def test_snake_case_key(self):
        view = EventView({"ticket_id": "T-123"})
        assert view.get("ticket_id") == "T-123"

    def test_pascal_case_fallback(self):
        view = EventView({"TicketId": "T-456"})
        assert view.get("ticket_id") == "T-456"

    def test_camel_case_fallback(self):
        view = EventView({"ticketId": "T-789"})
        assert view.get("ticket_id") == "T-789"

    def test_odoo_invoice_id_alias(self):
        view = EventView({"OdooInvoiceId": "INV-1"})
        assert view.get("invoice_id") == "INV-1"

    def test_default_when_missing(self):
        view = EventView({})
        assert view.get("ticket_id", "N/A") == "N/A"
        assert view.money("amount", 0.0) == 0.0

    def test_str_cast(self):
        view = EventView({"amount": 150.0})
        assert view.text("amount") == "150.0"

    def test_money_parsing(self):
        view = EventView({"amount": "250.5"})
        assert view.money("amount") == 250.5

    def test_merged(self):
        view = EventView({"ticket_id": "T-1"})
        enriched = view.merged({"title": "Network Issue"})
        assert enriched.get("ticket_id") == "T-1"
        assert enriched.get("title") == "Network Issue"


# ------------------------------------------------------------------
# NotificationComposer — common events
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestPaymentReceived:
    async def test_queues_email(self, composer, email_sender):
        result = await composer.notify(
            "payment.received",
            {
                "ticket_id": "T-1",
                "invoice_id": "INV-1",
                "customer_email": "customer@example.com",
                "amount": 150.0,
            },
        )
        assert result.status == "queued"
        assert result.reason is None
        assert result.sent is True
        assert len(email_sender.sent) == 1
        assert email_sender.sent[0].to_email == "customer@example.com"
        assert "Payment Received" in email_sender.sent[0].subject
        assert "€150.00" in email_sender.sent[0].body_html
        assert email_sender.sent[0].sender.email == "billing@garamatic.com"

    async def test_skips_when_no_recipient(self, composer, email_sender):
        result = await composer.notify(
            "payment.received",
            {
                "ticket_id": "T-1",
                "amount": 100.0,
            },
        )
        assert result.status == "skipped"
        assert result.reason == "missing_recipient"
        assert email_sender.sent == []


@pytest.mark.asyncio
class TestInvoiceCreated:
    async def test_queues_email(self, composer, email_sender):
        result = await composer.notify(
            "invoice.created",
            {
                "ticket_id": "T-2",
                "invoice_id": "INV-2",
                "customer_email": "customer@example.com",
                "amount": 250.0,
            },
        )
        assert result.status == "queued"
        assert "New Invoice" in email_sender.sent[0].subject
        assert "€250.00" in email_sender.sent[0].body_html
        assert email_sender.sent[0].sender.email == "billing@garamatic.com"


@pytest.mark.asyncio
class TestInvoiceOverdue:
    async def test_queues_email(self, composer, email_sender):
        result = await composer.notify(
            "invoice.overdue",
            {
                "invoice_id": "INV-3",
                "customer_email": "customer@example.com",
                "amount": 100.0,
                "days_overdue": 5,
            },
        )
        assert result.status == "queued"
        assert "5 days overdue" in email_sender.sent[0].body_html

    async def test_uses_day_singular(self, composer, email_sender):
        result = await composer.notify(
            "invoice.overdue",
            {
                "invoice_id": "INV-3",
                "customer_email": "customer@example.com",
                "amount": 100.0,
                "days_overdue": 1,
            },
        )
        assert result.status == "queued"
        assert "1 day overdue" in email_sender.sent[0].body_html
        assert "days overdue" not in email_sender.sent[0].body_html


@pytest.mark.asyncio
class TestTicketResolved:
    async def test_queues_email(self, composer, email_sender):
        result = await composer.notify(
            "ticket.resolved",
            {
                "ticket_id": "T-3",
                "customer_email": "customer@example.com",
                "customer_name": "Alice",
                "resolution_notes": "Fixed the VPN issue",
            },
        )
        assert result.status == "queued"
        assert "Resolved" in email_sender.sent[0].subject
        assert "Fixed the VPN issue" in email_sender.sent[0].body_html
        assert email_sender.sent[0].sender.email == "support@garamatic.com"


@pytest.mark.asyncio
class TestTicketCreated:
    async def test_queues_email(self, composer, email_sender):
        result = await composer.notify(
            "ticket.created",
            {
                "ticket_id": "T-4",
                "customer_email": "customer@example.com",
                "customer_name": "Bob",
                "title": "Printer not working",
            },
        )
        assert result.status == "queued"
        assert "Ticket #T-4 Created" in email_sender.sent[0].subject
        assert email_sender.sent[0].sender.email == "support@garamatic.com"


# ------------------------------------------------------------------
# NotificationComposer — escalation (enrichment)
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestTicketEscalated:
    async def test_enriches_and_sends_to_manager(
        self, composer_with_lookup, email_sender, ticket_lookup
    ):
        result = await composer_with_lookup.notify(
            "ticket.escalated",
            {
                "ticket_id": "T-5",
                "reason": "VIP complaint",
            },
        )
        assert result.status == "queued"
        assert email_sender.sent[0].to_email == "manager@garamatic.com"
        assert "Network Issue" in email_sender.sent[0].body_html
        assert "john@example.com" in email_sender.sent[0].body_html
        assert email_sender.sent[0].sender.email == "system@garamatic.com"
        # Verify lookup was called
        assert "T-5" in ticket_lookup.calls

    async def test_degrades_when_lookup_fails(self, composer_with_lookup, email_sender):
        # T-5 is in the fixture, but T-99 is not
        result = await composer_with_lookup.notify(
            "ticket.escalated",
            {
                "ticket_id": "T-99",
                "reason": "Unknown ticket",
            },
        )
        assert result.status == "queued"
        # Should still send with original event data
        assert email_sender.sent[0].to_email == "manager@garamatic.com"
        assert "Unknown" in email_sender.sent[0].body_html

    async def test_degrades_when_no_lookup_configured(self, composer, email_sender):
        # composer has no ticket_lookup
        result = await composer.notify(
            "ticket.escalated",
            {
                "ticket_id": "T-5",
                "reason": "VIP complaint",
            },
        )
        assert result.status == "queued"
        assert email_sender.sent[0].to_email == "manager@garamatic.com"


# ------------------------------------------------------------------
# NotificationComposer — edge cases
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestEdgeCases:
    async def test_unknown_routing_key_returns_unhandled(self, composer, email_sender):
        result = await composer.notify(
            "unknown.event",
            {
                "customer_email": "customer@example.com",
            },
        )
        assert result.status == "unhandled"
        assert result.reason == "unknown_event"
        assert email_sender.sent == []

    async def test_mq_failure_returns_failed(self, email_sender):
        composer = NotificationComposer(email_sender=BrokenEmailSender())
        result = await composer.notify(
            "payment.received",
            {
                "ticket_id": "T-1",
                "invoice_id": "INV-1",
                "customer_email": "customer@example.com",
                "amount": 150.0,
            },
        )
        assert result.status == "failed"
        assert result.reason == "delivery_unavailable"
        assert result.email is not None  # Email is captured for observability

    async def test_send_result_failure_returns_failed(self, email_sender):
        composer = NotificationComposer(email_sender=FailingEmailSender())
        result = await composer.notify(
            "payment.received",
            {
                "ticket_id": "T-1",
                "invoice_id": "INV-1",
                "customer_email": "customer@example.com",
                "amount": 150.0,
            },
        )
        assert result.status == "failed"
        assert result.reason == "delivery_unavailable"
        assert result.send_result is not None
        assert result.send_result.status == "failed"

    async def test_pascal_case_event_still_works(self, composer, email_sender):
        result = await composer.notify(
            "payment.received",
            {
                "TicketId": "T-1",
                "OdooInvoiceId": "INV-1",
                "CustomerEmail": "customer@example.com",
                "Amount": 150.0,
            },
        )
        assert result.status == "queued"
        assert email_sender.sent[0].to_email == "customer@example.com"


# ------------------------------------------------------------------
# StaticSenderDirectory
# ------------------------------------------------------------------


class TestStaticSenderDirectory:
    def test_billing_for_payment(self):
        directory = StaticSenderDirectory()
        sender = directory.for_routing_key("payment.received")
        assert sender.email == "billing@garamatic.com"

    def test_support_for_ticket(self):
        directory = StaticSenderDirectory()
        sender = directory.for_routing_key("ticket.resolved")
        assert sender.email == "support@garamatic.com"

    def test_system_for_escalation(self):
        directory = StaticSenderDirectory()
        sender = directory.for_routing_key("ticket.escalated")
        assert sender.email == "system@garamatic.com"

    def test_custom_identities(self):
        directory = StaticSenderDirectory(
            {
                "billing": SenderIdentity("bill@custom.com", "Billing"),
            }
        )
        sender = directory.for_routing_key("invoice.created")
        assert sender.email == "bill@custom.com"


# ------------------------------------------------------------------
# NotificationRegistry
# ------------------------------------------------------------------


class TestNotificationRegistry:
    def test_defaults_contains_all_routing_keys(self):
        registry = NotificationRegistry.defaults()
        for key in [
            "payment.received",
            "invoice.created",
            "invoice.overdue",
            "ticket.resolved",
            "ticket.created",
            "ticket.escalated",
        ]:
            assert registry.get(key) is not None

    def test_get_returns_none_for_unknown(self):
        registry = NotificationRegistry.defaults()
        assert registry.get("foo.bar") is None

    def test_custom_registry(self):
        registry = NotificationRegistry(
            [
                NotificationTemplate(
                    routing_key="custom.event",
                    recipient=lambda e: e.text("email"),
                    render=lambda e: ("Subject", "Body"),
                ),
            ]
        )
        template = registry.get("custom.event")
        assert template is not None
        assert template.routing_key == "custom.event"


# ------------------------------------------------------------------
# Integration-style: full flow through composer
# ------------------------------------------------------------------


@pytest.mark.asyncio
class TestFullFlow:
    async def test_all_default_workflows_are_queued(self):
        sender = InMemoryEmailSender()
        composer = NotificationComposer(email_sender=sender)

        events = {
            "payment.received": {"customer_email": "a@b.com", "amount": 100},
            "invoice.created": {"customer_email": "a@b.com", "amount": 100},
            "invoice.overdue": {"customer_email": "a@b.com", "amount": 100, "days_overdue": 1},
            "ticket.resolved": {"customer_email": "a@b.com", "resolution_notes": "Fixed"},
            "ticket.created": {"customer_email": "a@b.com", "title": "Test"},
            "ticket.escalated": {"ticket_id": "T-1", "reason": "Test"},
        }

        for routing_key, event in events.items():
            result = await composer.notify(routing_key, event)
            assert result.status == "queued"

        sent_events = {msg.routing_key: msg for msg in sender.sent}
        assert set(sent_events.keys()) == set(events.keys())
