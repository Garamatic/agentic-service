"""Approval store for human-in-the-loop invoice approval.

Tracks approval requests and grants for high-value operations
that exceed the configurable threshold.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger()

APPROVAL_TTL_MINUTES = 30


def generate_approval_id() -> str:
    """Generate a unique, human-readable approval ID."""
    short = uuid.uuid4().hex[:12]
    return f"ap-{short}"


class ApprovalRequiredError(Exception):
    """Raised when an operation exceeds the approval threshold.

    The caller must obtain an approval ID via `request_human_approval`
    and retry the operation with that ID.
    """

    def __init__(self, message: str, approval_id: str | None = None, amount: float | None = None):
        self.approval_id = approval_id
        self.amount = amount
        super().__init__(message)


class ApprovalStore:
    """Persistent store for approval requests and grants.

    Tracks pending/approved/rejected status with TTL expiry.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or ":memory:"
        self._conn: Any = None

    async def initialize(self) -> None:
        """Open database connection and create table."""
        import aiosqlite

        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                estimated_amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                approved_at TEXT
            )
            """
        )
        await self._conn.commit()
        logger.info("approval_store.initialized", db=self._db_path)

    async def create_request(
        self,
        action: str,
        estimated_amount: float,
    ) -> str:
        """Create a new approval request and return its ID."""
        approval_id = generate_approval_id()
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=APPROVAL_TTL_MINUTES)

        await self._conn.execute(
            """
            INSERT INTO approvals (id, action, estimated_amount, status, created_at, expires_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (approval_id, action, estimated_amount, now.isoformat(), expires_at.isoformat()),
        )
        await self._conn.commit()
        logger.info(
            "approval.request_created",
            approval_id=approval_id,
            action=action,
            amount=estimated_amount,
        )
        return approval_id

    async def is_approved(self, approval_id: str) -> bool:
        """Check if an approval ID is approved and not expired."""
        if self._conn is None:
            return False
        cursor = await self._conn.execute(
            """
            SELECT status, expires_at FROM approvals WHERE id = ?
            """,
            (approval_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        status, expires_at_str = row
        if status != "approved":
            return False
        expires_at = datetime.fromisoformat(expires_at_str)
        return not datetime.now(UTC) > expires_at

    async def approve(self, approval_id: str) -> bool:
        """Mark an approval request as approved."""
        if self._conn is None:
            return False
        cursor = await self._conn.execute(
            """
            UPDATE approvals SET status = 'approved', approved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (datetime.now(UTC).isoformat(), approval_id),
        )
        await self._conn.commit()
        approved = cursor.rowcount > 0
        if approved:
            logger.info("approval.approved", approval_id=approval_id)
        return approved

    async def reject(self, approval_id: str) -> bool:
        """Mark an approval request as rejected."""
        if self._conn is None:
            return False
        cursor = await self._conn.execute(
            "UPDATE approvals SET status = 'rejected' WHERE id = ? AND status = 'pending'",
            (approval_id,),
        )
        await self._conn.commit()
        rejected = cursor.rowcount > 0
        if rejected:
            logger.info("approval.rejected", approval_id=approval_id)
        return rejected

    async def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
