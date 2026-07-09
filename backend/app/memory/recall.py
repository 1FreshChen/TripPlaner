from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.long_term import LongTermMemory


class MemoryRecall:
    """Recall relevant long-term memory for a new trip planning request."""

    def __init__(self, long_term_memory: LongTermMemory):
        self._ltm = long_term_memory

    async def recall(
        self,
        user_id: uuid.UUID,
        db: AsyncSession,
        city: str,
        preferences: str,
    ) -> dict[str, Any]:
        prefs = await self._ltm.get_preferences(user_id, db)
        saved_items = await self._ltm.get_saved_items(user_id, db, item_type="attraction")
        saved_attractions = [
            item.item_data
            for item in saved_items
            if (item.item_data or {}).get("city") == city
        ]

        return {
            "preferred_categories": prefs.preferred_categories or [],
            "travel_style": prefs.travel_style or "",
            "budget_profile": prefs.budget_profile or {},
            "favorite_cities": prefs.favorite_cities or [],
            "saved_attractions_in_city": saved_attractions,
            "avg_trip_days": float(prefs.avg_trip_days) if prefs.avg_trip_days is not None else None,
        }
