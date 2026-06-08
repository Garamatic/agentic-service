"""Tool Registry — canonical collection of ToolDefinitions.

Tools are registered at module-import time via ``register_tools(registry)``
functions in each domain module. The registry is immutable once the first
consumer reads from it.
"""

from __future__ import annotations

from .types import ToolDefinition


class ToolRegistry:
    """Collection of ToolDefinitions.

    Thread-safe for read-only usage. Writes (register) are only allowed
    during module-import phase before the first read.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._frozen: bool = False

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition.

        Raises:
            RuntimeError: if the registry has already been read from.
            ValueError: if a tool with the same name already exists.
        """
        if self._frozen:
            raise RuntimeError(
                f"Cannot register tool '{tool.name}' after registry has been read."
            )
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        """Retrieve a tool by name."""
        self._freeze()
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Tool '{name}' is not registered.") from exc

    def all_tools(self, group: str | None = None) -> list[ToolDefinition]:
        """Return all tools, optionally filtered by group."""
        self._freeze()
        tools = list(self._tools.values())
        if group is not None:
            tools = [t for t in tools if t.group == group]
        return tools

    def names(self) -> list[str]:
        """Return all registered tool names."""
        self._freeze()
        return list(self._tools.keys())

    def _freeze(self) -> None:
        """Lock the registry for writes."""
        self._frozen = True


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_REGISTRY: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Return the global ToolRegistry singleton.

    Creates the registry on first call.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ToolRegistry()
    return _REGISTRY
