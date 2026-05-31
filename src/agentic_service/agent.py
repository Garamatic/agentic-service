"""Intelligent Agent for Garamatic using LangGraph.

Provides an autonomous agent that can:
- Plan multi-step workflows
- Use tools with reasoning
- Maintain conversation memory
- Request human approval for sensitive actions

Usage:
    async with create_service() as service:
        agent = GaramaticAgent(service=service)
        response = await agent.process("How many tickets does alice@example.com have?")
"""

import asyncio
import contextlib
import os
from typing import Annotated, Any, Literal

import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from .agent_toolkit import AgentToolkit
from .config import get_settings
from .service import GaramaticService, create_service

logger = structlog.get_logger()


# ------------------------------------------------------------------
# Agent state
# ------------------------------------------------------------------


class AgentState(TypedDict):
    """State maintained across agent invocations."""

    messages: Annotated[list, add_messages]


# ------------------------------------------------------------------
# Tool factory — now delegates to AgentToolkit
# ------------------------------------------------------------------


def create_tools(service: GaramaticService) -> list:
    """Create LangGraph tools bound to a service instance."""
    return AgentToolkit(service).get_tools()


# ------------------------------------------------------------------
# System prompt
# ------------------------------------------------------------------

_system_prompt = """You are Garamatic AI, an intelligent assistant for customer support and billing.

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


# ------------------------------------------------------------------
# Checkpointer
# ------------------------------------------------------------------


async def _get_checkpointer() -> BaseCheckpointSaver:
    """Get appropriate checkpoint saver based on configuration.

    Uses SQLite persistence if AGENT_DB_PATH is set (production),
    otherwise falls back to in-memory storage (development).
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


# ------------------------------------------------------------------
# Agent graph factory
# ------------------------------------------------------------------


def create_agent(
    service: GaramaticService,
    checkpointer: BaseCheckpointSaver,
) -> CompiledStateGraph:
    """Create and configure the LangGraph agent.

    Args:
        service: The business-logic service that tools call.
        checkpointer: Checkpoint saver for persisting conversation state.
    """
    tools = create_tools(service)
    tool_node = ToolNode(tools)

    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        """Determine if we should continue to tools or end."""
        messages = state["messages"]
        last_message = messages[-1]

        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"
        return "__end__"

    from langchain_openai import ChatOpenAI

    config = get_settings()
    model = ChatOpenAI(model=config.openai_model, temperature=0.1)
    model_with_tools = model.bind_tools(tools)

    def call_model(state: AgentState) -> dict[str, Any]:
        """Call the LLM with current state."""
        messages = state["messages"]
        response = model_with_tools.invoke(messages)
        return {"messages": [response]}

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", END: END},
    )
    workflow.add_edge("tools", "agent")

    return workflow.compile(checkpointer=checkpointer)


# ------------------------------------------------------------------
# High-level agent class
# ------------------------------------------------------------------


class GaramaticAgent:
    """High-level interface to the Garamatic AI Agent.

    Supports persistent conversation state via SQLite (production)
    or in-memory storage (development).
    """

    def __init__(
        self,
        service: GaramaticService,
        thread_id: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ):
        """Initialize agent with a service and optional thread_id.

        Args:
            service: The business-logic service that tools call.
            thread_id: Unique identifier for conversation thread.
            checkpointer: Optional custom checkpoint saver. Uses environment
                         config if not provided. Caller is responsible for cleanup.
        """
        self._service = service
        self.thread_id = thread_id or "garamatic-session"
        self.config = {"configurable": {"thread_id": self.thread_id}}
        self._checkpointer = checkpointer
        self._owns_checkpointer = checkpointer is None
        self._agent: CompiledStateGraph | None = None

    async def _get_agent(self):
        """Lazy initialization of the agent (async)."""
        if self._agent is None:
            if self._checkpointer is None:
                self._checkpointer = await _get_checkpointer()
            self._agent = create_agent(
                service=self._service,
                checkpointer=self._checkpointer,
            )
        return self._agent

    async def process(self, message: str) -> str:
        """Process a user message and return the response."""
        agent = await self._get_agent()

        state = await agent.aget_state(self.config)
        existing = state.values.get("messages", []) if state.values else []
        if existing:
            messages = [
                SystemMessage(content=_system_prompt),
                *existing,
                HumanMessage(content=message),
            ]
        else:
            messages = [
                SystemMessage(content=_system_prompt),
                HumanMessage(content=message),
            ]

        result = await agent.ainvoke(
            {"messages": messages},
            config=self.config,
        )

        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage):
                content = msg.content
                if isinstance(content, list):
                    return str(content)
                return content or ""

        return "No response generated"

    async def get_state(self) -> dict:
        """Get current conversation state."""
        agent = await self._get_agent()
        state = await agent.aget_state(self.config)
        messages = state.values.get("messages", []) if state.values else []
        return {
            "thread_id": self.thread_id,
            "message_count": len(messages),
            "last_message": messages[-1].content if messages else None,
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
            except EOFError:
                print("\nGoodbye! 👋")
                break
            except Exception as e:
                logger.error("agent.conversation_error", error=str(e))
                print(f"Error: {str(e)}\n")


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


async def main_async():
    """Async main entry point."""
    service = await create_service()
    agent = None
    try:
        agent = GaramaticAgent(service=service)
        await agent.run_conversation()
    finally:
        if agent:
            await agent.close()
        await service.close()


def main():
    """Run the AI Agent."""
    config = get_settings()
    api_key = config.openai_api_key.get_secret_value()
    if not api_key:
        print("⚠️  Warning: OPENAI_API_KEY not set. Agent will not function.")
        print("Set it with: export OPENAI_API_KEY='your-key-here'")
        return

    asyncio.run(main_async())


if __name__ == "__main__":
    main()
