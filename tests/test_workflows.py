"""Tests for WorkflowEngine and email templates."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentic_service.service import EmailQueueResult
from agentic_service.workflows import (
    DEFAULT_WORKFLOWS,
    Workflow,
    WorkflowEngine,
    _field,
)


class TestFieldExtraction:
    """Test event field extraction with fallback keys."""

    def test_snake_case_key(self):
        event = {"ticket_id": "T-123", "amount": 100}
        assert _field(event, "ticket_id") == "T-123"

    def test_pascal_case_fallback(self):
        event = {"TicketId": "T-456", "Amount": 200}
        assert _field(event, "ticket_id", "TicketId") == "T-456"

    def test_default_when_missing(self):
        event = {}
        assert _field(event, "ticket_id", "TicketId", default="N/A") == "N/A"
        assert _field(event, "amount", "Amount", default=0) == 0


@pytest.mark.asyncio
class TestWorkflowEngine:
    """Test WorkflowEngine with mock service."""

    async def test_runs_registered_workflow(self):
        """Should execute workflow for a registered routing key."""
        mock_service = MagicMock()
        mock_service.send_email = AsyncMock(return_value=EmailQueueResult(
            status="queued",
            to_email="customer@example.com",
            subject="Payment Received - Invoice #INV-1",
            message="Queued",
        ))

        engine = WorkflowEngine(mock_service)
        event = {
            "ticket_id": "T-1",
            "invoice_id": "INV-1",
            "customer_email": "customer@example.com",
            "amount": 150.0,
        }

        result = await engine.handle("payment.received", event)

        assert result is not None
        assert result.status == "queued"
        mock_service.send_email.assert_awaited_once()
        call = mock_service.send_email.await_args
        assert call.kwargs["to_email"] == "customer@example.com"
        assert "Payment Received" in call.kwargs["subject"]
        assert "€150.00" in call.kwargs["body"]

    async def test_skips_when_no_recipient(self):
        """Should return None when event has no recipient email."""
        mock_service = MagicMock()
        mock_service.send_email = AsyncMock()

        engine = WorkflowEngine(mock_service)
        event = {"ticket_id": "T-1", "amount": 100}  # no customer_email

        result = await engine.handle("payment.received", event)

        assert result is None
        mock_service.send_email.assert_not_awaited()

    async def test_returns_none_for_unknown_routing_key(self):
        """Should return None when no workflow matches."""
        mock_service = MagicMock()
        engine = WorkflowEngine(mock_service)

        result = await engine.handle("unknown.event", {})

        assert result is None

    async def test_invoice_created_workflow(self):
        """Should send invoice notification email."""
        mock_service = MagicMock()
        mock_service.send_email = AsyncMock(return_value=EmailQueueResult(
            status="queued",
            to_email="customer@example.com",
            subject="New Invoice",
            message="Queued",
        ))

        engine = WorkflowEngine(mock_service)
        event = {
            "ticket_id": "T-2",
            "invoice_id": "INV-2",
            "customer_email": "customer@example.com",
            "amount": 250.0,
        }

        result = await engine.handle("invoice.created", event)

        assert result is not None
        call = mock_service.send_email.await_args
        assert "New Invoice" in call.kwargs["subject"]
        assert "billing@garamatic.com" == call.kwargs["from_email"]

    async def test_ticket_resolved_workflow(self):
        """Should send resolution confirmation email."""
        mock_service = MagicMock()
        mock_service.send_email = AsyncMock(return_value=EmailQueueResult(
            status="queued",
            to_email="customer@example.com",
            subject="Ticket Resolved",
            message="Queued",
        ))

        engine = WorkflowEngine(mock_service)
        event = {
            "ticket_id": "T-3",
            "customer_email": "customer@example.com",
            "customer_name": "Alice",
            "resolution_notes": "Fixed the VPN issue",
        }

        result = await engine.handle("ticket.resolved", event)

        assert result is not None
        call = mock_service.send_email.await_args
        assert "Resolved" in call.kwargs["subject"]
        assert "Fixed the VPN issue" in call.kwargs["body"]
        assert "support@garamatic.com" == call.kwargs["from_email"]

    async def test_ticket_escalated_sends_to_manager(self):
        """Should send escalation alert to manager email."""
        mock_service = MagicMock()
        mock_service.send_email = AsyncMock(return_value=EmailQueueResult(
            status="queued",
            to_email="manager@garamatic.com",
            subject="URGENT",
            message="Queued",
        ))

        engine = WorkflowEngine(mock_service)
        event = {
            "ticket_id": "T-4",
            "reason": "Customer complained",
        }

        result = await engine.handle("ticket.escalated", event)

        assert result is not None
        call = mock_service.send_email.await_args
        assert call.kwargs["to_email"] == "manager@garamatic.com"
        assert "URGENT" in call.kwargs["subject"]
        assert "system@garamatic.com" == call.kwargs["from_email"]

    async def test_ticket_escalated_enriches_with_ticket_data(self):
        """Should look up ticket details before rendering escalation email."""
        from agentic_service.service import Ticket
        ticket = Ticket(
            id="T-5",
            status="open",
            customer_email="john@example.com",
            customer_name="John",
            title="Network Issue",
            description="desc",
            created_at="2024-01-01",
            resolved_at=None,
            resolution_notes=None,
            billable_amount=None,
        )
        mock_service = MagicMock()
        mock_service.get_ticket = AsyncMock(return_value=ticket)
        mock_service.send_email = AsyncMock(return_value=EmailQueueResult(
            status="queued",
            to_email="manager@garamatic.com",
            subject="URGENT",
            message="Queued",
        ))

        engine = WorkflowEngine(mock_service)
        event = {"ticket_id": "T-5", "reason": "Escalated"}

        result = await engine.handle("ticket.escalated", event)

        assert result is not None
        mock_service.get_ticket.assert_awaited_once_with("T-5")
        call = mock_service.send_email.await_args
        assert "Network Issue" in call.kwargs["body"]
        assert "john@example.com" in call.kwargs["body"]

    async def test_all_registered_workflows_have_templates(self):
        """Every registered workflow must have a valid template."""
        for routing_key, workflow in DEFAULT_WORKFLOWS.items():
            assert workflow.template is not None
            assert workflow.template.from_email
            assert workflow.template.from_name
            assert callable(workflow.template.subject)
            assert callable(workflow.template.body_html)
            assert callable(workflow.get_recipient)

    async def test_template_rendering_does_not_crash(self):
        """All templates should render without error for minimal events."""
        minimal_events = {
            "payment.received": {"customer_email": "a@b.com"},
            "invoice.created": {"customer_email": "a@b.com"},
            "invoice.overdue": {"customer_email": "a@b.com"},
            "ticket.resolved": {"customer_email": "a@b.com"},
            "ticket.created": {"customer_email": "a@b.com"},
            "ticket.escalated": {"ticket_id": "T-1"},
        }
        for routing_key, event in minimal_events.items():
            workflow = DEFAULT_WORKFLOWS[routing_key]
            subject = workflow.template.subject(event)
            body = workflow.template.body_html(event)
            assert isinstance(subject, str)
            assert isinstance(body, str)
            assert len(subject) > 0
            assert len(body) > 0


@pytest.mark.asyncio
class TestInvoiceOverdueWorkflow:
    """Test invoice.overdue specific behavior."""

    async def test_uses_correct_day_word(self):
        """Should use 'day' for 1 day, 'days' for >1."""
        mock_service = MagicMock()
        mock_service.send_email = AsyncMock(return_value=EmailQueueResult(
            status="queued",
            to_email="customer@example.com",
            subject="Overdue",
            message="Queued",
        ))

        engine = WorkflowEngine(mock_service)

        # 1 day overdue
        result = await engine.handle("invoice.overdue", {
            "customer_email": "customer@example.com",
            "days_overdue": 1,
            "amount": 100.0,
        })
        call = mock_service.send_email.await_args
        assert "1 day overdue" in call.kwargs["body"]

        # 5 days overdue
        mock_service.send_email.reset_mock()
        result = await engine.handle("invoice.overdue", {
            "customer_email": "customer@example.com",
            "days_overdue": 5,
            "amount": 100.0,
        })
        call = mock_service.send_email.await_args
        assert "5 days overdue" in call.kwargs["body"]
