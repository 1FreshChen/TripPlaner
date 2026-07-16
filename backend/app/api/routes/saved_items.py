from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_state_service
from app.models.schemas import SavedItemCreateRequest, SavedItemResponse
from app.services.state_service import StateService


router = APIRouter(prefix="/saved-items", tags=["saved-items"])


@router.get("", response_model=list[SavedItemResponse])
async def list_saved_items(
    session_id: str = Query(..., description="会话令牌"),
    state: StateService = Depends(get_state_service),
) -> list[SavedItemResponse]:
    user = await state._get_user_by_session(session_id)
    return await state.list_saved_items(str(user.id))


@router.post("", response_model=SavedItemResponse, status_code=status.HTTP_201_CREATED)
async def create_saved_item(
    request: SavedItemCreateRequest,
    session_id: str = Query(..., description="会话令牌"),
    state: StateService = Depends(get_state_service),
) -> SavedItemResponse:
    user = await state._get_user_by_session(session_id)
    return await state.create_saved_item(str(user.id), request)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_item(
    item_id: str,
    session_id: str = Query(..., description="会话令牌"),
    state: StateService = Depends(get_state_service),
) -> None:
    user = await state._get_user_by_session(session_id)
    await state.delete_saved_item(str(user.id), item_id)
