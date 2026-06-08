"""Interceptors for tool call pipelines.

Provides reusable wrappers that can be applied before the actual
handler is invoked. The primary interceptor is the approval gate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from ..approval import ApprovalRequiredError, generate_approval_id
from ..config import get_settings
from ..service import GaramaticService
from .types import ToolDefinition

# ------------------------------------------------------------------
# Approval interceptor
# ------------------------------------------------------------------


def with_approval(definition: ToolDefinition) -> Callable[..., Any]:
    """Wrap a tool handler with approval threshold checks.

    If ``definition.approval_threshold`` is None, the handler is
    returned unchanged.

    Otherwise, the wrapper inspects the call arguments for a monetary
    amount (``billable_amount`` or ``amount``). If that amount exceeds
    the runtime approval threshold (from settings) and no valid
    ``approval_id`` is provided, an :class:`ApprovalRequiredError` is
    raised.
    """
    if definition.approval_threshold is None:
        return definition.handler

    async def wrapper(
        service: GaramaticService, **kwargs: Any
    ) -> dict[str, Any]:
        amount = kwargs.get("billable_amount", kwargs.get("amount", 0))
        threshold = get_settings().approval_threshold
        if amount and amount > threshold:
            approval_id = kwargs.get("approval_id")
            approval_store = service.approval_store
            if approval_id is None:
                if approval_store is not None:
                    new_id = await approval_store.create_request(
                        f"{definition.name}:{kwargs.get('ticket_id', '?')}", amount
                    )
                else:
                    new_id = generate_approval_id()
                raise ApprovalRequiredError(
                    f"Amount €{amount:.2f} exceeds approval threshold "
                    f"of €{threshold:.2f}. "
                    f"Create an approval request and retry with "
                    f"approval_id='{new_id}'.",
                    approval_id=new_id,
                    amount=amount,
                )
            if approval_store is None or not await approval_store.is_approved(approval_id):
                raise ApprovalRequiredError(
                    f"Approval ID '{approval_id}' is not valid or has expired. "
                    "Request a new approval and try again.",
                    amount=amount,
                )

        return cast(dict[str, Any], await definition.handler(service, **kwargs))

    return wrapper
