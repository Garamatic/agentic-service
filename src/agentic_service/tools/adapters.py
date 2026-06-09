"""Adapters — expose ToolRegistry entries to MCP and LangGraph.

McpAdapter registers tools on a FastMCP instance.
LangGraphAdapter creates StructuredTool instances for use with LangGraph.

Both adapters are thin and stateless — they only know how to
transform a ToolDefinition into its native framework representation.
"""

from __future__ import annotations

from typing import Any, cast

import pydantic
from fastmcp import Context
from langchain_core.tools import StructuredTool

from ..approval import ApprovalRequiredError
from ..service import (
    GaramaticService,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from .interceptors import with_approval
from .registry import ToolRegistry
from .types import ToolDefinition

# ------------------------------------------------------------------
# MCP adapter
# ------------------------------------------------------------------


def _create_mcp_wrapper(
    tool: ToolDefinition,
    get_service: Any,  # callable that extracts service from ctx
) -> Any:
    """Build a wrapper function suitable for @mcp.tool().

    The wrapper signature matches the tool schema so that FastMCP can
    introspect it for the MCP tool definition. At runtime it extracts
    the service from the MCP Context, validates arguments, applies the
    approval interceptor, and returns a structured dict.
    """
    import inspect

    schema = tool.schema
    sig = inspect.signature(schema)
    params = list(sig.parameters.keys())[1:]  # skip 'self'

    # Build a dynamic function with the correct signature
    async def wrapper(*args: Any, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        # If the model hallucinated a dict ``ctx``, kwargs may also contain
        # it (FastMCP sometimes passes both).  Strip it so schema validation
        # does not fail on an extra field.
        kwargs.pop("ctx", None)
        service = get_service(ctx)

        # Report progress before starting (if client supports it)
        if hasattr(ctx, "report_progress"):
            try:
                ctx.report_progress(0.0, 1.0)
            except Exception:
                pass

        try:
            validated = schema(**kwargs)
            validated_dict = validated.model_dump()
            handler = with_approval(tool)
            result = cast(dict[str, Any], await handler(service, **validated_dict))

            # Report progress after completion
            if hasattr(ctx, "report_progress"):
                try:
                    ctx.report_progress(1.0, 1.0)
                except Exception:
                    pass

            return result
        except pydantic.ValidationError as e:
            return {"status": "validation_error", "error": str(e)}
        except ValidationError as e:
            return {"status": "validation_error", "error": str(e)}
        except NotFoundError as e:
            return {"status": "not_found", "error": str(e)}
        except ApprovalRequiredError as e:
            return {
                "status": "approval_required",
                "error": str(e),
                "approval_id": e.approval_id,
            }
        except ServiceUnavailableError as e:
            return {"status": "error", "error": f"Service unavailable: {e}"}
        except Exception as e:
            return {"status": "error", "error": f"Failed: {e}"}

    # Preserve the schema signature for introspection
    wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter(
                name=p,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=sig.parameters[p].default,
                annotation=sig.parameters[p].annotation,
            )
            for p in params
        ]
        + [
            inspect.Parameter(
                name="ctx",
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty,
                annotation=Context,
            )
        ]
    )
    # Set __annotations__ to match the signature so that
    # typing.get_type_hints() and FastMCP's function parser stay consistent.
    wrapper.__annotations__ = {
        p: sig.parameters[p].annotation
        for p in params
        if sig.parameters[p].annotation is not inspect.Parameter.empty
    }
    wrapper.__annotations__["ctx"] = Context
    wrapper.__annotations__["return"] = dict[str, Any]
    wrapper.__name__ = tool.name
    wrapper.__doc__ = tool.description
    return wrapper


class McpAdapter:
    """Auto-registers all tools from a registry onto a FastMCP instance."""

    def __init__(self, registry: ToolRegistry, mcp: Any) -> None:
        self._registry = registry
        self._mcp = mcp

    def register_all(self, get_service: Any) -> None:
        """Register every tool in the registry with the MCP server."""
        for tool in self._registry.all_tools():
            wrapper = _create_mcp_wrapper(tool, get_service)
            kwargs: dict[str, Any] = {}
            if tool.annotations is not None:
                kwargs["annotations"] = tool.annotations
            self._mcp.tool(name=tool.name, description=tool.description, **kwargs)(wrapper)


# ------------------------------------------------------------------
# LangGraph adapter
# ------------------------------------------------------------------


def _create_langgraph_wrapper(
    tool: ToolDefinition,
    service: GaramaticService,
) -> Any:
    """Build a wrapper coroutine for StructuredTool."""

    async def wrapper(**kwargs: Any) -> dict[str, Any]:
        # Small models sometimes hallucinate a ``ctx`` or ``data`` field.
        # Remove ctx so schema validation does not fail on an extra field.
        kwargs.pop("ctx", None)
        try:
            validated = tool.schema(**kwargs)
            validated_dict = validated.model_dump()
            handler = with_approval(tool)
            return cast(dict[str, Any], await handler(service, **validated_dict))
        except pydantic.ValidationError as e:
            return {"status": "validation_error", "error": str(e)}
        except ValidationError as e:
            return {"status": "validation_error", "error": str(e)}
        except NotFoundError as e:
            return {"status": "not_found", "error": str(e)}
        except ApprovalRequiredError as e:
            return {
                "status": "approval_required",
                "error": str(e),
                "approval_id": e.approval_id,
            }
        except ServiceUnavailableError as e:
            return {"status": "error", "error": f"Service unavailable: {e}"}
        except Exception as e:
            return {"status": "error", "error": f"Failed: {e}"}

    wrapper.__name__ = tool.name
    wrapper.__doc__ = tool.description
    return wrapper


class LangGraphAdapter:
    """Creates StructuredTool instances from a registry for LangGraph."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def tools(self, service: GaramaticService) -> list[StructuredTool]:
        """Return all tools as LangGraph StructuredTool instances."""
        result: list[StructuredTool] = []
        for tool in self._registry.all_tools():
            wrapper = _create_langgraph_wrapper(tool, service)
            result.append(
                StructuredTool(
                    name=tool.name,
                    description=tool.description,
                    args_schema=tool.schema,
                    coroutine=wrapper,
                )
            )
        return result
