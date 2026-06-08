"""Tests for McpAdapter and LangGraphAdapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agentic_service.tools.adapters import LangGraphAdapter, McpAdapter
from agentic_service.tools.registry import ToolRegistry
from agentic_service.tools.types import ToolDefinition

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


class FakeSchema(BaseModel):
    """Fake schema for testing."""

    value: str


async def fake_handler(service: object, **kwargs: object) -> dict[str, Any]:
    return {"status": "success", "value": kwargs.get("value")}


async def fake_approval_handler(service: object, **kwargs: object) -> dict[str, Any]:
    return {"status": "success", "value": kwargs.get("value")}


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="fake_tool",
            schema=FakeSchema,
            handler=fake_handler,
            description="A fake tool.",
        )
    )
    reg.register(
        ToolDefinition(
            name="approval_tool",
            schema=FakeSchema,
            handler=fake_approval_handler,
            description="A tool that needs approval.",
            approval_threshold=1.0,
        )
    )
    return reg


# ------------------------------------------------------------------
# McpAdapter
# ------------------------------------------------------------------


def test_mcp_adapter_register_all(registry: ToolRegistry) -> None:
    """McpAdapter.register_all() calls mcp.tool() for each definition."""
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    adapter = McpAdapter(registry, mcp)

    def fake_get_service(ctx: Any) -> Any:
        return MagicMock()

    adapter.register_all(fake_get_service)
    assert mcp.tool.call_count == 2


# ------------------------------------------------------------------
# LangGraphAdapter
# ------------------------------------------------------------------


def test_langgraph_adapter_tools(registry: ToolRegistry) -> None:
    """LangGraphAdapter.tools() returns a list of StructuredTool instances."""
    service = MagicMock()
    adapter = LangGraphAdapter(registry)
    tools = adapter.tools(service)

    assert len(tools) == 2
    assert all(isinstance(t, StructuredTool) for t in tools)
    names = {t.name for t in tools}
    assert names == {"fake_tool", "approval_tool"}


# ------------------------------------------------------------------
# Parity
# ------------------------------------------------------------------


def test_adapters_produce_same_names(registry: ToolRegistry) -> None:
    """Both adapters expose the same set of tool names."""
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    mcp_adapter = McpAdapter(registry, mcp)

    def fake_get_service(ctx: Any) -> Any:
        return MagicMock()

    mcp_adapter.register_all(fake_get_service)

    service = MagicMock()
    lg_adapter = LangGraphAdapter(registry)
    lg_tools = lg_adapter.tools(service)

    # MCP tool names are not directly accessible from the mock, but we can
    # verify the count matches.
    assert mcp.tool.call_count == len(lg_tools)
