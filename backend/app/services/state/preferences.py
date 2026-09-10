from ._common import *

class StateServicePreferencesMixin:
    async def get_preferences(self, user_id: str) -> UserPreferenceResponse:
        user_uuid = _parse_uuid(user_id, "user_id")
        pref = await self._db.scalar(select(UserPreference).where(UserPreference.user_id == user_uuid))
        if pref is None:
            return UserPreferenceResponse()
        return UserPreferenceResponse(
            preferred_categories=pref.preferred_categories or [],
            budget_profile=pref.budget_profile or {},
            travel_style=pref.travel_style or "",
            favorite_cities=pref.favorite_cities or [],
        )

    async def update_preferences(self, user_id: str, request: UserPreferenceUpdateRequest) -> UserPreferenceResponse:
        user_uuid = _parse_uuid(user_id, "user_id")
        pref = await self._db.scalar(select(UserPreference).where(UserPreference.user_id == user_uuid))
        if pref is None:
            pref = UserPreference(id=uuid.uuid4(), user_id=user_uuid)
            self._db.add(pref)
        pref.preferred_categories = request.preferred_categories
        pref.budget_profile = request.budget_profile
        pref.travel_style = request.travel_style
        pref.favorite_cities = request.favorite_cities
        await self._db.flush()
        return UserPreferenceResponse(**request.model_dump())

    async def list_saved_items(self, user_id: str) -> list[SavedItemResponse]:
        user_uuid = _parse_uuid(user_id, "user_id")
        items = (
            await self._db.execute(
                select(SavedItem)
                .where(SavedItem.user_id == user_uuid)
                .order_by(desc(SavedItem.created_at))
                .limit(50)
            )
        ).scalars().all()
        return [self._saved_item_response(item) for item in items]

    async def create_saved_item(
        self,
        user_id: str,
        request: SavedItemCreateRequest,
    ) -> SavedItemResponse:
        item = await _compat_symbol("LongTermMemory", LongTermMemory)().add_saved_item(
            user_id=_parse_uuid(user_id, "user_id"),
            db=self._db,
            item_type=request.item_type,
            item_data=request.item_data,
            tags=request.tags,
            note=request.note,
        )
        return self._saved_item_response(item)

    async def delete_saved_item(self, user_id: str, item_id: str) -> None:
        user_uuid = _parse_uuid(user_id, "user_id")
        item_uuid = _parse_uuid(item_id, "item_id")
        item = await self._db.scalar(
            select(SavedItem).where(
                SavedItem.id == item_uuid,
                SavedItem.user_id == user_uuid,
            )
        )
        if item is None:
            raise HTTPException(status_code=404, detail="收藏项不存在")
        await _compat_symbol("LongTermMemory", LongTermMemory)().forget_saved_item(
            user_id=user_uuid,
            item_id=item_uuid,
            db=self._db,
        )
        await self._db.delete(item)
        await self._db.flush()

    @staticmethod
    def _saved_item_response(item: SavedItem) -> SavedItemResponse:
        return SavedItemResponse(
            id=str(item.id),
            item_type=item.item_type,
            item_data=item.item_data,
            tags=item.tags or [],
            note=item.note,
            created_at=_iso(item.created_at),
        )
