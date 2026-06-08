"""Email operations — queueing via message broker."""

from __future__ import annotations

import contextlib

import structlog

from ..idempotency import idempotency_key_for_email
from ..ports import MessageQueuePort
from .base import (
    BaseService,
    EmailQueueResult,
    ValidationError,
    _redact_email,
    generate_request_id,
    validate_email,
)

logger = structlog.get_logger()


class EmailService(BaseService):
    """All email-related business logic."""

    def __init__(self, mq_publisher: MessageQueuePort | None) -> None:
        self._mq = mq_publisher

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._mq is not None:
            with contextlib.suppress(Exception):
                await self._mq.close()

    # ------------------------------------------------------------------
    # Messaging operations
    # ------------------------------------------------------------------

    async def send_email(
        self,
        *,
        to_email: str,
        subject: str,
        body: str,
        from_name: str = "Garamatic Support",
        from_email: str = "support@garamatic.com",
    ) -> EmailQueueResult:
        validate_email(to_email)
        if not subject or not subject.strip():
            raise ValidationError("Email subject is required")
        rid = generate_request_id()
        logger.info(
            "service.send_email",
            to=_redact_email(to_email),
            subject=subject,
            request_id=rid,
        )

        if self._mq is None:
            return EmailQueueResult(
                status="failed",
                to_email=to_email,
                subject=subject,
                message="Email service unavailable",
            )

        payload = {
            "event_type": "email.send",
            "to_email": to_email,
            "from_email": from_email,
            "from_name": from_name,
            "subject": subject,
            "body_html": body,
            "idempotency_key": idempotency_key_for_email(to_email, subject),
        }

        try:
            await self._mq.publish("email.send", payload)
            return EmailQueueResult(
                status="queued",
                to_email=to_email,
                subject=subject,
                message="Email queued for sending. Will be delivered within 1 minute.",
            )
        except Exception as e:
            logger.error(
                "service.send_email.failed",
                to=_redact_email(to_email),
                error=str(e),
                request_id=rid,
            )
            return EmailQueueResult(
                status="failed",
                to_email=to_email,
                subject=subject,
                message=f"Failed to queue email: {e}",
            )
