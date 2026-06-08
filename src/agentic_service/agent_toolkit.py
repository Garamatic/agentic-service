"""Agent toolkit — domain tools exposed to LLM agents.

The service layer owns the agent-facing interface. The agent module
consumes a toolkit from its service, becoming a thin orchestration adapter.

All tool definitions live in the ToolRegistry (src/agentic_service/tools/).
This module is now a thin wrapper that creates a LangGraphAdapter from the
registry and binds it to a service instance.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from .service import GaramaticService
from .tools import (
    LangGraphAdapter,
    customer_tools,  # noqa: F401
    email_tools,  # noqa: F401
    get_registry,
    invoice_tools,  # noqa: F401
    system_tools,  # noqa: F401
    ticket_tools,  # noqa: F401 — triggers auto-registration
)


class AgentToolkit:
    """Domain tools exposed to LLM agents, backed by a GaramaticService.

    This is the seam between the business-logic service and the agent
    orchestration layer. One adapter = LangGraph tools. Another could be
    MCP tools or OpenAI function definitions.
    """

    def __init__(self, service: GaramaticService) -> None:
        self._service = service
        self._adapter = LangGraphAdapter(get_registry())

    def get_tools(self) -> list[StructuredTool]:
        """Return all tools as LangGraph StructuredTool instances."""
        return self._adapter.tools(self._service)
