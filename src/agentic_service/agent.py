"""Intelligent Agent for Garamatic using LangGraph.

Provides an autonomous agent that can:
- Plan multi-step workflows
- Use tools with reasoning
- Maintain conversation memory
- Request human approval for sensitive actions
"""

import asyncio
import contextlib
import os
from typing import Annotated, Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from .service import (
    GaramaticService,
    _generate_request_id,
    create_service,
)

logger = structlog.get_logger()

# Global service (initialized by init_clients, used by tools)
service: GaramaticService | None = None

# Agent state
class AgentState(TypedDict):
    """State maintained across agent invocations."""
    messages: Annotated[list, add_messages]


# Define async tools for LangGraph
async def search_tickets_by_customer(customer_email: str, limit: int = 10) -> str:
    """Search for tickets by customer email address."""
    if service is None:
        return "Error: Service not available"

    try:
        tickets = await service.list_tickets(customer_email=customer_email, limit=limit)
        if not tickets:
            return f"No tickets found for {customer_email}"

        ticket_word = "ticket" if len(tickets) == 1 else "tickets"
        result = f"Found {len(tickets)} {ticket_word} for {customer_email}:\n\n"
        for t in tickets:
            result += f"- Ticket {t.id}: {t.title} [{t.status}]\n"
        return result
    except Exception as e:
        return f"Error searching tickets: {str(e)}"


async def get_ticket_details(ticket_id: str) -> str:
    """Get detailed information about a specific ticket."""
    if not ticket_id or not ticket_id.strip():
        return "Error: Ticket ID is required"
    if service is None:
        return "Error: Service not available"

    try:
        ticket = await service.get_ticket(ticket_id)
    except Exception as e:
        return f"Error getting ticket: {str(e)}"

    # Invoice lookup is best-effort; don't fail the whole tool if Odoo is down
    invoice = None
    with contextlib.suppress(Exception):
        invoice = await service.get_invoice_status(ticket_id)

    result = f"""Ticket {ticket_id}:
- Title: {ticket.title}
- Status: {ticket.status}
- Customer: {ticket.customer_name or 'Unknown'} ({ticket.customer_email or 'Unknown'})
- Created: {ticket.created_at or 'N/A'}
- Description: {ticket.description or 'No description'}
- Resolution Notes: {ticket.resolution_notes or 'Not resolved'}
"""
    if invoice and invoice.status != "not_found":
        amount = invoice.amount or 0
        result += (
            f"\nInvoice:\n- Status: {invoice.status}\n"
            f"- Amount: €{amount:.2f}\n"
        )

    return result


async def get_customer_summary(customer_email: str) -> str:
    """Get comprehensive customer context including tickets and invoices."""
    if service is None:
        return "Error: Service not available"

    try:
        context = await service.get_customer_context(customer_email)

        result = f"""Customer {customer_email} Summary:
- Total Tickets: {context.summary.total_tickets}
- Open Tickets: {context.summary.open_tickets}
- Lifetime Value: €{context.summary.lifetime_value:.2f}
- Outstanding Balance: €{context.summary.outstanding_balance:.2f}
"""
        if context.recent_tickets:
            result += "\nRecent Tickets:\n"
            for t in context.recent_tickets:
                result += f"- {t.title} [{t.status}]\n"

        return result
    except Exception as e:
        return f"Error getting customer context: {str(e)}"


async def resolve_ticket_with_invoice(
    ticket_id: str, resolution_notes: str, billable_amount: float | None = None
) -> str:
    """
    Resolve a ticket AND create an invoice in one action.
    Only use when explicitly requested or when work is clearly billable.
    """
    if not ticket_id or not ticket_id.strip():
        return "Error: Ticket ID is required"
    if service is None:
        return "Error: Service not available"
    if not resolution_notes or not resolution_notes.strip():
        return "Error: Resolution notes are required"
    if billable_amount is not None and billable_amount < 0:
        return "Error: Billable amount cannot be negative"

    try:
        result = await service.resolve_ticket(
            ticket_id=ticket_id,
            resolution_notes=resolution_notes,
            billable_amount=billable_amount,
        )
    except Exception as e:
        return f"❌ Error resolving ticket: {e}"

    if result.invoice and result.invoice.status == "queued":
        return (
            f"✅ Ticket {ticket_id} resolved and invoice "
            f"(€{result.invoice.estimated_amount:.2f}) queued."
        )
    if result.invoice and result.invoice.status == "unavailable":
        return (
            f"✅ Ticket {ticket_id} resolved. "
            f"Invoice service unavailable — invoice not queued."
        )
    return f"✅ Ticket {ticket_id} resolved."


