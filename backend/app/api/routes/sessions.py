from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import get_state_service
from app.models.schemas import SessionCreateResponse, SessionDetailResponse
from app.services.state_service import StateService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_session(state: StateService = Depends(get_state_service)) -> SessionCreateResponse:
    return await state.create_session()


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: str,
    state: StateService = Depends(get_state_service),
) -> SessionDetailResponse:
    return await state.get_session(session_id)
