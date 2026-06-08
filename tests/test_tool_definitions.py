"""Tests for tool definitions in domain modules."""

from __future__ import annotations

import inspect

from pydantic import BaseModel

from agentic_service.tools.types import ToolDefinition

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _all_tool_definitions() -> list[ToolDefinition]:
    """Import all tool modules (triggering auto-registration) and return
    every registered ToolDefinition.
    """
    # Import all tool modules to trigger auto-registration
    from agentic_service.tools import (  # noqa: F401
        customer_tools,
        email_tools,
        invoice_tools,
        system_tools,
        ticket_tools,
    )
    from agentic_service.tools.registry import get_registry

    registry = get_registry()
    return registry.all_tools()


# ------------------------------------------------------------------
# Schema validation
# ------------------------------------------------------------------


def test_all_tools_have_valid_schemas() -> None:
    for tool in _all_tool_definitions():
        assert issubclass(tool.schema, BaseModel), (
            f"Tool '{tool.name}' schema is not a Pydantic BaseModel"
        )


def test_all_tools_have_handlers() -> None:
    for tool in _all_tool_definitions():
        assert callable(tool.handler), f"Tool '{tool.name}' handler is not callable"
        assert inspect.iscoroutinefunction(tool.handler), (
            f"Tool '{tool.name}' handler is not async"
        )


def test_all_tools_have_descriptions() -> None:
    for tool in _all_tool_definitions():
        assert tool.description, f"Tool '{tool.name}' has empty description"
        assert isinstance(tool.description, str)


# ------------------------------------------------------------------
# Naming
# ------------------------------------------------------------------


def test_all_tools_names_unique() -> None:
    names = [tool.name for tool in _all_tool_definitions()]
    assert len(names) == len(set(names)), f"Duplicate tool names: {names}"


def test_all_tools_names_snake_case() -> None:
    for tool in _all_tool_definitions():
        assert " " not in tool.name, f"Tool '{tool.name}' name contains spaces"
        assert tool.name == tool.name.lower(), f"Tool '{tool.name}' name is not lowercase"


# ------------------------------------------------------------------
# Groups
# ------------------------------------------------------------------


def test_all_tools_have_known_groups() -> None:
    known_groups = {"ticket", "invoice", "email", "customer", "system", "general"}
    for tool in _all_tool_definitions():
        assert tool.group in known_groups, (
            f"Tool '{tool.name}' has unknown group '{tool.group}'"
        )


# ------------------------------------------------------------------
# Approval threshold
# ------------------------------------------------------------------


def test_approval_threshold_is_non_negative_or_none() -> None:
    for tool in _all_tool_definitions():
        if tool.approval_threshold is not None:
            assert tool.approval_threshold >= 0, (
                f"Tool '{tool.name}' has negative approval_threshold"
            )


# ------------------------------------------------------------------
# Expected tool count
# ------------------------------------------------------------------


def test_expected_tool_count() -> None:
    tools = _all_tool_definitions()
    # 4 ticket + 2 invoice + 1 email + 1 customer + 2 system = 10
    # (8 MCP tools + 2 extra LangGraph tools: request_human_approval, approve_request)
    assert len(tools) == 10, f"Expected 10 tools, got {len(tools)}: {[t.name for t in tools]}"