async def request_human_approval(
    action: str, details: str, estimated_amount: float | None = None
) -> str:
    """
    Request human approval for sensitive actions like large invoices.
    Use when amount > €500 or when uncertain about action.
    """
    approval_id = _generate_request_id()

    if estimated_amount is not None and estimated_amount > 500:
        return f"""⚠️ APPROVAL REQUIRED

Action: {action}
Details: {details}
Amount: €{estimated_amount:.2f}

Approval ID: {approval_id}

This action requires human approval due to high value.
Please confirm: "approve {approval_id}" or "reject {approval_id}"
"""
    else:
        return f"Action '{action}' approved automatically (below threshold)."


# Wrap async functions as LangChain tools
search_tickets_tool = StructuredTool.from_function(
    coroutine=search_tickets_by_customer,
    name="search_tickets_by_customer",
    description="Search for tickets by customer email address."
)

get_ticket_details_tool = StructuredTool.from_function(
    coroutine=get_ticket_details,
    name="get_ticket_details",
    description="Get detailed information about a specific ticket."
)

get_customer_summary_tool = StructuredTool.from_function(
    coroutine=get_customer_summary,
    name="get_customer_summary",
    description="Get comprehensive customer context including tickets and invoices."
)

resolve_ticket_tool = StructuredTool.from_function(
    coroutine=resolve_ticket_with_invoice,
    name="resolve_ticket_with_invoice",
    description="Resolve a ticket AND create an invoice in one action."
)

request_approval_tool = StructuredTool.from_function(
    coroutine=request_human_approval,
    name="request_human_approval",
    description="Request human approval for sensitive actions."
)


# Create tool node with wrapped tools
tools = [
    search_tickets_tool,
    get_ticket_details_tool,
    get_customer_summary_tool,
    resolve_ticket_tool,
    request_approval_tool,
]

tool_node = ToolNode(tools)

# Create the agent
system_prompt = """You are Garamatic AI, an intelligent assistant for customer support and billing.

Your capabilities:
1. Look up tickets and customer information
2. Resolve tickets and create invoices when appropriate
3. Get customer context across all services
4. Request human approval for sensitive actions (amounts > €500)

Guidelines:
- Always verify customer identity before making changes
- Explain your reasoning when taking actions
- Use resolve_ticket_with_invoice only when explicitly requested or when work is clearly billable
- Request approval for invoices over €500
- Be helpful but cautious with customer data

When asked about a customer:
1. Get their summary first
2. Look at recent tickets
3. Check outstanding balance
4. Provide helpful context

When resolving tickets:
1. Verify the ticket exists and is open
2. Get ticket details
3. If billable work was done, use resolve_ticket_with_invoice
4. Confirm the action taken
"""


async def _get_checkpointer() -> BaseCheckpointSaver:
    """Get appropriate checkpoint saver based on configuration.

    Uses SQLite persistence if AGENT_DB_PATH is set (production),
    otherwise falls back to in-memory storage (development).

    Note: This is async because AsyncSqliteSaver requires async context to initialize.
    The caller is responsible for closing the checkpointer when done.
    """
    import aiosqlite

    db_path = os.getenv("AGENT_DB_PATH", "").strip()

    if db_path:
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info("agent.using_persistent_storage", db_path=db_path)
        conn = await aiosqlite.connect(db_path)
        return AsyncSqliteSaver(conn)
    else:
        logger.info("agent.using_in_memory_storage")
        return MemorySaver()


async def create_agent(checkpointer: BaseCheckpointSaver):
    """Create and configure the LangGraph agent.

    Args:
        checkpointer: Checkpoint saver for persisting conversation state.
                     Must be provided (use _get_checkpointer() to create one).

    Note: This is async because checkpoint initialization may require async context.
    """

    def should_continue(state: AgentState) -> Literal["tools", END]:
        """Determine if we should continue to tools or end."""
        messages = state["messages"]
        last_message = messages[-1]

        # If last message has tool calls, go to tools
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            return "tools"

        # Otherwise end
        return END

    from langchain_openai import ChatOpenAI

    # Initialize model once (requires OPENAI_API_KEY env var)
    model = ChatOpenAI(model="gpt-4o-mini", temperature=0.1)
    model_with_tools = model.bind_tools(tools)

    def call_model(state: AgentState):
        """Call the LLM with current state."""
        messages = state["messages"]
        response = model_with_tools.invoke(messages)
        return {"messages": [response]}

    # Build graph
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)

    # Add edges
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            END: END
        }
    )
    workflow.add_edge("tools", "agent")

    return workflow.compile(checkpointer=checkpointer)


