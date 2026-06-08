"""Tool Registry — unified tool definitions for MCP and LangGraph.

Register tools once in a domain module, then expose them via adapters.
"""

from .adapters import LangGraphAdapter, McpAdapter
from .registry import ToolRegistry, get_registry
from .types import ToolDefinition, ToolResult

__all__ = [
    "ToolDefinition",
    "ToolResult",
    "ToolRegistry",
    "get_registry",
    "McpAdapter",
    "LangGraphAdapter",
]
