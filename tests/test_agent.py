"""Tests for the LangGraph agent.

Tests agent initialization, tool creation, and conversation flow with
mocked LLM and checkpointer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver

from agentic_service.agent import AgentState, GaramaticAgent, create_agent, create_tools
from agentic_service.agent_toolkit import AgentToolkit
from agentic_service.service import GaramaticService

# ------------------------------------------------------------------
# Fake service for testing
# ------------------------------------------------------------------


class FakeTicketClient:
    def __init__(self, tickets=None):
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
            t
            for t in self.tickets.values()
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
    def __init__(self, invoices=None):
        self.invoices = invoices or {}
        self.closed = False

    async def get_invoice_status(self, ticket_id: str):
        return self.invoices.get(ticket_id)

    async def list_customer_invoices(self, customer_email: str):
        return [
            inv for inv in self.invoices.values() if inv.get("customer_email") == customer_email
        ]

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
    """Create a GaramaticService with fake ports for testing."""
    ticket = {
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
    ticket_client = FakeTicketClient(tickets={"T-123": ticket})
    odoo_client = FakeOdooClient()
    mq = FakeMQ()
    return GaramaticService(ticket_client, odoo_client, mq)


# ------------------------------------------------------------------
# create_tools
# ------------------------------------------------------------------


class TestCreateTools:
    def test_returns_five_tools(self, fake_service):
        tools = create_tools(fake_service)
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert names == {
            "search_tickets_by_customer",
            "get_ticket_details",
            "get_customer_summary",
            "resolve_ticket_with_invoice",
            "request_human_approval",
        }
        assert all(isinstance(t, StructuredTool) for t in tools)

    @pytest.mark.asyncio
    async def test_search_tickets_by_customer(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "search_tickets_by_customer")
        result = await tool.ainvoke({"customer_email": "john@example.com", "limit": 10})
        assert result["status"] == "success"
        assert len(result["tickets"]) == 1
        assert result["tickets"][0]["id"] == "T-123"
        assert result["tickets"][0]["title"] == "Network Issue"

    @pytest.mark.asyncio
    async def test_get_ticket_details(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "get_ticket_details")
        result = await tool.ainvoke({"ticket_id": "T-123"})
        assert result["status"] == "success"
        assert result["ticket"]["title"] == "Network Issue"
        assert result["ticket"]["status"] == "open"

    @pytest.mark.asyncio
    async def test_get_ticket_details_not_found(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "get_ticket_details")
        result = await tool.ainvoke({"ticket_id": "T-999"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_get_customer_summary(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "get_customer_summary")
        result = await tool.ainvoke({"customer_email": "john@example.com"})
        assert result["status"] == "success"
        assert result["customer_email"] == "john@example.com"

    @pytest.mark.asyncio
    async def test_resolve_ticket_with_invoice(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "resolve_ticket_with_invoice")
        result = await tool.ainvoke(
            {
                "ticket_id": "T-123",
                "resolution_notes": "Fixed the VPN",
                "billable_amount": 200.0,
            }
        )
        assert result["status"] == "success"
        assert result["resolution"] == "resolved"

    @pytest.mark.asyncio
    async def test_request_human_approval_high_amount(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "request_human_approval")
        result = await tool.ainvoke(
            {
                "action": "create invoice",
                "details": "Large invoice",
                "estimated_amount": 600.0,
            }
        )
        assert result["status"] == "approval_required"
        assert "approval_id" in result

    @pytest.mark.asyncio
    async def test_request_human_approval_low_amount(self, fake_service):
        tools = create_tools(fake_service)
        tool = next(t for t in tools if t.name == "request_human_approval")
        result = await tool.ainvoke(
            {
                "action": "create invoice",
                "details": "Small invoice",
                "estimated_amount": 100.0,
            }
        )
        assert result["status"] == "approved"


# ------------------------------------------------------------------
# AgentToolkit
# ------------------------------------------------------------------


class TestAgentToolkit:
    def test_returns_five_tools(self, fake_service):
        toolkit = AgentToolkit(fake_service)
        tools = toolkit.get_tools()
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert names == {
            "search_tickets_by_customer",
            "get_ticket_details",
            "get_customer_summary",
            "resolve_ticket_with_invoice",
            "request_human_approval",
        }

    @pytest.mark.asyncio
    async def test_toolkit_tools_match_create_tools(self, fake_service):
        """AgentToolkit.get_tools() should produce the same tools as create_tools()."""
        from_agent = create_tools(fake_service)
        from_toolkit = AgentToolkit(fake_service).get_tools()
        assert len(from_agent) == len(from_toolkit)
        assert {t.name for t in from_agent} == {t.name for t in from_toolkit}


# ------------------------------------------------------------------
# GaramaticAgent
# ------------------------------------------------------------------


@pytest.fixture
def mock_model():
    """Mock ChatOpenAI that returns deterministic responses."""
    model = MagicMock()
    model.bind_tools = MagicMock(return_value=model)
    return model


@pytest.mark.asyncio
class TestGaramaticAgent:
    async def test_process_returns_response(self, fake_service, mock_model):
        """Should process a message and return the AI response."""
        with patch("langchain_openai.ChatOpenAI", return_value=mock_model):
            # First invocation: model responds with a tool call
            # Second invocation: model responds with final text
            mock_model.invoke = MagicMock(
                side_effect=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "name": "search_tickets_by_customer",
                                "args": {"customer_email": "john@example.com", "limit": 10},
                            }
                        ],
                    ),
                    AIMessage(content="Found 1 ticket for john@example.com."),
                ]
            )

            checkpointer = MemorySaver()
            agent = GaramaticAgent(
                service=fake_service,
                thread_id="test-thread",
                checkpointer=checkpointer,
            )

            try:
                response = await agent.process("How many tickets?")
                assert response == "Found 1 ticket for john@example.com."
            finally:
                await agent.close()

    async def test_process_without_tools(self, fake_service, mock_model):
        """Should handle a simple response without tool calls."""
        with patch("langchain_openai.ChatOpenAI", return_value=mock_model):
            mock_model.invoke = MagicMock(return_value=AIMessage(content="Hello!"))

            checkpointer = MemorySaver()
            agent = GaramaticAgent(
                service=fake_service,
                thread_id="test-thread",
                checkpointer=checkpointer,
            )

            try:
                response = await agent.process("Hello")
                assert response == "Hello!"
            finally:
                await agent.close()

    async def test_get_state(self, fake_service, mock_model):
        """Should return current conversation state."""
        with patch("langchain_openai.ChatOpenAI", return_value=mock_model):
            mock_model.invoke = MagicMock(return_value=AIMessage(content="Hi!"))

            checkpointer = MemorySaver()
            agent = GaramaticAgent(
                service=fake_service,
                thread_id="test-thread",
                checkpointer=checkpointer,
            )

            try:
                await agent.process("Hello")
                state = await agent.get_state()
                assert state["thread_id"] == "test-thread"
                assert state["message_count"] > 0
            finally:
                await agent.close()

    async def test_close_cleans_up(self, fake_service, mock_model):
        """Should close without errors."""
        with patch("langchain_openai.ChatOpenAI", return_value=mock_model):
            mock_model.invoke = MagicMock(return_value=AIMessage(content="Hi!"))

            checkpointer = MemorySaver()
            agent = GaramaticAgent(
                service=fake_service,
                thread_id="test-thread",
                checkpointer=checkpointer,
            )

            await agent.process("Hello")
            await agent.close()
            # Should not raise

    async def test_uses_injected_service(self, fake_service):
        """Agent should use the injected service, not create its own."""
        checkpointer = MemorySaver()
        agent = GaramaticAgent(
            service=fake_service,
            thread_id="test-thread",
            checkpointer=checkpointer,
        )
        assert agent._service is fake_service
        await agent.close()


# ------------------------------------------------------------------
# create_agent (graph factory)
# ------------------------------------------------------------------


class TestCreateAgent:
    def test_compiles_graph(self, fake_service, mock_model):
        """Should create a compilable LangGraph."""
        with patch("langchain_openai.ChatOpenAI", return_value=mock_model):
            checkpointer = MemorySaver()
            graph = create_agent(fake_service, checkpointer)
            assert graph is not None


# ------------------------------------------------------------------
# AgentState
# ------------------------------------------------------------------


class TestAgentState:
    def test_typed_dict(self):
        """AgentState should be a valid TypedDict."""
        state = AgentState(messages=[HumanMessage(content="Hello")])
        assert "messages" in state