class GaramaticAgent:
    """High-level interface to the Garamatic AI Agent.

    Supports persistent conversation state via SQLite (production)
    or in-memory storage (development).
    """

    def __init__(
        self, thread_id: str | None = None, checkpointer: BaseCheckpointSaver | None = None
    ):
        """Initialize agent with optional thread_id for multi-user support.

        Args:
            thread_id: Unique identifier for conversation thread. If not provided,
                      uses a default thread (single-user mode).
            checkpointer: Optional custom checkpoint saver. Uses environment
                         config if not provided. Caller is responsible for cleanup.
        """
        self.thread_id = thread_id or "garamatic-session"
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self._checkpointer = checkpointer
        self._owns_checkpointer = checkpointer is None
        self._agent = None

    async def _get_agent(self):
        """Lazy initialization of the agent (async)."""
        if self._agent is None:
            # If no checkpointer provided, create one and store it for cleanup
            if self._checkpointer is None:
                self._checkpointer = await _get_checkpointer()
            self._agent = await create_agent(checkpointer=self._checkpointer)
        return self._agent

    async def process(self, message: str) -> str:
        """Process a user message and return the response.

        State is automatically persisted based on checkpointer configuration.
        """
        agent = await self._get_agent()

        # Only prepend system prompt on first interaction to avoid
        # infinite accumulation in checkpointed state.
        state = await agent.aget_state(self.config)
        existing = state.values.get("messages", []) if state.values else []
        if existing:
            messages = [HumanMessage(content=message)]
        else:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=message)
            ]

        result = await agent.ainvoke(
            {"messages": messages},
            config=self.config
        )

        # Get last AI message
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage):
                return msg.content or ""

        return "No response generated"

    async def get_state(self) -> dict:
        """Get current conversation state."""
        agent = await self._get_agent()
        state = await agent.aget_state(self.config)
        messages = state.values.get("messages", []) if state.values else []
        return {
            "thread_id": self.thread_id,
            "message_count": len(messages),
            "last_message": messages[-1].content if messages else None
        }

    async def close(self):
        """Cleanup resources including checkpointer connections."""
        if self._agent is not None:
            self._agent = None

        if self._owns_checkpointer and self._checkpointer is not None:
            with contextlib.suppress(Exception):
                if hasattr(self._checkpointer, "close"):
                    await self._checkpointer.close()
                elif getattr(self._checkpointer, "conn", None):
                    await self._checkpointer.conn.close()
            self._checkpointer = None

    async def run_conversation(self):
        """Run interactive conversation loop."""
        print("🤖 Garamatic AI Agent")
        print("Type 'quit' to exit\n")

        while True:
            try:
                user_input = input("You: ").strip()

                if user_input.lower() in ("quit", "exit", "bye"):
                    print("Goodbye! 👋")
                    break

                if not user_input:
                    continue

                response = await self.process(user_input)
                print(f"\nAI: {response}\n")

            except KeyboardInterrupt:
                print("\nGoodbye! 👋")
                break
            except Exception as e:
                logger.error("agent.conversation_error", error=str(e))
                print(f"Error: {str(e)}\n")


async def init_clients():
    """Initialize GaramaticService for agent tools."""
    global service
    service = await create_service()
    logger.info("agent.service.initialized")


async def close_clients():
    """Cleanup service."""
    global service
    if service:
        await service.close()
        service = None
    logger.info("agent.service.closed")


async def main_async():
    """Async main entry point."""
    await init_clients()

    agent = None
    try:
        agent = GaramaticAgent()
        await agent.run_conversation()
    finally:
        if agent:
            await agent.close()
        await close_clients()


def main():
    """Run the AI Agent."""
    # Check for OpenAI API key
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️  Warning: OPENAI_API_KEY not set. Agent will not function.")
        print("Set it with: export OPENAI_API_KEY='your-key-here'")
        return

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
