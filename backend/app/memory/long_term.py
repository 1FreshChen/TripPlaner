from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import SavedItem, UserPreference


class LongTermMemory:
    """Cross-session user preferences and saved item memory."""

    def __init__(self, db_session_factory=None):
        self._db_factory = db_session_factory

    async def get_preferences(self, user_id: uuid.UUID, db: AsyncSession) -> UserPreference:
        result = await db.execute(select(UserPreference).where(UserPreference.user_id == user_id))
        prefs = result.scalar_one_or_none()
        if prefs is None:
            prefs = UserPreference(id=uuid.uuid4(), user_id=user_id)
            db.add(prefs)
            await db.flush()
        return prefs

    async def update_from_trip(
        self,
        user_id: uuid.UUID,
        db: AsyncSession,
        city: str,
        preferences: list[str],
        budget_level: str,
        days: int,
        liked_attractions: Optional[list[str]] = None,
    ) -> None:
        prefs = await self.get_preferences(user_id, db)

        categories = list(prefs.preferred_categories or [])
        seen = set(categories)
        for category in preferences:
            cleaned = category.strip()
            if cleaned and cleaned not in seen:
                categories.append(cleaned)
                seen.add(cleaned)
        prefs.preferred_categories = categories

        profile: dict[str, Any] = dict(prefs.budget_profile or {})
        if budget_level:
            profile[budget_level] = int(profile.get(budget_level, 0)) + 1
        prefs.budget_profile = profile

        prefs.avg_trip_days = (
            round((float(prefs.avg_trip_days) + days) / 2, 1)
            if prefs.avg_trip_days is not None
            else float(days)
        )

        cities = list(prefs.favorite_cities or [])
        if city and city not in cities:
            cities.append(city)
        prefs.favorite_cities = cities[:10]

        await db.flush()

    async def get_saved_items(
        self,
        user_id: uuid.UUID,
        db: AsyncSession,
        item_type: Optional[str] = None,
    ) -> list[SavedItem]:
        query = select(SavedItem).where(SavedItem.user_id == user_id)
        if item_type:
            query = query.where(SavedItem.item_type == item_type)
        result = await db.execute(query.order_by(SavedItem.created_at.desc()).limit(50))
        return list(result.scalars().all())

    async def add_saved_item(
        self,
        user_id: uuid.UUID,
        db: AsyncSession,
        item_type: str,
        item_data: dict[str, Any],
        tags: Optional[list[str]] = None,
        note: Optional[str] = None,
    ) -> SavedItem:
        item = SavedItem(
            id=uuid.uuid4(),
            user_id=user_id,
            item_type=item_type,
            item_data=item_data,
            tags=tags or [],
            note=note,
        )
        db.add(item)
        await db.flush()
        return item
