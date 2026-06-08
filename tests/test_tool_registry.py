"""Tests for the ToolRegistry."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentic_service.tools.registry import ToolRegistry
from agentic_service.tools.types import ToolDefinition

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


class FakeSchema(BaseModel):
    """Fake schema for testing."""

    name: str


async def fake_handler(service: object, **kwargs: object) -> dict:
    return {"status": "success", "name": kwargs.get("name")}


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def test_register_tool() -> None:
    registry = ToolRegistry()
    tool = ToolDefinition(
        name="fake_tool",
        schema=FakeSchema,
        handler=fake_handler,
        description="A fake tool for testing.",
    )
    registry.register(tool)
    assert registry.names() == ["fake_tool"]


def test_register_multiple_tools() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="tool_a",
            schema=FakeSchema,
            handler=fake_handler,
            description="Tool A",
        )
    )
    registry.register(
        ToolDefinition(
            name="tool_b",
            schema=FakeSchema,
            handler=fake_handler,
            description="Tool B",
        )
    )
    names = registry.names()
    assert "tool_a" in names
    assert "tool_b" in names
    assert len(names) == 2


def test_register_duplicate_name_raises() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="duplicate",
            schema=FakeSchema,
            handler=fake_handler,
            description="First",
        )
    )
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            ToolDefinition(
                name="duplicate",
                schema=FakeSchema,
                handler=fake_handler,
                description="Second",
            )
        )


def test_register_after_freeze_raises() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="tool",
            schema=FakeSchema,
            handler=fake_handler,
            description="Tool",
        )
    )
    # Trigger freeze
    registry.names()
    with pytest.raises(RuntimeError, match="Cannot register"):
        registry.register(
            ToolDefinition(
                name="tool2",
                schema=FakeSchema,
                handler=fake_handler,
                description="Tool2",
            )
        )


# ------------------------------------------------------------------
# Lookup
# ------------------------------------------------------------------


def test_get_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="lookup",
            schema=FakeSchema,
            handler=fake_handler,
            description="Lookup tool",
        )
    )
    tool = registry.get("lookup")
    assert tool.name == "lookup"


def test_get_missing_tool() -> None:
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="not registered"):
        registry.get("missing")


# ------------------------------------------------------------------
# Listing
# ------------------------------------------------------------------


def test_list_all() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="a",
            schema=FakeSchema,
            handler=fake_handler,
            description="A",
            group="ticket",
        )
    )
    registry.register(
        ToolDefinition(
            name="b",
            schema=FakeSchema,
            handler=fake_handler,
            description="B",
            group="invoice",
        )
    )
    all_tools = registry.all_tools()
    assert len(all_tools) == 2


def test_list_by_group() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="a",
            schema=FakeSchema,
            handler=fake_handler,
            description="A",
            group="ticket",
        )
    )
    registry.register(
        ToolDefinition(
            name="b",
            schema=FakeSchema,
            handler=fake_handler,
            description="B",
            group="invoice",
        )
    )
    ticket_tools = registry.all_tools(group="ticket")
    assert len(ticket_tools) == 1
    assert ticket_tools[0].name == "a"


def test_list_nonexistent_group() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="a",
            schema=FakeSchema,
            handler=fake_handler,
            description="A",
            group="ticket",
        )
    )
    assert registry.all_tools(group="nonexistent") == []


# ------------------------------------------------------------------
# Names
# ------------------------------------------------------------------


def test_names_empty() -> None:
    registry = ToolRegistry()
    assert registry.names() == []


def test_names_populated() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="x",
            schema=FakeSchema,
            handler=fake_handler,
            description="X",
        )
    )
    registry.register(
        ToolDefinition(
            name="y",
            schema=FakeSchema,
            handler=fake_handler,
            description="Y",
        )
    )
    assert set(registry.names()) == {"x", "y"}
