"""Idempotency store for event processing.

Prevents duplicate processing of events that may be redelivered
by RabbitMQ or retried due to transient failures.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger()


# ------------------------------------------------------------------
# Idempotency key helpers
# ------------------------------------------------------------------


def idempotency_key_for_invoice(ticket_id: str, amount: float) -> str:
    """Generate a deterministic idempotency key for invoice creation."""
    raw = f"invoice.create_requested:{ticket_id}:{amount:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def idempotency_key_for_email(to_email: str, subject: str) -> str:
    """Generate a deterministic idempotency key for email sending."""
    raw = f"email.send:{to_email}:{subject.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def idempotency_key_for_event(event_type: str, payload: dict[str, Any]) -> str | None:
    """Extract or generate an idempotency key from an event payload."""
    # Prefer explicit key from the publisher
    explicit = payload.get("idempotency_key")
    if explicit:
        return explicit  # type: ignore[no-any-return]

    # Fall back to deterministic key based on event type
    if event_type == "invoice.create_requested":
        ticket_id = payload.get("ticket_id", "")
        amount = payload.get("amount", 0.0)
        return idempotency_key_for_invoice(str(ticket_id), float(amount))

    if event_type == "email.send":
        to_email = payload.get("to_email", "")
        subject = payload.get("subject", "")
        return idempotency_key_for_email(str(to_email), str(subject))

    return None


# ------------------------------------------------------------------
# IdempotencyStore
# ------------------------------------------------------------------


class IdempotencyStore:
    """Persistent idempotency store backed by SQLite.

    Tracks processed event IDs so duplicate deliveries are skipped.
    Thread-safe for single-process async usage.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or ":memory:"
        self._conn: Any = None

    async def initialize(self) -> None:
        """Open database connection and create table."""
        import aiosqlite

        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        await self._conn.commit()
        logger.info("idempotency_store.initialized", db=self._db_path)

    async def is_processed(self, key: str) -> bool:
        """Check if a key has already been processed."""
        if self._conn is None:
            return False
        cursor = await self._conn.execute("SELECT 1 FROM idempotency WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row is not None

    async def mark_processed(self, key: str, status: str = "completed") -> None:
        """Mark a key as processed."""
        if self._conn is None:
            return
        await self._conn.execute(
            "INSERT OR IGNORE INTO idempotency (key, processed_at, status) VALUES (?, ?, ?)",
            (key, datetime.now(UTC).isoformat(), status),
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
