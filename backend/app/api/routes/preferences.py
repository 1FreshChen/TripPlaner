from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_state_service
from app.models.schemas import UserPreferenceResponse, UserPreferenceUpdateRequest
from app.services.state_service import StateService

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("", response_model=UserPreferenceResponse)
async def get_preferences(
    session_id: str = Query(..., description="会话令牌"),
    state: StateService = Depends(get_state_service),
) -> UserPreferenceResponse:
    user = await state._get_user_by_session(session_id)
    return await state.get_preferences(str(user.id))


@router.put("", response_model=UserPreferenceResponse)
async def update_preferences(
    request: UserPreferenceUpdateRequest,
    session_id: str = Query(..., description="会话令牌"),
    state: StateService = Depends(get_state_service),
) -> UserPreferenceResponse:
    user = await state._get_user_by_session(session_id)
    return await state.update_preferences(str(user.id), request)
