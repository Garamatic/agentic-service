"""Thread metadata storage for conversation history.

Stores thread titles, timestamps, and message counts alongside
the LangGraph checkpoint database. This lets us list and manage
conversations without parsing checkpoint blobs.
"""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite


class ThreadStore:
    """SQLite-backed thread metadata store.

    Creates a `thread_metadata` table in the same database as the
    LangGraph checkpointer. Used to list, label, and delete threads.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._initialized = False

    async def _ensure_table(self) -> None:
        """Create the thread_metadata table if it doesn't exist."""
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_metadata (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT 'New conversation',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def upsert(
        self,
        thread_id: str,
        title: str | None = None,
        message_count: int | None = None,
    ) -> None:
        """Insert or update a thread's metadata."""
        await self._ensure_table()
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO thread_metadata (thread_id, title, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    title = COALESCE(excluded.title, thread_metadata.title),
                    updated_at = excluded.updated_at,
                    message_count = COALESCE(excluded.message_count, thread_metadata.message_count)
                """,
                (thread_id, title or "New conversation", now, now, message_count or 0),
            )
            await db.commit()

    async def list_threads(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """List threads ordered by most recent activity."""
        await self._ensure_table()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT thread_id, title, created_at, updated_at, message_count
                FROM thread_metadata
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "thread_id": r["thread_id"],
                    "title": r["title"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "message_count": r["message_count"],
                }
                for r in rows
            ]

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        """Get a single thread's metadata."""
        await self._ensure_table()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM thread_metadata WHERE thread_id = ?",
                (thread_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {
                "thread_id": row["thread_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": row["message_count"],
            }

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete a thread's metadata. Returns True if deleted."""
        await self._ensure_table()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM thread_metadata WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_title(self, thread_id: str, title: str) -> bool:
        """Update a thread's title."""
        await self._ensure_table()
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "UPDATE thread_metadata SET title = ?, updated_at = ? WHERE thread_id = ?",
                (title, time.time(), thread_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_messages(self, thread_id: str) -> list[dict[str, Any]]:
        """Extract messages from the LangGraph checkpoint for a thread.

        This reads the checkpoint blob directly — it's a best-effort
        operation that may break if LangGraph changes its format.
        """
        await self._ensure_table()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT checkpoint
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY checkpoint_id DESC
                LIMIT 1
                """,
                (thread_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return []

            try:
                checkpoint = json.loads(row["checkpoint"])
                # LangGraph checkpoint format: {"channel_values": {"messages": [...]}}
                channel_values = checkpoint.get("channel_values", {})
                messages = channel_values.get("messages", [])
                result = []
                for msg in messages:
                    if isinstance(msg, dict):
                        role = "user" if msg.get("type") == "human" else (
                            "agent" if msg.get("type") == "ai" else "system"
                        )
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = str(content)
                        # Skip system messages and empty tool calls
                        if role == "system" or not content:
                            continue
                        result.append({
                            "role": role,
                            "content": content,
                        })
                    elif isinstance(msg, list) and len(msg) >= 2:
                        # Tuple format: [type, content]
                        role = "user" if msg[0] == "human" else (
                            "agent" if msg[0] == "ai" else "system"
                        )
                        if role == "system" or not msg[1]:
                            continue
                        result.append({"role": role, "content": msg[1]})
                return result
            except Exception:
                return []
