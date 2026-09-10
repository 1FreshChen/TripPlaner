from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.memory.embeddings import EmbeddingService, build_embedding_service
from app.models.db_models import MemoryEntry


logger = logging.getLogger(__name__)


class VectorMemoryStore:
    """Persistent, user-scoped semantic memory backed by PostgreSQL pgvector."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        *,
        top_k: int = 5,
        min_similarity: float = 0.3,
        max_entries_per_user: int = 1000,
    ) -> None:
        self._embeddings = embedding_service
        self._top_k = top_k
        self._min_similarity = min_similarity
        self._max_entries_per_user = max_entries_per_user

    async def remember_trip(
        self,
        *,
        user_id: uuid.UUID,
        db: AsyncSession,
        city: str,
        preferences: list[str],
        budget_level: str,
        days: int,
        source_id: uuid.UUID | None = None,
    ) -> bool:
        content = self._trip_content(city, preferences, budget_level, days)
        source_key = (
            f"trip:{source_id}"
            if source_id is not None
            else "trip:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        return await self.remember(
            user_id=user_id,
            db=db,
            source_key=source_key,
            memory_type="trip",
            content=content,
            metadata={
                "city": city,
                "preferences": preferences,
                "budget_level": budget_level,
                "days": days,
                "source_id": str(source_id) if source_id is not None else None,
            },
        )

    async def remember_saved_item(
        self,
        *,
        user_id: uuid.UUID,
        db: AsyncSession,
        item_id: uuid.UUID,
        item_type: str,
        item_data: dict[str, Any],
        tags: list[str],
        note: str | None,
    ) -> bool:
        name = item_data.get("name") or item_data.get("title") or "未命名"
        city = item_data.get("city") or ""
        parts = [f"用户收藏了{item_type}：{name}"]
        if city:
            parts.append(f"城市：{city}")
        if tags:
            parts.append(f"标签：{'、'.join(tags)}")
        if note:
            parts.append(f"备注：{note}")
        return await self.remember(
            user_id=user_id,
            db=db,
            source_key=f"saved_item:{item_id}",
            memory_type="saved_item",
            content="；".join(parts) + "。",
            metadata={
                "item_id": str(item_id),
                "item_type": item_type,
                "item_data": item_data,
                "tags": tags,
                "note": note,
            },
        )

    async def remember(
        self,
        *,
        user_id: uuid.UUID,
        db: AsyncSession,
        source_key: str,
        memory_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> bool:
        try:
            embedding = await self._embeddings.embed(content)
            statement = (
                insert(MemoryEntry)
                .values(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    source_key=source_key,
                    memory_type=memory_type,
                    content=content,
                    metadata_json=metadata,
                    embedding=embedding,
                    embedding_model=self._embeddings.model,
                )
                .on_conflict_do_update(
                    constraint="uq_memory_entries_user_source",
                    set_={
                        "memory_type": memory_type,
                        "content": content,
                        "metadata_json": metadata,
                        "embedding": embedding,
                        "embedding_model": self._embeddings.model,
                        "updated_at": func.now(),
                    },
                )
            )
            async with _savepoint(db):
                await db.execute(statement)
                await self._prune(user_id, db)
            return True
        except Exception:
            logger.warning(
                "vector memory write skipped",
                extra={"memory_type": memory_type, "source_key": source_key},
                exc_info=True,
            )
            return False

    async def search(
        self,
        *,
        user_id: uuid.UUID,
        db: AsyncSession,
        query: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            query_embedding = await self._embeddings.embed(query)
            distance = MemoryEntry.embedding.cosine_distance(query_embedding)
            max_distance = 1.0 - self._min_similarity
            statement = (
                select(MemoryEntry, distance.label("distance"))
                .where(
                    MemoryEntry.user_id == user_id,
                    distance <= max_distance,
                )
                .order_by(distance, desc(MemoryEntry.created_at))
                .limit(limit or self._top_k)
            )
            async with _savepoint(db):
                rows = (await db.execute(statement)).all()
            now = datetime.now(timezone.utc)
            recalled: list[dict[str, Any]] = []
            for entry, raw_distance in rows:
                entry.access_count += 1
                entry.last_accessed_at = now
                recalled.append(
                    {
                        "id": str(entry.id),
                        "memory_type": entry.memory_type,
                        "content": entry.content,
                        "metadata": entry.metadata_json or {},
                        "similarity": round(max(-1.0, min(1.0, 1.0 - float(raw_distance))), 4),
                        "created_at": _iso(entry.created_at),
                    }
                )
            return recalled
        except Exception:
            logger.warning("vector memory search skipped", exc_info=True)
            return []

    async def forget_source(
        self,
        *,
        user_id: uuid.UUID,
        db: AsyncSession,
        source_key: str,
    ) -> bool:
        try:
            async with _savepoint(db):
                await db.execute(
                    delete(MemoryEntry).where(
                        MemoryEntry.user_id == user_id,
                        MemoryEntry.source_key == source_key,
                    )
                )
            return True
        except Exception:
            logger.warning(
                "vector memory delete skipped",
                extra={"source_key": source_key},
                exc_info=True,
            )
            return False

    async def _prune(self, user_id: uuid.UUID, db: AsyncSession) -> None:
        stale_ids = (
            select(MemoryEntry.id)
            .where(MemoryEntry.user_id == user_id)
            .order_by(desc(MemoryEntry.created_at), desc(MemoryEntry.id))
            .offset(self._max_entries_per_user)
        )
        await db.execute(delete(MemoryEntry).where(MemoryEntry.id.in_(stale_ids)))

    @staticmethod
    def _trip_content(city: str, preferences: list[str], budget_level: str, days: int) -> str:
        preference_text = "、".join(preferences) if preferences else "无明确偏好"
        return f"用户规划了{city}{days}日行程；偏好：{preference_text}；预算：{budget_level}。"


def build_vector_memory_store(settings: Settings | None = None) -> VectorMemoryStore | None:
    settings = settings or get_settings()
    embedding_service = build_embedding_service(settings)
    if embedding_service is None:
        return None
    return VectorMemoryStore(
        embedding_service,
        top_k=settings.vector_memory_top_k,
        min_similarity=settings.vector_memory_min_similarity,
        max_entries_per_user=settings.vector_memory_max_entries_per_user,
    )


@asynccontextmanager
async def _savepoint(db: AsyncSession) -> AsyncIterator[None]:
    begin_nested = getattr(db, "begin_nested", None)
    if begin_nested is None:
        yield
        return
    async with begin_nested():
        yield


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
