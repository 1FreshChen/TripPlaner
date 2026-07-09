from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import ConversationMessage


class ShortTermMemory:
    """Current-session conversation memory backed by a sliding in-memory window."""

    def __init__(self, session_id: str | uuid.UUID, max_messages: int = 20):
        self.session_id = uuid.UUID(str(session_id))
        self.max_messages = max_messages
        self._buffer: list[dict[str, Any]] = []

    async def add(
        self,
        role: str,
        content: str,
        db: AsyncSession,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        tool_name: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        entry = {
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "tool_name": tool_name,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc),
        }
        self._buffer.append(entry)
        self._trim()

        db.add(
            ConversationMessage(
                id=uuid.uuid4(),
                session_id=self.session_id,
                role=role,
                content=content,
                tool_calls_json=tool_calls,
                tool_name=tool_name,
                metadata_json=metadata or {},
            )
        )

    def get_context(self) -> list[dict[str, str]]:
        return [{"role": message["role"], "content": message["content"]} for message in self._buffer]

    def get_last_n(self, n: int) -> list[dict[str, Any]]:
        if n <= 0:
            return []
        return self._buffer[-n:] if n < len(self._buffer) else self._buffer.copy()

    async def restore_from_db(self, db: AsyncSession) -> None:
        result = await db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == self.session_id)
            .order_by(desc(ConversationMessage.created_at))
            .limit(self.max_messages)
        )
        rows = sorted(result.scalars().all(), key=lambda row: row.created_at)
        self._buffer = [
            {
                "role": row.role,
                "content": row.content,
                "tool_calls": row.tool_calls_json,
                "tool_name": row.tool_name,
                "metadata": row.metadata_json or {},
                "timestamp": row.created_at,
            }
            for row in rows
        ]

    def clear(self) -> None:
        self._buffer.clear()

    def _trim(self) -> None:
        if len(self._buffer) > self.max_messages:
            self._buffer = self._buffer[-self.max_messages :]
