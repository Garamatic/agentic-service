"""Core types for the Tool Registry.

Defines ToolResult and ToolDefinition — the canonical contract between
business-logic handlers and the MCP / LangGraph adapter layers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired

from pydantic import BaseModel
from typing_extensions import TypedDict

# ------------------------------------------------------------------
# Tool result shape
# ------------------------------------------------------------------


class ToolResult(TypedDict):
    """Structured response every tool handler must return.

    Adapters (MCP, LangGraph) propagate this dict to the agent
    without modification. The ``status`` field drives the agent's
    next decision.
    """

    status: Literal[
        "success",
        "error",
        "validation_error",
        "not_found",
        "approval_required",
    ]
    error: NotRequired[str]
    # Tool-specific fields are added by the handler.


# ------------------------------------------------------------------
# Tool definition
# ------------------------------------------------------------------


@dataclass
class ToolDefinition:
    """Canonical declaration of a tool.

    Registered once in a domain module, then exposed via adapters.
    """

    name: str
    schema: type[BaseModel]
    handler: Callable[..., Any]  # async (service, **kwargs) -> ToolResult
    description: str
    group: str = "general"
    approval_threshold: float | None = None
    # Optional: extra metadata for future phases (telemetry, ACL, etc.)
    meta: dict[str, Any] = field(default_factory=dict)
